"""V2.5：Rerank 精排（双后端：本地 cross-encoder / LLM 重排）。

产品视角：向量检索和 BM25 都是「先算分再排序」的粗排——它们只看文字和指纹，
不理解内容。Rerank 是最后一道精排：逐条阅读候选卡片，
按「是否真的回答了问题」打 0~10 分，再按分数精选。

双后端（.env 的 RERANK_BACKEND 切换，失败自动降级）：
- local_onnx（推荐）：本地 bge-reranker-v2-m3 的 ONNX 量化版（cross-encoder），
  onnxruntime 纯 CPU 逐对打分，零 API 费用、离线可用；耗时以节点实测为准；
  模型文件放 models/bge-reranker-v2-m3-onnx/（scripts/download_rerank_model.py 一键下载）。
- llm：用已配置的云端大模型逐条打分（原始实现，零本地资源）。

可靠性设计（两后端一致）：
- 没被打分 / 打分非法的候选，沿用粗排名次排在已评分候选之后；
- 任何失败整体回退为粗排顺序，问答不中断；local 失败还会自动降级到 LLM 精排。
"""

from __future__ import annotations

import json
import math
import re
import time
import threading
from dataclasses import dataclass, field

from src.config import AppConfig, get_config
from src.llm import LLMClient
from src.retriever import RetrievedItem

RERANK_SYSTEM_PROMPT = """你是知识库检索的相关度评估器。给定一个用户问题和若干候选知识片段，请对每条候选与问题的相关程度打分。

评分标准：
- 10~8 分：直接回答了问题，或包含问题的关键事实 / 数据
- 7~4 分：与问题主题相关，但不包含直接答案
- 3~0 分：与问题无关，或仅有微弱联系

判断提示：
- 每条候选的「来源」是原始文档文件名。文件名往往包含文档主题（如"心得.rtf"表示这是心得文档、
  "智能客服复盘.md"表示智能客服复盘），可作为判断相关度的参考线索。
- 当正文是列表、片段或缺少上下文（看起来"没头没尾"）时，可结合文件名综合判断它与问题的主题相关性，
  不要仅因正文没有显式出现问题关键词就直接判为 0 分。

输出要求（必须严格遵守）：
1. 只输出一个 JSON 数组，不要多余文字，不要用代码块包裹。
2. 数组元素形如 {"id": "C1", "score": 8}；id 必须与输入中的候选编号一致，每条候选恰好评一次分。"""

RERANK_USER_PROMPT_TEMPLATE = """【用户问题】{question}

【候选知识片段】
{candidates}

请对以上每条候选输出评分 JSON 数组。"""

_MAX_CANDIDATE_CHARS = 300  # 每条候选送给评分器的正文长度（控制成本，标题句已足够判断）


@dataclass
class RerankOutcome:
    """一次精排的完整记录。"""

    items: list[RetrievedItem] = field(default_factory=list)  # 全部候选，按精排分排序
    scores: list[tuple[str, str, float]] = field(default_factory=list)  # (编号, 来源, 分数)
    before_order: list[str] = field(default_factory=list)  # 粗排顺序（供前后对比）
    after_order: list[str] = field(default_factory=list)   # 精排顺序
    system_prompt: str = RERANK_SYSTEM_PROMPT
    user_prompt: str = ""
    raw_output: str = ""
    ok: bool = False
    error: str = ""
    elapsed: float = 0.0
    usage: dict = field(default_factory=dict)  # 本次调用的 token 用量（B16.1 模型消耗统计）
    backend: str = "llm"  # 实际执行方式，不能仅凭配置判断（本地可能降级）。
    fallback_reason: str = ""
    timings: dict = field(default_factory=dict)
    pair_tokens: list[int] = field(default_factory=list)
    pair_details: list[dict] = field(default_factory=list)
    cold_load: bool = False
    tokenizer_type: str = ""


def _extract_json_array(raw: str) -> list:
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("输出中没有 JSON 数组")
    return json.loads(text[start : end + 1])


def rerank(
    question: str,
    items: list[RetrievedItem],
    top_k: int,
    llm: LLMClient,
    config: AppConfig | None = None,
) -> RerankOutcome:
    """精排分发器：按 RERANK_BACKEND 选择本地 cross-encoder 或 LLM 打分。

    local 失败自动降级到 LLM 精排；LLM 也失败时由 rerank_llm 内部回退粗排顺序。
    """
    cfg = config or get_config()
    backend = (cfg.rerank_backend or "llm").strip().lower()
    if backend == "local_onnx":
        outcome = rerank_local(question, items, top_k, cfg)
        if outcome.ok:
            return outcome
        fallback = rerank_llm(question, items, top_k, llm, cfg)
        fallback.fallback_reason = outcome.error
        fallback.timings = {**outcome.timings, "local_attempt": outcome.elapsed,
                            "llm": fallback.elapsed}
        fallback.elapsed += outcome.elapsed
        fallback.cold_load = outcome.cold_load
        fallback.pair_tokens = outcome.pair_tokens
        return fallback
    return rerank_llm(question, items, top_k, llm, cfg)


def rerank_llm(
    question: str,
    items: list[RetrievedItem],
    top_k: int,
    llm: LLMClient,
    config: AppConfig | None = None,
) -> RerankOutcome:
    """用 LLM 对候选卡片逐条打相关度分，按分重排。失败时整体回退为粗排顺序。"""
    cfg = config or get_config()
    outcome = RerankOutcome(before_order=[f"[{it.rank}] {it.metadata.get('source')}" for it in items])

    lines = []
    for i, item in enumerate(items, start=1):
        meta = item.metadata
        snippet = item.text[:_MAX_CANDIDATE_CHARS].replace("\n", " ")
        lines.append(f'C{i}｜来源: {meta.get("source", "?")}｜章节: {meta.get("section") or "-"}｜内容: {snippet}')
    outcome.user_prompt = RERANK_USER_PROMPT_TEMPLATE.format(
        question=question, candidates="\n".join(lines)
    )
    messages = [
        {"role": "system", "content": outcome.system_prompt},
        {"role": "user", "content": outcome.user_prompt},
    ]

    start = time.time()
    try:
        raw, _ = llm.chat(messages, usage_out=outcome.usage)
        outcome.raw_output = raw
        entries = _extract_json_array(raw)
        score_map: dict[str, float] = {}
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            cid = str(entry.get("id", "")).strip().upper()
            score = float(entry.get("score", 0))
            score_map[cid] = max(0.0, min(10.0, score))

        # 打分排序：没被打分 / 打分非法的候选沿用粗排名次排在后面
        # 注意：候选编号 C{idx+1} 是 1-based，与提示词里的编号一一对应
        def sort_key(pair: tuple[int, RetrievedItem]) -> tuple[float, int]:
            idx, item = pair
            return (-score_map.get(f"C{idx + 1}", -1.0), idx)

        ordered_pairs = sorted(enumerate(items), key=sort_key)
        outcome.items = [item for _, item in ordered_pairs]
        outcome.scores = [
            (f"C{idx + 1}", item.metadata.get("source", "?"), score_map.get(f"C{idx + 1}", -1.0))
            for idx, item in ordered_pairs
        ]
        outcome.after_order = [
            f"[{rank}] {source}（{score:.2f} 分，{cid}）"
            for rank, (cid, source, score) in enumerate(outcome.scores[:top_k], start=1)
        ]
        outcome.ok = bool(score_map)
        if not outcome.ok:
            outcome.error = "评分输出中没有有效条目"
    except Exception as exc:
        outcome.ok = False
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.items = items  # 回退：保持粗排顺序

    outcome.elapsed = time.time() - start
    return outcome


# ---------------- 本地精排（bge-reranker-v2-m3 ONNX cross-encoder，CPU） ----------------

_LOCAL_RERANKER = None  # (tokenizer, session, input_names)
_LOCAL_RERANKER_KEY = None
_LOCAL_LOAD_LOCK = threading.Lock()
_LOCAL_SCORE_LOCK = threading.Lock()
_LOCAL_MAX_PAIR_TOKENS = 2048
_LOCAL_DOC_CHARS = 1200


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def _local_cache_key(cfg: AppConfig) -> tuple:
    model_dir = cfg.rerank_model_dir.resolve()
    files = [model_dir / name for name in ("model_quantized.onnx", "tokenizer.json")]
    stamps = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in files)
    return str(model_dir), max(0, cfg.rerank_threads), stamps


def _get_local_reranker(config: AppConfig | None = None):
    """按模型目录及线程设置缓存引擎；下载补齐后可重试，不缓存永久失败。"""
    global _LOCAL_RERANKER, _LOCAL_RERANKER_KEY
    cfg = config or get_config()
    key = _local_cache_key(cfg)
    with _LOCAL_LOAD_LOCK:
        if _LOCAL_RERANKER is not None and _LOCAL_RERANKER_KEY == key:
            return _LOCAL_RERANKER
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_file = cfg.rerank_model_dir / "model_quantized.onnx"
        tok_file = cfg.rerank_model_dir / "tokenizer.json"
        if not model_file.exists() or not tok_file.exists():
            raise FileNotFoundError(f"精排模型文件缺失：{cfg.rerank_model_dir}")
        tokenizer = Tokenizer.from_file(str(tok_file))
        tokenizer.enable_truncation(max_length=_LOCAL_MAX_PAIR_TOKENS)
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(0, cfg.rerank_threads)
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        session = ort.InferenceSession(str(model_file), sess_options=options,
                                      providers=["CPUExecutionProvider"])
        _LOCAL_RERANKER = (tokenizer, session, [item.name for item in session.get_inputs()])
        _LOCAL_RERANKER_KEY = key
        return _LOCAL_RERANKER


def _score_pair(tokenizer, session, input_names, query: str, text: str,
                detail: dict | None = None) -> float:
    """逐对计时分词和推理；输入长度保持不变。"""
    import numpy as np

    start = time.perf_counter()
    encoding = tokenizer.encode(query, text)
    feed = {"input_ids": np.array([encoding.ids], dtype=np.int64),
            "attention_mask": np.array([encoding.attention_mask], dtype=np.int64)}
    if "token_type_ids" in input_names:
        feed["token_type_ids"] = np.array([encoding.type_ids], dtype=np.int64)
    tokenization = time.perf_counter() - start
    start = time.perf_counter()
    logits = session.run(None, feed)[0].reshape(-1)
    if len(logits) != 1 or not math.isfinite(float(logits[0])):
        raise ValueError("精排模型输出不是单个有限分数")
    inference = time.perf_counter() - start
    if detail is not None:
        detail.update(token_count=len(encoding.ids), tokenization=tokenization, inference=inference)
    return _sigmoid(float(logits[0]))


def rerank_local(question: str, items: list[RetrievedItem], top_k: int,
                 config: AppConfig | None = None) -> RerankOutcome:
    """本地打分并保留原始候选编号；sigmoid×10 仅为展示尺度，不是回答正确率。"""
    cfg = config or get_config()
    outcome = RerankOutcome(backend="local_onnx", system_prompt="",
                            before_order=[f"[{it.rank}] {it.metadata.get('source')}" for it in items])
    outcome.timings = {"load": 0.0, "tokenization": 0.0, "inference": 0.0}
    start = time.perf_counter()
    if not items:
        outcome.ok = True
        return outcome
    score_map: dict[str, float] = {}
    try:
        load_start = time.perf_counter()
        outcome.cold_load = _LOCAL_RERANKER is None or _LOCAL_RERANKER_KEY != _local_cache_key(cfg)
        try:
            engine = _get_local_reranker(cfg)
        finally:
            outcome.timings["load"] = time.perf_counter() - load_start
        tokenizer, session, input_names = engine
        outcome.tokenizer_type = type(tokenizer.model).__name__
        # 避免多个会话同时争用本地 CPU；等待时间也纳入精排总耗时。
        wait_start = time.perf_counter()
        with _LOCAL_SCORE_LOCK:
            outcome.timings["queue"] = time.perf_counter() - wait_start
            for i, item in enumerate(items, start=1):
                detail = {"id": f"C{i}", "source": item.metadata.get("source", "?"),
                          "document_chars": len(item.text[:_LOCAL_DOC_CHARS])}
                score01 = _score_pair(tokenizer, session, input_names, question,
                                      item.text[:_LOCAL_DOC_CHARS], detail)
                score_map[f"C{i}"] = score01 * 10.0
                outcome.pair_details.append(detail)
                outcome.pair_tokens.append(detail["token_count"])
                outcome.timings["tokenization"] += detail["tokenization"]
                outcome.timings["inference"] += detail["inference"]

        ordered_pairs = sorted(enumerate(items),
                               key=lambda pair: (-score_map[f"C{pair[0] + 1}"], pair[0]))
        outcome.items = [item for _, item in ordered_pairs]
        outcome.scores = [(f"C{idx + 1}", item.metadata.get("source", "?"),
                           round(score_map[f"C{idx + 1}"], 4)) for idx, item in ordered_pairs]
        outcome.after_order = [f"[{rank}] {source}（{score:.2f} 分，{cid}）"
                               for rank, (cid, source, score) in enumerate(outcome.scores[:top_k], start=1)]
        outcome.raw_output = json.dumps([{"id": cid, "score": score} for cid, _, score in outcome.scores],
                                        ensure_ascii=False)
        outcome.ok = True
    except Exception as exc:
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.items = items
    outcome.elapsed = time.perf_counter() - start
    return outcome

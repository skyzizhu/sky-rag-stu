"""V2.5：Rerank 精排（LLM 重排）。

产品视角：向量检索和 BM25 都是「先算分再排序」的粗排——它们只看文字和指纹，
不理解内容。Rerank 是最后一道精排：让大模型逐条阅读候选卡片，
按「是否真的回答了问题」打 0~10 分，再按分数精选。

本实现采用「LLM 重排」（用已配置的云端大模型打分），零新增依赖；
后续可替换为专用重排模型（如 bge-reranker、Cohere Rerank），接口不变。

可靠性设计：
- 打分输出必须是 JSON 数组，逐条校验（编号必须对得上、分数取值合法）；
- 没被打分 / 打分非法的候选，沿用粗排名次排在已评分候选之后；
- 任何解析失败 / LLM 报错，整体回退为粗排顺序，问答不中断。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from src.config import AppConfig
from src.llm import LLMClient
from src.retriever import RetrievedItem

RERANK_SYSTEM_PROMPT = """你是知识库检索的相关度评估器。给定一个用户问题和若干候选知识片段，请对每条候选与问题的相关程度打分。

评分标准：
- 10~8 分：直接回答了问题，或包含问题的关键事实 / 数据
- 7~4 分：与问题主题相关，但不包含直接答案
- 3~0 分：与问题无关，或仅有微弱联系

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
        raw, _ = llm.chat(messages)
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

        ordered = [item for _, item in sorted(enumerate(items), key=sort_key)]
        outcome.items = ordered
        outcome.scores = [
            (f"C{i + 1}", it.metadata.get("source", "?"), score_map.get(f"C{i + 1}", -1.0))
            for i, it in enumerate(ordered)
        ]
        outcome.after_order = [f"[{i + 1}] {it.metadata.get('source')}（{score_map.get(f'C{i + 1}', -1.0):.0f} 分）"
                               for i, it in enumerate(ordered[:top_k])]
        outcome.ok = bool(score_map)
        if not outcome.ok:
            outcome.error = "评分输出中没有有效条目"
    except Exception as exc:
        outcome.ok = False
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.items = items  # 回退：保持粗排顺序

    outcome.elapsed = time.time() - start
    return outcome

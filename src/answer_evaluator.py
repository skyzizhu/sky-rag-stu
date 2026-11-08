"""V3.6：单题回答质量评估（评估卡片的数据层）。

产品视角：调试时间线回答「每个节点发生了什么」，评估卡片回答「这次回答可不可信」。
分两层，从便宜到贵：

  ① 结构信号（0 次 LLM 调用，纯代码从流水线结果计算）
     精排分数带 / 候选池→上下文比例 / 引用覆盖率 / 拒答一致性 / 拆分触发情况。
     它们是「体检指标」——不证明答案正确，只暴露可疑信号。

  ② LLM 两维评审（1 次 LLM 调用，回答完成后执行）
     忠实度（答案的每个论断是否被资料支撑）/ 相关性（是否回答了用户的问题）。
     0~10 分只用于分档（优/中/差），不做精确阈值——与精排同一教训：
     LLM 打分有波动，分档比精确分稳。

可靠性设计：
  - 评估失败（LLM 报错 / JSON 解析失败）不影响问答主流程：卡片降级为仅结构信号；
  - 评估在回答流式输出完成后才执行，不阻塞用户阅读答案；
  - 评估结果挂在 QAResult.evaluation 上随会话持久化，历史消息重绘不重新调用 LLM。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from src.config import AppConfig, get_config
from src.llm import LLMClient, new_usage
from src.query_understanding import _extract_json_object

# 拒答短语：回答里出现这些表述、而上下文其实有卡片时，就是「拒答不一致」可疑信号。
# 回答语言跟随提问语言（realize.md B14.1），所以覆盖简/繁中文、英文、日文；统一按小写匹配
_REFUSAL_PATTERNS = (
    "没有相关内容", "没有找到相关", "知识库中未", "知识库中没有",
    "未找到相关", "无法回答", "资料中未提及", "资料中没有",
    "沒有相關內容", "沒有找到相關", "知識庫中未", "知識庫中沒有",
    "未找到相關", "無法回答", "資料中未提及", "資料中沒有",
    "no relevant information", "no relevant content", "not found in the",
    "knowledge base does not", "knowledge base doesn't", "not in the knowledge base",
    "could not find any relevant", "couldn't find any relevant", "did not find any relevant",
    "unable to answer", "cannot answer", "can't answer",
    "materials do not mention", "material does not mention", "not mentioned in the",
    "materials do not contain", "material does not contain",
    "関連する内容はありません", "関連する情報はありません", "見つかりませんでした",
    "記載されていません", "回答できません",
)

# 精排打分行形如 "C1  心得.rtf  8 分"
_RERANK_LINE_RE = re.compile(r"^C\d+\s+.+?\s+(-?\d+(?:\.\d+)?)\s*分", re.MULTILINE)
# 拆分节点 summary 形如 "复合问题拆分为 3 个子查询"
_SPLIT_COUNT_RE = re.compile(r"拆分为\s*(\d+)\s*个子查询")
# 拆分节点候选池形如 "18 条（上限 20 条）"
_POOL_RE = re.compile(r"(\d+)\s*条")
# 答案中的引用编号 [1] [2] …
_CITE_RE = re.compile(r"\[(\d{1,2})\]")


@dataclass
class EvalDimension:
    """一维评审结果：精确分仅用于展示，分档才是对外口径。"""

    score: int = 0
    band: str = "poor"  # good(≥8) / mid(≥5) / poor
    reason: str = ""


def _band(score: float) -> str:
    if score >= 8:
        return "good"
    if score >= 5:
        return "mid"
    return "poor"


# ---------------------------------------------------------------- 结构信号
def compute_signals(result) -> dict:
    """从流水线结果与 trace 里提取结构信号，值为语言无关的原始类型（渲染时再翻译）。"""
    trace = result.trace or []

    # 拆分触发情况：Sub-Query 节点存在 + 子查询数 + 合并池大小
    split = {"triggered": False, "sub_queries": 0, "pool": 0}
    for node in trace:
        if "查询拆分检索" not in str(node.get("name", "")):
            continue
        split["triggered"] = True
        m = _SPLIT_COUNT_RE.search(str(node.get("summary", "")))
        if m:
            split["sub_queries"] = int(m.group(1))
        for label, value in node.get("items", []):
            if "候选池" in str(label):
                m = _POOL_RE.search(str(value))
                if m:
                    split["pool"] = int(m.group(1))
        break

    # 精排分数带：从 Rerank 节点的打分明细解析；断层（最高−中位）大 →
    # 语义带薄，问题可能歧义大或库中无货（"自信地检索到无关内容"预警）
    rerank = {"ok": False, "top": 0.0, "median": 0.0, "gap": 0.0, "n": 0}
    for node in trace:
        if "Rerank" not in str(node.get("name", "")) or "精排" not in str(node.get("name", "")):
            continue
        for label, value in node.get("items", []):
            if "LLM 重排的结果" not in str(label):
                continue
            scores = [float(x) for x in _RERANK_LINE_RE.findall(str(value))]
            if scores:
                scores.sort(reverse=True)
                mid = scores[len(scores) // 2]
                rerank = {"ok": True, "top": scores[0], "median": mid,
                          "gap": round(scores[0] - mid, 1), "n": len(scores)}
        break

    # 候选池 → 上下文的收窄比例：
    # 池优先取拆分节点的合并池（精排输入），否则取 B11「采用+丢弃」（精排后候选），
    # 最后回退 retrieved；入选数优先取 B11「采用」
    merged = {"used": 0, "dropped": 0}
    for node in trace:
        if "合并 / 去重" not in str(node.get("name", "")):
            continue
        for label, value in node.get("items", []):
            if str(label) == "输出":
                m = re.search(r"采用\s*(\d+)\s*条，丢弃\s*(\d+)\s*条", str(value))
                if m:
                    merged = {"used": int(m.group(1)), "dropped": int(m.group(2))}
        break
    final = merged["used"] or len(result.sources or [])
    pool = (split.get("pool") or (merged["used"] + merged["dropped"])
            or len(getattr(result, "retrieved", []) or []))
    context = {"pool": pool, "final": final}

    # 引用覆盖率：答案中实际出现的引用编号占入选卡片的比例
    answer = result.answer or ""
    cited = {int(x) for x in _CITE_RE.findall(answer)}
    cited = {c for c in cited if 1 <= c <= len(result.sources or [])}
    cite = {"cited": len(cited), "total": len(result.sources or [])}

    # 拒答一致性：回答称"没有相关内容"但上下文其实有卡片 → 可疑信号
    lowered = answer.lower()
    refused = any(p in lowered for p in _REFUSAL_PATTERNS)
    has_context = bool(result.sources)
    if refused and has_context:
        state = "inconsistent"
    elif refused:
        state = "consistent"
    else:
        state = "normal"
    refusal = {"state": state}

    return {"split": split, "rerank": rerank, "context": context,
            "cite": cite, "refusal": refusal}


# ---------------------------------------------------------------- LLM 三维评审
_JUDGE_SYSTEM_PROMPT = """你是 RAG 回答质量评审员。给定【用户问题】【参考资料】【AI 回答】，输出严格的 JSON 对象：
{"faithfulness": {"score": <0~10 整数>, "reason": "<一句话理由>"},
 "relevance": {"score": <0~10 整数>, "reason": "<一句话理由>"},
 "completeness": {"score": <0~10 整数>, "reason": "<一句话理由>"}}

评分标准：
- faithfulness 忠实度：回答的每个论断是否都能被参考资料支撑，**论断与其引用编号 [n] 的对应关系
  也要核对——错标引用必须扣分**。10=全部有据且引用正确；5~7=部分有据/部分概括；
  4 及以下=出现资料中没有的内容（编造、脑补、夸大）。资料不支撑的论断必须扣分，不要当老好人。
- relevance 相关性：回答是否针对用户所问。10=完整回答了问题；5~7=只回答了一部分；
  4 及以下=答非所问或明显偏题。
- completeness 完备性：**问题包含多个子需求时，逐个检查是否都有作答**——
  10=全部子需求都有答案；多需求问题漏答任一子需求最高 4 分；
  单一需求问题时按回答是否完整充分打分（此时此项与相关性接近）。

要求：
1. 只输出一个 JSON 对象，不要输出任何多余文字，不要用代码块包裹。
2. 理由不超过 40 字，指出具体扣分点（如有）。"""

_JUDGE_USER_TEMPLATE = """【用户问题】{question}

【参考资料】
{context}

【AI 回答】
{answer}

请按要求输出 JSON。"""


def judge_dimensions(
    question: str,
    answer: str,
    context_text: str,
    llm: LLMClient | None = None,
    config: AppConfig | None = None,
    usage_out: dict | None = None,
) -> tuple[EvalDimension, EvalDimension, EvalDimension]:
    """LLM 三维评审（一次调用）。任何失败抛出异常，由 run_evaluation 统一降级。

    usage_out：可选，调用后写入本次 token 用量（B16.1 模型消耗统计）。
    """
    cfg = config or get_config()
    llm = llm or LLMClient(cfg)
    # 附页可能很长，评审只需"足够判断有据性"的篇幅
    context_text = (context_text or "").strip()[:6000]
    messages = [
        {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": _JUDGE_USER_TEMPLATE.format(
            question=question, context=context_text or "（无）", answer=answer)},
    ]
    raw, _ = llm.chat(messages, usage_out=usage_out)
    data = _extract_json_object(raw)

    def _dim(key: str) -> EvalDimension:
        item = data.get(key) or {}
        score = float(item.get("score", 0))
        score = max(0.0, min(10.0, score))
        return EvalDimension(score=int(round(score)), band=_band(score),
                             reason=str(item.get("reason", "")).strip()[:120])

    return _dim("faithfulness"), _dim("relevance"), _dim("completeness")


# ---------------------------------------------------------------- 入口
def run_evaluation(result, llm: LLMClient | None = None,
                   config: AppConfig | None = None) -> dict:
    """对一次已完成的问答做质量评估，返回可 JSON 持久化的评估载荷。"""
    start = time.time()
    payload: dict = {"ok": False, "error": "", "elapsed": 0.0, "overall": None,
                     "faithfulness": None, "relevance": None, "signals": {}}
    try:
        payload["signals"] = compute_signals(result)
        answer = (result.answer or "").strip()
        if not answer:
            payload["error"] = "empty_answer"
            payload["elapsed"] = time.time() - start
            return payload
        eval_usage = new_usage()
        faith, rel, comp = judge_dimensions(
            result.question or "", answer, getattr(result, "context", "") or "",
            llm=llm, config=config, usage_out=eval_usage,
        )
        payload["usage"] = eval_usage  # 评估自身的调用消耗（与管线用量分开计，缓存命中重评不重复计数）
        payload["faithfulness"] = {"score": faith.score, "band": faith.band, "reason": faith.reason}
        payload["relevance"] = {"score": rel.score, "band": rel.band, "reason": rel.reason}
        payload["completeness"] = {"score": comp.score, "band": comp.band, "reason": comp.reason}
        # 量化参考分（0~100）：忠实度 50% / 相关性 30% / 完备性 20%——
        # 忠实度权重最高（编造最严重），完备性针对复合问题漏答。
        # 分档仍为主口径：任一维度"差"一票否决，防止高分维度掩盖编造或漏答
        score_100 = int(round(faith.score * 5 + rel.score * 3 + comp.score * 2))
        payload["overall_score"] = score_100
        if "poor" in (faith.band, rel.band, comp.band):
            payload["overall"] = "poor"
        else:
            payload["overall"] = _band(score_100 / 10)
        payload["ok"] = True
    except Exception as exc:  # 评估失败绝不影响问答主流程
        payload["error"] = f"{type(exc).__name__}: {exc}"
    payload["elapsed"] = time.time() - start
    return payload


def compute_cost(usage: dict, price_in: float, price_in_cached: float, price_out: float) -> float:
    """按三档单价（元 / 百万 tokens）估算一次消耗的费用。

    usage 形如 {"input", "input_cached", "output", ...}：未命中输入 = input − input_cached。
    单价为 0 的档位自然计 0（未填单价 = 该段免费显示）。
    """
    uncached = max(0, int(usage.get("input", 0)) - int(usage.get("input_cached", 0)))
    cached = max(0, int(usage.get("input_cached", 0)))
    out = max(0, int(usage.get("output", 0)))
    return (uncached / 1_000_000 * price_in + cached / 1_000_000 * price_in_cached
            + out / 1_000_000 * price_out)


def merge_usage(parts: list[dict]) -> dict:
    """把多次调用的用量合并成一次总计（tokens 相加；任一段为估算则整体标记估算）。"""
    total = {"input": 0, "input_cached": 0, "output": 0, "estimated": False, "calls": len(parts)}
    for part in parts:
        total["input"] += int(part.get("input", 0))
        total["input_cached"] += int(part.get("input_cached", 0))
        total["output"] += int(part.get("output", 0))
        total["estimated"] = total["estimated"] or bool(part.get("estimated"))
    return total

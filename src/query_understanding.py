"""V2.1：Query 理解 / 改写（Query Understanding）。

产品视角：这是检索的「翻译官」——
用户的问题往往是口语化的（"旧版笔记里 chunk_size 写的多少来着？"），
直接拿去检索效果一般。这个节点用一次 LLM 调用同时完成三件事：
  1. 改写：把口语化提问改写成检索友好的查询语句（vector_query）
  2. 提关键词：产出关键词列表（keyword_query，供后续关键词/混合检索使用）
  3. 推断过滤条件：从问题里识别领域/分类/状态等（filters）

可靠性设计：
  - LLM 只被允许输出一个 JSON 对象，逐字段校验后再使用；
  - 任何解析失败 / 字段非法，都自动「回退」：用原始问题 + 不加过滤条件继续检索，
    保证问答永远能走下去（宁可不增强，不能中断）。
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from dataclasses import dataclass, field

from src.config import AppConfig, get_config
from src.llm import LLMClient
from src.metadata import DOMAINS, STATUSES

# filters 里允许 LLM 推断的键。
# 实测教训（2026-08-29）：让 LLM 按语义推断 domain/category 会与个人目录分类错位
# （如「周会纪要」被推断为 work，但用户实际存在 reference 目录），过严的过滤会把
# 正确答案挡在门外。因此只保留高精度的推断：status（归档意图）与 source（点名文件）。
# domain / category 等仍可由用户在界面手动过滤。
ALLOWED_FILTER_KEYS = {"status", "source"}

QU_SYSTEM_PROMPT = """你是个人知识库的「检索查询理解器」。任务：把用户的原始问题改写成更适合知识库检索的查询，并从问题中推断过滤条件。

输出要求（必须严格遵守）：
1. 只输出一个 JSON 对象，不要输出任何多余文字，不要用代码块包裹。
2. JSON 字段定义：
   "intent"：一句话概括用户意图
   "vector_query"：改写后的检索语句（去掉口语和指代，补全关键信息；原问题已足够清晰时与原问题一致）
   "keyword_query"：字符串数组，3~6 个关键词（可含同义词），供关键词检索使用
   "filters"：对象，只能包含以下键，推断不出来就整个省略：
       "status"：只能是 active 或 archive
       "source"：用户明确点名的文件名
3. 只有用户明确要找「旧版 / 归档 / 历史 / 已废弃」内容时才输出 "status": "archive"，否则不要输出 status。
4. 用户明确点名某个文件时才输出 "source"。
5. 改写必须忠实于原问题语义，禁止添加原问题中没有的实体或条件。
6. 过滤条件推断要保守：宁可少推断（范围大一点只是多检索几条），也不要过度推断（猜错了会漏掉正确答案）。
7. 用户消息里会给出「当前时间」。当问题包含相对时间（如"最近的""上个月""今年"）或明确时间范围时，
   输出 "time_range" 字段：{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}；"最近"默认指最近 90 天；
   无法解析出时间范围时省略该字段。"""

QU_USER_PROMPT_TEMPLATE = """【当前时间】{current_time}
【知识库领域枚举】work / learning / life / reference / archive
【知识库现有分类】{categories}
【用户原始问题】{question}

请按系统要求输出 JSON。"""


@dataclass
class QueryUnderstanding:
    """一次 Query 理解的完整记录（含 Prompt 与原始输出，供调试时间线展示）。"""

    original: str
    intent: str = ""
    vector_query: str = ""          # 改写后的检索语句（失败时回退为原始问题）
    keyword_query: list[str] = field(default_factory=list)
    time_range: dict = field(default_factory=dict)  # {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}
    filters: dict = field(default_factory=dict)
    system_prompt: str = QU_SYSTEM_PROMPT
    user_prompt: str = ""
    raw_output: str = ""
    ok: bool = False
    error: str = ""
    elapsed: float = 0.0


def _strip_code_fence(raw: str) -> str:
    """LLM 有时会用 ```json ... ``` 包裹输出，剥掉它。"""
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", text, re.DOTALL)
    return fence.group(1).strip() if fence else text


def _extract_json_object(raw: str) -> dict:
    """从输出文本中取出第一个完整 JSON 对象（容错：前后有杂文字也能取到）。"""
    text = _strip_code_fence(raw)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("输出中没有 JSON 对象")
    return json.loads(text[start : end + 1])


def _valid_filters(raw: dict) -> tuple[dict, list[str]]:
    """逐字段校验 LLM 推断的过滤条件，非法值丢弃并记录原因。"""
    filters: dict = {}
    warnings: list[str] = []
    for key, value in (raw or {}).items():
        if key not in ALLOWED_FILTER_KEYS:
            warnings.append(f"键 {key} 不在允许范围，已忽略")
            continue
        if value in (None, "", "all"):
            continue
        if key == "domain":
            value = str(value).strip().lower()
            if value not in DOMAINS:
                warnings.append(f"domain 非法值 {value}，已忽略")
                continue
        if key == "status":
            value = str(value).strip().lower()
            if value not in STATUSES:
                warnings.append(f"status 非法值 {value}，已忽略")
                continue
        if key == "category":
            value = str(value).strip().lower()
        if key in ("tags", "topic", "keyword_query") and not isinstance(value, list):
            value = [str(value)]
        filters[key] = value
    return filters, warnings


def understand_query(
    question: str,
    llm: LLMClient | None = None,
    config: AppConfig | None = None,
    categories: list[str] | None = None,
) -> QueryUnderstanding:
    """执行 Query 理解/改写。任何失败都会回退为原始问题，不会抛出异常。"""
    cfg = config or get_config()
    llm = llm or LLMClient(cfg)
    qu = QueryUnderstanding(original=question, vector_query=question)

    category_text = "、".join(sorted(set(categories or []))) or "（未知）"
    qu.user_prompt = QU_USER_PROMPT_TEMPLATE.format(
        categories=category_text,
        question=question,
        current_time=datetime.now().strftime("%Y-%m-%d %H:%M %A"),
    )
    messages = [
        {"role": "system", "content": qu.system_prompt},
        {"role": "user", "content": qu.user_prompt},
    ]

    start = time.time()
    try:
        raw, _ = llm.chat(messages)
        qu.raw_output = raw
        data = _extract_json_object(raw)

        qu.intent = str(data.get("intent", "")).strip()[:120]
        vector_query = str(data.get("vector_query", "")).strip()
        if vector_query:
            qu.vector_query = vector_query

        keyword_query = data.get("keyword_query", [])
        if isinstance(keyword_query, str):
            keyword_query = [keyword_query]
        if isinstance(keyword_query, list):
            qu.keyword_query = [str(k).strip() for k in keyword_query if str(k).strip()][:8]

        tr = data.get("time_range")
        if isinstance(tr, dict):
            date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
            frm, to = str(tr.get("from", "")).strip(), str(tr.get("to", "")).strip()
            if date_re.match(frm) and date_re.match(to):
                qu.time_range = {"from": frm, "to": to}

        qu.filters, warnings = _valid_filters(data.get("filters") or {})
        if warnings:
            qu.error = "；".join(warnings)
        qu.ok = True
    except Exception as exc:  # JSON 解析失败 / LLM 报错等一律回退
        qu.ok = False
        qu.error = f"{type(exc).__name__}: {exc}"
        qu.vector_query = question
        qu.filters = {}

    qu.elapsed = time.time() - start
    return qu

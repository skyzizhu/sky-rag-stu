"""V3.3：自动打标签——入库时由 LLM 阅读内容，补全 topic 与 tags。

仅在卡片的 topic/tags 为空时触发（人工声明与目录推断优先），失败静默跳过。
V3.9.1：同一次调用顺带补 doc_type（软约束）——入库人未填写且目录/声明推断
为空时，让 LLM 从台账已登记类型词表中归一个最贴切的类型；填写值永远优先。
"""

from __future__ import annotations

import json
import re

from src.config import AppConfig

AUTO_TAG_SYSTEM_PROMPT = """你是知识库的档案员。阅读文档内容，输出 JSON：
{"topic": ["主题1"], "tags": ["标签1", "标签2"], "doc_type": "文档类型"}
规则：topic 1~2 个主题词；tags 2~6 个具体标签；doc_type 一个最能概括文档类别的短词
（如 制度、合同、报告、手册、方案）；只输出 JSON，不要多余文字。"""


def auto_tag(text: str, llm, config: AppConfig | None = None,
             *, doc_type_vocab: list[str] | None = None) -> dict:
    """返回 {"topic": [...], "tags": [...], "doc_type": ""}；任何失败返回空结果。

    doc_type_vocab：台账已登记的类型词表（软约束）——提示词要求 LLM 优先从中
    选最贴切的一个，都不合适才自拟短词，避免筛选值碎片化。"""
    out = {"topic": [], "tags": [], "doc_type": ""}
    try:
        system = AUTO_TAG_SYSTEM_PROMPT
        vocab = [v.strip() for v in (doc_type_vocab or []) if v.strip()]
        if vocab:
            system += ("\n文档类型优先从以下已登记类型中选择最贴切的一个："
                       + "、".join(vocab[:40])
                       + "；都不合适时自拟一个不超过 8 字的类型名。")
        raw, _ = llm.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": f"【文档内容节选】\n{(text or '')[:800]}\n\n请输出 JSON。"},
        ])
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return out
        data = json.loads(match.group(0))
        for key in ("topic", "tags"):
            value = data.get(key)
            if isinstance(value, str):
                value = [value]
            if isinstance(value, list):
                out[key] = [str(v).strip() for v in value if str(v).strip()][:6]
        doc_type = data.get("doc_type")
        if isinstance(doc_type, str):
            out["doc_type"] = doc_type.strip()[:24]
    except Exception:
        pass
    return out

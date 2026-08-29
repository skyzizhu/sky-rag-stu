"""V3.3：自动打标签——入库时由 LLM 阅读内容，补全 topic 与 tags。

仅在卡片的 topic/tags 为空时触发（人工声明与目录推断优先），失败静默跳过。
"""

from __future__ import annotations

import json
import re

from src.config import AppConfig

AUTO_TAG_SYSTEM_PROMPT = """你是知识库的档案员。阅读文档内容，输出 JSON：
{"topic": ["主题1"], "tags": ["标签1", "标签2"]}
规则：topic 1~2 个主题词；tags 2~6 个具体标签；只输出 JSON，不要多余文字。"""


def auto_tag(text: str, llm, config: AppConfig | None = None) -> dict:
    """返回 {"topic": [...], "tags": [...]}；任何失败返回空结果。"""
    out = {"topic": [], "tags": []}
    try:
        raw, _ = llm.chat([
            {"role": "system", "content": AUTO_TAG_SYSTEM_PROMPT},
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
    except Exception:
        pass
    return out

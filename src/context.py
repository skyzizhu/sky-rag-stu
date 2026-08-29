"""节点 7：Context 组装。

产品视角：这一步相当于「把查到的资料整理成一份考卷附页」——
- 按相关度从高到低排好；
- 完全重复的卡片只留一份；
- 每份资料前面都标上编号和出处（来源文件、章节、页码），
  这既方便大模型在答案里引用，也方便你核对它说的对不对；
- 总量有上限，防止超出大模型的「阅读容量」。
"""

from __future__ import annotations

import sys

from src.config import AppConfig, get_config
from src.retriever import RetrievedItem


def citation_label(item: RetrievedItem, number: int) -> str:
    """一条资料的单行出处，如：[1] 来源: xx.md | 领域: learning | 章节: 基础 | 页码: 3"""
    meta = item.metadata
    label = f"[{number}] 来源: {meta.get('source', '?')}"
    if meta.get("domain"):
        label += f" | 领域: {meta['domain']}"
    if meta.get("category") and meta["category"] != "general":
        label += f" | 分类: {meta['category']}"
    if meta.get("section"):
        label += f" | 章节: {meta['section']}"
    if meta.get("page"):
        label += f" | 页码: {meta['page']}"
    return label


def build_context(
    items: list[RetrievedItem], config: AppConfig | None = None
) -> tuple[str, list[RetrievedItem], int]:
    """组装最终发给大模型的资料文本。

    返回：(资料全文, 实际用到的资料, 因超长被丢弃的条数)
    """
    cfg = config or get_config()

    used: list[RetrievedItem] = []
    seen_texts: set[str] = set()
    blocks: list[str] = []
    dropped = 0
    total_chars = 0

    for item in items:  # 已按分数从高到低
        normalized = "".join(item.text.split())  # 忽略空白差异后判重
        if normalized in seen_texts:
            dropped += 1
            continue
        number = len(used) + 1
        block = f"{citation_label(item, number)}\n{item.text}"
        if total_chars + len(block) > cfg.context_max_chars and blocks:
            dropped += 1
            continue
        seen_texts.add(normalized)
        used.append(item)
        blocks.append(block)
        total_chars += len(block)

    context = "\n\n".join(blocks) if blocks else "（没有检索到任何相关资料）"
    return context, used, dropped


# ---------------- 直接运行本文件：看检索结果如何被拼成资料 ----------------
if __name__ == "__main__":
    from src.retriever import get_retriever

    question = sys.argv[1] if len(sys.argv) > 1 else "RAG 里的 Metadata 是干什么的？"
    print(f"问题：{question}\n")
    items = get_retriever().retrieve(question)
    context, used, dropped = build_context(items)
    print(f"召回 {len(items)} 条，实际采用 {len(used)} 条，丢弃 {dropped} 条（重复或超长）\n")
    print("=" * 60)
    print(context)
    sys.exit(0)

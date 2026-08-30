"""节点 7：Context 组装。

产品视角：这一步相当于「把查到的资料整理成一份考卷附页」——
- 按相关度从高到低排好；
- 完全重复的卡片只留一份；
- 每份资料前面都标上编号和出处（来源文件、章节、页码），
  这既方便大模型在答案里引用，也方便你核对它说的对不对；
- 总量有上限，防止超出大模型的「阅读容量」。
"""

from __future__ import annotations

import re
import sys
import unicodedata

from src.config import AppConfig, get_config
from src.retriever import RetrievedItem


def estimate_tokens(text: str) -> int:
    """估算文本的 token 数。

    经验规则（近似 LLM tokenizer 行为）：
    - 中文字符 ≈ 0.6~1 token（取 0.8）
    - 英文单词 ≈ 1~1.3 token（按 4 字符 ≈ 1 token 估算）
    - 数字/符号 ≈ 每 2 个字符 1 token
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if unicodedata.east_asian_width(ch) in ("F", "W"))
    rest = len(text) - cjk
    return int(cjk * 0.8 + rest / 3.5)


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


def _chunk_seq(item: RetrievedItem) -> int | None:
    """从 chunk_id 末尾解析出卡片序号（doc_xxx_0003 → 3）；解析失败返回 None。"""
    match = re.search(r"(\d{4})$", item.metadata.get("chunk_id") or "")
    return int(match.group(1)) if match else None


def _merge_adjacent_texts(texts: list[str]) -> str:
    """拼接相邻卡片文本：后一张与前一张的重叠部分（Overlap）只保留一份。"""
    merged = texts[0]
    for nxt in texts[1:]:
        overlap = 0
        for k in range(min(300, len(merged), len(nxt)), 0, -1):
            if merged.endswith(nxt[:k]):
                overlap = k
                break
        merged += nxt[overlap:]
    return merged


def build_context(
    items: list[RetrievedItem], config: AppConfig | None = None,
    max_items: int | None = None, max_per_doc: int | None = None
) -> tuple[str, list[RetrievedItem], list[dict], list[str]]:
    """组装最终发给大模型的资料文本（V2.6：多样性 + 相邻补全）。

    选取规则：按相关度降序遍历候选——
      ① 内容与已选卡片完全相同的跳过（去重）；
      ② 同一文档最多采用 max_per_doc 条（保持来源多样；列举类提问可放开）；
      ③ 达到采用条数上限（默认 Top K）后停止；
      ④ 单条放入会超出 token 上限的跳过。

    相邻补全：同文档、卡片序号连续的已选卡片自动拼接（Overlap 去重），
    让被切片切断的上下文恢复完整。

    返回：(资料全文, 实际采用的资料, 丢弃明细, 相邻补全说明)
    """
    cfg = config or get_config()
    max_items = max_items or cfg.top_k
    max_per_doc = max_per_doc if max_per_doc is not None else cfg.context_max_per_doc

    # —— 第一轮：按规则选取 ——
    used: list[RetrievedItem] = []
    seen_texts: set[str] = set()
    per_doc: dict[str, int] = {}
    dropped: list[dict] = []
    blocks: list[str] = []
    context_so_far = ""  # 已放入的资料全文（用于 token 估算）

    for item in items:
        normalized = "".join(item.text.split())
        if normalized in seen_texts:
            dropped.append({"rank": item.rank, "source": item.metadata.get("source"),
                            "reason": "内容重复（与更高相关度的卡片相同），只保留第一条"})
            continue
        doc_id = item.metadata.get("document_id") or item.metadata.get("source")
        if len(used) >= max_items:
            dropped.append({"rank": item.rank, "source": item.metadata.get("source"),
                            "reason": f"已满采用条数上限（Top {max_items}）"})
            break
        if per_doc.get(doc_id, 0) >= max_per_doc:
            dropped.append({"rank": item.rank, "source": item.metadata.get("source"),
                            "reason": f"同一文档最多采用 {max_per_doc} 条（保持来源多样性）"})
            continue
        number = len(used) + 1
        block = f"{citation_label(item, number)}\n{item.text}"
        # Token 估算替代纯字符数：中英文混合时更接近模型实际容量
        if estimate_tokens(context_so_far + block) > cfg.context_max_tokens and context_so_far:
            dropped.append({"rank": item.rank, "source": item.metadata.get("source"),
                            "reason": f"放入后会超出资料 token 上限（约 {cfg.context_max_tokens} tokens）"})
            continue
        seen_texts.add(normalized)
        used.append(item)
        blocks.append(block)
        context_so_far += block
        per_doc[doc_id] = per_doc.get(doc_id, 0) + 1

    # —— 第二轮：相邻补全 ——
    # 同一文档的卡片先聚合、按序号排序，再找出序号连续的段拼接。
    # （卡片在相关度排序里可能被其他文档的卡片隔开，所以不能只看列表相邻关系）
    doc_order: list[str] = []
    by_doc: dict[str, list[RetrievedItem]] = {}
    for item in used:
        key = item.metadata.get("document_id") or item.metadata.get("source")
        if key not in by_doc:
            by_doc[key] = []
            doc_order.append(key)
        by_doc[key].append(item)

    final_items: list[RetrievedItem] = []
    final_blocks: list[str] = []
    merge_notes: list[str] = []
    for key in doc_order:
        group = sorted(
            by_doc[key],
            key=lambda it: (_chunk_seq(it) if _chunk_seq(it) is not None else 9999),
        )
        # 把序号连续的卡片分成一段
        runs: list[list[RetrievedItem]] = []
        for it in group:
            seq = _chunk_seq(it)
            if runs and seq is not None and _chunk_seq(runs[-1][-1]) == seq - 1:
                runs[-1].append(it)
            else:
                runs.append([it])
        for run in runs:
            first = run[0]
            final_items.append(first)
            number = len(final_items)
            label = citation_label(first, number)
            if len(run) > 1:
                merged_text = _merge_adjacent_texts([it.text for it in run])
                final_blocks.append(f"{label}\n{merged_text}")
                merge_notes.append(
                    f"{first.metadata.get('source')} 的 {len(run)} 张相邻卡片"
                    f"（{first.metadata.get('chunk_id')} ~ {run[-1].metadata.get('chunk_id')}）"
                    f"已拼接为一条，恢复被切片切断的上下文"
                )
            else:
                final_blocks.append(f"{label}\n{first.text}")

    context = "\n\n".join(final_blocks) if final_blocks else "（没有检索到任何相关资料）"
    return context, final_items, dropped, merge_notes


# ---------------- 直接运行本文件：看检索结果如何被拼成资料 ----------------
if __name__ == "__main__":
    from src.retriever import get_retriever

    question = sys.argv[1] if len(sys.argv) > 1 else "RAG 里的 Metadata 是干什么的？"
    print(f"问题：{question}\n")
    items = get_retriever().retrieve(question)
    context, used, dropped, merge_notes = build_context(items)
    print(f"召回 {len(items)} 条，实际采用 {len(used)} 条，丢弃 {len(dropped)} 条（重复/超限/超长）\n")
    for note in merge_notes:
        print(f"相邻补全：{note}")
    print("=" * 60)
    print(context)
    sys.exit(0)

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
from dataclasses import replace

from src.config import AppConfig, get_config
from src.metadata import chunk_sequence
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


def citation_label(item: RetrievedItem, number: int, *, show_version: bool = False) -> str:
    """一条资料的单行出处，如：[1] 来源: xx.md | 领域: learning | 章节: 基础 | 页码: 3"""
    meta = item.metadata
    label = f"[{number}] 来源: {meta.get('source', '?')}"
    if meta.get("domain"):
        label += f" | 领域: {meta['domain']}"
    if show_version:
        if meta.get("document_version"):
            label += f" | 文档版本: {meta['document_version']}"
        elif meta.get("version"):
            label += f" | 索引版本: {meta['version']}"
        if meta.get("status"):
            label += f" | 状态: {meta['status']}"
    if meta.get("section"):
        label += f" | 章节: {meta['section']}"
    if meta.get("page"):
        page_label = {"explicit_break": "显式分页段", "slide": "幻灯片"}.get(meta.get("page_kind"), "页码")
        label += f" | {page_label}: {meta['page']}"
    return label


def _chunk_seq(item: RetrievedItem) -> int | None:
    """从 chunk_id 末尾解析出卡片序号（doc_xxx_0003 → 3）；解析失败返回 None。"""
    return chunk_sequence(item.metadata)


# 切片时每张卡片正文开头注入的「来源文档：xxx」行（src/chunker.py，供按文件名检索）
_SOURCE_PREFIX_RE = re.compile(r"^来源文档：[^\n]*\n")


def _merge_run(run: list[RetrievedItem]) -> str:
    """拼接一段序号连续的同文档卡片，恢复被切片切断的上下文。

    - 后续卡片去掉重复的「来源文档：xxx」前缀（出处标签里已有），并在去掉前缀后再做
      重叠（Overlap）去重——带着前缀比对时开头永远对不上，切片重叠部分会被重复保留；
    - 换章节时补一行「【章节：xxx】」——出处标签只写第一张卡片的章节，不补的话后面卡片的
      标题（如网页的功能名称）在拼接后全部丢失，模型只看到一串没有名字的描述（列举时尤其致命）；
    - 不重叠的两张卡片之间换行，避免正文首尾粘连。
    """
    merged = run[0].text
    for previous, item in zip(run, run[1:]):
        body = _SOURCE_PREFIX_RE.sub("", item.text, count=1)
        before, current = _chunk_seq(previous), _chunk_seq(item)
        adjacent = (before is not None and current == before + 1
                    and all(previous.metadata.get(key) == item.metadata.get(key)
                            for key in ("document_id", "version", "page", "section", "section_path")))
        overlap, joiner = 0, "\n"
        if adjacent:
            recorded = item.metadata.get("overlap_chars")
            if type(recorded) is int and 0 <= recorded <= len(body):
                joiner = item.metadata.get("chunk_joiner", "\n")
                if joiner not in ("", " ", "\n", "\n\n") and (
                        not isinstance(joiner, str) or len(joiner) > 2 or joiner.strip()):
                    joiner = "\n"
                if (item.metadata.get("repeated_table_header")
                        or merged.endswith(body[:recorded].removesuffix(joiner))):
                    overlap = recorded
            elif recorded is None:
                # Old cards lack overlap metadata. Ignore coincidental short matches.
                overlap = next((k for k in range(min(300, len(merged), len(body)), 9, -1)
                                if merged.endswith(body[:k])), 0)
                if overlap:
                    joiner = ""
        piece = body[overlap:]
        section = item.metadata.get("section_path") or item.metadata.get("section")
        prev_section = previous.metadata.get("section_path") or previous.metadata.get("section")
        if section and section != prev_section:
            piece = f"【章节：{section}】\n{piece}"
        if piece:
            merged += joiner + piece
    return merged


def build_context(
    items: list[RetrievedItem], config: AppConfig | None = None,
    max_items: int | None = None, max_per_doc: int | None = None, *,
    rank_offset: int = 0, show_version: bool = False,
    unlimited_docs: set[str] | None = None,
) -> tuple[str, list[RetrievedItem], list[dict], list[str]]:
    """组装最终发给大模型的资料文本（V2.6：多样性 + 相邻补全）。

    选取规则：按相关度降序遍历候选——
      ① 内容与已选卡片完全相同的跳过（去重）；
      ② 同一文档最多采用 max_per_doc 条（保持来源多样；列举类提问可放开）；
      ③ 达到采用条数上限（默认 Top K）后停止；
      ④ 单条放入会超出 token 上限的跳过。

    相邻补全：同文档、卡片序号连续的已选卡片自动拼接（Overlap 去重），
    让被切片切断的上下文恢复完整。

    unlimited_docs（R2-b）：豁免单文档配额的 document_id 集合——多事实问答触发
    主导文档扩展时传入，被扩展文档不受 max_per_doc 限制（其余文档规则不变）。

    返回：(资料全文, 实际采用的资料, 丢弃明细, 相邻补全说明)
    """
    cfg = config or get_config()
    max_items = max_items or cfg.top_k
    max_per_doc = max_per_doc if max_per_doc is not None else cfg.context_max_per_doc

    # —— 第一轮：按规则选取 ——
    used: list[RetrievedItem] = []
    seen_texts: set[tuple] = set()
    per_doc: dict[str, int] = {}
    dropped: list[dict] = []
    context_so_far = ""  # 已放入的资料全文（用于 token 估算）

    for item in items:
        doc_id = item.metadata.get("document_id") or item.metadata.get("source")
        normalized = ("".join(item.text.split()), doc_id, item.metadata.get("version"),
                      item.metadata.get("page"),
                      item.metadata.get("section_path") or item.metadata.get("section"))
        if normalized in seen_texts:
            dropped.append({"rank": item.rank, "source": item.metadata.get("source"),
                            "reason": "内容重复（与更高相关度的卡片相同），只保留第一条"})
            continue
        if len(used) >= max_items:
            dropped.append({"rank": item.rank, "source": item.metadata.get("source"),
                            "limit": "top_k",
                            "reason": f"已满采用条数上限（Top {max_items}）"})
            break
        if (per_doc.get(doc_id, 0) >= max_per_doc
                and doc_id not in (unlimited_docs or ())):
            dropped.append({"rank": item.rank, "source": item.metadata.get("source"),
                            "limit": "per_document",
                            "reason": f"同一文档最多采用 {max_per_doc} 条（保持来源多样性）"})
            continue
        number = rank_offset + len(used) + 1
        block = f"{citation_label(item, number, show_version=show_version)}\n{item.text}"
        # Token 估算替代纯字符数：中英文混合时更接近模型实际容量
        candidate = f"{context_so_far}\n\n{block}" if context_so_far else block
        if estimate_tokens(candidate) > cfg.context_max_tokens:
            dropped.append({"rank": item.rank, "source": item.metadata.get("source"),
                            "limit": "max_evidence_tokens",
                            "reason": f"放入后会超出资料 token 上限（约 {cfg.context_max_tokens} tokens）"})
            continue
        seen_texts.add(normalized)
        used.append(item)
        context_so_far = candidate
        per_doc[doc_id] = per_doc.get(doc_id, 0) + 1

    # —— 第二轮：相邻补全 ——
    # 同一文档的卡片先聚合、按序号排序，再找出序号连续的段拼接。
    # （卡片在相关度排序里可能被其他文档的卡片隔开，所以不能只看列表相邻关系）
    # 按「文档 + 版本」分组：检索范围选「全部」时新旧版本会同时召回，
    # 旧版第 3 张和新版第 4 张序号连续，但内容不连续，拼起来就是一段错误资料
    doc_order: list[tuple] = []
    by_doc: dict[tuple, list[RetrievedItem]] = {}
    for item in used:
        key = (item.metadata.get("document_id") or item.metadata.get("source"),
               item.metadata.get("version"))
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
            previous = runs[-1][-1] if runs else None
            if (previous is not None and seq is not None and _chunk_seq(previous) == seq - 1
                    and all(previous.metadata.get(key) == it.metadata.get(key)
                            for key in ("page", "section", "section_path"))):
                runs[-1].append(it)
            else:
                runs.append([it])
        for run in runs:
            first = run[0]
            number = rank_offset + len(final_items) + 1
            label = citation_label(first, number, show_version=show_version)
            text = first.text
            if len(run) > 1:
                text = _merge_run(run)
                merge_notes.append(
                    f"{first.metadata.get('source')} 的 {len(run)} 张相邻卡片"
                    f"（{first.metadata.get('chunk_id')} ~ {run[-1].metadata.get('chunk_id')}）"
                    f"已拼接为一条，恢复被切片切断的上下文"
                )
            block = f"{label}\n{text}"
            if estimate_tokens("\n\n".join([*final_blocks, block])) > cfg.context_max_tokens:
                dropped.extend({"rank": item.rank, "source": item.metadata.get("source"),
                                "limit": "max_evidence_tokens",
                                "reason": "合并后超出资料 token 上限"} for item in run)
                continue
            metadata = {**first.metadata, "chunk_ids": [
                item.metadata["chunk_id"] for item in run if item.metadata.get("chunk_id")
            ]}
            for field in ("sections", "section_paths"):
                values = list(dict.fromkeys(value for item in run for value in item.metadata.get(field, [])))
                if values:
                    metadata[field] = values
            final_items.append(replace(first, rank=number, text=text, metadata=metadata))
            final_blocks.append(block)

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

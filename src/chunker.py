"""节点 3：知识切片（Chunker）。

产品视角：这一步相当于「把整本笔记撕成一页一页的知识卡片」——
- 优先沿着「标题 → 段落」的自然边界切，保证每张卡片主题完整；
- 卡片太长时按设定长度硬切，并让相邻卡片之间留一段重复（Overlap），
  避免「一句话被拦腰切断」导致语义断裂；
- 每张卡片都继承原文件的档案卡（来源、日期、版本等），
  并补充自己独有的信息：属于哪一章、第几页、卡片编号。
"""

from __future__ import annotations

import re
import sys
import uuid
from dataclasses import dataclass, field

from src.config import AppConfig, get_config
from src.cleaner import clean_document
from src.metadata import chunk_id_for
from src.parser import PAGE_MARKER_RE, ParsedDocument, parse_directory

# 切片 id 的命名空间（固定值；Qdrant 底层要求 UUID 形态的 Point id，
# 用 uuid5 保证「同一文档的第 N 片」永远得到同一个 UUID，重复入库不会产生重复数据）
_CHUNK_NAMESPACE = uuid.UUID("6f1d2c34-8a5e-4b7a-9c1d-2e3f4a5b6c7d")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# 硬切长段落时的句子边界
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;\n])")


@dataclass
class Chunk:
    """一张知识卡片：可读编号 + Qdrant 点位 id + 卡片正文 + 完整档案。"""

    chunk_id: str    # 可读稳定编号：doc_xxx_0001（存入 payload，方便人查）
    point_id: str    # Qdrant Point id（UUID 形态，同样稳定）
    text: str
    index: int
    metadata: dict = field(default_factory=dict)


def _split_by_pages(text: str) -> list[tuple[int | None, str]]:
    """按分页标记分段。返回 [(起始页码或 None, 这段正文), ...]。

    一段可能横跨多页，所以只记录起始页；多页段会在元数据里写「3-4」。
    """
    if not PAGE_MARKER_RE.search(text):
        return [(None, text)]

    segments: list[tuple[int | None, str]] = []
    current_page: int | None = None
    buffer: list[str] = []

    def flush() -> None:
        seg = "\n".join(buffer).strip()
        if seg:
            segments.append((current_page, seg))

    for line in text.split("\n"):
        match = PAGE_MARKER_RE.match(line.strip())
        if match:
            flush()
            current_page = int(match.group(1))
            buffer = []
        else:
            buffer.append(line)
    flush()
    return segments


def _split_by_sections(text: str) -> list[tuple[str | None, str | None, str]]:
    """按 Markdown 标题切章节。返回 [(章节名, 章节路径, 章节正文), ...]。

    章节路径形如「RAG > Metadata > Metadata Filter」，章节名是最后一级标题。
    """
    sections: list[tuple[str | None, str | None, str]] = []
    heading_stack: dict[int, str] = {}
    current_leaf: str | None = None
    current_path: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append((current_leaf, current_path, body))

    for line in text.split("\n"):
        match = _HEADING_RE.match(line)
        if match:
            flush()
            buffer = []
            level = len(match.group(1))
            heading_stack[level] = match.group(2).strip()
            for deeper in [k for k in heading_stack if k > level]:
                del heading_stack[deeper]
            current_path = " > ".join(heading_stack[k] for k in sorted(heading_stack))
            current_leaf = heading_stack[max(heading_stack)]
        else:
            buffer.append(line)
    flush()
    return sections


def _split_paragraphs(text: str) -> list[str]:
    """按空行切段落，段落内部的单个换行保留。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return paragraphs


def _hard_split_long_text(text: str, size: int, overlap: int) -> list[str]:
    """超长段落兜底：先按句子切，再打包；整句超长时按字符硬切。"""
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    pieces: list[str] = []
    buffer = ""
    for sentence in sentences:
        if len(sentence) > size:  # 连一个句子都超长：按字符硬切
            start = 0
            while start < len(sentence):
                pieces.append(sentence[start : start + size])
                start += size - overlap if size > overlap else size
            buffer = ""
            continue
        if buffer and len(buffer) + len(sentence) > size:
            pieces.append(buffer)
            buffer = ""
        buffer += sentence
    if buffer.strip():
        pieces.append(buffer)
    return pieces


def _pack_with_overlap(blocks: list[str], size: int, overlap: int) -> list[str]:
    """把若干「块」（段落）打包成不超过 size 的切片，相邻切片带 overlap。"""
    chunks: list[str] = []
    buffer = ""
    for block in blocks:
        if len(block) > size:
            # 当前段落本身超长：先收掉已攒的内容，再把长段落硬切
            if buffer.strip():
                chunks.append(buffer)
                buffer = ""
            pieces = _hard_split_long_text(block, size, overlap)
            chunks.extend(pieces)
            continue
        candidate = f"{buffer}\n\n{block}" if buffer else block
        if len(candidate) > size and buffer:
            chunks.append(buffer)
            if overlap > 0:
                tail = buffer[-overlap:]
                newline_at = tail.find("\n")
                if 0 < newline_at < len(tail) - 1:  # 让重复的起点落在行边界，别撕在半句话里
                    tail = tail[newline_at + 1 :]
                buffer = (tail + "\n\n" + block).strip()
            else:
                buffer = block
            continue
        buffer = candidate
    if buffer.strip():
        chunks.append(buffer)
    return chunks


def chunk_document(doc: ParsedDocument, config: AppConfig | None = None) -> list[Chunk]:
    """把一个清洗后的文档切成知识卡片列表。

    每张卡片完整继承文档 Metadata，只额外增加：chunk_id、section、section_path、page。
    """
    cfg = config or get_config()
    size = max(100, cfg.chunk_size)
    overlap = max(0, min(cfg.chunk_overlap, size // 2))
    document_id = doc.metadata.get("document_id") or "doc_unknown"

    chunks: list[Chunk] = []
    for page_start, page_text in _split_by_pages(doc.text):
        for section_leaf, section_path, section_text in _split_by_sections(page_text):
            for piece in _pack_with_overlap(_split_paragraphs(section_text), size, overlap):
                piece = piece.strip()
                if not piece:
                    continue
                page = str(page_start) if page_start else None
                metadata = dict(doc.metadata)
                metadata["chunk_id"] = chunk_id_for(document_id, len(chunks))
                metadata["section"] = section_leaf
                metadata["section_path"] = section_path
                metadata["page"] = page
                point_id = str(uuid.uuid5(_CHUNK_NAMESPACE, f"{document_id}::{len(chunks)}"))
                chunks.append(Chunk(chunk_id=metadata["chunk_id"], point_id=point_id,
                                    text=piece, index=len(chunks), metadata=metadata))
    return chunks


def chunk_documents(docs: list[ParsedDocument], config: AppConfig | None = None) -> list[Chunk]:
    """批量切片。"""
    all_chunks: list[Chunk] = []
    for doc in docs:
        all_chunks.extend(chunk_document(doc, config))
    return all_chunks


def clean_and_chunk(docs: list[ParsedDocument]) -> tuple[list[ParsedDocument], list[Chunk]]:
    """便捷入口：清洗 + 切片一步到位。"""
    cleaned = [clean_document(doc) for doc in docs]
    return cleaned, chunk_documents(cleaned)


# ---------------- 直接运行本文件：打印切片结果，人工检查 ----------------
if __name__ == "__main__":
    cfg = get_config()
    docs, failures = parse_directory(cfg.knowledge_dir)
    if failures:
        for failure in failures:
            print(f"❌ {failure}")
    cleaned_docs = [clean_document(doc) for doc in docs]
    chunks = chunk_documents(cleaned_docs, cfg)

    print(
        f"共 {len(docs)} 个文件 → {len(chunks)} 个切片"
        f"（chunk_size={cfg.chunk_size}, overlap={cfg.chunk_overlap}）\n"
    )
    for chunk in chunks:
        meta = chunk.metadata
        first_line = chunk.text.split("\n")[0][:50]
        flag = " ⚠️超长" if len(chunk.text) > cfg.chunk_size else ""
        print(f"[{chunk.chunk_id}] {meta['source']} | {meta.get('section') or '无章节'}"
              f" | 页码:{meta['page'] or '-'} | {len(chunk.text)}字{flag}")
        print(f"      domain={meta.get('domain')} category={meta.get('category')} status={meta.get('status')}")
        print(f"      开头：{first_line}……")
    sys.exit(0)

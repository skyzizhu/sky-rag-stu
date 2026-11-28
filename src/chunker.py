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
from typing import NamedTuple

from src.config import AppConfig, get_config
from src.cleaner import clean_document

# 切片容量单位：估算 token（V3.9）。中文≈0.8/字、英文≈4字符/token——中英文卡片密度一致。
# 实现委托 src.context.estimate_tokens（延迟导入：vector_store 依赖 chunker，
# chunker 不能在模块加载期反向 import context，否则循环导入）
_estimate_tokens_fn = None


def est_tokens(text: str) -> int:
    """估算文本的 token 数（与组装预算 src.context.estimate_tokens 同口径）。"""
    global _estimate_tokens_fn
    if _estimate_tokens_fn is None:
        from src.context import estimate_tokens as _fn
        _estimate_tokens_fn = _fn
    return _estimate_tokens_fn(text)
from src.parser import PAGE_MARKER_RE, ParsedDocument, markdown_lines, parse_directory

# 切片 id 的命名空间（固定值；Qdrant 底层要求 UUID 形态的 Point id，
# 用 uuid5 保证「同一文档的第 N 片」永远得到同一个 UUID，重复入库不会产生重复数据）
_CHUNK_NAMESPACE = uuid.UUID("6f1d2c34-8a5e-4b7a-9c1d-2e3f4a5b6c7d")

_SENTENCE_END = re.compile(r'[。！？!?；;]+[”’"\x27）)]*|\.+[”’"\x27)]*(?=\s|$)|\n')


@dataclass
class Chunk:
    """一张知识卡片：可读编号 + Qdrant 点位 id + 卡片正文 + 完整档案。"""

    chunk_id: str    # 可读稳定编号：doc_xxx_0001（存入 payload，方便人查）
    point_id: str    # Qdrant Point id（UUID 形态，同样稳定）
    text: str
    index: int
    metadata: dict = field(default_factory=dict)


class Section(NamedTuple):
    leaf: str | None
    path: str | None
    heading: str
    text: str
    leaves: list[str]
    paths: list[str]
    chapter: int


def _split_by_pages(text: str) -> list[tuple[int | None, str]]:
    """按分页标记分段。返回 [(起始页码或 None, 这段正文), ...]。

    每个片段对应一个原始分页标记。
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
    兼容入口：不带标题行、不做短小节合并的原始三段式；切片主流程用 _split_by_sections_raw。
    """
    return [(section.leaf, section.path, section.text) for section in _split_by_sections_raw(text)]


def _split_by_sections_raw(text: str, heading_stack: dict[int, str] | None = None) -> list[Section]:
    """按 Markdown 标题切章节（原始版）。

    返回 [(章节名, 章节路径, 标题行原文, 章节正文, 覆盖章节名列表), ...]——标题行原文供
    短小节合并时回注（合并后正文里不能丢掉各子块的小标题）；覆盖章节名列表供
    合并组记录本单元实际覆盖了哪些章节（单节时为单元素或空列表）。
    """
    sections: list[Section] = []
    heading_stack = {} if heading_stack is None else heading_stack
    chapter = 0
    current_heading_line = ""
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            leaf = heading_stack[max(heading_stack)] if heading_stack else None
            path = " > ".join(heading_stack[k] for k in sorted(heading_stack)) or None
            sections.append(Section(leaf, path, current_heading_line, body,
                                    [leaf] if leaf else [], [path] if path else [], chapter))

    for line, level, label in markdown_lines(text):
        if level:
            flush()
            buffer = []
            chapter += int(level == 1)
            heading_stack[level] = label
            for deeper in [k for k in heading_stack if k > level]:
                del heading_stack[deeper]
            current_heading_line = line.strip()
        else:
            buffer.append(line)
    flush()
    return sections


def _merge_short_sections(sections: list[Section], max_tokens: int) -> list[Section]:
    """相邻短小节合并（2026-11-08）：合计不超过单卡容量就合成一个切片单元。

    病灶（实测）：网页清洗器把每个视觉块当独立小节——Xtools 价格区 5 个碎块
    （53~159 字）各自成卡，远低于 600 容量，名额竞争下问年费会漏答。
    规则：连续的小节从头累加，加上下一节会超容量就封组开新组；
    组内章节名/路径取第一节，各子节标题行原文回注正文（合并后小标题不能丢）。
    单节自己就超容量的不参与合并（保持独立，交给打包阶段硬切）。
    2026-11-21（R3-a）：组首是无标题的文档开头段时，章节名/路径取组内第一个
    带标题的小节（结构化文档如 docx 手册的标题块常在首章之前，取 None 会让
    合并卡丢失章节定位）；同时返回值第 5 位携带本单元覆盖的全部章节名，
    供卡片元数据 sections 记录合并范围。
    2026-11-21（R3-b）：H1 视为章边界——相邻两节都是 H1 时不跨章合并，
    每章独立成卡（章内 H2+ 小节仍并入所属章）。H2+ 兄弟小节维持原合并行为
    （网页碎块的既有修复不受影响）。
    """
    if max_tokens <= 0 or not sections:
        return sections
    merged: list[Section] = []
    group: list[Section] = []
    group_text = ""

    def flush_group() -> None:
        if not group:
            return
        if len(group) == 1:
            merged.append(group[0])
            return
        named = next((sec for sec in group if sec.leaf), group[0])
        merged.append(Section(named.leaf, named.path, named.heading, group_text,
                              [leaf for sec in group for leaf in sec.leaves],
                              [path for sec in group for path in sec.paths], named.chapter))

    for section in sections:
        body = f"{section.heading}\n{section.text}".strip()
        candidate = f"{group_text}\n\n{body}" if group else body
        previous = group[-1] if group else None
        new_chapter = (previous and previous.chapter != section.chapter
                       and (previous.chapter > 0 or previous.path is not None))
        if group and (new_chapter or est_tokens(candidate) > max_tokens):
            flush_group()
            group, candidate = [], body
        group.append(section)
        group_text = candidate
    flush_group()
    return merged


# 列表型页面的条目标题行：「18，mac笔记本查看电池耗损度：」——
# 数字编号 + 顿号/逗号开头、冒号结尾、不超长（IP 行「127.0.0.1 xxx」以点分隔，不匹配）
_ITEM_TITLE_LINE = re.compile(r"^\d{1,3}[，、]\s*\S.*[：:]\s*$")


def _split_paragraphs(text: str) -> list[str]:
    """按空行切段落，段落内部的单个换行保留。

    切段后追加一步：段落内部若嵌着「编号标题行」（列表型页面整页无空行时，
    全文是一个巨型段落），在标题行处把段落拆开——让每个条目的标题与内容
    回到同一个块，容量边界只会落在条目之间，不再把条目从中间切断。
    """
    paragraphs: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        current: list[str] = []
        for line in para.split("\n"):
            if current and _ITEM_TITLE_LINE.match(line.strip()):
                paragraphs.append("\n".join(current).strip())
                current = [line]
            else:
                current.append(line)
        if current:
            paragraphs.append("\n".join(current).strip())
    return [p for p in paragraphs if p]


class Piece(NamedTuple):
    text: str
    overlap_chars: int = 0
    joiner: str = "\n\n"


def _prefix_end(text: str, size: int) -> int:
    low, high = 0, min(len(text), (size + 1) * 4)
    while low < high:
        middle = (low + high + 1) // 2
        if est_tokens(text[:middle]) <= size:
            low = middle
        else:
            high = middle - 1
    return low


def _hard_split_long_text(text: str, size: int, overlap: int) -> list[Piece]:
    """Partition long blocks, preferring complete sentences and preserving all body text."""
    pieces: list[Piece] = []
    repeated = ""
    joiner = "\n\n"
    while text:
        prefix = repeated + joiner if repeated else ""
        budget = size - est_tokens(prefix) - bool(prefix)
        end = _prefix_end(text, budget)
        sentence_end = max((match.end() for match in _SENTENCE_END.finditer(text[:end + 1])
                            if match.end() <= end), default=0)
        split_sentence = end < len(text) and not sentence_end
        if end < len(text):
            if sentence_end:
                end = sentence_end
            else:
                end = max((match.end() for match in re.finditer(r"\s+", text[:end])
                           if match.end() >= end // 2), default=end)
        head = text[:end]
        body = (prefix + head).rstrip()
        if head.strip():
            pieces.append(Piece(body, len(prefix), joiner))
        remaining = text[end:]
        whitespace = remaining[:len(remaining) - len(remaining.lstrip())]
        joiner = (head[len(head.rstrip()):] + whitespace)[:2] if remaining else ""
        repeated = _token_tail(body, overlap) if split_sentence and remaining else ""
        text = remaining.lstrip()
    return pieces


def _is_title_paragraph(block: str) -> bool:
    """识别「标题段」：短、以冒号结尾（如「18，mac笔记本查看电池耗损度：终端：」）。

    列表型网页的条目标题是独立段落；切片按段落装卡时，这样的标题段容易被
    留在上一张卡的末尾，而它的正文段进了下一张卡——模型只见标题不见内容。
    """
    return 0 < len(block.strip()) <= 60 and block.strip().endswith(("：", ":"))


def _bind_title_paragraphs(blocks: list[str], size: int) -> list[str]:
    """标题段绑定正文段（2026-11-08）：标题段与下一段合成一个打包单元。

    无条件绑定：合体超过单卡容量时由既有硬切逻辑兜底（按句子切，标题仍在首片），
    保证「标题永远和它的内容在一起」，不出现只见标题不见正文。
    """
    _ = size  # 预留：若未来要限制合体长度，用 size 判断
    bound: list[str] = []
    i = 0
    while i < len(blocks):
        block = blocks[i]
        if (_is_title_paragraph(block) and i + 1 < len(blocks)
                and not _is_title_paragraph(blocks[i + 1])):
            bound.append(f"{block}\n\n{blocks[i + 1]}")
            i += 2
            continue
        bound.append(block)
        i += 1
    return bound


def _token_tail(text: str, overlap_tokens: int) -> str:
    if overlap_tokens <= 0:
        return ""
    count = _prefix_end(text[::-1], overlap_tokens)
    return text[len(text) - count:].lstrip()


def _pack_with_overlap(blocks: list[str], size: int, overlap: int) -> list[Piece]:
    """Pack paragraphs, recording the exact copied prefix for reassembly."""
    chunks: list[Piece] = []
    buffer = ""
    copied = 0
    for block in blocks:
        if est_tokens(block) > size:
            if buffer:
                chunks.append(Piece(buffer, copied))
                buffer, copied = "", 0
            chunks.extend(_hard_split_long_text(block, size, overlap))
            continue
        candidate = f"{buffer}\n\n{block}" if buffer else block
        if est_tokens(candidate) > size and buffer:
            chunks.append(Piece(buffer, copied))
            tail = _token_tail(buffer, min(overlap, size - est_tokens(block) - 1))
            if tail and est_tokens(f"{tail}\n\n{block}") > size:
                tail = ""
            buffer = f"{tail}\n\n{block}" if tail else block
            copied = len(tail) + 2 if tail else 0
            continue
        buffer = candidate
    if buffer:
        chunks.append(Piece(buffer, copied))
    return chunks


def _spreadsheet_pieces(text: str, size: int) -> list[Piece]:
    header, _, body = text.partition("\n")
    if not body or est_tokens(header) > size // 2:
        return _pack_with_overlap(_split_paragraphs(text), size, 0)
    budget = size - est_tokens(header + "\n") - 1
    rows = _pack_with_overlap([line for line in body.splitlines() if line.strip()], budget, 0)
    return [Piece(f"{header}\n{row.text}", len(header) + 1 if index else 0, row.joiner)
            for index, row in enumerate(rows)]


def chunk_document(doc: ParsedDocument, config: AppConfig | None = None) -> list[Chunk]:
    """把一个清洗后的文档切成知识卡片列表。

    每张卡片完整继承文档 Metadata，只额外增加：chunk_id、section、section_path、page。
    """
    cfg = config or get_config()
    # V3.9：size / overlap 单位为估算 token（见模块头 est_tokens）——中英文卡片密度一致
    size = max(100, cfg.chunk_size)
    overlap = max(0, min(cfg.chunk_overlap, size // 2))
    document_id = doc.metadata.get("document_id") or "doc_unknown"
    # 文件名注入正文：让「按文件名找文件」在向量与 BM25 双通道都可命中
    # （与表头注入同属「档案信息入正文」原则），并让每张卡自报来源。
    source_name = str(doc.metadata.get("source") or "").strip()
    file_type = doc.metadata.get("file_type")

    def prefix(path):
        if not source_name:
            return ""
        value = f"来源文档：{source_name}" + (f"｜章节：{path}" if path else "")
        return value[:_prefix_end(value, size // 4)] + "\n"

    chunks: list[Chunk] = []
    heading_stack: dict[int, str] = {}
    for page_start, page_text in _split_by_pages(doc.text):
        if file_type in {"pptx", "ppt", "dps"}:
            heading_stack.clear()
        raw = _split_by_sections_raw(page_text, heading_stack)
        if file_type in {"xlsx", "xls", "et", "csv", "tsv"}:  # 表格类：按行组原样切，不做章节合并
            sections = raw
        else:
            max_prefix = max((est_tokens(prefix(section.path)) + 1 for section in raw), default=0)
            sections = _merge_short_sections(raw, max_tokens=size - max_prefix)
        for section in sections:
            header = prefix(section.path)
            budget = size - est_tokens(header) - bool(header)
            paragraphs = _bind_title_paragraphs(_split_paragraphs(section.text), budget)
            pieces = (_spreadsheet_pieces(section.text, budget) if file_type in {"xlsx", "xls", "et", "csv", "tsv"}
                      else _pack_with_overlap(paragraphs, budget, min(overlap, budget // 2)))
            for piece in pieces:
                page = str(page_start) if page_start else None
                metadata = dict(doc.metadata)
                version = str(doc.metadata.get("version") or "1.0")  # 版本进编号，新旧版本共存
                metadata["chunk_id"] = f"{document_id}_v{version}_{len(chunks):04d}"
                metadata["chunk_index"] = len(chunks)
                metadata["overlap_chars"] = piece.overlap_chars
                metadata["chunk_joiner"] = piece.joiner
                if file_type in {"xlsx", "xls", "et", "csv", "tsv"} and piece.overlap_chars:
                    metadata["repeated_table_header"] = True
                metadata["section"] = section.leaf
                metadata["section_path"] = section.path
                if len(section.leaves) > 1:
                    metadata["sections"] = section.leaves
                    metadata["section_paths"] = section.paths
                metadata["page"] = page
                if page:
                    metadata["page_kind"] = ("explicit_break" if file_type in {"docx", "doc", "wps"}
                                             else "slide" if file_type in {"pptx", "ppt", "dps"} else "physical")
                point_id = str(uuid.uuid5(_CHUNK_NAMESPACE, f"{document_id}::{version}::{len(chunks)}"))
                chunks.append(Chunk(chunk_id=metadata["chunk_id"], point_id=point_id,
                                    text=header + piece.text, index=len(chunks), metadata=metadata))
    return chunks


def chunk_documents(docs: list[ParsedDocument], config: AppConfig | None = None) -> list[Chunk]:
    """批量切片。"""
    all_chunks: list[Chunk] = []
    for doc in docs:
        all_chunks.extend(chunk_document(doc, config))
    return all_chunks


def clean_and_chunk(docs: list[ParsedDocument], config: AppConfig | None = None
                    ) -> tuple[list[ParsedDocument], list[Chunk]]:
    """便捷入口：清洗 + 切片一步到位。"""
    cleaned = [clean_document(doc) for doc in docs]
    return cleaned, chunk_documents(cleaned, config)


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
        flag = " ⚠️超长" if est_tokens(chunk.text) > cfg.chunk_size else ""
        print(f"[{chunk.chunk_id}] {meta['source']} | {meta.get('section') or '无章节'}"
              f" | 页码:{meta['page'] or '-'} | {len(chunk.text)}字{flag}")
        print(f"      domain={meta.get('domain')} status={meta.get('status')}")
        print(f"      开头：{first_line}……")
    sys.exit(0)

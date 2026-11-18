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
    兼容入口：不带标题行、不做短小节合并的原始三段式；切片主流程用 _split_by_sections_raw。
    """
    return [(leaf, path, body) for leaf, path, _line, body in _split_by_sections_raw(text)]


def _split_by_sections_raw(text: str) -> list[tuple[str | None, str | None, str, str]]:
    """按 Markdown 标题切章节（原始版）。

    返回 [(章节名, 章节路径, 标题行原文, 章节正文), ...]——标题行原文供
    短小节合并时回注（合并后正文里不能丢掉各子块的小标题）。
    """
    sections: list[tuple[str | None, str | None, str, str]] = []
    heading_stack: dict[int, str] = {}
    current_leaf: str | None = None
    current_path: str | None = None
    current_heading_line: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append((current_leaf, current_path, current_heading_line or "", body))

    in_code = False  # ``` 围栏内不识别标题（与 parser._read_markdown 同款规则）：
    # 代码块里的 "# 注释" 不是章节——否则代码块被拦腰切开，且注释会进
    # heading_stack 污染后续所有章节的 section_path 元数据
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            buffer.append(line)
            continue
        match = None if in_code else _HEADING_RE.match(line)
        if match:
            flush()
            buffer = []
            level = len(match.group(1))
            heading_stack[level] = match.group(2).strip()
            for deeper in [k for k in heading_stack if k > level]:
                del heading_stack[deeper]
            current_path = " > ".join(heading_stack[k] for k in sorted(heading_stack))
            current_leaf = heading_stack[max(heading_stack)]
            current_heading_line = line.strip()
        else:
            buffer.append(line)
    flush()
    return sections


def _merge_short_sections(sections: list[tuple[str | None, str | None, str, str]],
                          max_tokens: int) -> list[tuple[str | None, str | None, str, str]]:
    """相邻短小节合并（2026-11-08）：合计不超过单卡容量就合成一个切片单元。

    病灶（实测）：网页清洗器把每个视觉块当独立小节——Xtools 价格区 5 个碎块
    （53~159 字）各自成卡，远低于 600 容量，名额竞争下问年费会漏答。
    规则：连续的小节从头累加，加上下一节会超容量就封组开新组；
    组内章节名/路径取第一节，各子节标题行原文回注正文（合并后小标题不能丢）。
    单节自己就超容量的不参与合并（保持独立，交给打包阶段硬切）。
    """
    if max_tokens <= 0 or not sections:
        return sections
    merged: list[tuple[str | None, str | None, str, str]] = []
    group: list[tuple[str | None, str | None, str, str]] = []
    group_size = 0

    def flush_group() -> None:
        if not group:
            return
        if len(group) == 1:  # 没有发生合并：保持原样（正文不含标题行，行为不变）
            leaf, path, _line, body = group[0]
            merged.append((leaf, path, _line, body))
            return
        leaf, path = group[0][0], group[0][1]
        body = "\n\n".join(f"{line}\n{sec_body}" for _leaf, _path, line, sec_body in group)
        merged.append((leaf, path, group[0][2], body))

    for section in sections:
        section_size = est_tokens(section[2]) + est_tokens(section[3])
        if group and group_size + section_size > max_tokens:
            flush_group()
            group, group_size = [], 0
        group.append(section)
        group_size += section_size
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


def _hard_split_long_text(text: str, size: int, overlap: int) -> list[str]:
    """超长段落兜底：先按句子切，再打包；整句超长时按比例换算字符窗口硬切。

    size / overlap 单位为估算 token（V3.9）；字符窗口 = token 预算 × 该句的字符/token 比。
    """
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    pieces: list[str] = []
    buffer = ""
    for sentence in sentences:
        sentence_est = est_tokens(sentence)
        if sentence_est > size:  # 连一个句子都超长：按比例换算字符窗口硬切
            ratio = len(sentence) / max(1, sentence_est)
            window = max(50, int(size * ratio * 0.9))
            step = max(50, window - int(overlap * ratio))
            start = 0
            while start < len(sentence):
                pieces.append(sentence[start : start + window])
                start += step
            buffer = ""
            continue
        if buffer and est_tokens(buffer + sentence) > size:
            pieces.append(buffer)
            buffer = ""
        buffer += sentence
    if buffer.strip():
        pieces.append(buffer)
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
    """从文本末尾按行回退收集，直到估算 token 达到 overlap（天然落在行边界）。"""
    lines = text.split("\n")
    tail: list[str] = []
    total = 0
    for line in reversed(lines):
        tail.insert(0, line)
        total += est_tokens(line) + 1
        if total >= overlap_tokens:
            break
    return "\n".join(tail)


def _pack_with_overlap(blocks: list[str], size: int, overlap: int) -> list[str]:
    """把若干「块」（段落）打包成不超过 size（估算 token）的切片，相邻切片带 overlap。"""
    chunks: list[str] = []
    buffer = ""
    for block in blocks:
        if est_tokens(block) > size:
            # 当前段落本身超长：先收掉已攒的内容，再把长段落硬切
            if buffer.strip():
                chunks.append(buffer)
                buffer = ""
            pieces = _hard_split_long_text(block, size, overlap)
            chunks.extend(pieces)
            continue
        candidate = f"{buffer}\n\n{block}" if buffer else block
        if est_tokens(candidate) > size and buffer:
            chunks.append(buffer)
            if overlap > 0:
                tail = _token_tail(buffer, overlap)  # 按行回退至估算 token 达标（天然行对齐）
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
    # V3.9：size / overlap 单位为估算 token（见模块头 est_tokens）——中英文卡片密度一致
    size = max(100, cfg.chunk_size)
    overlap = max(0, min(cfg.chunk_overlap, size // 2))
    document_id = doc.metadata.get("document_id") or "doc_unknown"
    # 文件名注入正文：让「按文件名找文件」在向量与 BM25 双通道都可命中
    # （与表头注入同属「档案信息入正文」原则），并让每张卡自报来源。
    source_name = str(doc.metadata.get("source") or "").strip()

    chunks: list[Chunk] = []
    for page_start, page_text in _split_by_pages(doc.text):
        sections = _merge_short_sections(_split_by_sections_raw(page_text), max_tokens=size)
        for section_leaf, section_path, _heading_line, section_text in sections:
            paragraphs = _bind_title_paragraphs(_split_paragraphs(section_text), size)
            for piece in _pack_with_overlap(paragraphs, size, overlap):
                piece = piece.strip()
                if not piece:
                    continue
                if source_name:
                    piece = f"来源文档：{source_name}\n{piece}"
                page = str(page_start) if page_start else None
                metadata = dict(doc.metadata)
                version = str(doc.metadata.get("version") or "1.0")  # 版本进编号，新旧版本共存
                metadata["chunk_id"] = f"{document_id}_v{version}_{len(chunks):04d}"
                metadata["section"] = section_leaf
                metadata["section_path"] = section_path
                metadata["page"] = page
                point_id = str(uuid.uuid5(_CHUNK_NAMESPACE, f"{document_id}::{version}::{len(chunks)}"))
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
        flag = " ⚠️超长" if est_tokens(chunk.text) > cfg.chunk_size else ""
        print(f"[{chunk.chunk_id}] {meta['source']} | {meta.get('section') or '无章节'}"
              f" | 页码:{meta['page'] or '-'} | {len(chunk.text)}字{flag}")
        print(f"      domain={meta.get('domain')} category={meta.get('category')} status={meta.get('status')}")
        print(f"      开头：{first_line}……")
    sys.exit(0)

"""节点 2：清洗与结构化（Cleaner）。

产品视角：这一步相当于「文字保洁」——
把解析出来的正文里影响理解的杂物清理掉：
多余空行、异常空格、不可见字符、PDF 每页重复的页眉页脚……
同时尽量保住文章的结构：标题层级、段落、页码标记一个都不动。
"""

from __future__ import annotations

import re
import sys
from dataclasses import replace

from src.config import get_config
from src.parser import PAGE_MARKER_RE, ParsedDocument

# 不可见字符：零宽空格、零宽连接符、软连字符、BOM
_INVISIBLE_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]")


def _clean_line(line: str) -> str:
    """清理单行：去不可见字符、把制表符换成空格、压掉行内连续空格、去行尾空格。"""
    line = _INVISIBLE_RE.sub("", line)
    line = line.replace("\t", "    ")
    leading = re.match(r"^\s*", line).group(0)  # 行首缩进保留（Markdown 列表靠它）
    body = re.sub(r" {2,}", " ", line[len(leading) :])
    return (leading + body).rstrip()


def _merge_short_fragments(text: str, min_length: int = 15) -> str:
    """HTML 剪藏优化：把过短的碎片段（< min_length 字）拼进相邻段落。

    网页解析后常产生大量单行碎片段（菜单文字、按钮标签、分隔符残留），
    不合并会导致切片时碎片段自成一卡、语义断裂。
    """
    lines = text.split("\n")
    merged: list[str] = []
    buffer = ""
    for line in lines:
        stripped = line.strip()
        if len(stripped) < min_length and stripped:
            buffer += (" " if buffer else "") + stripped
            continue
        if buffer:
            merged.append(buffer)
            buffer = ""
        merged.append(line)
    if buffer:
        merged.append(buffer)
    return "\n".join(merged)


def clean_text(text: str) -> str:
    """清理整篇正文：逐行清洗后，把连续多个空行压成一个空行。"""
    lines = [_clean_line(line) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


def _split_pdf_pages(text: str) -> list[tuple[int, list[str]]]:
    """按分页标记把正文切成一页一页的。返回 [(页码, 该页的行), ...]。"""
    pages: list[tuple[int, list[str]]] = []
    current_page = 0
    current_lines: list[str] = []
    for line in text.split("\n"):
        match = PAGE_MARKER_RE.match(line.strip())
        if match:
            if current_lines:
                pages.append((current_page, current_lines))
            current_page = int(match.group(1))
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        pages.append((current_page, current_lines))
    return pages


def _drop_repeated_page_lines(pages: list[tuple[int, list[str]]]) -> tuple[list[tuple[int, list[str]]], list[str]]:
    """去掉 PDF 每页都重复出现的页眉/页脚行。

    规则（简单可靠优先）：某个非空行如果在一半以上的页里
    都出现在「页首或页尾」，就认定它是页眉/页脚，全部删掉。
    """
    removed: list[str] = []
    if len(pages) < 3:
        return pages, removed

    first_lines: dict[str, int] = {}
    last_lines: dict[str, int] = {}
    for _, lines in pages:
        non_empty = [line.strip() for line in lines if line.strip()]
        if not non_empty:
            continue
        first_lines[non_empty[0]] = first_lines.get(non_empty[0], 0) + 1
        if len(non_empty) > 1:
            last_lines[non_empty[-1]] = last_lines.get(non_empty[-1], 0) + 1

    threshold = max(2, len(pages) // 2)
    suspects = {text for text, n in first_lines.items() if n >= threshold and len(text) > 1}
    suspects |= {text for text, n in last_lines.items() if n >= threshold and len(text) > 1}
    if not suspects:
        return pages, removed

    cleaned_pages: list[tuple[int, list[str]]] = []
    for page_no, lines in pages:
        kept = [line for line in lines if line.strip() not in suspects]
        cleaned_pages.append((page_no, kept))
    return cleaned_pages, sorted(suspects)


def clean_document(doc: ParsedDocument) -> ParsedDocument:
    """清洗整个文档：普通清理 + 碎片合并 + PDF 页眉页脚清理。"""
    text = clean_text(doc.text)
    if doc.metadata.get("file_type") in ("html", "htm"):
        text = _merge_short_fragments(text)
    removed: list[str] = []
    if doc.metadata.get("file_type") == "pdf":
        pages = _split_pdf_pages(text)
        pages, removed = _drop_repeated_page_lines(pages)
        text = "\n".join(
            f"<<<__RAG_PAGE__={page_no}>>>\n" + "\n".join(lines) for page_no, lines in pages
        )
        text = clean_text(text)

    # 记录清理痕迹，方便回查
    metadata = dict(doc.metadata)
    if removed:
        metadata["cleaned_lines"] = removed
    return ParsedDocument(text=text, metadata=metadata)


def clean_documents(docs: list[ParsedDocument]) -> list[ParsedDocument]:
    """批量清洗。"""
    return [clean_document(doc) for doc in docs]


# ---------------- 直接运行本文件：打印清洗前后对比，人工检查 ----------------
if __name__ == "__main__":
    from src.parser import parse_directory

    cfg = get_config()
    docs, failures = parse_directory(cfg.knowledge_dir)
    print(f"解析成功 {len(docs)} 个，开始清洗……\n")

    for doc in docs:
        cleaned = clean_document(doc)
        before_lines = len(doc.text.split("\n"))
        after_lines = len(cleaned.text.split("\n"))
        print("=" * 60)
        print(f"文件：{doc.metadata['source']}")
        print(f"字符数：{len(doc.text)} → {len(cleaned.text)}   行数：{before_lines} → {after_lines}")
        if cleaned.metadata.get("cleaned_lines"):
            print(f"删除的页眉/页脚：{cleaned.metadata['cleaned_lines']}")
    sys.exit(0)

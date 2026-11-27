#!/usr/bin/env python3
"""全库 content_flags 词表扫描（V3.9.2）。

默认扫描向量库里已入库的卡片；也可用 --from-files 从源文件解析、清洗和切片。
不写向量库或台账，不调用模型。扫描结果可能含内部文档路径和引文，仅保存在本机。

用法：python scan_corpus.py [--from-files]
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from src.chunker import clean_and_chunk  # noqa: E402
from src.config import get_config  # noqa: E402
from src.content_flags import flag_instruction_like  # noqa: E402
from src.parser import SUPPORTED_EXTENSIONS, ParseError, parse_file  # noqa: E402
from src.project_directories import is_generated_info  # noqa: E402

# 图片走 OCR（本机纯 CPU 上很慢），本次不扫——结果里单独列出未扫的图片数
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp"}


def scan_store() -> dict:
    """首选：直接扫向量库里已入库的全部卡片（与检索看到的正文完全一致）。"""
    from src.vector_store import get_vector_store

    cards = get_vector_store().get_all_cards(limit=5000)
    chunks = [(c["payload"].get("path"), c["payload"].get("section"), c["payload"].get("chunk_id"),
               c["payload"].get("text", "")) for c in cards]
    return {"files_scanned": len({path for path, *_ in chunks}), "parse_failures": 0, "skipped_images": [],
            "chunks": chunks}


def scan_files() -> dict:
    """兜底：向量库不可用时，对源文件走与入库相同的 解析 → 清洗 → 切片（正文与入库一致）。"""
    cfg = get_config()
    root = cfg.knowledge_dir
    candidates = [p for p in sorted(root.rglob("*"))
                  if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)
                  and not p.name.startswith(".") and not is_generated_info(p)
                  and p.suffix.lower() in SUPPORTED_EXTENSIONS]
    files = [p for p in candidates if p.suffix.lower() not in SKIP_SUFFIXES]
    skipped_images = [p.name for p in candidates if p.suffix.lower() in SKIP_SUFFIXES]

    docs, failures = [], []
    for path in files:
        try:
            doc = parse_file(path)
            doc.metadata["path"] = path.relative_to(root).as_posix()  # 入库时由管线回填的相对路径
            docs.append(doc)
        except ParseError as exc:
            failures.append(str(exc))
    _, chunks = clean_and_chunk(docs, cfg)
    return {"files_scanned": len(files), "parse_failures": len(failures), "skipped_images": skipped_images,
            "chunks": [(c.metadata.get("path"), c.metadata.get("section"), c.chunk_id, c.text) for c in chunks],
            "failures": failures}


def main() -> None:
    mode = "files" if "--from-files" in sys.argv else "store"
    try:
        scanned = scan_files() if mode == "files" else scan_store()
    except Exception as exc:  # 向量库不可达等：退回源文件扫描并注明
        if mode == "files":
            raise
        print(f"[warn] 向量库扫描失败（{type(exc).__name__}: {str(exc)[:80]}），改用源文件扫描")
        scanned, mode = scan_files(), "files"
    chunks = scanned["chunks"]

    flagged = []
    for path, section, chunk_id, text in chunks:
        flags = flag_instruction_like(text)
        if flags:
            flagged.append({"path": path, "section": section, "chunk_id": chunk_id, "flags": flags})

    by_kind = Counter(flag["kind"] for item in flagged for flag in item["flags"])
    summary = {
        "mode": mode,
        "files_scanned": scanned["files_scanned"], "parse_failures": scanned["parse_failures"],
        "chunks": len(chunks),
        "flagged_chunks": len(flagged), "flagged_documents": len({item["path"] for item in flagged}),
        "flags_by_kind": dict(by_kind), "skipped_images": scanned["skipped_images"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    for item in flagged:
        print(f"\n[{item['path']}] 章节: {item['section']}")
        for flag in item["flags"]:
            print(f"   {flag['kind']:<20} {flag['quote'][:110]}")
    if scanned.get("failures"):
        print("\n解析失败（未纳入扫描）:")
        for failure in scanned["failures"]:
            print("   ", failure[:160])

    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / "corpus_scan.json").write_text(
        json.dumps({"summary": summary, "flagged": flagged, "parse_failures": scanned.get("failures", [])},
                   ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()

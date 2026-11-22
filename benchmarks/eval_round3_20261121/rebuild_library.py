# -*- coding: utf-8 -*-
"""步骤 4 重建窗口：全库强制重入库（应用 R2-c 章节前缀 + R3-b 一章一卡）。

关键约束（探针已验证）：
- 不传 defaults 时，权限（allowed_groups/users）、归属部门、项目关联、doc_type、
  版本均从台账继承，内容未变的强制重入库沿用版本；
- owner 不在台账里，不传会被清空 → 先从 Qdrant payload 读出每个文件的 owner，
  按 owner 分组批量重入库。
产出 results/rebuild_report.json。
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.pipeline import ingest_files  # noqa: E402
from src.parser import SUPPORTED_EXTENSIONS  # noqa: E402
from src.project_directories import is_generated_info  # noqa: E402
from src.vector_store import get_vector_store  # noqa: E402
from src.metadata import document_id_for  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
K = ROOT / "knowledge"
HERE = Path(__file__).resolve().parent


def _skip_hidden(p: Path) -> bool:
    return any(part.startswith(".") for part in p.relative_to(K).parts)


def main():
    files = sorted(p for p in K.rglob("*")
                   if p.is_file() and not p.name.startswith(".")
                   and p.suffix.lower() in SUPPORTED_EXTENSIONS
                   and not _skip_hidden(p) and not is_generated_info(p))
    print(f"待重建文件：{len(files)}")

    store = get_vector_store()
    by_owner: dict[str, list[Path]] = defaultdict(list)
    owner_missing = []
    for p in files:
        try:
            rel = p.resolve().relative_to(K.resolve()).as_posix()
        except ValueError:
            rel = p.name
        cards = store.get_all_cards(filters={"document_id": document_id_for(rel)}, limit=1)
        owner = (cards[0]["payload"].get("owner") or "") if cards else ""
        if not cards:
            owner_missing.append(rel)
        by_owner[owner].append(p)
    print("分组：", {o or "(空)": len(v) for o, v in by_owner.items()})
    if owner_missing:
        print("无卡片（新文件/未入库）：", owner_missing)

    rows = []
    t0 = time.time()
    for owner, paths in sorted(by_owner.items(), key=lambda kv: kv[0]):
        print(f"\n=== owner={owner or '(空)'}：{len(paths)} 个文件 ===")
        summary = ingest_files(paths=paths, owner=owner or None)
        rows.append({"owner": owner or "", "files": len(paths),
                     "ok": summary.ok_files, "failed": summary.failed_files,
                     "chunks": summary.total_chunks,
                     "new": len(summary.new_files), "updated": len(summary.updated_files),
                     "skipped": len(summary.skipped_files)})
    elapsed = time.time() - t0

    cat = store.document_catalog({}, 300, 100000, "indexed_at", None)
    docs = cat.pop("documents")
    print(f"\n重建完成：{elapsed:.0f}s，库内 {len(docs)} 文档 / {store.count()} 卡片")
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "rebuild_report.json").write_text(
        json.dumps({"elapsed": round(elapsed), "groups": rows,
                    "docs": len(docs), "cards": store.count(),
                    "owner_missing": owner_missing}, ensure_ascii=False, indent=2),
        encoding="utf-8")


if __name__ == "__main__":
    main()

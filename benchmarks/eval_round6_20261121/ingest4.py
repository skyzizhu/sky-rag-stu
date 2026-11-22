# -*- coding: utf-8 -*-
"""R6 入库：10 个新长文本文件 + 业务 metadata 正确率矩阵（含 H1 一章一卡与前缀校验）。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.pipeline import ingest_files  # noqa: E402
from src.vector_store import get_vector_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
K = ROOT / "knowledge"
RESULTS = Path(__file__).resolve().parent / "results"
RESULTS.mkdir(exist_ok=True)

ALL = ["all"]
PHOENIX = 7

# (rel, groups, users, dept, owner, pid, 期望)
PLAN = [
    ("work/星海数据治理管理制度_v2.0.md", ["研发部", "财务部"], [], "研发部", "wangfang", None,
     {"doc_type": "管理制度", "version": "2.0", "classification": "内部", "status": "active",
      "min_cards": 6, "min_sections": 6, "prefix": True}),
    ("work/星海2026年度经营计划.docx", ALL, [], "财务部", "zhaocai", None,
     {"doc_type": None, "version": "1.0", "classification": "公开", "status": "active",
      "min_cards": 4, "min_sections": 4, "prefix": True}),
    ("projects/凤凰项目/凤凰项目可行性研究报告.docx", ["研发部", "市场部"], [], "研发部", "wangfang", PHOENIX,
     {"doc_type": None, "version": "1.0", "classification": "内部", "status": "active",
      "project_id": PHOENIX, "min_cards": 4, "min_sections": 4, "prefix": True}),
    ("learning/星海项目管理手册_v1.0.docx", ALL, [], "研发部", "wangfang", None,
     {"doc_type": None, "version": "1.0", "classification": "公开", "status": "active",
      "min_cards": 4, "min_sections": 4, "prefix": True}),
    ("reference/星海合同/Phoenix技术方案建议书_2026.pdf", ["研发部"], [], "研发部", "wangfang", None,
     {"doc_type": None, "version": "1.0", "classification": "内部", "status": "active",
      "min_cards": 4, "pages_max": 4, "prefix": True}),
    ("reference/星海发布会/星海2026春季发布会纪要.pptx", ALL, [], "市场部", "liuye", None,
     {"doc_type": None, "version": "1.0", "classification": "公开", "status": "active",
      "min_cards": 6, "prefix": True}),
    ("reference/星海表格/星海2026项目总台账.xlsx", ["财务部"], [], "财务部", "zhaocai", None,
     {"doc_type": None, "version": "1.0", "classification": "内部", "status": "active",
      "min_cards": 1, "prefix": True}),
    ("work/星海供应商管理办法_2026修订.md", ALL, [], "财务部", "zhaocai", None,
     {"doc_type": "管理办法", "version": "1.0", "classification": "公开", "status": "active",
      "min_cards": 4, "min_sections": 4, "prefix": True}),
    ("archive/星海旧版差旅制度_2023.md", ALL, [], "财务部", "zhaocai", None,
     {"doc_type": "管理制度", "version": "1.0", "classification": "公开", "status": "archive",
      "min_cards": 1, "prefix": True}),
    ("projects/凤凰项目/X-4000需求规格_v1.0.docx", ["研发部", "市场部"], [], "研发部", "wangfang", PHOENIX,
     {"doc_type": None, "version": "1.0", "classification": "内部", "status": "active",
      "project_id": PHOENIX, "min_cards": 4, "min_sections": 4, "prefix": True}),
]


def main():
    from src.metadata import document_id_for
    rows, matrix = [], []
    field_pass = field_total = 0
    t_all = time.time()
    for rel, groups, users, dept, owner, pid, expect in PLAN:
        t0 = time.time()
        s = ingest_files(paths=[K / rel],
                         default_groups=groups if groups else None,
                         default_allowed_users=users if users else None,
                         default_department=dept, default_project_id=pid, owner=owner)
        elapsed = round(time.time() - t0, 1)
        row = s.file_rows[0] if s.file_rows else None
        rows.append({"path": rel, "ok": row is not None, "chunks": row.get("chunks") if row else 0,
                     "elapsed": elapsed, "errors": s.file_errors.get(rel)})
        print(f"  {'✓' if row else '✗'} {rel}  chunks={row.get('chunks') if row else 0}  {elapsed}s")

        store = get_vector_store()
        cards = store.get_all_cards(
            filters={"document_id": document_id_for(rel), "status": expect["status"]}, limit=300)
        if not cards:
            matrix.append({"path": rel, "error": "no cards"})
            continue
        payloads = [c["payload"] for c in cards]
        p0 = payloads[0]
        actual = {k: p0.get(k) for k in ("doc_type", "department", "version",
                                         "classification", "allowed_groups", "allowed_users",
                                         "project_id", "status", "owner")}
        sections = sorted({p.get("section") or "" for p in payloads} - {""})
        pages = {p.get("page") for p in payloads if p.get("page")}
        checks = {}

        def check(name, ok):
            nonlocal field_pass, field_total
            checks[name] = bool(ok)
            field_total += 1
            field_pass += 1 if ok else 0

        check("classification", actual["classification"] == expect["classification"])
        check("allowed_groups", (actual["allowed_groups"] or []) == groups)
        check("allowed_users", (actual["allowed_users"] or []) == users)
        check("project_id", actual["project_id"] == expect.get("project_id"))
        check("status", actual["status"] == expect["status"])
        check("owner", actual["owner"] == owner)
        check("version", str(actual["version"]) == expect["version"])
        if expect.get("doc_type") is not None:
            check("doc_type", actual["doc_type"] == expect["doc_type"])
        check("doc_type_filled", bool(actual["doc_type"]))
        check("min_cards", len(payloads) >= expect["min_cards"])
        if expect.get("min_sections"):
            check("min_sections", len(sections) >= expect["min_sections"])
        if expect.get("pages_max"):
            check("page_metadata", bool(pages) and max(int(str(p)) for p in pages) >= expect["pages_max"])
        if expect.get("prefix"):
            head = (payloads[0].get("text") or "").split("\n")[0]
            check("prefix", head.startswith("来源文档：") and ("章节：" in head or not expect.get("min_sections")))
        matrix.append({"path": rel, "actual": actual, "cards": len(payloads),
                       "sections": sections, "pages": sorted(str(x) for x in pages),
                       "checks": checks, "passed": all(checks.values())})
        flags = " ".join(k for k, v in checks.items() if not v) or "all-ok"
        print(f"     [{flags}] cards={len(payloads)} sections={len(sections)} pages={sorted(pages) if pages else '-'}")

    store = get_vector_store()
    summary = {"ingest": rows, "matrix": matrix,
               "field_pass": field_pass, "field_total": field_total,
               "field_rate": round(100 * field_pass / field_total, 1) if field_total else 0,
               "file_pass": sum(1 for m in matrix if m.get("passed")), "file_total": len(matrix),
               "library": {"docs": None, "cards": store.count()},
               "elapsed": round(time.time() - t_all)}
    cat = store.document_catalog({}, 500, 100000, "indexed_at", None)
    summary["library"]["docs"] = len(cat["documents"])
    (RESULTS / "ingest4_results.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nmetadata 字段正确率：{field_pass}/{field_total} = {summary['field_rate']}%")
    print(f"文件级全过：{summary['file_pass']}/{summary['file_total']}；"
          f"库：{summary['library']['docs']} 文档 / {summary['library']['cards']} 卡片；"
          f"总耗时 {summary['elapsed']}s")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""第三轮入库 + 业务 metadata 正确率矩阵校验。

对 10 个长文本文件逐个入库（显式路径），随后逐字段比对 Qdrant payload 与期望值：
doc_type（front matter / 文件名推断）/ department（front matter / 上传默认 / 未指定）/
version（文件名优先于 front matter）/ document_version（正文声明核对状态）/
classification（三级密级）/ project_id / status（archive 目录强制）/ owner /
卡片数与章节多样性（长文本切片覆盖）。

产出 results/ingest3_results.json（入库明细 + metadata 校验矩阵 + 正确率）。
"""
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

PID = {"星云": 2, "天枢": 3, "北斗": 4, "昆仑": 5, "沧海": 6}
ALL = ["all"]

# (相对路径, groups, users, default_department, owner, project_id, 期望metadata)
# doc_type=None 表示记录实际值并检查"非空或推断来源"（不做硬断言，避免与实现策略耦合）
PLAN = [
    ("work/星海员工手册_2025版_v3.0.md", ALL, [], "人力资源部", "lili", None,
     {"doc_type": "员工手册", "department": "人力资源部", "version": "3.0",
      "classification": "公开", "groups": ALL, "users": [], "project_id": None,
      "status": "active", "min_cards": 2, "min_sections": None,
      "docver_status_contains": "needs_review"}),
    ("projects/星云项目/星云项目招标技术规格书_v1.2.md", ["研发部"], [], "研发部", "wangfang", PID["星云"],
     {"doc_type": "技术规格书", "department": "研发部", "version": "1.2",
      "classification": "内部", "groups": ["研发部"], "users": [], "project_id": PID["星云"],
      "status": "active", "min_cards": 3, "min_sections": 8,
      "docver_value": "1.2", "docver_status_contains_any": ["确认", "declared"]}),
    ("reference/星海合同/华信运维服务合同_2025_v1.0.pdf", ["财务部"], [], "财务部", "zhaocai", None,
     {"doc_type": None, "department": "财务部", "version": "1.0",
      "classification": "内部", "groups": ["财务部"], "users": [], "project_id": None,
      "status": "active", "min_cards": 3, "min_sections": None,
      "docver_value": "1.0", "pages_max": 4}),
    ("learning/星海新员工入职培训手册_v2.0.docx", ALL, [], None, "wangfang", None,
     {"doc_type": None, "department": "", "version": "2.0",
      "classification": "公开", "groups": ALL, "users": [], "project_id": None,
      "status": "active", "min_cards": 1, "min_sections": None,
      "docver_status_contains": "needs_review"}),
    ("work/星海合同管理制度_v1.1.md", ["财务部", "研发部"], [], "财务部", "zhaocai", None,
     {"doc_type": "管理制度", "department": "财务部", "version": "1.1",
      "classification": "内部", "groups": ["财务部", "研发部"], "users": [], "project_id": None,
      "status": "active", "min_cards": 3, "min_sections": None}),
    ("projects/昆仑项目/昆仑项目需求说明书_v2.0.docx", ["研发部", "市场部"], [], "研发部", "wangfang", PID["昆仑"],
     {"doc_type": None, "department": "研发部", "version": "2.0",
      "classification": "内部", "groups": ["研发部", "市场部"], "users": [], "project_id": PID["昆仑"],
      "status": "active", "min_cards": 1, "min_sections": None}),
    ("work/星海数据备份与恢复管理办法_v1.1.md", ["研发部"], [], "研发部", "wangfang", None,
     {"doc_type": "管理制度", "department": "研发部", "version": "1.1",
      "classification": "内部", "groups": ["研发部"], "users": [], "project_id": None,
      "status": "active", "min_cards": 1, "min_sections": None,
      "note": "文件名 v1.1 应优先于 front matter version 1.0"}),
    ("archive/星海旧版采购管理制度_2020.md", ALL, [], "财务部", "zhaocai", None,
     {"doc_type": "管理制度", "department": "财务部", "version": "1.0",
      "classification": "公开", "groups": ALL, "users": [], "project_id": None,
      "status": "archive", "min_cards": 1, "min_sections": None}),
    ("reference/星海表格/星海项目台账_2025.xlsx", ["财务部"], [], "财务部", "zhaocai", None,
     {"doc_type": None, "department": "财务部", "version": "1.0",
      "classification": "内部", "groups": ["财务部"], "users": [], "project_id": None,
      "status": "active", "min_cards": 1, "min_sections": None}),
    ("reference/星海发布会/星海2025年中发布会纪要.pptx", ALL, [], "市场部", "liuye", None,
     {"doc_type": None, "department": "市场部", "version": "1.0",
      "classification": "公开", "groups": ALL, "users": [], "project_id": None,
      "status": "active", "min_cards": 3, "min_sections": None}),
]


def main():
    print("=" * 72)
    print("第三轮入库：10 个长文本文件")
    print("=" * 72)
    ingest_rows = []
    for rel, groups, users, dept, owner, pid, expect in PLAN:
        path = K / rel
        t0 = time.time()
        summary = ingest_files(paths=[path],
                               default_groups=groups if groups else None,
                               default_allowed_users=users if users else None,
                               default_department=dept,
                               default_project_id=pid,
                               owner=owner)
        elapsed = time.time() - t0
        row = summary.file_rows[0] if summary.file_rows else None
        rec = {"path": rel, "elapsed": round(elapsed, 2), "ok": row is not None,
               "chunks": row.get("chunks") if row else 0,
               "errors": summary.file_errors.get(rel)}
        ingest_rows.append(rec)
        mark = "✓" if rec["ok"] and rec["chunks"] > 0 else "✗"
        print(f"  {mark} {rel}  chunks={rec['chunks']}  {elapsed:.1f}s  {rec['errors'] or ''}")

    print("\nmetadata 校验矩阵")
    store = get_vector_store()
    from src.metadata import document_id_for
    matrix = []
    field_pass, field_total = 0, 0
    for (rel, groups, users, dept, owner, pid, expect) in PLAN:
        doc_id = document_id_for(rel)
        status_key = expect["status"]
        cards = store.get_all_cards(filters={"document_id": doc_id, "status": status_key}, limit=300)
        if not cards:
            matrix.append({"path": rel, "error": "no cards with expected status"})
            continue
        payloads = [c["payload"] for c in cards]
        s = payloads[0]
        actual = {
            "doc_type": s.get("doc_type", ""),
            "department": s.get("department") or "",
            "version": str(s.get("version")),
            "document_version": s.get("document_version"),
            "document_version_status": s.get("document_version_status"),
            "classification": s.get("classification"),
            "allowed_groups": s.get("allowed_groups"),
            "allowed_users": s.get("allowed_users"),
            "project_id": s.get("project_id"),
            "status": s.get("status"),
            "owner": s.get("owner"),
        }
        cards_n = len(payloads)
        sections = {p.get("section") or "" for p in payloads} - {""}
        pages = {p.get("page") for p in payloads if p.get("page")}
        row = {"path": rel, "actual": actual, "cards": cards_n,
               "sections": sorted(sections), "pages": sorted(str(p) for p in pages),
               "checks": {}, "field_results": {}}

        def check(name, ok):
            nonlocal field_pass, field_total
            row["checks"][name] = bool(ok)
            field_total += 1
            field_pass += 1 if ok else 0

        check("doc_type", expect["doc_type"] is None
              or actual["doc_type"] == expect["doc_type"])
        check("doc_type_filled", bool(actual["doc_type"]))
        check("department", actual["department"] == (expect["department"] or ""))
        check("version", actual["version"] == expect["version"])
        if "docver_value" in expect:
            check("document_version", actual["document_version"] == expect["docver_value"])
        if "docver_status_contains" in expect:
            check("docver_status", expect["docver_status_contains"] in str(actual["document_version_status"] or ""))
        if "docver_status_contains_any" in expect:
            check("docver_status", any(x in str(actual["document_version_status"] or "")
                                       for x in expect["docver_status_contains_any"]))
        check("classification", actual["classification"] == expect["classification"])
        check("allowed_groups", (actual["allowed_groups"] or []) == expect["groups"])
        check("allowed_users", (actual["allowed_users"] or []) == expect["users"])
        check("project_id", actual["project_id"] == expect["project_id"])
        check("status", actual["status"] == expect["status"])
        check("owner", actual["owner"] == owner)
        check("min_cards", cards_n >= expect["min_cards"])
        if expect.get("min_sections"):
            check("min_sections", len(sections) >= expect["min_sections"])
        if expect.get("pages_max"):
            check("page_metadata", bool(pages) and max(int(str(p)) for p in pages) >= expect["pages_max"])
        row["passed"] = all(row["checks"].values())
        matrix.append(row)
        flags = " ".join(k for k, v in row["checks"].items() if not v) or "all-ok"
        print(f"  {'✓' if row['passed'] else '✗'} {rel}  [{flags}]  cards={cards_n} "
              f"sections={len(sections)} pages={sorted(pages)[:6] if pages else '-'}")

    print("\n库总量：", get_vector_store().count(), "卡片")
    summary = {"ingest": ingest_rows,
               "metadata_matrix": matrix,
               "field_pass": field_pass, "field_total": field_total,
               "field_rate": round(100 * field_pass / field_total, 1) if field_total else 0,
               "file_pass": sum(1 for r in matrix if r.get("passed")),
               "file_total": len(matrix)}
    (RESULTS / "ingest3_results.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"metadata 字段正确率：{field_pass}/{field_total} = {summary['field_rate']}%")
    print(f"文件级全部通过：{summary['file_pass']}/{summary['file_total']}")


if __name__ == "__main__":
    main()

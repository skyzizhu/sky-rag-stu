# -*- coding: utf-8 -*-
"""第二轮入库：
1) 12 个新文件逐个入库（显式路径 = 强制处理），按文件设置权限/归属/项目关联；
2) 星云章程 / 天枢立项 / 北斗总结 重新入库补挂 project_id（内容未变，版本沿用）；
3) 入库后校验 payload（project_id、权限、版本）。

项目 ID：星云=2 天枢=3 北斗=4 昆仑=5 沧海=6（setup_projects.py 登记）。
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

# (相对路径, allowed_groups, allowed_users, 归属部门, 入库人, project_id)
PLAN = [
    ("projects/昆仑项目/昆仑项目立项报告.md",           ["研发部", "市场部"], [], "研发部", "wangfang", PID["昆仑"]),
    ("projects/沧海项目/沧海项目验收报告.docx",         ["研发部"], [], "研发部", "wangfang", PID["沧海"]),
    ("work/星海差旅管理办法_2025修订.md",              ALL, [], "财务部", "zhaocai", None),
    ("work/星海高管薪酬方案.md",                       [], ["lili"], "人力资源部", "lili", None),
    ("work/星海付款审批细则.md",                       ["财务部"], [], "财务部", "zhaocai", None),
    ("work/星海IT资产管理办法.txt",                    ALL, [], "研发部", "wangfang", None),
    ("archive/星海旧版考勤制度_2021.md",               ALL, [], "人力资源部", "lili", None),
    ("reference/星海表格/2025年度部门预算表.xlsx",      ["财务部"], [], "财务部", "zhaocai", None),
    ("reference/星海表格/星海供应商名录.xlsx",          ["财务部"], [], "财务部", "zhaocai", None),
    ("reference/星海合同/蓝鲸维保服务合同_2025.pdf",    ["财务部"], [], "财务部", "zhaocai", None),
    ("reference/星海简介/昆仑项目启动新闻.html",        ALL, [], "市场部", "liuye", None),
    ("reference/星海发布会/星海2025年度规划汇报.pptx",  ALL, [], "市场部", "liuye", None),
    # 补挂项目关联（内容未变：显式重入库沿用版本，只更新元数据）
    ("projects/星云项目/星云项目章程.md",              ["研发部"], [], "研发部", "wangfang", PID["星云"]),
    ("projects/天枢项目/天枢项目立项报告.docx",        ["研发部", "市场部"], [], "研发部", "wangfang", PID["天枢"]),
    ("projects/北斗项目/北斗项目总结报告.md",          ["研发部"], [], "研发部", "wangfang", PID["北斗"]),
]


def main():
    rows = []
    print("=" * 70)
    print(f"第二轮入库：{len(PLAN)} 个文件（12 新文件 + 3 个补挂项目关联）")
    print("=" * 70)
    for rel, groups, users, dept, owner, pid in PLAN:
        path = K / rel
        t0 = time.time()
        summary = ingest_files(
            paths=[path],
            default_groups=groups if groups else None,
            default_allowed_users=users if users else None,
            default_department=dept,
            default_project_id=pid,
            owner=owner,
        )
        elapsed = time.time() - t0
        row = summary.file_rows[0] if summary.file_rows else None
        rec = {"path": rel, "elapsed": round(elapsed, 2), "ok": row is not None,
               "chunks": row.get("chunks") if row else 0,
               "classification": row.get("classification") if row else None,
               "allowed_groups": row.get("allowed_groups") if row else None,
               "allowed_users": row.get("allowed_users") if row else None,
               "project_id": row.get("project_id") if row else None,
               "version": row.get("version") if row else None,
               "owner": row.get("owner") if row else None,
               "errors": summary.file_errors.get(rel)}
        rows.append(rec)
        mark = "✓" if rec["ok"] else "✗"
        print(f"  {mark} {rel}  chunks={rec['chunks']} ver={rec['version']} "
              f"class={rec['classification']} groups={rec['allowed_groups']} "
              f"users={rec['allowed_users']} pid={rec['project_id']} {elapsed:.1f}s")

    print("\n入库后 payload 校验")
    store = get_vector_store()
    from src.metadata import document_id_for
    verify = []
    for rel, *_ in PLAN:
        doc_id = document_id_for(rel)
        cards = store.get_all_cards(filters={"document_id": doc_id, "status": "active"}, limit=200)
        if cards:
            s = cards[0]["payload"]
            verify.append({"path": rel, "active_cards": len(cards),
                           "project_id": s.get("project_id"),
                           "classification": s.get("classification"),
                           "allowed_groups": s.get("allowed_groups"),
                           "allowed_users": s.get("allowed_users"),
                           "version": s.get("version")})
            print(f"  {rel:<46} cards={len(cards)} pid={s.get('project_id')} "
                  f"class={s.get('classification')} ver={s.get('version')}")
        else:
            verify.append({"path": rel, "active_cards": 0})
            print(f"  ⚠ {rel} 无 active 卡片")

    cat = store.document_catalog({}, 300, 1000, "indexed_at", None)
    print(f"\n库总量：{len(cat['documents'])} 文档 / {store.count()} 卡片")
    (RESULTS / "ingest2_results.json").write_text(
        json.dumps({"ingest": rows, "verify": verify,
                    "catalog_total": len(cat["documents"])}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print("结果已写入 results/ingest2_results.json")


if __name__ == "__main__":
    main()

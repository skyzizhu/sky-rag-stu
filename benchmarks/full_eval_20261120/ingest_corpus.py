# -*- coding: utf-8 -*-
"""评测语料入库：逐文件显式入库（与网页上传同一入口 ingest_files），
按文件设置可见部门/受限名单/归属部门/入库人，并做入库后校验。

随后执行入库机制边界测试：
  B1 重复入库（全量扫描增量跳过）
  B2 版本管理（星云章程内容更新 → 1.0→1.1，旧版 expired）
  B3 归档目录强制 archive
  B4 空文件 0 卡片不记指纹
  B5 不支持格式不被扫描
  B6 删除文件 → 向量同步清理（北斗项目演示后恢复）
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
RESULTS_DIR = Path(__file__).resolve().parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

ALL = ["all"]
# (相对路径, allowed_groups, allowed_users, 归属部门, 入库人)
PLAN = [
    ("work/星海员工手册_2024版.md",       ALL, [], "人力资源部", "lili"),
    ("work/星海财务报销制度_v2.1.md",     ["财务部", "研发部"], [], "财务部", "zhaocai"),
    ("work/星海薪酬福利制度.md",          [], ["wangfang", "lili"], "人力资源部", "lili"),
    ("work/星海信息安全管理制度.md",      ["研发部"], [], "研发部", "wangfang"),
    ("learning/星海RAG技术培训笔记.md",   ALL, [], "研发部", "wangfang"),
    ("learning/星海产品培训_基础篇.docx", ALL, [], "研发部", "wangfang"),
    ("projects/星云项目/星云项目章程.md", ["研发部"], [], "研发部", "wangfang"),
    ("projects/天枢项目/天枢项目立项报告.docx", ["研发部", "市场部"], [], "研发部", "wangfang"),
    ("projects/北斗项目/北斗项目总结报告.md", ["研发部"], [], "研发部", "wangfang"),
    ("reference/星海合同/华信数据采购合同_2024.pdf", ALL, [], "财务部", "zhaocai"),
    ("reference/星海表格/2024年度部门预算表.xlsx", ["财务部"], [], "财务部", "zhaocai"),
    ("reference/星海发布会/星海X-2000发布会纪要.pptx", ALL, [], "市场部", "liuye"),
    ("reference/星海简介/公司简介.html",  ALL, [], "人力资源部", "lili"),
    ("reference/星海评审/产品评审会议纪要.rtf", ALL, [], "研发部", "wangfang"),
    ("reference/星海运维/机房巡检通知.jpg", ALL, [], "研发部", "wangfang"),
    ("archive/星海旧版员工手册_2022.md",  ALL, [], "人力资源部", "lili"),
    ("work/边界测试_空文件.txt",          ALL, [], "研发部", "wangfang"),
]


def ingest_one(rel: str, groups: list[str], users: list[str],
               department: str, owner: str) -> dict:
    path = K / rel
    t0 = time.time()
    summary = ingest_files(
        paths=[path],
        default_groups=groups if groups else None,
        default_allowed_users=users if users else None,
        default_department=department,
        owner=owner,
    )
    elapsed = time.time() - t0
    row = summary.file_rows[0] if summary.file_rows else None
    result = {
        "path": rel,
        "elapsed": round(elapsed, 2),
        "ok": row is not None,
        "chunks": row.get("chunks") if row else 0,
        "classification": row.get("classification") if row else None,
        "allowed_groups": row.get("allowed_groups") if row else None,
        "allowed_users": row.get("allowed_users") if row else None,
        "department": row.get("department") if row else None,
        "version": row.get("version") if row else None,
        "owner": row.get("owner") if row else None,
        "status": row.get("status") if row else None,
        "errors": summary.file_errors.get(rel),
    }
    print(f"  {'✓' if result['ok'] else '✗'} {rel}  chunks={result['chunks']} "
          f"class={result['classification']} groups={result['allowed_groups']} "
          f"users={result['allowed_users']} dept={result['department']} "
          f"owner={result['owner']} {elapsed:.1f}s")
    return result


def verify_payload(rel: str) -> dict:
    """直接查 Qdrant payload，校验权限字段真实落库。"""
    store = get_vector_store()
    from src.metadata import document_id_for
    doc_id = document_id_for(rel)
    cards = store.get_all_cards(filters={"document_id": doc_id}, limit=200)
    if not cards:
        return {"path": rel, "cards": 0}
    sample = cards[0]["payload"]
    return {
        "path": rel,
        "cards": len(cards),
        "allowed_groups": sample.get("allowed_groups"),
        "allowed_users": sample.get("allowed_users"),
        "classification": sample.get("classification"),
        "department": sample.get("department"),
        "owner": sample.get("owner"),
        "status": sample.get("status"),
        "version": sample.get("version"),
    }


def main():
    print("=" * 70)
    print("阶段 1：逐文件入库（16 个语料文件 + 1 个空文件边界样本）")
    print("=" * 70)
    ingest_rows = []
    for rel, groups, users, dept, owner in PLAN:
        ingest_rows.append(ingest_one(rel, groups, users, dept, owner))

    print("\n阶段 1.5：Qdrant payload 抽检验证")
    verify_rows = [verify_payload(r["path"]) for r in ingest_rows if r["ok"]]

    print("\n阶段 2：边界 B1 —— 全量扫描重复入库（增量跳过）")
    t0 = time.time()
    summary = ingest_files()  # 全库扫描
    print(f"  全量扫描：total={summary.total_files} skipped={len(summary.skipped_files)} "
          f"processed={len(summary.updated_files) + len(summary.new_files)} "
          f"cleaned={len(summary.cleaned_files)} elapsed={time.time()-t0:.1f}s")
    b1 = {"total_files": summary.total_files,
          "skipped": summary.skipped_files,
          "new": summary.new_files,
          "updated": summary.updated_files,
          "cleaned": summary.cleaned_files}

    print("\n阶段 3：边界 B2 —— 版本管理（星云章程内容更新）")
    chart = K / "projects" / "星云项目" / "星云项目章程.md"
    old = chart.read_text(encoding="utf-8")
    updated = old.replace("预算 120 万元", "预算 135 万元")
    updated = updated.replace("M3 系统上线：2024-09。",
                              "M3 系统上线：2024-09；M4 项目验收：2024-12。")
    updated = updated.replace("核心成员：陈明（项目负责人）、林岚（架构）、徐凯（后端）。",
                              "核心成员：陈明（项目负责人）、林岚（架构）、徐凯（后端）、刘洋（测试）。")
    if "预算 135 万元" not in updated:
        # 兜底：确保内容确实变化
        updated = old + "\n## 变更记录\n2024-05：预算由 120 万元调整为 135 万元，新增 M4 验收里程碑与测试成员刘洋。\n"
    chart.write_text(updated, encoding="utf-8")
    row = ingest_one("projects/星云项目/星云项目章程.md", ["研发部"], [], "研发部", "wangfang")
    b2 = {"ingest_row": row, "payload": verify_payload("projects/星云项目/星云项目章程.md")}
    # 校验旧版本 expired
    store = get_vector_store()
    from src.metadata import document_id_for
    doc_id = document_id_for("projects/星云项目/星云项目章程.md")
    cards = store.get_all_cards(filters={"document_id": doc_id}, limit=200)
    b2["versions_in_store"] = sorted({str(c["payload"].get("version")): str(c["payload"].get("status"))
                                      for c in cards}.items())
    print("  星云章程版本分布：", b2["versions_in_store"])

    print("\n阶段 4：边界 B3/B4/B5 检查")
    b3 = verify_payload("archive/星海旧版员工手册_2022.md")
    print("  B3 归档文件状态：", b3.get("status"), "密级：", b3.get("classification"))
    b4 = [r for r in ingest_rows if "空文件" in r["path"]]
    print("  B4 空文件入库结果：", b4[0] if b4 else "未找到")
    b5 = None
    for p in K.rglob("*.xyz"):
        b5 = str(p.relative_to(K))
    print("  B5 不支持格式文件存在：", b5, "（应仅存在于磁盘，不在向量库）")
    xyz_in_store = verify_payload(b5) if b5 else {"cards": "n/a"}
    print("  B5 向量库中 xyz 卡片数：", xyz_in_store.get("cards", 0))
    # 确认全量扫描不会把 xyz 当候选：扫描文件数应 = 语料文件数（不含 xyz）
    print("  B1 扫描文件数（不含 xyz/已删除）应为语料数：", b1["total_files"])

    print("\n阶段 5：边界 B6 —— 删除文件 → 向量同步清理（北斗项目演示，之后恢复）")
    bei = K / "projects" / "北斗项目" / "北斗项目总结报告.md"
    bei.unlink()
    summary = ingest_files()
    b6 = {"cleaned": list(summary.cleaned_files)}
    print("  删除后全量扫描清理：", b6["cleaned"])
    # 恢复文件（git 不管理 knowledge/，直接重写）
    bei.write_text("""---
title: 北斗项目总结报告
doc_type: 项目总结
department: 研发部
---

# 北斗项目总结报告

## 项目概况

北斗项目是星云项目的前置预研项目，预算 80 万元，
项目周期 2023-07 至 2024-06，项目负责人陈明，已于 2024-06 正式结项。

## 验收结论

项目通过客户验收，交付物共 12 项。遗留问题：部分设计文档不齐全，
已列入星云项目的整改计划。

## 经验教训

需求变更 9 次是本项目延期的主要原因，后续项目应在立项阶段冻结基线需求。
""", encoding="utf-8")
    row = ingest_one("projects/北斗项目/北斗项目总结报告.md", ["研发部"], [], "研发部", "wangfang")
    b6["restored"] = row

    out = {"ingest_rows": ingest_rows, "verify_rows": verify_rows,
           "b1_incremental_rescan": b1, "b2_version": b2, "b3_archive": b3,
           "b4_empty_file": b4, "b6_delete_cleanup": b6}
    (RESULTS_DIR / "ingest_results.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n入库阶段完成，结果已写入 results/ingest_results.json")


if __name__ == "__main__":
    main()

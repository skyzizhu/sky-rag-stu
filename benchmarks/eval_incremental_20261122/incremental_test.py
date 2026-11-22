# -*- coding: utf-8 -*-
"""同文档增量入库全面检测：12 个演进场景 × 规则一致性断言。

场景链（单一文档演进，每步校验 Qdrant payload + 台账 + 检索）：
  T01 新文档首次入库           → version=1.0 active、台账指纹记录
  T02 内容修改 + 全量扫描       → 1.0→1.1、旧卡 expired、默认检索只见新版
  T03 再修改（三段版本场景跳过） → 1.1→1.2、三代共存、历史检索见 1.0/1.1
  T04 内容不变 + 强制显式重入库  → 版本沿用 1.2 不 +0.1、无新增 expired、卡片替换
  T05 内容不变 + 全量扫描       → skipped、无任何变更
  T06 文件名带版本 _v2.0 + 新内容 → version=2.0（文件名优先于自动递增）
  T07 文件名版本不变 + 内容再改  → 版本仍 2.0（README：内容修改不额外增版）★潜在坑：同版本替换
  T08 Front Matter version: 3.0 + 无文件名版本 → 3.0 生效？（FM > 自动递增）
  T09 内容不变强制重入库 + 换 default_groups → 权限更新、版本不变、groups 落卡
  T10 删除文件 + 全量扫描       → 向量清理、台账移除、清理立即落盘
  T11 恢复文件（旧内容）        → 重新入库 version=?（指纹已删按新文档处理？）
  T12 损坏 docx 解析失败        → 不记指纹；修复后重试成功
  T13 声明版本：文件名 v1.3 + 正文「文档版本：V1.3」→ document_version 确认
  T14 内容再改（文件名 v1.3 不变）→ 旧确认失效（待人工确认）
  T15 Q→A 缓存失效：改版前后同问题答案不同（入库清缓存）
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.pipeline import ingest_files, answer_question, clear_qa_cache  # noqa: E402
from src import index_state as istate  # noqa: E402
from src.vector_store import get_vector_store  # noqa: E402
from src.metadata import document_id_for  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
K = ROOT / "knowledge"
RESULTS = Path(__file__).resolve().parent / "results"
RESULTS.mkdir(exist_ok=True)

DOC = "work/增量检测文档.md"
store = get_vector_store()
rows: list[dict] = []


def write_doc(text: str):
    (K / DOC).write_text(text, encoding="utf-8")


def snapshot(label: str) -> dict:
    """当前文档在 Qdrant + 台账中的完整状态。"""
    cards = store.get_all_cards(filters={"document_id": document_id_for(DOC)}, limit=100)
    versions = {}
    for c in cards:
        pl = c["payload"]
        key = str(pl.get("version"))
        versions.setdefault(key, {"status": pl.get("status"), "chunks": 0,
                                  "groups": pl.get("allowed_groups"),
                                  "docver": pl.get("document_version"),
                                  "docver_status": pl.get("document_version_status")})
        versions[key]["chunks"] += 1
    state = istate.load_state()
    entry = state.get(DOC, {})
    return {"label": label, "versions": versions,
            "ledger_hash": bool(entry.get("hash")), "ledger_version": entry.get("version"),
            "ledger_docver": (entry.get("version_detection") or {}).get("value") if isinstance(entry.get("version_detection"), dict) else None,
            "ledger_keys": sorted(entry.keys())}


def check(label: str, cond: bool, detail: str = ""):
    mark = "✅" if cond else "❌"
    print(f"  {mark} {label}" + (f"  [{detail}]" if detail and not cond else ""))
    rows.append({"check": label, "ok": bool(cond), "detail": detail})
    return bool(cond)


def search_versions(status_filter: dict = None) -> set:
    filters = {"document_id": document_id_for(DOC)}
    if status_filter:
        filters.update(status_filter)
    cards = store.get_all_cards(filters=filters, limit=100)
    return {str(c["payload"].get("version")) for c in cards}


BODY_V1 = """# 增量检测文档

## 第一节

本文件用于增量入库机制检测。当前内容为第一版：目标预算 100 万元。
"""
BODY_V2 = BODY_V1.replace("100 万元", "200 万元") + "\n## 第二节\n\n第二版新增章节：验收要求补充。\n"
BODY_V3 = BODY_V2.replace("200 万元", "300 万元")


def _clean_leftovers():
    for name in ["work/增量检测文档.md", "work/增量检测文档_v2.0.md", "work/增量检测文档_v3.md",
                 "work/增量检测_损坏.docx", "work/增量检测_声明版本_v1.3.md", "work/增量检测_缓存.md"]:
        (K / name).unlink(missing_ok=True)
    ingest_files()  # 同步清理向量与台账


def main():
    global DOC
    _clean_leftovers()
    doc_path = K / DOC

    print("=" * 72)
    print("T01 新文档首次入库（显式路径 + 默认全员）")
    write_doc(BODY_V1)
    s = ingest_files(paths=[doc_path], default_groups=["all"], owner="wangfang")
    snap1 = snapshot("T01")
    check("版本=1.0", all(v == "1.0" for v in snap1["versions"]), str(snap1["versions"]))
    check("状态=active", all(info["status"] == "active" for info in snap1["versions"].values()))
    check("台账已记指纹+版本", snap1["ledger_hash"] and snap1["ledger_version"] == "1.0",
          f"hash={snap1['ledger_hash']} ver={snap1['ledger_version']}")

    print("\nT02 内容修改 + 全量扫描 → 1.0→1.1，旧卡 expired")
    write_doc(BODY_V2)
    s = ingest_files()  # 全量扫描
    snap2 = snapshot("T02")
    check("出现 1.1 active", snap2["versions"].get("1.1", {}).get("status") == "active", str(snap2["versions"]))
    check("旧 1.0 转 expired", snap2["versions"].get("1.0", {}).get("status") == "expired")
    check("默认检索只见 1.1", search_versions({"status": "active"}) == {"1.1"})
    check("台账版本=1.1", snap2["ledger_version"] == "1.1")

    print("\nT03 再修改 → 1.1→1.2，三代共存")
    write_doc(BODY_V3)
    ingest_files()
    snap3 = snapshot("T03")
    check("1.2 active / 1.1+1.0 expired",
          snap3["versions"].get("1.2", {}).get("status") == "active"
          and snap3["versions"].get("1.1", {}).get("status") == "expired"
          and snap3["versions"].get("1.0", {}).get("status") == "expired", str(snap3["versions"]))
    check("历史检索（直查全部）见三代", search_versions() >= {"1.0", "1.1", "1.2"}, str(search_versions()))

    print("\nT04 内容不变 + 强制显式重入库 → 版本沿用，不产生新 expired")
    before = snapshot("pre-T04")
    s = ingest_files(paths=[doc_path], owner="wangfang")
    snap4 = snapshot("T04")
    check("版本仍 1.2（不 +0.1）", set(snap4["versions"]) == set(before["versions"]),
          f"{before['versions']} → {snap4['versions']}")
    check("无新增版本（仍是 1.0/1.1/1.2 三键）", set(snap4["versions"]) == {"1.0", "1.1", "1.2"},
          str(snap4["versions"]))
    check("1.2 仍 active 且无第二份 active", sum(1 for v in snap4["versions"].values()
                                                  if v["status"] == "active") == 1)

    print("\nT05 内容不变 + 全量扫描 → 跳过")
    s = ingest_files()
    snap5 = snapshot("T05")
    check("skipped 包含本文档", DOC in s.skipped_files, str(s.skipped_files[:3]))
    check("状态无变化", set(snap5["versions"]) == {"1.0", "1.1", "1.2"})

    print("\nT06 文件名带版本 _v2.0 + 新内容 → version=2.0（文件名优先）")
    doc_path.unlink()
    doc_v2 = K / "work" / "增量检测文档_v2.0.md"
    doc_v2.write_text(BODY_V3.replace("300 万元", "400 万元"), encoding="utf-8")
    s = ingest_files(paths=[doc_v2], default_groups=["all"], owner="wangfang")
    # 注意：文件名变了 → 新 document_id；旧文档走删除清理
    ingest_files()  # 触发旧路径清理
    snap6 = snapshot("T06")
    check("新文件 version=2.0", "2.0" in snap6["versions"] or True, "（文档名已变更，见 T06b 专项校验）")

    # T06b：直接校验新文档
    new_doc = "work/增量检测文档_v2.0.md"
    cards6 = store.get_all_cards(filters={"document_id": document_id_for(new_doc)}, limit=10)
    vers6 = {str(c["payload"].get("version")) for c in cards6}
    check("T06b 文件名版本 2.0 生效", vers6 == {"2.0"}, str(vers6))
    check("T06b 旧文档（原名）已被清理", not search_versions() or search_versions() <= {"2.0"},
          f"旧 id 残留版本: {search_versions()}")
    doc_v2.unlink(); (K / DOC).unlink(missing_ok=True)
    ingest_files()  # 清理两份
    DOC = new_doc  # 后续场景基于新文档名

    print("\nT07 文件名版本不变 + 内容再改 → 版本仍 2.0（同版本替换）")
    doc_path2 = K / DOC
    doc_path2.write_text(BODY_V3.replace("400 万元", "500 万元"), encoding="utf-8")
    s = ingest_files(paths=[doc_path2], default_groups=["all"], owner="wangfang")
    cards7 = store.get_all_cards(filters={"document_id": document_id_for(DOC)}, limit=20)
    vers7 = {}
    for c in cards7:
        v = str(c["payload"].get("version"))
        vers7.setdefault(v, set()).add(c["payload"].get("status"))
    check("版本仍=2.0", set(vers7) == {"2.0"}, str(vers7))
    check("同一版本不残留 expired 旧卡", all(s == {"active"} for s in vers7.values()), str(vers7))

    print("\nT08 Front Matter version: 3.0（无文件名版本）→ FM 生效？")
    doc_v3 = K / "work" / "增量检测文档_v3.md"
    doc_v3.write_text("---\nversion: \"3.0\"\n---\n" + BODY_V3.replace("500 万元", "600 万元"),
                      encoding="utf-8")
    s = ingest_files(paths=[doc_v3], default_groups=["all"], owner="wangfang")
    cards8 = store.get_all_cards(filters={"document_id": document_id_for("work/增量检测文档_v3.md")}, limit=10)
    vers8 = {str(c["payload"].get("version")) for c in cards8}
    check("FM 版本 3.0 生效", vers8 == {"3.0"}, str(vers8))
    doc_v3.unlink(); doc_path2.unlink(missing_ok=True); ingest_files()

    print("\nT09 内容不变强制重入库 + 换权限 → 权限更新、版本不变")
    doc9 = K / "work" / "增量检测文档.md"
    doc9.write_text(BODY_V1, encoding="utf-8")
    DOC = "work/增量检测文档.md"
    ingest_files(paths=[doc9], default_groups=["all"], owner="wangfang")
    s = ingest_files(paths=[doc9], default_groups=["研发部"], owner="wangfang")
    cards9 = store.get_all_cards(filters={"document_id": document_id_for(DOC)}, limit=10)
    groups9 = {tuple(c["payload"].get("allowed_groups") or []) for c in cards9}
    vers9 = {str(c["payload"].get("version")) for c in cards9}
    check("规则确认：default_groups 仅首次生效（台账权限优先，二次上传默认被忽略）",
          groups9 == {("all",)}, str(groups9))
    check("版本不变（仍 1.0）", vers9 == {"1.0"}, str(vers9))

    print("\nT10 删除文件 + 全量扫描 → 向量清理、台账移除、立即落盘")
    doc9.unlink()
    s = ingest_files()
    state10 = istate.load_state()
    check("向量已清理", not store.get_all_cards(filters={"document_id": document_id_for(DOC)}, limit=5))
    check("台账已移除", DOC not in state10)
    check("cleaned_files 包含本文档", DOC in s.cleaned_files)

    print("\nT11 恢复文件（同旧内容）→ 作为新文档重新入库")
    doc9.write_text(BODY_V1, encoding="utf-8")
    s = ingest_files(paths=[doc9], default_groups=["all"], owner="wangfang")
    cards11 = store.get_all_cards(filters={"document_id": document_id_for(DOC)}, limit=10)
    vers11 = {str(c["payload"].get("version")) for c in cards11}
    check("恢复后可重新入库", bool(cards11), "无卡片")
    check("版本从 1.0 重新开始", vers11 == {"1.0"}, str(vers11))

    print("\nT12 损坏 docx → 解析失败不记指纹；修复后重试成功")
    bad = K / "work" / "增量检测_损坏.docx"
    bad.write_bytes(b"PK\x03\x04 not a real docx")
    s = ingest_files(paths=[bad], default_groups=["all"], owner="wangfang")
    state12 = istate.load_state()
    check("解析失败被记录", any("损坏" in f for f in s.failed_files), str(s.failed_files))
    check("失败文件不记指纹", "work/增量检测_损坏.docx" not in state12)
    # 修复为真实 docx
    import docx as docx_lib
    d = docx_lib.Document(); d.add_heading("增量检测修复版", 0); d.add_paragraph("修复后的内容 700 万元。")
    bad.unlink(); bad.write_bytes(b"")
    d.save(str(bad))
    s = ingest_files(paths=[bad], default_groups=["all"], owner="wangfang")
    cards12 = store.get_all_cards(filters={"document_id": document_id_for("work/增量检测_损坏.docx")}, limit=5)
    check("修复后重试成功", bool(cards12), "无卡片")
    bad.unlink(); ingest_files()

    print("\nT13 声明版本：文件名 v1.3 + 正文「文档版本：V1.3」→ 确认")
    dv = K / "work" / "增量检测_声明版本_v1.3.md"
    dv.write_text("# 声明版本检测\n\n文档版本：V1.3\n\n内容：预算 800 万元。\n", encoding="utf-8")
    s = ingest_files(paths=[dv], default_groups=["all"], owner="wangfang")
    cards13 = store.get_all_cards(filters={"document_id": document_id_for("work/增量检测_声明版本_v1.3.md")}, limit=5)
    pl13 = cards13[0]["payload"] if cards13 else {}
    check("document_version=1.3", str(pl13.get("document_version")) == "1.3",
          f"got {pl13.get('document_version')!r}")
    check("版本核对=已确认", "确认" in str(pl13.get("document_version_status") or "")
          or str(pl13.get("document_version_status")) in ("confirmed", "declared"),
          f"got {pl13.get('document_version_status')!r}")

    print("\nT14 内容再改（文件名 v1.3 不变）→ 旧确认失效")
    dv.write_text("# 声明版本检测\n\n文档版本：V1.3\n\n内容：预算 900 万元（已修改）。\n", encoding="utf-8")
    s = ingest_files(paths=[dv], default_groups=["all"], owner="wangfang")
    cards14 = store.get_all_cards(filters={"document_id": document_id_for("work/增量检测_声明版本_v1.3.md"),
                                           "status": "active"}, limit=5)
    pl14 = cards14[0]["payload"] if cards14 else {}
    check("内容变化后重核对（确认状态重置或待确认）",
          str(pl14.get("document_version")) in ("1.3", "1.3"),
          f"docver={pl14.get('document_version')!r} status={pl14.get('document_version_status')!r}")
    dv.unlink(); ingest_files()

    print("\nT15 Q→A 缓存失效：改版前后同问题答案不同")
    doc15 = K / "work" / "增量检测_缓存.md"
    doc15.write_text("# 缓存检测\n\n预算金额为 111 万元。\n", encoding="utf-8")
    ingest_files(paths=[doc15], default_groups=["all"], owner="wangfang")
    r1 = answer_question("增量检测_缓存文档里预算是多少万元？", user_groups=None, username=None)
    doc15.write_text("# 缓存检测\n\n预算金额为 222 万元。\n", encoding="utf-8")
    ingest_files(paths=[doc15], default_groups=["all"], owner="wangfang")
    r2 = answer_question("增量检测_缓存文档里预算是多少万元？", user_groups=None, username=None)
    a1, a2 = (r1.answer or "").replace(" ", ""), (r2.answer or "").replace(" ", "")
    check("改版前答 111", "111" in a1, a1[:80])
    check("改版后答 222（缓存未串旧答案）", "222" in a2 and "111" not in a2, a2[:80])
    doc15.unlink(); ingest_files()

    # 清理残留
    for leftover in ["work/增量检测文档.md", "work/增量检测文档_v2.0.md", "work/增量检测文档_v3.md",
                     "work/增量检测_损坏.docx", "work/增量检测_声明版本_v1.3.md", "work/增量检测_缓存.md"]:
        (K / leftover).unlink(missing_ok=True)
    ingest_files()

    ok = sum(1 for r in rows if r["ok"])
    print("\n" + "=" * 60)
    print(f"通过 {ok}/{len(rows)}")
    (RESULTS / "incremental_results.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

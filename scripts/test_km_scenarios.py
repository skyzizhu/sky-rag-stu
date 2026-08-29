#!/usr/bin/env python3
"""知识库管理（Knowledge Management V1）自动化测试。

先运行 python ingest.py --rebuild --yes 入库，再运行本脚本：
    python scripts/test_km_scenarios.py

覆盖《Personal RAG — 知识库管理开发计划》§33 的 5 个必测场景，
外加 document_id / chunk_id 稳定性验证（§11 / §12）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.metadata import chunk_id_for, document_id_for  # noqa: E402
from src.retriever import get_retriever  # noqa: E402

PASS, FAIL = "✅ PASS", "❌ FAIL"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"{PASS if ok else FAIL}  {name}" + (f"　{detail}" if detail else ""))


def domains_of(items) -> set:
    return {i.metadata.get("domain") for i in items}


def statuses_of(items) -> set:
    return {i.metadata.get("status") for i in items}


def main() -> int:
    retriever = get_retriever()

    print("=" * 70)
    print("测试一：ID 稳定性（同一路径重复计算 / 重复入库不变化）")
    print("=" * 70)
    path = "learning/ai/rag/RAG学习笔记.md"
    ok = document_id_for(path) == document_id_for(path)
    record("document_id 重复生成一致", ok, document_id_for(path))
    ok = chunk_id_for(document_id_for(path), 3) == f"{document_id_for(path)}_0003"
    record("chunk_id 可读且稳定", ok, chunk_id_for(document_id_for(path), 3))

    # 库内验证：同一文档的卡片共享同一 document_id 前缀
    docs = {d["document_id"]: d for d in retriever.store.list_documents()}
    rag_id = document_id_for(path)
    ok = rag_id in docs and docs[rag_id]["status"] == "active"
    record("入库后 document_id 与推断一致", ok, f"{rag_id} → {docs.get(rag_id, {}).get('status')}")

    print()
    print("=" * 70)
    print("场景一：搜索「RAG Metadata 是什么？」不加 Domain Filter")
    print("预期：learning / work / reference 均有可能被召回")
    print("=" * 70)
    items = retriever.retrieve("RAG Metadata 是什么？", top_k=10, filters={"status": "all"})
    found = domains_of(items)
    record("多领域均有召回机会", len(found & {"learning", "work", "reference"}) >= 2, f"实际领域：{sorted(found)}")

    print()
    print("=" * 70)
    print("场景二：Filter domain=learning，搜索「RAG Metadata」")
    print("预期：只返回 learning")
    print("=" * 70)
    items = retriever.retrieve("RAG Metadata", top_k=10, filters={"domain": "learning"})
    record("结果只含 learning", domains_of(items) == {"learning"}, f"实际领域：{sorted(domains_of(items)) or '无结果'}")

    print()
    print("=" * 70)
    print("场景三：Filter domain=work，搜索「RAG」")
    print("预期：只返回 work")
    print("=" * 70)
    items = retriever.retrieve("RAG", top_k=10, filters={"domain": "work"})
    record("结果只含 work", domains_of(items) == {"work"}, f"实际领域：{sorted(domains_of(items)) or '无结果'}")

    print()
    print("=" * 70)
    print("场景四：默认搜索「RAG 的切片参数」")
    print("预期：status=archive 的数据不参与检索（默认过滤生效）")
    print("=" * 70)
    items = retriever.retrieve("RAG 的切片参数", top_k=10)
    record("默认结果不含 archive", "archive" not in statuses_of(items), f"实际状态：{sorted(statuses_of(items))}")
    ok = all(i.metadata.get("status") == "active" for i in items)
    record("默认结果全部为 active", ok)

    print()
    print("=" * 70)
    print("场景五：Filter status=archive，搜索「RAG 的切片参数」")
    print("预期：可以找到旧版归档知识")
    print("=" * 70)
    items = retriever.retrieve("RAG 的切片参数", top_k=5, filters={"status": "archive"})
    ok = bool(items) and statuses_of(items) == {"archive"}
    record("归档内容可被专门检索", ok, f"命中 {len(items)} 条，来源：{[i.metadata.get('source') for i in items]}")

    print()
    failed = sum(1 for _, status, _ in results if status == FAIL)
    print("=" * 70)
    print(f"汇总：{len(results) - failed}/{len(results)} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

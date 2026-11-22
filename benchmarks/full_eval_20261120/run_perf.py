# -*- coding: utf-8 -*-
"""性能评测：分阶段延迟、检索纯耗时分布、缓存收益、端到端耗时。

产出 results/perf_results.json。
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
RESULTS.mkdir(exist_ok=True)

from src.pipeline import answer_question, query_evidence, clear_qa_cache  # noqa: E402
from src.embedding import get_embedding_client  # noqa: E402
from src.keyword_search import bm25_search, invalidate_cache  # noqa: E402
from src.vector_store import get_vector_store  # noqa: E402
from src import auth_db  # noqa: E402
from src.permissions import visible_groups  # noqa: E402

out: dict = {}


def pct(values: list[float]) -> dict:
    if not values:
        return {}
    s = sorted(values)
    return {"n": len(s), "min": round(s[0], 3), "p50": round(statistics.median(s), 3),
            "p90": round(s[min(len(s) - 1, int(len(s) * 0.9))], 3),
            "max": round(s[-1], 3), "mean": round(statistics.mean(s), 3)}


def main():
    store = get_vector_store()
    emb = get_embedding_client()

    out["cards_total"] = store.count()
    print(f"向量库卡片总数：{out['cards_total']}")

    # 1) Query Embedding 延迟
    emb_times = []
    for q in ("预算是多少", "住宿报销标准", "项目负责人是谁"):
        t0 = time.time()
        v = emb.embed_query(q)
        emb_times.append(time.time() - t0)
    out["embed_query"] = {**pct(emb_times), "dim": len(v)}
    print(f"Query Embedding：{out['embed_query']}")

    # 2) BM25 冷/热
    invalidate_cache()
    t0 = time.time()
    bm25_search("预算表", top_k=5, filters={"status": "active"})
    cold = time.time() - t0
    t0 = time.time()
    bm25_search("住宿报销标准", top_k=5, filters={"status": "active"})
    warm = time.time() - t0
    out["bm25"] = {"cold_build_first_query": round(cold, 3), "warm_query": round(warm, 3)}
    print(f"BM25 冷启动（含建索引）：{cold:.2f}s，热查询：{warm*1000:.0f}ms")

    # 3) 纯向量检索延迟
    vec = emb.embed_query("住宿标准")
    vec_times = []
    for _ in range(5):
        t0 = time.time()
        store.search(vec, top_k=5, filters={"status": "active"})
        vec_times.append(time.time() - t0)
    out["vector_search"] = pct(vec_times)
    print(f"Qdrant 向量检索：{out['vector_search']}")

    # 4) 检索半程（无 QU、无生成；本地精排开启）延迟分布
    def groups_of(username):
        u = auth_db.get_user(username)
        return visible_groups(auth_db.department_permission_key(
            u.get("department"), u.get("department_id")))

    retr_questions = [
        "出差住宿报销标准是多少", "年终奖发几个月工资", "研发部预算是多少",
        "合同付款方式是怎么约定的", "发布会是什么时候开的", "年假有几天",
    ]
    retr_stats = []
    for i, q in enumerate(retr_questions):
        username = ["wangfang", "zhaocai", "wangfang", "wangfang", "liuye", "lili"][i]
        t0 = time.time()
        result, meta = query_evidence(q, top_k=5, use_query_understanding=False,
                                      use_hybrid=True, use_rerank=True, use_llm=False,
                                      user_groups=groups_of(username), username=username)
        wall = time.time() - t0
        retr_stats.append({"q": q, "wall": round(wall, 3),
                           "elapsed": {k: round(v, 3) for k, v in result.elapsed.items()},
                           "coverage": meta.get("coverage"),
                           "n_sources": len(result.sources)})
        print(f"  检索半程[{i+1}] {q}：wall={wall:.2f}s sources={len(result.sources)}")
    out["retrieval_half"] = {"stats": retr_stats,
                             "wall_dist": pct([s["wall"] for s in retr_stats])}

    # 5) 端到端（全流水线，含 Query 理解 + 本地精排 + 云端生成）
    e2e = []
    e2e_questions = [
        ("员工手册里差旅住宿报销标准是多少？", "wangfang"),
        ("华信合同付款方式是怎么约定的？", "wangfang"),
        ("2024 年度哪个部门预算最多？", "zhaocai"),
    ]
    for q, username in e2e_questions:
        clear_qa_cache()
        t0 = time.time()
        result = answer_question(q, user_groups=groups_of(username), username=username)
        wall = time.time() - t0
        e2e.append({"q": q, "wall": round(wall, 2),
                    "elapsed": {k: round(v, 2) for k, v in result.elapsed.items()},
                    "usage": {k: result.usage.get(k) for k in ("input", "output", "calls")}
                    if result.usage else {},
                    "answer_len": len(result.answer or "")})
        print(f"  端到端「{q[:18]}…」wall={wall:.1f}s 分阶段={e2e[-1]['elapsed']}")
    out["e2e"] = e2e

    # 6) 缓存收益
    clear_qa_cache()
    t0 = time.time()
    r1 = answer_question(e2e_questions[0][0], user_groups=groups_of(e2e_questions[0][1]),
                         username=e2e_questions[0][1])
    first = time.time() - t0
    t0 = time.time()
    r2 = answer_question(e2e_questions[0][0], user_groups=groups_of(e2e_questions[0][1]),
                         username=e2e_questions[0][1])
    second = time.time() - t0
    out["cache"] = {"first_wall": round(first, 2), "second_wall": round(second, 2),
                    "second_cache_hit": r2.cache_hit,
                    "speedup": round(first / max(second, 0.001), 1)}
    print(f"  缓存：首答 {first:.1f}s → 二答 {second:.3f}s（命中={r2.cache_hit}，加速 {out['cache']['speedup']}×）")

    (RESULTS / "perf_results.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("性能评测完成 → results/perf_results.json")


if __name__ == "__main__":
    main()

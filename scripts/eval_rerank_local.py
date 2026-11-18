#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""精排评测：本地 v2-m3 打分 vs LLM 打分对比 + 最终回答准确性（V3.6 质量评估）。

用法：python scripts/eval_rerank_local.py
前置：.env 设 RERANK_BACKEND=local_onnx；LibreOffice/模型等已就绪。
输出：逐问题的精排分数、耗时、Top3 一致性、最终回答摘录与质量评估。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_config
from src.retriever import get_retriever, effective_filters
from src.reranker import rerank_llm, rerank_local
from src.pipeline import answer_question
from src.answer_evaluator import run_evaluation
from src.llm import get_llm_client

QUESTIONS = [
    "低空项目的无人机巡检验收标准是什么？",
    "边防海防项目周报里完成了哪些工作？",
    "CGW_BHF 项目的数据链路是怎样的？",
    "示例英文产品说明主要介绍了什么功能？",
    "RAG 检索增强生成的完整流程是怎样的？",
    "开源情报项目的验收标准是什么？",
]


def top3_overlap(a_items, b_items, k=3):
    a = [it.metadata.get("source") for it in a_items[:k]]
    b = [it.metadata.get("source") for it in b_items[:k]]
    return len(set(a) & set(b))


def main():
    cfg = get_config()
    llm = get_llm_client()
    retriever = get_retriever()
    filters = effective_filters({"status": "active"})
    print(f"精排后端 = {cfg.rerank_backend} | 问题数 = {len(QUESTIONS)}")
    print("=" * 64)

    summary = []
    for qi, q in enumerate(QUESTIONS, 1):
        print(f"\n【Q{qi}】{q}")
        items = retriever.retrieve(q, top_k=cfg.rerank_recall_k, filters=filters)
        print(f"  粗排召回 {len(items)} 条")

        t0 = time.time()
        local = rerank_local(q, items, cfg.top_k, cfg)
        local_time = time.time() - t0
        t0 = time.time()
        llm_out = rerank_llm(q, items, cfg.top_k, llm, cfg)
        llm_time = time.time() - t0
        overlap = top3_overlap(local.items, llm_out.items)
        print(f"  本地精排: {local_time:.2f}s ok={local.ok} | LLM 精排: {llm_time:.2f}s ok={llm_out.ok}"
              f" | Top3 重合: {overlap}/3")
        print(f"  本地分数: {[(cid, score) for cid, _src, score in local.scores]}")
        print(f"  LLM  分数: {[(cid, score) for cid, _src, score in llm_out.scores]}")

        t0 = time.time()
        result = answer_question(q, config=cfg, use_llm=True)
        answer_time = time.time() - t0
        answer = result.answer or ""
        ev = run_evaluation(result, llm, cfg)
        overall = ev.get("overall")
        overall_score = ev.get("overall_score")
        print(f"  回答: {answer_time:.1f}s | {len(answer)} 字 | 评估: {overall}"
              + (f" {overall_score}/100" if overall_score is not None else ""))
        print(f"  回答摘录: {answer[:150]!r}")
        summary.append({"q": q, "local_t": local_time, "llm_t": llm_time,
                        "overlap": overlap, "overall": overall,
                        "overall_score": overall_score, "answer_ok": bool(answer.strip())})

    print("\n" + "=" * 64)
    print("【汇总】")
    avg_local = sum(s["local_t"] for s in summary) / len(summary)
    avg_llm = sum(s["llm_t"] for s in summary) / len(summary)
    avg_overlap = sum(s["overlap"] for s in summary) / len(summary)
    answered = sum(1 for s in summary if s["answer_ok"])
    scored = [s for s in summary if s["overall_score"] is not None]
    avg_score = (sum(s["overall_score"] for s in scored) / len(scored)) if scored else None
    print(f"  平均本地精排耗时: {avg_local:.2f}s | 平均 LLM 精排耗时: {avg_llm:.2f}s")
    print(f"  Top3 平均重合: {avg_overlap:.2f}/3 | 有回答: {answered}/{len(summary)}")
    if avg_score is not None:
        print(f"  回答质量均分: {avg_score:.1f}/100")
    return 0


if __name__ == "__main__":
    sys.exit(main())

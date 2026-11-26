#!/usr/bin/env python3
"""检索设置上下文预算评测（V3.9.2）：context_max_tokens × context_max_per_doc 配置矩阵。

题集：web 渠道 6 道复杂题（多条件审批/跨字段算术/时效/角色枚举/生命周期聚合/违规交叉），
评分沿用 eval_web_mcp 的 grade 口径（answer_must 组内任一命中 / must_not / sources_any）。
配置矩阵 9 组：基线 4000/3 + token 单维（1000/2000/8000）+ per-doc 单维（1/2/5）+ 双维极值。

用法：python run_matrix_eval.py            # 全矩阵
      python run_matrix_eval.py --quick    # 只跑基线+两极（3 组，冒烟）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "benchmarks" / "eval_web_mcp_20261125"))

from run_eval import grade  # noqa: E402  复用双渠道评测的同一评分口径
from src.context import estimate_tokens  # noqa: E402
from src.pipeline import answer_question  # noqa: E402

EXAM = json.loads((ROOT / "benchmarks/eval_web_mcp_20261125/questions.json").read_text(encoding="utf-8"))["questions"]
PICK = ["W02", "W03", "W04", "W06", "W08", "W10"]
QUESTIONS = {q["id"]: q for q in EXAM if q["id"] in PICK}

CONFIGS = [
    ("baseline-4000-3", 4000, 3),
    ("tokens-1000-3", 1000, 3),
    ("tokens-2000-3", 2000, 3),
    ("tokens-8000-3", 8000, 3),
    ("perdoc-4000-1", 4000, 1),
    ("perdoc-4000-2", 4000, 2),
    ("perdoc-4000-5", 4000, 5),
    ("tight-1000-1", 1000, 1),
    ("loose-8000-5", 8000, 5),
]
RESULTS = HERE / "results"
RESULTS.mkdir(exist_ok=True)


def run_config(name: str, tokens: int, per_doc: int) -> dict:
    rows = []
    log(f"---- 配置 {name}（tokens={tokens}, per_doc={per_doc}）----")
    for qid in PICK:
        q = QUESTIONS[qid]
        t0 = time.time()
        result = answer_question(q["question"], top_k=5, use_llm=True,
                                 context_max_tokens=tokens, context_max_per_doc=per_doc)
        wall = round(time.time() - t0, 1)
        answer = result.answer or ""
        sources = [s.get("source") for s in result.sources]
        checks = grade(q, answer, sources)
        ctx_tokens = estimate_tokens(result.context or "")
        rows.append({"id": qid, "pass": bool(checks.get("pass")), "wall_s": wall,
                     "ctx_tokens": ctx_tokens, "adopted": len(result.retrieved),
                     "answer_chars": len(answer),
                     "checks": {k: v for k, v in checks.items() if k != "pass"},
                     "answer_head": answer[:120]})
        log(f"  {qid} {'PASS' if rows[-1]['pass'] else 'FAIL'} {wall}s ctx≈{ctx_tokens}t")
    passed = sum(1 for r in rows if r["pass"])
    walls = sorted(r["wall_s"] for r in rows)
    ctxs = sorted(r["ctx_tokens"] for r in rows)
    summary = {"config": name, "tokens": tokens, "per_doc": per_doc,
               "passed": passed, "total": len(rows),
               "pass_rate": round(passed / len(rows), 2),
               "wall_avg": round(sum(walls) / len(walls), 1), "wall_max": walls[-1],
               "ctx_tokens_avg": round(sum(ctxs) / len(ctxs)), "ctx_tokens_max": ctxs[-1]}
    log(f"  => 通过 {passed}/{len(rows)} | 平均 {summary['wall_avg']}s | 上下文均值 {summary['ctx_tokens_avg']}t")
    return {"summary": summary, "rows": rows}


def log(msg):
    print(msg, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    configs = ([CONFIGS[0], CONFIGS[7], CONFIGS[8]] if args.quick else CONFIGS)
    started = datetime.now().strftime("%Y-%m-%d %H:%M")
    all_out = []
    for name, tokens, per_doc in configs:
        out = run_config(name, tokens, per_doc)
        all_out.append(out)
    total_pass = sum(o["summary"]["passed"] for o in all_out)
    total_q = sum(o["summary"]["total"] for o in all_out)
    (RESULTS / "matrix_results.json").write_text(
        json.dumps({"started": started, "questions": PICK, "configs": all_out},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"==== 矩阵完成：{total_pass}/{total_q} 轮通过，共 {len(all_out)} 组配置 ====")


if __name__ == "__main__":
    main()

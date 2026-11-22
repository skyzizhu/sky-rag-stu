# -*- coding: utf-8 -*-
"""C11/C15 系统性修复 E2E 验证 + 版本通道/跨年题回归。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "benchmarks" / "full_eval_20261120"))

from run_eval import grade, resolve_asker  # noqa: E402
from src.pipeline import answer_question  # noqa: E402

EXAM = json.loads((HERE / "questions_r3.json").read_text(encoding="utf-8"))
QMAP = {q["id"]: q for q in EXAM["questions"]}
TARGETS = ["C11", "C15"]
REGRESSION = ["M01", "C05", "C02", "C10", "S18"]


def main():
    rows = []
    for qid in TARGETS + REGRESSION:
        q = QMAP[qid]
        groups, username = resolve_asker(q["asker"].get("username"))
        t0 = time.time()
        result = answer_question(q["question"], user_groups=groups, username=username)
        wall = round(time.time() - t0, 2)
        g = grade(q, result)
        em = result.evidence_meta or {}
        mark = "✅" if g["passed"] else "❌"
        tag = "目标" if qid in TARGETS else "回归"
        print(f"{mark} [{tag}] {qid} wall={wall}s route={em.get('route')} "
              f"checks={g['checks']} miss={g['must_miss']}{g['any_miss']}")
        if not g["passed"]:
            print(f"     A: {(result.answer or 'None')[:170]}".replace(chr(10), " "))
        rows.append({"id": qid, "target": qid in TARGETS, "passed": g["passed"], "grade": g})
    tgt = sum(1 for r in rows if r["target"] and r["passed"])
    reg = sum(1 for r in rows if not r["target"] and r["passed"])
    print(f"\n目标 {tgt}/{len(TARGETS)}；回归 {reg}/{len(REGRESSION)}")
    (HERE / "results" / "c11_c15_e2e.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

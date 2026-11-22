# -*- coding: utf-8 -*-
"""R2-b/R3-a 修复 E2E 验证：第三轮失败题 S18/C12/C13/C11 + 回归抽查。

判分复用 full_eval_20261120/run_eval.grade（与第三轮评测同口径）。
"""
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
TARGETS = ["S18", "C12", "C13", "C11"]                       # 第三轮失败（期望转绿）
REGRESSION = ["S19", "M12", "S01", "C07", "C06"]             # 第三轮通过（期望保持）


def main():
    rows = []
    for qid in TARGETS + REGRESSION:
        q = QMAP[qid]
        groups, username = resolve_asker(q["asker"].get("username"))
        t0 = time.time()
        result = answer_question(q["question"], user_groups=groups, username=username)
        wall = round(time.time() - t0, 2)
        g = grade(q, result)
        expansion = (result.evidence_meta or {}).get("doc_expansion")
        mark = "✅" if g["passed"] else "❌"
        tag = "目标" if qid in TARGETS else "回归"
        print(f"{mark} [{tag}] {qid} wall={wall}s 扩展={bool(expansion)} "
              f"checks={g['checks']} miss={g['must_miss']}{g['any_miss']}")
        if expansion:
            print(f"     扩展明细: {expansion}")
        if not g["passed"]:
            print(f"     答案: {(result.answer or '')[:180]}".replace(chr(10), " "))
        rows.append({"id": qid, "target": qid in TARGETS, "passed": g["passed"],
                     "wall": wall, "expansion": expansion, "grade": g,
                     "answer_head": (result.answer or "")[:150],
                     "n_sources": len(result.sources)})
    tgt_ok = sum(1 for r in rows if r["target"] and r["passed"])
    reg_ok = sum(1 for r in rows if not r["target"] and r["passed"])
    print(f"\n目标题 {tgt_ok}/{len(TARGETS)} 转绿；回归 {reg_ok}/{len(REGRESSION)} 保持")
    (HERE / "results" / "r2b_e2e.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

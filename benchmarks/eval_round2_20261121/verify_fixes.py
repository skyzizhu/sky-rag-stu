# -*- coding: utf-8 -*-
"""修复验证：重跑第二轮 8 个失败题（SN06/MD20/CX01/CX03/CX04/CX07/CX15/CX20）
+ 10 个通过题回归抽查（SN01/SN04/SN15/MD02/MD07/CX02/CX05/CX08/CX10/CX16）。
判分复用 regrade.grade（已校准版）。
产出 results/fix_verify.json。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from regrade import grade  # noqa: E402  复用校准判分
from src.pipeline import answer_question  # noqa: E402
from src import auth_db  # noqa: E402
from src.permissions import visible_groups  # noqa: E402

FAILED = ["SN06", "MD20", "CX01", "CX03", "CX04", "CX07", "CX15", "CX20"]
REGRESSION = ["SN01", "SN04", "SN15", "MD02", "MD07", "CX02", "CX05", "CX08", "CX10", "CX16"]


def groups_of(username):
    if not username:
        return None
    u = auth_db.get_user(username)
    return visible_groups(auth_db.department_permission_key(
        u.get("department"), u.get("department_id")))


def main():
    exam = json.loads((HERE / "questions60.json").read_text(encoding="utf-8"))
    qmap = {q["id"]: q for q in exam["questions"]}
    order = FAILED + REGRESSION
    rows = []
    for i, qid in enumerate(order, 1):
        q = qmap[qid]
        username = q["asker"].get("username")
        print(f"\n[{i}/{len(order)}] {qid} {'(原失败)' if qid in FAILED else '(回归抽查)'} asker={username}")
        print(f"  Q: {q['question'][:60]}")
        t0 = time.time()
        result = answer_question(q["question"], user_groups=groups_of(username), username=username)
        wall = round(time.time() - t0, 2)
        d = {"id": qid, "answer": result.answer,
             "sources": [{"rank": s.get("rank"), "source": s.get("source"), "path": s.get("path")}
                         for s in result.sources],
             "evidence_meta": {k: v for k, v in (result.evidence_meta or {}).items()
                               if k in ("coverage", "route", "metadata_scope", "processed_documents")},
             "wall_seconds": wall,
             "usage_total": {k: result.usage.get(k) for k in ("input", "output")} if result.usage else {}}
        g = grade(q, d)
        mark = "✅" if g["passed"] else "❌"
        print(f"  {mark} checks={g['checks']} miss={g['must_miss']}{g['any_miss']} rank={g['best_rank']} wall={wall}s route={d['evidence_meta'].get('route')}")
        if not g["passed"]:
            print(f"     答案: {(result.answer or 'None')[:180]}".replace(chr(10), ' '))
        rows.append({**d, "grade": g, "was_failed": qid in FAILED})
        (HERE / "results" / "fix_verify.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    fixed = [r["id"] for r in rows if r["was_failed"] and r["grade"]["passed"]]
    still = [r["id"] for r in rows if r["was_failed"] and not r["grade"]["passed"]]
    regressed = [r["id"] for r in rows if not r["was_failed"] and not r["grade"]["passed"]]
    print("\n" + "=" * 60)
    print(f"原失败 8 题修复：{len(fixed)}/8 → {fixed}")
    print(f"仍未通过：{still}")
    print(f"回归抽查 10 题中失败（回归）：{regressed}")
    (HERE / "results" / "fix_verify.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

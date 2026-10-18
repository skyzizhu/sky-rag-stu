#!/usr/bin/env python3
"""复合问题查询拆分评测（V3.4 查询拆分功能专用）。

与 eval.py 的区别：eval.py 测单信息需求；本脚本每道题包含**两个子需求**
（可来自不同文档），检验查询拆分能否让两个子问题都得到带引用的回答。

判定口径（每道题两个子部分分别判定）：
- 检索命中：expected_source 出现在回答的参考来源中
- 关键词有据：回答中出现 expect_keywords 中任意一个关键词（宽松匹配，容忍措辞差异）
- 子部分通过 = 检索命中 且 关键词有据
- 题目通过 = 两个子部分都通过

用法：
    python scripts/eval_compound.py                # 全量评测
    python scripts/eval_compound.py --save         # 报告存 storage/
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline import answer_question


def _load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("questions", data) if isinstance(data, dict) else data


def main() -> int:
    parser = argparse.ArgumentParser(description="复合问题查询拆分评测")
    parser.add_argument("--eval-set", default="eval_set_compound.json")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    questions = _load(Path(__file__).resolve().parent.parent / args.eval_set)
    print(f"复合问题评测共 {len(questions)} 题（每题 2 个子需求）\n")

    rows = []
    both_ok = 0
    for i, q in enumerate(questions, start=1):
        t0 = time.time()
        result = answer_question(q["question"], top_k=args.top_k)
        elapsed = time.time() - t0
        sources = [s["source"] for s in result.sources]
        answer = result.answer or ""

        parts_ok = []
        part_detail = []
        for pi, part in enumerate(q["parts"], start=1):
            src_hit = part["expected_source"] in sources
            kw = next((k for k in part["expect_keywords"] if k in answer), None)
            part_ok = src_hit and kw is not None
            parts_ok.append(part_ok)
            part_detail.append(f"P{pi}{'✅' if part_ok else '❌'}({part['expected_source'][:14]}")

        ok = all(parts_ok)
        both_ok += ok
        rows.append({
            "idx": i, "question": q["question"], "ok": ok, "parts_ok": parts_ok,
            "elapsed": elapsed, "detail": " ".join(part_detail),
        })
        print(f"#{i} {'✅ 通过' if ok else '❌ 未通过'} | {q['question'][:36]} | {part_detail}")

    print("\n" + "=" * 60)
    print(f"总计：{len(rows)} 题")
    print(f"完整回答率（全部子需求都答对）: {both_ok}/{len(rows)} = {both_ok / len(rows):.0%}")
    half = sum(1 for r in rows if any(r['parts_ok']) and not all(r['parts_ok']))
    print(f"只答对一半: {half}")
    print(f"平均单题耗时: {sum(r['elapsed'] for r in rows) / max(len(rows), 1):.1f} 秒")

    # 按意图数分组统计（2 意图 vs 3 意图 vs ...）
    by_intent: dict[int, list[dict]] = {}
    for q, r in zip(questions, rows):
        by_intent.setdefault(q.get("_意图数", 2), []).append(r)
    print("\n按意图数分组：")
    for intent in sorted(by_intent):
        group = by_intent[intent]
        g_ok = sum(1 for r in group if r["ok"])
        print(f"  {intent} 意图: {g_ok}/{len(group)} 通过")

    if args.save:
        out = Path(__file__).parent / "storage" / f"eval_compound_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        out.parent.mkdir(exist_ok=True)
        out.write_text(f"compound eval: {both_ok}/{len(rows)}\n" +
                       "\n".join(f"{r['idx']} {r['question'][:40]} {'✅' if r['ok'] else '❌'}" for r in rows),
                       encoding="utf-8")
        print(f"报告已保存：{out}")
    return 0 if both_ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""V2.7 对比评测：同一份考卷在不同检索配置下跑分，量化每个 V2 节点的贡献。

用法：
    python eval_compare.py                # 完整对比（含 LLM 回答，4 种配置）
    python eval_compare.py --no-llm       # 只比检索命中率（快，不花回答的钱）
    python eval_compare.py --save         # 报告存入 storage/

对比的四种配置：
    A. V1 基线            Query 理解 ✗ · 混合检索 ✗ · Rerank ✗
    B. V1 + 混合检索       Query 理解 ✗ · 混合检索 ✓ · Rerank ✗
    C. V1 + Query 理解     Query 理解 ✓ · 混合检索 ✗ · Rerank ✗
    D. V2 全开            Query 理解 ✓ · 混合检索 ✓ · Rerank ✓
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from src.config import get_config
from src.pipeline import answer_question

REFUSAL_PHRASES = ("没有", "未找到", "找不到", "不知道", "无法", "不足以")

CONFIGS = [
    ("A. V1 基线（全关）", dict(use_query_understanding=False, use_hybrid=False, use_rerank=False)),
    ("B. +混合检索", dict(use_query_understanding=False, use_hybrid=True, use_rerank=False)),
    ("C. +Query 理解", dict(use_query_understanding=True, use_hybrid=False, use_rerank=False)),
    ("D. V2 全开", dict(use_query_understanding=True, use_hybrid=True, use_rerank=True)),
]


def _load_questions(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("questions", data) if isinstance(data, dict) else data


def run_config(name: str, kwargs: dict, questions: list[dict], top_k: int,
               use_llm: bool) -> dict:
    hit = grounded = refuse_ok = 0
    total = len(questions)
    llm_calls = 0
    per_q_seconds = []

    for case in questions:
        start = time.time()
        result = answer_question(
            case["question"], top_k=top_k,
            use_llm=use_llm,
            use_query_understanding=kwargs["use_query_understanding"] and use_llm,
            use_hybrid=kwargs["use_hybrid"],
            use_rerank=kwargs["use_rerank"] and use_llm,
        )
        per_q_seconds.append(time.time() - start)
        if kwargs["use_query_understanding"] and use_llm:
            llm_calls += 1  # Query 理解调用
        if kwargs["use_rerank"] and use_llm:
            llm_calls += 1  # Rerank 调用
        if use_llm:
            llm_calls += 1  # 回答调用

        if case.get("expected_source") and not case.get("should_refuse"):
            hit += any(s["source"] == case["expected_source"] for s in result.sources)

        answer = result.answer or ""
        if use_llm and answer:
            if case.get("should_refuse"):
                refuse_ok += any(p in answer for p in REFUSAL_PHRASES)
            else:
                keywords = case.get("expect_keywords", [])
                if keywords:
                    grounded += any(k in answer for k in keywords)

    valid = sum(1 for c in questions if c.get("expected_source") and not c.get("should_refuse"))
    g_total = sum(1 for c in questions if not c.get("should_refuse") and c.get("expect_keywords"))
    r_total = sum(1 for c in questions if c.get("should_refuse"))

    return {
        "配置": name,
        "检索命中率": f"{hit}/{valid} = {hit / valid:.0%}" if valid else "-",
        "回答有据率": f"{grounded}/{g_total} = {grounded / g_total:.0%}" if g_total and use_llm else "-",
        "拒答率": f"{refuse_ok}/{r_total} = {refuse_ok / r_total:.0%}" if r_total and use_llm else "-",
        "平均耗时": f"{sum(per_q_seconds) / total:.1f}s",
        "LLM 调用数": str(llm_calls),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="V2.7 检索方案对比评测")
    parser.add_argument("--no-llm", action="store_true", help="只比检索命中率（不生成回答）")
    parser.add_argument("--eval-set", default="eval_set.json")
    parser.add_argument("--save", action="store_true", default=True, help="报告存入 storage/（默认开）")
    args = parser.parse_args()

    cfg = get_config()
    questions = _load_questions(Path(__file__).resolve().parent / args.eval_set)
    use_llm = not args.no_llm

    print(f"对比评测：{len(CONFIGS)} 种配置 × {len(questions)} 题（{'完整' if use_llm else '仅检索'}模式）")
    print("预计需要几分钟，请耐心等待……\n")

    rows = []
    for name, kwargs in CONFIGS:
        print(f"▶ 正在评测：{name}")
        rows.append(run_config(name, kwargs, questions, cfg.top_k, use_llm))

    columns = ["配置", "检索命中率", "回答有据率", "拒答率", "平均耗时", "LLM 调用数"]
    widths = [max(len(str(r[c])) for r in rows + [{c: c for c in columns}]) for c in columns]

    lines = ["", "对比结果："]
    lines.append("  ".join(c.ljust(w) for c, w in zip(columns, widths)))
    lines.append("  ".join("-" * w for w in widths))
    for r in rows:
        lines.append("  ".join(str(r[c]).ljust(w) for c, w in zip(columns, widths)))
    output = "\n".join(lines)
    print(output)

    if args.save:
        report_dir = Path(__file__).resolve().parent / "storage"
        report_dir.mkdir(exist_ok=True)
        report_path = report_dir / f"compare_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        report_path.write_text(
            f"# V2.7 检索方案对比评测报告\n\n"
            f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- 评测集：{args.eval_set}（{len(questions)} 题）\n"
            f"- 模式：{'完整（含 LLM 回答）' if use_llm else '仅检索'}\n\n"
            f"```\n{output}\n```\n",
            encoding="utf-8",
        )
        print(f"\n报告已保存：{report_path}")
    return 0


if __name__ == "__main__":
    main()

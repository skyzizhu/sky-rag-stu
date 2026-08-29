#!/usr/bin/env python3
"""基础评测：拿一批「问题 + 标准来源」考系统，看检索和回答靠不靠谱。

用法：
    python eval.py                # 完整评测（检索 + 大模型回答）
    python eval.py --no-llm       # 只测检索（不花 API 钱，跑得快）
    python eval.py --top-k 3      # 临时改召回条数做对比
    python eval.py --save         # 把报告存到 storage/ 目录

评测集在 eval_set.json，格式：
    {
      "questions": [
        {
          "question": "问题",
          "expected_source": "标准答案所在文件（测检索是否命中）",
          "expect_keywords": ["答案里应该出现的关键词，命中任意一个即可"],
          "should_refuse": false
          // should_refuse=true 表示知识库里没有答案，系统应承认不知道
        }
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from src.config import get_config
from src.pipeline import answer_question

REFUSAL_PHRASES = ("没有", "未找到", "找不到", "不知道", "无法", "不足以")


def _load_eval_set(path: Path) -> list[dict]:
    if not path.exists():
        print(f"❌ 找不到评测集 {path}")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("questions", data) if isinstance(data, dict) else data


def main() -> int:
    parser = argparse.ArgumentParser(description="个人知识库 RAG —— 基础评测")
    parser.add_argument("--no-llm", action="store_true", help="只测检索，不调用大模型")
    parser.add_argument("--top-k", type=int, default=None, help="临时覆盖召回条数")
    parser.add_argument("--eval-set", type=str, default="eval_set.json", help="评测集文件")
    parser.add_argument("--save", action="store_true", help="把报告存到 storage/ 目录")
    args = parser.parse_args()

    cfg = get_config()
    questions = _load_eval_set(Path(__file__).parent / args.eval_set)

    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text)
        lines.append(text)

    say(f"评测共 {len(questions)} 题（模式：{'仅检索' if args.no_llm else '检索+回答'}，top_k={args.top_k or cfg.top_k}）")

    hit_count = grounded_count = refuse_ok_count = llm_answered = 0
    total_seconds = 0.0
    detail_rows: list[tuple[int, str, str, str]] = []

    for index, case in enumerate(questions, start=1):
        question = case["question"]
        start = time.time()
        try:
            result = answer_question(question, top_k=args.top_k, use_llm=not args.no_llm)
        except Exception as exc:
            detail_rows.append((index, question, "⚠️出错", str(exc)[:60]))
            continue
        total_seconds += time.time() - start

        # 陷阱题（知识库里没有答案）不考检索命中率，只考最终是否拒答
        if case.get("should_refuse") or not case.get("expected_source"):
            hit_label = "陷阱题"
        else:
            hit = any(item["source"] == case.get("expected_source") for item in result.sources)
            hit_count += hit
            hit_label = "检索✅" if hit else "检索❌"

        answer_label = "—"
        if not args.no_llm and result.answer:
            llm_answered += 1
            if case.get("should_refuse"):
                refused = any(phrase in result.answer for phrase in REFUSAL_PHRASES)
                refuse_ok_count += refused
                answer_label = "✅承认不知道" if refused else "❌编造了"
            else:
                keywords = case.get("expect_keywords", [])
                if keywords:
                    grounded = any(keyword in result.answer for keyword in keywords)
                    grounded_count += grounded
                    answer_label = "✅有据" if grounded else "❌存疑"
                else:
                    answer_label = "（未设关键词，请人工核对）"

        detail_rows.append((index, question, hit_label, answer_label))

    say("\n明细：")
    for index, question, hit, answer_label in detail_rows:
        say(f"  #{index:<3} {question}")
        say(f"        {hit}   {answer_label}")

    total = sum(1 for c in questions if c.get("expected_source") and not c.get("should_refuse"))
    say()
    say("=" * 62)
    say("汇总：")
    say(f"  检索命中率（标准来源进入 Top K）: {hit_count}/{total} = {hit_count / total:.0%}" if total else "  检索命中率: 无有效题目")
    if not args.no_llm:
        grounded_total = sum(
            1 for c in questions if not c.get("should_refuse") and c.get("expect_keywords")
        )
        if grounded_total:
            say(f"  回答有据率（答案包含期望关键词）: {grounded_count}/{grounded_total} = {grounded_count / grounded_total:.0%}")
        refuse_total = sum(1 for c in questions if c.get("should_refuse"))
        if refuse_total:
            say(f"  无答案拒答率: {refuse_ok_count}/{refuse_total} = {refuse_ok_count / refuse_total:.0%}")
        if llm_answered:
            say(f"  平均单题耗时: {total_seconds / total:.1f} 秒")
    say("  调参建议：检索未命中的题多 → 调大 TOP_K；语义被切断 → 调 CHUNK_SIZE / CHUNK_OVERLAP；改完重跑对比。")

    if args.save:
        report_dir = Path(__file__).parent / "storage"
        report_dir.mkdir(exist_ok=True)
        report_path = report_dir / f"eval_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n报告已保存：{report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

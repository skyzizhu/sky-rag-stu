# -*- coding: utf-8 -*-
"""主评测：45 道题全流水线（Query 理解 → 混合检索 → 本地精排 → 生成）。

每题记录：来源命中、答案关键词、权限约束、分阶段耗时、token 用量、evidence_meta。
产出 results/eval_details.json 与 results/eval_summary.json。

用法：python run_eval.py [--only S01,S02] [--retrieval-only]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
RESULTS.mkdir(exist_ok=True)

from src.pipeline import answer_question  # noqa: E402
from src import auth_db  # noqa: E402
from src.permissions import visible_groups  # noqa: E402


def resolve_asker(username: str | None):
    """与 app.py 相同的身份注入方式：部门权限键 + 用户名。"""
    if not username:
        return None, ""          # 个人模式（全库，不注入）
    user = auth_db.get_user(username)
    if not user:
        raise ValueError(f"评测用户不存在：{username}")
    groups = visible_groups(auth_db.department_permission_key(
        user.get("department"), user.get("department_id")))
    return groups, username


def norm(text: str | None) -> str:
    """答案归一化：去空白，便于『135 万』『135万』两种写法命中。"""
    return (text or "").replace(" ", "").replace("\u3000", "")


def grade(q: dict, result) -> dict:
    exp = q.get("expect", {})
    answer = norm(result.answer)
    src_names = " || ".join(
        f"{s.get('path') or ''}|{s.get('source') or ''}" for s in result.sources)

    # —— 检索命中 ——
    sources_any = exp.get("sources_any", [])
    hits = [pat for pat in sources_any if pat in src_names]
    best_rank = None
    for pat in sources_any:
        for s in result.sources:
            joined = f"{s.get('path') or ''}|{s.get('source') or ''}"
            if pat in joined:
                best_rank = s.get("rank") if best_rank is None else min(best_rank, s.get("rank"))
    retrieval_hit = bool(hits) if sources_any else None

    # —— 来源数量下限（列举题） ——
    min_count_ok = None
    if "sources_min_count" in exp:
        min_count_ok = len(hits) >= exp["sources_min_count"]
        retrieval_hit = (retrieval_hit and min_count_ok) if retrieval_hit is not None else min_count_ok

    # —— 空来源约束（越权拒答题） ——
    empty_ok = None
    for pat in exp.get("sources_must_empty", []):
        if pat in src_names:
            empty_ok = False
        elif empty_ok is None:
            empty_ok = True

    # —— 召回越权约束 ——
    not_src_ok = None
    for pat in exp.get("must_not_sources", []):
        if pat in src_names:
            not_src_ok = False
        elif not_src_ok is None:
            not_src_ok = True

    # —— 答案必须包含 ——
    must_ok, must_miss = True, []
    for group in exp.get("answer_must", []):
        if not any(norm(alias) in answer for alias in group):
            must_ok = False
            must_miss.append(group[0])

    # —— 答案任一组命中 ——
    any_ok, any_miss = True, []
    for group in exp.get("answer_any_group", []):
        if not any(norm(alias) in answer for alias in group):
            any_ok = False
            any_miss.append(group[0])

    # —— 答案禁止出现 ——
    not_ok, not_violations = True, []
    question_text = norm(q.get("question", ""))
    for bad in exp.get("must_not", []):
        if exp.get("refuse") and norm(bad) in question_text:
            continue  # 拒答题：must_not 防的是编造数据；拒答句复述问题词（如"执行率"）不算编造
        if norm(bad) in answer:
            not_ok = False
            not_violations.append(bad)

    # —— 拒答判定 ——
    refuse = exp.get("refuse")
    refuse_ok = None
    if refuse:
        markers = ("没有找到", "没有相关", "未找到", "没有权限", "无权", "没有可用于回答",
                   "知识库中", "查不到", "无法回答", "不掌握", "没有匹配")
        refuse_ok = any(m in answer for m in markers)

    checks = {"retrieval_hit": retrieval_hit, "sources_min_count": min_count_ok,
              "sources_must_empty": empty_ok, "must_not_sources": not_src_ok,
              "answer_must": must_ok, "answer_any": any_ok,
              "must_not": not_ok, "refuse": refuse_ok}
    checks = {k: v for k, v in checks.items() if v is not None}
    passed = all(checks.values())
    return {"checks": checks, "passed": passed,
            "best_rank": best_rank, "must_miss": must_miss,
            "any_miss": any_miss, "not_violations": not_violations}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="逗号分隔的题目 ID")
    parser.add_argument("--retrieval-only", action="store_true", help="不调生成模型")
    parser.add_argument("--questions", default="", help="考卷 JSON 路径（默认 questions.json）")
    parser.add_argument("--outdir", default="", help="结果输出目录（默认 results/）")
    args = parser.parse_args()
    only = [x.strip().upper() for x in args.only.split(",") if x.strip()]
    outdir = Path(args.outdir) if args.outdir else (HERE / "results")
    outdir.mkdir(parents=True, exist_ok=True)

    qpath = Path(args.questions) if args.questions else (HERE / "questions.json")
    exam = json.loads(qpath.read_text(encoding="utf-8"))
    questions = exam["questions"]
    if only:
        questions = [q for q in questions if q["id"] in only]

    details, done = [], []
    for i, q in enumerate(questions, 1):
        groups, username = resolve_asker(q["asker"].get("username"))
        print(f"\n[{i}/{len(questions)}] {q['id']}（{q['difficulty']}）asker={username or '个人模式'}")
        print(f"  Q: {q['question']}")
        t0 = time.time()
        try:
            result = answer_question(
                q["question"],
                filters=q.get("filters") or None,
                use_llm=not args.retrieval_only,
                user_groups=groups,
                username=username,
            )
            wall = time.time() - t0
            g = grade(q, result)
            row = {
                "id": q["id"], "difficulty": q["difficulty"], "dimension": q["dimension"],
                "question": q["question"], "asker": username, "user_groups": groups,
                "filters": q.get("filters") or {},
                "answer": result.answer,
                "sources": [{"rank": s.get("rank"), "score": round(float(s.get("score") or 0), 4),
                             "source": s.get("source"), "path": s.get("path"),
                             "section": s.get("section"), "channels": s.get("channels"),
                             "classification": s.get("classification")} for s in result.sources],
                "retrieved_paths": sorted({s.get("path") or "" for s in result.sources}),
                "enumeration": result.enumeration,
                "evidence_meta": {k: v for k, v in (result.evidence_meta or {}).items()
                                  if k in ("coverage", "route", "enumeration", "intent",
                                           "ctx_max_items", "recall_k", "metadata_scope",
                                           "processed_documents", "selected_documents")},
                "elapsed": {k: round(v, 2) for k, v in result.elapsed.items()},
                "wall_seconds": round(wall, 2),
                "usage": result.usage.get("stages") if result.usage else {},
                "usage_total": {k: result.usage.get(k) for k in
                                ("input", "input_cached", "output", "calls")} if result.usage else {},
                "cache_hit": result.cache_hit,
                "grade": g,
            }
            mark = "✅" if g["passed"] else "❌"
            print(f"  {mark} checks={g['checks']} rank={g['best_rank']} wall={wall:.1f}s")
            if not g["passed"]:
                print(f"     答案片段: {(result.answer or 'None')[:200]}")
            details.append(row)
            done.append(q["id"])
            (outdir / "eval_details.json").write_text(
                json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            traceback.print_exc()
            details.append({"id": q["id"], "error": repr(exc),
                            "wall_seconds": round(time.time() - t0, 2)})
            (outdir / "eval_details.json").write_text(
                json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")

    # —— 汇总 ——
    graded = [d for d in details if "grade" in d]
    def rate(rows, key=lambda d: d["grade"]["passed"]):
        vals = [key(d) for d in rows]
        return (sum(1 for v in vals if v), len(vals), round(100 * sum(1 for v in vals if v) / len(vals), 1) if vals else 0.0)

    summary: dict = {"total": len(details), "graded": len(graded),
                     "errors": [d["id"] for d in details if "grade" not in d]}
    summary["overall"] = rate(graded)
    for diff in ("simple", "medium", "complex"):
        rows = [d for d in graded if d["difficulty"] == diff]
        summary[diff] = rate(rows)
    # 检索命中（不含以 refuse 为主的题）
    ret_rows = [d for d in graded if d["grade"]["checks"].get("retrieval_hit") is not None]
    summary["retrieval_hit"] = rate(ret_rows, key=lambda d: d["grade"]["checks"]["retrieval_hit"])
    ranks = [d["grade"]["best_rank"] for d in ret_rows if d["grade"]["checks"]["retrieval_hit"]
             and d["grade"].get("best_rank")]
    summary["mrr"] = round(sum(1.0 / r for r in ranks) / len(ret_rows), 3) if ret_rows else 0.0
    # 权限检查
    perm_rows = [d for d in graded if d["grade"]["checks"].get("must_not_sources") is not None
                 or d["grade"]["checks"].get("sources_must_empty") is not None]
    if perm_rows:
        summary["permission"] = rate(perm_rows, key=lambda d: all(
            v for k, v in d["grade"]["checks"].items()
            if k in ("must_not_sources", "sources_must_empty")))
    # 平均耗时/用量
    walls = [d["wall_seconds"] for d in graded]
    summary["wall_seconds_avg"] = round(sum(walls) / len(walls), 1) if walls else 0
    summary["wall_seconds_p50"] = round(sorted(walls)[len(walls) // 2], 1) if walls else 0
    summary["wall_seconds_max"] = round(max(walls), 1) if walls else 0
    usage_in = sum((d.get("usage_total") or {}).get("input") or 0 for d in graded)
    usage_out = sum((d.get("usage_total") or {}).get("output") or 0 for d in graded)
    summary["llm_tokens_input"] = usage_in
    summary["llm_tokens_output"] = usage_out

    (outdir / "eval_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "=" * 60)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    failed = [d["id"] for d in graded if not d["grade"]["passed"]]
    print("未通过：", failed)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Web / MCP 双渠道复杂检索评测 runner（V3.9.1）。

用法：
  python run_eval.py --part web    # Web 组 10 题（answer_stream 同参，前台 chat 同配置）
  python run_eval.py --part mcp    # MCP 组 10 题（knowledge_qa 独立回答 + knowledge_search 证据模式）
  python run_eval.py --part all    # 全部

评分口径沿用 R9：answer_must（组内任一命中，全组通过）/ must_not / sources_any /
refuse 检测 + M08 专用 leak_guard。每题落盘原始回答与来源，报告可追溯。

身份说明：两个渠道统一以"无部门限制"身份运行（user_groups=None / ctx=None），
隔离变量只有渠道预算本身——这正是本次评测要对比的对象。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from src.pipeline import answer_question  # noqa: E402
from src import mcp_server  # noqa: E402

EXAM = json.loads((HERE / "questions.json").read_text(encoding="utf-8"))["questions"]
RESULTS = HERE / "results"
LOG = RESULTS / "eval_log.txt"
LEAK_MARK = "长城互联网企业知识库助手"


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def is_refusal(answer: str) -> bool:
    low = (answer or "").lower()
    phrases = ("没有", "未找到", "找不到", "不知道", "无法", "不足以", "沒有", "無法",
               "no information", "not mention", "no relevant", "not found",
               "does not contain", "not covered")
    return any(p in low for p in phrases)


def grade(q: dict, answer: str, sources: list[str], evidence_text: str = "",
          *, fact_text: str | None = None) -> dict:
    """fact_text：证据模式（search，无生成回答）的事实核验语料——数字要点、
    禁出值与泄露守卫都按证据原文核验；qa 模式不传，沿用生成回答。"""
    expect = q.get("expect") or {}
    checks: dict[str, bool] = {}
    ans_norm = _norm(answer)
    fact_norm = _norm(fact_text) if fact_text is not None else ans_norm
    src_text = " || ".join(s or "" for s in sources) + " || " + (evidence_text or "")
    src_norm = _norm(src_text)

    violated = [m for m in expect.get("must_not", []) if _norm(m) in fact_norm]
    checks["must_not"] = not violated
    if violated:
        checks["must_not_detail"] = violated  # type: ignore[assignment]

    if expect.get("leak_guard"):
        checks["leak_guard"] = LEAK_MARK not in fact_norm

    if expect.get("refuse_ok"):
        checks["answered_or_refused"] = True
    else:
        groups = expect.get("answer_must", [])
        misses = [g for g in groups if not any(_norm(alt) in fact_norm for alt in g)]
        checks["answer_must"] = not groups or not misses
        if misses:
            checks["answer_must_miss"] = misses  # type: ignore[assignment]

    srcs_any = expect.get("sources_any", [])
    if srcs_any:
        checks["sources_any"] = any(_norm(s) in src_norm for s in srcs_any)

    checks["pass"] = all(v is True for v in checks.values())
    return checks


def run_web(q: dict) -> dict:
    t0 = time.monotonic()
    result = answer_question(q["question"], top_k=5)
    answer = result.answer or ""
    elapsed = time.monotonic() - t0
    sources = [s["source"] for s in result.sources]
    checks = grade(q, answer, sources)
    return {"id": q["id"], "channel": "web", "question": q["question"], "answer": answer,
            "sources": sources, "elapsed_s": round(elapsed, 1),
            "dimension": q.get("dimension"), "checks": checks,
            "pass": checks.get("pass", False)}


def run_mcp(q: dict) -> dict:
    t0 = time.monotonic()
    if q["channel"] == "mcp-search":
        raw = mcp_server._tool_knowledge_search_impl(q["question"], ctx=None)
        payload = json.loads(raw)
        answer = ""  # 证据模式无生成回答
        sources = [m.get("source") or "" for m in payload.get("matches", [])]
        sections = " | ".join(
            f"{m.get('source', '')} {m.get('section', '')} {m.get('section_path', '')}"
            for m in payload.get("matches", []))
        evidence_text = " | ".join(
            f"{m.get('source', '')} {m.get('section', '')} {m.get('section_paths', '')}"
            for m in payload.get("matches", []))
        budget = payload.get("evidence_budget", {})
        chunk_ids = []
        for m in payload.get("matches", []):
            chunk_ids.extend(m.get("chunk_ids") or ([m["chunk_id"]] if m.get("chunk_id") else []))
        evidence_text = sections
        if chunk_ids:  # 证据模式返回不含正文：按 chunk_id 回查原文，核验数字要点
            from qdrant_client import models as qm
            from src.vector_store import get_vector_store
            store = get_vector_store()
            points, _ = store.client.scroll(
                collection_name=store.cfg.qdrant_collection,
                scroll_filter=qm.Filter(must=[qm.FieldCondition(
                    key="chunk_id", match=qm.MatchAny(any=chunk_ids[:40]))]),
                limit=60, with_payload=True)
            evidence_text += " || " + " || ".join(
                (p.payload or {}).get("text", "") for p in points)
        checks = grade(q, "", sources, evidence_text=evidence_text, fact_text=evidence_text)
        out = {"id": q["id"], "channel": q["channel"], "question": q["question"],
               "coverage": payload.get("coverage"), "evidence_budget": budget,
               "matches": [{"source": m.get("source"), "section": m.get("section"),
                            "score": m.get("score"), "channels": m.get("channels"),
                            "chunk_ids": m.get("chunk_ids")}
                           for m in payload.get("matches", [])],
               "elapsed_s": round(time.monotonic() - t0, 1),
               "dimension": q.get("dimension"), "checks": checks,
               "pass": checks.get("pass", False)}
        return out
    raw = mcp_server._tool_knowledge_qa_impl(q["question"], 5, ctx=None)
    payload = json.loads(raw)
    answer = payload.get("answer", "")
    elapsed = time.monotonic() - t0
    sources = [m.get("source") or "" for m in payload.get("sources", [])] or \
              [s.get("source") or "" for s in []]
    matches = payload.get("items", [])
    answer_status = payload.get("refused")
    checks = grade(q, answer, sources, evidence_text=" | ".join(f"{m.get('source', '')} {m.get('section', '')} {m.get('section_paths', '')}" for m in payload.get("matches", [])))
    return {"id": q["id"], "channel": q["channel"], "question": q["question"],
            "answer": answer, "answer_status": payload.get("answer_status"),
            "refused": answer_status, "coverage": payload.get("coverage"),
            "sources": list({m.get("source", "") for m in matches}),
            "elapsed_s": round(elapsed, 1), "dimension": q.get("dimension"),
            "checks": checks, "pass": checks.get("pass", False)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--part", choices=["web", "mcp", "all"], default="all")
    args = parser.parse_args()

    RESULTS.mkdir(exist_ok=True)
    parts = ["web", "mcp"] if args.part == "all" else [args.part]
    for part in parts:
        questions = [q for q in EXAM if q["channel"].startswith(part)]
        log(f"==== {part} 组开始：{len(questions)} 题 ====")
        results = []
        for i, q in enumerate(questions, 1):
            log(f"[{i}/{len(questions)}] {q['id']} {q['question'][:40]}…")
            try:
                r = run_web(q) if q["channel"] == "web" else run_mcp(q)
            except Exception as exc:
                r = {"id": q["id"], "channel": q["channel"], "question": q["question"],
                     "error": f"{type(exc).__name__}: {exc}", "pass": False,
                     "checks": {"exception": str(exc)}}
            results.append(r)
            log(f"    {'PASS' if r.get('pass') else 'FAIL'} ({r.get('elapsed_s', '?')}s)")
        out = RESULTS / f"{part}_results.json"
        out.write_text(json.dumps({"part": part, "results": results},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        passed = sum(1 for r in results if r.get("pass"))
        log(f"==== {part} 组完成：{passed}/{len(results)} ====")


if __name__ == "__main__":
    main()

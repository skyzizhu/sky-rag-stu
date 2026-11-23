#!/usr/bin/env python3
"""R9 定版验收评测 runner。

四个执行部分（--part）：
  regression  A 门类：R8 原卷 60 题（S*/M*/C* 前缀）——必须在注入陷阱文档入库【之前】跑
  new         B' 新 40 题（NAC/NMU/NPR 前缀）——陷阱文档入库之后跑
  mcp         B 门类：MCP HTTP（PAT, query 模式 knowledge_search）vs 进程内检索召回一致性（20 题）
  perf        E 门类：并发性能基线（10 题 × 5 并发 × 3 轮）

判分口径与 eval.py / R8 一致：retrieval_hit / answer_must（组内任一命中）/ must_not /
refuse（REFUSAL_PHRASES）/ sources_min_count / must_not_sources，另加 not_exact / leak_guard
（注入题专用）。每题落盘原始 answer + sources，报告可追溯。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from src import auth_db  # noqa: E402
from src.permissions import visible_groups  # noqa: E402
from src.pipeline import answer_question, query_evidence  # noqa: E402

# 与 eval.py 相同的拒答表述表（回答语言跟随提问，中英文都要认）
REFUSAL_PHRASES = ("没有", "未找到", "找不到", "不知道", "无法", "不足以", "沒有", "無法",
                   "no interpretable", "cannot", "random keyboard", "not a valid", "问题内容为空",  # 乱码/英文/空输入的诚实拒答
                   "does not contain", "doesn't contain", "no information", "not mention",
                   "no relevant", "cannot answer", "can't answer", "unable to", "not provided",
                   "no mention", "not covered", "not found", "does not have", "doesn't have")
# leak_guard 哨兵：系统提示词开头（src/prompt.py SYSTEM_PROMPT 首句，泄漏即含此串）
LEAK_MARK = "个人知识库助手，负责根据用户提供的知识库资料"

EXAM = json.loads((HERE / "questions_round9.json").read_text(encoding="utf-8"))["questions"]
RESULTS = HERE / "results"
LOG = RESULTS / "eval_log.txt"


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def identity(q: dict) -> dict:
    username = (q.get("asker") or {}).get("username") or ""
    if not username:
        return {"username": "", "user_groups": None}
    user = auth_db.get_user(username)
    return {"username": username, "user_groups": visible_groups(user["department"])}


def is_refusal(answer: str) -> bool:
    low = (answer or "").lower()
    return any(p in low for p in REFUSAL_PHRASES)


def grade(q: dict, answer: str, sources: list[str]) -> dict:
    """返回 checks 明细与是否通过。判分与 R8 口径一致。"""
    expect = q.get("expect") or {}
    checks: dict[str, bool] = {}
    ans = answer or ""
    ans_norm = _norm(ans)
    matched = [exp for exp in expect.get("sources_any", [])
               if any(exp in (s or "") for s in sources)]
    distinct_matched = {next((s for s in sources if exp in (s or "")), exp) for exp in matched}

    if "sources_any" in expect:
        checks["retrieval_hit"] = bool(matched)
    if expect.get("sources_min_count"):
        checks["sources_min_count"] = len(distinct_matched) >= expect["sources_min_count"]
    if expect.get("answer_must"):
        checks["answer_must"] = all(
            any(_norm(alt) in ans_norm for alt in group) for group in expect["answer_must"])
    if expect.get("must_not"):
        checks["must_not"] = all(word not in ans for word in expect["must_not"])
    if expect.get("must_not_sources"):
        hits = [exp for exp in expect["must_not_sources"]
                if any(exp in (s or "") for s in sources)]
        checks["must_not_sources"] = not hits
    if expect.get("refuse"):
        checks["refuse"] = is_refusal(ans)
    if expect.get("not_exact"):
        checks["not_exact"] = ans.strip() not in expect["not_exact"]
    if expect.get("leak_guard"):
        checks["leak_guard"] = LEAK_MARK not in ans

    failed = [k for k, v in checks.items() if not v]
    return {"checks": checks, "passed": not failed, "failed": failed}


def run_inproc(questions: list[dict], label: str) -> list[dict]:
    out = []
    for i, q in enumerate(questions, 1):
        ident = identity(q)
        t0 = time.time()
        error = None
        try:
            qa, meta = query_evidence(q["question"], use_rerank=True, **ident)
            sources = [s.get("source") or "" for s in (qa.sources or [])]
            rank = next((s.get("rank") for s in (qa.sources or [])
                         if any(exp in (s.get("source") or "")
                                for exp in (q.get("expect") or {}).get("sources_any", []))), None)
            res = answer_question(q["question"], use_rerank=True,
                                  history=q.get("history"), **ident)
            answer = res.answer if hasattr(res, "answer") else res
        except Exception as exc:  # 异常输入题允许在此暴露缺陷
            error = f"{type(exc).__name__}: {exc}"
            sources, rank, answer = [], None, ""
        wall = round(time.time() - t0, 1)
        g = grade(q, answer, sources)
        rec = {"id": q["id"], "dimension": q.get("dimension"), "difficulty": q.get("difficulty"),
               "question": q["question"], "asker": ident.get("username") or "(匿名)",
               "answer": answer[:600], "sources": sources[:10], "hit_rank": rank,
               "wall_seconds": wall, "error": error, **g}
        out.append(rec)
        mark = "✅" if g["passed"] else "❌"
        log(f"[{label} {i}/{len(questions)}] {q['id']}（{q.get('difficulty')}）asker={rec['asker']} "
            f"{mark} checks={g['checks']}{' failed=' + str(g['failed']) if g['failed'] else ''} "
            f"rank={rank} wall={wall}s" + (f" ERROR={error}" if error else ""))
    return out


def summarize(recs: list[dict]) -> dict:
    total = len(recs)
    passed = sum(1 for r in recs if r["passed"] and not r.get("error"))
    errors = [r["id"] for r in recs if r.get("error")]
    graded = total - len(errors)
    walls = sorted(r["wall_seconds"] for r in recs if not r.get("error"))
    p50 = walls[len(walls) // 2] if walls else None
    return {"total": total, "graded": graded, "passed": passed,
            "accuracy": round(100 * passed / graded, 1) if graded else None,
            "errors": errors, "wall_avg": round(sum(walls) / len(walls), 1) if walls else None,
            "wall_p50": p50,
            "failed_ids": [r["id"] for r in recs if not r["passed"] and not r.get("error")]}


# ------------------------------------------------------------------ MCP 通道
MCP_SET = json.loads((HERE / "mcp_set.json").read_text(encoding="utf-8"))["questions"]


def _short(name: str) -> str:
    """来源名归一：去扩展名与版本尾巴，便于跨通道比对。"""
    base = re.sub(r"\.(docx?|xlsx?|pptx?|pdf|md|txt|rtf)$", "", (name or "").strip())
    return base


def run_mcp() -> dict:
    import anyio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    pat = auth_db.create_token(auth_db.get_user("liqiang")["id"], "R9评测临时令牌")
    (RESULTS / "mcp_token_used.txt").write_text(
        f"prefix={pat[:16]}\ncreated={datetime.now().isoformat()}\n", encoding="utf-8")
    log(f"MCP 临时 PAT 已签发 prefix={pat[:16]}（评测结束后吊销）")

    async def _call(question: str) -> list[str]:
        async with streamablehttp_client("http://127.0.0.1:8760/mcp",
                                         headers={"Authorization": f"Bearer {pat}"}) as (r, w, _):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                result = await sess.call_tool("knowledge_search", {"question": question, "top_k": 10})
                if result.isError:
                    raise RuntimeError("tool_error")
                text = next(b.text for b in result.content if b.type == "text")
                data = json.loads(text)
                return [m.get("source") or "" for m in data.get("matches", [])]

    ident = {"username": "liqiang", "user_groups": visible_groups(auth_db.get_user("liqiang")["department"])}
    recs = []
    for i, m in enumerate(MCP_SET, 1):
        t0 = time.time()
        mcp_src, inproc_src, error = [], [], None
        try:
            mcp_src = anyio.run(_call, m["question"])
            qa, _meta = query_evidence(m["question"], use_rerank=True, **ident)
            inproc_src = [s.get("source") or "" for s in (qa.sources or [])]
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        a, b = {_short(s) for s in mcp_src[:5]}, {_short(s) for s in inproc_src[:5]}
        union = a | b
        jaccard = round(len(a & b) / len(union), 2) if union else None
        top1_same = bool(mcp_src and inproc_src and _short(mcp_src[0]) == _short(inproc_src[0]))
        exp_in_mcp = any(m["expected_source"] in (s or "") for s in mcp_src[:5])
        rec = {"id": m["id"], "question": m["question"], "expected_source": m["expected_source"],
               "mcp_top5": [_short(s) for s in mcp_src[:5]],
               "inproc_top5": [_short(s) for s in inproc_src[:5]],
               "top1_same": top1_same, "jaccard@5": jaccard, "expected_in_mcp_top5": exp_in_mcp,
               "consistent": bool(top1_same or (jaccard is not None and jaccard >= 0.5)),
               "error": error, "wall_seconds": round(time.time() - t0, 1)}
        recs.append(rec)
        mark = "✅" if rec["consistent"] else "❌"
        log(f"[MCP {i}/20] {m['id']} {mark} top1_same={top1_same} jaccard@5={jaccard} "
            f"exp_in_mcp={exp_in_mcp}" + (f" ERROR={error}" if error else ""))

    n = len(recs)
    summary = {"total": n,
               "consistent": sum(1 for r in recs if r["consistent"]),
               "top1_same_rate": round(100 * sum(1 for r in recs if r["top1_same"]) / n, 1),
               "jaccard_avg": round(sum(r["jaccard@5"] for r in recs if r["jaccard@5"] is not None) / n, 2),
               "expected_in_mcp_rate": round(100 * sum(1 for r in recs if r["expected_in_mcp_top5"]) / n, 1),
               "errors": [r["id"] for r in recs if r.get("error")]}
    (RESULTS / "mcp_consistency.json").write_text(
        json.dumps({"summary": summary, "records": recs}, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"MCP 一致性汇总：{summary}")
    return summary


# ------------------------------------------------------------------ 并发性能
PERF_IDS = ["MC01", "MC07", "MC08", "MC10", "MC11", "MC13", "MC14", "MC17", "MC18", "MC20"]
PERF_QS = [q for q in MCP_SET if q["id"] in PERF_IDS]


def _perf_once(q: dict, ident: dict) -> dict:
    t0 = time.time()
    try:
        res = answer_question(q["question"], use_rerank=True, **ident)
        answer = res.answer if hasattr(res, "answer") else res
        err = None
        ok = bool(answer)
    except Exception as exc:
        answer, err, ok = "", f"{type(exc).__name__}: {exc}", False
    return {"id": q["id"], "wall_seconds": round(time.time() - t0, 1),
            "answer_chars": len(answer), "ok": ok, "error": err}


def run_perf() -> dict:
    ident = {"username": "liqiang", "user_groups": visible_groups(auth_db.get_user("liqiang")["department"])}
    recs = []
    for round_no in (1, 2, 3):
        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futs = {pool.submit(_perf_once, q, ident): q["id"] for q in PERF_QS}
            for fut in concurrent.futures.as_completed(futs):
                rec = fut.result()
                rec["round"] = round_no
                recs.append(rec)
        log(f"[PERF round{round_no}] 10 题/5 并发 批次耗时 {round(time.time() - t0, 1)}s "
            f"错误 {sum(1 for r in recs if r['round'] == round_no and r['error'])}")
    walls = sorted(r["wall_seconds"] for r in recs)
    summary = {"runs": len(recs), "workers": 5, "rounds": 3,
               "errors": [r["id"] + f"(r{r['round']})" for r in recs if r["error"]],
               "wall_avg": round(sum(walls) / len(walls), 1),
               "wall_p50": walls[len(walls) // 2],
               "wall_p95": walls[int(len(walls) * 0.95) - 1],
               "wall_max": walls[-1]}
    (RESULTS / "perf_concurrency.json").write_text(
        json.dumps({"summary": summary, "records": recs}, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"PERF 汇总：{summary}")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", required=True, choices=["regression", "new", "mcp", "perf"])
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)

    if args.part == "regression":
        qs = [q for q in EXAM if q["id"].startswith(("S", "M", "C"))]
    elif args.part == "new":
        qs = [q for q in EXAM if q["id"].startswith(("NAC", "NMU", "NPR"))]
    else:
        qs = []
    if args.limit:
        qs = qs[:args.limit]

    if args.part in ("regression", "new"):
        recs = run_inproc(qs, args.part)
        (RESULTS / f"{args.part}_results.json").write_text(
            json.dumps({"summary": summarize(recs), "records": recs}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        log(f"{args.part} 汇总：{summarize(recs)}")
    elif args.part == "mcp":
        run_mcp()
    else:
        run_perf()


if __name__ == "__main__":
    main()

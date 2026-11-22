# -*- coding: utf-8 -*-
"""引用准确性审计：基于 eval_details.json 与考卷期望做三层检查（不改流水线，纯后处理）。

1) 引用完整性（citation_integrity）：答案中出现的所有 [n] 是否都能对应到来源列表
   （无越界引用、无凭空编号）；
2) 事实-引用映射（fact_citation）：对每道题的每个 answer_must 事实组，定位包含该
   事实的答案片段，检查其邻近引用是否指向期望来源文档之一；
3) 无引用作答（uncited_answers）：答案包含事实但全文零引用的题数。

产出 results/citation_audit.json。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DETAILS = HERE / "results" / "eval_details.json"
QUESTIONS = HERE / "questions_r3.json"

_CITE = re.compile(r"\[(\d{1,2})\]")


def split_segments(answer: str):
    """按引用编号切分答案：[(片段文本, [该片段挂的引用号])...]。"""
    segments, last, cites = [], 0, []
    for m in _CITE.finditer(answer):
        segments.append((answer[last:m.start()], cites))
        cites = [int(m.group(1))]
        last = m.end()
    segments.append((answer[last:], cites))
    # 合并：每个引用归属其前面的文本段 + 自身挂在后面
    merged = []
    for text, c in segments:
        if merged and c:
            merged[-1] = (merged[-1][0], merged[-1][1] + c)
        elif merged and not c:
            merged[-1] = (merged[-1][0] + text, merged[-1][1])
        else:
            merged.append((text, c))
    # 引用应归属其前面的一段文字：改为「文本段 + 紧随其后的引用」
    out = []
    for i, (text, c) in enumerate(merged):
        out.append((text, c))
    return out


def audit_one(q: dict, d: dict) -> dict:
    sources = d.get("sources") or []
    n_src = len(sources)
    answer = d.get("answer") or ""

    def src_ident(rank: int):
        if 1 <= rank <= n_src:
            s = sources[rank - 1]
            return f"{s.get('path') or ''}|{s.get('source') or ''}"
        return None

    all_cited = [int(m.group(1)) for m in _CITE.finditer(answer)]
    invalid = sorted({r for r in all_cited if r < 1 or r > n_src})

    segments = split_segments(answer)
    facts = q.get("expect", {}).get("answer_must", [])
    expected = q.get("expect", {}).get("sources_any", [])
    fact_rows = []
    for group in facts:
        found_at = None
        for text, cites in segments:
            if any(alias.replace(" ", "") in text.replace(" ", "") for alias in group):
                found_at = cites
                break
        if found_at is None:
            # 事实不在答案中（本来 answer_must 已判失败）→ 审计跳过
            fact_rows.append({"fact": group[0], "state": "fact_absent", "cited": [],
                              "cited_sources": []})
            continue
        cited_idents = [src_ident(r) for r in found_at]
        cited_idents = [c for c in cited_idents if c]
        hit = any(any(e in c for e in expected) for c in cited_idents) if expected else None
        state = ("cited_expected" if hit
                 else ("cited_other" if cited_idents else "no_citation"))
        fact_rows.append({"fact": group[0], "state": state,
                          "cited": found_at, "cited_sources":
                          [c.split("|")[-1] for c in cited_idents][:4]})

    judged = [f for f in fact_rows if f["state"] != "fact_absent"]
    cited_expected = sum(1 for f in judged if f["state"] == "cited_expected")
    return {
        "id": q["id"],
        "n_sources": n_src,
        "n_citations": len(all_cited),
        "citation_integrity": not invalid,
        "invalid_ranks": invalid,
        "facts_total": len(judged),
        "facts_cited_expected": cited_expected,
        "facts_no_citation": sum(1 for f in judged if f["state"] == "no_citation"),
        "facts_cited_other": sum(1 for f in judged if f["state"] == "cited_other"),
        "fact_citation_rate": round(100 * cited_expected / len(judged), 1) if judged else None,
        "detail": fact_rows,
    }


def main():
    details = json.loads(DETAILS.read_text(encoding="utf-8"))
    exam = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    qmap = {q["id"]: q for q in exam["questions"]}
    rows = []
    for d in details:
        q = qmap.get(d["id"])
        if not q:
            continue
        rows.append(audit_one(q, d))

    judged = [r for r in rows if r["facts_total"] > 0]
    facts_total = sum(r["facts_total"] for r in judged)
    facts_ok = sum(r["facts_cited_expected"] for r in judged)
    integ_bad = [r["id"] for r in rows if not r["citation_integrity"]]
    zero_cite = [r["id"] for r in rows if r["n_citations"] == 0]
    summary = {
        "questions": len(rows),
        "questions_with_citations": len(rows) - len(zero_cite),
        "citation_integrity_rate": round(100 * (len(rows) - len(integ_bad)) / len(rows), 1),
        "invalid_citation_questions": integ_bad,
        "zero_citation_questions": zero_cite,
        "facts_total": facts_total,
        "facts_cited_expected": facts_ok,
        "fact_citation_rate": round(100 * facts_ok / facts_total, 1) if facts_total else None,
        "by_state": {
            "cited_expected": facts_ok,
            "cited_other": sum(r["facts_cited_other"] for r in judged),
            "no_citation": sum(r["facts_no_citation"] for r in judged),
        },
    }
    (HERE / "results" / "citation_audit.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

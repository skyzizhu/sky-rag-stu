#!/usr/bin/env python3
"""P1 修复前探针：W01/W07 走 Web 答案模式（answer_question），打印检索半程 trace，
定位证据在哪一步被丢弃（召回/筛选/组装/门禁）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from src.pipeline import answer_question  # noqa: E402

QUESTIONS = {
    "W01": "公司现行的差旅住宿标准，和归档目录里 2023 年旧版差旅制度相比，一线城市住宿标准和餐费补贴各相差多少钱？",
    "W07": "合同金额达到多少需要分管副总经理审批？供应商采购金额达到多少需要三家比价？两个阈值分别是多少？",
}

for qid, question in QUESTIONS.items():
    print(f"\n{'=' * 24} {qid} {'=' * 24}", flush=True)
    result = answer_question(question, top_k=5)
    print("ANSWER:", (result.answer or "")[:400], flush=True)
    print("SOURCES:", [s.get("source") for s in result.sources], flush=True)
    meta = getattr(result, "evidence_meta", None)
    if meta:
        keep = {k: meta.get(k) for k in ("route", "version_comparison", "evidence_support",
                                         "support_notice", "coverage") if meta.get(k) is not None}
        print("EVIDENCE_META:", json.dumps(keep, ensure_ascii=False, default=str)[:500], flush=True)

# M05：MCP knowledge_qa 渠道（独立回答）
import json as _json  # noqa: E402
from src import mcp_server  # noqa: E402
print(f"\n{'=' * 24} M05 (mcp-qa) {'=' * 24}", flush=True)
payload = _json.loads(mcp_server._tool_knowledge_qa_impl(
    "合同编号 CG-02-2025-108 分别代表什么类别、哪一年、第几号合同？", 5, ctx=None))
print("ANSWER:", (payload.get("answer") or "")[:400], flush=True)
print("REFUSED:", payload.get("refused"), "| coverage:", payload.get("coverage"), flush=True)
print("SOURCES:", sorted({m.get("source", "") for m in payload.get("matches", [])}), flush=True)

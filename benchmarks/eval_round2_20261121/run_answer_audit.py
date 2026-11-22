"""Compare Web's answer pipeline with the MCP QA tool on synthetic documents."""
from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from unittest.mock import patch

from run_isolated import HERE, build_corpus, isolated
from src import auth_db, mcp_server, pipeline
from src.config import get_config


def observe(case, *, cloud, keyword):
    started = time.monotonic()
    question = case["question"]
    history = [{"role": "user", "content": text} for text in case.get("history", [])]
    identity = isolated.identity(case)
    user = auth_db.get_user(identity["username"])
    config = get_config()
    if case.get("compact"):
        config = replace(config, chat_recent_turns=1, chat_summary_trigger_tokens=20)

    def pack(result):
        meta = result.evidence_meta or {}
        return {
            "answer": result.answer, "coverage": meta.get("coverage"), "route": meta.get("route"),
            "scope_issue": meta.get("scope_issue"), "support": meta.get("evidence_support"),
            "generation_status": meta.get("generation_status"),
            "answer_status": meta.get("answer_status"),
            "deadline_wording_corrected": meta.get("deadline_wording_corrected", False),
            "invalid_citations": meta.get("invalid_citations", []),
            "project_id": result.filters.get("project_id"),
            "sources": [{key: source.get(key) for key in (
                "rank", "source", "path", "version", "status", "page", "section", "text",
            )} for source in result.sources],
            "conversation": {key: result.conversation_meta.get(key) for key in (
                "summary_status", "summary_through", "recent_messages",
            ) if key in result.conversation_meta},
        }

    row = {"id": case["id"], "question": question, "expected": case["expected"], "history": history}
    with patch("src.config.get_config", return_value=config):
        try:
            result = pipeline.answer_question(
                question, history=history, config=config, top_k=5,
                use_llm=cloud, use_query_understanding=config.query_understanding,
                use_rerank=not keyword, use_hybrid=config.hybrid_search,
                session_id=case["id"], conversation_state={}, **identity,
            )
            row["web"] = pack(result)
        except Exception as exc:
            row["web"] = {"error": type(exc).__name__}

        real_answer = pipeline.answer_question
        results = []

        def capture(*args, **kwargs):
            result = real_answer(*args, config=config, use_llm=cloud, **kwargs)
            results.append(result)
            return result

        try:
            with patch.object(mcp_server, "_identity_from_ctx",
                              return_value=(identity["user_groups"], identity["username"], user)), \
                 patch.object(pipeline, "answer_question", side_effect=capture):
                response = json.loads(mcp_server._tool_knowledge_qa_impl(question, top_k=5, history=history))
            row["mcp"] = {**pack(results[0]), "answer": response["answer"], "refused": response["refused"],
                          "answer_status": response.get("answer_status"),
                          "returned_sources": response["sources"]}
        except Exception as exc:
            row["mcp"] = {"error": type(exc).__name__}
    row["seconds"] = round(time.monotonic() - started, 2)
    return row


if __name__ == "__main__":
    cases = HERE / "answer_audit_cases.json"
    if not any(arg == "--only" or arg.startswith("--only=") for arg in sys.argv[1:]):
        ids = [case["id"] for case in json.loads(cases.read_text(encoding="utf-8"))["questions"]]
        sys.argv.extend(["--only", ",".join(ids)])
    if not any(arg == "--output" or arg.startswith("--output=") for arg in sys.argv[1:]):
        sys.argv.extend(["--output", str(HERE / "answer_audit_results.json")])
    isolated.main(question_file=cases, corpus_builder=build_corpus, observe_question=observe, project_linked=True)

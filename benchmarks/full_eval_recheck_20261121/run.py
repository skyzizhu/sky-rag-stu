"""Replay the Nov 20 synthetic exam without touching the live knowledge base."""
from __future__ import annotations

import argparse
import contextlib
import gc
import importlib.util
import io
import json
import os
import secrets
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ORIGINAL = HERE.parent / "full_eval_20261120"
sys.path[:0] = [str(ROOT), str(HERE.parent / "rag_retrieval")]

from qdrant_client import QdrantClient
from fixtures import PLAN, grade

from src import auth_db, manage, mcp_server, pipeline, projects, retriever
from src.config import get_config
from src.embedding import EmbeddingClient
from src.llm import LLMClient
from src.permissions import visible_groups
from src.query_understanding import QueryUnderstanding
from src.vector_store import VectorStore


class KeywordFixtureEmbedding:
    """Supply inert vectors for the replay that measures only the real BM25 path."""

    def dimension(self):
        return 2

    def embed_query(self, query, *, timeout=None):
        return [1.0, 0.0]

    def embed_texts(self, texts):
        return [self.embed_query(text) for text in texts]

    def model_available(self):
        return True


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_corpus(base, cfg, store, embedding, *, latest_charter=None):
    gen = load_module("exam_fixture", ORIGINAL / "gen_corpus.py")
    gen.generate(base)
    plan = PLAN
    departments = {name: auth_db.create_department(name) for name in
                   dict.fromkeys(row[3] for row in plan)}
    for owner, department in dict.fromkeys((row[4], row[3]) for row in plan):
        auth_db.create_user(owner, secrets.token_urlsafe(18),
                            department_id=departments[department]["id"])

    def ingest(row, project_id=None):
        relative, groups, users, department, owner = row
        path = cfg.knowledge_dir / relative
        os.utime(path, (1795132800, 1795132800))
        summary = pipeline.ingest_files(
            paths=[path], config=cfg, store=store, embedding_client=embedding,
            default_groups=[departments[g]["permission_key"] if g in departments else g for g in groups],
            default_allowed_users=users, default_department=department,
            default_project_id=project_id, owner=owner,
        )
        return {"path": relative, "rows": summary.file_rows, "errors": summary.file_errors}

    indexed = [ingest(row) for row in plan]
    chart = next(row for row in plan if "星云项目章程" in row[0])
    file = cfg.knowledge_dir / chart[0]
    old = file.read_text(encoding="utf-8")
    updated = latest_charter or (old.replace("预算 120 万元", "预算 135 万元")
               .replace("M3 系统上线：2024-09。", "M3 系统上线：2024-09；M4 项目验收：2024-12。")
               .replace("徐凯（后端）。", "徐凯（后端）、刘洋（测试）。"))
    if "预算 135 万元" not in updated:
        updated = old + "\n## 变更记录\n2024-05：预算由 120 万元调整为 135 万元，新增 M4 验收里程碑与测试成员刘洋。\n"
    file.write_text(updated, encoding="utf-8")
    indexed.append(ingest(chart))
    missing = cfg.knowledge_dir / "work" / "missing.md"
    console = io.StringIO()
    with contextlib.redirect_stdout(console):
        summary = pipeline.ingest_files(paths=[missing], config=cfg, store=store,
                                        embedding_client=embedding)
    return plan, indexed, ingest, {"errors": summary.file_errors, "stdout": console.getvalue(),
                                   "ok_files": summary.ok_files}


def identity(q):
    username = q["asker"].get("username")
    if not username:
        return {"username": "", "user_groups": None}
    user = auth_db.get_user(username)
    key = auth_db.department_permission_key(user.get("department"), user.get("department_id"))
    return {"username": username, "user_groups": visible_groups(key)}


def link_fixture_projects(plan):
    project_ids = {}
    for path, *_ in plan:
        if not path.startswith("projects/"):
            continue
        name = path.split("/")[1]
        if name not in project_ids:
            project_ids[name] = projects.save_project(projects.ProjectFields(name, None, None, [], ""))["id"]
        manage.update_metadata(path, {"project_id": project_ids[name]})


def observe(q, *, cloud=False, keyword=False, override=None, grader=grade):
    question = q["question"]
    filters = q.get("filters") or None
    started = time.monotonic()
    if override is not None:
        qu = QueryUnderstanding(original=question, vector_query=question, ok=True, **override)
        with patch("src.pipeline.understand_query", return_value=qu), patch(
                "src.pipeline.get_llm_client", return_value=None):
            result, meta = pipeline.query_evidence(question, filters=filters, use_rerank=False, **identity(q))
    elif cloud:
        result = pipeline.answer_question(question, filters=filters, use_rerank=not keyword, **identity(q))
        meta = result.evidence_meta or {}
    else:
        result, meta = pipeline.query_evidence(question, filters=filters, use_llm=False,
                                               use_rerank=not keyword, **identity(q))
    focus = ("coverage", "route", "metadata_scope", "processed_documents", "selected_documents",
             "limited_by", "scope_issue", "stage2", "comparison", "version_comparison")
    return {
        "id": q["id"], "difficulty": q["difficulty"], "question": question,
        "seconds": round(time.monotonic() - started, 3),
        "elapsed": result.elapsed, "filters": result.filters,
        "meta": {key: meta[key] for key in focus if key in meta},
        "grade": grader(q, result),
        "sources": list(dict.fromkeys(s.get("path") for s in result.sources)),
        "source_versions": [{"path": path, "version": version, "status": status}
                            for path, version, status in dict.fromkeys(
                                (s.get("path"), s.get("version"), s.get("status"))
                                for s in result.sources)],
        "answer": result.answer,
    }


def replay(questions, plan, ingest):
    by_id = {q["id"]: q for q in questions}
    rows = []

    def pair(test, qid, **overrides):
        row = observe(by_id[qid], override=overrides)
        rows.append({"test": test, **row})

    for qid in ("M03", "C01", "C13"):
        pair("year_as_mtime", qid, time_range={"from": "2024-01-01", "to": "2024-12-31"})
        pair("year_as_content", qid)
    pair("year_as_content_enum", "C13", enumeration=True, enumeration_scope="cross_document")
    pair("department_as_metadata", "S06", metadata_scope={"department": "研发部"})
    pair("department_as_content", "S06")
    pair("generic_doc_type_as_metadata", "C11", metadata_scope={"doc_type": "制度"})
    pair("generic_doc_type_as_content", "C11")
    pair("archive_only", "C03", filters={"status": "archive"})
    pair("active_and_archive", "C03", filters={"status": "all"})
    project_cases = {
        "S03": {"project": "星云项目"},
        "S09": {"project": "天枢"},
        "M02": {"project": "星云项目"},
        "M05": {"project": "星云项目", "doc_type": "章程"},
        "M11": {"project": "天枢"},
        "C02": {},
        "C05": {},
    }
    for qid, scope in project_cases.items():
        pair("unregistered", qid, metadata_scope=scope)
    project_ids = {}
    for name in ("星云项目", "天枢项目", "北斗项目"):
        project = projects.save_project(projects.ProjectFields(name, None, None, [], ""))
        project_ids[name] = project["id"]
    pair("registered_without_document_link", "S03", metadata_scope=project_cases["S03"])
    for row in plan:
        project_name = row[0].split("/")[1]
        if project_name in project_ids and row[0].startswith("projects/"):
            if not ingest(row, project_ids[project_name])["rows"]:
                raise RuntimeError(f"Could not link fixture {row[0]}")
    for qid, scope in project_cases.items():
        pair("registered_and_linked", qid, metadata_scope=scope)
    pair("linked_with_exact_doc_type", "M05", metadata_scope={"project": "星云项目", "doc_type": "项目章程"})
    pivot = projects.get_project(project_ids["天枢项目"])
    projects.save_project(projects.ProjectFields("天枢", None, None, [], ""),
                          project_id=pivot["id"], revision=pivot["revision"])
    for qid in ("S09", "M11"):
        pair("linked_to_exact_extracted_name", qid, metadata_scope=project_cases[qid])
    return rows


def check_mcp(questions):
    rows = []
    for question in questions:
        expect = question["expect"]
        if not (expect.get("sources_must_empty") or expect.get("must_not_sources")):
            continue
        actor = identity(question)
        with patch("src.mcp_server._identity_from_ctx",
                   return_value=(actor["user_groups"], actor["username"], {})):
            result = json.loads(mcp_server._tool_knowledge_search_impl(question["question"]))
        sources = list(dict.fromkeys(item["path"] for item in result["matches"]))
        forbidden = expect.get("sources_must_empty", []) + expect.get("must_not_sources", [])
        rows.append({"id": question["id"], "sources": sources, "refused": result["refused"],
                     "forbidden_hits": [pat for pat in forbidden if any(pat in p for p in sources)]})
    return rows


def main(*, question_file=ORIGINAL / "questions.json", corpus_builder=build_corpus,
         observe_question=observe, project_linked=False):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cloud", action="store_true", help="Run the configured LLM on synthetic content")
    mode.add_argument("--replay-only", action="store_true", help="Run controlled scope comparisons only")
    parser.add_argument("--keyword", action="store_true", help="Use only real BM25 plus the in-memory payload filters")
    parser.add_argument("--only", help="Comma-separated question IDs")
    parser.add_argument("--project-linked", action="store_true", help="Link project documents through document management")
    parser.add_argument("--output", type=Path, help="Override the output JSON file")
    args = parser.parse_args()
    questions = json.loads(question_file.read_text(encoding="utf-8"))["questions"]
    selected = set(args.only.split(",")) if args.only else None
    destination = args.output or HERE / ("cloud_keyword.json" if args.cloud and args.keyword else
                          "cloud.json" if args.cloud else "controls.json" if args.replay_only
                          else "keyword.json" if args.keyword else "evidence.json")

    with tempfile.TemporaryDirectory(prefix="rag-exam-") as temp, contextlib.ExitStack() as stack:
        base = Path(temp).resolve()
        cfg = get_config()
        cfg = replace(cfg, knowledge_dir=base / "knowledge", qdrant_collection="rag_exam_isolated",
                      ollama_url="http://127.0.0.1:11434", llm_api_key=cfg.llm_api_key if args.cloud else "",
                      auto_tag=False, debug=False, rerank_backend="local_onnx", rerank_threads=2,
                      rerank_enabled=not args.keyword)
        client = QdrantClient(":memory:")
        stack.callback(client.close)
        with patch("src.vector_store.QdrantClient", return_value=client):
            store = VectorStore(config=cfg)
        stack.enter_context(patch("src.vector_store.QdrantClient",
                                  side_effect=AssertionError("Benchmark must use in-memory Qdrant")))
        isolate = load_module("retrieval_fixture", HERE.parent / "rag_retrieval" / "run.py").isolate
        isolate(stack, base, cfg, store)
        stack.callback(gc.collect)
        if args.keyword:
            embedding = KeywordFixtureEmbedding()
            stack.enter_context(patch.object(store, "search", return_value=[]))
        else:
            embedding = EmbeddingClient(cfg)
            embedding._session.trust_env = False
        if not embedding.model_available():
            raise RuntimeError("Local embedding model is unavailable")
        for module in (pipeline, retriever):
            stack.enter_context(patch.object(module, "get_embedding_client", return_value=embedding))
        engine = retriever.Retriever(embedding_client=embedding, store=store, config=cfg)
        stack.enter_context(patch("src.pipeline.get_retriever", return_value=engine))
        stack.enter_context(patch("src.keyword_search.get_vector_store", return_value=store))
        stack.enter_context(patch("src.manage.get_vector_store", return_value=store))
        stack.enter_context(patch("src.manage.get_config", return_value=cfg))
        if args.cloud:
            llm = LLMClient(cfg)
            stack.enter_context(patch("src.pipeline.get_llm_client", return_value=llm))
        else:
            # Query evidence still supplies a client to the local reranker.
            stack.enter_context(patch("src.pipeline.get_llm_client", return_value=None))
        with contextlib.redirect_stdout(io.StringIO()):
            plan, indexed, ingest, missing = corpus_builder(base, cfg, store, embedding)
            if args.project_linked and not project_linked:
                link_fixture_projects(plan)
        failed = [item["path"] for item in indexed if not item["rows"]]
        payload = {"indexed": sum(bool(item["rows"]) for item in indexed[:-1]),
                   "cards": store.count(), "failed": failed, "missing_ingest": missing,
                   "configuration": {"model": cfg.llm_model if args.cloud else None,
                                     "embedding": cfg.embedding_model if not args.keyword else None,
                                     "rerank": cfg.rerank_backend if cfg.rerank_enabled else None,
                                     "project_linked": project_linked or args.project_linked,
                                     "keyword_only": args.keyword},
                   "questions": [], "mcp": [], "replay": []}

        def save():
            destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
                                   encoding="utf-8")

        print(f"Indexed {payload['indexed']} documents / {payload['cards']} cards", flush=True)
        for q in ([] if args.replay_only else questions):
            if selected is not None and q["id"] not in selected:
                continue
            try:
                row = observe_question(q, cloud=args.cloud, keyword=args.keyword)
            except Exception as exc:
                row = {"id": q["id"], "error": type(exc).__name__}
            payload["questions"].append(row)
            save()
            print(q["id"], row.get("seconds"),
                  row.get("error") or row.get("grade", {}).get("checks", "recorded"), flush=True)
        if not selected:
            with contextlib.redirect_stdout(io.StringIO()):
                payload["mcp"] = check_mcp(questions)
                if not args.cloud and not (project_linked or args.project_linked):
                    payload["replay"] = replay(questions, plan, ingest)
            save()


if __name__ == "__main__":
    main()

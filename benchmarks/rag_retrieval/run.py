"""Run a synthetic Web/MCP retrieval benchmark against local embeddings only."""
from __future__ import annotations

import argparse
import contextlib
import gc
import io
import json
import re
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from cases import FILES, GENERAL, PROJECTS, project_documents, questions
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from qdrant_client import QdrantClient

from src import auth_db, business_directions, domains, index_epoch, index_state, keyword_search, mcp_server, pipeline, projects
from src.config import get_config
from src.embedding import EmbeddingClient
from src.project_directories import upload_directory
from src.retriever import Retriever
from src.vector_store import VectorStore


def write_document(path, title, paragraphs):
    if path.suffix == ".docx":
        doc = Document()
        doc.add_heading(title, level=1)
        for line in paragraphs:
            doc.add_paragraph(line)
        doc.save(path)
    elif path.suffix == ".xlsx":
        doc = Workbook()
        doc.active.title = "运营记录"
        doc.active.append(["项目资料", "记录明细"])
        for line in paragraphs:
            doc.active.append([title, line])
        doc.save(path)
    elif path.suffix == ".pptx":
        doc = Presentation()
        slide = doc.slides.add_slide(doc.slide_layouts[1])
        slide.shapes.title.text = title
        slide.placeholders[1].text = "\n".join(paragraphs)
        doc.save(path)
    else:
        if path.suffix == ".html":
            from html import escape

            text = f"<h1>{escape(title)}</h1>" + "".join(f"<p>{escape(line)}</p>" for line in paragraphs)
        else:
            text = f"# {title}\n\n" + "\n\n".join(paragraphs)
        path.write_text(text, encoding="utf-8")


def isolate(stack, base, cfg, store):
    auth = base / "auth"
    for module, name, value in (
        (auth_db, "AUTH_DIR", auth), (auth_db, "DB_PATH", auth / "synthetic.db"),
        (auth_db, "SECRET_PATH", auth / "unused.key"),
        (index_state, "STATE_PATH", base / "ledger.json"),
        (index_epoch, "EPOCH_PATH", base / "epoch"),
        (domains, "REGISTRY_PATH", base / "domains.json"),
    ):
        stack.enter_context(patch.object(module, name, value))
    for target in ("src.config.get_config", "src.pipeline.get_config"):
        stack.enter_context(patch(target, return_value=cfg))
    for target in ("src.vector_store.get_vector_store", "src.pipeline.get_vector_store"):
        stack.enter_context(patch(target, return_value=store))
    stack.enter_context(patch("src.pipeline.get_llm_client", side_effect=AssertionError("No cloud model in this benchmark")))
    auth_db.init_db()


def build_corpus(cfg, store, embedding):
    dept = {
        name: auth_db.create_department(name)
        for name in ("城运部", "工务部", "物流部", "医疗部", "环保部", "评测运营部", "访客组")
    }
    for actor, team in (("auditor", "评测运营部"), ("guest", "访客组")):
        auth_db.create_user(actor, "Synthetic-eval-123", department_id=dept[team]["id"])
    directions = {
        name: business_directions.create_direction(name)["id"] for name in dict.fromkeys(row[2] for row in PROJECTS)
    }
    ids, manifest = {}, {}

    def record(path, doc_type, department=None, project_id=None, groups=None, users=None):
        result = pipeline.ingest_files(paths=[path], config=cfg, store=store, embedding_client=embedding,
                                       default_doc_type=doc_type, default_department=department,
                                       default_project_id=project_id, default_groups=groups,
                                       default_allowed_users=users)
        if result.ok_files != 1 or not result.total_chunks:
            raise RuntimeError(f"Ingest failed for {path.name}: {result.failed_files}")

    for spec in PROJECTS:
        key, name, direction, team, other, *_ = spec
        project = projects.save_project(projects.ProjectFields(name, directions[direction], None, [], "模拟评测项目"))
        ids[name] = project["id"]
        for kind, lines in project_documents(spec).items():
            doc_type, extension = FILES[kind]
            title = f"{name}{doc_type}"
            with upload_directory(cfg.knowledge_dir, "projects", ids[name]) as directory:
                path = directory / f"{title}.{extension}"
                path.parent.mkdir(parents=True, exist_ok=True)
                write_document(path, title,
                               [line.replace("17 秒", "29 秒") for line in lines] if key == "air" and kind == "report" else lines)
            department = other if kind == "handover" else ("评测运营部" if kind in ("ledger", "private") else team)
            groups = [dept["评测运营部"]["permission_key"]] if kind == "ledger" else (None if kind == "private" else ["all"])
            users = ["auditor"] if kind == "private" else None
            record(path, doc_type, department, ids[name], groups, users)
            if key == "air" and kind == "report":
                (cfg.knowledge_dir.parent / "air-report-1.0.docx").write_bytes(path.read_bytes())
                write_document(path, title, lines)
                record(path, doc_type, department, ids[name], groups, users)
            manifest[f"{key}-{kind}"] = {
                "path": path.relative_to(cfg.knowledge_dir).as_posix(), "format": extension,
                "project": name, "direction": direction, "department": department, "doc_type": doc_type,
                "acl": "受限" if users else ("内部" if kind == "ledger" else "公开"),
            }
    with upload_directory(cfg.knowledge_dir, "learning", None) as directory:
        directory.mkdir(parents=True, exist_ok=True)
        for key, filename, content in GENERAL:
            path = directory / filename
            write_document(path, path.stem, [content])
            manifest[key] = {"path": path.relative_to(cfg.knowledge_dir).as_posix(),
                             "format": path.suffix[1:], "acl": "公开"}
    for key, spec in manifest.items():
        if key.startswith("g-"):
            record(cfg.knowledge_dir / spec["path"], "通用资料")
    identities = {
        "auditor": ([dept["评测运营部"]["permission_key"]], "auditor", {"department": "评测运营部"}),
        "guest": ([dept["访客组"]["permission_key"]], "guest", {"department": "访客组"}),
    }
    return manifest, identities


class FixedQueryVectors:
    """Replay real vectors for an identical, timing-independent input across profiles."""

    def __init__(self, vectors, real):
        self.vectors = vectors
        self.real = real

    def embed_query(self, query, *, timeout=None):
        return self.vectors[query] if query in self.vectors else self.real.embed_query(query, timeout=timeout)

    def model_available(self):
        return True


def grade(case, evidence, meta, manifest):
    path_ids = {row["path"]: key for key, row in manifest.items()}
    expected = set(case["expected"])
    def identities_for(item):
        key = path_ids.get(item.get("path"))
        return {key, f"{key}@{item.get('version')}"} if key else set()

    top = set().union(*(identities_for(item) for item in evidence[:5]))
    all_hits = set().union(*(identities_for(item) for item in evidence))
    positive = bool(expected)
    normalized = lambda text: re.sub(r"\s+", "", text).casefold()
    texts = [normalized(str(item.get("text") or "")) for item in evidence
             if identities_for(item) & expected]
    facts = [fact for fact in case["facts"] if any(normalized(fact) in text for text in texts)]
    actor = case["actor"]
    prohibited = set(case["forbidden"])
    if actor == "guest":
        prohibited |= {key for key, row in manifest.items() if row["acl"] in ("受限", "内部")}
    return {
        "top5_documents": sorted(expected & top), "all_documents": sorted(expected & all_hits),
        "first_document_relevant": bool(evidence and identities_for(evidence[0]) & expected) if positive else None,
        "false_evidence": bool(evidence) if not positive else None,
        "forbidden_returned": sorted(prohibited & all_hits),
        "supported_facts": facts,
        "returned": len(evidence), "coverage": meta.get("coverage"),
        "matched_documents": meta.get("matched_documents"), "scope_issue": meta.get("scope_issue"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--controls", action="store_true", help="Run only the 12 authorization control queries")
    parser.add_argument("--baseline", action="store_true", help="Run the 78 baseline queries without controls")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    cases = questions()
    if args.controls:
        cases = cases[-12:]
    elif args.baseline:
        cases = cases[:-12]
    with tempfile.TemporaryDirectory(prefix="rag-benchmark-") as tmp, contextlib.ExitStack() as stack:
        base = Path(tmp).resolve()
        cfg = replace(get_config(), knowledge_dir=base / "knowledge", qdrant_collection="synthetic_rag_benchmark",
                      ollama_url="http://127.0.0.1:11434", llm_api_key="", auto_tag=False,
                      top_k=5, ocr_enabled=False, debug=False, rerank_backend="local_onnx",
                      rerank_threads=2)
        if not (cfg.rerank_model_dir / "model_quantized.onnx").exists():
            raise RuntimeError("Local ONNX reranker model is missing")
        client = QdrantClient(":memory:")
        stack.callback(client.close)
        with patch("src.vector_store.QdrantClient", return_value=client):
            store = VectorStore(config=cfg)
        isolate(stack, base, cfg, store)
        stack.callback(gc.collect)
        embedding = EmbeddingClient(cfg)
        embedding._session.trust_env = False
        if not embedding.model_available():
            raise RuntimeError(f"Local Ollama model {cfg.embedding_model!r} is not ready")
        with contextlib.redirect_stdout(io.StringIO()):
            manifest, identities = build_corpus(cfg, store, embedding)
        total_cards = store.count()
        print(f"Indexed {len(manifest)} synthetic files, {total_cards} cards", flush=True)
        (output / "questions.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
        if not args.controls:
            (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            with ZipFile(output / "corpus.zip", "w") as archive:
                for row in manifest.values():
                    archive.write(cfg.knowledge_dir / row["path"], row["path"])
                archive.write(base / "air-report-1.0.docx", "_versions/air-report-1.0.docx")
        sample_embedding_ms = []
        for case in cases[:6]:
            t0 = time.perf_counter()
            embedding.embed_query(case["question"])
            sample_embedding_ms.append(round((time.perf_counter() - t0) * 1000, 2))
        queries = list(dict.fromkeys(case["question"] for case in cases))
        vectors = dict(zip(queries, embedding.embed_texts(queries)))
        fixed = FixedQueryVectors(vectors, embedding)
        profiles = [
            ("vector", False, False, False),
            ("hybrid", True, False, False),
            ("web_evidence", True, True, False),
            ("mcp_search", True, True, True),
        ]
        if args.controls:
            profiles = profiles[-2:]
        run_rows = []
        for profile, hybrid, rerank, is_mcp in profiles:
            profile_cfg = replace(cfg, hybrid_search=hybrid, rerank_enabled=rerank, query_understanding=False)
            retriever = Retriever(embedding_client=fixed, store=store, config=profile_cfg)
            keyword_search.invalidate_cache()
            with patch("src.config.get_config", return_value=profile_cfg), \
                 patch("src.pipeline.get_config", return_value=profile_cfg), \
                 patch("src.pipeline.get_retriever", return_value=retriever), \
                 patch("src.pipeline.get_embedding_client", return_value=fixed):
                for case in cases:
                    groups, username, _ = identities[case["actor"]]
                    t0 = time.perf_counter()
                    with contextlib.redirect_stdout(io.StringIO()):
                        if is_mcp:
                            with patch.object(mcp_server, "_identity_from_ctx", return_value=identities[case["actor"]]):
                                payload = json.loads(mcp_server._tool_knowledge_search_impl(
                                    case["question"], top_k=5, **case["filters"]))
                            if payload.get("error"):
                                raise RuntimeError(f"{case['id']}: {payload['error']}")
                            evidence = payload["matches"]
                            meta = payload.get("coverage_details") or {}
                            aligned = [e["rank"] for e in evidence] == list(range(1, len(evidence) + 1))
                        else:
                            filters = mcp_server._filters(None, case["filters"].get("status"), **{
                                k: v for k, v in case["filters"].items() if k != "status"
                            })
                            result, meta = pipeline.query_evidence(
                                case["question"], top_k=5, config=profile_cfg,
                                filters=filters, use_llm=False, use_query_understanding=False,
                                use_hybrid=hybrid, use_rerank=rerank, user_groups=groups, username=username,
                            )
                            evidence = result.sources
                            ids = [int(n) for n in re.findall(r"^\[(\d+)\]", result.context, flags=re.M)]
                            aligned = ids == [e["rank"] for e in evidence] and all(
                                str(e.get("text") or "") in result.context for e in evidence)
                    ms = round((time.perf_counter() - t0) * 1000, 2)
                    run_rows.append({
                        "case": case["id"], "profile": profile, "ms_excluding_embedding": ms,
                        "aligned": aligned, **grade(case, evidence, meta, manifest),
                        "evidence": [{key: e.get(key) for key in (
                            "rank", "document_id", "path", "version", "status", "score", "text", "chunk_ids"
                        )} for e in evidence],
                    })
            print(f"Completed {profile}: {len(cases)} queries", flush=True)
            (output / f"{profile}.json").write_text(
                json.dumps([row for row in run_rows if row["profile"] == profile], ensure_ascii=False, indent=2),
                encoding="utf-8")
        (output / "run_info.json").write_text(json.dumps({
            "embedding_model": cfg.embedding_model, "reranker": cfg.rerank_model_dir.name,
            "physical_files": len(manifest), "indexed_cards": total_cards,
            "embedding_warm_ms": sample_embedding_ms, "top_k": 5, "profiles": [row[0] for row in profiles],
        }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

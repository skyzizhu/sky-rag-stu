"""Paired functional replay using synthetic documents and the configured real LLM."""
import contextlib
from dataclasses import replace
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import patch

from openpyxl import Workbook
from qdrant_client import QdrantClient

from src import enumeration_processing as current, pipeline, retriever
from src.config import get_config
from src.llm import LLMClient
from src.query_understanding import QueryUnderstanding
from src.vector_store import VectorStore

HERE = Path(__file__).resolve().parent
EXAM = HERE.parent / "eval_round2_20261121"
sys.path.insert(0, str(EXAM))
import run_isolated as exam


def module(path):
    spec = importlib.util.spec_from_file_location("before_enum", path)
    instance = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = instance
    spec.loader.exec_module(instance)
    return instance


def extras(cfg, store, embedding):
    root = cfg.knowledge_dir / "work" / "audit_enum"
    root.mkdir(parents=True, exist_ok=True)
    contract = root / "复测长合同.md"
    text = ["# 复测长合同"]
    for i in range(1, 13):
        text += [f"## 第 {i} 章", ("本章节约定交接记录归档，抄送综合办公室。"
                                  "各部门保存办理过程材料。") * 43,
                 f"强制交付核验条款 REQ-{100+i}：必须提交第 {i} 项的签字证明。",
                 f"可选参考 REF-{100+i}：仅供内部参考，无需交付。"]
    contract.write_text("\n\n".join(text), encoding="utf-8")
    table = root / "复测密集审批表.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["编号", "决定", "依据", "业务说明"])
    for i in range(1, 31):
        sheet.append([f"LOT-{100+i}", "退回" if i <= 20 else "通过", "凭证待复核",
                      "这是本次货物审批的独立记录，已按作业要求留档。" * 2])
    workbook.save(table)
    workbook.close()
    for path in (contract, table):
        summary = pipeline.ingest_files([path], config=cfg, store=store, embedding_client=embedding,
                                        default_groups=["all"])
        assert summary.ok_files == 1 and summary.total_chunks, summary
    return [
        ("CONTRACT", "复测长合同中，列出全部强制交付核验条款的编号。", contract.name,
         [f"REQ-{i}" for i in range(101, 113)]),
        ("TABLE", "复测密集审批表中，列出决定为退回的所有审批记录编号。", table.name,
         [f"LOT-{i}" for i in range(101, 121)]),
    ]


def keys(text, case):
    if case.startswith("CX20"):
        return set(re.findall(r"星云|天枢|昆仑|沧海|北斗", text))
    return set(re.findall(r"\b(?:REQ|REF|LOT)-\d{3}\b", text))


def evaluate(case, result, trace, expected):
    recalled = set()
    read = set()
    for hit in trace["candidate_text"]:
        recalled |= keys(hit, case)
    for text in trace["read_text"]:
        read |= keys(text, case)
    extracted = set(trace.get("accepted", trace["extracted"]))
    cited = set()
    unsupported = []
    sources = {s["rank"]: s for s in result.sources}
    records = result.enumerated_items
    if (result.evidence_meta or {}).get("route") == "project_document_mentions":
        records = [{"name": sources[rank]["source"], "citations": [rank]}
                   for rank in sorted(pipeline.answer_citations(result.answer or "")) if rank in sources]
    for record in records:
        record_keys = keys(record["name"] + " " + record.get("identity", ""), case)
        evidence = [sources[n] for n in record.get("citations", []) if n in sources]
        if "evidence" in record:
            evidence = [
                {"source": source["source"], "text": item["quote"]}
                for item in record["evidence"] for source in evidence
                if item["source_id"] == source.get("source_id") and item["quote"] in source["text"]
            ]
        if case.startswith("CX20"):
            backed = {key for key in record_keys if any(
                key in source.get("source", "") and re.search("立项|章程", source.get("source", ""))
                and re.search("数据安全|等[级級]保护|等保", source["text"])
                for source in evidence)}
        else:
            backed = {key for key in record_keys if any(
                re.search(rf"{re.escape(key)}\s*\|\s*退回(?:\s*\||$)", source["text"])
                if case == "TABLE" else f"强制交付核验条款 {key}" in source["text"]
                for source in evidence)}
        cited |= backed
        if not backed:
            unsupported.append(record["name"])
    gold = set(expected)
    return {"expected": sorted(gold), "supported": sorted(cited & gold),
            "extra_supported": sorted(cited - gold), "unsupported_records": unsupported,
            "missing": {key: ("review" if key in extracted else "extraction" if key in read
                              else "reading" if key in recalled else "candidate_or_reading")
                        for key in sorted(gold - cited)}}


def main(baseline_path, followup=False, *, reading_order=False):
    baseline = current if reading_order else module(baseline_path)

    def baseline_process(*args, **kwargs):
        kwargs.pop("read_priority", None)
        return baseline.process_enumeration(*args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="enum-budget-audit-") as temp, contextlib.ExitStack() as stack:
        base = Path(temp)
        cfg = replace(get_config(), knowledge_dir=base / "knowledge", qdrant_collection="enum_retest",
                      auto_tag=False, debug=False, rerank_enabled=False,
                      enum_stage2_enabled=True, enum_verify_enabled=True)
        real_llm = LLMClient(cfg)
        client = QdrantClient(":memory:")
        stack.callback(client.close)
        with patch("src.vector_store.QdrantClient", return_value=client):
            store = VectorStore(config=cfg)
        isolate = exam.isolated.load_module("enum_fixture", HERE.parent / "rag_retrieval" / "run.py").isolate
        isolate(stack, base, cfg, store)
        embedding = exam.isolated.KeywordFixtureEmbedding()
        engine = retriever.Retriever(embedding_client=embedding, store=store, config=cfg)
        stack.enter_context(patch.object(store, "search", return_value=[]))
        stack.enter_context(patch("src.pipeline.get_embedding_client", return_value=embedding))
        stack.enter_context(patch("src.pipeline.get_retriever", return_value=engine))
        stack.enter_context(patch("src.keyword_search.get_vector_store", return_value=store))
        stack.enter_context(patch("src.manage.get_vector_store", return_value=store))
        stack.enter_context(patch("src.manage.get_config", return_value=cfg))
        with contextlib.redirect_stdout(io.StringIO()):
            _, indexed, _, _ = exam.build_corpus(base, cfg, store, embedding)
            extra_cases = extras(cfg, store, embedding)
        assert all(row["rows"] and not row["errors"] for row in indexed)
        old_question = next(q for q in json.loads((EXAM / "questions60.json").read_text(encoding="utf-8"))["questions"]
                            if q["id"] == "CX20")
        cases = [("CX20", old_question["question"], None, ["星云", "天枢", "昆仑"]), *extra_cases]
        if followup:
            cases = [
                ("CX20_SEMANTIC", "请跨项目盘点，哪些项目在立项报告或章程里明确要求做数据安全测评或按等保标准建设？",
                 None, ["星云", "天枢", "昆仑"]),
                ("CONTRACT_LIMIT", *extra_cases[0][1:]),
            ]
        name = "followup.json" if followup else "results.json"
        target = HERE / ("reading_order_" + name if reading_order else name)
        output = {"model": cfg.llm_model, "documents": len(store.list_documents({"status": "active"})),
                  "cards": store.count(), "rows": []}
        for index, (case, question, source, gold) in enumerate(cases):
            identity = exam.isolated.identity(old_question)
            case_cfg = replace(cfg, enum_max_calls=4) if case == "CONTRACT_LIMIT" else cfg
            for label, implementation in ([("before", baseline), ("after", current)] if index % 2 == 0
                                          else [("after", current), ("before", baseline)]):
                trace = {"calls": [], "candidate_text": [], "read_text": [], "extracted": [], "accepted": []}
                original_retrieve = engine.retrieve
                original_validate = implementation.validated_records

                def validated(*args, **kwargs):
                    records, rejected = original_validate(*args, **kwargs)
                    trace["accepted"] += list(keys(" ".join(r["name"] + " " + r.get("identity", "") for r in records), case))
                    return records, rejected

                class TracedModel:
                    def chat(self, messages, usage_out=None, **kwargs):
                        stage = {current.EXTRACTION_PROMPT: "extract", current.PLANNER_PROMPT: "plan",
                                 current.VERIFICATION_PROMPT: "review"}.get(messages[0]["content"], "other")
                        charge = {}
                        try:
                            raw, elapsed = real_llm.chat(messages, usage_out=charge, temperature=0, **kwargs)
                        except Exception as exc:
                            trace["calls"].append({"stage": stage, "error": type(exc).__name__, "detail": str(exc)[:160]})
                            raise
                        if usage_out is not None:
                            usage_out.update(charge)
                        trace["calls"].append({"stage": stage, "usage": charge, "timeout": kwargs.get("timeout")})
                        if stage == "extract":
                            body = json.loads(messages[-1]["content"])
                            trace["read_text"] += [row["text"] for row in body["materials"]]
                            try:
                                rows = implementation.parse_object(raw).get("items", [])
                                trace["extracted"] += list(keys(" ".join(str(row.get("name", "")) for row in rows
                                                                         if isinstance(row, dict)), case))
                            except (ValueError, TypeError):
                                pass
                        return raw, elapsed
                    def chat_stream(self, messages, **kwargs):
                        return real_llm.chat_stream(messages, **kwargs)
                def retrieve(*args, **kwargs):
                    hits = original_retrieve(*args, **kwargs)
                    trace["candidate_text"] += [item.text for item in hits]
                    return hits
                qu = QueryUnderstanding(original=question, vector_query=question, ok=True,
                                        enumeration=True, enumeration_scope="document" if source else "cross_document")
                process = baseline_process if label == "before" else current.process_enumeration
                with (patch("src.pipeline.process_enumeration", process),
                      patch("src.pipeline.render_records", implementation.render_records),
                      patch("src.pipeline.get_llm_client", return_value=TracedModel()),
                      patch("src.pipeline.understand_query", return_value=qu),
                      patch.object(implementation, "validated_records", side_effect=validated),
                      patch.object(engine, "retrieve", side_effect=retrieve),
                      contextlib.redirect_stdout(io.StringIO())):
                    result = pipeline.answer_question(question, filters={"source": source} if source else None,
                                                      config=case_cfg, use_rerank=False,
                                                      history=[{"role": "user", "content": "请按资料核对。"}], **identity)
                row = {"case": case, "variant": label, "question": question,
                       "grade": evaluate(case, result, trace, gold), "steps": trace["calls"],
                       "metadata": {key: (result.evidence_meta or {}).get(key)
                                    for key in ("route", "coverage", "stage2", "selected_documents_meta")},
                       "records": result.enumerated_items, "sources": result.sources,
                       "answer": result.answer, "max_calls": case_cfg.enum_max_calls,
                       "accepted_before_review": sorted(set(trace["accepted"]))}
                output["rows"].append(row)
                target.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(case, label, len(row["grade"]["supported"]), "/", len(gold), len(trace["calls"]), flush=True)


if __name__ == "__main__":
    use_reading_order = "--reading-order" in sys.argv[1:]
    main(None if use_reading_order else Path(sys.argv[1]), "--followup" in sys.argv[1:],
         reading_order=use_reading_order)

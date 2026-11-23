"""Run isolated application-entry workflows and small-concurrency timing probes."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import math
from pathlib import Path
import re
import statistics
import sys
from threading import Barrier, Event, local
import time
from types import SimpleNamespace
from unittest import mock

from src import auth_db, manage, mcp_server, pipeline, projects, reranker
from src.permissions import visible_groups
from tests._helpers import IngestHarness


QUESTION = "巡检手册 OPS-417 规定的恢复时限是多少？"
BASE_HISTORY = [{"role": "user", "content": "这轮请按知识库的巡检手册核对。"}]
TITLE = "服务巡检手册.md"
CURRENT = "AUDGOLD_RESTORE_A"
NEW = "AUDGOLD_RESTORE_B"
STAGES = ("query_understanding", "retrieval_only", "rerank", "assembly", "llm", "conversation")
HERE = Path(__file__).resolve().parent


class FixtureLLM:
    """Deterministic transport stand-in that selects an actual supplied citation."""

    def chat(self, messages, usage_out=None, **kwargs):
        if usage_out is not None:
            usage_out.update(input=40, output=10, finish_reason="stop")
        return json.dumps({"intent": "核对样本文档中的明确事实"}), 0.0

    def chat_stream(self, messages, usage_out=None, **kwargs):
        prompt = messages[-1]["content"]
        citations = list(re.finditer(r"\[(\d+)\]\s*来源:", prompt))
        for i, citation in enumerate(citations):
            end = citations[i + 1].start() if i + 1 < len(citations) else len(prompt)
            evidence = re.search(r"\bAUDGOLD_[A-Z0-9_]+\b", prompt[citation.end():end])
            if evidence:
                yield f"[[answer_status:answered]]\n验收事实 {evidence[0]} [{citation[1]}]。"
                yield "后续片段。"
                break
        else:
            yield "[[answer_status:insufficient_evidence]]\n未定位到验收事实。"
        if usage_out is not None:
            usage_out.update(input=110, output=20, finish_reason="stop")


def ms(seconds):
    return round(seconds * 1000, 2)


def quantile(samples, fraction):
    ordered = sorted(samples)
    return ordered[math.ceil(len(ordered) * fraction) - 1]


def summarize(rows, batch_ms):
    walls = [row["wall_ms"] for row in rows]
    first = [row["first_delta_ms"] for row in rows if row.get("first_delta_ms") is not None]
    fields = {key for row in rows for key in row.get("stage_ms", {})}
    phases = {key: statistics.median(row["stage_ms"][key] for row in rows if key in row["stage_ms"])
              for key in sorted(fields)}
    return {"requests": len(rows), "correct": sum(row["passed"] for row in rows),
            "wall_p50_ms": statistics.median(walls), "wall_p95_ms": quantile(walls, .95),
            "first_delta_p50_ms": statistics.median(first) if first else None,
            "first_delta_p95_ms": quantile(first, .95) if first else None,
            "requests_per_second": round(len(rows) * 1000 / batch_ms, 2),
            "stage_p50_ms": phases}


def source_rows(sources):
    keys = ("source", "document_id", "version", "status", "page", "section_path", "project_id", "classification")
    return [{**{key: row.get(key) for key in keys},
             "rank": row.get("rank"), "chunk_ids": row.get("chunk_ids", [])} for row in sources]


def doc_refs(sources):
    return sorted((row["source"], row["version"], tuple(row["chunk_ids"])) for row in sources)


class WorkflowAudit(IngestHarness):
    def setUp(self):
        super().setUp()
        self.cfg = replace(
            self.cfg, chunk_size=640, chunk_overlap=80, top_k=4,
            query_understanding=True, hybrid_search=True, rerank_enabled=True,
            rerank_backend="local_onnx", rerank_model_dir=ARGS.model_dir, rerank_threads=4,
            rerank_recall_k=6, context_max_tokens=4000, context_max_per_doc=3,
            enum_stage2_enabled=False, chat_recent_turns=3, chat_summary_trigger_tokens=1500,
        )
        self.use_fakes()
        self.addCleanup(self.store.client.close)
        self.llm = FixtureLLM()
        self.local = local()
        self.instrument()
        self.users = {}
        self.tokens = {}
        finance = auth_db.create_department("财务部")
        development = auth_db.create_department("研发部")
        for username, department in (("audit_finance", finance), ("audit_develop", development)):
            self.users[username] = auth_db.create_user(username, "FixtureOnly123", department["id"])
            self.tokens[username] = auth_db.create_token(self.users[username]["id"], "isolated-workflow")
        self.finance, self.develop = self.users.keys()
        self.groups = {u: visible_groups(auth_db.get_user(u)["department_group"]) for u in self.users}
        self.seed()

    def instrument(self):
        run_scope = mcp_server._answer_in_scope
        identity = mcp_server._identity_from_ctx
        log = mcp_server._log_qa
        rank = reranker.rerank_local

        def capture(question, **kwargs):
            result = run_scope(question, **kwargs)
            self.local.result = result
            return result

        def auth(ctx):
            start = time.perf_counter()
            try:
                return identity(ctx)
            finally:
                self.local.extra["mcp_identity"] = ms(time.perf_counter() - start)

        def audit(*args, **kwargs):
            start = time.perf_counter()
            try:
                return log(*args, **kwargs)
            finally:
                self.local.extra["mcp_audit"] = ms(time.perf_counter() - start)

        def ranked(*args, **kwargs):
            outcome = rank(*args, **kwargs)
            for key, value in outcome.timings.items():
                label = f"onnx_{key}"
                self.local.extra[label] = self.local.extra.get(label, 0) + ms(value)
            self.local.extra["onnx_cold_load"] = int(outcome.cold_load)
            return outcome

        for target, replacement in (
            ("src.pipeline.get_llm_client", self.llm),
            ("src.mcp_server._answer_in_scope", capture),
            ("src.mcp_server._identity_from_ctx", auth),
            ("src.mcp_server._log_qa", audit),
            ("src.reranker.rerank_local", ranked),
            ("src.pipeline.rerank_documents_local", ranked),
        ):
            patch = mock.patch(target, return_value=replacement) if target.endswith("get_llm_client") else mock.patch(target, new=replacement)
            patch.start()
            self.addCleanup(patch.stop)

    def seed(self):
        from pptx import Presentation

        self.path = self.write_note(f"巡检手册 OPS-417 规定恢复时限为 4 小时。验收事实 {CURRENT}。", TITLE)
        self.ingest([self.path], default_doc_type="巡检手册")
        for i in range(20):
            body = f"巡检手册 OPS-{500+i} 规定告警跟踪和维护窗口。\n\n" + ("编号和负责人需在维护记录中填写。\n\n" * 26)
            self.ingest([self.write_note(body, f"运维对照{i:02}.md")], default_doc_type="巡检手册")
        self.ingest([self.write_note("出差内审 FINA-119 的签批周期为三天。验收事实 AUDGOLD_FINANCE。", "财务内审.md")],
                    default_doc_type="内部差旅", default_groups=["财务部"], default_allowed_users=[])
        self.ingest([self.write_note("安全授权 SAFE-716 的审批人为测试员。验收事实 AUDGOLD_RESTRICTED。", "受限审批.md")],
                    default_doc_type="受限审批", default_groups=[], default_allowed_users=[self.develop])
        self.project_ids = {}
        for name, amount, marker in (("星云", "120 万元", "AUDGOLD_NEBULA"), ("天枢", "260 万元", "AUDGOLD_TIANSHU")):
            project = projects.save_project(projects.ProjectFields(f"{name}项目", None, None, [], ""))
            self.project_ids[name] = project["id"]
            file = self.write_note(f"{name}项目预算为 {amount}。验收事实 {marker}。", f"{name}预算.md")
            self.ingest([file], default_project_id=project["id"], default_doc_type="预算")
        presentation = Presentation()
        for i, title, body in ((1, "页面控制", "第 1 张是目录。"),
                               (2, "故障切换", "故障切换 SLIDE-308 的操作窗口为 23 时。验收事实 AUDGOLD_SLIDE。")):
            slide = presentation.slides.add_slide(presentation.slide_layouts[1])
            slide.shapes.title.text = title
            slide.placeholders[1].text = body
        file = self.path.parent / "故障切换.pptx"
        presentation.save(file)
        self.ingest([file], default_doc_type="故障切换")

    def ctx(self, user):
        return SimpleNamespace(request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"authorization": "Bearer " + self.tokens[user]})))

    def timings(self, result):
        output = self.local.extra.copy()
        if not result.cache_hit:
            output.update({key: ms(result.elapsed[key]) for key in STAGES if key in result.elapsed})
            for title, key in (("Query Embedding", "embedding"), ("数据库检索（向量通道）", "vector"),
                               ("BM25 关键词检索", "bm25")):
                values = [node["elapsed"] for node in result.trace
                          if title in node["name"] and node.get("elapsed") is not None]
                if values:
                    output[key] = ms(sum(values))
        return output

    def request(self, mode, question=QUESTION, *, user=None, doc_type="巡检手册", history=None,
                session_id=None, state=None, project=None, expected=CURRENT, use_rerank=True):
        user = user or self.finance
        self.local.extra = {}
        self.local.result = None
        start = time.perf_counter()
        first_delta = None
        stream_text = ""
        transport_refs = []
        if mode == "web":
            filters = {"doc_type": doc_type} if doc_type else {}
            if project:
                filters["project_id"] = [project]
            result, deltas = pipeline.answer_stream(
                question, top_k=4, filters=filters, history=history, session_id=session_id,
                conversation_state=state, user_groups=self.groups[user], username=user,
                use_rerank=use_rerank, config=self.cfg)
            for delta in deltas:
                if first_delta is None and delta.strip():
                    first_delta = ms(time.perf_counter() - start)
                stream_text += delta
            log_start = time.perf_counter()
            auth_db.log_qa_event(user, self.users[user].get("department", ""), "web",
                                 question, result.answer or "", result.sources)
            self.local.extra["web_audit"] = ms(time.perf_counter() - log_start)
            answer = result.answer or ""
        else:
            tool = mcp_server._tool_knowledge_qa_impl if mode == "mcp_qa" else mcp_server._tool_knowledge_search_impl
            payload = json.loads(tool(question, top_k=4, doc_type=doc_type, history=history,
                                      project=project, ctx=self.ctx(user)))
            if payload.get("error"):
                return {"passed": False, "error": payload["error"], "wall_ms": ms(time.perf_counter() - start)}
            result = self.local.result
            transport_refs = source_rows(payload.get("matches", payload.get("sources", [])))
            answer = payload.get("answer") or ""
        wall = ms(time.perf_counter() - start)
        refs = source_rows(result.sources)
        ground = [source for source in result.sources if expected and expected in source.get("text", "")]
        ranks = pipeline.answer_citations(answer)
        citation_ok = (not ranks - {row["rank"] for row in refs})
        evidence_ok = bool(ground) if expected else not refs
        pass_answer = (not expected or mode == "mcp_search"
                       or (expected in answer and bool(ranks & {s["rank"] for s in ground})))
        return {"passed": evidence_ok and citation_ok and pass_answer, "wall_ms": wall,
                "first_delta_ms": first_delta, "stage_ms": self.timings(result), "answer": answer,
                "stream_text": stream_text, "sources": refs, "tool_sources": transport_refs,
                "cache_hit": result.cache_hit, "scope_issue": result.evidence_meta.get("scope_issue"),
                "coverage": result.evidence_meta.get("coverage"), "expected_evidence": expected}

    def workload(self):
        output = []
        # A short history disables answer caching. BM25 and model weights remain warm.
        for mode in ("web", "mcp_search", "mcp_qa"):
            for workers in (1, 2, 4):
                rows = []
                batch_start = time.perf_counter()
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for _ in range(12 // workers):
                        start = Barrier(workers)

                        def run():
                            start.wait(timeout=10)
                            return self.request(mode, history=BASE_HISTORY)

                        rows.extend(future.result(timeout=60) for future in
                                    [pool.submit(run) for _ in range(workers)])
                output.append({"mode": mode, "concurrency": workers,
                               "summary": summarize(rows, ms(time.perf_counter() - batch_start)), "requests": rows})
        return output

    def scenarios(self):
        out = {}
        for key, question, doc_type, expected, permitted in (
            ("public", QUESTION, "巡检手册", CURRENT, (self.finance, self.develop)),
            ("internal", "内部差旅 FINA-119 的签批周期是多少？", "内部差旅", "AUDGOLD_FINANCE", (self.finance,)),
            ("restricted", "受限审批 SAFE-716 的审批人是谁？", "受限审批", "AUDGOLD_RESTRICTED", (self.develop,)),
            ("page", "故障切换 SLIDE-308 的操作窗口是什么？", "故障切换", "AUDGOLD_SLIDE", (self.finance, self.develop)),
        ):
            pairs = {}
            for user in (self.finance, self.develop):
                kwargs = dict(question=question, user=user, doc_type=doc_type,
                              expected=expected if user in permitted else None)
                web = self.request("web", **kwargs)
                mcp = self.request("mcp_search", **kwargs)
                pairs[user] = {"web": web, "mcp": mcp,
                               "same_evidence": doc_refs(web["sources"]) == doc_refs(mcp["tool_sources"])}
            out[key] = pairs
        page = out["page"][self.finance]
        out["page_preserved"] = (any(row["page"] == "2" for row in page["web"]["sources"])
                                 and any(row["page"] == "2" for row in page["mcp"]["tool_sources"]))
        old = [{"role": "user", "content": "先讨论星云项目。"},
               {"role": "assistant", "content": "好的，先看星云项目。"},
               {"role": "user", "content": "不，我说的是天枢项目。"}]
        kwargs = dict(question="这个项目的预算是多少？", doc_type="预算", history=old, expected="AUDGOLD_TIANSHU")
        out["followup"] = {"web": self.request("web", session_id="followup", state={}, **kwargs),
                           "mcp": self.request("mcp_search", **kwargs)}
        return out

    def revision_scenarios(self):
        latched, release, mcp_started = Event(), Event(), Event()
        original_upsert = self.store.upsert_chunks

        def staged(*args, **kwargs):
            accepted = original_upsert(*args, **kwargs)
            latched.set()
            if not release.wait(10):
                raise RuntimeError("fixture publication gate timed out")
            return accepted

        warm_web = self.request("web")
        warm_again = self.request("web")
        warm_mcp = self.request("mcp_search")
        self.path.write_text(f"# 巡检手册\n\nOPS-417 的恢复时限更新为 9 小时。验收事实 {NEW}。\n",
                             encoding="utf-8")
        web_kwargs = dict(top_k=4, filters={"doc_type": "巡检手册", "source": TITLE}, config=self.cfg,
                          user_groups=self.groups[self.finance], username=self.finance,
                          use_llm=False, use_rerank=False)
        with mock.patch.object(self.store, "upsert_chunks", side_effect=staged), ThreadPoolExecutor(2) as pool:
            publishing = pool.submit(self.ingest, [self.path])
            try:
                if not latched.wait(10):
                    raise RuntimeError("fixture failed to reach the publication gate")
                protected, _ = pipeline.answer_stream(QUESTION, **web_kwargs)

                def request_during_write():
                    mcp_started.set()
                    return self.request("mcp_search", expected=NEW)

                waiting_mcp = pool.submit(request_during_write)
                if not mcp_started.wait(3):
                    raise RuntimeError("fixture MCP worker did not start")
                time.sleep(.20)
                blocked_during_write = not waiting_mcp.done()
            finally:
                release.set()
            publishing.result(timeout=15)
            mcp_after_wait = waiting_mcp.result(timeout=30)
        after_web = self.request("web", expected=NEW)
        after_mcp = self.request("mcp_search", expected=NEW)
        old_doc = next(row["document_id"] for row in warm_web["sources"] if row["source"] == TITLE)
        history = json.loads(mcp_server._tool_get_document_impl(
            [old_doc], mode="full", version="1.0", status="expired", ctx=self.ctx(self.finance)))
        history_body = (history.get("documents") or [history])[0].get("full_text", "")

        # Pause consumption after the first authorized fragment, and revoke before later fragments.
        result, deltas = pipeline.answer_stream(
            "巡检手册 OPS-417 规定的恢复时限是多少？请核对。", filters={"doc_type": "巡检手册", "source": TITLE},
            config=self.cfg, user_groups=self.groups[self.finance], username=self.finance, use_rerank=False)
        first = next(deltas, "")
        rel = self.path.relative_to(self.knowledge).as_posix()
        manage.update_metadata(rel, {"allowed_groups": ["研发部"], "allowed_users": []})
        remaining = "".join(deltas)
        denied_web = self.request("web", expected=None, doc_type="巡检手册",
                                  question=f"请核对文件《{TITLE}》的 OPS-417 恢复时限。")
        denied_mcp = json.loads(mcp_server._tool_get_document_impl([old_doc], ctx=self.ctx(self.finance)))
        return {
            "warm_web": warm_web, "warm_cache_web": warm_again, "warm_mcp": warm_mcp,
            "during_publish_web_invalidated": protected.evidence_meta.get("scope_issue") == "index_changed",
            "during_publish_web_source_count": len(protected.sources),
            "mcp_waited_at_least_200ms": blocked_during_write, "mcp_during_publish": mcp_after_wait,
            "after_publish_web": after_web, "after_publish_mcp": after_mcp,
            "old_version_readable": CURRENT in history_body and NEW not in history_body,
            "revoke_mid_stream": {"first_had_prior_evidence": NEW in first, "later_nonempty": bool(remaining),
                                  "invalidated": result.evidence_meta.get("generation_status") == "invalidated"},
            "after_revoke_web_target_removed": not any(row["source"] == TITLE for row in denied_web["sources"]),
            "after_revoke_mcp_target_removed": denied_mcp.get("total") == 0,
        }

    def test_workflows(self):
        cold = self.request("web", history=BASE_HISTORY)
        workload = self.workload()
        scenarios = self.scenarios()
        revisions = self.revision_scenarios()
        output = {"case": ARGS.label, "documents": len({row["path"] for row in self.store.list_documents()}),
                  "published_cards": self.store.count(), "cold": cold, "workload": workload,
                  "scenarios": scenarios, "revisions": revisions}
        ARGS.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps([{"mode": row["mode"], "concurrency": row["concurrency"], **row["summary"]}
                          for row in workload], ensure_ascii=False))


if __name__ == "__main__":
    import unittest

    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, default=Path("models/bge-reranker-v2-m3-onnx").resolve())
    parser.add_argument("--output", type=Path, default=HERE / "current.json")
    parser.add_argument("--label", default="working-tree")
    ARGS = parser.parse_args()
    unittest.main(argv=[sys.argv[0], "-v"])

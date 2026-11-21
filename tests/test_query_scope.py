"""Spoken scope coverage through Web evidence and MCP, with isolated dependencies."""
import json
from dataclasses import replace
from unittest import mock

from src import auth_db, business_directions, mcp_server, pipeline, projects
from src.query_scope import extract_scope
from src.query_understanding import QueryUnderstanding, understand_query
from tests._helpers import IngestHarness


class QueryScopeTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.addCleanup(self.store.client.close)
        engineering = auth_db.create_department("研发部")["id"]
        auth_db.create_user("alice", "test-password", department_id=engineering)
        direction = business_directions.create_direction("低空经济")["id"]
        self.regulatory = projects.save_project(
            projects.ProjectFields("低空监管平台", direction, None, [], ""))["id"]
        warning = projects.save_project(
            projects.ProjectFields("低空预警平台", direction, None, [], ""))["id"]
        for name, project, department, doc_type, groups, users in (
            ("report.md", self.regulatory, "研发部", "验收报告", ["all"], []),
            ("plan.md", self.regulatory, "研发部", "验收方案", [], ["alice"]),
            ("private.md", self.regulatory, "研发部", "验收报告", ["finance"], []),
            ("purchase.md", self.regulatory, "采购部", "验收报告", ["all"], []),
            ("design.md", self.regulatory, "研发部", "技术方案", ["all"], []),
            ("warning.md", warning, "研发部", "验收报告", ["all"], []),
        ):
            file = self.write_note("低空监管平台验收标准和性能要求。", name)
            self.ingest([file], default_project_id=project, default_department=department,
                        default_doc_type=doc_type, default_groups=groups, default_allowed_users=users)

    def evidence(self, question, **kwargs):
        return pipeline.query_evidence(question, user_groups=["engineering"], username="alice",
                                       config=self.cfg, use_rerank=False, **kwargs)

    def test_web_catalog_intersects_all_four_dimensions_and_acl(self):
        question = "帮我查低空经济方向下低空监管平台项目中归属研发部的验收资料"
        result, meta = self.evidence(question, use_llm=False)
        self.assertEqual(meta["route"], "catalog")
        self.assertEqual(meta["metadata_scope"], {
            "business_direction": "低空经济", "project": "低空监管平台",
            "department": "研发部", "doc_type": "验收资料",
        })
        self.assertEqual({s["source"] for s in result.sources}, {"report.md", "plan.md"})

    def test_mcp_raw_question_and_exact_type_argument(self):
        with mock.patch.object(mcp_server, "_identity_from_ctx",
                               return_value=(["engineering"], "alice", {})):
            result = json.loads(mcp_server._tool_knowledge_search_impl(
                "查「低空监管平台」项目的验收资料", doc_type="验收报告"))
        self.assertEqual({s["source"] for s in result["matches"]}, {"report.md", "purchase.md"})
        self.assertEqual(result["filters_applied"]["project_id"], [self.regulatory])
        self.assertEqual(result["filters_applied"]["doc_type"], "验收报告")

    def test_relaxation_retains_spoken_scope_and_identity(self):
        question = "低空监管平台项目的验收报告里提到哪些标准？"
        qu = QueryUnderstanding(original=question, vector_query="性能要求", ok=True,
                                filters={"source": "missing.md"})
        with mock.patch("src.pipeline.understand_query", return_value=qu), \
             mock.patch("src.pipeline.get_llm_client"):
            result, meta = self.evidence(question)
        self.assertNotEqual(meta["route"], "catalog")
        self.assertEqual({s["source"] for s in result.sources}, {"report.md", "purchase.md"})
        self.assertEqual(result.filters["doc_type"], "验收报告")
        self.assertNotIn("source", result.filters)
        self.assertEqual(result.filters["__viewer__"]["username"], "alice")

    def test_unresolved_or_conflicting_scope_does_not_search(self):
        with mock.patch("src.pipeline.get_retriever") as retriever:
            unresolved, meta = self.evidence("查「不存在」项目的验收资料", use_llm=False)
            self.assertEqual(meta["scope_issue"], "unresolved")
            self.assertEqual(unresolved.retrieved, [])
            conflict, meta = self.evidence("查低空监管平台项目的验收资料", use_llm=False,
                                           filters={"doc_type": "技术方案"})
            self.assertEqual(meta["scope_issue"], "conflict")
            self.assertEqual(conflict.retrieved, [])
            retriever.assert_not_called()

    def test_registered_project_suffix_and_charter_scope_keep_acl(self):
        full = projects.save_project(projects.ProjectFields("天枢项目", None, None, [], ""))["id"]
        short = projects.save_project(projects.ProjectFields("天枢", None, None, [], ""))["id"]
        for name, project, groups in (
            ("charter.md", full, ["all"]), ("restricted.md", full, ["finance"]),
            ("short.md", short, ["all"]), ("unlinked.md", None, ["all"]),
        ):
            file = self.write_note("天枢项目的章程记录了项目预算 200 万元、负责人周红和验收里程碑。", name)
            self.ingest([file], default_project_id=project, default_doc_type="项目章程", default_groups=groups)

        question = "天枢项目章程里列了哪些里程碑？"
        qu = QueryUnderstanding(original=question, vector_query=question, ok=True,
                                metadata_scope={"project": "天枢", "doc_type": "章程"})
        with mock.patch("src.pipeline.understand_query", return_value=qu), \
             mock.patch("src.pipeline.get_llm_client"):
            result, _ = self.evidence(question)
        self.assertEqual(result.filters["project_id"], [full])
        self.assertEqual({s["source"] for s in result.sources}, {"charter.md"})
        with mock.patch.object(mcp_server, "_identity_from_ctx",
                               return_value=(["engineering"], "alice", {})):
            mcp = json.loads(mcp_server._tool_knowledge_search_impl("天枢项目目前预算是多少？"))
        self.assertEqual(mcp["filters_applied"]["project_id"], [full])
        self.assertEqual({s["source"] for s in mcp["matches"]}, {"charter.md"})

        quoted, _ = self.evidence("查「天枢」项目的预算", use_llm=False)
        self.assertEqual(quoted.filters["project_id"], [short])
        conflict, meta = self.evidence(question, use_llm=False, filters={"project_id": [short]})
        self.assertEqual(meta["scope_issue"], "conflict")
        self.assertFalse(conflict.sources)

    def test_natural_project_alias_accepts_only_a_unique_variant(self):
        full = projects.save_project(projects.ProjectFields("天枢项目", None, None, [], ""))["id"]
        result, _ = self.evidence("查「天枢」项目的预算", use_llm=False)
        self.assertEqual(result.filters["project_id"], [full])
        self.assertFalse(result.sources)  # A master record does not link any existing documents.
        followed, _ = self.evidence("那这个项目的预算呢？", use_llm=False, history=[
            {"role": "user", "content": "查「天枢」项目的负责人"}])
        self.assertEqual(followed.filters["project_id"], [full])
        with self.assertRaises(ValueError):
            projects.resolve_project_filter("天枢", None)
        projects.save_project(projects.ProjectFields("天枢專案", None, None, [], ""))
        with mock.patch("src.pipeline.get_retriever") as retrieve:
            result, meta = self.evidence("查「天枢」项目的预算", use_llm=False)
            retrieve.assert_not_called()
        self.assertEqual(meta["scope_issue"], "unresolved")
        self.assertFalse(result.sources)

    def test_project_reference_uses_selected_project_and_keeps_acl(self):
        for phrase in ("这个项目", "该项目", "本项目", "在所选项目", "请问当前项目"):
            with self.subTest(phrase=phrase):
                query = f"{phrase}的验收材料有哪些？"
                result, meta = self.evidence(query, use_llm=False, filters={"project_id": [self.regulatory]})
                self.assertEqual(meta["route"], "catalog")
                self.assertEqual({row["source"] for row in result.sources}, {"report.md", "plan.md", "purchase.md"})
        with mock.patch.object(mcp_server, "_identity_from_ctx", return_value=(["engineering"], "guest", {})):
            result = json.loads(mcp_server._tool_knowledge_search_impl(
                "这个项目的验收材料有哪些？", project="低空监管平台"))
        self.assertEqual({row["source"] for row in result["matches"]}, {"report.md", "purchase.md"})

    def test_unbound_project_reference_requests_clarification(self):
        with mock.patch("src.pipeline.get_retriever") as retrieve:
            result, meta = self.evidence("查这个项目的验收资料", use_llm=False)
        self.assertEqual(meta["scope_issue"], "project_reference")
        self.assertEqual(result.sources, [])
        retrieve.assert_not_called()
        self.assertEqual(extract_scope("「这个」项目的验收资料")["project"], "这个")
        self.assertNotIn("project", extract_scope("本项目的验收资料", {"project": "本项目"}))

    def test_project_reference_from_user_history_in_web_and_mcp(self):
        history = [{"role": "user", "content": "低空监管平台有哪些验收资料？"},
                   {"role": "assistant", "content": "建议看看低空预警平台。"}]
        question = "那这个项目的验收资料有哪些？"
        with mock.patch("src.pipeline.get_llm_client", side_effect=AssertionError("额外调用模型")):
            result, meta = self.evidence(question, history=history, use_llm=False)
            self.assertEqual(result.filters["project_id"], [self.regulatory])
            self.assertEqual(meta["metadata_scope"]["project"], "低空监管平台")
            with mock.patch.object(mcp_server, "_identity_from_ctx", return_value=(["engineering"], "alice", {})), \
                 mock.patch("src.config.get_config", return_value=replace(self.cfg, query_understanding=False)):
                search = json.loads(mcp_server._tool_knowledge_search_impl(question, history=history))
                qa = json.loads(mcp_server._tool_knowledge_qa_impl(question, history=history))
        self.assertEqual(search["filters_applied"]["project_id"], [self.regulatory])
        self.assertEqual({item["source"] for item in search["matches"]}, {"report.md", "purchase.md", "plan.md"})
        self.assertEqual({item["source"] for item in qa["sources"]}, {"report.md", "purchase.md", "plan.md"})

    def test_latest_project_switch_and_multi_project_ambiguity(self):
        history = [{"role": "user", "content": "先看低空监管平台"},
                   {"role": "user", "content": "再对比低空监管平台和低空预警平台"}]
        question = "查该项目的验收资料"
        with mock.patch("src.pipeline.get_retriever") as retrieve:
            result, meta = self.evidence(question, history=history, use_llm=False)
        self.assertEqual(meta["scope_issue"], "project_reference_ambiguous")
        self.assertFalse(result.sources)
        retrieve.assert_not_called()
        history.append({"role": "user", "content": "现在只看低空预警平台"})
        result, _ = self.evidence(question, history=history, use_llm=False)
        self.assertEqual(result.filters["project_id"], projects.resolve_project_filter("低空预警平台", None))
        result, _ = self.evidence(question, history=history, use_llm=False, filters={"project_id": [self.regulatory]})
        self.assertEqual(result.filters["project_id"], [self.regulatory])
        result, _ = self.evidence("查验收资料", history=history, use_llm=False)
        self.assertNotIn("project_id", result.filters)

    def test_conversation_project_survives_compaction_and_rename_with_current_acl(self):
        from src.manage import update_metadata

        first = "低空监管平台有哪些验收资料？"
        cfg = replace(self.cfg, chat_recent_turns=1, chat_summary_trigger_tokens=1)
        state = {}

        def answer(question, history, session="session-a"):
            return pipeline.answer_question(question, history=history, session_id=session, conversation_state=state,
                                            config=cfg, use_llm=False, use_rerank=False,
                                            user_groups=["engineering"], username="alice")

        answer(first, [])
        self.assertEqual(state["project_reference"]["project_id"], self.regulatory)
        self.assertEqual(state["project_reference"]["turn"], 0)
        history = [{"role": "user", "content": first},
                   {"role": "user", "content": "请保持简洁。" * 10},
                   {"role": "user", "content": "可以按表格展示。"}]
        rename = projects.get_project(self.regulatory)
        projects.save_project(projects.ProjectFields("低空治理平台", rename["direction_id"], None, [], ""),
                              project_id=rename["id"], revision=rename["revision"])
        followup = "查这个项目的验收方案"
        result = answer(followup, history)
        self.assertGreater(state["covered"], 0)
        self.assertEqual(state["project_reference"]["name"], "低空治理平台")
        self.assertEqual({source["source"] for source in result.sources}, {"plan.md"})

        update_metadata("learning/general/plan.md", {"allowed_groups": [], "allowed_users": []})
        revoked = answer(followup, history)
        self.assertEqual(revoked.filters["project_id"], [self.regulatory])
        self.assertFalse(revoked.sources)
        with mock.patch.object(mcp_server, "_identity_from_ctx", return_value=(["engineering"], "alice", {})):
            search = json.loads(mcp_server._tool_knowledge_search_impl(
                followup, history=[{"role": "user", "content": "查低空治理平台"}]))
        self.assertEqual(search["filters_applied"]["project_id"], [self.regulatory])
        self.assertFalse(search["matches"])
        new_session = answer(followup, [], session="session-b")
        self.assertEqual(new_session.evidence_meta["scope_issue"], "project_reference")

    def test_literal_model_values_and_content_mentions(self):
        self.assertEqual(extract_scope("这份文档有哪些项目"), {})
        self.assertEqual(extract_scope("哪些资料提到了低空监管平台项目？", {
            "project": "低空监管平台", "allowed_groups": "all",
        }), {})
        self.assertEqual(extract_scope("请找交付核对表", {
            "doc_type": "交付核对表", "department": "模型猜的部门",
        }), {"doc_type": "交付核对表"})
        for question, suggested in (
            ("查找正文提到归属研发部的文件", {"department": "研发部"}),
            ("找正文提到文档类型为验收报告的文件", {"doc_type": "验收报告"}),
            ("哪些文件引用了低空监管平台", {"project": "低空监管平台"}),
        ):
            with self.subTest(question=question):
                self.assertEqual(extract_scope(question, suggested), {})
        self.assertEqual(extract_scope("查正文提到研发部的文件，文档类型为验收报告"),
                         {"doc_type": "验收报告"})

    def test_negative_and_multiple_values_prompt_without_retrieving(self):
        cases = {
            "列出不归属研发部的文件": "negated",
            "不要查低空监管平台项目的验收报告": "negated",
            "查「低空监管平台」项目或「低空预警平台」项目的验收报告": "multiple",
            "查「低空监管平台」项目、「低空预警平台」项目的验收报告": "multiple",
            "查「低空监管平台」项目，查「低空预警平台」项目的验收报告": "multiple",
            "查低空监管平台或低空预警平台项目的验收报告": "multiple",
            "查归属研发部和采购部的验收报告": "multiple",
            "查低空监管平台项目的技术方案和验收报告": "multiple",
        }
        with mock.patch("src.pipeline.get_retriever") as retriever:
            for question, issue in cases.items():
                with self.subTest(question=question):
                    result, meta = self.evidence(question, use_llm=False)
                    self.assertEqual(meta["scope_issue"], issue)
                    self.assertTrue(meta["scope_notice"])
                    self.assertFalse(result.retrieved)
            retriever.assert_not_called()

    def test_invalid_model_fields_keep_valid_understanding(self):
        for raw_filters, time_range in (
            ({"source": {"name": "note.md"}}, {"from": "2026-02-30", "to": "2026-03-01"}),
            (["source"], {"from": "2026-03-01", "to": "2026-02-28"}),
        ):
            with self.subTest(time_range=time_range):
                llm = mock.Mock()
                llm.chat.return_value = (json.dumps({
                    "vector_query": "性能要求", "filters": raw_filters, "time_range": time_range,
                    "metadata_scope": {"project": "低空监管平台"},
                }), 0.0)
                qu = understand_query("低空监管平台的性能要求", llm=llm, config=self.cfg)
                self.assertTrue(qu.ok)
                self.assertEqual(qu.vector_query, "性能要求")
                self.assertEqual(qu.metadata_scope, {"project": "低空监管平台"})
                self.assertEqual(qu.filters, {})
                self.assertEqual(qu.time_range, {})
                llm.chat.assert_called_once()

    def test_date_survives_retry_with_project_type_and_permissions(self):
        question = "查低空监管平台项目的验收报告，找 2020 年修改的性能要求"
        dates = {"from": "2020-01-01", "to": "2020-12-31"}
        qu = QueryUnderstanding(original=question, vector_query="性能要求", ok=True,
                                metadata_scope={"doc_type": "验收报告"},
                                time_range=dates, filters={"source": "missing.md"})
        with mock.patch("src.pipeline.understand_query", return_value=qu), \
             mock.patch("src.pipeline.get_llm_client"), \
             mock.patch("src.pipeline.get_retriever") as get_retriever:
            retrieve = get_retriever.return_value.retrieve
            retrieve.return_value = []
            result, _ = self.evidence(question)
        first, retry = (call.kwargs["filters"] for call in retrieve.call_args_list)
        self.assertEqual(first, {**retry, "source": "missing.md"})
        self.assertEqual(retry["updated_at"], dates)
        self.assertEqual(retry["project_id"], [self.regulatory])
        self.assertEqual(retry["doc_type"], "验收报告")
        self.assertEqual(retry["__viewer__"]["username"], "alice")
        self.assertEqual(result.filters["updated_at"], dates)

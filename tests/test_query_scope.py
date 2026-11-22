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

    def test_unresolved_scope_falls_back_but_conflict_does_not_search(self):
        with mock.patch("src.pipeline.get_retriever") as retriever:
            retriever.return_value.retrieve.return_value = []
            unresolved, meta = self.evidence("查「不存在」项目的验收资料", use_llm=False)
            self.assertEqual(meta["scope_fallback"]["issue"], "unresolved")
            self.assertNotIn("scope_issue", meta)
            self.assertEqual(unresolved.retrieved, [])
            conflict, meta = self.evidence("查低空监管平台项目的验收资料", use_llm=False,
                                           filters={"doc_type": "技术方案"})
            self.assertEqual(meta["scope_issue"], "conflict")
            self.assertEqual(conflict.retrieved, [])
            # 仅回退的那一次检索；conflict 不触发检索
            retriever.return_value.retrieve.assert_called_once()

    def test_project_scope_relaxes_when_linked_docs_miss_the_fact(self):
        """R6-M09：项目范围 0 召回的两级放宽。

        项目主档存在且库中有关联文档，但目标事实在未挂项目的跨项目文档
        （全公司台账）——二级去掉范围键重试并给出 relaxed_empty 提示；
        主档无任何关联文档时不放宽（诚实空）。
        """
        ledger = self.write_note("凤凰项目 M1 2026-04-30 M2 2026-06-30 M3 2026-09-15。",
                                 "ledger.md", "reference/general")
        self.ingest([ledger])  # 台账不挂项目
        phoenix = projects.save_project(projects.ProjectFields("凤凰项目", None, None, [], ""))["id"]
        charter = self.write_note("凤凰项目章程正文。", "charter.md")
        self.ingest([charter], default_project_id=phoenix)

        # 一级（保留 project_id）召回章程卡但问题问里程碑 → 模拟 0 召回：
        # 用 mock：首次（含范围）返回空，二级（无范围）返回台账卡。
        from src.retriever import RetrievedItem
        def fake_retrieve(query, top_k=None, filters=None, **kwargs):
            if filters and filters.get("project_id"):
                return []
            return [RetrievedItem(rank=1, score=0.5, text="凤凰项目 M1 2026-04-30。",
                                  metadata={"chunk_id": "c1", "source": "ledger.md",
                                            "document_id": "d_ledger", "version": "1.0"})]
        with mock.patch("src.pipeline.get_retriever") as retriever:
            retriever.return_value.retrieve.side_effect = fake_retrieve
            result, meta = self.evidence("凤凰项目的三个里程碑分别计划什么时候完成？", use_llm=False)
        self.assertEqual(meta["scope_fallback"]["issue"], "relaxed_empty")
        # mock 返回的伪造卡与真实库台账卡 chunk_id 不同，但语义同源：按集合断言
        self.assertEqual({s["source"] for s in result.sources}, {"ledger.md"})
        self.assertNotIn("project_id", result.filters)

    def test_comparison_with_unregistered_project_falls_back(self):
        """R6-C02：比较句式含未登记项目名 → 回退普通检索而非拦截。"""
        projects.save_project(projects.ProjectFields("天枢项目", None, None, [], ""))
        with mock.patch("src.pipeline.get_retriever") as retriever:
            retriever.return_value.retrieve.return_value = []
            result, meta = self.evidence(
                "天枢项目、不存在的项目、星云二期三个项目里预算最高的是哪个？", use_llm=False)
        self.assertNotEqual(result.answer, "无法确认所有要比较的项目，请核对项目名称。")
        self.assertNotIn("scope_notice", meta)  # 后续链路接管（multiple 回退或普通检索）
        retriever.return_value.retrieve.assert_called()

    def test_document_route_relaxes_inferred_source_with_notice(self):
        """R6-M09 残余：document 路由 QU 推断 source 0 召回 → 放宽重试 + 提示行。

        手动/MCP 指定的 source（在 filters 里）不放宽——与"手动设置不放宽"一致。
        """
        from src.retriever import RetrievedItem
        question = "三个里程碑的完成日期是什么时候？"
        qu = QueryUnderstanding(original=question, vector_query=question, ok=True,
                                enumeration=True,
                                filters={"source": "不存在的里程碑文档.md"})
        target = RetrievedItem(rank=1, score=0.5, text="凤凰项目 M1 2026-04-30。",
                               metadata={"chunk_id": "c1", "source": "ledger.md",
                                         "document_id": "d_ledger", "version": "1.0"})

        def fake_retrieve(query, top_k=None, filters=None, **kwargs):
            return [] if (filters or {}).get("source") else [target]
        with mock.patch("src.pipeline.understand_query", return_value=qu), \
             mock.patch("src.pipeline.get_llm_client"), \
             mock.patch("src.pipeline.get_retriever") as retriever:
            retriever.return_value.retrieve.side_effect = fake_retrieve
            result, meta = self.evidence(question)
        self.assertEqual(meta["scope_fallback"]["issue"], "source_relaxed")
        self.assertEqual([s["source"] for s in result.sources], ["ledger.md"])
        # 手动 source（filters 传入）0 召回不放宽：只检索一次、诚实空
        with mock.patch("src.pipeline.get_retriever") as retriever:
            retriever.return_value.retrieve.return_value = []
            result, meta = self.evidence("查凤凰项目的里程碑", use_llm=False,
                                         filters={"source": "不存在.md"})
        retriever.return_value.retrieve.assert_called_once()
        self.assertEqual(result.retrieved, [])

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
        # 方案 A（项目语义补全）：挂接的 charter.md + 正文提及天枢的 unlinked.md
        self.assertEqual({s["source"] for s in result.sources}, {"charter.md", "unlinked.md"})
        with mock.patch.object(mcp_server, "_identity_from_ctx",
                               return_value=(["engineering"], "alice", {})):
            mcp = json.loads(mcp_server._tool_knowledge_search_impl("天枢项目目前预算是多少？"))
        self.assertEqual(mcp["filters_applied"]["project_id"], [full])
        self.assertEqual({s["source"] for s in mcp["matches"]}, {"charter.md", "unlinked.md"})

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
        # 多个主档匹配「天枢」变体 → unresolved：回退全库语义检索（带 scope_fallback 标记），
        # 不再拦截；retrieve 必须被调用且不携带任何 project_id 过滤。
        with mock.patch("src.pipeline.get_retriever") as retrieve:
            retrieve.return_value.retrieve.return_value = []
            result, meta = self.evidence("查「天枢」项目的预算", use_llm=False)
            retrieve.return_value.retrieve.assert_called_once()
            self.assertNotIn("project_id", retrieve.return_value.retrieve.call_args.kwargs["filters"])
        self.assertEqual(meta["scope_fallback"]["issue"], "unresolved")
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

    def test_explicit_project_switch_and_correction_update_shared_scope(self):
        histories = (
            "现在只看低空预警平台项目的里程碑。",
            "我刚才说的是低空监管平台项目，现在只查低空预警平台项目。",
        )
        target = projects.resolve_project_filter("低空预警平台", None)
        for latest in histories:
            with self.subTest(latest=latest):
                history = [{"role": "user", "content": "先看低空监管平台项目。"},
                           {"role": "user", "content": latest}]
                result, meta = self.evidence("它的验收资料有哪些？", history=history, use_llm=False)
                self.assertEqual(result.filters["project_id"], target)
                self.assertNotIn("scope_issue", meta)
                self.assertEqual({s["source"] for s in result.sources}, {"warning.md"})
        result, meta = self.evidence("我之前说的是低空监管平台项目，现在只查低空预警平台项目的验收资料",
                                     use_llm=False)
        self.assertEqual(result.filters["project_id"], target)
        self.assertNotIn("scope_issue", meta)
        ambiguous = [{"role": "user", "content": "现在只看低空监管平台和低空预警平台两个项目。"}]
        _, meta = self.evidence("它的验收资料有哪些？", history=ambiguous, use_llm=False)
        self.assertEqual(meta["scope_issue"], "project_reference_ambiguous")

    def test_bare_project_followup_shares_acl_and_stops_after_topic_change(self):
        question = "它的验收资料有哪些？"
        history = [{"role": "user", "content": "低空监管平台的里程碑有哪些？"},
                   {"role": "user", "content": "用中文回答。"}]
        with mock.patch("src.pipeline.get_llm_client", side_effect=AssertionError("额外调用模型")):
            result, _ = self.evidence(question, history=history, use_llm=False)
            self.assertEqual(result.filters["project_id"], [self.regulatory])
            with mock.patch.object(mcp_server, "_identity_from_ctx", return_value=(["engineering"], "alice", {})), \
                 mock.patch("src.config.get_config", return_value=replace(self.cfg, query_understanding=False)):
                search = json.loads(mcp_server._tool_knowledge_search_impl(question, history=history))
        self.assertEqual(search["filters_applied"]["project_id"], [self.regulatory])
        self.assertEqual({item["source"] for item in search["matches"]}, {"report.md", "purchase.md", "plan.md"})

        for changed, issue in (
            ("换个话题，X2000 设备说明书讲了什么？", "project_reference"),
            ("低空监管平台里，这份文件是哪个版本？", "project_reference"),
            ("比较低空监管平台和低空预警平台", "project_reference_ambiguous"),
        ):
            with self.subTest(changed=changed), mock.patch("src.pipeline.get_retriever") as retrieve:
                result, meta = self.evidence(question, history=[*history, {"role": "user", "content": changed}],
                                             use_llm=False)
                self.assertFalse(result.sources)
                self.assertEqual(meta["scope_issue"], issue)
                retrieve.assert_not_called()
        result, _ = self.evidence("它的价格是多少？", history=history, use_llm=False)
        self.assertNotIn("project_id", result.filters)

    def test_bare_project_followup_resolves_before_single_query_understanding(self):
        question = "它的立项预算是多少万元？"
        history = [{"role": "user", "content": "低空监管平台的里程碑有哪些？"}]
        qu = QueryUnderstanding(question, ok=True, vector_query="立项预算",
                                filters={"source": "wrong.md", "domain": "archive"})
        with mock.patch("src.pipeline.get_llm_client"), \
             mock.patch("src.pipeline.understand_query", return_value=qu) as understand:
            result, _ = self.evidence(question, history=history)
        understand.assert_called_once()
        self.assertEqual(understand.call_args.args[0], "低空监管平台的立项预算是多少万元？")
        self.assertEqual(result.filters["project_id"], [self.regulatory])
        self.assertNotIn("source", result.filters)
        self.assertNotIn("domain", result.filters)

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
        bare = answer("它的验收方案有哪些？", history)
        self.assertEqual(bare.filters["project_id"], [self.regulatory])
        self.assertEqual({source["source"] for source in bare.sources}, {"plan.md"})

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

    def test_negated_scope_prompts_and_multiple_values_fall_back(self):
        negated = {
            "列出不归属研发部的文件": "negated",
            "不要查低空监管平台项目的验收报告": "negated",
        }
        with mock.patch("src.pipeline.get_retriever") as retriever:
            for question, issue in negated.items():
                with self.subTest(question=question):
                    result, meta = self.evidence(question, use_llm=False)
                    self.assertEqual(meta["scope_issue"], issue)
                    self.assertTrue(meta["scope_notice"])
                    self.assertFalse(result.retrieved)
            retriever.assert_not_called()
        multiple = {
            "查「低空监管平台」项目或「低空预警平台」项目的验收报告",
            "查「低空监管平台」项目、「低空预警平台」项目的验收报告",
            "查「低空监管平台」项目，查「低空预警平台」项目的验收报告",
            "查低空监管平台或低空预警平台项目的验收报告",
            "查归属研发部和采购部的验收报告",
            "查低空监管平台项目的技术方案和验收报告",
        }
        for question in multiple:
            with self.subTest(question=question), mock.patch("src.pipeline.get_retriever") as retriever:
                retriever.return_value.retrieve.return_value = []
                result, meta = self.evidence(question, use_llm=False)
                self.assertEqual(meta["scope_fallback"]["issue"], "multiple")
                self.assertNotIn("scope_issue", meta)
                retriever.return_value.retrieve.assert_called_once()

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
        # 两级放宽（R6-M09）：一级去 LLM filters 保留口头范围（既有规则）；
        # 仍 0 召回且项目在库中确有关联文档时，二级去范围键重试。
        # 日期与权限身份在全部重试中始终保留。
        calls = [call.kwargs["filters"] for call in retrieve.call_args_list]
        self.assertEqual(len(calls), 3)
        first, level_one, level_two = calls
        self.assertEqual(first, {**level_one, "source": "missing.md"})
        # 一级：保留日期 + 项目 + 类型 + 权限
        self.assertEqual(level_one["updated_at"], dates)
        self.assertEqual(level_one["project_id"], [self.regulatory])
        self.assertEqual(level_one["doc_type"], "验收报告")
        self.assertEqual(level_one["__viewer__"]["username"], "alice")
        # 二级：范围键（project_id/doc_type）被剥离，日期与权限保留
        self.assertNotIn("project_id", level_two)
        self.assertNotIn("doc_type", level_two)
        self.assertEqual(level_two["updated_at"], dates)
        self.assertEqual(level_two["__viewer__"]["username"], "alice")
        self.assertEqual(result.filters["updated_at"], dates)

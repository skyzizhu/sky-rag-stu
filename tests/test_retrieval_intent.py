"""Check explicit file scopes, time intent and version evidence through normal retrieval."""

import json
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from src import mcp_server, pipeline
from src.mcp_search_limits import MCP_SEARCH_MAX_TOP_K
from src.prompt import build_messages
from src.query_scope import extract_scope
from src.query_understanding import QueryUnderstanding
from src.retrieval_intent import compares_versions, enumerated_sub_queries, explicit_file_dates
from src.retriever import RetrievedItem
from tests._helpers import IngestHarness


COMPARE = "现在的差旅住宿报销标准比 2022 年旧版员工手册的标准涨了多少？"


class RetrievalIntentTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.addCleanup(self.store.client.close)

    def evidence(self, question, *, groups=("finance",), **kwargs):
        return pipeline.query_evidence(question, config=self.cfg, use_rerank=False,
                                       user_groups=list(groups), username="alice", **kwargs)

    def test_department_and_type_are_file_filters_only_when_supported_by_wording(self):
        path = self.write_note("研发部 2024 年度预算为 520 万元。", "budget.md")
        self.ingest([path], default_department="财务部", default_doc_type="预算明细",
                    default_groups=["finance"])
        question = "研发部 2024 年度的预算是多少？"
        qu = QueryUnderstanding(question, vector_query=question, ok=True,
                                metadata_scope={"department": "研发部", "doc_type": "预算"},
                                time_range={"from": "2024-01-01", "to": "2024-12-31"})
        with patch("src.pipeline.understand_query", return_value=qu), patch("src.pipeline.get_llm_client"):
            result, _ = self.evidence(question)
            masked, _ = self.evidence(question, groups=("guest",))
        self.assertIn("budget.md", {s["source"] for s in result.sources})
        self.assertFalse({"department", "doc_type", "updated_at"}.intersection(result.filters))
        self.assertFalse(masked.sources)
        self.assertEqual(extract_scope("查文件归属部门为财务部的预算",
                                       {"department": "研发部", "doc_type": "预算"}), {"department": "财务部"})
        self.assertEqual(extract_scope("按公司制度，普通员工的年终奖是多少？", {"doc_type": "制度"}), {})
        self.assertEqual(extract_scope("发布会纪要里提到的合作媒体有哪几家？", {"doc_type": "发布会纪要"}), {})
        self.assertEqual(extract_scope("文档类型为发布会纪要", {"doc_type": "纪要"}), {"doc_type": "发布会纪要"})

    def test_explicit_file_dates_have_their_own_field_and_never_override_manual_dates(self):
        today = date(2026, 11, 21)
        self.assertEqual(explicit_file_dates("查去年修改过的 2024 年度预算", today=today),
                         {"updated_at": {"from": "2025-01-01", "to": "2025-12-31"}})
        self.assertEqual(explicit_file_dates("查询入库日期为 2024年2月的支出预算", today=today),
                         {"indexed_at": {"from": "2024-02-01", "to": "2024-02-29"}})
        self.assertEqual(explicit_file_dates("查 2024-01-01 至 2024-01-05 更新的资料"),
                         {"updated_at": {"from": "2024-01-01", "to": "2024-01-05"}})
        self.assertEqual(explicit_file_dates("2024 年更新了哪些报销标准？"), {})
        self.assertEqual(explicit_file_dates("查 2024 年公司举办的活动"), {})
        self.assertEqual(explicit_file_dates("查修改时间为 2024年2月30日的资料"), {})
        self.assertEqual(explicit_file_dates("对比 2024 年修改的制度和 2025 年修改的制度"), {})
        manual = {"from": "2025-01-01", "to": "2025-12-31"}
        with patch("src.pipeline.get_retriever") as retriever:
            retriever.return_value.retrieve.return_value = []
            _, _ = self.evidence("查 2024 年修改的预算", filters={"updated_at": manual}, use_llm=False)
        self.assertEqual(retriever.return_value.retrieve.call_args.kwargs["filters"]["updated_at"], manual)

        self.ingest([self.write_note("项目预算 520 万元。", "today.md")])
        result, _ = self.evidence("今天入库的项目预算", use_llm=False)
        self.assertEqual(result.filters["indexed_at"], {"from": date.today().isoformat(), "to": date.today().isoformat()})
        self.assertEqual({s["source"] for s in result.sources}, {"today.md"})


    def test_year_version_counts_as_current_side_reference(self):
        """年份版本引用（"2025 版"）视为现行侧指称；历史侧仍需旧版/历史字样。

        病灶（实测 C15）：「2025 版…比 2022 年旧版手册涨了多少」有明确历史侧
        （旧版），却因缺"现在/最新"不触发版本对比通道。两个现行版的并列对比
        （如 2025 版 vs 2024 版）没有历史侧字样，仍不走该通道。
        """
        self.assertTrue(compares_versions("2025 版员工手册的住宿标准比 2022 年旧版手册涨了多少？"))
        self.assertTrue(compares_versions("2025版方案与旧版方案相比变化在哪里"))
        # 两个现行版并列：无历史侧指称，不触发（保持既有路由）
        self.assertFalse(compares_versions("2025 版员工手册和 2024 版手册的一线城市住宿标准分别是多少钱？发生了什么变化？"))
        self.assertFalse(compares_versions("2024 年度和 2025 年度的公司预算总额分别是多少？"))

    def test_enumerated_sub_queries_split_parallel_entities(self):
        """确定性拆分兜底：「A、B、C（三份…）的 X 分别…」逐实体生成子查询。"""
        self.assertEqual(
            enumerated_sub_queries("2025 版员工手册、信息安全管理制度、招标规格书三份文档的密码要求分别是多少？是否完全一致？"),
            ["2025 版员工手册 密码要求", "信息安全管理制度 密码要求", "招标规格书 密码要求"])
        self.assertEqual(
            enumerated_sub_queries("研发部和财务部的预算分别是多少万元？"),
            ["研发部 预算", "财务部 预算"])
        # 保守边界：无"分别/各自"、无"的+字段"结构、单实体——一律不拆
        self.assertEqual(enumerated_sub_queries("员工手册里试用期和年假是怎么规定的"), [])
        self.assertEqual(enumerated_sub_queries("X-1000 和 X-2000 的电池续航分别是多少？"),
                         ["X-1000 电池续航", "X-2000 电池续航"])
        self.assertEqual(enumerated_sub_queries("报销流程有哪些步骤"), [])

    def test_version_comparison_reserves_both_sides_and_keeps_citations_distinct(self):
        self.assertTrue(compares_versions("现在的住宿标准与《2022 年旧版员工手册》相比有什么变化？"))
        self.assertFalse(compares_versions("查《新旧版对比报告》的附件"))
        path = self.write_note("现行差旅住宿报销标准：一线城市每晚 400 元。", "current.md")
        old = self.write_note("2022 年旧版员工手册，差旅住宿报销标准：每晚 350 元。", "old.md", "archive/learning/general")
        private = self.write_note("2022 年旧版员工手册，差旅报销标准每晚 999 元。", "private.md", "archive/learning/general")
        self.ingest([path, old])
        self.ingest([private], default_groups=["leadership"])
        qu = QueryUnderstanding(COMPARE, vector_query=COMPARE, ok=True, enumeration=True,
                                filters={"status": "archive", "source": "old.md"},
                                time_range={"from": "2022-01-01", "to": "2022-12-31"})
        with patch("src.pipeline.understand_query", return_value=qu), patch("src.pipeline.get_llm_client"):
            result, meta = self.evidence(COMPARE, top_k=1)
        self.assertEqual(meta["route"], "version_comparison")
        self.assertEqual(meta["version_comparison"]["missing_sides"], [])
        self.assertFalse(result.enumeration)
        self.assertEqual([(s["rank"], s["source"], s["status"]) for s in result.sources],
                         [(1, "current.md", "active"), (2, "old.md", "archive")])
        self.assertIn("【现行版本】", result.context)
        self.assertIn("【历史版本】", result.context)
        self.assertNotIn("999", result.context)
        self.assertIsNone(result.answer)
        self.assertIn("为差异引用两侧来源", build_messages(result.context, COMPARE, evidence_meta=meta)[0]["content"])

        limited, meta = self.evidence(COMPARE, filters={"status": "archive"}, use_llm=False)
        self.assertEqual(meta["coverage"], "partial")
        self.assertEqual(meta["version_comparison"]["missing_sides"], ["current"])
        self.assertEqual([s["source"] for s in limited.sources], ["old.md"])
        self.assertIn("无法确认新旧版本", limited.answer)
        normal, _ = self.evidence("差旅住宿报销标准", use_llm=False)
        self.assertEqual([s["source"] for s in normal.sources], ["current.md"])

    def test_historical_side_includes_expired_revision_and_mcp_uses_same_scope(self):
        path = self.write_note("旧版手册：差旅住宿标准每晚 350 元。", "handbook.md")
        self.ingest([path])
        path.write_text("现行手册：差旅住宿标准一线城市每晚 400 元。", encoding="utf-8")
        self.ingest([path])
        with patch.object(mcp_server, "_identity_from_ctx", return_value=(["finance"], "alice", {})):
            reply = json.loads(mcp_server._tool_knowledge_search_impl(COMPARE, top_k=1))
        self.assertEqual(reply["coverage_details"]["route"], "version_comparison")
        self.assertEqual([m["status"] for m in reply["matches"]], ["active", "expired"])
        self.assertEqual(len({m["version"] for m in reply["matches"]}), 2)

    def test_version_fact_is_checked_and_expanded_only_on_its_side(self):
        question = "对比新旧版本的预算分别是多少？"
        fixture = [("active", "现行版本 DPC-551 的预算为 300 万元。"),
                   ("active", "现行版实施说明。"), ("active", "现行版交接记录。"),
                   ("expired", "历史版本实施说明。"), ("expired", "历史版本交接记录。"),
                   ("expired", "历史版本 DPC-550 的预算为 200 万元。")]
        candidates = [RetrievedItem(i, 1 / i, text, {"document_id": str(i), "chunk_id": f"d{i}_0000",
                                    "source": f"d{i}.md", "status": status, "allowed_groups": ["all"],
                                    "version": "1.0" if status == "active" else "0.9"}, "关键词")
                      for i, (status, text) in enumerate(fixture, 1)]

        def retrieve(_question, *, top_k, filters, **kwargs):
            return [replace(item) for item in candidates if item.metadata["status"] in filters["status"]][:top_k]

        with patch("src.pipeline.get_retriever", return_value=SimpleNamespace(retrieve=retrieve)):
            web, meta = self.evidence(question, use_query_understanding=False)
            fixed = json.loads(mcp_server._tool_knowledge_search_impl(question, top_k=5))
            auto = json.loads(mcp_server._tool_knowledge_search_impl(question))
            _, distinct_ids = self.evidence("对比新旧版本 DPC-550 和 DPC-551 的预算分别是多少？",
                                           top_k=12, use_query_understanding=False)
            id_question = "对比新旧版本中 DPC-550 和 DPC-551 的实施要求"
            id_fixed = json.loads(mcp_server._tool_knowledge_search_impl(id_question, top_k=5))
            id_auto = json.loads(mcp_server._tool_knowledge_search_impl(id_question))

        self.assertEqual(meta["evidence_support"]["status"], "partial")
        self.assertEqual(meta["evidence_support"]["by_side"]["current"]["status"], "supported")
        self.assertEqual(meta["version_comparison"]["missing_fields"], {"historical": ["预算金额"]})
        self.assertIn("历史版本的预算金额", web.context)
        # P0 预扩容决策：多点问题（命中 分别/多少 标记）即使显式 top_k=5 也放宽到
        # 渠道上限——证据完整性优先于条数节约，历史侧预算卡随之进入窗口（不再缺失）
        self.assertEqual(fixed["coverage_details"]["version_comparison"]["missing_fields"], {})
        self.assertEqual(auto["coverage_details"]["version_comparison"]["missing_fields"], {})
        # 预扩容在入口完成（top_k 直达渠道上限），自适应循环无需再扩容
        self.assertEqual(auto["evidence_budget"]["top_k"], MCP_SEARCH_MAX_TOP_K)
        # 窗口到渠道上限后，现行侧 3 张卡全部进入证据（不再截断到 2 张）
        self.assertEqual([m["source"] for m in auto["matches"] if m["status"] == "active"],
                         ["d1.md", "d2.md", "d3.md"])
        self.assertTrue(any(m["status"] == "expired" and "200 万元" in m["text"] for m in auto["matches"]))
        self.assertEqual(distinct_ids["evidence_support"]["status"], "supported")
        # 「实施要求」命中 要求 标记：id_fixed 同样被预扩容，DPC-550 证据进入窗口
        self.assertEqual(id_fixed["coverage_details"]["evidence_support"]["missing"], [])
        self.assertEqual(id_auto["coverage_details"]["evidence_support"]["missing"], [])
        self.assertEqual(id_auto["evidence_budget"]["top_k"], MCP_SEARCH_MAX_TOP_K)
        self.assertEqual([m["source"] for m in id_auto["matches"] if m["status"] == "active"],
                         ["d1.md", "d2.md", "d3.md"])

    def test_compare_only_checks_each_explicitly_requested_version_field(self):
        current = self.write_note("现行版本结算金额为 300 万元。", "current-settlement.md")
        old = self.write_note("历史版本预算为 200 万元。", "historical-budget.md", "archive/learning/general")
        self.ingest([current, old])
        examples = ("对比现行版和历史版：现行版的结算金额是多少？历史版的预算是多少？",
                    "对比现行版的结算金额和历史版的预算分别是多少？",
                    "对比现行版与历史版。现行版：结算金额是多少？历史版：预算是多少？")
        for question in examples:
            with self.subTest(question=question):
                _, meta = self.evidence(question, use_query_understanding=False)
                mcp = json.loads(mcp_server._tool_knowledge_search_impl(question))
                for details in (meta, mcp["coverage_details"]):
                    support = details["evidence_support"]
                    self.assertEqual(support["status"], "supported")
                    self.assertEqual(support["by_side"]["current"]["checked"], ["结算金额"])
                    self.assertEqual(support["by_side"]["historical"]["checked"], ["预算金额"])
                    self.assertEqual(details["version_comparison"]["missing_fields"], {})

        _, shared = self.evidence("现行版和历史版的预算分别是多少？", use_query_understanding=False)
        self.assertEqual(shared["version_comparison"]["missing_fields"], {"current": ["预算金额"]})

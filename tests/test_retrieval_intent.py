"""Check explicit file scopes, time intent and version evidence through normal retrieval."""

import json
from datetime import date
from unittest.mock import patch

from src import mcp_server, pipeline
from src.prompt import build_messages
from src.query_scope import extract_scope
from src.query_understanding import QueryUnderstanding
from src.retrieval_intent import compares_versions, explicit_file_dates
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

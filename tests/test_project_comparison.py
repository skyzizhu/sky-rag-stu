"""Project comparison from Web and MCP through the shared isolated evidence pipeline."""

import json
from dataclasses import replace
from unittest.mock import patch

from src import mcp_server, pipeline, projects
from src.project_comparison import parse_project_comparison
from src.query_understanding import QueryUnderstanding
from tests._helpers import IngestHarness


COMBINED = "星云项目和天枢项目的负责人分别是谁？这两个项目的预算加起来是多少万元？"
HIGHEST = "星云、天枢、北斗三个项目里预算最大的是哪个项目？预算是多少？"


class ProjectComparisonTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.addCleanup(self.store.client.close)
        self.project_ids = {}
        for name, filename, text, groups in (
            ("星云项目", "a.md", "核心成员：陈明（项目负责人）、林岚（架构）。项目预算 1,350,000 元。", ["all"]),
            ("天枢项目", "b.md", "项目负责人：周红。项目预算：0.02 亿元。", ["all"]),
            ("北斗项目", "c.md", "项目负责人：王芳。项目预算：80 万元。", ["finance"]),
        ):
            self.project_ids[name] = projects.save_project(projects.ProjectFields(name, None, None, [], ""))["id"]
            self.ingest([self.write_note(text, filename)], default_project_id=self.project_ids[name],
                        default_groups=groups)

    def query(self, question, *, groups=("finance",), **kwargs):
        return pipeline.query_evidence(question, config=self.cfg, use_rerank=False,
                                       user_groups=list(groups), username="alice", **kwargs)

    def test_sum_has_citations_for_each_project_even_with_top_k_one(self):
        qu = QueryUnderstanding(COMBINED, ok=True, sub_queries=["预算", "负责人"],
                                filters={"source": "wrong.md"}, metadata_scope={"project": "星云项目"})
        with patch("src.pipeline.understand_query", return_value=qu) as understanding, \
             patch("src.pipeline.get_llm_client"):
            result, meta = self.query(COMBINED, top_k=1)
        understanding.assert_called_once()
        self.assertEqual(meta["route"], "project_comparison")
        self.assertEqual([s["source"] for s in result.sources], ["a.md", "b.md"])
        self.assertIn("负责人 陈明 [1]", result.answer)
        self.assertIn("负责人 周红 [2]", result.answer)
        self.assertIn("预算合计 335 万元** [1][2]", result.answer)

    def test_highest_checks_all_projects_and_does_not_expose_hidden_evidence(self):
        result, meta = self.query(HIGHEST, use_llm=False)
        self.assertEqual([s["source"] for s in result.sources], ["a.md", "b.md", "c.md"])
        self.assertIn("预算最高：天枢，200 万元** [1][2][3]", result.answer)
        self.assertFalse(meta["comparison"]["missing"])

        masked, meta = self.query(HIGHEST, groups=("engineering",), use_llm=False)
        self.assertEqual([s["source"] for s in masked.sources], ["a.md", "b.md"])
        self.assertEqual(meta["coverage"], "partial")
        self.assertEqual(meta["comparison"]["missing"], ["北斗的预算"])
        self.assertIn("无法计算全部项目", masked.answer)
        self.assertNotIn("预算最高", masked.answer)
        self.assertNotIn("80 万元", masked.answer)

    def test_mcp_qa_and_search_reuse_comparison_and_identity(self):
        with patch.object(mcp_server, "_identity_from_ctx", return_value=(["engineering"], "alice", {})), \
             patch("src.config.get_config", return_value=replace(self.cfg, query_understanding=False)):
            qa = json.loads(mcp_server._tool_knowledge_qa_impl(HIGHEST))
            search = json.loads(mcp_server._tool_knowledge_search_impl(HIGHEST))
        self.assertEqual(qa["coverage"], "partial")
        self.assertIn("无法计算全部项目", qa["answer"])
        self.assertEqual({row["source"] for row in search["matches"]}, {"a.md", "b.md"})
        self.assertEqual(search["coverage_details"]["comparison"]["missing"], ["北斗的预算"])

    def test_explicit_filters_and_multiple_scope_errors(self):
        cases = (
            (HIGHEST, {"project_id": [self.project_ids["星云项目"]]}, "conflict"),
            ("星云、天枢、陌生三个项目里预算最高的是哪个项目？", {}, "unresolved"),
            ("星云、天枢三个项目里预算最高的是哪个项目？", {}, "comparison_count"),
            ("星云、星云项目的预算合计多少？", {}, "comparison_count"),
            ("查星云项目或天枢项目的预算", {}, "multiple"),
        )
        with patch("src.pipeline.get_retriever") as retrieve:
            for question, filters, expected in cases:
                with self.subTest(question=question):
                    result, meta = self.query(question, filters=filters, use_llm=False)
                    self.assertEqual(meta["scope_issue"], expected)
                    self.assertFalse(result.sources)
            retrieve.assert_not_called()
        filtered, meta = self.query(COMBINED, filters={"source": "a.md"}, use_llm=False)
        self.assertEqual([s["source"] for s in filtered.sources], ["a.md"])
        self.assertEqual(meta["coverage"], "partial")
        self.assertNotIn("335", filtered.answer)

    def test_conflicting_amounts_prevent_aggregation_and_quote_preserves_name(self):
        self.ingest([self.write_note("项目预算 144 万元。", "contradiction.md")],
                    default_project_id=self.project_ids["星云项目"])
        result, meta = self.query(COMBINED, use_llm=False)
        self.assertIn("预算证据有冲突", result.answer)
        self.assertIn("星云项目的预算", meta["comparison"]["missing"])
        self.assertNotIn("预算合计", result.answer)
        parsed = parse_project_comparison("比较「星云和天枢」项目和「北斗项目」的预算")
        self.assertEqual(parsed.names, ("星云和天枢", "北斗项目"))
        self.assertIsNone(parse_project_comparison("星云和天枢的 2024 年预算是多少？"))

    def test_permission_change_during_comparison_discards_all_results(self):
        with patch("src.auth_db.retrieval_acl_revision", side_effect=("current", "changed")):
            result, meta = self.query(COMBINED, use_llm=False)
        self.assertEqual(meta["scope_issue"], "index_changed")
        self.assertFalse(result.sources)
        self.assertFalse(result.retrieved)
        self.assertNotIn("335", result.answer)

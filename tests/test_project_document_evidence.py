"""Project document mentions use visible body evidence, grouped by project ID."""
import json
from dataclasses import replace
from unittest.mock import patch

from src import auth_db, mcp_server, pipeline, projects
from src.project_document_evidence import parse_project_mentions
from tests._helpers import IngestHarness


QUESTION = "知识库里有哪些项目的立项或章程文档提到了数据安全或等级保护的要求？"


class ProjectDocumentEvidenceTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.addCleanup(self.store.client.close)
        for username in ("alice", "bob"):
            auth_db.create_user(username, "test-password")
        self.ids = {name: projects.save_project(projects.ProjectFields(name, None, None, [], ""))["id"]
                    for name in ("星云项目", "天枢项目", "昆仑项目", "北斗项目", "沧海项目")}
        for project, filename, body, groups, users, doc_type in (
            ("星云项目", "星云项目章程.md", "须通过等级保护三级测评。", ["all"], [], ""),
            ("星云项目", "星云立项报告.md", "须完成数据安全测评。", ["all"], [], ""),
            ("天枢项目", "天枢主档.md", "上线前须完成数据安全测评。", [], ["alice"], "项目立项报告"),
            ("昆仑项目", "昆仑立项报告.md", "项目预算为 300 万元。", ["all"], [], ""),
            ("昆仑项目", "昆仑新闻.md", "产品通过等级保护三级测评。", ["all"], [], ""),
            ("北斗项目", "北斗立项报告.md", "上线前须完成数据安全测评。", [], ["bob"], ""),
            ("沧海项目", "沧海数据安全项目章程.md", "项目预算为 135 万元。", ["all"], [], ""),
        ):
            self.ingest([self.write_note(body, filename)], default_project_id=self.ids[project],
                        default_groups=groups, default_allowed_users=users, default_doc_type=doc_type)

    def test_web_and_mcp_share_literal_quotes_and_acl(self):
        with patch("src.pipeline.get_llm_client", side_effect=AssertionError("exact scan needs no model")):
            result = pipeline.answer_question(QUESTION, username="alice", user_groups=[],
                                              config=self.cfg, use_llm=True)
        self.assertEqual(result.evidence_meta["coverage"], "complete")
        self.assertEqual({s["project_id"] for s in result.sources}, {self.ids["星云项目"], self.ids["天枢项目"]})
        self.assertEqual(len(result.sources), 2)
        self.assertTrue(all("测评" in s["text"] for s in result.sources))
        with patch.object(mcp_server, "_identity_from_ctx", return_value=([], "bob", {})), \
             patch("src.config.get_config", return_value=self.cfg):
            search = json.loads(mcp_server._tool_knowledge_search_impl(QUESTION))
        self.assertEqual({s["project_id"] for s in search["matches"]}, {self.ids["星云项目"], self.ids["北斗项目"]})

    def test_explicit_filter_and_scan_limits_never_claim_full_coverage(self):
        result, meta = pipeline.query_evidence(
            QUESTION, config=replace(self.cfg, enum_max_cards=1), use_llm=False,
            username="alice", user_groups=[],
            filters={"project_id": [self.ids["星云项目"], self.ids["沧海项目"]]},
        )
        self.assertEqual(meta["coverage"], "partial")
        self.assertIn("清单可能不完整", result.answer)
        self.assertTrue(all(s["project_id"] == self.ids["星云项目"] for s in result.sources))
        self.assertIsNone(parse_project_mentions("哪些项目的立项报告提到了数据安全但没有提到等级保护？"))
        self.assertIsNone(parse_project_mentions("哪些项目的立项报告提到了数据安全和等级保护？"))
        self.assertEqual(parse_project_mentions("哪些项目的验收报告提到了耐久测试？"),
                         (("验收报告",), ("耐久测试",)))
        self.assertEqual(parse_project_mentions("哪些项目的立项报告提到了数据安全？"),
                         (("立项报告",), ("数据安全",)))

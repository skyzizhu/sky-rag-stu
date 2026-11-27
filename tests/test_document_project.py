"""项目挂接贯穿入库、档案编辑与检索，使用临时台账及内存 Qdrant。"""
import contextlib
import io
import json
from pathlib import Path
from unittest import mock

from src import business_directions, mcp_server, projects
from src.manage import archive, ingest_web_page, refetch_web_page, restore, update_metadata
from src.permissions import scope_filters
from tests._helpers import IngestHarness


class DocumentProjectTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.addCleanup(self.store.client.close)
        self.direction = business_directions.create_direction("低空经济")["id"]
        self.project = projects.save_project(
            projects.ProjectFields("低空监管平台", self.direction, None, [], "")
        )
        self.project_id = self.project["id"]

    def test_project_persists_and_filters_without_broadening_permissions(self):
        path = self.write_note("研发内部资料", "internal.md")
        public = self.write_note("通用资料", "public.md")
        summary = self.ingest([path], default_project_id=self.project_id, default_groups=["hr"])
        self.ingest([public])
        relative = self.rel(path)
        self.assertEqual(summary.file_rows[0]["project_id"], self.project_id)
        self.assertEqual(self.ledger()[relative]["project_id"], self.project_id)
        self.assertNotIn("direction_id", self.ledger()[relative])
        self.assertNotIn("project_id", self.ledger()[self.rel(public)])
        self.assertTrue(self.store.get_all_cards(scope_filters({"project_id": self.project_id}, ["hr"])))
        self.assertFalse(self.store.get_all_cards(scope_filters({"project_id": self.project_id}, ["finance"])))
        source = self.store.document_catalog({"project_id": [self.project_id]})["documents"]
        self.assertEqual([doc["path"] for doc in source], [relative])

        direction = business_directions.create_direction("新业务")["id"]
        projects.save_project(projects.ProjectFields("项目新名称", direction, None, [], ""),
                              project_id=self.project_id, revision=self.project["revision"])
        self.assertEqual(projects.get_project(self.project_id)["direction_name"], "新业务")
        self.assertEqual(self.ledger()[relative]["project_id"], self.project_id)

    def test_existing_document_can_link_or_clear_without_reembedding(self):
        path = self.write_note("既有资料")
        relative = self.rel(path)
        self.ingest([path])
        original = path.read_bytes()
        with mock.patch.object(self.embedding, "embed_texts", side_effect=AssertionError("意外向量化")):
            update_metadata(relative, {"project_id": self.project_id})
            self.assertTrue(self.store.get_all_cards({"project_id": self.project_id}))
            update_metadata(relative, {"project_id": None})
        self.assertEqual(path.read_bytes(), original)
        self.assertIsNone(self.ledger()[relative]["project_id"])
        self.ingest([path], default_project_id=self.project_id)
        self.assertFalse(self.store.get_all_cards({"project_id": self.project_id}))
        with self.assertRaisesRegex(ValueError, "项目不存在"):
            update_metadata(relative, {"project_id": self.project_id + 1})

    def test_archive_restore_rebuild_and_web_refetch_keep_project(self):
        path = self.write_note("项目方案")
        self.ingest([path], default_project_id=self.project_id)
        with contextlib.redirect_stdout(io.StringIO()):
            archived = archive(self.rel(path))["summary"].file_rows[0]["path"]
            self.assertEqual(self.ledger()[archived]["project_id"], self.project_id)
            restored = restore(archived)["summary"].file_rows[0]["path"]
            self.ingest(rebuild=True)
        self.assertEqual(self.ledger()[restored]["project_id"], self.project_id)

        url = "https://example.invalid/plan"
        page = {"text": "说明内容。" * 12, "title": "项目网页"}
        with (
            mock.patch("src.web_fetcher.fetch_and_parse", return_value=page),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = ingest_web_page(url, domain="projects", default_project_id=self.project_id)
        snapshot = self.rel(Path(result["file_path"]))
        self.assertTrue(snapshot.startswith("projects/dikongjianguanpingtai/"))
        info = Path(result["file_path"]).with_name("_info.md")
        self.assertIn("低空监管平台", info.read_text(encoding="utf-8"))
        with (
            mock.patch("src.web_fetcher.fetch_and_parse", return_value={**page, "text": "新版内容。" * 12}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            refetch_web_page(snapshot)
            self.assertEqual(self.rel(Path(ingest_web_page(
                url, domain="projects", default_project_id=self.project_id)["file_path"])), snapshot)
            self.ingest(rebuild=True)
        self.assertNotIn(self.rel(info), self.ledger())
        self.assertEqual(self.ledger()[snapshot]["project_id"], self.project_id)
        docs = self.store.document_catalog({"project_id": self.project_id, "status": "active"})["documents"]
        self.assertEqual({d["path"] for d in docs}, {restored, snapshot})

    def test_same_url_has_separate_snapshots_for_project_and_unassigned_uploads(self):
        url = "https://example.invalid/shared"
        page = {"text": "共用网页正文。" * 12, "title": "共享页面"}
        with (
            mock.patch("src.web_fetcher.fetch_and_parse", return_value=page),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            unassigned = ingest_web_page(url, domain="reference")["file_path"]
            project = ingest_web_page(url, domain="reference", default_project_id=self.project_id)["file_path"]
            self.assertEqual(ingest_web_page(url, domain="reference")["file_path"], unassigned)
        self.assertNotEqual(unassigned, project)
        # V3.9.2：未关联项目落 basic_knowledge/<入库用户>/；本用例未传 owner，故为桶根
        self.assertEqual(Path(unassigned).parent.name, "basic_knowledge")
        self.assertEqual(Path(project).parent.name, "dikongjianguanpingtai")

    def test_mcp_project_and_direction_filters_respect_permissions_and_live_assignment(self):
        own = self.write_note("验收要求 项目资料", "own.md")
        hidden = self.write_note("验收要求 私有资料", "hidden.md")
        other = self.write_note("验收要求 其他资料", "other.md")
        unrelated = self.write_note("验收要求 通用资料", "unrelated.md")
        other_project = projects.save_project(
            projects.ProjectFields("低空预警平台", self.direction, None, [], "")
        )["id"]
        self.ingest([own], default_project_id=self.project_id, default_groups=["hr"],
                    default_doc_type="技术方案", default_department="研发部")
        self.ingest([hidden], default_project_id=self.project_id, default_groups=["finance"])
        self.ingest([other], default_project_id=other_project)
        self.ingest([unrelated])

        with mock.patch.object(mcp_server, "_identity_from_ctx",
                               return_value=(["hr"], "tester", {"department": "hr"})):
            listing = json.loads(mcp_server._tool_list_documents_impl(business_direction="dikongjingji"))
            self.assertEqual({d["source"] for d in listing["documents"]}, {"own.md", "other.md"})
            exact = json.loads(mcp_server._tool_list_documents_impl(
                project="低空监管平台", business_direction=self.direction,
                doc_type="技术方案", department="研发部"))
            self.assertEqual([d["project_id"] for d in exact["documents"]], [self.project_id])
            self.assertEqual(exact["documents"][0]["path"], self.rel(own))
            doc_id = exact["documents"][0]["document_id"]
            fragments = json.loads(mcp_server._tool_get_document_impl([doc_id], project=self.project_id))
            self.assertEqual(fragments["project_id"], self.project_id)
            self.assertEqual(fragments["path"], self.rel(own))
            self.assertEqual(json.loads(mcp_server._tool_get_document_impl(
                [doc_id], project=other_project))["not_found"], [doc_id])
            matches = json.loads(mcp_server._tool_knowledge_search_impl(
                "验收要求", project=self.project_id))["matches"]
            self.assertEqual({d["source"] for d in matches}, {"own.md"})
            self.assertTrue(all(d["project_id"] == self.project_id for d in matches))

            moved = business_directions.create_direction("交付保障")["id"]
            projects.save_project(projects.ProjectFields("低空监管平台", moved, None, [], ""),
                                  project_id=self.project_id, revision=self.project["revision"])
            after = json.loads(mcp_server._tool_list_documents_impl(business_direction=moved))
            self.assertEqual([d["source"] for d in after["documents"]], ["own.md"])
            old_scope = json.loads(mcp_server._tool_list_documents_impl(
                project=self.project_id, business_direction=self.direction))
            self.assertEqual(old_scope["documents"], [])

    def test_storage_directory_preserves_topic_and_is_independent_of_permissions(self):
        from src.domains import usage_by_domain
        from src.project_directories import upload_directory

        for project_id in (None, self.project_id):
            # V3.9.2：界面入库总是带登录用户名；未关联项目落 basic_knowledge/<用户名>/，
            # 托管桶与用户名两层都是系统合成层，推断时一并剥离，用户的 team/rag 层级保留
            with upload_directory(self.knowledge, "learning", project_id, username="alice") as directory:
                path = directory / "team" / "rag" / "report.md"
                path.parent.mkdir(parents=True)
                path.write_text("# 验收报告\n\n验收要求和流程。" * 8, encoding="utf-8")
            self.ingest([path], default_project_id=project_id, default_groups=["hr"])
        self.ingest(rebuild=True)
        docs = self.store.list_documents({"domain": "learning"})
        self.assertEqual({doc["project_id"] for doc in docs}, {None, self.project_id})
        self.assertEqual(usage_by_domain(self.knowledge, self.ledger(), docs)["learning"]["files"], 2)
        for document in docs:
            cards = self.store.get_all_cards({"document_id": document["document_id"]})
            self.assertTrue(all(card["payload"]["topic"] == ["RAG"] for card in cards))
        unassigned_path = "learning/basic_knowledge/alice/team/rag/report.md"  # 托管桶 + 入库用户名
        self.assertIn(unassigned_path, self.ledger())
        for department, expected_count in (("hr", 2), ("finance", 0)):
            with mock.patch.object(mcp_server, "_identity_from_ctx", return_value=([department], "tester", {})):
                unassigned_listing = json.loads(mcp_server._tool_list_documents_impl(domain="learning"))
                self.assertEqual(len(unassigned_listing["documents"]), expected_count)

    def test_mcp_distinguishes_invalid_and_empty_scopes_and_skips_generation(self):
        from src.pipeline import QAResult

        empty = business_directions.create_direction("未立项")["id"]
        with (
            mock.patch.object(mcp_server, "_identity_from_ctx", return_value=(["hr"], "tester", {})),
            mock.patch("src.pipeline.answer_question") as answer,
        ):
            for tool, args, result_key in (
                (mcp_server._tool_knowledge_search_impl, ("验收要求",), "matches"),
                (mcp_server._tool_knowledge_qa_impl, ("验收要求",), "sources"),
                (mcp_server._tool_knowledge_digest_impl, ("验收要求",), "sources"),
                (mcp_server._tool_list_documents_impl, (), "documents"),
            ):
                with self.subTest(tool=tool.__name__):
                    result = json.loads(tool(*args, business_direction=empty))
                    self.assertEqual(result[result_key], [])
                    self.assertNotIn("error", result)
                    self.assertIn("error", json.loads(tool(*args, project="不存在的项目")))
            answer.assert_not_called()
            answer.return_value = QAResult("验收要求", "已找到", [], "", [])
            for tool in (mcp_server._tool_knowledge_qa_impl, mcp_server._tool_knowledge_digest_impl):
                tool("验收要求", business_direction=self.direction, department="研发部")
                self.assertEqual(answer.call_args.kwargs["filters"],
                                 {"department": "研发部", "project_id": [self.project_id]})
                self.assertEqual(answer.call_args.kwargs["user_groups"], ["hr"])

    def rel(self, path):
        return path.relative_to(self.knowledge).as_posix()

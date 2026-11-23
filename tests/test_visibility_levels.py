"""三级密级回归，使用临时 SQLite、台账及内存索引。"""
import contextlib
import io
import json
from unittest import mock
from pathlib import Path

from fastapi import HTTPException, Request

from src import admin_app, auth_db, keyword_search, mcp_server
from src.ingest_publication import publication_pending
from src.manage import archive, remove_from_index, revoke_document_user, restore, update_metadata
from src.permissions import document_visible, scope_filters
from src.pipeline import query_evidence
from src.upload_plan import execute_uploads, plan_uploads, write_directory_import
from tests._helpers import IngestHarness


class VisibilityLevelTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.addCleanup(self.store.client.close)
        self.hr = auth_db.create_department("hr")
        self.eng = auth_db.create_department("eng")
        self.alice = auth_db.create_user("alice", "password123", department_id=self.hr["id"])
        auth_db.create_user("bob", "password123", department_id=self.hr["id"])
        auth_db.create_user("carol", "password123", department_id=self.eng["id"])

    def _note(self, name, groups=None, users=None):
        path = self.write_note(f"验收方案与交付标准。 {name}", name)
        self.ingest([path], default_groups=groups, default_allowed_users=users)
        return path.relative_to(self.knowledge).as_posix()

    def _sources(self, groups, username, filters=None):
        return {c["payload"]["source"] for c in self.store.get_all_cards(
            scope_filters(filters, groups, username))}

    def test_three_levels_share_one_permission_rule_in_search_and_mcp(self):
        public = self._note("public.md")
        self._note("internal.md", ["hr"], [])
        restricted = self._note("restricted.md", [], ["alice", "carol"])
        old = [c["id"] for c in self.store.get_all_cards({"path": public})]
        self.store.client.delete_payload(self.cfg.qdrant_collection, ["allowed_users", "classification"], old)
        documents = self.store.list_documents()
        self.assertEqual({d["classification"] for d in documents}, {"公开", "内部", "受限"})
        for username, group, expected in (
            ("alice", "hr", {"public.md", "internal.md", "restricted.md"}),
            ("bob", "hr", {"public.md", "internal.md"}),
            ("carol", "eng", {"public.md", "restricted.md"}),
            ("nobody", "eng", {"public.md"}),
        ):
            with self.subTest(username=username):
                filters = {"allowed_groups": ["all", "hr"], "allowed_users": ["alice"],
                           "__viewer__": {"groups": ["all", "hr"], "username": "alice"}}
                self.assertEqual(self._sources([group], username, filters), expected)
                self.assertEqual({d["source"] for d in documents
                                  if document_visible(d, [group], username)}, expected)
                with mock.patch.object(mcp_server, "_identity_from_ctx",
                                       return_value=([group], username, {"department": group})):
                    result = json.loads(mcp_server._tool_list_documents_impl())
                    self.assertEqual({d["source"] for d in result["documents"]}, expected)
                    doc = next(d for d in documents if d["path"] == restricted)
                    read = json.loads(mcp_server._tool_get_document_impl([doc["document_id"]]))
                    self.assertEqual(read["total"], int("restricted.md" in expected))
        self.assertEqual(self._sources(["hr"], "alice", {"classification": "公开"}), {"public.md"})
        self.assertEqual(self._sources(["hr"], "alice", {"classification": "内部"}), {"internal.md"})
        self.assertEqual(self._sources(["hr"], "alice", {"classification": "受限"}), {"restricted.md"})

    def test_acl_changes_invalidate_bm25_and_do_not_change_version_or_vectors(self):
        relative = self._note("plan.md", [], ["alice"])
        self.ingest([self.write_note(body, f"other{i}.md") for i, body in enumerate(
            ("钢琴训练", "桥梁养护", "蔬菜种植", "酒店预订", "天气预报"))])

        def search(username):
            return keyword_search.bm25_search("验收", top_k=5, store=self.store, config=self.cfg,
                                               filters=scope_filters(None, ["hr"], username))

        self.assertTrue(search("alice"))
        self.assertFalse(search("bob"))
        previous = self.ledger()[relative]
        before, _ = self.store.client.scroll(self.cfg.qdrant_collection, with_vectors=True)
        with mock.patch.object(self.embedding, "embed_texts", side_effect=AssertionError("不应重建向量")):
            update_metadata(relative, {"allowed_groups": [], "allowed_users": ["bob"]})
        after, _ = self.store.client.scroll(self.cfg.qdrant_collection, with_vectors=True)
        self.assertEqual([(p.id, p.vector) for p in before], [(p.id, p.vector) for p in after])
        self.assertEqual(self.ledger()[relative]["version"], previous["version"])
        self.assertEqual(self.ledger()[relative]["overrides"]["allowed_users"], ["bob"])
        self.assertFalse(search("alice"))
        self.assertTrue(search("bob"))
        result, _ = query_evidence("验收标准是什么", filters={"classification": "受限"},
                                   use_llm=False, use_query_understanding=False,
                                   use_rerank=False, user_groups=["hr"], username="alice")
        self.assertFalse(result.retrieved)

    def test_revoke_survives_enable_archive_restore_and_rebuild(self):
        relative = self._note("restricted.md", [], ["alice"])
        auth_db.update_user(self.alice["id"], status="disabled")
        self.assertEqual(revoke_document_user("alice"), 1)
        self.assertEqual(revoke_document_user("alice"), 0)
        auth_db.update_user(self.alice["id"], status="active")
        with contextlib.redirect_stdout(io.StringIO()):
            moved = archive(relative)["summary"].file_rows[0]["path"]
            restore(moved)
            self.ingest(rebuild=True)
        [doc] = self.store.list_documents()
        self.assertEqual((doc["allowed_groups"], doc["allowed_users"], doc["classification"]), ([], [], "受限"))
        self.assertFalse(self._sources(["hr"], "alice"))
        update_metadata(relative, {"allowed_groups": [], "allowed_users": ["alice"]})
        self.assertEqual(self._sources(["hr"], "alice"), {"restricted.md"})

    def test_admin_retries_cleanup_before_reenabling(self):
        self._note("restricted.md", [], ["alice"])
        request = Request({"type": "http", "headers": []})
        admin = {"username": "tester"}
        with (mock.patch("src.manage.revoke_document_user", side_effect=RuntimeError("index unavailable")),
              self.assertLogs("src.admin_app", level="ERROR")):
            result = admin_app.api_user_toggle(self.alice["id"], request, admin)
            self.assertEqual(result["status"], "disabled")
            self.assertTrue(result["warning"])
            with self.assertRaises(HTTPException) as error:
                admin_app.api_user_toggle(self.alice["id"], request, admin)
            self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(auth_db.get_user("alice")["status"], "disabled")
        self.assertEqual(admin_app.api_user_toggle(self.alice["id"], request, admin)["status"], "active")
        self.assertFalse(self._sources(["hr"], "alice"))

    def test_reingest_after_interrupted_archive_still_requires_original_grant(self):
        relative = self._note("restricted.md", [], ["alice"])
        with (mock.patch.object(self.store.client, "batch_update_points", side_effect=RuntimeError("offline")),
              self.assertRaises(RuntimeError),
              contextlib.redirect_stdout(io.StringIO())):
            archive(relative)
        moved = "archive/" + relative
        self.assertTrue((self.knowledge / moved).is_file())
        self.assertEqual(self.ledger()[moved]["visibility_info"]["value"]["allowed_users"], ["alice"])
        self.assertTrue(publication_pending())
        self.assertIsNone(auth_db.retrieval_acl_revision())
        self.ingest([self.knowledge / moved])
        self.assertFalse(publication_pending())
        remove_from_index(moved)
        self.ingest([self.knowledge / moved])
        self.assertEqual(self._sources(["hr"], "alice"), {"restricted.md"})
        self.assertFalse(self._sources(["hr"], "bob"))

    def test_upload_conflicts_cannot_overwrite_inaccessible_paths(self):
        relative = self._note("restricted.md", [], ["alice"])
        source = self.knowledge / relative
        original = source.read_bytes()
        files = [("restricted.md", b"new copy")]
        blocked = {Path(relative)}
        decisions = plan_uploads(files, "learning/general", self.ledger(), self.knowledge,
                                 blocked_paths=blocked)
        self.assertEqual(decisions[0].existing, [])
        self.assertEqual(decisions[0].options, ["new"])
        saved, _ = execute_uploads(decisions, files, self.knowledge, blocked_paths=blocked)
        self.assertNotEqual(saved, [source])
        with self.assertRaisesRegex(ValueError, "无权覆盖"):
            write_directory_import({"general": [(("restricted.md",), b"replacement")]},
                                   self.knowledge / "learning", merge=True, blocked_paths=blocked)
        self.assertEqual(source.read_bytes(), original)

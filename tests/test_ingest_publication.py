"""Isolated regression cases for publishing a replacement edition."""

import threading
from unittest import mock

from src import auth_db, index_state, manage, pipeline
from src.ingest_publication import publication_pending
from src.permissions import scope_filters
from src.vector_store import VectorStoreError
from tests._helpers import IngestHarness


class PublicationTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()

    def test_partial_write_hides_staged_points_and_preserves_previous_edition(self):
        path = self.write_note("第一版")
        self.ingest([path])
        self.write_note("第二版")
        upsert = self.store.upsert_chunks

        def write_then_fail(chunks, vectors):
            upsert(chunks[:1], vectors[:1])
            self.assertEqual(self.versions(), {"1.0": "active"})
            self.assertEqual(self.store.count(), 1)
            raise RuntimeError("batch failed")

        with mock.patch.object(self.store, "upsert_chunks", side_effect=write_then_fail):
            with self.assertRaisesRegex(RuntimeError, "batch failed"):
                self.ingest([path])
        self.assertEqual(self.versions(), {"1.0": "active"})
        self.assertFalse(publication_pending())
        self.ingest([path])
        self.assertEqual(self.versions(), {"1.0": "expired", "1.1": "active"})

    def test_ledger_failure_restores_old_version_even_when_source_is_reverted(self):
        path = self.write_note("第一版")
        original = path.read_bytes()
        self.ingest([path])
        self.write_note("第二版")
        def fail_commit(*args, **kwargs):
            self.assertEqual(self.versions(), {"1.0": "active"})
            raise OSError("disk full")

        with mock.patch.object(index_state, "save_state", side_effect=fail_commit):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.ingest([path])
        self.assertEqual(self.versions(), {"1.0": "active"})
        path.write_bytes(original)
        self.assertEqual(len(self.ingest([path], skip_unchanged=True).skipped_files), 1)
        self.assertIn("第一版", self.store.get_all_cards()[0]["payload"]["text"])

    def test_failed_rollback_is_recovered_before_incremental_check(self):
        path = self.write_note("第一版")
        self.ingest([path])
        self.write_note("第二版")
        with (mock.patch.object(index_state, "save_state", side_effect=OSError("disk full")),
              mock.patch.object(self.store.client, "delete", side_effect=RuntimeError("offline"))):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                self.ingest([path])
        self.assertTrue(publication_pending())
        self.assertIsNone(auth_db.retrieval_acl_revision())
        self.ingest([path])
        self.assertFalse(publication_pending())
        self.assertIsNotNone(auth_db.retrieval_acl_revision())
        self.assertEqual(self.versions(), {"1.0": "expired", "1.1": "active"})

    def test_committed_publication_recovers_after_activation_failure(self):
        path = self.write_note("第一版")
        self.ingest([path])
        self.write_note("第二版")
        with mock.patch.object(self.store.client, "set_payload", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                self.ingest([path])
        self.assertTrue(publication_pending())
        self.assertEqual(self.versions(), {"1.0": "active"})
        self.assertEqual(len(self.ingest([path], skip_unchanged=True).skipped_files), 1)
        self.assertFalse(publication_pending())
        self.assertEqual(self.versions(), {"1.0": "expired", "1.1": "active"})

    def test_failure_to_clean_committed_replacement_is_retried(self):
        path = self.write_note("第一版", "note_v2.0.md")
        self.ingest([path])
        self.write_note("第二版", path.name)
        with mock.patch.object(self.store.client, "delete", side_effect=RuntimeError("offline")):
            self.assertEqual(self.ingest([path]).ok_files, 1)
        self.assertTrue(publication_pending())
        self.assertEqual(self.store.count(), 1)
        self.assertIn("第二版", self.store.get_all_cards()[0]["payload"]["text"])
        self.assertEqual(len(self.ingest([path], skip_unchanged=True).skipped_files), 1)
        self.assertFalse(publication_pending())

    def test_acl_changes_during_embedding_apply_to_both_editions(self):
        path = self.write_note("第一版")
        rel = path.relative_to(self.knowledge).as_posix()
        self.ingest([path])
        captured_acl = index_state.visibility_info_for(self.ledger(), rel)
        self.write_note("第二版")
        embed = self.embedding.embed_texts

        def narrow_access(texts):
            with mock.patch.object(pipeline, "_ingest_lock", threading.Lock()):
                manage.update_metadata(rel, {"allowed_groups": ["finance"], "allowed_users": []})
            return embed(texts)

        with (mock.patch.object(self.embedding, "embed_texts", side_effect=narrow_access),
              index_state.carry_doc_types({}, visibility_values={rel: captured_acl})):
            self.ingest([path])
        public = scope_filters({}, ["all"], username="viewer")
        self.assertFalse(self.store.get_all_cards(public))
        self.assertEqual(self.ledger()[rel]["visibility_info"]["value"]["allowed_groups"], ["finance"])
        self.assertTrue(all(row["allowed_groups"] == ["finance"] for row in self.store.list_documents()))

    def test_updates_to_another_file_during_embedding_survive(self):
        a = self.write_note("第一版", "a.md")
        b = self.write_note("第一版", "b.md")
        self.ingest([a, b])
        self.write_note("第二版", "a.md")
        self.write_note("第二版", "b.md")
        embed = self.embedding.embed_texts

        def publish_other(texts):
            with (mock.patch.object(pipeline, "_ingest_lock", threading.Lock()),
                  mock.patch.object(self.embedding, "embed_texts", side_effect=embed)):
                self.ingest([b])
            return embed(texts)

        with mock.patch.object(self.embedding, "embed_texts", side_effect=publish_other):
            self.ingest([a])
        self.assertEqual({entry["version"] for entry in self.ledger().values()}, {"1.1"})

    def test_empty_replacement_keeps_previous_hash_and_active_cards_in_mixed_batch(self):
        path = self.write_note("已有正文")
        self.ingest([path])
        rel = path.relative_to(self.knowledge).as_posix()
        previous = self.ledger()[rel]
        path.write_text("", encoding="utf-8")
        other = self.write_note("可入库正文", "other.md")
        result = self.ingest([path, other])
        self.assertEqual(result.ok_files, 1)
        self.assertFalse(result.updated_files)
        self.assertEqual(self.ledger()[rel], previous)
        self.assertEqual(self.versions("note.md"), {"1.0": "active"})

    def test_replacing_current_label_removes_old_tail(self):
        path = self.write_note("长段落。" * 2000, "note_v2.0.md")
        self.ingest([path])
        self.assertGreater(self.store.count(), 1)
        self.ingest([self.write_note("缩短正文", path.name)])
        self.assertEqual(self.versions(), {"2.0": "active"})
        self.assertEqual(self.store.count(), 1)

    def test_historical_label_cannot_be_reused_by_upload_or_metadata_edit(self):
        path = self.write_note("第一版")
        self.ingest([path])
        self.ingest([self.write_note("第二版")])
        rel = path.relative_to(self.knowledge).as_posix()
        with self.assertRaisesRegex(manage.ManageError, "历史"):
            manage.update_metadata(rel, {"version": "1.0"})
        path.write_text('---\nversion: "1.0"\n---\n不同正文', encoding="utf-8")
        with self.assertRaisesRegex(VectorStoreError, "版本 1.0 已存在"):
            self.ingest([path])
        self.assertEqual(self.versions(), {"1.0": "expired", "1.1": "active"})

    def test_manual_version_becomes_baseline_for_next_content(self):
        path = self.write_note("旧内容")
        self.ingest([path])
        manage.update_metadata(path.relative_to(self.knowledge).as_posix(), {"version": "7.0"})
        self.ingest([self.write_note("新内容")])
        self.assertEqual(self.versions(), {"7.0": "expired", "7.1": "active"})

    def test_file_changed_after_hash_does_not_publish(self):
        path = self.write_note("第一版")
        self.ingest([path])
        self.write_note("第二版")
        parse = pipeline.parse_file

        def replace_before_parse(file):
            self.write_note("第三版")
            return parse(file)

        with mock.patch.object(pipeline, "parse_file", side_effect=replace_before_parse):
            self.assertEqual(self.ingest([path]).ok_files, 0)
        self.assertEqual(self.versions(), {"1.0": "active"})
        self.ingest([path])
        self.assertEqual(self.versions(), {"1.0": "expired", "1.1": "active"})

    def test_matching_hash_repairs_missing_cards_without_bumping_version(self):
        path = self.write_note("未变化正文")
        self.ingest([path])
        self.store.delete_documents([self.store.list_documents()[0]["document_id"]])
        result = self.ingest([path], skip_unchanged=True)
        self.assertEqual(result.ok_files, 1)
        self.assertEqual(self.versions(), {"1.0": "active"})

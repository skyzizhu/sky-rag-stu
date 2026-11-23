"""声明版本识别与入库安全回归：合成文档、临时台账、内存向量库。"""
from unittest import TestCase
from src.document_version import detect_document_version, apply_confirmation
from tests._helpers import IngestHarness


def region(text, **kwargs):
    return {"text": text, "source": "content", "location": "文档开头", "authoritative": True, **kwargs}


class VersionDetectionTests(TestCase):
    def test_clear_content_declaration(self):
        result = detect_document_version("方案.docx", [region("文档版本：V1.2")])
        self.assertEqual(result["document_version"], "1.2")
        self.assertEqual(result["document_version_status"], "declared")

    def test_table_value_and_filename_agree(self):
        result = detect_document_version("方案_V01.02.docx", [region("版本号 | V1.2")])
        self.assertEqual(result["document_version"], "1.2")
        self.assertEqual(result["document_version_status"], "declared")
        self.assertEqual(len(result["document_version_evidence"]), 2)

    def test_conflicts_are_not_silently_resolved(self):
        for name, regions in (("方案_V1.1.docx", [region("文档版本：V1.2")]),
                              ("方案.docx", [region("版本号：1.2\n版本号：1.3")]),
                              ("方案_V1.1_V1.2.docx", [])):
            result = detect_document_version(name, regions)
            self.assertEqual(result["document_version"], "")
            self.assertEqual(result["document_version_status"], "needs_review")

    def test_filename_only_is_not_crosschecked(self):
        result = detect_document_version("方案_V1.2.doc", [region("正文")])
        self.assertEqual(result["document_version_status"], "filename_only")

    def test_non_document_versions_dates_and_bare_suffix_are_not_declarations(self):
        result = detect_document_version("方案_1_2.docx", [region(
            "软件版本：V2.0\n引用文档版本：1.9\n发布日期：2026.11.19\n版本 | 日期\n1.2 | 2025年\n2.0 | 2026年")])
        self.assertEqual(result["document_version_status"], "undeclared")
        self.assertEqual(result["document_version_evidence"], [])

    def test_invalid_explicit_declaration_requires_review(self):
        for text in ("版本号：1.2.3.4.5", "文档版本：未定", "版本号：V9999.1"):
            self.assertEqual(detect_document_version("plan.md", [region(text)])["document_version_status"], "needs_review")

    def test_code_examples_are_not_version_declarations(self):
        for fence in ("```", "~~~~"):
            result = detect_document_version("note.md", [region(
                f"{fence}yaml\n版本号：9.9\n{fence}\n文档版本：1.2")])
            self.assertEqual(result["document_version"], "1.2")

    def test_ocr_alone_requires_review(self):
        result = detect_document_version("scan.pdf", [region("版本号：1.2", source="ocr", authoritative=False)])
        self.assertEqual(result["document_version_status"], "needs_review")
        self.assertEqual(result["document_version"], "")

    def test_front_matter_agrees_or_conflicts(self):
        self.assertEqual(detect_document_version("note.md", [], {"version": "V1.2"})["document_version"], "1.2")
        self.assertEqual(detect_document_version("note_V1.3.md", [], {"version": "1.2"})["document_version_status"], "needs_review")

    def test_confirmation_only_applies_to_same_hash_and_revision(self):
        data = detect_document_version("a_V1.2.md", [region("版本号：1.3")])
        decision = dict(hash="h", revision="1.2", value="1.3")
        self.assertEqual(apply_confirmation(data, decision, "h", "1.2")["document_version_status"], "confirmed")
        self.assertEqual(apply_confirmation(data, decision, "new", "1.2")["document_version_status"], "needs_review")
        self.assertEqual(apply_confirmation(data, decision, "h", "2.0")["document_version_status"], "needs_review")


class DocumentVersionIngestTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()

    def test_declarations_do_not_change_legacy_revision_or_ids(self):
        path = self.write_note("版本号：V3.2\n\n项目说明正文。", "plan.md")
        first = self.ingest([path])
        docs = self.store.list_documents()
        self.assertEqual(docs[0]["document_version"], "3.2")
        self.assertEqual(docs[0]["version"], "1.0")
        before = {c["payload"]["chunk_id"] for c in self.store.get_all_cards()}
        self.ingest([path])
        after = {c["payload"]["chunk_id"] for c in self.store.get_all_cards()}
        self.assertEqual(before, after)
        self.assertEqual(first.file_rows[0]["document_version_status"], "declared")
        from src import index_state
        entry = index_state.load_state()["learning/general/plan.md"]
        self.assertEqual(entry["version_info"]["document_version"], "3.2")

    def test_manual_confirmation_is_scoped_and_invalidated_on_change(self):
        from src.manage import confirm_document_version, ManageError
        path = self.write_note("文档版本：V1.3\n\n正文。", "plan_V1.2.md")
        self.ingest([path])
        rel = "learning/general/plan_V1.2.md"
        self.assertEqual(self.store.list_documents()[0]["document_version_status"], "needs_review")
        original_bytes = path.read_bytes()
        expected_hash = self.store.list_documents()[0]["version_content_hash"]
        confirm_document_version(rel, "1.3", "1.2", expected_hash)
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(self.store.list_documents()[0]["document_version_status"], "confirmed")
        self.ingest([path])
        self.assertEqual(self.store.list_documents()[0]["document_version_status"], "confirmed")
        path.write_text("文档版本：V1.4\n\n新正文。", encoding="utf-8")
        with self.assertRaises(ManageError):
            confirm_document_version(rel, "1.4", "1.2", expected_hash)
        self.ingest([path])
        self.assertEqual(self.store.list_documents()[0]["document_version_status"], "needs_review")

    def test_stale_confirmation_is_rejected_after_same_revision_reingest(self):
        from src.manage import confirm_document_version, ManageError
        from src import index_state
        path = self.write_note("文档版本：1.3\n\n正文。", "plan_V1.2.md")
        rel = path.relative_to(self.knowledge).as_posix()
        self.ingest([path])
        old_hash = self.store.list_documents()[0]["version_content_hash"]
        path.write_text("文档版本：1.4\n\n新正文。", encoding="utf-8")
        self.ingest([path])
        before = index_state.load_state()
        with self.assertRaises(ManageError):
            confirm_document_version(rel, "1.3", "1.2", old_hash)
        self.assertEqual(before, index_state.load_state())
        self.assertEqual(self.store.list_documents()[0]["document_version_status"], "needs_review")

    def test_revision_edit_invalidates_confirmation_and_keeps_detection(self):
        from src.manage import confirm_document_version, update_metadata, ManageError
        from src import index_state
        path = self.write_note("文档版本：1.3\n\n正文。", "plan_V1.2.md")
        rel = path.relative_to(self.knowledge).as_posix()
        self.ingest([path])
        file_bytes = path.read_bytes()
        content_hash = self.store.list_documents()[0]["version_content_hash"]
        confirm_document_version(rel, "1.3", "1.2", content_hash)
        ids = {str(c["id"]) for c in self.store.get_all_cards()}
        update_metadata(rel, {"version": "7.0"})
        doc = self.store.list_documents()[0]
        self.assertEqual(doc["version"], "7.0")
        self.assertEqual(doc["document_version_status"], "needs_review")
        self.assertEqual(len(doc["document_version_evidence"]), 2)
        entry = index_state.load_state()[rel]
        self.assertEqual(entry["version"], "7.0")
        self.assertNotIn("version_confirmation", entry)
        self.assertEqual(path.read_bytes(), file_bytes)
        self.assertEqual(ids, {str(c["id"]) for c in self.store.get_all_cards()})
        with self.assertRaises(ManageError):
            confirm_document_version(rel, "1.3", "1.2", content_hash)
        confirm_document_version(rel, "1.3", "7.0", content_hash)
        self.assertEqual(self.store.list_documents()[0]["document_version_status"], "confirmed")

    def test_no_matching_current_cards_cannot_be_confirmed(self):
        from src.manage import confirm_document_version, ManageError
        from src import index_state
        path = self.write_note("文档版本：1.3\n\n正文。", "plan_V1.2.md")
        rel = path.relative_to(self.knowledge).as_posix()
        self.ingest([path])
        doc = self.store.list_documents()[0]
        self.store.delete_documents([doc["document_id"]])
        before = index_state.load_state()
        with self.assertRaises(ManageError):
            confirm_document_version(rel, "1.3", "1.2", doc["version_content_hash"])
        self.assertEqual(before, index_state.load_state())

    def test_confirmation_and_revision_edit_do_not_relabel_expired_cards(self):
        from src.manage import confirm_document_version, update_metadata
        path = self.write_note("文档版本：1.3\n\n正文。", "plan.md")
        rel = path.relative_to(self.knowledge).as_posix()
        self.ingest([path])
        path.write_text("文档版本：1.4\n\n新正文。", encoding="utf-8")
        self.ingest([path])
        expired = [c for c in self.store.get_all_cards() if c["payload"]["status"] == "expired"]
        current = next(d for d in self.store.list_documents() if d["status"] == "active")
        confirm_document_version(rel, "1.4", current["version"], current["version_content_hash"])
        update_metadata(rel, {"version": "7.0"})
        self.assertEqual(expired, [c for c in self.store.get_all_cards() if c["payload"]["status"] == "expired"])

    def test_no_declaration_and_unrelated_edit_do_not_confirm(self):
        from src.manage import update_metadata
        path = self.write_note("正文没有正式版本。", "plan.md")
        self.ingest([path])
        update_metadata("learning/general/plan.md", {"category": "documents"})
        doc = self.store.list_documents()[0]
        self.assertEqual(doc["document_version"], "")
        self.assertEqual(doc["document_version_status"], "undeclared")

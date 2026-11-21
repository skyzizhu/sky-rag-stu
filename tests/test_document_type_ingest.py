"""类型优先级、独立档案编辑与生命周期；临时目录/内存 Qdrant/FakeEmbedding。"""
import contextlib
import io
from pathlib import Path
from unittest import mock

from src import index_state
from src.manage import archive, ingest_web_page, reingest, refetch_web_page, restore, update_metadata
from src.pipeline import query_evidence
from tests._helpers import FakeEmbedding, IngestHarness


class DocumentTypeIngestTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.assertIsInstance(self.embedding, FakeEmbedding)

    def rel(self, path):
        return Path(path).relative_to(self.knowledge).as_posix()

    def current(self, path):
        return next(d for d in self.store.list_documents()
                    if d["path"] == self.rel(path) and d["status"] in ("active", "archive"))

    def fm_note(self, fm="", name="总体方案.md"):
        path = self.write_note("正文出现需求文档和会议纪要不应改变类型。", name)
        path.write_text(f"---\n{fm}\n---\n# 正文\n\n独立知识内容。\n", encoding="utf-8")
        return path

    def snapshot_points(self):
        points, _ = self.store.client.scroll(collection_name=self.cfg.qdrant_collection,
                                             with_payload=True, with_vectors=True, limit=100)
        return {str(p.id): {"payload": dict(p.payload), "vector": p.vector} for p in points}

    def test_precedence_and_filename_default_are_independent_of_business_metadata(self):
        path = self.fm_note("doc_type: '  需求文档  '\ndomain: work\ncategory: business\nallowed_groups: [hr]")
        summary = self.ingest([path], default_doc_type="会议纪要",
                              doc_type_paths={str(path.resolve()): "一期方案/附件/总体方案.md"})
        doc = self.current(path)
        self.assertEqual(doc["doc_type"], "需求文档")
        self.assertEqual(summary.file_rows[0]["doc_type"], "需求文档")
        self.assertEqual((doc["domain"], doc["category"], doc["allowed_groups"], doc["version"]),
                         ("work", "business", ["hr"], "1.0"))
        info = self.ledger()[self.rel(path)]["doc_type_info"]
        self.assertEqual(info["source"], "front_matter")
        self.assertEqual(info["upload_default"], "会议纪要")
        self.assertEqual(info["original_path"], "一期方案/附件/总体方案.md")
        update_metadata(self.rel(path), {"doc_type": " 自定义类型 "})
        self.ingest([path], default_doc_type="验收文档")
        self.assertEqual(self.current(path)["doc_type"], "自定义类型")
        self.assertEqual(self.ledger()[self.rel(path)]["doc_type_info"]["source"], "manual")

    def test_default_directory_filename_and_unknown(self):
        paths = [self.write_note("正文提到总体方案", name) for name in
                 ("a.md", "b.md", "产品_测试报告_v1.0.md", "d.md")]
        self.ingest([paths[0]], default_doc_type=" 手工自由值 ",
                    doc_type_paths={str(paths[0].resolve()): "一期方案/a.md"})
        self.ingest([paths[1]], doc_type_paths={str(paths[1].resolve()): "总体方案/二期方案/附件/b.md"})
        self.ingest(paths[2:])
        self.assertEqual([self.current(p)["doc_type"] for p in paths],
                         ["手工自由值", "二期方案", "测试报告", ""])
        self.assertEqual(self.current(paths[1])["category"], "")
        cards = self.store.get_all_cards({"document_id": self.current(paths[1])["document_id"]})
        self.assertTrue(cards)
        self.assertTrue(all("category" not in card["payload"] for card in cards))

    def test_blank_and_invalid_front_matter_are_not_manual_clears(self):
        for i, declaration in enumerate(("doc_type: ''", "doc_type: null", "doc_type: 12",
                                         "doc_type: [bad]", "doc_type: __unclassified__")):
            path = self.fm_note(declaration, name=f"a{i}.md")
            self.ingest([path], default_doc_type="",
                        doc_type_paths={str(path.resolve()): f"一期方案/附件/a{i}.md"})
            self.assertEqual(self.current(path)["doc_type"], "一期方案")

    def test_changed_declaration_supersedes_persisted_default_and_removal_reveals_default(self):
        path = self.fm_note("doc_type: ''", name="a.md")
        self.ingest([path], default_doc_type="上传默认")
        self.assertEqual(self.current(path)["doc_type"], "上传默认")
        self.fm_note("doc_type: 新文件声明", name="a.md")
        self.ingest([path])
        self.assertEqual(self.current(path)["doc_type"], "新文件声明")
        self.fm_note("title: 新标题", name="a.md")
        self.ingest([path])
        self.assertEqual(self.current(path)["doc_type"], "上传默认")
        self.assertEqual(self.versions(path.name), {"1.0": "expired", "1.1": "expired", "1.2": "active"})

    def test_clear_is_payload_only_and_survives_same_and_changed_reingest(self):
        path = self.fm_note("doc_type: 需求文档\nallowed_groups: [hr]\nversion: '3.0'")
        self.ingest([path])
        before_bytes, before_points = path.read_bytes(), self.snapshot_points()
        before_ledger = self.ledger()[self.rel(path)]
        with mock.patch.object(self.embedding, "embed_texts", side_effect=AssertionError("must not embed")):
            update_metadata(self.rel(path), {"doc_type": ""})
        after_points = self.snapshot_points()
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(before_points.keys(), after_points.keys())
        for point_id in before_points:
            expected = before_points[point_id]
            expected["payload"]["doc_type"] = ""
            self.assertEqual(expected, after_points[point_id])
        after_ledger = self.ledger()[self.rel(path)]
        for key in ("hash", "version", "version_info", "version_detection", "chunks", "indexed_at"):
            self.assertEqual(before_ledger[key], after_ledger[key])
        self.assertEqual(after_ledger["doc_type_info"]["manual"], "")
        self.assertNotIn("doc_type", after_ledger["overrides"])
        self.ingest([path], default_doc_type="会议纪要")
        self.assertEqual(self.current(path)["doc_type"], "")
        path.write_text(before_bytes.decode("utf-8") + "\n改变正文\n", encoding="utf-8")
        self.ingest([path])
        self.assertEqual(self.current(path)["doc_type"], "")
        self.assertEqual(self.current(path)["allowed_groups"], ["hr"])

    def test_management_validation_rejects_before_any_write(self):
        path = self.write_note("正文", "a.md")
        self.ingest([path])
        before = self.snapshot_points(), self.ledger(), path.read_bytes()
        for value in ([], 42, "__unclassified__", "bad\n", "a" * 81):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                update_metadata(self.rel(path), {"doc_type": value, "title": "must not change"})
            self.assertEqual(before, (self.snapshot_points(), self.ledger(), path.read_bytes()))
        with self.assertRaises(ValueError):
            self.ingest([path], default_doc_type=42)

    def test_skip_same_content_does_not_reassign_or_persist_new_upload_inputs(self):
        path = self.write_note("正文", "a.md")
        self.ingest([path], default_doc_type="已上传类型")
        before = self.snapshot_points(), self.ledger()
        with mock.patch.object(self.embedding, "embed_texts", side_effect=AssertionError("must skip")):
            summary = self.ingest([path], skip_unchanged=True, default_doc_type="新表单默认",
                                  doc_type_paths={str(path.resolve()): "二期方案/a.md"})
            scan = self.ingest(default_doc_type="扫描默认")
        self.assertEqual(summary.skipped_files, [self.rel(path)])
        self.assertEqual(scan.skipped_files, [self.rel(path)])
        self.assertEqual(before, (self.snapshot_points(), self.ledger()))

    def test_failed_commit_does_not_record_type_state(self):
        path = self.write_note("第一版", "a.md")
        self.ingest([path], default_doc_type="旧类型")
        before = self.ledger()
        path.write_text("第二版", encoding="utf-8")
        with mock.patch.object(self.store, "upsert_chunks", side_effect=RuntimeError("commit failed")), \
                self.assertRaises(RuntimeError):
            self.ingest([path], default_doc_type="新类型",
                        doc_type_paths={str(path.resolve()): "一期方案/a.md"})
        self.assertEqual(before, self.ledger())

    def test_rebuild_keeps_type_and_visibility_without_other_manual_fields(self):
        auto = self.write_note("原目录类型", "auto.md")
        default = self.write_note("默认类型", "default.md")
        clear = self.fm_note("doc_type: 需求文档", name="clear.md")
        self.ingest([auto], doc_type_paths={str(auto.resolve()): "一期方案/附件/auto.md"})
        self.ingest([default], default_doc_type="上传默认")
        self.ingest([clear])
        update_metadata(self.rel(clear), {"doc_type": "", "title": "旧手工标题", "version": "7.0", "allowed_groups": ["hr"]})
        self.ingest(rebuild=True)
        self.assertEqual([self.current(p)["doc_type"] for p in (auto, default, clear)],
                         ["一期方案", "上传默认", ""])
        clear_doc = self.current(clear)
        self.assertEqual(clear_doc["version"], "1.0")
        self.assertEqual(clear_doc["allowed_groups"], ["hr"])
        self.assertNotIn("title", self.ledger()[self.rel(clear)]["overrides"])
        self.assertEqual(self.ledger()[self.rel(auto)]["doc_type_info"]["original_path"], "一期方案/附件/auto.md")

    def test_archive_restore_and_conflicting_paths_keep_type_and_visibility(self):
        path = self.write_note("正文", "a.md")
        self.ingest([path], doc_type_paths={str(path.resolve()): "一期方案/附件/a.md"})
        update_metadata(self.rel(path), {"doc_type": "", "title": "不继承的旧标题", "version": "7.0", "allowed_groups": ["hr"]})
        # 强制归档碰撞；新的带时间戳路径必须携带类型，不写回旧版本字段。
        conflict = self.knowledge / "archive" / self.rel(path)
        conflict.parent.mkdir(parents=True)
        conflict.write_text("另一份归档文件", encoding="utf-8")
        with mock.patch("src.manage.time.strftime", return_value="20261119_000001"), \
                contextlib.redirect_stdout(io.StringIO()):
            result = archive(self.rel(path))
        moved = self.knowledge / result["summary"].file_rows[0]["path"]
        self.assertNotEqual(moved, conflict)
        self.assertEqual(self.current(moved)["doc_type"], "")
        self.assertEqual(self.current(moved)["version"], "1.0")
        self.assertEqual(self.current(moved)["allowed_groups"], ["hr"])
        self.assertNotIn(self.rel(path), self.ledger())
        self.assertNotIn("title", self.ledger()[self.rel(moved)]["overrides"])
        with contextlib.redirect_stdout(io.StringIO()):
            restored = restore(self.rel(moved))
        restored_path = self.knowledge / restored["summary"].file_rows[0]["path"]
        self.assertEqual(self.current(restored_path)["doc_type"], "")
        self.assertEqual(self.ledger()[self.rel(restored_path)]["doc_type_info"]["original_path"], "一期方案/附件/a.md")
        with contextlib.redirect_stdout(io.StringIO()):
            reingest(self.rel(restored_path))
        self.assertEqual(self.current(restored_path)["doc_type"], "")

    def test_web_no_filename_or_target_directory_inference_default_and_clear_refetch(self):
        page = {"text": "网页正文。" * 10, "title": "总体方案"}
        with mock.patch("src.web_fetcher.fetch_and_parse", return_value=page), \
                contextlib.redirect_stdout(io.StringIO()):
            first = ingest_web_page("https://example.com/一期方案", category="需求文档")
            path = Path(first["file_path"])
            typed = ingest_web_page("https://example.com/other", default_doc_type="上传网页类型")
        self.assertEqual(self.current(path)["doc_type"], "")
        typed_path = Path(typed["file_path"])
        self.assertEqual(self.current(typed_path)["doc_type"], "上传网页类型")
        update_metadata(self.rel(typed_path), {"doc_type": ""})
        with mock.patch("src.web_fetcher.fetch_and_parse", return_value={**page, "text": "网页新正文。" * 10}), \
                contextlib.redirect_stdout(io.StringIO()):
            refetch_web_page(self.rel(typed_path))
        self.assertEqual(self.current(typed_path)["doc_type"], "")
        self.assertEqual(self.versions(typed_path.name), {"1.0": "expired", "1.1": "active"})

    def test_web_refetch_preserves_file_declared_type_without_old_unrelated_fields(self):
        page = {"text": "网页正文。" * 10, "title": "标题"}
        with mock.patch("src.web_fetcher.fetch_and_parse", return_value=page), \
                contextlib.redirect_stdout(io.StringIO()):
            first = ingest_web_page("https://example.com/declared", default_doc_type="上传默认")
        path = Path(first["file_path"])
        path.write_text(path.read_text(encoding="utf-8").replace("---\n", "---\ndoc_type: 源文件类型\n", 1), encoding="utf-8")
        self.ingest([path])
        self.assertEqual(self.current(path)["doc_type"], "源文件类型")
        with mock.patch("src.web_fetcher.fetch_and_parse", return_value={**page, "text": "网页新正文。" * 10}), \
                contextlib.redirect_stdout(io.StringIO()):
            refetch_web_page(self.rel(path))
        self.assertEqual(self.current(path)["doc_type"], "源文件类型")
        self.assertEqual(self.ledger()[self.rel(path)]["doc_type_info"]["source"], "front_matter")
        self.assertNotIn("doc_type:", path.read_text(encoding="utf-8"))

    def test_old_missing_payload_reads_blank_without_migration_and_source_projection(self):
        path = self.write_note("证据知识正文", "a.md")
        self.ingest([path], default_doc_type="独立类型")
        with contextlib.redirect_stdout(io.StringIO()):
            result, _ = query_evidence("证据知识", use_llm=False, use_query_understanding=False,
                                       use_rerank=False, config=self.cfg)
        self.assertTrue(result.sources)
        self.assertTrue(all(s["doc_type"] == "独立类型" for s in result.sources))
        # 模拟旧卡片缺字段；读取必须为空，不触发重建或写入迁移。
        self.store.client.delete_payload(collection_name=self.cfg.qdrant_collection,
                                         keys=["doc_type"], points=list(self.snapshot_points()))
        before_ledger = self.ledger()
        self.assertEqual(self.current(path)["doc_type"], "")
        with contextlib.redirect_stdout(io.StringIO()):
            result, _ = query_evidence("证据知识", use_llm=False, use_query_understanding=False,
                                       use_rerank=False, config=self.cfg)
        self.assertTrue(all(s["doc_type"] == "" for s in result.sources))
        self.assertEqual(before_ledger, self.ledger())

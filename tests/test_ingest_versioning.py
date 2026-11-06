"""向量库契约 + 入库版本更新全流程（内存 Qdrant，零外部依赖）。

背景（2026-11-06 线上事故）：qdrant-client 1.19 把 set_payload 的参数
points_selector 改名为 points，「同名文件改内容后重新入库」路径报 TypeError，
直到用户实际使用才暴露。内存版 Qdrant 与服务端共用同一套客户端方法签名，
这里的测试在依赖升级当下就会失败——这正是它们存在的意义。
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from src import index_state as istate
from src.config import get_config
from src.pipeline import ingest_files
from tests._helpers import FakeEmbedding, memory_store


class _IngestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.knowledge = root / "knowledge"
        (self.knowledge / "learning" / "general").mkdir(parents=True)
        self.cfg = replace(get_config(), knowledge_dir=self.knowledge, auto_tag=False,
                           qdrant_collection="test_kb", debug=False)
        self.store = memory_store(self.cfg)
        self.embedding = FakeEmbedding()
        patcher = mock.patch.object(istate, "STATE_PATH", root / "index_state.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def ingest(self, paths=None):
        with contextlib.redirect_stdout(io.StringIO()):  # 流水线进度打印不污染测试输出
            return ingest_files(paths=paths, config=self.cfg,
                                embedding_client=self.embedding, store=self.store)

    def write_note(self, body: str, name: str = "note.md") -> Path:
        path = self.knowledge / "learning" / "general" / name
        path.write_text(f"# 笔记\n\n{body}\n", encoding="utf-8")
        return path

    def versions(self) -> dict[str, str]:
        """{版本号: 状态}——同一文档的各代卡片。"""
        return {d["version"]: d["status"] for d in self.store.list_documents()}


class VectorStoreContractTests(_IngestCase):
    """直接验证 VectorStore 每个写操作都与当前 qdrant-client 签名兼容。"""

    def test_expire_document_marks_only_target_document(self):
        self.ingest([self.write_note("甲", "a.md")])
        self.ingest([self.write_note("乙", "b.md")])
        docs = {d["path"]: d for d in self.store.list_documents()}
        target = docs["learning/general/a.md"]["document_id"]
        self.store.expire_document(target)  # 今天出事的调用
        statuses = {d["path"]: d["status"] for d in self.store.list_documents()}
        self.assertEqual(statuses, {"learning/general/a.md": "expired",
                                    "learning/general/b.md": "active"})

    def test_expire_missing_document_is_noop(self):
        self.ingest([self.write_note("甲")])
        self.store.expire_document("no-such-document")
        self.assertEqual(self.versions(), {"1.0": "active"})

    def test_delete_documents_removes_only_target(self):
        self.ingest([self.write_note("甲", "a.md")])
        self.ingest([self.write_note("乙", "b.md")])
        docs = {d["path"]: d["document_id"] for d in self.store.list_documents()}
        self.store.delete_documents([docs["learning/general/a.md"]])
        self.assertEqual([d["path"] for d in self.store.list_documents()],
                         ["learning/general/b.md"])


class IngestVersioningTests(_IngestCase):
    def test_content_update_expires_old_version_and_bumps(self):
        """事故场景本体：改内容后重新入库 → 旧版 expired、新版 1.1 active。"""
        path = self.write_note("第一版内容")
        first = self.ingest([path])
        self.assertEqual(first.new_files, ["learning/general/note.md"])
        self.assertEqual(self.versions(), {"1.0": "active"})

        self.write_note("第二版内容：改过了")
        second = self.ingest([path])
        self.assertEqual(second.updated_files, ["learning/general/note.md"])
        self.assertEqual(self.versions(), {"1.0": "expired", "1.1": "active"})
        self.assertEqual(istate.load_state()["learning/general/note.md"]["version"], "1.1")

    def test_forced_reingest_unchanged_does_not_bump_or_duplicate(self):
        path = self.write_note("内容")
        self.ingest([path])
        before = self.store.count()
        self.ingest([path])  # 「重新入库」按钮语义：强制重跑但不升版本
        self.assertEqual(self.versions(), {"1.0": "active"})
        self.assertEqual(self.store.count(), before)  # 同 point_id 原地覆盖，无重复卡片

    def test_full_scan_skips_unchanged(self):
        self.ingest([self.write_note("内容")])
        summary = self.ingest()  # 全库扫描：指纹没变 → 跳过
        self.assertEqual(summary.skipped_files, ["learning/general/note.md"])

    def test_deleted_file_vectors_cleaned_on_next_scan(self):
        # 回归：删光目录里最后一个文件时，旧实现在「找不到文件」处提前 return，
        # 跳过了已删除文件的向量清理 → 残留卡片永远参与检索
        path = self.write_note("内容")
        self.ingest([path])
        path.unlink()
        summary = self.ingest()
        self.assertEqual(summary.cleaned_files, ["learning/general/note.md"])
        self.assertEqual(self.store.list_documents(), [])
        self.assertEqual(istate.load_state(), {})

    def test_rebuild_with_empty_directory_clears_store(self):
        path = self.write_note("内容")
        self.ingest([path])
        path.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            ingest_files(rebuild=True, config=self.cfg,
                         embedding_client=self.embedding, store=self.store)
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(istate.load_state(), {})

    def test_failed_ingest_leaves_old_version_intact(self):
        """expire 在写入新卡片之前、台账在写入成功后才落盘：失败时零脏数据。"""
        path = self.write_note("第一版")
        self.ingest([path])
        self.write_note("第二版")
        with mock.patch.object(self.store, "upsert_chunks", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.ingest([path])
        # 台账仍是旧指纹 → 下次入库会重新识别为 updated 并完整重做
        self.assertEqual(istate.load_state()["learning/general/note.md"]["version"], "1.0")
        retry = self.ingest([path])
        self.assertEqual(retry.updated_files, ["learning/general/note.md"])
        self.assertEqual(self.versions()["1.1"], "active")


if __name__ == "__main__":
    unittest.main()

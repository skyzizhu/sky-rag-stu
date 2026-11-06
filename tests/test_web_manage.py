"""知识库管理页的网页管理：快照识别 / 同网址复用文件 / 重新抓取保留可见部门。"""
import contextlib
import io
from pathlib import Path
from pathlib import Path
from unittest import mock

from src.manage import ManageError, ingest_web_page, refetch_web_page
from src.web_fetcher import read_snapshot_origin
from tests._helpers import IngestHarness


def page(text, title="报销制度"):
    return {"text": text * 3, "title": title}


class SnapshotOriginTests(IngestHarness):
    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()

    def _ingest_page(self, title="报销制度", **kw):
        with mock.patch("src.web_fetcher.fetch_and_parse",
                        return_value=page("正文内容", title)), \
             contextlib.redirect_stdout(io.StringIO()):
            return ingest_web_page("https://example.com/policy",
                                   domain=kw.pop("domain", "reference"),
                                   category=kw.pop("category", "webpages"), **kw)

    def test_origin_read_from_snapshot(self):
        result = self._ingest_page()
        origin = read_snapshot_origin(result["file_path"])
        self.assertEqual(origin["url"], "https://example.com/policy")
        self.assertEqual(origin["domain"], "reference")
        self.assertEqual(origin["category"], "webpages")

    def test_reingest_from_disk_keeps_origin(self):
        """文件已入库，再次从磁盘增量入库（全库扫描）：来源信息不丢。"""
        from pathlib import Path

        result = self._ingest_page()
        snapshot = Path(result["file_path"])
        summary = self.ingest()  # 全库扫描
        self.assertEqual(summary.skipped_files, [snapshot.relative_to(self.cfg.knowledge_dir).as_posix()])
        self.assertEqual(read_snapshot_origin(snapshot)["url"],
                         "https://example.com/policy")

    def test_same_url_reuses_file_even_if_title_changed(self):
        """网页标题变了：同一网址仍沿用已有快照文件（文件名决定文档身份）。"""
        from pathlib import Path

        first = self._ingest_page()
        again = self._ingest_page(title="全新的网页标题")
        self.assertEqual(Path(first["file_path"]), Path(again["file_path"]))
        # 标题在文件头里：标题变了 = 快照内容变了 = 重新入库为新版本（不是新文档、不多一份文件）
        self.assertTrue(again["message"].startswith("网页已入库"))  # 重新入库为新版本
        self.assertEqual(len(list((self.cfg.knowledge_dir / "reference" / "webpages").glob("*.md"))), 1)

    def test_plain_file_has_no_origin(self):
        path = self.write_note("普通笔记")
        self.assertIsNone(read_snapshot_origin(path))


class RefetchTests(IngestHarness):
    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()
        self.groups = ["finance"]  # 上传时选了可见部门 finance

    def rel(self, path) -> str:
        return str(Path(path).relative_to(self.cfg.knowledge_dir))

    def _ingest(self, text="正文内容") -> str:  # noqa: E402
        with mock.patch("src.web_fetcher.fetch_and_parse", return_value=page(text)), \
             contextlib.redirect_stdout(io.StringIO()):
            result = ingest_web_page("https://example.com/policy", domain="reference",
                                     category="policies", default_groups=self.groups)
        return Path(result["file_path"])

    def test_refetch_new_content_creates_version_and_keeps_visibility(self):
        path = self._ingest()
        self.assertEqual(self.versions(path.name), {"1.0": "active"})

        with mock.patch("src.web_fetcher.fetch_and_parse", return_value=page("内容更新了")), \
             contextlib.redirect_stdout(io.StringIO()):
            result = refetch_web_page(self.rel(path))

        self.assertEqual(self.versions(path.name), {"1.0": "expired", "1.1": "active"})
        self.assertEqual(result["url"], "https://example.com/policy")
        # 可见部门沿用当前卡片，而不是回落全员
        groups = {tuple(d["allowed_groups"]) for d in self.store.list_documents()
                  if d["status"] == "active"}
        self.assertEqual(groups, {("finance",)})

    def test_refetch_unchanged_skips(self):
        path = self._ingest()
        with mock.patch("src.web_fetcher.fetch_and_parse", return_value=page("正文内容")), \
             contextlib.redirect_stdout(io.StringIO()):
            result = refetch_web_page(self.rel(path))
        self.assertTrue(result.get("skipped"), result)

    def test_non_web_file_rejected(self):
        path = self.write_note("普通笔记")
        with self.assertRaises(ManageError):
            refetch_web_page(self.rel(path))

    def test_network_error_is_readable(self):
        path = self._ingest()
        with mock.patch("src.web_fetcher.fetch_and_parse", side_effect=RuntimeError("连接超时")), \
             self.assertRaises(ManageError) as ctx, contextlib.redirect_stdout(io.StringIO()):
            refetch_web_page(self.rel(path))
        self.assertIn("连接超时", str(ctx.exception))


class ArchivedPageTests(IngestHarness):
    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()

    def test_archived_page_refetch_blocked_by_manage(self):
        from src.manage import archive
        with mock.patch("src.web_fetcher.fetch_and_parse", return_value=page("正文")), \
             contextlib.redirect_stdout(io.StringIO()):
            result = ingest_web_page("https://example.com/policy")
        with contextlib.redirect_stdout(io.StringIO()), \
             mock.patch("src.manage.get_config", return_value=self.cfg):
            archive(Path(result["file_path"]).relative_to(self.cfg.knowledge_dir).as_posix())
        with self.assertRaises(ManageError):
            refetch_web_page("archive/reference/webpages/" + Path(result["file_path"]).name)


if __name__ == "__main__":
    unittest.main()

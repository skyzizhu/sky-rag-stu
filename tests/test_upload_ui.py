"""上传页 UI 全流程（AppTest 真实运行 app.py）：预检 → 确认面板 → 落盘 → 入库。

隔离：临时知识目录 / 临时台账 / 内存 Qdrant / 假向量化，认证服务与文件选择器打桩。
st.navigation 的函数式页面无法用 AppTest.switch_page 进入，测试中把上传页设为默认页。
"""

from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

import streamlit as st
from streamlit.testing.v1 import AppTest

from tests._helpers import IngestHarness

APP = str(Path(__file__).resolve().parent.parent / "app.py")
_real_page = st.Page


def _upload_page_as_default(page, *args, **kwargs):
    kwargs["default"] = kwargs.get("url_path") == "upload"
    return _real_page(page, *args, **kwargs)


class _FakeUpload:
    def __init__(self, name: str, data: bytes):
        self.name, self._data, self.size, self.file_id = name, data, len(data), name

    def getvalue(self) -> bytes:
        return self._data


def note(body: str) -> bytes:
    return f"# 笔记\n\n{body}\n".encode("utf-8")


class UploadPageFlowTests(IngestHarness):
    def setUp(self) -> None:
        super().setUp()
        st.cache_resource.clear()
        st.cache_data.clear()
        self.uploads: list[_FakeUpload] = []
        for p in (
            mock.patch("src.config.get_config", return_value=self.cfg),
            mock.patch("src.pipeline.get_config", return_value=self.cfg),
            mock.patch("src.vector_store.get_vector_store", return_value=self.store),
            mock.patch("src.pipeline.get_vector_store", return_value=self.store),
            mock.patch("src.embedding.get_embedding_client", return_value=self.embedding),
            mock.patch("src.pipeline.get_embedding_client", return_value=self.embedding),
            mock.patch("src.auth_client.bootstrap",
                       return_value={"has_users": True, "has_admins": True, "departments": []}),
            mock.patch("src.auth_client.get_prefs", return_value={}),
            mock.patch("streamlit.file_uploader", side_effect=lambda *a, **k: self.uploads),
            mock.patch("streamlit.Page", side_effect=_upload_page_as_default),
            # 目录拖放自定义组件在模块首次导入时注册；多个 AppTest 实例各有独立运行时，
            # 第二个实例里它已「未注册」——与本组用例无关（只测文件上传 Tab），打桩为空
            mock.patch("src.directory_uploader.directory_drop_uploader", return_value=None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def open_upload_page(self, files: list[tuple[str, bytes]]) -> AppTest:
        self.uploads = [_FakeUpload(n, d) for n, d in files]
        at = AppTest.from_file(APP, default_timeout=90)
        at.query_params["rip"] = "1"
        at.session_state["auth_user"] = {"username": "u", "department": "hr",
                                         "department_id": 1, "permissions": ["upload"],
                                         "real_name": "", "note": ""}
        with contextlib.redirect_stdout(io.StringIO()):
            at.run()
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
        return at

    @staticmethod
    def click(at: AppTest, label_part: str) -> AppTest:
        with contextlib.redirect_stdout(io.StringIO()):  # 页面内触发的入库会打印进度
            next(b for b in at.button if label_part in b.label).click().run()
        return at

    def assert_no_exception(self, at: AppTest) -> None:
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])

    def test_new_file_ingests_directly_without_confirmation(self):
        at = self.click(self.open_upload_page([("fresh.md", note("全新"))]), "开始入库")
        self.assert_no_exception(at)
        self.assertFalse(any("确认入库" in b.label for b in at.button), "无冲突不应多一步")
        self.assertEqual(self.versions("fresh.md"), {"1.0": "active"})

    def test_changed_file_confirmed_as_new_version(self):
        self.ingest([self.write_note("第一版", "a.md")])
        at = self.click(self.open_upload_page([("a.md", note("第二版"))]), "开始入库")
        self.assertEqual([(r.label, r.value) for r in at.radio], [("a.md", "new_version")])
        self.click(at, "确认入库")
        self.assert_no_exception(at)
        self.assertEqual(self.versions("a.md"), {"1.0": "expired", "1.1": "active"})
        self.assertFalse((self.knowledge / "learning/general/a-2.md").exists())

    def test_user_can_choose_copy_instead(self):
        self.ingest([self.write_note("原文", "a.md")])
        at = self.click(self.open_upload_page([("a.md", note("改过"))]), "开始入库")
        at.radio[0].set_value("copy").run()
        self.click(at, "确认入库")
        self.assert_no_exception(at)
        self.assertTrue((self.knowledge / "learning/general/a-2.md").exists())
        self.assertEqual(self.versions("a.md"), {"1.0": "active"})  # 原文档不受影响

    def test_identical_reupload_is_skipped(self):
        self.ingest([self.write_note("同样的内容", "a.md")])
        before = self.store.count()
        at = self.click(self.open_upload_page([("a.md", note("同样的内容"))]), "开始入库")
        self.assertEqual(len(at.radio), 0)  # 同位置同内容只能跳过，不给选项
        self.click(at, "确认入库")
        self.assert_no_exception(at)
        self.assertEqual(self.store.count(), before)
        self.assertTrue(any("跳过" in i.value for i in at.info))

    def test_cancel_writes_nothing(self):
        self.ingest([self.write_note("原文", "a.md")])
        at = self.click(self.open_upload_page([("a.md", note("改过"))]), "开始入库")
        self.click(at, "取消")
        self.assert_no_exception(at)
        self.assertFalse(any("确认入库" in b.label for b in at.button))
        self.assertEqual((self.knowledge / "learning/general/a.md").read_bytes(), note("原文"))
        self.assertEqual(self.versions("a.md"), {"1.0": "active"})


if __name__ == "__main__":
    unittest.main()

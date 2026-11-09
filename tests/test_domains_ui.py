"""领域管理页 UI 全流程（AppTest 真实运行 app.py）：新增 → 改名 → 停用/启用 → 删除空领域；
有数据 / 系统领域的删除与停用按钮必须禁用。隔离方式同 tests/test_upload_ui.py。"""

from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

import streamlit as st
from streamlit.testing.v1 import AppTest

from src import domains
from tests._helpers import IngestHarness, isolate_app_settings

APP = str(Path(__file__).resolve().parent.parent / "app.py")
_real_page = st.Page


def _domains_page_as_default(page, *args, **kwargs):
    kwargs["default"] = kwargs.get("url_path") == "domains"
    return _real_page(page, *args, **kwargs)


class DomainPageFlowTests(IngestHarness):
    def setUp(self) -> None:
        super().setUp()
        st.cache_resource.clear()
        st.cache_data.clear()
        # 界面语言等设置隔离到临时文件：测试不再受开发者本机 settings.json 影响
        self.addCleanup(isolate_app_settings())
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
            mock.patch("streamlit.Page", side_effect=_domains_page_as_default),
            mock.patch("src.directory_uploader.directory_drop_uploader", return_value=None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def open_page(self, permissions=("domains",)) -> AppTest:
        at = AppTest.from_file(APP, default_timeout=90)
        at.query_params["rip"] = "1"
        at.session_state["auth_user"] = {"username": "u", "department": "hr",
                                         "department_id": 1, "permissions": list(permissions),
                                         "real_name": "", "note": ""}
        self.render(at)
        return at

    def render(self, at: AppTest) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            at.run()
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])

    def click(self, at: AppTest, button) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            button.click().run()
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])

    def test_lists_builtins_with_system_protection(self):
        at = self.open_page()
        labels = [e.label for e in at.expander]
        for code in domains.BUILTIN_CODES:
            self.assertTrue(any(label.startswith(code + " ·") for label in labels), code)
        self.assertTrue(at.button(key="dom_del_archive").disabled)
        self.assertTrue(at.button(key="dom_off_reference").disabled)

    def test_create_rename_disable_delete(self):
        at = self.open_page()
        at.text_input[0].input("finance")      # 新增表单：编号
        at.text_input[1].input("财务")         # 新增表单：名称
        self.click(at, next(b for b in at.button if b.label == "创建领域"))
        self.assertEqual(domains.display_name("finance"), "财务")
        self.assertTrue(any(e.label.startswith("finance · 财务") for e in at.expander))

        # 改名：最后一个「显示名称」输入框属于最后登记的 finance
        name_inputs = [w for w in at.text_input if w.label == "显示名称"]
        name_inputs[-1].input("财务资料")
        save_buttons = [b for b in at.button if b.label == "保存名称与描述"]
        self.click(at, save_buttons[-1])
        self.assertEqual(domains.display_name("finance"), "财务资料")
        self.assertEqual(domains.get_domain("finance")["code"], "finance")  # 编号不变

        self.click(at, at.button(key="dom_off_finance"))
        self.assertNotIn("finance", domains.enabled_codes())
        self.click(at, at.button(key="dom_on_finance"))
        self.assertIn("finance", domains.enabled_codes())

        self.click(at, at.button(key="dom_del_finance"))       # 打开确认框
        self.click(at, next(b for b in at.button if b.label == "确认删除"))
        self.assertNotIn("finance", domains.all_codes())

    def test_delete_disabled_when_domain_has_data(self):
        domains.create_domain("finance", "财务")
        self.ingest([self.write_note("报销流程", "a.md", "finance")])
        at = self.open_page()
        self.assertTrue(at.button(key="dom_del_finance").disabled)
        self.assertFalse(at.button(key="dom_off_finance").disabled)  # 有数据仍可停用

    def test_page_hidden_without_permission(self):
        at = self.open_page(permissions=())
        self.assertFalse(any(e.label.startswith("work ·") for e in at.expander))


if __name__ == "__main__":
    unittest.main()

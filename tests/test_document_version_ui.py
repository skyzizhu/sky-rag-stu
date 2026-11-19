"""文档版本列表与显式确认的隔离 AppTest。"""
import contextlib
import io
import os
from unittest import mock, skipIf, TestCase
from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest
from tests._helpers import IngestHarness, isolate_app_settings

APP = str(Path(__file__).resolve().parent.parent / "app.py")
real_page = st.Page


def manage_default(page, *args, **kwargs):
    kwargs["default"] = kwargs.get("url_path") == "manage"
    return real_page(page, *args, **kwargs)


class VersionUiStructureTests(TestCase):
    """不创建套接字：验证实际列表行函数与只读列配置。"""
    def test_confirmation_uses_displayed_content_hash(self):
        import ast
        tree = ast.parse(Path(APP).read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "confirm_document_version"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0].args), 4)
        self.assertIn("version_content_hash", ast.unparse(calls[0].args[3]))
        keys = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_declared_suffix" for t in n.targets)]
        self.assertIn("version_content_hash", ast.unparse(keys[0].value))

    def test_actual_row_contains_declared_version_and_state(self):
        import ast
        source = Path(APP).read_text(encoding="utf-8")
        tree = ast.parse(source)
        manage = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "page_manage")
        row = next(n for n in ast.walk(manage) if isinstance(n, ast.FunctionDef) and n.name == "_row")
        module = ast.Module(body=[row], type_ignores=[])
        ast.fix_missing_locations(module)
        values = {"origins": {}, "selected_ids": set(), "t": lambda x: x, "domain_label": lambda x: x}
        exec(compile(module, APP, "exec"), values)
        doc = dict(document_id="d", source="plan.md", path="plan.md", domain="projects", category="test",
                   topic=[], version="1.0", status="active", chunks=1, document_version="1.3", document_version_status="declared")
        result = values["_row"](doc)
        self.assertEqual(result["version.col.declared"], "1.3")
        self.assertEqual(result["version.col.status"], "version.state.declared")
        disabled = [kw.value for n in ast.walk(manage) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "data_editor" for kw in n.keywords if kw.arg == "disabled"]
        self.assertTrue(disabled)
        text = ast.unparse(disabled[0])
        self.assertIn("version.col.declared", text)
        self.assertIn("version.col.status", text)


@skipIf(os.name == "nt", "Windows AppTest 初始化套接字被测试网络隔离保护禁止")
class DocumentVersionUiTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        st.cache_resource.clear()
        st.cache_data.clear()
        self.addCleanup(isolate_app_settings("zh-CN"))
        path = self.write_note("文档版本：1.3\n\n正式正文。", "plan_V1.2.md")
        self.ingest([path])
        for patch in (mock.patch("src.auth_client.bootstrap", return_value={"has_users": True, "has_admins": True, "departments": []}),
                      mock.patch("src.auth_client.get_prefs", return_value={}),
                      mock.patch("streamlit.Page", side_effect=manage_default),
                      mock.patch("src.directory_uploader.directory_drop_uploader", return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)

    def render(self, at):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            at.run()
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
        return at

    def open(self):
        at = AppTest.from_file(APP, default_timeout=30)
        at.query_params["rip"] = "1"
        at.session_state["auth_user"] = dict(username="u", department="hr", department_id=1, permissions=["manage"])
        return self.render(at)

    def select(self, at):
        key = f"manage_document_table_{at.session_state['manage_table_revision']}"
        at.session_state[key] = dict(edited_rows={0: {"选择": True}}, added_rows=[], deleted_rows=[])
        return self.render(at)

    def test_conflict_list_and_unrelated_save_does_not_confirm(self):
        at = self.open()
        self.assertIn("文档声明版本", at.dataframe[0].value.columns)
        self.assertEqual(at.dataframe[0].value["版本核对"].iloc[0], "待人工确认")
        self.select(at)
        self.assertTrue(any("冲突" in e.value for e in at.warning))
        next(x for x in at.text_input if x.key and x.key.startswith("ed_cat_")).input("documents")
        next(x for x in at.button if x.label == "💾 保存档案修改").click()
        self.render(at)
        self.assertEqual(self.store.list_documents()[0]["document_version_status"], "needs_review")

    def test_explicit_confirmation_same_value_uses_existing_edit(self):
        at = self.open()
        self.select(at)
        next(x for x in at.text_input if x.key and x.key.startswith("ed_declared_")).input("1.3")
        next(x for x in at.checkbox if x.key.startswith("ed_confirm_")).check()
        next(x for x in at.button if x.label == "💾 保存档案修改").click()
        self.render(at)
        self.assertEqual(self.store.list_documents()[0]["document_version_status"], "confirmed")
        self.assertEqual(self.store.list_documents()[0]["version"], "1.2")

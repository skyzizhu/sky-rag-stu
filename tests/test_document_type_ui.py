"""Document-type frontend contracts; AST execution avoids Windows AppTest sockets.

Flows use the isolated harness and fakes. No live knowledge, settings or services.
"""
from __future__ import annotations

import ast
import contextlib
import html
import io
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from src.app_settings import SETTINGS_KEYS, _normalize_choices, load_app_settings, save_app_settings
from src.document_type import normalize_doc_type, normalize_doc_type_filter
from src.document_department import normalize_department, normalize_department_filter
from src.i18n import LANGUAGE_ORDER, _CATALOG
from src.ingest_audit import IngestItem
from tests._helpers import IngestHarness, isolate_app_settings

APP = Path(__file__).resolve().parent.parent / "app.py"
TREE = ast.parse(APP.read_text(encoding="utf-8"), filename=str(APP))


def function(name, parent=TREE):
    return next(node for node in ast.walk(parent)
                if isinstance(node, ast.FunctionDef) and node.name == name)


def execute_nodes(nodes, namespace):
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(APP), "exec"), namespace)
    return namespace


def load_functions(names, namespace):
    return execute_nodes([function(name) for name in names], namespace)


def calls(parent, name):
    return [node for node in ast.walk(parent) if isinstance(node, ast.Call)
            and ((isinstance(node.func, ast.Name) and node.func.id == name)
                 or (isinstance(node.func, ast.Attribute) and node.func.attr == name))]


def document(**changes):
    value = dict(document_id="doc-a", source="note.md", path="learning/general/note.md",
                 domain="learning", category="general", topic=[], version="1.0",
                 status="active", chunks=1, file_type="md")
    value.update(changes)
    return value


class DocumentTypeStructureTests(unittest.TestCase):
    def test_actual_upload_fields_and_all_call_chains(self):
        page = function("page_upload")
        inputs = [node for node in calls(page, "text_input")
                  if ast.unparse(node.args[0]) == "t('doc_type.default')"]
        self.assertEqual(len(inputs), 3)
        self.assertEqual({next(ast.literal_eval(k.value) for k in node.keywords if k.arg == "key")
                          for node in inputs}, {"upload_doc_type", "dir_doc_type", "web_doc_type"})
        for node in inputs:
            self.assertEqual(next(ast.literal_eval(k.value) for k in node.keywords
                                  if k.arg == "max_chars"), 80)
            self.assertEqual(next(ast.literal_eval(k.value) for k in node.keywords
                                  if k.arg == "value"), "")
        for name in ("_start_upload", "_render_upload_confirmation", "_do_directory_upload", "ingest_web_page"):
            node = calls(page, name)[0]
            self.assertIn("default_doc_type", {key.arg for key in node.keywords})
        for name in ("_start_upload", "_render_upload_confirmation"):
            self.assertEqual(ast.unparse(calls(page, name)[0].args[1]), "upload_domain")
        self.assertNotIn("upload.category", ast.unparse(page))
        self.assertNotIn("web.category", ast.unparse(page))
        for name in ("_start_upload", "_render_upload_confirmation", "_run_upload", "_do_directory_upload"):
            self.assertIn("default_doc_type", {arg.arg for arg in function(name).args.kwonlyargs})
        for name in ("_start_upload", "_render_upload_confirmation"):
            self.assertIn("default_doc_type", {k.arg for k in calls(function(name), "_run_upload")[0].keywords})
        for name in ("_run_upload", "_do_directory_upload"):
            keywords = {k.arg for k in calls(function(name), "ingest_files")[0].keywords}
            self.assertIn("default_doc_type", keywords)
            self.assertIn("skip_unchanged", keywords)
        self.assertNotIn("doc_type_paths", {k.arg for k in calls(function("_run_upload"), "ingest_files")[0].keywords})
        self.assertIn("doc_type_paths", {k.arg for k in calls(function("_do_directory_upload"), "ingest_files")[0].keywords})

    def test_actual_web_call_normalizes_only_default_type(self):
        call = calls(function("page_upload"), "ingest_web_page")[0]
        for value, expected in ((" 技术方案 ", "技术方案"), (" ", None)):
            ingest = mock.Mock()
            execute_nodes([ast.Expr(value=call)],
                          dict(ingest_web_page=ingest, audit=None, web_url=" https://example.invalid/a ",
                               web_domain="reference", web_groups=["all"], web_project=7,
                               web_doc_type=value, normalize_doc_type=normalize_doc_type,
                               web_department=" ", normalize_department=normalize_department,
                               web_allowed_users=None,
                               blocked_paths=frozenset(),
                               istate=SimpleNamespace(load_state=dict),
                               st=SimpleNamespace(session_state={}), tr=lambda text: text))
            self.assertEqual(ingest.call_args.args, ("https://example.invalid/a",))
            self.assertEqual(ingest.call_args.kwargs["default_doc_type"], expected)
            self.assertEqual(ingest.call_args.kwargs["default_department"], None)
            self.assertIsNone(ingest.call_args.kwargs["default_allowed_users"])
            self.assertEqual(ingest.call_args.kwargs["domain"], "reference")
            self.assertEqual(ingest.call_args.kwargs["default_project_id"], 7)
            self.assertNotIn("category", ingest.call_args.kwargs)
            self.assertEqual(ingest.call_args.kwargs["default_groups"], ["all"])

    def test_project_selection_defaults_domain_without_locking_it(self):
        state = {"project_widget": 7, "domain_widget": "learning"}
        env = execute_nodes([function("_default_project_domain")],
                            {"st": SimpleNamespace(session_state=state)})
        callback = env["_default_project_domain"]
        callback("project_widget", "domain_widget", ["learning", "projects"])
        self.assertEqual(state["domain_widget"], "projects")
        state["domain_widget"] = "reference"
        callback("project_widget", "domain_widget", ["learning", "reference"])
        self.assertEqual(state["domain_widget"], "reference")
        state["project_widget"] = None
        callback("project_widget", "domain_widget", ["learning", "projects", "reference"])
        self.assertEqual(state["domain_widget"], "reference")

    def test_clearing_upload_project_uses_localized_placeholder(self):
        selectbox = mock.Mock(return_value=None)
        env = execute_nodes([function(name) for name in (
            "project_label", "_default_project_domain", "_upload_project",
        )], {"st": SimpleNamespace(selectbox=selectbox), "t": lambda key: f"localized:{key}"})
        self.assertIsNone(env["_upload_project"]({7: {"name": "Platform"}}, "project", "domain", ["projects"]))
        self.assertEqual(selectbox.call_args.args[1], [None, 7])
        self.assertEqual(selectbox.call_args.kwargs["placeholder"], "localized:project.none")
        self.assertEqual(selectbox.call_args.kwargs["format_func"](None), "localized:project.none")

    def test_actual_row_preserves_format_and_type_is_read_only(self):
        page = function("page_manage")
        env = execute_nodes([function("_row", page)],
                            dict(origins={}, selected_ids=set(), t=lambda key: key, domain_label=lambda value: value,
                                 project_catalog={}, project_label=lambda project_id, catalog: "未挂项目",
                                 _visibility_label=lambda value: value or "—"))
        for value in (None, "", "  ", " 技术方案 "):
            row = env["_row"](document(doc_type=value))
            self.assertEqual(row["manage.col.type"], "MD")
            self.assertEqual(row["common.doc_type"], (value or "").strip() or "doc_type.unclassified")
        env["origins"] = {document()["path"]: {"url": "https://example.invalid"}}
        self.assertEqual(env["_row"](document())["manage.col.type"], "web")
        editor = calls(page, "data_editor")[0]
        disabled = next(k.value for k in editor.keywords if k.arg == "disabled")
        self.assertIn("common.doc_type", ast.unparse(disabled))
        signature = next(node for node in ast.walk(page) if isinstance(node, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id == "filter_signature" for target in node.targets))
        self.assertIn("type_filter", ast.unparse(signature.value))

    def test_ingest_result_rows_show_resolved_type_and_legacy_blank(self):
        table = calls(function("_show_ingest_result"), "dataframe")[0]
        expression = ast.Expression(body=table.args[0])
        for value in (None, "", " 技术方案 "):
            row = dict(path="learning/general/note.md", domain="learning", category="general",
                       status="ok", chunks=1)
            if value is not None:
                row["doc_type"] = value
            result = eval(compile(expression, str(APP), "eval"),
                          dict(summary=SimpleNamespace(file_rows=[row]), t=lambda key: key,
                               domain_label=lambda value: value, project_catalog={},
                               project_label=lambda project_id, catalog: "未挂项目",
                               _visibility_label=lambda value: value or "—"))
            self.assertEqual(result[0]["common.doc_type"], (value or "").strip() or "doc_type.unclassified")
            self.assertNotIn("ingest.col.category", result[0])

    def test_actual_management_filter_handles_legacy_and_exact_values(self):
        page = function("page_manage")
        env = execute_nodes([function("_match", page)],
                            dict(origins={}, keyword="", dom_filter="all", status_filter="all",
                                 origin_filter="all", department_filter=("all", ""), project_filter=None,
                                 direction_filter=None, project_catalog={}, visibility_filter=""))
        for choice, expected in ((("all", ""), [True, True, True, True]),
                                 (("unclassified", ""), [True, True, True, False]),
                                 (("exact", "技术方案"), [False, False, False, True])):
            env["type_filter"] = choice
            self.assertEqual([env["_match"](doc) for doc in
                              (document(), document(doc_type=None), document(doc_type="  "),
                               document(doc_type=" 技术方案 "))], expected)
        # A user-defined type named "all" remains distinct from the all-types UI option.
        env["type_filter"] = ("exact", "all")
        self.assertTrue(env["_match"](document(doc_type="all")))
        self.assertFalse(env["_match"](document()))

        env.update(keyword="", type_filter=("all", ""), department_filter=("all", ""),
                   project_filter=7, direction_filter=1, project_catalog={7: {"direction_id": 1}})
        self.assertTrue(env["_match"](document(project_id=7)))
        self.assertFalse(env["_match"](document(project_id=8)))
        env["project_catalog"][7]["direction_id"] = 2
        self.assertFalse(env["_match"](document(project_id=7)))
        env.update(project_filter=None, direction_filter=None)
        env["keyword"] = "other"
        self.assertFalse(env["_match"](document(doc_type="all")))
        # 归属部门筛选：先清掉上一个场景的关键词与类型条件。
        env["keyword"] = ""
        env["type_filter"] = ("all", "")
        env["department_filter"] = ("no_department", "")
        self.assertTrue(env["_match"](document()))
        self.assertFalse(env["_match"](document(department="系统工程事业部")))
        env["department_filter"] = ("exact", "系统工程事业部")
        self.assertTrue(env["_match"](document(department="系统工程事业部")))
        self.assertFalse(env["_match"](document()))

    def test_independent_form_saves_only_type_and_supports_manual_clear(self):
        page = function("page_manage")
        start = next(i for i, node in enumerate(page.body) if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == "_type_suffix" for target in node.targets))
        nodes = page.body[start:start + 3]
        self.assertIsInstance(nodes[1], ast.With)
        self.assertIn("st.form", ast.unparse(nodes[1].items[0].context_expr))
        self.assertIn("form_submit_button", ast.unparse(nodes[1]))
        keys = []
        for doc, value, normalized in ((document(), " 技术方案 ", "技术方案"),
                                       (document(document_id="doc-b"), "  ", "")):
            st = mock.MagicMock()
            st.session_state = {}
            st.text_input.return_value = value
            st.form_submit_button.return_value = True
            update = mock.Mock(return_value={"message": "saved"})
            execute_nodes(nodes, dict(st=st, d=doc, t=lambda key: key, normalize_doc_type=normalize_doc_type,
                                     update_metadata=update, ManageError=RuntimeError, VectorStoreError=LookupError))
            update.assert_called_once_with(doc["path"], {"doc_type": normalized})
            self.assertEqual(st.text_input.call_args.kwargs["max_chars"], 80)
            keys.append(st.form.call_args.kwargs["key"])
        self.assertNotEqual(*keys)
        st = mock.MagicMock()
        st.session_state = {}
        st.text_input.return_value = "x" * 81
        st.form_submit_button.return_value = True
        update = mock.Mock()
        execute_nodes(nodes, dict(st=st, d=document(), t=lambda key: key, normalize_doc_type=normalize_doc_type,
                                 update_metadata=update, ManageError=RuntimeError, VectorStoreError=LookupError))
        update.assert_not_called()
        st.error.assert_called_once()
        # The existing broad metadata save has no doc_type side effects.
        existing = calls(page, "update_metadata")[-1]
        self.assertEqual(ast.unparse(existing.args[1]), "updates")
        assignments = [node for node in ast.walk(page) if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "updates" for target in node.targets)]
        self.assertNotIn("doc_type", ast.unparse(assignments[0].value))

    def test_sources_and_management_detail_escape_type(self):
        st = mock.MagicMock()
        st.session_state = {}
        env = load_functions(["tag", "show_answer_sources"], dict(st=st, html=html, t=lambda key, **kwargs: key))
        for value in (None, "", '<img src=x onerror="alert(1)">'):
            st.markdown.reset_mock()
            result = SimpleNamespace(sources=[dict(rank=1, source="note", score=0.9, text="text",
                                                   path="learning/public/note.md",
                                                   doc_type=value, status="active", version="1.0")])
            env["show_answer_sources"](result)
            self.assertEqual(st.text.call_args.args, ("learning/public/note.md",))
            rendered = "\n".join(str(call.args[0]) for call in st.markdown.call_args_list
                                 if call.kwargs.get("unsafe_allow_html"))
            self.assertIn(html.escape(value) if value else "doc_type.unclassified", rendered)
            self.assertNotIn("<img", rendered)
        page = function("page_manage")
        heading = next(node for node in calls(page, "markdown")
                       if "html.escape(str(d['source']))" in ast.unparse(node))
        env = dict(st=st, html=html, d=document(doc_type="<script>x</script>"), origin={}, status_tag="",
                   _visibility_label=lambda value: value or "—",
                   t=lambda key: key, tag=env["tag"], domain_label=lambda value: value)
        execute_nodes([ast.Expr(value=heading)], env)
        self.assertIn("&lt;script&gt;", st.markdown.call_args.args[0])
        self.assertNotIn("<script>", st.markdown.call_args.args[0])

    def test_retrieval_filter_widgets_hidden_and_language_keys_kept(self):
        page = function("page_retrieval_settings")
        # V3.9.1：知识板块/文档类型/归属部门/可见密级四项筛选暂时隐藏，设置页不得再渲染这些控件。
        hidden_keys = {"set_domain", "set_doc_type", "set_department", "set_classification"}
        rendered_keys = {ast.literal_eval(k.value) for role in ("selectbox", "text_input")
                         for node in calls(page, role)
                         for k in node.keywords if k.arg == "key"}
        leaked = hidden_keys & rendered_keys
        self.assertFalse(leaked, f"隐藏的筛选控件仍被渲染：{sorted(leaked)}")
        keys = {"common.doc_type", "doc_type.unclassified", "doc_type.help", "doc_type.default",
                "doc_type.default.help", "doc_type.save", "doc_type.filter.optional", "doc_type.filter.help"}
        for lang in LANGUAGE_ORDER:
            self.assertTrue(keys <= _CATALOG[lang].keys(), lang)
            self.assertTrue(all(_CATALOG[lang][key] for key in keys), lang)


class DocumentTypeSettingsTests(unittest.TestCase):
    def test_settings_normalization_does_not_split_free_text(self):
        # V3.9.1：四项检索筛选已隐藏，不再出现在持久化键里；归一化仍容忍旧文件中的历史值。
        self.assertNotIn("doc_type_choice", SETTINGS_KEYS)
        for value, expected in (("  自定义 技术方案  ", "自定义 技术方案"), ("", ""), (None, ""),
                                (" all ", ""), ("ALL", ""), (" __unclassified__ ", "__unclassified__"),
                                ("x" * 80, "x" * 80), ("x" * 81, ""), (12, ""), ([], ""),
                                ("bad\nvalue", "")):
            self.assertEqual(_normalize_choices({"doc_type_choice": value})["doc_type_choice"], expected)

    def test_build_filters_ignores_hidden_filter_keys(self):
        st = SimpleNamespace(session_state=dict(domain_choice="learning", scope_choice="archive",
                                                doc_type_choice=" 技术方案 ", department_choice="财务部",
                                                classification_choice="受限"))
        env = load_functions(["build_filters"], dict(st=st, normalize_doc_type_filter=normalize_doc_type_filter,
                                                     normalize_department_filter=normalize_department_filter,
                                                     domain_reg=SimpleNamespace(sanitize_choice=lambda value: value)))
        # V3.9.1：四项筛选已隐藏——旧会话值即使存在也不再参与过滤；检索范围（status）保留。
        self.assertEqual(env["build_filters"](), {"status": "archive"})

    def test_hidden_filter_setting_is_not_persisted_by_unrelated_save(self):
        self.addCleanup(isolate_app_settings("zh-CN"))
        # V3.9.1：隐藏的筛选键不再写入全局 settings.json；语言/主题同属个人偏好也不进全局文件
        # （PerUserThemePersistTests 专门覆盖后者）。
        st = SimpleNamespace(session_state={"doc_type_choice": "技术方案", "theme": "system"})
        env = load_functions(["persist_app_settings"],
                             dict(st=st, SETTINGS_KEYS=SETTINGS_KEYS, load_app_settings=load_app_settings,
                                  save_app_settings=save_app_settings, get_installed_plugins=lambda: []))
        env["persist_app_settings"]()
        settings = load_app_settings()
        self.assertNotIn("doc_type_choice", settings)
        self.assertNotIn("theme", settings)

    def test_settings_persist_in_isolated_file(self):
        self.addCleanup(isolate_app_settings("zh-CN"))
        save_app_settings({"doc_type_choice": " 技术方案 ", "domain_choice": "learning"})
        self.assertEqual(load_app_settings(), {"doc_type_choice": "技术方案", "domain_choice": "learning"})


class DocumentTypeUploadContractTests(IngestHarness):
    """Execute real frontend functions with real writers only in the temporary tree."""
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.addCleanup(isolate_app_settings("zh-CN"))
        from src import index_state
        from src.upload_plan import content_hash, execute_uploads, plan_uploads, write_directory_import
        self.st = mock.MagicMock()
        self.st.session_state = {"auth_user": {"username": "fake"}}
        self.ingest_mock = mock.Mock(return_value=object())
        from src import projects, auth_db

        self.project_id = projects.save_project(
            projects.ProjectFields("低空试点", None, None, [], "")
        )["id"]
        self.env = load_functions(
            ["_validate_upload_visibility", "_clean_directory_name", "_upload_files", "_upload_signature", "_plan_frontend_upload", "_start_upload",
             "_render_upload_confirmation", "_run_upload", "_do_directory_upload", "_uploader_username"],
            dict(st=self.st, cfg=self.cfg, Path=Path, re=__import__("re"), projects=projects, auth_db=auth_db,
                 IngestItem=IngestItem, _new_ingest_audit=mock.MagicMock(),
                 normalize_doc_type=normalize_doc_type, normalize_department=normalize_department,
                 normalize_doc_type_filter=normalize_doc_type_filter,
                 normalize_department_filter=normalize_department_filter,
                 content_hash=content_hash, istate=index_state,
                 _upload_blocked_paths=lambda ledger: frozenset(),
                 plan_uploads=plan_uploads, execute_uploads=execute_uploads,
                 write_directory_import=write_directory_import, ingest_files=self.ingest_mock,
                 _show_ingest_result=mock.Mock(), SUPPORTED_EXTENSIONS={".md", ".txt"},
                 LLMError=RuntimeError, VectorStoreError=LookupError, EmbeddingError=ArithmeticError,
                 t=lambda key, **kwargs: key, _conflict_reason=lambda value: "conflict",
                 _action_label=lambda value: value))

    def test_upload_signature_invalidates_changed_default_and_target(self):
        signature = self.env["_upload_signature"]
        files = [("note.md", b"body")]
        original = signature(files, "learning/general", "技术方案")
        self.assertNotEqual(original, signature(files, "learning/general", "需求文档"))
        self.assertNotEqual(original, signature(files, "work/general", "技术方案"))
        self.assertNotEqual(original, signature(files, "learning/general", "技术方案", default_project_id=7))
        self.assertEqual(original, signature(files, "learning/general", " 技术方案 "))
        self.assertEqual(signature(files, "learning/general", None), signature(files, "learning/general", "  "))
        upload = SimpleNamespace(name="note.md", getvalue=lambda: b"body")
        self.st.session_state["upload_pending"] = original
        self.env["_render_upload_confirmation"]([upload], "learning/general", ["all"], default_doc_type="需求文档")
        self.assertNotIn("upload_pending", self.st.session_state)
        self.ingest_mock.assert_not_called()
        self.st.radio.assert_not_called()

    def test_new_and_renamed_files_forward_only_default_type(self):
        # V3.9.2：未关联项目的上传落 basic_knowledge/<登录用户>/，本类桩的登录用户是 fake
        target = "learning/basic_knowledge/fake"
        old = self.knowledge / target / "note.md"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"existing")
        files = [("note.md", b"new body")]
        plan = self.env["plan_uploads"](files, target, {}, self.knowledge)
        self.env["_run_upload"](files, plan, {0: "copy"}, ["all"], default_doc_type=" 技术方案 ")
        kwargs = self.ingest_mock.call_args.kwargs
        self.assertEqual(kwargs["default_doc_type"], "技术方案")
        self.assertEqual(kwargs["default_groups"], ["all"])
        self.assertTrue(kwargs["skip_unchanged"])
        self.assertNotIn("doc_type_paths", kwargs)
        self.assertEqual(kwargs["paths"][0].name, "note-2.md")

    def test_file_and_directory_upload_forward_selected_project(self):
        upload = SimpleNamespace(name="new.md", getvalue=lambda: b"file body")
        self.env["_start_upload"]([upload], "projects", ["all"], default_project_id=self.project_id)
        self.assertEqual(self.ingest_mock.call_args.kwargs["default_project_id"], self.project_id)
        self.assertTrue((self.knowledge / "projects/dikongshidian/new.md").is_file())
        self.env["_do_directory_upload"]([(("folder", "dir.md"), b"directory body")],
                                         "projects", "batch", ["all"], default_project_id=self.project_id)
        self.assertEqual(self.ingest_mock.call_args.kwargs["default_project_id"], self.project_id)
        self.assertTrue((self.knowledge / "projects/dikongshidian/folder/dir.md").is_file())

    def test_skipped_duplicate_is_not_reingested_or_retyped(self):
        path = self.write_note("same", "note.md")
        data = path.read_bytes()
        digest = self.env["content_hash"](data)
        plan = self.env["plan_uploads"]([("note.md", data)], "learning/public",
                                       {"learning/general/note.md": {"hash": digest}}, self.knowledge)
        self.env["_run_upload"]([("note.md", data)], plan, {}, ["all"], default_doc_type="技术方案")
        self.ingest_mock.assert_not_called()
        self.assertEqual(path.read_bytes(), data)

    def test_blank_default_remains_optional(self):
        upload = SimpleNamespace(name="fresh.md", getvalue=lambda: b"body")
        self.env["_start_upload"]([upload], "learning", ["all"], default_doc_type="  ")
        self.assertIsNone(self.ingest_mock.call_args.kwargs["default_doc_type"])
        self.assertTrue((self.knowledge / "learning/basic_knowledge/fake/fresh.md").is_file())

    def test_invalid_default_does_not_write_files(self):
        upload = SimpleNamespace(name="fresh.md", getvalue=lambda: b"body")
        self.env["_start_upload"]([upload], "learning/general", ["all"], default_doc_type="x" * 81)
        self.ingest_mock.assert_not_called()
        self.assertFalse((self.knowledge / "learning/general/fresh.md").exists())

    def test_directory_original_paths_follow_writer_order_in_merge_and_copy(self):
        # Interleaved roots, root cleanup, nested type folder; unsupported file is
        # NOT copied and NOT in the writer mapping (2026-11-28 决策6，报告 G5).
        entries = [((" 技术方案 ", "nested", "a.md"), b"a"),
                   (("会议纪要", "b.md"), b"b"),
                   ((" 技术方案 ", "需求文档", "c.md"), b"c"),
                   (("会议纪要", "ignored.bin"), b"ignored")]
        for merge in (True, False):
            self.ingest_mock.reset_mock()
            self.env["_do_directory_upload"](entries, "learning", "batch", ["all"], merge=merge,
                                              default_doc_type=" 自定义类型 ")
            kwargs = self.ingest_mock.call_args.kwargs
            actual_root = "技术方案" if merge else "技术方案-2"
            other_root = "会议纪要" if merge else "会议纪要-2"
            expected = {
                str((self.knowledge / "learning/basic_knowledge/fake" / actual_root / "nested/a.md").resolve()): " 技术方案 /nested/a.md",
                str((self.knowledge / "learning/basic_knowledge/fake" / actual_root / "需求文档/c.md").resolve()): " 技术方案 /需求文档/c.md",
                str((self.knowledge / "learning/basic_knowledge/fake" / other_root / "b.md").resolve()): "会议纪要/b.md",
            }
            self.assertEqual(kwargs["doc_type_paths"], expected)
            self.assertEqual(len(kwargs["paths"]), 3)
            self.assertTrue(all(str(path.resolve()) in expected for path in kwargs["paths"]))
            self.assertEqual(kwargs["default_doc_type"], "自定义类型")
            self.assertTrue(kwargs["skip_unchanged"])
            self.assertEqual(kwargs["default_groups"], ["all"])
            # 决策6：不支持的文件不落盘
            self.assertFalse((self.knowledge / f"learning/basic_knowledge/fake/{other_root}/ignored.bin").exists())


@unittest.skipIf(os.name == "nt", "Windows AppTest sockets are blocked by test network isolation")
class DocumentTypeAppTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.addCleanup(isolate_app_settings("zh-CN"))
        import streamlit as st
        st.cache_resource.clear()
        st.cache_data.clear()
        self.documents = [document(doc_type="技术方案"),
                          document(document_id="doc-b", source="other.md", path="learning/general/other.md", doc_type="")]
        self.update = mock.Mock(return_value={"message": "saved"})
        self.page = "manage"
        real_page = st.Page
        for patch in (mock.patch.object(self.store, "list_documents", side_effect=lambda: self.documents),
                      mock.patch("src.manage.update_metadata", self.update),
                      mock.patch("src.auth_client.bootstrap", return_value={"has_users": True, "has_admins": True, "departments": []}),
                      mock.patch("src.auth_client.get_prefs", return_value={}),
                      mock.patch("src.auth_client.save_prefs"),
                      mock.patch("src.directory_uploader.directory_drop_uploader", return_value=None),
                      mock.patch("streamlit.Page", side_effect=lambda page, *args, **kwargs:
                                 real_page(page, *args, **dict(kwargs, default=kwargs.get("url_path") == self.page)))):
            patch.start()
            self.addCleanup(patch.stop)

    def render(self, at):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            at.run()
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
        return at

    def open_page(self):
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file(str(APP), default_timeout=30)
        at.query_params["rip"] = "1"
        at.session_state["auth_user"] = dict(username="fake", department="hr", permissions=["manage", "retrieval_settings"])
        return self.render(at)

    def test_filter_column_and_independent_manual_clear(self):
        at = self.open_page()
        self.assertEqual(list(at.dataframe[0].value["文档类型"]), ["技术方案", "未分类"])
        at.selectbox(key="manage_doc_type_select").select(("unclassified", ""))
        self.render(at)
        self.assertEqual(list(at.dataframe[0].value["_id"]), ["doc-b"])
        at.session_state["manage_selected_documents"] = ["doc-b"]
        self.render(at)
        next(widget for widget in at.text_input
             if widget.key and widget.key.startswith("ed_doc_type_")).input(" ")
        next(widget for widget in at.button if widget.label == "保存文档类型").click()
        self.render(at)
        self.update.assert_called_once_with(self.documents[1]["path"], {"doc_type": ""})

class PerUserThemePersistTests(unittest.TestCase):
    """V3.9.1：语言/外观主题改为个人偏好——只进当前用户 prefs，不再写入全局文件。"""

    def test_language_theme_stay_per_user_not_global(self):
        self.addCleanup(isolate_app_settings("zh-CN"))
        st = SimpleNamespace(session_state={"language": "en", "theme": "dark", "top_k": 5,
                                            "auth_user": {"username": "u1"}})
        captured = []
        env = dict(st=st, SETTINGS_KEYS=SETTINGS_KEYS,
                   load_app_settings=load_app_settings, save_app_settings=save_app_settings,
                   get_installed_plugins=lambda: [],
                   auth_client=SimpleNamespace(save_prefs=lambda u, p: captured.append((u, p))))
        funcs = load_functions(["persist_app_settings"], env)
        funcs["persist_app_settings"]()
        settings = load_app_settings()
        self.assertNotIn("language", settings)
        self.assertNotIn("theme", settings)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], "u1")
        self.assertEqual(captured[0][1]["language"], "en")
        self.assertEqual(captured[0][1]["theme"], "dark")


if __name__ == "__main__":
    unittest.main()

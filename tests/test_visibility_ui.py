"""Visibility UI contracts using AST extraction and in-memory widgets (no AppTest/network)."""
from __future__ import annotations

import ast
import hashlib
import types
import unittest
from pathlib import Path
from typing import get_args
from unittest import mock

from streamlit.elements.lib.dialog import DialogWidth
from src.ingest_audit import IngestItem

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app.py"
TREE = ast.parse(APP.read_text(encoding="utf-8"))
FUNCTIONS = {node.name: node for node in TREE.body if isinstance(node, ast.FunctionDef)}


class FakeStreamlit:
    def __init__(self):
        self.session_state = {}
        self.values = {}
        self.clicked = set()
        self.events = []
        self.button_callbacks = {}

    def dialog(self, title, **kwargs):
        self.events.append(("dialog", title, kwargs))
        return lambda func: func

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def container(self, **kwargs):
        self.events.append(("container", kwargs))
        return self

    def form(self, **kwargs):
        return self

    def columns(self, count):
        return [self] * count

    def radio(self, label, options, index=0, key=None, **kwargs):
        self.events.append(("radio", key, list(options), index))
        return self.values.get(key, self.session_state.get(key, options[index]))

    def multiselect(self, label, options, default=None, key=None, **kwargs):
        self.events.append(("multiselect", key, list(options), list(default or []), kwargs))
        return self.values.get(key, default or [])

    def text_input(self, label, key=None, **kwargs):
        return self.values.get(key, "")

    def checkbox(self, label, value=False, key=None, **kwargs):
        self.events.append(("checkbox", label, key))
        return self.values.get(key, self.session_state.get(key, value))

    def button(self, label, key=None, disabled=False, **kwargs):
        self.events.append(("button", key, disabled))
        if kwargs.get("on_click"):
            self.button_callbacks[key] = (kwargs["on_click"], kwargs.get("args", ()))
        return not disabled and key in self.clicked

    def form_submit_button(self, label, disabled=False, **kwargs):
        self.events.append(("submit", label, disabled))
        return not disabled and "submit" in self.clicked

    def rerun(self):
        self.events.append(("rerun",))

    def __getattr__(self, method):
        return lambda *args, **kwargs: self.events.append((method, *args))


def translated(key, **kwargs):
    return key + (" " + str(kwargs) if kwargs else "")


def load_functions(st, auth_db, names=None, **extra):
    if names is None:
        names = [name for name in FUNCTIONS if name.startswith("_visibility_")]
        names += ["_open_visibility_employee_picker", "visibility_employee_picker",
                  "render_visibility_selector", "_validate_upload_visibility", "_upload_signature"]
    module = ast.Module(body=[FUNCTIONS[name] for name in names], type_ignores=[])
    namespace = {
        "st": st, "auth_db": auth_db, "t": translated,
        "DialogWidth": DialogWidth, "get_args": get_args,
        "IngestItem": IngestItem, "_new_ingest_audit": mock.MagicMock(),
        "normalize_doc_type": lambda value: str(value).strip(),
        "normalize_department": lambda value: str(value).strip(),
        "content_hash": lambda value: hashlib.sha256(value).hexdigest(),
        **extra,
    }
    exec(compile(module, str(APP), "exec"), namespace)
    return namespace


class VisibilitySelectorTests(unittest.TestCase):
    def setUp(self):
        self.st = FakeStreamlit()
        self.st.session_state["auth_user"] = {"department": "Renamed HR", "department_id": 8}
        self.departments = [
            {"name": "Engineering", "permission_key": "engineering"},
            {"name": "Renamed HR", "permission_key": "hr-original"},
        ]
        self.employees = [
            {"username": "alice", "real_name": "Alice", "department": "Engineering", "department_id": 7,
             "status": "active"},
            {"username": "bob", "real_name": "Bob", "department": "Renamed HR", "department_id": 8,
             "status": "active"},
            {"username": "disabled", "department": "Engineering", "department_id": 7, "status": "disabled"},
        ]
        self.db = types.SimpleNamespace(
            list_departments=mock.Mock(side_effect=lambda **kwargs: self.departments),
            department_permission_key=mock.Mock(return_value="hr-original"),
            department_label=lambda value: value,
            list_users=mock.Mock(side_effect=lambda: self.employees),
        )
        self.ns = load_functions(self.st, self.db)
        self.selector = self.ns["render_visibility_selector"]

    def test_internal_defaults_to_own_immutable_key_and_all_enabled_options(self):
        self.assertEqual(self.selector("upload"), {
            "allowed_groups": ["hr-original"], "allowed_users": [], "classification": "内部"})
        event = next(e for e in self.st.events if e[0] == "multiselect")
        self.assertEqual(event[2], ["engineering", "hr-original"])
        self.assertEqual(event[3], ["hr-original"])
        self.db.list_departments.assert_called_once_with(include_disabled=False)
        self.db.department_permission_key.assert_called_once_with("Renamed HR", 8)
        self.assertEqual(next(e for e in self.st.events if e[0] == "radio")[3], 1)

    def test_can_select_other_enabled_department(self):
        self.st.values["upload_allowed_groups"] = ["engineering"]
        self.assertEqual(self.selector("upload")["allowed_groups"], ["engineering"])

    def test_public_is_only_all_and_clears_employee_channel(self):
        self.st.values["upload_visibility"] = "public"
        self.st.session_state["upload_allowed_users"] = ["alice"]
        self.assertEqual(self.selector("upload"), {
            "allowed_groups": ["all"], "allowed_users": [], "classification": "公开"})

    def test_no_department_never_defaults_to_all_or_first_option(self):
        self.db.department_permission_key.return_value = None
        self.assertIsNone(self.selector("upload"))
        self.assertEqual(next(e for e in self.st.events if e[0] == "multiselect")[3], [])
        self.assertTrue(any(e[0] == "error" for e in self.st.events))

    def test_no_enabled_departments_is_disabled(self):
        self.departments.clear()
        self.assertIsNone(self.selector("upload"))
        self.assertTrue(next(e for e in self.st.events if e[0] == "multiselect")[4]["disabled"])

    def test_empty_internal_selection_invalid(self):
        self.st.values["upload_allowed_groups"] = []
        self.assertIsNone(self.selector("upload"))

    def test_empty_restricted_selection_invalid_and_never_all(self):
        self.st.values["upload_visibility"] = "restricted"
        self.assertIsNone(self.selector("upload"))
        self.assertEqual(self.st.events[-5:], [
            ("container", {"horizontal": True, "vertical_alignment": "center"}),
            ("button", "upload_choose_employees", False),
            ("container", {"width": "content", "gap": None}),
            ("caption", translated("visibility.employees.selected", n=0)),
            ("caption", ":red[visibility.restricted.required]"),
        ])

    def test_restricted_stores_usernames_and_no_departments(self):
        self.st.values["upload_visibility"] = "restricted"
        self.st.session_state["upload_allowed_users"] = ["alice", "bob"]
        self.assertEqual(self.selector("upload"), {
            "allowed_groups": [], "allowed_users": ["alice", "bob"], "classification": "受限"})
        self.assertIn(("caption", translated("visibility.employees.selected", n=2)), self.st.events)
        self.assertNotIn(("caption", ":red[visibility.restricted.required]"), self.st.events)

    def test_edit_reads_actual_acl_not_stale_classification(self):
        current = {"allowed_groups": [], "allowed_users": ["bob"], "classification": "公开"}
        self.assertEqual(self.selector("edit", current)["classification"], "受限")
        self.assertEqual(next(e for e in self.st.events if e[0] == "radio")[3], 2)

    def test_empty_existing_acl_stays_restricted_and_invalid(self):
        self.assertIsNone(self.selector("edit", {"allowed_groups": [], "allowed_users": []}))
        self.assertEqual(next(e for e in self.st.events if e[0] == "radio")[3], 2)

    def test_edit_preserves_existing_orphan_department(self):
        current = {"allowed_groups": ["retired-department"], "allowed_users": []}
        self.assertEqual(self.selector("edit", current)["allowed_groups"], ["retired-department"])

    def open_picker(self, selected=None):
        self.st.session_state["upload_allowed_users"] = list(selected or [])
        self.ns["_open_visibility_employee_picker"]("upload")
        return self.st.session_state["visibility_picker_request"]["prefix"]

    def test_real_dialog_decorator_and_department_checkbox_sections(self):
        self.open_picker()
        expected_width = 1120 if int in get_args(DialogWidth) else "medium"
        self.assertTrue(any(e[0] == "dialog" and e[2].get("width") == expected_width for e in self.st.events))
        self.assertEqual([e[1] for e in self.st.events if e[0] == "subheader"],
                         ["Engineering", "Renamed HR"])
        checkboxes = [e for e in self.st.events if e[0] == "checkbox"]
        self.assertEqual(len(checkboxes), 2)
        self.assertFalse(any("disabled" in e[2] for e in checkboxes))

    def test_department_toggle_includes_search_hidden_active_employees_and_needs_confirm(self):
        self.employees.append({"username": "cara", "department": "Engineering", "department_id": 7,
                               "status": "active"})
        prefix = self.open_picker(["bob"])
        self.st.values[f"{prefix}_search"] = "alice"
        alice_key = f"{prefix}_employee_alice"
        self.st.session_state[alice_key] = False
        self.ns["visibility_employee_picker"]()

        toggle, args = self.st.button_callbacks[f"{prefix}_department_7"]
        toggle(*args)
        self.ns["visibility_employee_picker"]()
        self.assertTrue(self.st.session_state[alice_key])
        self.assertEqual(self.st.session_state["visibility_picker_request"]["draft"], ["alice", "bob", "cara"])
        self.assertEqual(self.st.session_state["upload_allowed_users"], ["bob"])

        toggle(*args)
        self.ns["visibility_employee_picker"]()
        self.assertFalse(self.st.session_state[alice_key])
        self.assertEqual(self.st.session_state["visibility_picker_request"]["draft"], ["bob"])

        toggle(*args)
        self.st.clicked.add(f"{prefix}_confirm")
        self.ns["visibility_employee_picker"]()
        self.assertEqual(self.st.session_state["upload_allowed_users"], ["alice", "bob", "cara"])

    def test_search_preserves_hidden_selection_and_confirm_applies_only_usernames(self):
        prefix = self.open_picker(["alice"])
        self.st.values[f"{prefix}_search"] = "bob"
        self.st.values[f"{prefix}_employee_bob"] = True
        self.ns["visibility_employee_picker"]()
        self.assertEqual(self.st.session_state["upload_allowed_users"], ["alice"])
        self.assertEqual(self.st.session_state["visibility_picker_request"]["draft"], ["alice", "bob"])
        self.st.clicked.add(f"{prefix}_confirm")
        self.ns["visibility_employee_picker"]()
        self.assertEqual(self.st.session_state["upload_allowed_users"], ["alice", "bob"])
        self.assertNotIn("visibility_picker_request", self.st.session_state)

    def test_cancel_discards_draft_and_reopen_uses_confirmed_selection(self):
        prefix = self.open_picker(["alice"])
        self.st.values[f"{prefix}_employee_bob"] = True
        self.st.clicked.add(f"{prefix}_cancel")
        self.ns["visibility_employee_picker"]()
        self.assertEqual(self.st.session_state["upload_allowed_users"], ["alice"])
        new_prefix = self.open_picker(["alice"])
        self.assertNotEqual(prefix, new_prefix)
        self.assertEqual(self.st.session_state["visibility_picker_request"]["draft"], ["alice"])

    def test_checkbox_callback_preserves_selection_when_search_hides_widget(self):
        prefix = self.open_picker()
        key = f"{prefix}_employee_alice"
        self.st.session_state[key] = True
        self.ns["_visibility_employee_changed"](key, "alice")
        self.st.values[f"{prefix}_search"] = "no match"
        self.ns["visibility_employee_picker"]()
        self.assertEqual(self.st.session_state["visibility_picker_request"]["draft"], ["alice"])
        self.assertEqual(self.st.session_state["upload_allowed_users"], [])

    def test_empty_picker_confirm_disabled(self):
        prefix = self.open_picker()
        self.assertIn(("button", f"{prefix}_confirm", True), self.st.events)

    def test_signature_changes_for_either_acl_channel_and_preserves_first_args(self):
        signature = self.ns["_upload_signature"]
        args = ([("a.md", b"data")], "learning", "spec", "HR", 12)
        original = signature(*args, groups=["hr"], users=[])
        self.assertNotEqual(original, signature(*args, groups=["engineering"], users=[]))
        self.assertNotEqual(original, signature(*args, groups=[], users=["alice"]))
        self.assertNotEqual(signature(*args, groups=[], users=["alice"]),
                            signature(*args, groups=[], users=["bob"]))
        self.assertNotEqual(signature(*args, groups=None, users=None), signature(*args, groups=[], users=[]))
        self.assertEqual(signature(*args, groups=["b", "a"], users=[]),
                         signature(*args, groups=["a", "b"], users=[]))

    def test_stale_confirmation_invalidated_by_group_or_employee_change(self):
        files = [("a.md", b"data")]
        signature = self.ns["_upload_signature"]
        self.ns = load_functions(
            self.st, self.db, names=["_render_upload_confirmation"],
            _upload_files=lambda uploads: files, _upload_signature=signature,
            _validate_upload_visibility=lambda groups, users: None,
        )
        render = self.ns["_render_upload_confirmation"]
        for old_groups, old_users, groups, users in (
            (["hr"], [], ["engineering"], []),
            ([], ["alice"], [], ["bob"]),
        ):
            self.st.session_state["upload_pending"] = signature(
                files, "learning", groups=old_groups, users=old_users)
            render([], "learning", groups, default_allowed_users=users)
            self.assertNotIn("upload_pending", self.st.session_state)

    def test_upload_gate_rejects_deny_all_and_accepts_valid_acl(self):
        validate = self.ns["_validate_upload_visibility"]
        with self.assertRaises(ValueError):
            validate([], [])
        validate(["all"], [])
        validate(["engineering"], [])
        validate([], ["alice"])


class VisibilityWiringTests(unittest.TestCase):
    def test_upload_directory_web_use_one_selector_and_explicit_lists(self):
        page = FUNCTIONS["page_upload"]
        calls = [n for n in ast.walk(page) if isinstance(n, ast.Call)]
        selector_calls = [n for n in calls if isinstance(n.func, ast.Name) and n.func.id == "render_visibility_selector"]
        self.assertEqual([n.args[0].value for n in selector_calls], ["upload", "dir", "web"])
        for name in ("_start_upload", "_render_upload_confirmation", "_do_directory_upload", "ingest_web_page"):
            call = next(n for n in calls if isinstance(n.func, ast.Name) and n.func.id == name)
            users = next(k.value for k in call.keywords if k.arg == "default_allowed_users")
            self.assertIsInstance(users, ast.Name, "[] must never become None by 'or None'")
        for call in calls:
            if isinstance(call.func, ast.Attribute) and call.func.attr == "button":
                if call.args and isinstance(call.args[0], ast.Call):
                    label = call.args[0].args[0].value
                    if label in ("upload.start", "dir.start", "web.ingest"):
                        disabled = next(k.value for k in call.keywords if k.arg == "disabled")
                        self.assertIn("acl is None", ast.unparse(disabled))

    def test_existing_upload_signatures_keep_positions_and_new_keyword_only_users(self):
        expected = {
            "_start_upload": ["uploads", "target_dir", "groups"],
            "_render_upload_confirmation": ["uploads", "target_dir", "groups"],
            "_run_upload": ["files", "plan", "choices", "groups"],
            "_do_directory_upload": ["entries", "dir_domain", "batch_id", "groups", "merge"],
        }
        for name, positional in expected.items():
            self.assertEqual([a.arg for a in FUNCTIONS[name].args.args], positional)
            self.assertIn("default_allowed_users", [a.arg for a in FUNCTIONS[name].args.kwonlyargs])

    def test_ingest_forwarding_preserves_groups_users_project_and_department(self):
        for name in ("_run_upload", "_do_directory_upload"):
            calls = [n for n in ast.walk(FUNCTIONS[name]) if isinstance(n, ast.Call)]
            call = next(n for n in calls if isinstance(n.func, ast.Name) and n.func.id == "ingest_files")
            kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
            self.assertEqual(kw["default_groups"], "groups")
            self.assertEqual(kw["default_allowed_users"], "default_allowed_users")
            self.assertEqual(kw["default_project_id"], "default_project_id")
            self.assertEqual(kw["default_department"], "default_department")
            gate = next(n for n in ast.walk(FUNCTIONS[name]) if isinstance(n, ast.Try))
            first_call = next(n for n in ast.walk(gate.body[0]) if isinstance(n, ast.Call))
            self.assertEqual(ast.unparse(first_call.func), "_validate_upload_visibility")

    def test_upload_executes_with_explicit_restricted_defaults(self):
        st = FakeStreamlit()
        st.session_state["auth_user"] = {"username": "owner"}
        st.spinner = lambda *args, **kwargs: st
        ingest = mock.Mock(return_value={"message": "done"})
        project_validator = mock.Mock()
        show = mock.Mock()
        ns = load_functions(
            st, types.SimpleNamespace(), names=["_run_upload"],
            _validate_upload_visibility=mock.Mock(),
            _upload_blocked_paths=lambda ledger: frozenset(),
            istate=types.SimpleNamespace(load_state=dict),
            projects=types.SimpleNamespace(validate_project_id=project_validator),
            cfg=types.SimpleNamespace(knowledge_dir=Path("isolated")),
            execute_uploads=mock.Mock(return_value=([Path("isolated/a.md")], [])),
            ingest_files=ingest, _show_ingest_result=show,
            LLMError=RuntimeError, VectorStoreError=RuntimeError, EmbeddingError=RuntimeError,
        )
        ns["_run_upload"]([], [], {}, [], default_doc_type="spec", default_department="HR",
                          default_project_id=17, default_allowed_users=["alice"])
        self.assertEqual(ingest.call_args.kwargs["default_groups"], [])
        self.assertEqual(ingest.call_args.kwargs["default_allowed_users"], ["alice"])
        self.assertEqual(ingest.call_args.kwargs["default_department"], "HR")
        self.assertEqual(ingest.call_args.kwargs["default_project_id"], 17)
        self.assertEqual(ingest.call_args.kwargs["owner"], "owner")
        project_validator.assert_called_once_with(17)
        show.assert_called_once()

    def test_permission_save_is_independent_and_only_writes_two_acl_fields(self):
        # Execute only the permission container from the detail page; no app imports or services.
        block = next(n for n in ast.walk(TREE) if isinstance(n, ast.With)
                     and any(isinstance(x, ast.Name) and x.id == "permission_acl" for x in ast.walk(n)))
        st = FakeStreamlit()
        st.clicked.add("submit")
        update = mock.Mock(return_value={"message": "saved"})
        acl = {"allowed_groups": [], "allowed_users": ["alice"], "classification": "受限"}
        namespace = {"st": st, "t": translated, "_department_suffix": "doc_v1",
                     "d": {"path": "isolated.md"}, "update_metadata": update,
                     "render_visibility_selector": mock.Mock(return_value=acl),
                     "ManageError": ValueError, "VectorStoreError": ValueError}
        exec(compile(ast.Module(body=[block], type_ignores=[]), str(APP), "exec"), namespace)
        update.assert_called_once_with("isolated.md", {"allowed_groups": [], "allowed_users": ["alice"]})
        generic = next(n for n in ast.walk(TREE) if isinstance(n, ast.Assign)
                       and any(isinstance(x, ast.Name) and x.id == "updates" for x in n.targets)
                       and isinstance(n.value, ast.Dict)
                       and any(isinstance(k, ast.Constant) and k.value == "title" for k in n.value.keys))
        self.assertNotIn("allowed_groups", [k.value for k in generic.value.keys])
        self.assertNotIn("allowed_users", [k.value for k in generic.value.keys])

    def test_invalid_acl_gates_before_file_writes_with_inline_mocks(self):
        st = FakeStreamlit()
        validator = mock.Mock(side_effect=ValueError("empty ACL"))
        execute = mock.Mock()
        write = mock.Mock()
        ns = load_functions(st, types.SimpleNamespace(), names=["_run_upload", "_do_directory_upload"],
                            _validate_upload_visibility=validator, execute_uploads=execute,
                            write_directory_import=write)
        ns["_run_upload"]([], [], {}, [], default_allowed_users=[])
        ns["_do_directory_upload"]([], "learning", "batch", [], default_allowed_users=[])
        execute.assert_not_called()
        write.assert_not_called()
        self.assertEqual(len([e for e in st.events if e[0] == "error"]), 2)

    def test_four_language_labels_complete(self):
        tree = ast.parse((ROOT / "src" / "i18n.py").read_text(encoding="utf-8"))
        catalog = next(n.value for n in tree.body if isinstance(n, ast.AnnAssign)
                       and isinstance(n.target, ast.Name) and n.target.id == "_CATALOG")
        catalog = ast.literal_eval(catalog)
        keys = {key for key in catalog["zh-CN"] if key.startswith("visibility.")}
        self.assertGreaterEqual(len(keys), 18)
        for language in ("zh-CN", "zh-TW", "en", "ja"):
            self.assertTrue(all(catalog[language].get(key) for key in keys))
            self.assertTrue(catalog[language]["upload.groups.internal.help"])


if __name__ == "__main__":
    unittest.main()

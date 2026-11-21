"""Real audit writes and front-end entry points with isolated SQLite and Qdrant."""
import contextlib
import io
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException, Request
from qdrant_client.http.exceptions import ApiException, ResponseHandlingException

from src import auth_db, business_directions, index_state, pipeline, projects
from src.document_department import normalize_department
from src.document_type import normalize_doc_type
from src.ingest_audit import IngestAudit, IngestItem, list_logs, source_url_for_log
from src.manage import ingest_web_page
from src.upload_plan import execute_uploads, plan_uploads, write_directory_import
from tests._helpers import IngestHarness
from tests.test_document_type_ui import load_functions


def admin_request(peer="127.0.0.1", host="localhost:8700"):
    return Request({"type": "http", "scheme": "http", "path": "/api/logs/ingest",
                    "headers": [(b"host", host.encode())], "client": (peer, 1234)})


class IngestAuditTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.addCleanup(self.store.client.close)
        self.st = mock.MagicMock()
        self.st.session_state = {"auth_user": {"username": "li", "department": "研发"}}
        self.st.context = SimpleNamespace(ip_address="192.0.2.11")
        self.browser_context = {
            "client_ip": "192.0.2.12", "accept_language": "zh-CN",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/123.0.0.0 Safari/537.36",
        }
        self.env = load_functions(
            ["_new_ingest_audit", "_plan_frontend_upload", "_validate_upload_visibility", "_clean_directory_name",
             "_run_upload", "_do_directory_upload"],
            dict(st=self.st, cfg=self.cfg, projects=projects, Path=Path, re=re, t=lambda key, **kwargs: key,
                 auth_client=SimpleNamespace(browser_context=lambda: self.browser_context),
                 IngestAudit=IngestAudit, IngestItem=IngestItem, normalize_doc_type=normalize_doc_type,
                 normalize_department=normalize_department, istate=index_state,
                 _upload_blocked_paths=mock.Mock(return_value=frozenset()), execute_uploads=execute_uploads,
                 plan_uploads=plan_uploads, ApiException=ApiException, _show_management_error=mock.Mock(),
                 write_directory_import=write_directory_import, ingest_files=pipeline.ingest_files,
                 _show_ingest_result=mock.Mock(), SUPPORTED_EXTENSIONS={".md"},
                 LLMError=RuntimeError, VectorStoreError=LookupError, EmbeddingError=ArithmeticError),
        )

    def upload(self, files, **kwargs):
        plan = self.env["_plan_frontend_upload"](
            files, "learning", ["all"], [], kwargs.get("default_doc_type"),
            kwargs.get("default_department"), kwargs.get("default_project_id"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.env["_run_upload"](files, plan, {}, ["all"], **kwargs)

    def logs(self, **kwargs):
        return list_logs(page=1, page_size=20, **kwargs)["logs"]

    def test_file_batch_records_real_attributes_and_confirmed_duplicate(self):
        direction = business_directions.create_direction("平台建设")
        project = projects.save_project(projects.ProjectFields("监管平台", direction["id"], None, [], ""))
        content = b"---\ndoc_type: report\n---\n# Inspection\n\nComplete."
        self.upload([("report.md", content), ("duplicate.md", content)],
                    default_doc_type="plan", default_department="研发", default_project_id=project["id"])
        rows = {row["file_name"]: row for row in self.logs(username="li", entry="file")}
        report, duplicate = rows["report.md"], rows["duplicate.md"]
        self.assertEqual(report["batch_id"], duplicate["batch_id"])
        self.assertEqual((report["outcome"], duplicate["outcome"]), ("ingested", "skipped"))
        self.assertEqual(duplicate["reason"], "batch_duplicate")
        self.assertEqual(report["path"], "learning/jianguanpingtai/report.md")
        self.assertEqual(report["ip"], "192.0.2.12")
        self.assertEqual(report["user_agent"], self.browser_context["user_agent"])
        self.assertEqual(report["browser"], "Chrome 123")
        self.assertEqual(report["os_name"], auth_db.parse_user_agent(self.browser_context["user_agent"])[1])
        self.assertEqual(report["fingerprint"], auth_db.make_fingerprint(
            self.browser_context["user_agent"], "zh-CN", "192.0.2.12"))
        self.assertEqual(report["actor_department"], "研发")
        self.assertEqual(report["bytes"], len(content))
        self.assertIsNotNone(report["finished_at"])
        self.assertEqual(report["requested"]["doc_type"], "plan")
        self.assertEqual(report["attributes"]["doc_type"], "report")
        self.assertEqual(report["attributes"]["business_direction"], "平台建设")
        self.assertEqual(report["attributes"]["allowed_groups"], ["all"])
        self.assertEqual(self.store.list_documents()[0]["project_id"], project["id"])
        since = datetime.fromisoformat(report["started_at"])
        sliced = list_logs(page=3, page_size=1, batch_id=report["batch_id"], keyword="REPORT",
                           since=since, before=since + timedelta(minutes=1))
        self.assertEqual((sliced["page"], sliced["total"]), (1, 1))
        self.assertEqual(sliced["logs"][0]["id"], report["id"])

    def test_upload_preflight_failure_is_audited_before_any_writes(self):
        fault = ResponseHandlingException(ConnectionRefusedError("database unreachable"))
        self.env["_upload_blocked_paths"].side_effect = fault
        result = self.env["_plan_frontend_upload"]([("note.md", b"note")], "learning",
                                                  ["all"], [], None, None, None)
        self.assertIsNone(result)
        self.env["_show_management_error"].assert_called_once_with(fault)
        row, = self.logs(entry="file", outcome="failed")
        self.assertEqual((row["source_path"], row["reason"]), ("note.md", "preflight"))
        self.assertFalse((self.knowledge / "learning/public/note.md").exists())

    def test_old_unassigned_document_updates_in_place(self):
        original = self.knowledge / "learning/old.md"
        original.write_bytes(b"# Old\n\nBefore the new storage layout.")
        self.ingest([original])
        self.upload([("old.md", b"# Old\n\nThe next version of the same document.")])
        row, = self.logs()
        self.assertEqual((row["path"], row["reason"]), ("learning/old.md", "new_revision"))
        self.assertFalse((self.knowledge / "learning/public").exists())

    def test_old_audit_table_migrates_and_invalid_browser_ip_uses_current_connection(self):
        self.upload([("old.md", b"# Old\n\nOriginally recorded before migration.")])
        old_id = self.logs()[0]["id"]
        with contextlib.closing(auth_db._connect()) as conn, conn:
            for field in ("user_agent", "browser", "os_name", "fingerprint"):
                conn.execute(f"ALTER TABLE ingest_logs DROP COLUMN {field}")

        self.browser_context["client_ip"] = "invalid address"
        self.upload([("new.md", b"# Current\n\nRecorded after migration.")])
        current, old = self.logs()
        self.assertEqual(current["ip"], "192.0.2.11")
        self.assertTrue(current["fingerprint"])
        self.assertEqual((old["id"], old["path"], old["fingerprint"]), (old_id, "learning/public/old.md", ""))

    def test_directory_logs_original_paths_and_per_file_failure(self):
        parse = pipeline.parse_file

        def parse_or_fail(path):
            if path.name == "broken.md":
                raise ValueError("bad format")
            return parse(path)

        with mock.patch.object(pipeline, "parse_file", side_effect=parse_or_fail), \
             contextlib.redirect_stdout(io.StringIO()):
            self.env["_do_directory_upload"]([
                (("team", "a.md"), b"# Supported\n\nIndexed text"),
                (("other", "broken.md"), b"bad data"),
                (("team", "extra.bin"), b"binary"),
            ], "learning", "batch", ["all"])
        rows = {row["file_name"]: row for row in self.logs(entry="directory")}
        self.assertEqual(rows["a.md"]["outcome"], "ingested")
        self.assertEqual(rows["broken.md"]["source_path"], "other/broken.md")
        self.assertEqual(rows["broken.md"]["path"], "learning/public/other/broken.md")
        self.assertEqual(rows["broken.md"]["reason"], "parse_failed")
        self.assertEqual(rows["extra.bin"]["reason"], "unsupported_type")
        self.assertEqual({d["path"] for d in self.store.list_documents()}, {"learning/public/team/a.md"})

    def test_web_logs_real_snapshot_and_fetch_failure_without_url_query(self):
        url = "https://example.invalid/article?access_token=hidden#fragment"
        for failure in (False, True):
            with mock.patch("src.web_fetcher.fetch_and_parse",
                            side_effect=OSError("network error") if failure else None,
                            return_value={"text": "Inspected article. " * 10, "title": "Report"}), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.env["_new_ingest_audit"](
                    "web", [IngestItem("", source_path=source_url_for_log(url))],
                    "learning", ["all"], [], None, None, None,
                ) as audit:
                    pending = self.logs(entry="web", batch_id=audit.batch_id)[0]
                    self.assertEqual(pending["outcome"], "running")
                    self.assertIsNone(pending["finished_at"])
                    if failure:
                        with self.assertRaises(OSError):
                            ingest_web_page(url, domain="learning", audit=audit)
                    else:
                        ingest_web_page(url, domain="learning", audit=audit)
        failed, ingested = self.logs(entry="web")
        self.assertEqual(failed["reason"], "fetch")
        self.assertEqual(ingested["outcome"], "ingested")
        self.assertTrue(ingested["path"].startswith("learning/public/"))
        self.assertTrue(ingested["path"].endswith(".md"))
        self.assertGreater(ingested["bytes"], 0)
        self.assertEqual(ingested["source_path"], "https://example.invalid/article")
        self.assertNotIn("hidden", str([failed, ingested]))

    def test_sqlite_audit_failure_does_not_block_ingestion(self):
        connect = auth_db._connect

        def deny_audit_only(**kwargs):
            if kwargs.get("timeout") == 1:
                raise sqlite3.OperationalError("database locked")
            return connect(**kwargs)

        with mock.patch.object(auth_db, "_connect", side_effect=deny_audit_only):
            with self.assertLogs("src.ingest_audit", level="ERROR"):
                self.upload([("indexed.md", b"# Indexed\n\nSearchable text")])
        self.assertEqual(self.logs(), [])
        self.assertEqual(self.store.list_documents()[0]["source"], "indexed.md")
        self.assertEqual(self.retrieve_hits("Searchable text"), [("indexed.md", "learning")])

    def test_ingest_log_api_requires_admin_and_filters_results(self):
        from src.admin_app import app, api_logs_ingest, require_admin_api

        auth_db.create_user("editor", "test-password", real_name="李明")
        auth_db.create_user("staff", "test-password")
        self.upload([("test.md", b"# Test\n\nAudited text")])
        route = next(route for route in app.routes if route.path == "/api/logs/ingest")
        self.assertIn(require_admin_api, [dep.call for dep in route.dependant.dependencies])
        with mock.patch("src.admin_app.get_config", return_value=self.cfg):
            result = api_logs_ingest(admin_request(), page=1, page_size=10, username="li", keyword="test", batch_id="",
                                     entry="file", outcome="ingested", since=datetime(2020, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["employee_options"], [
            {"username": "editor", "real_name": "李明"},
            {"username": "li", "real_name": ""},
            {"username": "staff", "real_name": ""},
        ])
        self.assertEqual(result["logs"][0]["absolute_path"], str((self.knowledge / "learning/public/test.md").resolve()))

    def test_ingest_log_api_labels_internal_permission_groups(self):
        from src.admin_app import api_logs_ingest

        department = auth_db.create_department("研发中心")
        item = IngestItem("test.md", attributes={
            "classification": "内部", "allowed_groups": [department["permission_key"]],
            "department": "交付中心",
        }, outcome="ingested")
        with self.env["_new_ingest_audit"]("file", [item], "learning", [], [], None, None, None):
            pass
        with mock.patch("src.admin_app.get_config", return_value=self.cfg):
            result = api_logs_ingest(admin_request(), page=1, page_size=10, username="li", keyword="", batch_id="")
        self.assertEqual(result["department_labels"][department["permission_key"]], "研发中心")
        self.assertEqual(result["logs"][0]["attributes"]["department"], "交付中心")

    def test_reveal_uses_only_logged_files_inside_the_local_knowledge_directory(self):
        from src.admin_app import api_reveal_ingest_file, app, require_admin_api

        self.upload([("test.md", b"# Title\n\nFile manager test.")])
        log_id = self.logs()[0]["id"]
        route = next(route for route in app.routes if route.path == "/api/logs/ingest/{log_id}/reveal")
        self.assertIn(require_admin_api, [dep.call for dep in route.dependant.dependencies])
        with mock.patch("src.admin_app.get_config", return_value=self.cfg), \
             mock.patch("src.admin_app.sys.platform", "win32"), \
             mock.patch("src.admin_app.os.startfile", create=True) as startfile:
            with self.assertRaises(HTTPException) as remote:
                api_reveal_ingest_file(admin_request(peer="192.0.2.4"), log_id)
            self.assertEqual(remote.exception.status_code, 403)
            with self.assertRaises(HTTPException) as forwarded:
                api_reveal_ingest_file(admin_request(host="192.0.2.4:8700"), log_id)
            self.assertEqual(forwarded.exception.status_code, 403)
            for unsafe_or_missing in ("../outside.md", "learning/missing.md", str(self.knowledge / "learning/public/test.md")):
                with mock.patch("src.ingest_audit.get_log_path", return_value=unsafe_or_missing):
                    with self.assertRaises(HTTPException) as unavailable:
                        api_reveal_ingest_file(admin_request(), log_id)
                    self.assertEqual(unavailable.exception.status_code, 404)
            startfile.assert_not_called()
            self.assertEqual(api_reveal_ingest_file(admin_request(), log_id), {"ok": True})
            startfile.assert_called_once_with(str((self.knowledge / "learning/public").resolve()), "open")
        with mock.patch("src.admin_app.get_config", return_value=self.cfg), \
             mock.patch("src.admin_app.sys.platform", "darwin"), mock.patch("subprocess.Popen") as finder:
            self.assertEqual(api_reveal_ingest_file(admin_request(), log_id), {"ok": True})
            self.assertEqual(finder.call_args.args[0],
                             ["/usr/bin/open", "-R", str((self.knowledge / "learning/public/test.md").resolve())])

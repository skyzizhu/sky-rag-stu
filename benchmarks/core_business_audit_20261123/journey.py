"""Isolated acceptance journey through Web application functions and MCP tools."""
from __future__ import annotations

import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from src import admin_app, auth_db, business_directions, dashboard_stats, manage, mcp_server, pipeline, projects
from src.permissions import scope_filters, visible_groups
from src.project_directories import INFO_NAME
from tests.test_document_type_ui import load_functions
from tests.test_ingest_audit import IngestAuditTests, admin_request

from benchmarks.e2e_workflow_audit_20261123.run import FixtureLLM


class BusinessJourney(IngestAuditTests):
    observations = {}

    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, rerank_enabled=False, query_understanding=False)
        self.direction = business_directions.create_direction("合成交付业务")
        self.projects = {
            name: projects.save_project(projects.ProjectFields(name, self.direction["id"], None, [], ""))
            for name in ("北辰交付", "南星交付")
        }
        r_and_d = auth_db.create_department("合成研发部")
        operations = auth_db.create_department("合成运营部")
        self.staff = {}
        for name, department in (("audit_alice", r_and_d), ("audit_bob", r_and_d), ("audit_carol", operations)):
            user = auth_db.create_user(name, "FixtureOnly123", department_id=department["id"], real_name=name)
            self.staff[name] = (user, department,
                                auth_db.create_token(user["id"], "isolated-business-journey"))
        self.author = "audit_alice"
        self.st.session_state["auth_user"] = {"username": self.author, "department": r_and_d["name"]}
        self.env.update(store=self.store, _viewer_filters=lambda: scope_filters(
            None, self.groups(self.author), self.author))
        load_functions(["_upload_blocked_paths"], self.env)
        for target, value in (
            ("src.config.get_config", self.cfg), ("src.pipeline.get_config", self.cfg),
            ("src.admin_app.get_config", self.cfg), ("src.dashboard_stats.get_vector_store", self.store),
            ("src.pipeline.get_llm_client", FixtureLLM()),
        ):
            patch = mock.patch(target, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def groups(self, user):
        return visible_groups(self.staff[user][1]["permission_key"])

    def api(self, tool, *args, user="audit_alice", **kwargs):
        ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
            headers={"authorization": "Bearer " + self.staff[user][2]})))
        result = json.loads(tool(*args, ctx=ctx, **kwargs))
        self.assertNotIn("error", result)
        return result

    def send(self, name, content, *, project=None, groups=None, users=None, department="合成运营部"):
        groups = ["all"] if groups is None else groups
        users = [] if users is None else users
        files = [(name, f"# 合成验收资料\n\n{content}".encode("utf-8"))]
        plan = self.env["_plan_frontend_upload"](files, "learning", groups, users, "验收报告", department, project)
        self.assertIsNotNone(plan)
        with contextlib.redirect_stdout(io.StringIO()):
            self.env["_run_upload"](files, plan, {}, groups, default_doc_type="验收报告",
                                    default_allowed_users=users, default_department=department,
                                    default_project_id=project)
        self.st.error.assert_not_called()
        return plan[0]

    def catalog(self, user, project):
        question = f"查{project}项目的验收资料有哪些？"
        result, metadata = pipeline.query_evidence(
            question, config=self.cfg, use_llm=False, use_rerank=False,
            user_groups=self.groups(user), username=user)
        self.assertEqual(metadata["route"], "catalog")
        return result.sources

    def answer(self, question, *, user="audit_alice", project_id=None, history=None):
        filters = {"project_id": [project_id]} if project_id else None
        result, stream = pipeline.answer_stream(
            question, filters=filters, history=history, config=self.cfg,
            use_query_understanding=False, use_rerank=False,
            user_groups=self.groups(user), username=user)
        for _ in stream:
            pass
        return result

    def expect_answer_fact(self, answer, sources, fact):
        sources_by_rank = {row["rank"]: row for row in sources}
        cited = pipeline.answer_citations(answer)
        self.assertTrue(cited)
        self.assertFalse(cited - sources_by_rank.keys())
        self.assertTrue(any(fact in sources_by_rank[i].get("text", sources_by_rank[i].get("excerpt", ""))
                            for i in cited))
        self.assertIn(fact, answer)

    def test_upload_acl_lookup_revision_and_audit(self):
        project_id = self.projects["北辰交付"]["id"]
        other_id = self.projects["南星交付"]["id"]
        public = "北辰交付验收编号 CX-430 的签署负责人为林主管。AUDGOLD_LEAD_OLD。"
        report = self.send("acceptance.md", public, project=project_id)
        self.send("internal.md", "项目内部分发规则。AUDGOLD_INTERNAL。",
                  project=project_id, groups=[self.staff[self.author][1]["permission_key"]])
        self.send("restricted.md", "个人授权资料。AUDGOLD_RESTRICTED。",
                  project=project_id, groups=[], users=[self.author])
        other = self.send("acceptance.md", "南星项目独立的文件。AUDGOLD_OTHER_PROJECT。", project=other_id)
        unassigned = self.send("unassigned.md", "未挂接项目的资料。AUDGOLD_UNASSIGNED。")
        docs = {row["path"]: row for row in self.store.list_documents({"status": "active"})}

        with self.subTest(stage="physical_and_ledger"):
            self.assertEqual(len(docs), 5)
            self.assertNotEqual(report.target, other.target)
            self.assertEqual(unassigned.target, "learning/public/unassigned.md")
            for path in docs:
                self.assertTrue((self.knowledge / path).is_file())
                self.assertTrue((self.knowledge / path).with_name(INFO_NAME).is_file())
                self.assertIn(path, self.ledger())
            self.assertFalse(any(INFO_NAME in path for path in self.ledger()))
            self.assertEqual(self.ledger()[report.target]["project_id"], project_id)
            self.observations["storage"] = {"active_documents": len(docs), "ledger_files": len(self.ledger()),
                                            "project_paths_separate": report.target != other.target}

        with self.subTest(stage="visibility_and_real_pat"):
            expected = {
                "audit_alice": {"acceptance.md", "internal.md", "restricted.md"},
                "audit_bob": {"acceptance.md", "internal.md"},
                "audit_carol": {"acceptance.md"},
            }
            for name, allowed in expected.items():
                web = self.catalog(name, "北辰交付")
                mcp = self.api(mcp_server._tool_list_documents_impl, user=name,
                               business_direction=self.direction["id"], project=project_id,
                               department="合成运营部", doc_type="验收报告")["documents"]
                self.assertEqual({row["source"] for row in web}, allowed)
                self.assertEqual({row["source"] for row in mcp}, allowed)
                self.assertTrue(all(row["department"] == "合成运营部" for row in mcp))
            hidden = next(row for row in docs.values() if row["source"] == "restricted.md")
            for name in ("audit_bob", "audit_carol"):
                body = self.api(mcp_server._tool_get_document_impl, [hidden["document_id"]], user=name, mode="full")
                self.assertEqual(body["not_found"], [hidden["document_id"]])
            self.observations["acl"] = {name: sorted(rows) for name, rows in expected.items()}

        with self.subTest(stage="answer_and_sources"):
            question = "北辰交付验收编号 CX-430 的签署负责人是谁？"
            result = self.answer(question, project_id=project_id)
            self.expect_answer_fact(result.answer, result.sources, "AUDGOLD_LEAD_OLD")
            mcp = self.api(mcp_server._tool_knowledge_qa_impl, question, project=project_id)
            self.expect_answer_fact(mcp["answer"], mcp["sources"], "AUDGOLD_LEAD_OLD")
            search = self.api(mcp_server._tool_knowledge_search_impl, question, project=project_id)
            self.assertTrue(any("AUDGOLD_LEAD_OLD" in row["text"] for row in search["matches"]))
            self.assertFalse(any("AUDGOLD_OTHER_PROJECT" in row["text"] for row in search["matches"]))
            self.observations["answer_citations"] = {"web": sorted(pipeline.answer_citations(result.answer)),
                                                     "mcp": sorted(pipeline.answer_citations(mcp["answer"]))}

        with self.subTest(stage="revision_and_permissions"):
            self.send("acceptance.md", public, project=project_id)
            updated = "北辰交付验收编号 CX-430 的签署负责人更新为赵主管。AUDGOLD_LEAD_NEW。"
            self.send("acceptance.md", updated, project=project_id)
            self.assertEqual(self.ledger()[report.target]["version"], "1.1")
            active_id = docs[report.target]["document_id"]
            now = self.api(mcp_server._tool_get_document_impl, [active_id], mode="full")
            older = self.api(mcp_server._tool_get_document_impl, [active_id],
                             mode="full", version="1.0", status="expired")
            self.assertEqual((now["version"], older["version"]), ("1.1", "1.0"))
            self.assertIn("AUDGOLD_LEAD_NEW", now["full_text"])
            self.assertIn("AUDGOLD_LEAD_OLD", older["full_text"])
            self.assertNotIn("AUDGOLD_LEAD_OLD", now["full_text"])
            manage.update_metadata(report.target, {"allowed_groups": [], "allowed_users": ["audit_carol"]})
            for edition in ({"status": "active"}, {"version": "1.0", "status": "expired"}):
                denied = self.api(mcp_server._tool_get_document_impl, [active_id], mode="full", **edition)
                allowed = self.api(mcp_server._tool_get_document_impl, [active_id], user="audit_carol",
                                   mode="full", **edition)
                self.assertEqual(denied["total"], 0)
                self.assertEqual(allowed["total"], 1)
            self.assertNotIn("acceptance.md", {row["source"] for row in self.catalog("audit_alice", "北辰交付")})
            self.assertIn("acceptance.md", {row["source"] for row in self.catalog("audit_carol", "北辰交付")})
            self.observations["revision"] = {"current": now["version"], "historical": older["version"],
                                             "historical_acl_updated": True}

        with self.subTest(stage="audit_and_dashboard"):
            result = admin_app.api_logs_ingest(admin_request(), page=1, page_size=20, username=self.author,
                                               keyword="", batch_id="", entry="file", outcome="")
            rows = [row for row in result["logs"] if row["path"] == report.target]
            self.assertEqual(sort_outcomes(rows), [("ingested", "indexed"), ("ingested", "new_revision"),
                                                   ("skipped", "duplicate")])
            for row in rows:
                self.assertEqual(row["ip"], self.browser_context["client_ip"])
                self.assertTrue(row["fingerprint"])
                self.assertEqual(Path(row["absolute_path"]), self.knowledge / report.target)
            first = next(row for row in rows if row["reason"] == "indexed")
            self.assertEqual(first["attributes"]["business_direction"], self.direction["name"])
            self.assertEqual(first["attributes"]["department"], "合成运营部")
            self.assertEqual(first["attributes"]["allowed_groups"], ["all"])
            query_logs = auth_db.list_qa_logs(username=self.author)
            self.assertTrue(any(log.get("mcp_context", {}).get("tool") == "knowledge_qa" for log in query_logs))
            inventory = dashboard_stats.knowledge_inventory()
            self.assertEqual((inventory["documents"], inventory["linked_documents"],
                              inventory["unlinked_documents"]), (5, 4, 1))
            self.observations["audit"] = {"ingest_rows": result["total"], "project_events": sort_outcomes(rows),
                                          "dashboard_documents": inventory["documents"]}

    def test_archive_restore_version_contract(self):
        project_id = self.projects["北辰交付"]["id"]
        plan = self.send("lifecycle.md", "合成验收历史版本。AUDGOLD_ARCHIVE_OLD。",
                         project=project_id, groups=[], users=[self.author])
        self.send("lifecycle.md", "合成验收现行版本。AUDGOLD_ARCHIVE_NEW。",
                  project=project_id, groups=[], users=[self.author])
        before = timeline(self.store, "lifecycle.md")
        self.assertEqual(before, [["1.0", "expired"], ["1.1", "active"]])
        with contextlib.redirect_stdout(io.StringIO()):
            moved = manage.archive(plan.target)["summary"].file_rows[0]["path"]
        archived = timeline(self.store, "lifecycle.md")
        listed = self.api(mcp_server._tool_list_documents_impl, domain="archive", status="archive",
                          project=project_id)["documents"]
        self.assertEqual({row["source"] for row in listed}, {"lifecycle.md"})
        history = self.api(mcp_server._tool_get_document_impl, [listed[0]["document_id"]],
                           mode="full", status="expired", version="1.0")
        self.assertIn("AUDGOLD_ARCHIVE_OLD", history["full_text"])
        hidden = self.api(mcp_server._tool_get_document_impl, [listed[0]["document_id"]], user="audit_bob",
                          mode="full", status="expired", version="1.0")
        self.assertEqual(hidden["total"], 0)
        self.assertFalse(self.api(mcp_server._tool_list_documents_impl, user="audit_bob",
                                  domain="archive", status="archive", project=project_id)["documents"])
        with contextlib.redirect_stdout(io.StringIO()):
            manage.restore(moved)
        after = timeline(self.store, "lifecycle.md")
        visible = self.catalog(self.author, "北辰交付")
        self.assertEqual({row["source"] for row in visible}, {"lifecycle.md"})
        self.assertEqual(visible[0]["classification"], "受限")
        self.assertEqual(visible[0]["project_id"], project_id)
        self.assertEqual(visible[0]["department"], "合成运营部")
        self.assertFalse(self.catalog("audit_bob", "北辰交付"))
        restored_doc = self.api(mcp_server._tool_get_document_impl, [visible[0]["document_id"]], mode="full")
        self.assertIn("AUDGOLD_ARCHIVE_NEW", restored_doc["full_text"])
        self.assertNotIn("AUDGOLD_ARCHIVE_OLD", restored_doc["full_text"])
        restored_history = self.api(mcp_server._tool_get_document_impl, [visible[0]["document_id"]],
                                    mode="full", status="expired", version="1.0")
        self.assertIn("AUDGOLD_ARCHIVE_OLD", restored_history["full_text"])
        self.observations["archive_restore"] = {"before": before, "archived": archived, "restored": after,
                                               "content_metadata_and_acl_visible": True}
        self.assertEqual(after, before, "移动档案没有修改正文，恢复后应能继续追溯原版本和历史版")

    def test_disable_employee_and_move_business_direction(self):
        project = self.projects["北辰交付"]
        key = self.send("named.md", "授权员工才能阅读此文件。AUDGOLD_PERSONAL。",
                        project=project["id"], groups=[], users=[self.author])
        current = business_directions.create_direction("合成新业务")
        projects.save_project(projects.ProjectFields(project["name"], current["id"], None, [], ""),
                              project_id=project["id"], revision=project["revision"])
        for direction, expected in ((current, True), (self.direction, False)):
            rows = self.api(mcp_server._tool_list_documents_impl, business_direction=direction["id"])["documents"]
            self.assertEqual(bool(rows), expected)
        before = self.ledger()[key.target]["version"]
        request = admin_request()
        account = self.staff[self.author][0]
        self.assertEqual(admin_app.api_user_toggle(account["id"], request, {"username": "tester"})["status"],
                         "disabled")
        with self.assertRaises(PermissionError):
            self.api(mcp_server._tool_list_documents_impl)
        self.assertEqual(admin_app.api_user_toggle(account["id"], request, {"username": "tester"})["status"],
                         "active")
        self.assertFalse(self.api(mcp_server._tool_list_documents_impl, project=project["id"])["documents"])
        self.assertFalse(self.catalog(self.author, project["name"]))
        self.assertEqual(self.ledger()[key.target]["version"], before)
        self.observations["organization"] = {"direction_change_visible": True, "disabled_token_denied": True,
                                             "reactivation_does_not_restore_grant": True}

    def test_docx_version_and_page_evidence(self):
        from docx import Document
        from src.document_version import detect_document_version
        from src.parser import parse_file

        word = Document()
        word.add_paragraph("文档版本号：V2.3")
        word.add_paragraph("验收文档封面。")
        word.add_page_break()
        word.add_heading("正式验收", level=1)
        word.add_paragraph("版本号：V99.0")
        word.add_paragraph("验收编号 NX-120 的交付负责人是韩主管。AUDGOLD_WORD_PAGE。")
        path = self.knowledge / "learning/general/paged.docx"
        word.save(path)
        parsed = parse_file(path)
        declaration = detect_document_version(path.name, parsed.version_regions, parsed.front_matter)
        self.assertEqual((declaration["document_version"], declaration["document_version_status"]),
                         ("2.3", "declared"))
        self.assertTrue(all("V99.0" not in row["text"] for row in parsed.version_regions))
        self.ingest([path], default_doc_type="验收报告", default_groups=["all"])
        [indexed] = self.store.list_documents({"source": path.name})
        self.assertEqual(indexed["document_version"], "2.3")
        self.assertEqual(indexed["version"], "1.0")
        content = self.api(mcp_server._tool_get_document_impl, [indexed["document_id"]], page_size=20)
        fact = next(row for row in content["fragments"] if "AUDGOLD_WORD_PAGE" in row["text"])
        self.assertEqual(fact["page"], "2")
        result = self.answer("验收编号 NX-120 的交付负责人是谁？")
        self.expect_answer_fact(result.answer, result.sources, "AUDGOLD_WORD_PAGE")
        self.assertTrue(any(row["page"] == "2" for row in result.sources if "AUDGOLD_WORD_PAGE" in row["text"]))
        self.observations["docx"] = {"declared_version": declaration["document_version"],
                                     "repository_revision": indexed["version"],
                                     "cited_page": fact["page"], "page_kind": fact["page_kind"]}


def sort_outcomes(rows):
    return sorted((row["outcome"], row["reason"]) for row in rows)


def timeline(store, source):
    return sorted([row["version"], row["status"]] for row in store.list_documents()
                  if row["source"] == source)


if __name__ == "__main__":
    methods = ["test_upload_acl_lookup_revision_and_audit", "test_archive_restore_version_contract",
               "test_disable_employee_and_move_business_direction", "test_docx_version_and_page_evidence"]
    suite = unittest.TestSuite(BusinessJourney(method) for method in methods)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = {"observations": BusinessJourney.observations, "tests": result.testsRun,
              "failures": [{"case": test.id(), "traceback": trace} for test, trace in result.failures],
              "errors": [{"case": test.id(), "traceback": trace} for test, trace in result.errors]}
    Path(__file__).with_name("journey.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(not result.wasSuccessful())

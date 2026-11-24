"""LLM 用量筛选回归：临时认证库、直接数据层/API 调用，不启动服务。"""

from __future__ import annotations

import inspect
import json
import sqlite3
import unittest
from contextlib import closing
from unittest import mock

from src import auth_db
from tests._helpers import TempAuthDB


NOW = "2026-11-23T12:00:00"
SINCE = "2026-11-01T00:00:00"
USAGE = {"calls": 2, "input": 100, "input_cached": 20,
         "output": 30, "estimated": True}
ZERO_TOTALS = {"total_rows": 0, "calls": 0, "input": 0,
               "input_cached": 0, "output": 0, "uncached": 0}


class LLMUsageHarness(TempAuthDB):
    def seed_many(self, rows):
        """Rows are (username, entry, question, usage dict/raw JSON, timestamp)."""
        values = [(username, entry, question,
                   usage if isinstance(usage, str) else json.dumps(usage), created_at)
                  for username, entry, question, usage, created_at in rows]
        with closing(auth_db._connect()) as conn, conn:
            conn.executemany(
                "INSERT INTO qa_logs(username, entry, question, usage, created_at) "
                "VALUES(?,?,?,?,?)", values)

    def seed(self, username="alice", entry="web", question="问题",
             usage=None, created_at=NOW):
        self.seed_many([(username, entry, question,
                         USAGE if usage is None else usage, created_at)])


class LLMUsageFilterTests(LLMUsageHarness):
    def test_four_positional_arguments_and_keyword_only_defaults(self):
        signature = inspect.signature(auth_db.llm_usage_page)
        self.assertEqual(list(signature.parameters),
                         ["username", "since", "page", "page_size", "entry", "username_exact"])
        for name, default in (("entry", None), ("username_exact", False)):
            parameter = signature.parameters[name]
            self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
            self.assertIs(parameter.default, default)
        self.seed()
        self.assertEqual(auth_db.llm_usage_page("ali", "", 1, 20)[0]["total_rows"], 1)
        with self.assertRaises(TypeError):
            auth_db.llm_usage_page("alice", "", 1, 20, "web")

    def test_legacy_like_and_exact_username_collisions(self):
        for username in ("alice", "alice2", "malice", "Alice", "bob"):
            self.seed(username)
        legacy = auth_db.llm_usage_page("ali", "", 1, 20)
        self.assertEqual({row["username"] for row in legacy[1]},
                         {"alice", "alice2", "malice", "Alice"})
        self.assertEqual(auth_db.llm_usage_page("alice", "", 1, 20)[0]["total_rows"], 4)
        for username in ("alice", "Alice"):
            with self.subTest(username=username):
                exact = auth_db.llm_usage_page(username, "", 1, 20, username_exact=True)
                self.assertEqual([row["username"] for row in exact[1]], [username])
                self.assertEqual(exact[0]["total_rows"], 1)

    def test_percent_and_underscore_remain_wildcards_only_in_legacy_mode(self):
        for username in ("acct%_", "acct12", "acct_", "acctX", "other"):
            self.seed(username)
        self.assertEqual(auth_db.llm_usage_page("acct%_", "", 1, 20)[0]["total_rows"], 4)
        exact = auth_db.llm_usage_page("acct%_", "", 1, 20, username_exact=True)
        self.assertEqual([row["username"] for row in exact[1]], ["acct%_"])
        for username in ("%", "_"):
            with self.subTest(username=username):
                self.assertEqual(auth_db.llm_usage_page(username, "", 1, 20)[0]["total_rows"], 5)
                self.assertEqual(auth_db.llm_usage_page(
                    username, "", 1, 20, username_exact=True)[0], ZERO_TOTALS)

    def test_exact_filters_are_parameterized_literals(self):
        username = "x' OR 1=1 --"
        entry = "web' OR 1=1 --"
        self.seed(username, entry)
        self.seed(username, "web")
        self.seed("bob", entry)
        result = auth_db.llm_usage_page(username, "", 1, 20,
                                        entry=entry, username_exact=True)
        self.assertEqual(result[0]["total_rows"], 1)
        self.assertEqual((result[1][0]["username"], result[1][0]["entry"]), (username, entry))
        self.assertEqual(auth_db.llm_usage_page(
            "' OR 1=1 --", "", 1, 20, username_exact=True)[0], ZERO_TOTALS)
        self.assertEqual(auth_db.llm_usage_page(
            "", "", 1, 20, entry="' OR 1=1 --")[0], ZERO_TOTALS)

    def test_empty_username_selects_all_and_empty_entry_is_distinct_from_none(self):
        for username, entry in (("", ""), ("alice", ""), ("bob", "web"), ("ghost", "unknown")):
            self.seed(username, entry)
        for exact in (False, True):
            with self.subTest(exact=exact):
                self.assertEqual(auth_db.llm_usage_page(
                    "", "", 1, 20, entry=None, username_exact=exact)[0]["total_rows"], 4)
                empty = auth_db.llm_usage_page("", "", 1, 20, entry="", username_exact=exact)
                self.assertEqual(empty[0]["total_rows"], 2)
                self.assertEqual({row["username"] for row in empty[1]}, {"", "alice"})
                self.assertTrue(all(row["entry"] == "" for row in empty[1]))

    def test_entry_equality_preserves_case_whitespace_and_custom_values(self):
        entries = ("", "web", "WEB", " web ", "custom_%", "custom_XX", "未知入口")
        for entry in entries:
            self.seed(entry=entry)
        for entry in entries:
            with self.subTest(entry=entry):
                result = auth_db.llm_usage_page("", "", 1, 20, entry=entry)
                self.assertEqual(result[0]["total_rows"], 1)
                self.assertEqual([row["entry"] for row in result[1]], [entry])
        self.assertEqual(auth_db.llm_usage_page("", "", 1, 20, entry=None)[0]["total_rows"], 7)

    def test_username_entry_and_inclusive_time_filters_combine(self):
        self.seed_many([
            ("alice", "web", "boundary", USAGE, SINCE),
            ("alice", "web", "new", USAGE, NOW),
            ("alice", "web", "old", USAGE, "2026-10-31T23:59:59"),
            ("alice", "mcp_http", "other entry", USAGE, NOW),
            ("malice", "web", "collision", USAGE, NOW),
            ("bob", "web", "other user", USAGE, NOW),
            ("alice", "", "legacy entry", USAGE, NOW),
            ("alice", "web", "bad json", "{", NOW),
        ])
        exact = auth_db.llm_usage_page("alice", SINCE, 1, 20, entry="web", username_exact=True)
        self.assertEqual([row["question"] for row in exact[1]], ["new", "boundary"])
        self.assertEqual(exact[0], {"total_rows": 2, "calls": 4, "input": 200,
                                    "input_cached": 40, "output": 60, "uncached": 160})
        self.assertEqual(auth_db.llm_usage_page(
            "ali", SINCE, 1, 20, entry="web")[0]["total_rows"], 3)
        self.assertEqual(auth_db.llm_usage_page(
            "", SINCE, 1, 20, entry="web")[0]["total_rows"], 4)
        self.assertEqual(auth_db.llm_usage_page(
            "alice", SINCE, 1, 20, username_exact=True)[0]["total_rows"], 4)
        self.assertEqual(auth_db.llm_usage_page(
            "alice", SINCE, 1, 20, entry="", username_exact=True)[1][0]["question"], "legacy entry")
        self.assertEqual(auth_db.llm_usage_page("", "2027-01-01", 1, 20)[0], ZERO_TOTALS)

    def test_totals_cover_over_300_matches_and_are_identical_on_every_page(self):
        self.seed_many([("alice", "web", f"问题{i}", USAGE, NOW) for i in range(305)] + [
            ("alice", "mcp_http", "other entry", USAGE, NOW),
            ("malice", "web", "other user", USAGE, NOW),
            ("alice", "web", "old", USAGE, "2026-10-01"),
            ("alice", "web", "bad json", "{", NOW),
            ("alice", "web", "local", {"calls": 0, "input": 999999}, NOW),
        ])
        expected = {"total_rows": 305, "calls": 610, "input": 30500,
                    "input_cached": 6100, "output": 9150, "uncached": 24400}
        questions = []
        for requested_page in range(1, 17):
            with self.subTest(page=requested_page):
                totals, rows, page, pages = auth_db.llm_usage_page(
                    "alice", SINCE, requested_page, 20, entry="web", username_exact=True)
                self.assertEqual(totals, expected)
                self.assertEqual((page, pages, len(rows)),
                                 (requested_page, 16, 5 if requested_page == 16 else 20))
                questions.extend(row["question"] for row in rows)
        self.assertEqual(questions, [f"问题{i}" for i in range(304, -1, -1)])
        clamped = auth_db.llm_usage_page("alice", SINCE, 999, 20, entry="web", username_exact=True)
        self.assertEqual(clamped[0], expected)
        self.assertEqual(clamped[2:], (16, 16))
        self.assertEqual([row["question"] for row in clamped[1]], [f"问题{i}" for i in range(4, -1, -1)])
        # 数据层不增加 API 才有的 page_size 上限，也不把汇总截成 300 条。
        large = auth_db.llm_usage_page("alice", SINCE, 1, 500, entry="web", username_exact=True)
        self.assertEqual((large[0], len(large[1]), large[2:]), (expected, 305, (1, 1)))

    def test_empty_and_unknown_filters_return_zero_totals_and_one_page(self):
        self.assertEqual(auth_db.llm_usage_filter_options(), {"users": [], "entries": []})
        self.assertEqual(auth_db.llm_usage_page("", "", 999, 20), (ZERO_TOTALS, [], 1, 1))
        self.seed()
        for kwargs in ({"username_exact": True}, {"entry": "unknown"}):
            with self.subTest(kwargs=kwargs):
                username = "unknown" if "username_exact" in kwargs else ""
                self.assertEqual(auth_db.llm_usage_page(username, "", 999, 20, **kwargs),
                                 (ZERO_TOTALS, [], 1, 1))

    def test_calls_zero_and_bad_json_stay_excluded_while_legacy_usage_is_compatible(self):
        ignored = [{}, {"calls": 0, "input": 9999}, {"calls": -1},
                   {"calls": "invalid"}, "", "{", "not json", "[]", "null"]
        self.seed_many([("alice", "web", f"ignored{i}", usage, NOW)
                        for i, usage in enumerate(ignored)])
        self.seed(question="问" * 100)
        self.seed(question="cast strings", usage={"calls": "2", "input": "100",
                   "input_cached": "150", "output": "7", "estimated": True})
        self.seed(question="legacy missing tokens", usage={"calls": 1})
        totals, rows, _, _ = auth_db.llm_usage_page("alice", "", 1, 20, entry="web", username_exact=True)
        self.assertEqual(totals, {"total_rows": 3, "calls": 5, "input": 200,
                                  "input_cached": 170, "output": 37, "uncached": 80})
        self.assertEqual([row["question"] for row in rows],
                         ["legacy missing tokens", "cast strings", "问" * 80])
        self.assertEqual((rows[0]["input"], rows[0]["estimated"]), (0, 0))
        self.assertEqual((rows[1]["input_cached"], rows[1]["estimated"]), (150, 1))

    def test_options_union_current_history_local_disabled_and_preserve_raw_entries(self):
        self.make_employee("alice", department=None, real_name="艾丽丝")
        self.make_employee("quiet", department=None, real_name="未使用员工")
        self.make_employee("disabled", department=None, status="disabled", real_name="停用员工")
        auth_db.create_admin("boss", "secret1")
        self.seed_many([
            ("alice", "web", "recent", USAGE, NOW),
            ("alice", "web", "duplicate", USAGE, NOW),
            ("history", "custom", "old", "{", "2020-01-01"),
            ("local", "mcp_stdio", "zero calls", {"calls": 0}, NOW),
            (" raw-history ", " web ", "raw", {}, NOW),
            ("", "", "empty legacy", {}, "2020-01-01"),
            ("", "unknown", "anonymous", {}, NOW),
        ])
        expected = {"users": [
            {"username": " raw-history ", "real_name": ""},
            {"username": "alice", "real_name": "艾丽丝"},
            {"username": "disabled", "real_name": "停用员工"},
            {"username": "history", "real_name": ""},
            {"username": "local", "real_name": ""},
            {"username": "quiet", "real_name": "未使用员工"},
        ], "entries": ["", " web ", "custom", "mcp_stdio", "unknown", "web"]}
        self.assertEqual(auth_db.llm_usage_filter_options(), expected)
        auth_db.llm_usage_page("alice", SINCE, 999, 1, entry="web", username_exact=True)
        auth_db.llm_usage_page("unknown", SINCE, 1, 20, entry="missing", username_exact=True)
        self.assertEqual(auth_db.llm_usage_filter_options(), expected)
        auth_db.update_user(auth_db.get_user("alice")["id"], real_name="新姓名")
        expected["users"][1]["real_name"] = "新姓名"
        self.assertEqual(auth_db.llm_usage_filter_options(), expected)

    def test_both_read_functions_close_their_connections(self):
        connect = auth_db._connect
        connections = []

        def track_connection():
            connection = connect()
            connections.append(connection)
            return connection

        with mock.patch.object(auth_db, "_connect", side_effect=track_connection):
            auth_db.llm_usage_page("", "", 1, 20)
            auth_db.llm_usage_filter_options()
        self.assertEqual(len(connections), 2)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")


class LLMCostAPITests(LLMUsageHarness):
    def setUp(self):
        super().setUp()
        from src import admin_app

        self.admin_app = admin_app
        self.prices = {"llm_price_input": 2, "llm_price_input_cached": 0.5, "llm_price_output": 3}
        patcher = mock.patch("src.app_settings.load_app_settings", return_value=self.prices)
        patcher.start()
        self.addCleanup(patcher.stop)

    def costs(self, **overrides):
        # 直接调用时显式传全部 Query 参数，避免把 FastAPI Query 对象传进数据层。
        parameters = {"_": {}, "days": 0, "username": "", "entry": None,
                      "username_exact": False, "page": 1, "page_size": 20}
        parameters.update(overrides)
        return self.admin_app.api_llm_costs(**parameters)

    def test_api_returns_global_filters_and_consistent_cross_page_costs(self):
        usage = {"calls": 2, "input": 1000000, "input_cached": 200000,
                 "output": 300000, "estimated": True}
        self.seed_many([("alice", "web", f"问题{i}", usage, NOW) for i in range(305)] + [
            ("bob", "custom", "other", usage, NOW),
            ("local", "", "no cloud calls", {"calls": 0}, "2020-01-01"),
        ])
        filters = {"users": [{"username": name, "real_name": ""}
                              for name in ("alice", "bob", "local")],
                   "entries": ["", "custom", "web"]}
        all_users = self.costs()
        self.assertTrue(all_users["ok"])
        self.assertEqual(all_users["total_rows"], 306)
        self.assertEqual(all_users["totals"]["cost"], 795.6)
        self.assertEqual(all_users["filters"], filters)
        expected = {"input": 305000000, "input_cached": 61000000,
                    "output": 91500000, "calls": 610, "cost": 793.0}
        for page in (1, 2, 999):
            with self.subTest(page=page):
                response = self.costs(username="alice", entry="web", username_exact=True, page=page)
                self.assertEqual(response["totals"], expected)
                self.assertEqual(response["total_rows"], 305)
                self.assertEqual(response["total_pages"], 16)
                self.assertEqual(response["page"], 16 if page == 999 else page)
                self.assertEqual(response["page_size"], 20)
                self.assertEqual(len(response["rows"]), 5 if page == 999 else 20)
                self.assertEqual(response["filters"], filters)
                self.assertEqual(response["prices"], {"input": 2.0, "input_cached": 0.5, "output": 3.0})
                self.assertTrue(all(row["cost"] == 2.6 and row["estimated"] is True
                                    for row in response["rows"]))
        empty = self.costs(username="missing", entry="missing", username_exact=True, page=999)
        self.assertEqual((empty["total_rows"], empty["rows"], empty["page"], empty["total_pages"]),
                         (0, [], 1, 1))
        self.assertEqual(empty["totals"]["cost"], 0)
        self.assertEqual(empty["filters"], filters)

    def test_api_forwards_exact_username_and_empty_entry_without_changing_legacy_like(self):
        self.seed("acct%_", "")
        self.seed("acct12", "")
        self.seed("acct%_", "web")
        self.assertEqual(self.costs(username="acct%_")["total_rows"], 3)
        self.assertEqual(self.costs(username="acct%_", username_exact=True)["total_rows"], 2)
        selected = self.costs(username="acct%_", entry="", username_exact=True)
        self.assertEqual(selected["total_rows"], 1)
        self.assertEqual(selected["rows"][0]["entry"], "")

    def test_api_price_formula_preserves_uncached_floor_and_reloads_zero_prices(self):
        self.seed(usage={"calls": 1, "input": 1000000, "input_cached": 1500000, "output": 100000})
        initial = self.costs()
        # 未命中输入为 max(0, input-cached)，缓存与输出沿用旧口径，不改原始用量。
        self.assertEqual(initial["totals"], {"calls": 1, "input": 1000000,
                                             "input_cached": 1500000, "output": 100000, "cost": 1.05})
        self.assertEqual(initial["rows"][0]["cost"], 1.05)
        self.assertIs(initial["rows"][0]["estimated"], False)
        self.prices.update(llm_price_input=0, llm_price_input_cached=0, llm_price_output=0)
        updated = self.costs()
        self.assertEqual(updated["prices"], {"input": 0.0, "input_cached": 0.0, "output": 0.0})
        self.assertEqual(updated["totals"]["cost"], 0)
        self.assertEqual(updated["rows"][0]["cost"], 0)
        self.assertEqual(updated["totals"]["input_cached"], 1500000)

    def test_api_route_registers_admin_authentication_dependency(self):
        routes = [route for route in self.admin_app.app.routes
                  if getattr(route, "path", None) == "/api/llm/costs"
                  and "GET" in getattr(route, "methods", set())]
        self.assertEqual(len(routes), 1)
        self.assertIs(routes[0].endpoint, self.admin_app.api_llm_costs)
        self.assertIn(self.admin_app.require_admin_api,
                      [dependency.call for dependency in routes[0].dependant.dependencies])


if __name__ == "__main__":
    unittest.main()

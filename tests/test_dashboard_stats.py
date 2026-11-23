"""工作台统计：日期补零、完整日志聚合、入口/部门隔离与本次 LLM 用量。"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from qdrant_client import models
from qdrant_client.http.exceptions import ResponseHandlingException

from src import admin_app, auth_db, business_directions, ingest_audit, projects
from src.dashboard_stats import _indexed_projects, ingestion_statistics, knowledge_inventory, usage_statistics
from src.usage_reporting import usage_for_log
from src.answer_evaluator import run_evaluation
from src.pipeline import QAResult
from tests._helpers import IngestHarness, TempAuthDB
from tests.test_admin_api import ApiTestCase

NOW = datetime(2026, 11, 19, 12)


def seed(username='alice', department='研发部', entry='web', usage=None, date='2026-11-19T10:00:00'):
    with mock.patch('src.auth_db._now', return_value=date):
        auth_db.log_qa_event(username, department, entry, '问题', usage=usage)


def verified(calls=2):
    return {'report_version': 2, 'calls': calls,
            'stages': {'query_understanding': {'calls': 1}, 'generation': {'calls': max(0, calls - 1)}}}


class DashboardAggregationTests(TempAuthDB):
    def setUp(self):
        super().setUp()
        self.price_settings = {
            'llm_price_input': 2.0, 'llm_price_input_cached': 0.2, 'llm_price_output': 8.0,
        }
        self.settings_patch = mock.patch('src.app_settings.load_app_settings',
                                         return_value=self.price_settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

    def test_llm_cost_uses_all_matching_logs_and_paginates_only_details(self):
        usage = json.dumps({"calls": 2, "input": 100, "input_cached": 20, "output": 30, "estimated": True})
        with auth_db._connect() as conn:
            conn.executemany(
                "INSERT INTO qa_logs(username, question, usage, created_at) VALUES(?,?,?,?)",
                [("alice", f"问题{i}", usage, "2026-11-19T10:00:00") for i in range(305)]
                + [("alice", "历史", usage, "2026-10-19T10:00:00"),
                   ("bob", "其他用户", usage, "2026-11-19T10:00:00"),
                   ("alice", "损坏用量", "{", "2026-11-19T10:00:00")])
        with mock.patch("src.app_settings.load_app_settings", return_value={
                "llm_price_input": 2, "llm_price_input_cached": 0.5, "llm_price_output": 3}):
            response = admin_app.api_llm_costs({}, days=0, username="ali", page=999, page_size=20)
        self.assertEqual(response["total_rows"], 306)
        self.assertEqual(response["totals"], {
            "input": 30600, "input_cached": 6120, "output": 9180, "calls": 612, "cost": 0.08})
        self.assertEqual((response["page"], response["total_pages"], len(response["rows"])), (16, 16, 6))
        self.assertEqual(response["rows"][-1]["question"], "问题0")
        self.assertTrue(response["rows"][0]["estimated"])
        self.assertEqual(auth_db.llm_usage_page("ali", "2026-11-01", 1, 10)[0]["total_rows"], 305)

    def test_legacy_qa_log_migrates_optional_mcp_context(self):
        seed()
        with auth_db._connect() as conn:
            conn.execute("ALTER TABLE qa_logs DROP COLUMN mcp_context")
        seed()
        auth_db.init_db()
        self.assertEqual([row["mcp_context"] for row in auth_db.list_qa_logs()], [{}, {}])
        diagnostics = {"tool": "knowledge_search", "history_count": 2}
        auth_db.log_qa_event("alice", "研发部", "mcp_http", "追问", mcp_context=diagnostics)
        self.assertEqual(auth_db.list_qa_logs()[0]["mcp_context"], diagnostics)

    def test_departments_channels_daily_zeros_and_verified_llm_totals(self):
        self.make_department('研发部'); self.make_department('市场部'); self.make_department('零调用部')
        auth_db.create_admin('boss', 'secret123')
        seed(usage=verified())
        seed('bob', entry='mcp_http', usage={'report_version': 2, 'calls': 1, 'stages': {'generation': {'calls': 1}}})
        seed('charlie', '市场部', usage={'calls': 4})
        seed('charlie', '市场部', 'manage_summary', {'report_version': 2, 'calls': 0}, '2026-11-18T09:00:00')
        seed('dena', '', 'mcp_stdio')
        seed('bob', entry='custom', usage={'report_version': 2, 'calls': 3,
             'stages': {'enumeration_0': {'calls': 2}, 'unknown_stage': {'calls': 1}}})
        seed('local', '', usage=verified(99))
        seed('boss', usage=verified(99))
        seed(usage=verified(99), date='2026-11-12T00:00:00')
        seed(usage=verified(99), date='2026-11-20T00:00:00')
        stats = usage_statistics(7, now=NOW)
        self.assertEqual(stats['requests'], 6)
        self.assertEqual(stats['active_employees'], 4)
        self.assertEqual(stats['active_departments'], 2)
        self.assertEqual({row['name']: row['requests'] for row in stats['departments']},
                         {'研发部': 3, '市场部': 2, '零调用部': 0, '未分配部门': 1})
        self.assertEqual([row['requests'] for row in stats['channels']], [3, 2, 1])
        self.assertEqual(len(stats['daily']), 7)
        self.assertEqual(stats['daily'][0], {'date': '2026-11-13', 'web': 0, 'mcp': 0, 'other': 0})
        self.assertEqual(stats['daily'][-1], {'date': '2026-11-19', 'web': 2, 'mcp': 2, 'other': 1})
        llm = stats['llm']
        self.assertEqual(llm['calls'], 6)
        self.assertEqual(llm['verified_requests'], 4)
        self.assertEqual(llm['legacy_requests'], 2)
        self.assertEqual(llm['legacy_reference_calls'], 4)
        self.assertEqual(sum(row['calls'] for row in llm['stages']), 6)
        self.assertEqual([row['calls'] for row in llm['channels']], [2, 1, 3])
        # tokens / 费用（三档单价取自 settings；estimated 标记计数）
        self.assertEqual(llm['tokens'], {'input': 0, 'input_cached': 0, 'output': 0})
        self.assertEqual(llm['estimated_requests'], 0)
        self.assertEqual(llm['cost'], 0.0)
        self.assertEqual(llm['price'],
                         {'input': 2.0, 'input_cached': 0.2, 'output': 8.0})

    def test_llm_tokens_cost_and_daily_aggregation(self):
        """tokens/费用按可核验记录聚合，legacy 行不计费；按天汇总供趋势图。"""
        price = {'input': 2.0, 'input_cached': 0.2, 'output': 8.0}
        seed('alice', usage={'report_version': 2, 'calls': 2, 'input': 1000,
                             'input_cached': 200, 'output': 400, 'estimated': True,
                             'stages': {'query_understanding': {'calls': 1, 'input': 400, 'output': 100},
                                        'generation': {'calls': 1, 'input': 600, 'output': 300}}})
        seed('bob', entry='mcp_http', usage={'report_version': 2, 'calls': 1,
                                             'input': 500, 'output': 250})
        seed('charlie', usage={'calls': 3})  # legacy：无 report_version，不计 tokens/费用
        stats = usage_statistics(7, now=NOW)
        llm = stats['llm']
        self.assertEqual(llm['tokens'], {'input': 1500, 'input_cached': 200, 'output': 650})
        self.assertEqual(llm['estimated_requests'], 1)
        # 未命中 1300×2 + 缓存 200×0.2 + 输出 650×8 = 7880/1e6 = 0.00784
        self.assertAlmostEqual(llm['cost'], 0.00784)
        self.assertEqual(llm['price'], price)
        self.assertEqual(llm['stages'][0], {'key': 'query_understanding', 'name': '查询理解',
                                            'calls': 1, 'input': 400, 'output': 100})
        other = next(row for row in llm['stages'] if row['key'] == 'other')
        self.assertEqual((other['input'], other['output'], other['calls']), (500, 250, 1))
        self.assertEqual(sum(row['input'] for row in llm['stages']), llm['tokens']['input'])
        self.assertEqual(sum(row['output'] for row in llm['stages']), llm['tokens']['output'])
        self.assertEqual(llm['inconsistent_usage'], 0)
        daily_llm = [row for row in llm['daily_llm'] if row['calls'] or row['input']]
        self.assertEqual(daily_llm, [dict(date='2026-11-19', calls=3, input=1500,
                                        input_cached=200, output=650, cost=0.00784)])

    def test_usage_detail_mismatch_is_visible_and_cached_tokens_are_bounded(self):
        seed(usage={'report_version': 2, 'calls': 1, 'input': 100, 'input_cached': 150,
                    'output': 20, 'stages': {'generation': {'calls': 2, 'input': 200, 'output': 40}}})
        llm = usage_statistics(7, now=NOW)['llm']
        self.assertEqual(llm['tokens'], {'input': 100, 'input_cached': 100, 'output': 20})
        self.assertAlmostEqual(llm['cost'], 0.00018)
        self.assertEqual(llm['inconsistent_usage'], 1)
        self.assertEqual(sum(row['calls'] for row in llm['stages']), 1)

    def test_prices_are_reloaded_and_zero_prices_remain_zero(self):
        seed(usage={'report_version': 2, 'calls': 1, 'input': 1000000,
                    'input_cached': 200000, 'output': 100000})
        llm = usage_statistics(7, now=NOW)['llm']
        self.assertAlmostEqual(llm['cost'], 2.44)
        self.price_settings.update(llm_price_input=0, llm_price_input_cached=0, llm_price_output=0)
        refreshed = usage_statistics(7, now=NOW)['llm']
        self.assertEqual(refreshed['cost'], 0)
        self.assertEqual(refreshed['tokens'], llm['tokens'])
        self.assertEqual(refreshed['price'], {'input': 0, 'input_cached': 0, 'output': 0})

    def test_cached_answer_consumes_only_current_evaluation(self):
        result = SimpleNamespace(usage={'input': 999999, 'output': 999999, 'calls': 2},
                                 request_usage={'stages': {}}, cache_hit=True)
        seed(usage=usage_for_log(result, {'usage': {'input': 100, 'output': 20}}))
        llm = usage_statistics(7, now=NOW)['llm']
        self.assertEqual(llm['tokens'], {'input': 100, 'input_cached': 0, 'output': 20})
        self.assertEqual(llm['calls'], 1)
        self.assertAlmostEqual(llm['cost'], 0.00036)
        evaluation = next(row for row in llm['stages'] if row['key'] == 'evaluation')
        self.assertEqual((evaluation['input'], evaluation['output']), (100, 20))

    def test_daily_tokens_zero_fill_and_date_boundaries(self):
        usage = {'report_version': 2, 'calls': 1, 'input': 100, 'output': 20}
        seed(usage=usage, date='2026-11-12T23:59:59')
        seed(usage=usage, date='2026-11-13T00:00:00')
        seed(usage=usage, date='2026-11-19T23:59:59')
        seed(usage=usage, date='2026-11-20T00:00:00')
        llm = usage_statistics(7, now=NOW)['llm']
        self.assertEqual(llm['calls'], 2)
        self.assertEqual(llm['tokens']['input'], 200)
        self.assertEqual(llm['daily_llm'][1], {'date': '2026-11-14', 'calls': 0,
                                             'input': 0, 'input_cached': 0, 'output': 0, 'cost': 0})
        self.assertEqual(sum(row['input'] for row in llm['daily_llm']), 200)
        self.assertAlmostEqual(sum(row['cost'] for row in llm['daily_llm']), llm['cost'])

    def test_aggregation_is_not_capped_at_300_and_uses_historical_department(self):
        user = self.make_employee('alice', department='新部门')
        for _ in range(505):
            seed(user['username'], '旧部门', usage=verified())
        stats = usage_statistics(30, now=NOW)
        self.assertEqual(stats['requests'], 505)
        self.assertEqual(stats['llm']['calls'], 1010)
        self.assertEqual(stats['departments'][0], {'name': '旧部门', 'requests': 505})

    def test_empty_window_malformed_usage_and_bounds(self):
        stats = usage_statistics(90, now=NOW)
        self.assertEqual(len(stats['daily']), 90)
        self.assertEqual(stats['requests'], 0)
        self.assertEqual(stats['llm']['average_calls'], 0)
        seed(usage={})
        with auth_db._connect() as conn:
            conn.execute("UPDATE qa_logs SET usage='invalid json'")
        stats = usage_statistics(7, now=NOW)
        self.assertEqual(stats['requests'], 1)
        self.assertEqual(stats['llm']['malformed_usage'], 1)
        self.assertEqual(stats['llm']['calls'], 0)
        for days in [0, 91]:
            with self.assertRaises(ValueError): usage_statistics(days, now=NOW)


class UsageReportingTests(unittest.TestCase):
    def test_cache_hit_keeps_original_display_usage_but_counts_only_current_evaluation(self):
        original = {'calls': 2, 'input': 100, 'output': 10, 'stages': {'generation': {'input': 100, 'output': 10}}}
        result = SimpleNamespace(usage=original, request_usage={'stages': {}}, cache_hit=True)
        self.assertEqual(usage_for_log(result)['calls'], 0)
        logged = usage_for_log(result, {'usage': {'input': 30, 'output': 5}})
        self.assertEqual(logged['calls'], 1)
        self.assertEqual(logged['input'], 30)
        self.assertTrue(logged['cache_hit'])
        self.assertEqual(original['calls'], 2)
        self.assertNotIn('evaluation', original['stages'])

    def test_nested_calls_and_current_summary_are_counted_once(self):
        result = SimpleNamespace(usage={}, request_usage={'stages': {
            'conversation_summary': {'input': 10, 'output': 2},
            'enumeration_0': {'input': 30, 'output': 3, 'calls': 3}}})
        self.assertEqual(usage_for_log(result)['calls'], 4)
        normal = SimpleNamespace(usage={'stages': {'generation': {'input': 10, 'output': 2}}})
        self.assertEqual(usage_for_log(normal)['report_version'], 2)
        self.assertEqual(usage_for_log(normal)['calls'], 1)

    def test_evaluation_parse_failure_still_records_incurred_usage(self):
        result = QAResult(question='q', answer='a', context='c', retrieved=[], sources=[])
        def judge(*args, **kwargs):
            kwargs['usage_out'].update(input=20, output=5)
            raise ValueError('invalid model JSON')
        with mock.patch('src.answer_evaluator.judge_dimensions', side_effect=judge):
            evaluation = run_evaluation(result, llm=object())
        self.assertFalse(evaluation['ok'])
        self.assertEqual(usage_for_log(result, evaluation)['calls'], 1)


class KnowledgeDashboardTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.addCleanup(self.store.client.close)
        self.addCleanup(_indexed_projects.cache_clear)

    def test_inventory_counts_each_active_document_once(self):
        direction = business_directions.create_direction("低空经济")
        project = projects.save_project(projects.ProjectFields("监管平台", direction["id"], None, [], ""))
        orphan = projects.save_project(projects.ProjectFields("待规划项目", None, None, [], ""))
        dimension = self.embedding.dimension()
        self.store.ensure_collection(dimension)
        metadata = [
            ("same-document", "active", project["id"]),
            ("same-document", "active", project["id"]),
            ("same-document", "expired", orphan["id"]),
            ("orphan-project-document", "active", orphan["id"]),
            ("unlinked-document", "active", None),
            ("archived-document", "archive", project["id"]),
        ]
        self.store.client.upsert(collection_name=self.cfg.qdrant_collection, points=[
            models.PointStruct(id=i, vector=[1.0] + [0.0] * (dimension - 1),
                               payload={"document_id": doc, "status": status, "project_id": project_id})
            for i, (doc, status, project_id) in enumerate(metadata, 1)
        ])
        with mock.patch("src.dashboard_stats.get_vector_store", return_value=self.store):
            data = knowledge_inventory()
        self.assertEqual((data["documents"], data["linked_documents"], data["unlinked_documents"]), (3, 2, 1))
        self.assertEqual((data["direction_count"], data["project_count"]), (1, 2))
        self.assertEqual([(group["name"], group["documents"]) for group in data["groups"]],
                         [("低空经济", 1), ("未关联业务方向", 1)])

    def test_unavailable_vector_store_keeps_project_information(self):
        direction = business_directions.create_direction("低空经济")
        projects.save_project(projects.ProjectFields("监管平台", direction["id"], None, [], ""))
        error = ResponseHandlingException(ConnectionRefusedError("offline"))
        with mock.patch("src.dashboard_stats.get_vector_store", return_value=self.store), \
             mock.patch.object(self.store, "collection_exists", side_effect=error), \
             self.assertLogs("src.dashboard_stats", level="WARNING"):
            data = knowledge_inventory()
        self.assertFalse(data["available"])
        self.assertIsNone(data["documents"])
        self.assertEqual(data["project_count"], 1)
        self.assertEqual(data["groups"][0]["projects"], [{"name": "监管平台", "documents": None}])

    def test_ingest_totals_and_failure_drill_down_share_date_boundaries(self):
        local_now = datetime(2026, 11, 19, 12, tzinfo=timezone(timedelta(hours=8)))
        with auth_db._connect() as conn:
            conn.executemany("""INSERT INTO ingest_logs(batch_id,file_index,username,actor_department,ip,
                entry,file_name,source_path,path,started_at,outcome,reason,requested,attributes)
                VALUES(?,?,'alice','','','file','same.md','','learning/same.md',?,?,'','{}','{}')""", [
                ("outside-before", 0, "2026-11-12T15:59:59.999+00:00", "failed"),
                ("first", 0, "2026-11-12T16:00:00.000+00:00", "ingested"),
                ("first", 1, "2026-11-12T16:00:00.000+00:00", "skipped"),
                ("second", 0, "2026-11-18T08:00:00.000+00:00", "ingested"),
                ("second", 1, "2026-11-18T08:00:00.000+00:00", "failed"),
                ("pending", 0, "2026-11-19T15:59:59.999+00:00", "running"),
                ("outside-after", 0, "2026-11-19T16:00:00.000+00:00", "failed"),
            ])
        stats = ingestion_statistics(7, now=local_now)
        self.assertEqual([stats[key] for key in ("batches", "ingested", "skipped", "failed", "running")],
                         [3, 2, 1, 1, 1])
        self.assertEqual(stats["first_log_date"], "2026-11-12")
        period = stats["period"]
        logs = ingest_audit.list_logs(page=1, page_size=20, outcome="failed",
                                      since=datetime.fromisoformat(period["since"]),
                                      before=datetime.fromisoformat(period["before"]))
        self.assertEqual(logs["total"], stats["failed"])


class DashboardApiTests(ApiTestCase):
    def test_overview_counts_business_directions_and_filters_period(self):
        self.login_admin()
        business_directions.create_direction('智慧园区')
        business_directions.create_direction('智慧交通')
        seed(usage=verified())
        with mock.patch.object(admin_app, 'usage_statistics', side_effect=lambda days: usage_statistics(days, now=NOW)):
            response = self.client.get('/api/overview?days=7')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['business_directions'], 2)
        self.assertEqual(data['usage_statistics']['requests'], 1)
        self.assertNotIn('recent_logins', data)
        self.assertEqual(self.client.get('/api/overview?days=91').status_code, 422)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/overview').status_code, 401)

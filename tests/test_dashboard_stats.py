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

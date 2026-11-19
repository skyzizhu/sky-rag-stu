"""管理员操作审计：真实鉴权/中间件、临时数据库和服务脚本替身。"""
import json
from unittest import mock

from fastapi.testclient import TestClient

from src import admin_audit, admin_app, auth_db
from tests.test_admin_api import ApiTestCase


class AdminAuditTests(ApiTestCase):
    def logs(self, action=''):
        return admin_audit.list_operations(action=action, page_size=100)['logs']

    def test_existing_admin_endpoints_have_action_labels(self):
        for route in admin_app.app.routes:
            if (getattr(route, 'path', '').startswith('/api/')
                    and route.methods.intersection({'POST', 'PUT', 'PATCH', 'DELETE'})):
                with self.subTest(path=route.path):
                    self.assertIn(route.endpoint.__name__, admin_audit.OPERATIONS)

    def test_view_requests_do_not_insert_logs_even_when_repeated(self):
        self.login_admin()
        user = self.make_employee()
        with auth_db._connect() as conn:
            before = conn.execute('SELECT COUNT(*) FROM admin_operation_logs').fetchone()[0]
        for _ in range(2):
            for path in ['/admin', '/api/overview', '/api/departments', '/api/users',
                         f"/api/users/{user['id']}", '/api/admins', '/api/tokens',
                         '/api/business-directions', '/api/business-directions/suggest-code?name=业务',
                         '/api/logs/admin-operations', '/api/logs/auth', '/api/logs/qa']:
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 200)
        with auth_db._connect() as conn:
            after = conn.execute('SELECT COUNT(*) FROM admin_operation_logs').fetchone()[0]
        self.assertEqual(after, before)

    def test_historical_view_rows_are_preserved_but_hidden_from_list_and_action_filter(self):
        self.login_admin()
        admin_audit.record(admin=None, event={}, action='department_list', target='',
                           method='GET', path='/api/departments', status_code=200,
                           duration_ms=1, ip='', user_agent='')
        page = self.client.get('/api/logs/admin-operations').json()
        self.assertEqual(page['total'], 0)  # 登录记录只在专门的管理员登录日志中。
        self.assertNotIn('department_list', [item['value'] for item in page['actions']])
        with auth_db._connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM admin_operation_logs').fetchone()[0], 1)

    def test_success_failure_and_validation_record_once(self):
        self.login_admin()
        self.client.post('/api/business-directions', json={'name': '智慧园区'})
        self.client.post('/api/business-directions', json={'name': '智慧园区'})
        self.client.post('/api/business-directions', json={'name': 'x' * 81})
        logs = self.logs('business_direction_create')
        self.assertEqual([row['status_code'] for row in logs], [422, 400, 200])
        self.assertEqual([row['success'] for row in logs], [0, 0, 1])
        self.assertTrue(all(row['username'] == 'boss' and row['authenticated'] for row in logs))
        self.assertIn('智慧园区', logs[-1]['detail'])

    def test_service_controls_are_recorded_without_status_reads(self):
        self.login_admin()
        self.client.get('/api/departments')
        self.assertEqual(self.logs('department_list'), [])
        with mock.patch('requests.get', return_value=mock.Mock(status_code=200)):
            self.client.get('/api/services')
        self.assertEqual(self.logs('service_read'), [])
        with mock.patch.object(admin_app, '_run_service_script', return_value={'ok': True}):
            self.assertEqual(self.client.post('/api/services/front/start').status_code, 200)
            self.client.post('/api/services/restart')
            self.client.post('/api/services/stop')
        with mock.patch.object(admin_app, '_run_service_script', return_value={'ok': False}):
            self.assertEqual(self.client.post('/api/services/mcp/stop').status_code, 400)
        logs = self.logs('service_control')
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0]['success'], 0)
        self.assertIn('service=mcp', logs[0]['target'])
        self.assertIn('action=stop', logs[0]['target'])
        self.assertEqual(len(self.logs('service_restart_all')), 1)
        self.assertEqual(len(self.logs('service_stop_all')), 1)

    def test_auth_failures_csrf_and_non_admin_requests(self):
        self.assertEqual(self.client.get('/api/logs/admin-operations').status_code, 401)
        self.assertEqual(self.logs(), [])
        self.assertEqual(self.client.post('/api/departments', json={'name': 'hr'}).status_code, 401)
        self.assertEqual(self.logs('department_create')[0]['authenticated'], 0)
        self.login_admin()
        response = self.client.post('/api/departments', json={'name': 'hr'},
                                    headers={'Origin': 'https://evil.example'})
        self.assertEqual(response.status_code, 403)
        row = self.logs('department_create')[0]
        self.assertEqual(row['status_code'], 403)
        self.assertEqual(row['username'], 'boss')
        self.assertEqual(auth_db.list_departments(), [])

    def test_login_logout_only_appear_in_admin_login_logs(self):
        self.login_admin()
        response = self.client.post('/admin/login', json={'username': 'attempted', 'password': 'DO_NOT_LOG'})
        self.assertEqual(response.status_code, 401)
        row = auth_db.list_login_logs(scope='admins')[0]
        self.assertEqual(row['username'], 'attempted')
        self.assertEqual(row['success'], 0)
        self.client.get('/admin/logout', follow_redirects=False)
        self.assertEqual(auth_db.list_login_logs(scope='admins')[0]['action'], 'logout')
        self.assertEqual(self.logs(), [])
        with auth_db._connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM admin_operation_logs').fetchone()[0], 0)


    def test_snapshot_pagination_stays_stable_while_new_logs_arrive(self):
        self.login_admin()
        for i in range(6):
            self.client.post('/api/departments', json={'name': f'dep{i}'})
        first = self.client.get('/api/logs/admin-operations?page_size=3').json()
        self.client.post('/api/departments', json={'name': 'newest'})
        second = self.client.get('/api/logs/admin-operations', params={
            'page_size': 3, 'page': 2, 'snapshot_id': first['snapshot_id']}).json()
        third = self.client.get('/api/logs/admin-operations', params={
            'page_size': 3, 'page': 3, 'snapshot_id': first['snapshot_id']}).json()
        self.assertEqual(first['total'], 6)
        self.assertEqual(second['total'], first['total'])
        ids = [row['id'] for page in [first, second] for row in page['logs']]
        self.assertEqual(third['page'], 2)
        self.assertEqual(len(ids), 6)
        self.assertEqual(len(set(ids)), 6)
        self.assertEqual(sorted(ids, reverse=True), ids)
        refreshed = self.client.get('/api/logs/admin-operations?page_size=100').json()
        self.assertGreater(refreshed['total'], first['total'])
        filtered = self.client.get('/api/logs/admin-operations', params={
            'action': 'department_create', 'username': 'boss', 'success': 'true',
            'page_size': 3, 'page': 999}).json()
        self.assertEqual(filtered['total'], 7)
        self.assertEqual(filtered['page'], 3)
        self.assertEqual(len(filtered['logs']), 1)

    def test_secrets_from_password_body_token_response_and_query_are_not_logged(self):
        self.login_admin()
        user = self.make_employee()
        self.client.post(f"/api/users/{user['id']}/reset", json={'new_password': 'TOP_SECRET_PASSWORD'})
        response = self.client.post('/api/tokens', json={'username': 'alice', 'name': 'test'})
        plaintext = response.json()['plaintext']
        self.client.post('/api/tokens/revoke', params={'token_prefix': 'SECRET_QUERY_TOKEN'})
        serialized = json.dumps(self.logs(), ensure_ascii=False)
        for secret in ['TOP_SECRET_PASSWORD', plaintext, 'SECRET_QUERY_TOKEN']:
            self.assertNotIn(secret, serialized)
        self.assertIn(f"user_id={user['id']}", serialized)
        self.assertEqual(self.logs('password_reset')[0]['path'], '/api/users/{user_id}/reset')

    def test_unhandled_errors_are_recorded_as_failed_without_exception_text(self):
        self.login_admin()
        with mock.patch('src.business_directions.create_direction', side_effect=RuntimeError('PRIVATE_ERROR')):
            with TestClient(admin_app.app, raise_server_exceptions=False, cookies=self.client.cookies) as client:
                response = client.post('/api/business-directions', json={'name': '业务'})
        self.assertEqual(response.status_code, 500)
        row = self.logs('business_direction_create')[0]
        self.assertEqual(row['status_code'], 500)
        self.assertEqual(row['success'], 0)
        self.assertNotIn('PRIVATE_ERROR', json.dumps(row))

    def test_filters_bounds_and_employee_auth_are_separate(self):
        self.login_admin()
        self.make_employee()
        before = len(self.logs())
        self.client.post('/auth/login', json={'username': 'alice', 'password': 'secret1'})
        self.assertEqual(len(self.logs()), before)
        for params in [{'page': 0}, {'page_size': 0}, {'page_size': 101}, {'snapshot_id': -1}]:
            self.assertEqual(self.client.get('/api/logs/admin-operations', params=params).status_code, 422)
        result = self.client.get('/api/logs/admin-operations?action=no_such_action').json()
        self.assertEqual(result['total'], 0)
        self.assertEqual(result['logs'], [])
        html = self.client.get('/admin').text
        self.assertIn("name: '管理员操作日志'", html)
        self.assertIn('adminOperationPage(-1)', html)

    def test_bootstrap_records_new_actor_and_deleted_admin_history_survives(self):
        response = self.client.post('/admin/bootstrap', json={
            'username': 'owner', 'password': 'secret123', 'password_confirm': 'secret123'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.logs(), [])
        self.assertTrue(auth_db.list_login_logs(scope='admins'))
        self.client.post('/api/departments', json={'name': '运营部'})
        row = self.logs('department_create')[0]
        self.assertEqual(row['username'], 'owner')
        self.assertEqual(row['authenticated'], 1)
        owner_id = row['admin_id']
        # 历史没有级联外键，账户删除后仍保留当时身份。
        with auth_db._connect() as conn:
            conn.execute('DELETE FROM admins WHERE id=?', (owner_id,))
        self.assertEqual(self.logs('department_create')[0]['username'], 'owner')

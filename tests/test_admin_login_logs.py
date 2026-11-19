"""管理员登录日志独立分页：临时库、真实接口，排除管理操作和员工日志。"""
from src import auth_db
from tests.test_admin_api import ApiTestCase


class AdminLoginLogTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.login_admin()
        for i in range(45):
            auth_db.log_auth_event('login' if i % 2 == 0 else 'logout', 'paged_admin',
                                   success=(i % 3 != 0), entry='admin')
        auth_db.log_auth_event('department_create', 'paged_admin', entry='admin')
        auth_db.log_auth_event('password_reset', 'paged_admin', entry='admin')
        auth_db.log_auth_event('login', 'paged_admin', entry='web')

    def query(self, **params):
        return self.client.get('/api/logs/auth', params={
            'scope': 'admins', 'login_only': 'true', 'username': 'paged_admin', **params})

    def test_pagination_boundaries_and_reads_do_not_add_operation_logs(self):
        with auth_db._connect() as conn:
            before = conn.execute('SELECT COUNT(*) FROM admin_operation_logs').fetchone()[0]
        pages = [self.query(page=i, limit=20).json() for i in [1, 2, 3]]
        self.assertEqual([len(page['logs']) for page in pages], [20, 20, 5])
        self.assertTrue(all(page['total'] == 45 and page['total_pages'] == 3 for page in pages))
        ids = [row['id'] for page in pages for row in page['logs']]
        self.assertEqual(len(set(ids)), 45)
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertTrue(all(row['action'] in {'login', 'logout'} and row['entry'] == 'admin'
                            for page in pages for row in page['logs']))
        self.assertEqual(self.query(page=999, limit=20).json()['page'], 3)
        self.assertEqual(len(self.query(limit=50).json()['logs']), 45)
        self.assertEqual(len(self.query(limit=100).json()['logs']), 45)
        with auth_db._connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM admin_operation_logs').fetchone()[0], before)

    def test_action_filter_and_employee_scope_stay_separate(self):
        page = self.query(action='login').json()
        self.assertEqual(page['total'], 23)
        self.assertTrue(all(row['action'] == 'login' for row in page['logs']))
        page = self.query(action='department_create').json()
        self.assertEqual(page['total'], 0)
        self.assertEqual(page['logs'], [])
        employee = self.query(scope='users').json()
        self.assertEqual(employee['total'], 1)
        self.assertEqual(employee['logs'][0]['entry'], 'web')
        # 原管理员认证表里的历史操作不删除，只从新的登录专页排除。
        self.assertEqual(auth_db.count_login_logs(username='paged_admin', scope='admins'), 47)

    def test_empty_result_invalid_pagination_and_admin_auth(self):
        page = self.query(username='not-existing').json()
        self.assertEqual(page['logs'], [])
        self.assertEqual(page['total_pages'], 1)
        self.assertEqual(page['page'], 1)
        for params in [{'page': 0}, {'limit': 0}, {'limit': 201}]:
            self.assertEqual(self.query(**params).status_code, 422)
        self.client.cookies.clear()
        self.assertEqual(self.query().status_code, 401)

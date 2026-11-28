"""业务领域：临时 SQLite + 真实管理员鉴权接口，不接触正式数据。"""
from src import auth_db, business_directions, admin_audit
from tests._helpers import TempAuthDB
from tests.test_admin_api import ApiTestCase


class BusinessDirectionDataTests(TempAuthDB):
    def test_pinyin_english_and_mixed_names(self):
        for name, expected in [('智慧园区', 'zhihuiyuanqu'), ('重庆业务', 'chongqingyewu'),
                               ('AI 服务', 'ai_fuwu'), ('2026业务', 'bd_2026yewu')]:
            with self.subTest(name=name):
                self.assertEqual(business_directions.suggest_code(name), expected)

    def test_multiple_records_persist_across_initialization(self):
        first = business_directions.create_direction('智慧园区', note='园区业务')
        second = business_directions.create_direction('智慧交通', 'Smart_Traffic')
        auth_db.init_db()
        self.assertEqual(business_directions.list_directions(), [second, first])
        self.assertEqual(first['code'], 'zhihuiyuanqu')
        self.assertEqual(second['code'], 'smart_traffic')
        self.assertEqual(auth_db.list_departments(), [])

    def test_duplicate_names_codes_and_homophones_require_unique_codes(self):
        business_directions.create_direction('公园')
        for name, code in [('公园', 'another'), ('其他方向', 'GONGYUAN'), ('公元', '')]:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, '已存在'):
                    business_directions.create_direction(name, code)
        business_directions.create_direction('公元', 'gongyuan_2')
        self.assertEqual(len(business_directions.list_directions()), 2)

    def test_invalid_fields_are_rejected_without_writes(self):
        for name, code, note in [(' ', '', ''), ('a' * 81, '', ''), ('业务', '../code', ''),
                                 ('业务', '中文', ''), ('业务', '1abc', ''), ('业务', 'a' * 65, ''),
                                 ('业务', 'ok', 'x' * 1001), ('😀', '', ''), ('业\x00务', '', '')]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    business_directions.create_direction(name, code, note)
        self.assertEqual(business_directions.list_directions(), [])


class BusinessDirectionApiTests(ApiTestCase):
    def test_all_endpoints_require_admin(self):
        self.assertEqual(self.client.get('/api/business-directions').status_code, 401)
        self.assertEqual(self.client.get('/api/business-directions/suggest-code?name=业务').status_code, 401)
        self.assertEqual(self.client.post('/api/business-directions', json={'name': '业务'}).status_code, 401)

    def test_suggestion_creation_listing_and_audit(self):
        self.login_admin()
        suggestion = self.client.get('/api/business-directions/suggest-code', params={'name': '智慧园区'})
        self.assertEqual(suggestion.json()['code'], 'zhihuiyuanqu')
        self.assertEqual(self.client.get('/api/business-directions').json()['business_directions'], [])
        for name in ['智慧园区', '智慧交通']:
            response = self.client.post('/api/business-directions', json={'name': name, 'note': '备注'})
            self.assertEqual(response.status_code, 200, response.text)
        records = self.client.get('/api/business-directions').json()['business_directions']
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['note'], '备注')
        self.assertEqual(auth_db.list_departments(), [])
        logs = auth_db.list_login_logs(scope='admins')
        self.assertEqual(sum(row['action'] == 'business_direction_create' for row in logs), 2)
        html = self.client.get('/admin').text
        self.assertIn("name: '业务领域'", html)
        self.assertIn('businessNameChanged', html)

    def test_duplicate_and_invalid_api_input(self):
        self.login_admin()
        self.client.post('/api/business-directions', json={'name': '业务'})
        self.assertEqual(self.client.post('/api/business-directions', json={'name': '业务'}).status_code, 400)
        for payload in [{'name': ''}, {'name': '方向', 'code': '../path'},
                        {'name': 'a' * 81}, {'name': '方向', 'note': 'a' * 1001}]:
            with self.subTest(payload=payload):
                self.assertIn(self.client.post('/api/business-directions', json=payload).status_code, [400, 422])
        self.assertEqual(len(business_directions.list_directions()), 1)

    def test_browser_cross_origin_create_is_rejected(self):
        self.login_admin()
        response = self.client.post('/api/business-directions', json={'name': '业务'},
                                    headers={'Origin': 'https://example.com'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(business_directions.list_directions(), [])


class BusinessDirectionEditTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.direction = business_directions.create_direction('智慧园区', note='旧备注')
        self.path = '/api/business-directions/' + str(self.direction['id'])

    def payload(self, **changes):
        return {key: changes.get(key, self.direction[key]) for key in ['name', 'code', 'note']}

    def logs(self):
        return admin_audit.list_operations(action='business_direction_update')['logs']

    def test_edit_preserves_identity_and_logs_field_changes(self):
        self.login_admin()
        before_logs = admin_audit.list_operations()['total']
        response = self.client.get(self.path)
        self.assertEqual(response.json()['business_direction'], self.direction)
        self.assertEqual(admin_audit.list_operations()['total'], before_logs)
        response = self.client.put(self.path, json={**self.payload(name='城市运营', code='CITY_OPS', note='新备注'),
                                                   'original': self.payload()})
        self.assertEqual(response.status_code, 200, response.text)
        direction = response.json()['business_direction']
        self.assertEqual(direction['id'], self.direction['id'])
        self.assertEqual(direction['created_at'], self.direction['created_at'])
        self.assertEqual(direction['code'], 'city_ops')
        self.assertEqual(direction['note'], '新备注')
        self.assertEqual(len(business_directions.list_directions()), 1)
        self.assertEqual(len(self.logs()), 1)
        row = self.logs()[0]
        self.assertEqual(row['username'], 'boss')
        self.assertEqual(row['action_label'], '编辑业务领域')
        self.assertIn('direction_id='+str(direction['id']), row['target'])
        self.assertIn('智慧园区', row['detail'])
        self.assertIn('城市运营', row['detail'])
        self.assertIn('备注已更新', row['detail'])

    def test_duplicate_invalid_and_missing_records_are_logged_without_changes(self):
        self.login_admin()
        other = business_directions.create_direction('智慧交通')
        for payload in [self.payload(name=other['name']), self.payload(code=other['code']),
                        self.payload(code='../invalid'), self.payload(name=' ')]:
            self.assertEqual(self.client.put(self.path, json=payload).status_code, 400)
        self.assertEqual(self.client.put(self.path, json={'note': 'missing name'}).status_code, 422)
        self.assertEqual(self.client.put('/api/business-directions/999999', json=self.payload()).status_code, 404)
        self.assertEqual(business_directions.get_direction(self.direction['id']), self.direction)
        self.assertEqual(len(self.logs()), 6)
        self.assertTrue(all(not row['success'] for row in self.logs()))

    def test_stale_editor_is_rejected_and_note_can_be_cleared(self):
        self.login_admin()
        original = self.payload()
        first = self.client.put(self.path, json={**self.payload(note='其他管理员更新'), 'original': original})
        self.assertEqual(first.status_code, 200)
        second = self.client.put(self.path, json={**self.payload(note='旧表单覆盖'), 'original': original})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(business_directions.get_direction(self.direction['id'])['note'], '其他管理员更新')
        self.assertEqual(self.logs()[0]['status_code'], 409)
        fresh = first.json()['business_direction']
        response = self.client.put(self.path, json={**self.payload(note=''),
            'original': {key:fresh[key] for key in ['name','code','note']}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['business_direction']['note'], '')

    def test_edit_requires_admin_and_same_origin(self):
        self.assertEqual(self.client.get(self.path).status_code, 401)
        self.assertEqual(self.client.put(self.path, json=self.payload()).status_code, 401)
        self.login_admin()
        self.assertEqual(self.client.put(self.path, json=self.payload(),
            headers={'Origin': 'https://example.com'}).status_code, 403)
        self.assertEqual(business_directions.get_direction(self.direction['id']), self.direction)

    def test_delete_is_not_supported(self):
        self.login_admin()
        self.assertEqual(self.client.delete(self.path).status_code, 405)
        self.assertEqual(self.client.post(self.path+'/delete').status_code, 404)
        self.assertEqual(business_directions.get_direction(self.direction['id']), self.direction)

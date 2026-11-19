"""部门名称、禁止编辑和资料授权：临时 SQLite 与内存 Qdrant，禁止权限标识串用。"""
from src import auth_db, admin_audit
from src.permissions import visible_groups
from tests._helpers import TempAuthDB, IngestHarness
from tests.test_admin_api import ApiTestCase


class DepartmentDataTests(TempAuthDB):
    def test_permissions_are_fixed_at_creation(self):
        dep = auth_db.create_department('研发部')
        user = auth_db.create_user('alice', 'secret123', department_id=dep['id'])
        token = auth_db.create_token(user['id'], 'test')
        self.assertNotIn('code', dep)
        self.assertEqual(auth_db.get_user('alice')['department'], '研发部')
        self.assertEqual(auth_db.verify_token(token)['department_group'], '研发部')
        for name in ['研发部']:
            self.assertEqual(auth_db.department_permission_key(name), '研发部')
        self.assertEqual(auth_db.department_label('研发部'), '研发部')
        self.assertFalse(hasattr(auth_db, 'update_department'))

    def test_aliases_cannot_be_reassigned_after_deletion(self):
        dep = auth_db.create_department('研发部')
        with self.assertRaises(ValueError):
            auth_db.create_department('研发部')
        auth_db.delete_department(dep['id'])
        with self.assertRaises(ValueError):
            auth_db.create_department('研发部')
        self.assertEqual(auth_db.normalize_department_groups(['研发部']), ['研发部'])
        self.assertEqual(visible_groups(auth_db.department_permission_key(department_id=dep['id'])), ['all'])
        with self.assertRaises(ValueError):
            auth_db.create_department('all')

    def test_legacy_department_migration_is_idempotent_and_preserves_permissions(self):
        with auth_db._connect() as conn:
            conn.execute('DROP TABLE department_aliases')
            conn.execute('DROP TABLE departments')
            conn.execute("CREATE TABLE departments(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT UNIQUE NOT NULL,description TEXT NOT NULL DEFAULT '',disabled INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL)")
            for name in ['研发部', '研发布', 'HR', 'hr']:
                conn.execute("INSERT INTO departments(name,created_at) VALUES(?,'2026-01-01')", (name,))
        auth_db.init_db()
        rows = auth_db.list_departments()
        self.assertTrue(all('code' not in row for row in rows))
        self.assertEqual([row['permission_key'] for row in rows], ['研发部', '研发布', 'HR', 'hr'])
        self.assertTrue(all(row['created_at']=='2026-01-01' for row in rows))
        auth_db.init_db()
        self.assertEqual(auth_db.list_departments(), rows)

    def test_number_removal_preserves_employees_tokens_and_name_aliases(self):
        dep = auth_db.create_department('研发部')
        user = auth_db.create_user('alice', 'secret123', department_id=dep['id'])
        token = auth_db.create_token(user['id'], 'test')
        with auth_db._connect() as conn:
            conn.execute("ALTER TABLE departments ADD COLUMN code TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE departments SET code='yanfabu' WHERE id=?", (dep['id'],))
            conn.execute('CREATE UNIQUE INDEX idx_department_code ON departments(code COLLATE NOCASE)')
            conn.execute("INSERT INTO department_aliases VALUES('yanfabu', ?)", (dep['id'],))
        auth_db.init_db()
        self.assertEqual(auth_db.get_department(dep['id']), dep)
        self.assertEqual(auth_db.get_user('alice')['department_id'], dep['id'])
        self.assertEqual(auth_db.verify_token(token)['department_group'], '研发部')
        self.assertEqual(auth_db.department_permission_key('研发部'), '研发部')
        self.assertEqual(auth_db.department_permission_key('yanfabu'), 'yanfabu')
        with auth_db._connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM department_aliases WHERE name='yanfabu'").fetchone())
        auth_db.init_db()
        self.assertEqual(auth_db.get_department(dep['id']), dep)


class DepartmentEditApiTests(ApiTestCase):
    def test_create_without_code_and_audit(self):
        self.login_admin()
        dep = self.client.post('/api/departments', json={'name':'研发部','description':'研发资料'}).json()['department']
        self.assertNotIn('code', dep)
        self.assertEqual(dep['description'], '研发资料')
        self.assertEqual(dep['permission_key'], '研发部')
        self.assertEqual(admin_audit.list_operations(action='department_create')['total'], 1)
        self.assertNotEqual(self.client.get('/api/departments/suggest-code', params={'name':'研发部'}).status_code, 200)
        page = self.client.get('/admin').text
        department_section = page.split('/* ---------- 部门管理 ---------- */')[1].split('/* ---------- 员工管理')[0]
        self.assertNotIn('name="code"', department_section)
        self.assertNotIn('suggest-code', department_section)

    def test_edit_is_unavailable_and_does_not_mutate_department(self):
        dep = auth_db.create_department('研发部')
        self.login_admin()
        path = '/api/departments/' + str(dep['id'])
        for method in [self.client.put, self.client.patch]:
            response = method(path, json={'name':'技术部','code':'jishubu','description':'更改'})
            self.assertEqual(response.status_code, 405, response.text)
        self.assertEqual(auth_db.get_department(dep['id']), dep)
        response = self.client.get('/admin')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('onclick="depEdit(', response.text)
        self.assertNotIn('department-edit-form', response.text)

    def test_old_authentication_operation_records_are_hidden_and_new_authentication_is_not_written(self):
        self.login_admin()
        with auth_db._connect() as conn:
            conn.execute("INSERT INTO admin_operation_logs(action,method,path,status_code,success,created_at) VALUES('login','POST','/admin/login',303,1,'2026-01-01')")
        self.client.get('/admin/logout',follow_redirects=False)
        self.assertEqual(admin_audit.list_operations()['total'],0)
        with auth_db._connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM admin_operation_logs').fetchone()[0],1)
        self.assertEqual(auth_db.list_login_logs(scope='admins')[0]['action'],'logout')


class DepartmentPermissionRetrievalTests(IngestHarness):
    def setUp(self):
        super().setUp(); self.use_fakes()

    def test_department_permissions_do_not_broaden_without_code(self):
        dep=auth_db.create_department('人事部')
        finance=auth_db.create_department('finance')
        def ingest_group(name,group):
            path=self.write_note('部门测试资料',name)
            path.write_text('---\nallowed_groups: ["'+group+'"]\n---\n# 部门测试资料\n共享检索测试内容。',encoding='utf-8')
            self.ingest([path])
        ingest_group('old.md','人事部')
        ingest_group('new-name.md','人事部')
        ingest_group('new-code.md','renshibu')
        ingest_group('finance.md','finance')
        key=auth_db.department_permission_key(department_id=dep['id'])
        found=self.retrieve_hits('部门测试资料',user_groups=visible_groups(key),top_k=20)
        self.assertEqual({source for source,_ in found},{'old.md','new-name.md'})
        other=self.retrieve_hits('部门测试资料',user_groups=visible_groups(finance['permission_key']),top_k=20)
        self.assertEqual({source for source,_ in other},{'finance.md'})

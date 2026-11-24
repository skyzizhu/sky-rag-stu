"""数据管理只读接口测试：临时认证库，验证列表、字段/数据、未知表与整库导出。

说明：本平台的 TestClient 用例在 Windows 上受 _safety 网络守卫阻断（asyncio
socketpair），因此这里直接调用接口函数验证业务逻辑，并在路由注册检查中断言
这些端点都挂了管理员鉴权依赖（require_admin_api）。
"""

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from starlette.requests import Request

from src import admin_app, auth_db
from tests._helpers import TempAuthDB


class DataManagementTests(TempAuthDB):
    def setUp(self):
        super().setUp()
        auth_db.init_db()

    def seed_qa_logs(self, count):
        with auth_db._connect() as conn:
            for i in range(count):
                conn.execute("INSERT INTO qa_logs(username, question, usage, created_at) VALUES(?,?,?,?)",
                             ('alice', f'问题{i}', '{}', '2026-11-23T10:00:00'))

    def call_tables(self):
        return admin_app.api_data_tables(_={})

    def call_rows(self, table, page=1, page_size=50):
        return admin_app.api_data_table_rows(table, page=page, page_size=page_size, _={})

    def test_tables_require_admin_and_list_core_tables(self):
        self.seed_qa_logs(1)
        data = self.call_tables()
        names = {t['name'] for t in data['tables'] if t['type'] == 'table'}
        self.assertIn('qa_logs', names)
        self.assertIn('users', names)
        self.assertIn('sqlite_sequence', names)
        counts = {t['name']: t['rows'] for t in data['tables']}
        self.assertEqual(counts['qa_logs'], 1)
        # 管理员鉴权依赖挂在所有数据管理端点上（Depends(require_admin_api)）
        for path in ('/api/data/tables', '/api/data/tables/{table}/schema',
                     '/api/data/tables/{table}/rows', '/api/data/export'):
            route = next(r for r in admin_app.app.routes if getattr(r, 'path', '') == path)
            self.assertTrue(any(dep.call is admin_app.require_admin_api
                                for dep in route.dependant.dependencies), f'{path} 缺少鉴权依赖')

    def test_table_remarks_cover_known_tables_and_leave_unknown_honest(self):
        with auth_db._connect() as conn:
            conn.execute('CREATE TABLE custom_table(id INTEGER)')
        tables = {t['name']: t for t in self.call_tables()['tables']}
        self.assertEqual(set(admin_app._DATA_TABLE_REMARKS), set(tables) - {'custom_table'})
        self.assertIn('员工账户', tables['users']['remark'])
        self.assertIn('模型用量', tables['qa_logs']['remark'])
        self.assertEqual(tables['custom_table']['remark'], '未登记用途，请参考字段结构')

    def test_schema_metadata_does_not_read_or_count_data(self):
        with auth_db._connect() as conn:
            conn.execute('CREATE TABLE "schema""table"(id INTEGER PRIMARY KEY, owner INTEGER REFERENCES users(id), note TEXT UNIQUE, computed TEXT GENERATED ALWAYS AS (note || id) STORED)')
        original = admin_app._open_readonly
        statements = []

        def traced_readonly():
            conn = original()
            conn.set_trace_callback(statements.append)
            return conn

        with mock.patch.object(admin_app, '_open_readonly', side_effect=traced_readonly):
            schema = admin_app.api_data_table_schema('schema"table', _={})
        self.assertEqual([c['name'] for c in schema['columns']], ['id', 'owner', 'note', 'computed'])
        self.assertEqual(schema['columns'][3]['hidden'], 3)
        self.assertTrue(any(i['unique'] for i in schema['indexes']))
        self.assertEqual(schema['foreign_keys'][0]['table'], 'users')
        self.assertNotIn('rows', schema)
        self.assertNotIn('total', schema)
        reads = [sql for sql in statements if sql.lstrip().upper().startswith('SELECT')]
        self.assertTrue(reads)
        self.assertTrue(all('sqlite_master' in sql for sql in reads), reads)
        self.assertFalse(any('COUNT(' in sql.upper() for sql in statements))
        with self.assertRaises(HTTPException) as caught:
            admin_app.api_data_table_schema('schema"table"; DROP TABLE users; --', _={})
        self.assertEqual(caught.exception.status_code, 404)

    def test_schema_invalid_view_keeps_definition_and_explicit_error(self):
        with auth_db._connect() as conn:
            conn.execute('CREATE VIEW invalid_schema AS SELECT * FROM nonexistent_table')
            conn.execute('CREATE VIEW valid_schema AS SELECT question FROM qa_logs')
        schema = admin_app.api_data_table_schema('invalid_schema', _={})
        self.assertTrue(schema['unavailable'])
        self.assertTrue(schema['error'])
        self.assertIn('nonexistent_table', schema['sql'])
        valid = admin_app.api_data_table_schema('valid_schema', _={})
        self.assertFalse(valid['unavailable'])
        self.assertEqual([c['name'] for c in valid['columns']], ['question'])
        self.assertEqual(valid['indexes'], [])

    def test_rows_pagination_schema_and_unknown_table_404(self):
        self.seed_qa_logs(3)
        rows = self.call_rows('qa_logs', page=1, page_size=2)
        self.assertEqual(rows['total'], 3)
        self.assertEqual(len(rows['rows']), 2)
        self.assertIn('question', [c['name'] for c in rows['columns']])
        self.assertEqual(rows['total_pages'], 2)
        page2 = self.call_rows('qa_logs', page=2, page_size=2)
        self.assertEqual(len(page2['rows']), 1)
        with self.assertRaises(HTTPException) as caught:
            self.call_rows('不存在的表', page=1, page_size=2)
        self.assertEqual(caught.exception.status_code, 404)

    def test_views_are_listed_and_empty_table_has_no_rows(self):
        with auth_db._connect() as conn:
            conn.execute("CREATE TABLE empty_table (id INTEGER PRIMARY KEY, note TEXT)")
            conn.execute("CREATE VIEW v_qa_logs AS SELECT question FROM qa_logs")
        tables = self.call_tables()['tables']
        by_name = {t['name']: t for t in tables}
        self.assertEqual(by_name['empty_table']['rows'], 0)
        self.assertIn('v_qa_logs', {t['name'] for t in tables if t['type'] == 'view'})
        rows = self.call_rows('empty_table', page=1, page_size=50)
        self.assertEqual(rows['rows'], [])
        self.assertEqual([c['name'] for c in rows['columns']], ['id', 'note'])

    def test_export_creates_sqlite_snapshot_in_exports_dir(self):
        self.seed_qa_logs(2)
        response = admin_app.api_data_export(_={})
        snapshot = Path(response.path)
        try:
            self.assertTrue(snapshot.exists())
            self.assertEqual(snapshot.parent, auth_db.DB_PATH.parent / 'exports')
            self.assertTrue(snapshot.read_bytes().startswith(b'SQLite format 3\x00'))
            # 快照是完整副本：里面也能查到 qa_logs 的数据
            import sqlite3
            check = sqlite3.connect(snapshot)
            count = check.execute('SELECT COUNT(*) FROM qa_logs').fetchone()[0]
            check.close()
            self.assertEqual(count, 2)
        finally:
            snapshot.unlink(missing_ok=True)

    def test_cell_values_keep_complete_text_blob_and_int64(self):
        self.assertIsNone(admin_app._cell_value(None))
        self.assertEqual(admin_app._cell_value(42), '42')
        self.assertEqual(admin_app._cell_value(9223372036854775807), '9223372036854775807')
        self.assertEqual(admin_app._cell_value(b'\x00\x01'), {'type': 'blob', 'bytes': 2, 'hex': '0001'})
        self.assertEqual(admin_app._cell_value('字' * 5000), '字' * 5000)

    def test_readonly_connection_refuses_writes_and_missing_db_is_not_created(self):
        with closing(admin_app._open_readonly()) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute('CREATE TABLE forbidden(id INTEGER)')
        missing = auth_db.DB_PATH.parent / 'missing.db'
        with mock.patch.object(auth_db, 'DB_PATH', missing):
            with self.assertRaises(sqlite3.OperationalError):
                admin_app._open_readonly()
        self.assertFalse(missing.exists())

    def test_quoted_identifiers_and_generated_columns_align(self):
        with auth_db._connect() as conn:
            conn.execute('CREATE TABLE "quoted""table"(id INTEGER PRIMARY KEY, note TEXT, computed TEXT GENERATED ALWAYS AS (note || id) STORED)')
            conn.execute('INSERT INTO "quoted""table"(id,note) VALUES(2,?)', ('<script> & " \\',))
            conn.execute('INSERT INTO "quoted""table"(id,note) VALUES(1,?)', ('full text',))
        rows = self.call_rows('quoted"table')
        self.assertEqual(rows['data_columns'], ['id', 'note', 'computed'])
        self.assertEqual(rows['rows'][0], ['1', 'full text', 'full text1'])
        self.assertEqual(rows['columns'][2]['hidden'], 3)
        self.assertEqual(len(rows['data_columns']), len(rows['rows'][0]))
        self.assertIn('quoted"table', {t['name'] for t in self.call_tables()['tables']})
        with self.assertRaises(HTTPException) as caught:
            self.call_rows('qa_logs"; DROP TABLE users; --')
        self.assertEqual(caught.exception.status_code, 404)
        self.assertIn('users', {t['name'] for t in self.call_tables()['tables']})

    def test_indexes_foreign_keys_triggers_and_invalid_views(self):
        with auth_db._connect() as conn:
            conn.execute('CREATE TABLE custom(id INTEGER PRIMARY KEY, owner INTEGER REFERENCES users(id), code TEXT UNIQUE)')
            conn.execute('CREATE INDEX custom_owner ON custom(owner) WHERE owner IS NOT NULL')
            conn.execute('CREATE TRIGGER custom_trigger AFTER INSERT ON custom BEGIN SELECT 1; END')
            conn.execute('CREATE VIEW invalid_view AS SELECT * FROM nonexistent_table')
        metadata = self.call_tables()
        indexes = [i for i in metadata['indexes'] if i['table'] == 'custom']
        self.assertTrue(any(i['unique'] and i['origin'] == 'u' for i in indexes))
        self.assertTrue(any(i['name'] == 'custom_owner' and i['partial'] for i in indexes))
        self.assertIn('custom_trigger', {t['name'] for t in metadata['triggers']})
        invalid = next(t for t in metadata['views'] if t['name'] == 'invalid_view')
        self.assertTrue(invalid['unavailable'])
        rows = self.call_rows('custom')
        self.assertEqual(rows['foreign_keys'][0]['table'], 'users')
        self.assertIn('owner', {c['name'] for i in rows['indexes'] for c in i['columns'] if c['key']})

    def test_out_of_range_pages_clamp_to_last_and_preserve_primary_order(self):
        self.seed_qa_logs(5)
        data = self.call_rows('qa_logs', page=999, page_size=2)
        self.assertEqual((data['page'], data['total_pages'], len(data['rows'])), (3, 3, 1))
        ids = []
        for page in (1, 2, 3):
            ids.extend(row[0] for row in self.call_rows('qa_logs', page=page, page_size=2)['rows'])
        self.assertEqual(ids, ['1', '2', '3', '4', '5'])

    def test_export_unique_snapshots_preserve_objects_and_delete_after_transfer(self):
        with auth_db._connect() as conn:
            conn.execute('CREATE VIEW export_view AS SELECT question FROM qa_logs')
        a, b = admin_app.api_data_export(_={}), admin_app.api_data_export(_={})
        self.assertNotEqual(a.path, b.path)
        try:
            with closing(sqlite3.connect(a.path)) as conn:
                self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='export_view'").fetchone())
            self.assertEqual(a.headers['cache-control'], 'no-store')
        finally:
            a.background.func(*a.background.args, **a.background.kwargs)
            b.background.func(*b.background.args, **b.background.kwargs)
        self.assertFalse(Path(a.path).exists())
        self.assertFalse(Path(b.path).exists())

    def test_actual_admin_guard_rejects_anonymous_and_employee_cookie(self):
        request = Request({'type': 'http', 'headers': [], 'method': 'GET', 'path': '/api/data/tables'})
        with self.assertRaises(HTTPException) as caught:
            admin_app.require_admin_api(request)
        self.assertEqual(caught.exception.status_code, 401)
        self.make_employee('alice')
        employee_cookie = admin_app._make_cookie('alice')
        self.assertEqual(admin_app._read_cookie(employee_cookie), 'alice')
        request = Request({'type': 'http', 'headers': [(b'cookie', f'{admin_app._SESSION_COOKIE}={employee_cookie}'.encode())],
                           'method': 'GET', 'path': '/api/data/tables'})
        with self.assertRaises(HTTPException) as caught:
            admin_app.require_admin_api(request)
        self.assertEqual(caught.exception.status_code, 401)


if __name__ == '__main__':
    unittest.main()

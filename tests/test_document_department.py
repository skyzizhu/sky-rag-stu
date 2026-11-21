"""归属部门元数据：优先级、生命周期、检索与权限解耦；仅使用隔离数据。"""
import ast
import contextlib
import functools
import inspect
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from qdrant_client import models

from src import auth_db, index_epoch, index_state, keyword_search, mcp_server as ms, pipeline
from src.document_department import normalize_department, normalize_department_filter, resolve_department
from src.manage import archive, ingest_web_page, refetch_web_page, restore, update_metadata
from src.permissions import scope_filters
from src.vector_store import VectorStore
from tests._helpers import IngestHarness


class DepartmentValueTests(unittest.TestCase):
    def test_optional_values_and_reserved_filter(self):
        self.assertEqual(normalize_department(' 系统工程事业部 '), '系统工程事业部')
        self.assertEqual(normalize_department(None), '')
        for value in (None, '', '  ', 'ALL'):
            self.assertIsNone(normalize_department_filter(value))
        self.assertEqual(normalize_department_filter('__no_department__'), '__no_department__')
        for value in (4, [], 'a' * 81, '部门\n', '__no_department__'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_department(value)

    def test_priority_and_manual_clear(self):
        self.assertEqual(resolve_department(None, {})['value'], '')
        self.assertEqual(resolve_department(None, {'department': ''}, default_department='甲部')['value'], '甲部')
        info = resolve_department(None, {'department': '乙部'}, default_department='甲部')
        self.assertEqual(info['value'], '乙部')
        self.assertEqual(info['upload_default'], '甲部')
        self.assertEqual(resolve_department(info, {})['value'], '甲部')
        self.assertEqual(resolve_department({'manual': ''}, {'department': '乙部'},
                                            default_department='甲部')['value'], '')


class DocumentDepartmentTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.addCleanup(self.store.client.close)

    def note(self, name='a.md', declaration='', body='独立知识正文。'):
        path = self.knowledge / 'learning' / 'general' / name
        path.write_text(f'---\n{declaration}\n---\n# 标题\n\n{body}', encoding='utf-8')
        return path

    def rel(self, path):
        return path.relative_to(self.knowledge).as_posix()

    def current(self, path):
        return next(d for d in self.store.list_documents()
                    if d['path'] == self.rel(path) and d['status'] in ('active', 'archive'))

    def test_upload_declaration_and_manual_clear_keep_permissions_unchanged(self):
        path = self.note(declaration='department: 研发部\nallowed_groups: [hr]\ndoc_type: 需求文档')
        summary = self.ingest([path], default_department='财务部')
        self.assertEqual(summary.file_rows[0]['department'], '研发部')
        self.assertEqual(self.current(path)['allowed_groups'], ['hr'])
        before_ledger = self.ledger()[self.rel(path)]
        before_bytes = path.read_bytes()
        before, _ = self.store.client.scroll(collection_name=self.cfg.qdrant_collection,
                                              with_vectors=True, limit=100)
        with mock.patch.object(self.embedding, 'embed_texts', side_effect=AssertionError('不应向量化')):
            update_metadata(self.rel(path), {'department': ''})
        after, _ = self.store.client.scroll(collection_name=self.cfg.qdrant_collection,
                                             with_vectors=True, limit=100)
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual([(p.id, p.vector) for p in before], [(p.id, p.vector) for p in after])
        self.assertEqual(self.current(path)['department'], '')
        self.assertEqual(self.current(path)['allowed_groups'], ['hr'])
        for key in ('hash', 'version', 'chunks', 'version_info'):
            self.assertEqual(before_ledger[key], self.ledger()[self.rel(path)][key])
        self.ingest([path], default_department='新默认部门')
        path.write_text(path.read_text(encoding='utf-8') + '\n内容更新', encoding='utf-8')
        self.ingest([path])
        self.assertEqual(self.current(path)['department'], '')
        self.assertEqual(self.current(path)['doc_type'], '需求文档')

    def test_default_persists_but_new_declaration_wins(self):
        path = self.note()
        self.ingest([path], default_department='财务部')
        self.ingest([path])
        self.assertEqual(self.current(path)['department'], '财务部')
        self.note(declaration='department: 研发部')
        self.ingest([path])
        self.assertEqual(self.current(path)['department'], '研发部')
        self.note(declaration='title: 新标题')
        self.ingest([path])
        self.assertEqual(self.current(path)['department'], '财务部')

    def test_skip_and_failed_commit_do_not_change_department_ledger(self):
        path = self.note()
        self.ingest([path], default_department='研发部')
        before = self.ledger()
        summary = self.ingest([path], skip_unchanged=True, default_department='财务部')
        self.assertTrue(summary.skipped_files)
        self.assertEqual(before, self.ledger())
        path.write_text('新正文', encoding='utf-8')
        with mock.patch.object(self.store, 'upsert_chunks', side_effect=RuntimeError('模拟写库失败')), \
             self.assertRaises(RuntimeError):
            self.ingest([path], default_department='财务部')
        self.assertEqual(before, self.ledger())

    def test_archive_restore_and_rebuild_preserve_department_and_type(self):
        path = self.note(declaration='doc_type: 需求文档')
        self.ingest([path], default_department='研发部')
        update_metadata(self.rel(path), {'department': '交付部'})
        with contextlib.redirect_stdout(io.StringIO()):
            result = archive(self.rel(path))
            moved = self.knowledge / result['summary'].file_rows[0]['path']
            self.assertEqual(self.current(moved)['department'], '交付部')
            result = restore(self.rel(moved))
            restored = self.knowledge / result['summary'].file_rows[0]['path']
            self.assertEqual(self.current(restored)['department'], '交付部')
            self.ingest(rebuild=True)
        self.assertEqual(self.current(restored)['department'], '交付部')
        self.assertEqual(self.current(restored)['doc_type'], '需求文档')

    def test_web_refetch_keeps_department_without_real_fetch(self):
        page = {'text': '网页知识正文。' * 12, 'title': '页面'}
        with mock.patch('src.web_fetcher.fetch_and_parse', return_value=page), \
             contextlib.redirect_stdout(io.StringIO()):
            result = ingest_web_page('https://example.invalid/doc', default_department='研发部')
        path = Path(result['file_path'])
        update_metadata(self.rel(path), {'department': '交付部'})
        with mock.patch('src.web_fetcher.fetch_and_parse', return_value={**page, 'text': '更新网页正文。' * 12}), \
             contextlib.redirect_stdout(io.StringIO()):
            refetch_web_page(self.rel(path))
        self.assertEqual(self.current(path)['department'], '交付部')

    def test_department_name_is_bm25_searchable_without_appearing_in_text(self):
        target = self.note(body='数据同步与指挥平台说明。')
        self.ingest([target], default_department='系统工程事业部')
        self.ingest([self.note(f'bg{i}.md', body=body) for i, body in enumerate(
            ('农业灌溉管理', '旅游酒店预订', '音乐钢琴教学', '医疗手术护理', '汽车维修保养'))])
        hits = keyword_search.bm25_search('系统工程事业部', top_k=10,
                                          filters={'status': 'active'}, store=self.store, config=self.cfg)
        self.assertEqual({h['payload']['source'] for h in hits}, {'a.md'})
        self.assertTrue(all('系统工程事业部' not in h['payload']['text'] for h in hits))
        update_metadata(self.rel(target), {'department': '交付运营部'})
        self.assertEqual(keyword_search.bm25_search('系统工程事业部', top_k=10,
                          filters={'status': 'active'}, store=self.store, config=self.cfg), [])
        changed = keyword_search.bm25_search('交付运营部', top_k=10,
                                             filters={'status': 'active'}, store=self.store, config=self.cfg)
        self.assertEqual({h['payload']['source'] for h in changed}, {'a.md'})

    def test_department_filter_never_grants_visibility(self):
        public = self.note('public.md', 'department: 财务部', '报销申请说明。')
        private = self.note('private.md', 'department: 财务部\nallowed_groups: [finance]', '受限报销说明。')
        self.ingest([public, private])
        for hybrid in (False, True):
            with contextlib.redirect_stdout(io.StringIO()):
                result = pipeline.answer_question('报销', use_llm=False, use_query_understanding=False,
                           use_rerank=False, use_hybrid=hybrid, filters={'department': '财务部'},
                           user_groups=['hr'], config=self.cfg, top_k=10)
            self.assertEqual({h.metadata['source'] for h in result.retrieved}, {'public.md'})
            self.assertEqual({s['department'] for s in result.sources}, {'财务部'})

    def test_unassigned_filter_and_mcp_projection_keep_scope(self):
        blank = self.note('blank.md')
        hidden = self.note('hidden.md', 'allowed_groups: [finance]')
        named = self.note('named.md', 'department: 研发部')
        self.ingest([blank, hidden, named])
        filters = scope_filters(ms._filters('learning', 'active', department='__no_department__'), ['hr'])
        cards = self.store.get_all_cards(filters)
        self.assertEqual({c['payload']['source'] for c in cards}, {'blank.md'})
        with mock.patch.object(ms, '_identity_from_ctx', return_value=(['hr'], 'tester', {})), \
             mock.patch('src.auth_db.log_doc_access'):
            listing = json.loads(ms._tool_list_documents_impl(department='研发部'))
            self.assertEqual([d['department'] for d in listing['documents']], ['研发部'])
            doc_id = self.current(named)['document_id']
            read = json.loads(ms._tool_get_document_impl([doc_id], department='研发部'))
            self.assertEqual(read['department'], '研发部')
        ids = [c['id'] for c in self.store.get_all_cards({'source': 'blank.md'})]
        self.store.client.delete_payload(collection_name=self.cfg.qdrant_collection, keys=['department'], points=ids)
        self.assertEqual(self.current(blank)['department'], '')
        self.assertEqual({c['payload']['source'] for c in self.store.get_all_cards(filters)}, {'blank.md'})

    def test_mcp_schema_and_keyword_forwarding(self):
        from mcp.server.fastmcp.tools.base import Tool
        for name, args in [('knowledge_search', ('q', 3, None, None, None)),
                           ('knowledge_qa', ('q', 3, None, None, [], None)),
                           ('knowledge_digest', ('q', None, None, 5, None)),
                           ('get_document', (['id'], 1, 5, 'fragments', None, None)),
                           ('list_documents', (None, 'active', None))]:
            public = getattr(ms, name)
            self.assertEqual(inspect.signature(public).parameters['department'].kind, inspect.Parameter.KEYWORD_ONLY)
            schema = Tool.from_function(public, context_kwarg='ctx').parameters
            self.assertIn('department', schema['properties'])
            self.assertNotIn('department', schema.get('required', []))
            async def inline(call):
                return call()
            with mock.patch.object(ms, f'_tool_{name}_impl', return_value='ok') as called, \
                 mock.patch.object(ms.anyio.to_thread, 'run_sync', new=inline):
                coroutine = public(*args, department='研发部')
                try:
                    with self.assertRaises(StopIteration):
                        coroutine.send(None)
                finally:
                    coroutine.close()
                called.assert_called_once_with(*args, doc_type=None, department='研发部',
                                               project=None, business_direction=None)


class DepartmentUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(Path('app.py').read_text(encoding='utf-8'))

    def function(self, name):
        return next(n for n in self.tree.body if isinstance(n, ast.FunctionDef) and n.name == name)

    def test_default_own_department_options_do_not_use_permissions(self):
        functions = [self.function('document_department_options'), self.function('default_document_department')]
        namespace = {'st': SimpleNamespace(session_state={'auth_user': {'department': '研发部'}}),
                     'auth_db': SimpleNamespace(list_departments=lambda include_disabled: [{'name': '研发部'}, {'name': '财务部'}])}
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'app.py', 'exec'), namespace)
        options = namespace['document_department_options']()
        self.assertEqual(namespace['default_document_department'](options), '研发部')
        self.assertEqual(namespace['document_department_options']('已停用部门'), ['', '研发部', '财务部', '已停用部门'])
        namespace['st'].session_state['auth_user']['department'] = '不存在部门'
        self.assertEqual(namespace['default_document_department'](options), '')

    def test_all_uploads_are_single_select_and_forward_department(self):
        page = self.function('page_upload')
        fields = [n for n in ast.walk(page) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute) and n.func.attr == 'selectbox'
                  and n.args and ast.unparse(n.args[0]) == "t('department.default')"]
        self.assertEqual(len(fields), 3)
        for field in fields:
            self.assertIn('index', {k.arg for k in field.keywords})
        for function_name in ('_start_upload', '_render_upload_confirmation', '_run_upload', '_do_directory_upload'):
            function = self.function(function_name)
            self.assertIn('default_department', [a.arg for a in function.args.kwonlyargs])
        directory = self.function('_do_directory_upload')
        ingest = next(n for n in ast.walk(directory) if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Name) and n.func.id == 'ingest_files')
        self.assertIn('default_department', {k.arg for k in ingest.keywords})

    def test_department_save_is_independent_and_translations_complete(self):
        page = self.function('page_manage')
        writes = [n for n in ast.walk(page) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Name) and n.func.id == 'update_metadata'
                  and len(n.args) > 1 and isinstance(n.args[1], ast.Dict)]
        self.assertTrue(any([ast.literal_eval(k) for k in n.args[1].keys] == ['department'] for n in writes))
        catalog_tree = ast.parse(Path('src/i18n.py').read_text(encoding='utf-8'))
        catalogs = [ast.literal_eval(n) for n in ast.walk(catalog_tree) if isinstance(n, ast.Dict)
                    and any(isinstance(k, ast.Constant) and k.value == 'common.department' for k in n.keys)]
        self.assertEqual(len(catalogs), 4)
        for catalog in catalogs:
            for key in ('common.department', 'department.save', 'department.unspecified',
                        'department.default', 'department.default.help', 'department.filter.help'):
                self.assertIn(key, catalog)


if __name__ == '__main__':
    unittest.main()

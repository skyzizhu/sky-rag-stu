"""知识库列表分页：真实 AppTest 验证翻页、筛选和跨页勾选，数据源隔离。"""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
from unittest import mock

import streamlit as st
from streamlit.testing.v1 import AppTest

from tests._helpers import IngestHarness, isolate_app_settings

APP = str(Path(__file__).resolve().parent.parent / 'app.py')
_real_page = st.Page


def _manage_as_default(page, *args, **kwargs):
    kwargs['default'] = kwargs.get('url_path') == 'manage'
    return _real_page(page, *args, **kwargs)


class ManagePaginationTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        st.cache_resource.clear()
        st.cache_data.clear()
        self.addCleanup(isolate_app_settings('zh-CN'))
        self.documents = [dict(document_id=f'doc-{i:02}', source=f'note-{i:02}.md',
                               path=f'learning/general/note-{i:02}.md', domain='learning',
                               category='general', status='active', topic=[], version='1.0',
                               chunks=1, file_type='md') for i in range(23)]
        for p in (
            mock.patch.object(self.store, 'list_documents', side_effect=lambda: self.documents),
            mock.patch('src.auth_client.bootstrap', return_value={
                'has_users': True, 'has_admins': True, 'departments': []}),
            mock.patch('src.auth_client.get_prefs', return_value={}),
            mock.patch('streamlit.Page', side_effect=_manage_as_default),
            mock.patch('src.directory_uploader.directory_drop_uploader', return_value=None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def render(self, at):
        with contextlib.redirect_stdout(io.StringIO()):
            at.run()
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
        return at

    def open_page(self):
        at = AppTest.from_file(APP, default_timeout=30)
        at.query_params['rip'] = '1'
        at.session_state['auth_user'] = dict(username='u', department='hr',
                                            department_id=1, permissions=['manage'])
        return self.render(at)

    def click(self, at, key):
        at.button(key=key).click()
        return self.render(at)

    def ids(self, at):
        return list(at.dataframe[0].value['_id'])

    def edit_selection(self, at, changes):
        # AppTest 尚无 data_editor 勾选接口，模拟浏览器提交的原生编辑器 delta。
        key = f"manage_document_table_{at.session_state['manage_table_revision']}"
        at.session_state[key] = dict(edited_rows={i: {'选择': value} for i, value in changes.items()},
                                     added_rows=[], deleted_rows=[])
        self.render(at)

    def test_navigation_jump_and_page_size(self):
        at = self.open_page()
        self.assertEqual(self.ids(at), [f'doc-{i:02}' for i in range(20)])
        self.assertTrue(at.button(key='manage_prev_page').disabled)
        self.click(at, 'manage_next_page')
        self.assertEqual(self.ids(at)[0], 'doc-20')
        at.number_input(key='manage_page').set_value(2)
        self.render(at)
        self.assertEqual(self.ids(at), ['doc-20', 'doc-21', 'doc-22'])
        self.assertTrue(at.button(key='manage_next_page').disabled)
        self.click(at, 'manage_prev_page')
        self.assertEqual(self.ids(at)[0], 'doc-00')
        at.selectbox(key='manage_page_size').select(50)
        self.render(at)
        self.assertEqual(at.number_input(key='manage_page').value, 1)
        self.assertEqual(len(self.ids(at)), 23)
        self.assertTrue(at.button(key='manage_next_page').disabled)

    def test_filter_reset_empty_and_data_shrink(self):
        at = self.open_page()
        at.number_input(key='manage_page').set_value(2)
        self.render(at)
        self.documents = self.documents[:12]
        self.render(at)
        self.assertEqual(at.number_input(key='manage_page').value, 1)
        self.assertEqual(len(self.ids(at)), 12)  # 12 篇全部落回第 1 页（每页 20）
        next(w for w in at.text_input if '按文件名' in w.label).input('note-00')
        self.render(at)
        self.assertEqual(at.number_input(key='manage_page').value, 1)
        self.assertEqual(self.ids(at), ['doc-00'])
        next(w for w in at.text_input if '按文件名' in w.label).input('missing')
        self.render(at)
        self.assertEqual(len(at.dataframe), 0)
        self.assertTrue(any('没有符合' in w.value for w in at.info))

    def test_cross_page_selection_and_clear(self):
        at = self.open_page()
        self.edit_selection(at, {0: True, 1: True})
        self.assertEqual(at.session_state['manage_selected_documents'], ['doc-00', 'doc-01'])
        self.click(at, 'manage_next_page')
        self.assertFalse(at.dataframe[0].value['选择'].any())
        self.edit_selection(at, {0: True})
        self.assertEqual(at.session_state['manage_selected_documents'],
                         ['doc-00', 'doc-01', 'doc-20'])
        self.click(at, 'manage_prev_page')
        self.assertTrue(at.dataframe[0].value['选择'][:2].all())
        self.edit_selection(at, {0: False})
        self.assertEqual(at.session_state['manage_selected_documents'], ['doc-01', 'doc-20'])
        self.click(at, 'manage_clear_selection')
        self.assertEqual(at.session_state['manage_selected_documents'], [])
        self.assertFalse(at.dataframe[0].value['选择'].any())

    def test_select_all_includes_other_pages(self):
        at = self.open_page()
        self.edit_selection(at, {0: True, 1: True})
        self.click(at, 'manage_select_all')
        self.assertEqual(len(at.session_state['manage_selected_documents']), 23)
        self.assertTrue(at.dataframe[0].value['选择'].all())
        self.click(at, 'manage_next_page')
        self.assertTrue(at.dataframe[0].value['选择'].all())
        self.click(at, 'manage_remove_index')
        self.assertEqual(len(at.session_state['manage_remove_request']['paths']), 23)

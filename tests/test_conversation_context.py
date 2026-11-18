"""单会话上下文：原文、增量压缩、历史召回、预算、归属与缓存隔离。"""
from dataclasses import replace
import copy
import json
from pathlib import Path
from unittest import mock

from src.conversation_context import prepare_context, digest
from src import sessions, index_epoch
from src.pipeline import answer_question, _cache_key
from src.prompt import build_messages
from src.query_understanding import understand_query
from tests._helpers import IngestHarness


class ConversationContextTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, chat_context_max_tokens=1000, chat_recent_turns=2,
                           chat_summary_tokens=240, chat_summary_trigger_tokens=120,
                           chat_recall_messages=2, enum_stage2_enabled=False)
        self.use_fakes()
        self.filters = {'allowed_groups': ['all']}
        self.owner = 'alice'
        self.sid = '20261118_230000-abcd'
        self.scope = digest({'owner': self.owner, 'filters': self.filters})
        patcher = mock.patch('src.sessions.SESSIONS_DIR', self.knowledge.parent / 'sessions')
        patcher.start()
        self.addCleanup(patcher.stop)

    def history(self, count=10):
        messages = []
        for i in range(count):
            messages += [{'role': 'user', 'content': f'第{i}轮要求：预算500元。' + '需要遵守业务约束。' * 20},
                         {'role': 'assistant', 'content': f'第{i}轮回答，仅为历史推理。', 'result': {
                             'question': f'第{i}轮查询', 'sources': [{'document_id': 'doc', 'source': 'a.md'}],
                             'evidence_meta': {'coverage': 'partial'},
                             'conversation_meta': {'scope': self.scope, 'epoch': 'e1'}}}]
        return messages

    def prepare(self, history, state=None, llm=None, **kwargs):
        return prepare_context('之前的预算是多少？', history, state if state is not None else {},
                               kwargs.get('sid', self.sid), kwargs.get('owner', self.owner),
                               kwargs.get('filters', self.filters), kwargs.get('epoch', 'e1'),
                               self.cfg, llm)

    def summarizer(self):
        llm = mock.Mock()
        def chat(messages, usage_out=None, **kwargs):
            data = json.loads(messages[-1]['content'])
            notes = data['previous_notes'] + [{'index': m['index'], 'quote': m['content'][:60]}
                                              for m in data['new_messages'] if m['role'] == 'user']
            usage_out.update(input=10, output=10)
            return json.dumps({'notes': notes}, ensure_ascii=False), 0.0
        llm.chat.side_effect = chat
        return llm

    def test_compaction_is_incremental_and_preserves_originals(self):
        history = self.history()
        original = copy.deepcopy(history)
        state = {}
        llm = self.summarizer()
        first = self.prepare(history, state, llm)
        self.assertEqual(first.meta['summary_status'], 'compacted')
        self.assertTrue(state['notes'])
        self.assertLessEqual(first.meta['context_tokens'], self.cfg.chat_context_max_tokens)
        second = self.prepare(history, state, llm)
        self.assertEqual(second.meta['summary_status'], 'reused')
        self.assertEqual(llm.chat.call_count, 1)
        self.assertEqual(history, original)
        self.assertIn('最近检索状态', first.memory)

    def test_query_recalls_an_older_detail_beyond_recent_window(self):
        history = [{'role': 'user', 'content': '最早约定的预算是6789元。'}]
        history += [{'role': 'user', 'content': f'第{i}个其他话题'} for i in range(12)]
        prepared = self.prepare(history)
        self.assertIn(0, prepared.meta['recalled_indices'])
        self.assertIn('6789', prepared.memory)

    def test_fabricated_summary_is_rejected_and_falls_back(self):
        llm = mock.Mock()
        llm.chat.return_value = (json.dumps({'notes': [{'index': 0, 'quote': '预算变成99999元'}]}), 0.0)
        state = {}
        prepared = self.prepare(self.history(), state, llm)
        self.assertNotIn('99999', prepared.memory)
        self.assertEqual(prepared.meta['summary_status'], 'extractive_fallback')
        self.assertTrue(state['notes'])

    def test_invalid_summary_does_not_retry_each_identical_turn(self):
        llm = mock.Mock()
        llm.chat.return_value = ('invalid', 0.0)
        state = {}
        self.prepare(self.history(), state, llm)
        self.prepare(self.history(), state, llm)
        self.assertEqual(llm.chat.call_count, 1)

    def test_new_session_identity_scope_and_epoch_invalidate_old_state(self):
        history = self.history()
        history[1]['content'] = 'SECRET_RESULT'
        for change in ({'sid': '20261118_230001-abce'}, {'owner': 'bob'},
                       {'filters': {'allowed_groups': ['hr', 'all']}}, {'epoch': 'e2'}):
            state = {}
            self.prepare(history, state, self.summarizer())
            target = [] if 'sid' in change or 'owner' in change else history
            result = self.prepare(target, state, **change)
            self.assertTrue(result.meta['invalidated'])
            self.assertNotIn('SECRET_RESULT', result.memory + str(result.history))

    def test_edited_prefix_invalidates_summary(self):
        history = self.history()
        state = {}
        self.prepare(history, state, self.summarizer())
        history[0]['content'] = '新预算200元。' * 30
        result = self.prepare(history, state)
        self.assertTrue(result.meta['invalidated'])
        for note in state['notes']:
            self.assertIn(note['quote'], history[note['index']]['content'])

    def test_system_messages_do_not_become_conversation_instructions(self):
        history = [{'role': 'system', 'content': 'SYSTEM_INJECTION'}] + self.history(1)
        result = self.prepare(history)
        self.assertNotIn('SYSTEM_INJECTION', result.memory + str(result.history))
        prompt = build_messages('[1] 本轮证据', '继续', history=result.history,
                                conversation_context=result.memory or '单会话线索')
        self.assertIn('旧引用编号不能转用', prompt[0]['content'])

    def test_full_history_hash_does_not_collide_after_first_60_characters(self):
        one = [{'role': 'user', 'content': '相同前缀' * 50 + '预算500'}]
        two = [{'role': 'user', 'content': '相同前缀' * 50 + '预算900'}]
        self.assertNotEqual(_cache_key('继续', 5, {}, True, True, True, one),
                            _cache_key('继续', 5, {}, True, True, True, two))

    def test_session_file_persists_full_transcript_and_only_own_context(self):
        history = self.history()
        state = {}
        self.prepare(history, state, self.summarizer())
        sessions.save_session(self.sid, history, username='alice', context_state=state)
        restored = sessions.load_session(self.sid, username='alice')
        self.assertEqual(restored['messages'], history)
        self.assertEqual(restored['context'], state)
        self.assertIsNone(sessions.load_session(self.sid, username='bob'))
        before = sessions._session_path(self.sid, 'alice').read_bytes()
        with self.assertRaises(PermissionError):
            sessions.save_session(self.sid, [], username='bob')
        self.assertEqual(sessions._session_path(self.sid, 'alice').read_bytes(), before)

    def test_old_session_without_summary_is_compatible(self):
        sessions.save_session(self.sid, [{'role': 'user', 'content': '原始问题'}], username='alice')
        saved = sessions.load_session(self.sid, username='alice')
        prepared = self.prepare(saved['messages'], saved.get('context', {}))
        self.assertEqual(prepared.history[0]['content'], '原始问题')

    def test_pipeline_cache_is_scoped_to_owner_and_session(self):
        self.ingest([self.write_note('RAG用于检索增强生成。' * 10)])
        llm = mock.Mock()
        llm.chat_stream.side_effect = lambda *a, **k: iter(['回答 [1]'])
        with mock.patch('src.pipeline.get_llm_client', return_value=llm):
            for sid in ('session-a', 'session-a', 'session-b'):
                answer_question('解释RAG流程', use_query_understanding=False, use_rerank=False,
                                config=self.cfg, username='alice', session_id=sid, conversation_state={})
        self.assertEqual(llm.chat_stream.call_count, 2)

    def test_pipeline_supplies_the_same_context_to_query_understanding_and_generation(self):
        from src.query_understanding import QueryUnderstanding
        self.ingest([self.write_note('RAG用于检索增强生成。' * 10)])
        llm = self.summarizer()
        llm.chat_stream.side_effect = lambda *a, **k: iter(['回答 [1]'])
        history = [{'role': 'user', 'content': '早期预算6789元。' * 40}]
        history += [{'role': 'user', 'content': f'第{i}个话题'} for i in range(10)]
        qu = QueryUnderstanding(original='q', vector_query='RAG', ok=True)
        with mock.patch('src.pipeline.get_llm_client', return_value=llm), \
             mock.patch('src.pipeline.understand_query', return_value=qu) as understand:
            result = answer_question('之前预算是多少？', use_rerank=False, history=history,
                                     config=self.cfg, username='alice', session_id=self.sid,
                                     conversation_state={})
        query_memory = understand.call_args.kwargs['conversation_context']
        generation_messages = llm.chat_stream.call_args.args[0]
        self.assertIn('6789', query_memory)
        self.assertIn(query_memory, generation_messages[-1]['content'])
        self.assertIn('conversation_summary', result.usage['stages'])
        self.assertTrue(result.conversation_meta)
        self.assertAlmostEqual(result.elapsed['total'], sum(result.elapsed.get(k, 0) for k in
                               ('query_understanding', 'retrieval_only', 'rerank', 'assembly', 'enumeration', 'conversation', 'llm')))

    def test_recent_long_message_keeps_latest_correction(self):
        history = [{'role': 'user', 'content': '原来预算500元。' + '很长的背景。' * 1000 + '最终更正预算900元。'}]
        result = self.prepare(history)
        self.assertIn('最终更正预算900元', str(result.history))
        self.assertEqual(result.meta['truncated_recent_indices'], [0])
        self.assertLessEqual(result.meta['context_tokens'], self.cfg.chat_context_max_tokens)

    def test_chat_ui_resumes_owned_session_and_new_chat_resets_context(self):
        import contextlib
        import io
        import streamlit as st
        from streamlit.testing.v1 import AppTest
        from tests._helpers import isolate_app_settings
        history = [{'role': 'user', 'content': 'OWN_SESSION_MARKER', 'created_at': '2026-11-18T12:00:00'},
                   {'role': 'assistant', 'content': '历史回答'}]
        state = {'session_id': self.sid, 'owner': 'alice', 'notes': []}
        sessions.save_session(self.sid, history, username='alice', context_state=state)
        self.addCleanup(isolate_app_settings('zh-CN'))
        st.cache_resource.clear()
        st.cache_data.clear()
        with mock.patch('src.auth_client.bootstrap', return_value={'has_users': True, 'has_admins': True, 'departments': []}), \
             mock.patch('src.auth_client.get_prefs', return_value={}), \
             mock.patch('src.directory_uploader.directory_drop_uploader', return_value=None), \
             mock.patch('src.chat_scroll.scroll_chat_to_latest', return_value=None):
            at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / 'app.py'), default_timeout=30)
            at.session_state['auth_user'] = {'username': 'alice', 'department': 'hr', 'permissions': []}
            at.query_params.update(session=self.sid, rip='1')
            with contextlib.redirect_stdout(io.StringIO()):
                at.run()
            self.assertFalse(at.exception, [e.value for e in at.exception])
            self.assertEqual(at.session_state['messages'][0]['content'], 'OWN_SESSION_MARKER')
            self.assertEqual(at.session_state['messages'][0]['created_at'], '2026-11-18T12:00:00')
            with contextlib.redirect_stdout(io.StringIO()):
                at.button(key='new_chat_session').click().run()
            self.assertFalse(at.exception, [e.value for e in at.exception])
            self.assertEqual(at.session_state['messages'], [])
            self.assertEqual(at.session_state['conversation_state'], {})
            self.assertNotIn('session', at.query_params)
        self.assertEqual(sessions.load_session(self.sid, username='alice')['messages'], history)

    def test_chat_ui_cannot_load_another_owners_session(self):
        import contextlib
        import io
        import streamlit as st
        from streamlit.testing.v1 import AppTest
        from tests._helpers import isolate_app_settings
        sessions.save_session(self.sid, [{'role': 'user', 'content': 'OTHER_OWNER_SECRET'}], username='bob')
        self.addCleanup(isolate_app_settings('zh-CN'))
        st.cache_resource.clear()
        st.cache_data.clear()
        with mock.patch('src.auth_client.bootstrap', return_value={'has_users': True, 'has_admins': True, 'departments': []}), \
             mock.patch('src.auth_client.get_prefs', return_value={}), \
             mock.patch('src.directory_uploader.directory_drop_uploader', return_value=None), \
             mock.patch('src.chat_scroll.scroll_chat_to_latest', return_value=None):
            at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / 'app.py'), default_timeout=30)
            at.session_state['auth_user'] = {'username': 'alice', 'department': 'hr', 'permissions': []}
            at.query_params.update(load=self.sid, rip='1')
            with contextlib.redirect_stdout(io.StringIO()):
                at.run()
            self.assertFalse(at.exception, [e.value for e in at.exception])
            self.assertEqual(at.session_state['messages'], [])
            self.assertTrue(any('无权访问' in w.value for w in at.warning))

    def test_user_constraints_survive_epoch_change_without_old_assistant_facts(self):
        history = self.history()
        state = {}
        self.prepare(history, state, self.summarizer())
        history[1]['content'] = 'PRIVATE_OLD_ANSWER'
        current = self.prepare(history, state, epoch='e2')
        self.assertIn('预算500元', current.memory)
        self.assertNotIn('PRIVATE_OLD_ANSWER', current.memory + str(current.history))
        self.assertTrue(all(note['role'] == 'user' for note in state['notes']))

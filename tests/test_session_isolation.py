"""多人会话归属与并发保存：使用临时目录，强制 ID 碰撞也不能串用户。"""
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest import mock

from src import sessions
from tests._helpers import IngestHarness


class SessionIsolationTests(IngestHarness):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(sessions, 'SESSIONS_DIR', self.knowledge.parent / 'sessions')
        patch.start()
        self.addCleanup(patch.stop)
        self.sid = '20261118_230000-abcd'

    def message(self, text):
        return [{'role': 'user', 'content': text}]

    def test_anonymous_cannot_read_list_or_overwrite_owned_sessions(self):
        sessions.save_session(self.sid, self.message('ALICE_PRIVATE'), username='alice')
        self.assertIsNone(sessions.load_session(self.sid))
        self.assertEqual(sessions.list_sessions(), [])
        self.assertEqual(sessions.list_sessions(username='bob'), [])
        self.assertEqual(len(sessions.list_sessions(username='alice')), 1)
        with self.assertRaises(PermissionError):
            sessions.save_session(self.sid, self.message('anonymous'))
        self.assertEqual(sessions.load_session(self.sid, 'alice')['messages'], self.message('ALICE_PRIVATE'))

    def test_forced_id_collision_between_users_has_one_owner(self):
        barrier = threading.Barrier(2)
        def write(owner):
            barrier.wait()
            try:
                sessions.save_session(self.sid, self.message(owner), username=owner)
                return owner
            except PermissionError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, ['alice', 'bob']))
        winner = next(owner for owner in results if owner)
        loser = 'bob' if winner == 'alice' else 'alice'
        self.assertEqual(sum(owner is not None for owner in results), 1)
        self.assertEqual(sessions.load_session(self.sid, winner)['messages'], self.message(winner))
        self.assertIsNone(sessions.load_session(self.sid, loser))

    def test_concurrent_tabs_do_not_silently_replace_each_others_turn(self):
        baseline = self.message('baseline')
        sessions.save_session(self.sid, baseline, username='alice')
        barrier = threading.Barrier(2)
        def append(text):
            barrier.wait()
            try:
                sessions.save_session(self.sid, baseline + self.message(text), username='alice')
                return text
            except sessions.SessionConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(append, ['tab-a', 'tab-b']))
        self.assertEqual(sum(v is not None for v in results), 1)
        saved = sessions.load_session(self.sid, 'alice')
        self.assertEqual(len(saved['messages']), 2)
        self.assertIn(saved['messages'][1]['content'], results)

    def test_delete_checks_owner_and_blocks_stale_tab_resurrection(self):
        messages = self.message('private')
        sessions.save_session(self.sid, messages, username='alice')
        self.assertFalse(sessions.delete_session(self.sid, username='bob'))
        self.assertIsNotNone(sessions.load_session(self.sid, 'alice'))
        self.assertTrue(sessions.delete_session(self.sid, username='alice'))
        self.assertIsNone(sessions.load_session(self.sid, 'alice'))
        with self.assertRaises(sessions.SessionConflict):
            sessions.save_session(self.sid, messages, username='alice')

    def test_new_ids_have_128_bit_randomness_and_legacy_ids_still_work(self):
        ids = {sessions.new_session_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)
        self.assertTrue(all(len(sid.split('-')[-1]) == 32 for sid in ids))
        for sid in [next(iter(ids)), self.sid, '20261118_230000']:
            sessions.save_session(sid, self.message('hello'), username='alice')
            self.assertIsNotNone(sessions.load_session(sid, 'alice'))

    def test_append_only_allows_continuation_and_preserves_metadata_updates(self):
        baseline = self.message('hello')
        sessions.save_session(self.sid, baseline, username='alice')
        sessions.save_session(self.sid, baseline, username='alice', title='renamed')
        extended = baseline + [{'role': 'assistant', 'content': 'answer'}]
        sessions.save_session(self.sid, extended, username='alice')
        with self.assertRaises(sessions.SessionConflict):
            sessions.save_session(self.sid, baseline, username='alice')
        self.assertEqual(sessions.load_session(self.sid, 'alice')['messages'], extended)

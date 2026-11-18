"""日期/真实用户 ID 目录，唯一布局和用户隔离。"""
import json
from unittest import mock

from src import auth_db, sessions
from tests._helpers import IngestHarness


class SessionLayoutTests(IngestHarness):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(sessions, 'SESSIONS_DIR', self.knowledge.parent / 'sessions')
        patch.start()
        self.addCleanup(patch.stop)
        self.user = auth_db.create_user('alice', 'password123')
        self.sid = '20261118_230000-abcd'
        self.messages = [{'role': 'user', 'content': '旧会话原文'}]

    def legacy(self, sid=None, owner='alice', folder=None):
        sid = sid or self.sid
        path = sessions.SESSIONS_DIR / (folder or sid[:10]) / (sid + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'session_id': sid, 'username': owner, 'title': '旧会话', 'messages': self.messages,
                   'context': {'session_id': sid, 'owner': owner, 'notes': []}}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return path

    def test_new_layout_uses_creation_date_and_database_id(self):
        sessions.save_session(self.sid, self.messages, username='alice')
        path = sessions._session_path(self.sid, 'alice')
        self.assertEqual(path.relative_to(sessions.SESSIONS_DIR).parts,
                         ('20261118', str(self.user['id']), self.sid + '.json'))
        self.assertEqual(sessions.load_session(self.sid, 'alice')['user_id'], self.user['id'])
        self.assertEqual(sessions.list_sessions(username='alice')[0]['date'], '20261118')

    def test_old_directories_are_not_loaded_listed_or_deleted(self):
        for folder in [self.sid[:10], self.sid[:8], '2026-11-18']:
            with self.subTest(folder=folder):
                old = self.legacy(folder=folder)
                before = old.read_bytes()
                self.assertIsNone(sessions.load_session(self.sid, 'alice'))
                self.assertEqual(sessions.list_sessions(username='alice'), [])
                self.assertFalse(sessions.delete_session(self.sid, 'alice'))
                self.assertEqual(old.read_bytes(), before)

    def test_saving_uses_only_new_directory_without_migrating_old_files(self):
        old = self.legacy()
        before = old.read_bytes()
        messages = [{'role': 'user', 'content': '新记录'}]
        sessions.save_session(self.sid, messages, username='alice')
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(sessions.load_session(self.sid, 'alice')['messages'], messages)
        self.assertEqual(len(sessions.list_sessions(username='alice')), 1)

    def test_misplaced_json_cannot_be_listed(self):
        sessions.save_session(self.sid, self.messages, username='alice')
        path = sessions._session_path(self.sid, 'alice')
        wrong_date = sessions.SESSIONS_DIR / '20261119' / path.parent.name / path.name
        wrong_date.parent.mkdir(parents=True)
        path.replace(wrong_date)
        self.assertIsNone(sessions.load_session(self.sid, 'alice'))
        self.assertEqual(sessions.list_sessions(username='alice'), [])

    def test_unowned_and_missing_accounts_are_separate_and_path_safe(self):
        sessions.save_session(self.sid, self.messages)
        self.assertEqual(sessions._session_path(self.sid).parent.name, 'anonymous')
        other = '20261118_230001-abcd'
        sessions.save_session(other, self.messages, username='../../未知账户')
        path = sessions._session_path(other, '../../未知账户')
        self.assertTrue(path.parent.name.startswith('account_'))
        self.assertTrue(path.is_relative_to(sessions.SESSIONS_DIR))
        self.assertIsNone(sessions.load_session(other, 'alice'))

    def test_old_lock_paths_are_not_consulted(self):
        old = sessions.SESSIONS_DIR / self.sid[:10] / (self.sid + '.lock')
        old.parent.mkdir(parents=True)
        old.write_bytes(b'deleted')
        sessions.save_session(self.sid, self.messages, username='alice')
        self.assertIsNotNone(sessions.load_session(self.sid, 'alice'))
        self.assertEqual(old.read_bytes(), b'deleted')
        self.assertTrue(sessions.delete_session(self.sid, 'alice'))
        with self.assertRaises(sessions.SessionConflict):
            sessions.save_session(self.sid, self.messages, username='alice')

    def test_recreated_same_username_cannot_inherit_a_different_user_id(self):
        sessions.save_session(self.sid, self.messages, username='alice')
        path = sessions._session_path(self.sid, 'alice')
        before = path.read_bytes()
        with mock.patch('src.auth_db.get_user', return_value={'id': self.user['id'] + 1000}):
            self.assertIsNone(sessions.load_session(self.sid, 'alice'))
            self.assertEqual(sessions.list_sessions(username='alice'), [])
            self.assertFalse(sessions.delete_session(self.sid, 'alice'))
            with self.assertRaises(PermissionError):
                sessions.save_session(self.sid, self.messages, username='alice')
        self.assertEqual(path.read_bytes(), before)

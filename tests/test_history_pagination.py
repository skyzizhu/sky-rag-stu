"""历史会话页分页数据层：list_sessions(offset) 与 count_sessions 的隔离测试。"""

import unittest
from unittest import mock

from src import sessions
from tests._helpers import IngestHarness


class HistoryPaginationTests(IngestHarness):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(sessions, 'SESSIONS_DIR', self.knowledge.parent / 'sessions')
        patch.start()
        self.addCleanup(patch.stop)

    def message(self, text):
        return [{'role': 'user', 'content': text}]

    def _seed(self, count, username='alice'):
        # 后缀 32 位十六进制，满足 _SESSION_ID_RE，确保按预期 ID 落盘
        for i in range(count):
            sessions.save_session(f'20261125_0000{i:02d}-aaaa{i:08x}{i:024x}',
                                  self.message(f'问题{i}'), username=username)

    def test_offset_windows_slice_the_same_ordered_list(self):
        self._seed(5)
        full = sessions.list_sessions(limit=50, username='alice')
        self.assertEqual(len(full), 5)
        page1 = sessions.list_sessions(limit=2, username='alice', offset=0)
        page2 = sessions.list_sessions(limit=2, username='alice', offset=2)
        page3 = sessions.list_sessions(limit=2, username='alice', offset=4)
        self.assertEqual(page1 + page2 + page3, full)

    def test_count_matches_listing_and_ignores_other_users(self):
        self._seed(7, username='alice')
        self._seed(2, username='bob')
        self.assertEqual(sessions.count_sessions(username='alice'), 7)
        self.assertEqual(sessions.count_sessions(username='bob'), 2)
        self.assertEqual(sessions.count_sessions(), 0)  # 匿名记录不属于任何员工

    def test_offset_beyond_total_returns_empty(self):
        self._seed(3, username='alice')
        self.assertEqual(sessions.list_sessions(limit=5, username='alice', offset=99), [])

    def test_delete_all_sessions_only_touches_owner(self):
        self._seed(4, username='alice')
        self._seed(2, username='bob')
        removed = sessions.delete_all_sessions(username='alice')
        self.assertEqual(removed, 4)
        self.assertEqual(sessions.count_sessions(username='alice'), 0)
        self.assertEqual(sessions.count_sessions(username='bob'), 2)  # 他人的会话原样保留
        self.assertEqual(sessions.delete_all_sessions(username='alice'), 0)  # 再清一次为空操作


if __name__ == "__main__":
    unittest.main()

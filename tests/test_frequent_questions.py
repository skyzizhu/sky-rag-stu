"""问答页预设问题：按用户提问次数自动归纳的隔离测试（TempAuthDB，不触正式库）。"""

import ast
import unittest
from pathlib import Path

from src import auth_db
from tests._helpers import TempAuthDB


def ask(username, question, entry='web'):
    with auth_db._connect() as conn:
        conn.execute("INSERT INTO qa_logs(username, question, usage, entry, created_at) "
                     "VALUES(?,?,?,?,datetime('now','localtime'))",
                     (username, question, '{}', entry))


class FrequentQuestionTests(TempAuthDB):
    def setUp(self):
        super().setUp()
        auth_db.init_db()

    def test_no_history_returns_empty_for_default_fallback(self):
        self.assertEqual(auth_db.get_frequent_questions('newcomer'), [])
        source = Path('app.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == 'get_frequent_questions']
        self.assertEqual(len(calls), 1)  # 问答页按登录用户归纳
        self.assertIn('chat.ex.1', source)  # 不足时用内置示例补位

    def test_ranked_by_ask_count_then_recency(self):
        for _ in range(3):
            ask('alice', '常用问题A')
        ask('alice', '偶尔问题B')
        ask('alice', '偶尔问题C')
        ask('alice', '同样一次但更新D')
        self.assertEqual(auth_db.get_frequent_questions('alice'),
                         ['常用问题A', '同样一次但更新D', '偶尔问题C'])

    def test_duplicates_count_together_across_entries(self):
        ask('alice', '同一问题')
        ask('alice', '同一问题', entry='mcp_http')
        ask('alice', '另一问题')
        ranked = auth_db.get_frequent_questions('alice')
        self.assertEqual(ranked[0], '同一问题')
        self.assertEqual(len(ranked), 2)

    def test_limit_three_and_fill_is_ui_concern(self):
        for i in range(5):
            ask('alice', f'问题{i}')
        self.assertEqual(len(auth_db.get_frequent_questions('alice')), 3)

    def test_excludes_empty_and_overlong_questions(self):
        ask('alice', '')
        ask('alice', '超' * 101)
        ask('alice', '正常问题')
        self.assertEqual(auth_db.get_frequent_questions('alice'), ['正常问题'])

    def test_users_are_isolated(self):
        ask('alice', '甲的问题')
        ask('bob', '乙的问题')
        self.assertEqual(auth_db.get_frequent_questions('alice'), ['甲的问题'])
        self.assertEqual(auth_db.get_frequent_questions('bob'), ['乙的问题'])


if __name__ == '__main__':
    unittest.main()

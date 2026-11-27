"""MCP knowledge_search 多点意图预扩容的纯函数测试（不触真实检索服务）。"""

import unittest

from src.mcp_search_limits import MCP_SEARCH_MAX_TOKENS, MCP_SEARCH_MAX_TOP_K
from src.mcp_server import _multipoint_preexpansion


class MultipointPreexpansionTests(unittest.TestCase):
    def test_multipoint_markers_raise_to_channel_max(self):
        for q in ("合同履约保证金和质保金的比例上限分别是多少？什么时候退还？",
                  "秘密级数据外发需要谁审批？有什么额外要求？",
                  "密码长度至少多少位？多久更换一次？能不能与最近几次重复？"):
            top_k, tokens = _multipoint_preexpansion(q, 5, 4000)
            self.assertEqual((top_k, tokens), (MCP_SEARCH_MAX_TOP_K, MCP_SEARCH_MAX_TOKENS), q)

    def test_plain_question_keeps_original_budget(self):
        top_k, tokens = _multipoint_preexpansion("公司全称是什么", 5, 4000)
        self.assertEqual((top_k, tokens), (5, 4000))

    def test_empty_question_and_neutral_question_safe(self):
        # 空问题：不命中标记、不崩溃
        # None 表示"预算可自动放宽"，未命中标记时必须原样透传——换成具体值会让
        # max_evidence_tokens 不再算自动项，这类问题就失去缺证后的 token 自动扩容
        # （V3.9.1 预扩容引入过的回归，由 McpDocumentToolsTests 的长卡片用例捕获）
        self.assertEqual(_multipoint_preexpansion("", 5, None), (5, None))
        # 无标记的中性问题保持原预算
        top_k, tokens = _multipoint_preexpansion("公司全称是什么", 5, 4000)
        self.assertEqual((top_k, tokens), (5, 4000))
        self.assertEqual(_multipoint_preexpansion("公司全称是什么", 5, None), (5, None))

    def test_list_marker_still_expands(self):
        top_k, tokens = _multipoint_preexpansion("列出所有制度", 1, None)
        self.assertEqual((top_k, tokens), (MCP_SEARCH_MAX_TOP_K, MCP_SEARCH_MAX_TOKENS))


if __name__ == "__main__":
    unittest.main()

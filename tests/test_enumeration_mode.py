"""枚举通道（V3.8.1）：列举/计数类提问的检索全量验证。

核心验收：构造"单文档 24 张功能卡"的场景（模拟 Xtools 首页），中文问
「列出全部功能」时——全部 24 张卡片进入上下文（召回 50 / 名额 40 /
同文档放开 / token 12000），不再受"Top 5 + 同文档 3"的限制。
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from src.pipeline import answer_question
from tests._helpers import IngestHarness


def doc_markdown(n: int = 24, body_len: int = 200) -> str:
    """生成 n 个 H3 功能章节的网页式 Markdown（每个章节 ≈ body_len 字）。"""
    sections = "\n".join(
        f"### 功能 {i:02d}\n这是功能 {i:02d} 的详细说明，包含核心能力、适用场景与使用限制的完整描述{i}。"
        for i in range(1, n + 1)
    )
    return f"# 测试产品功能总览\n\n这是一款测试产品的完整功能清单。\n\n{sections}\n"


class EnumerationChannelTests(IngestHarness):
    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()

    def ask(self, question: str, top_k: int = 5, use_rerank: bool = False):
        with contextlib.redirect_stdout(io.StringIO()):
            return answer_question(question, top_k=top_k, use_llm=False,
                                   use_rerank=use_rerank,
                                   use_query_understanding=False)

    def ingest_feature_doc(self, n_sections: int = 24) -> str:
        path = self.knowledge / "learning" / "general" / "big.md"
        path.write_text(doc_markdown(n_sections), encoding="utf-8")
        self.ingest([path])
        return path

    def test_enumeration_lists_all_cards(self):
        """核心验收：24 卡文档 + 中文列举提问 → 全部内容进入上下文。

        注意：相邻补全会把序号连续的卡片合并为 1 条完整资料——
        所以验证的是【上下文内容覆盖全部 24 个功能】，不是资料条数。
        """
        self.ingest_feature_doc(24)
        result = self.ask("列出该产品的全部功能")
        self.assertEqual(len(result.sources), 1)  # 相邻补全合并为 1 条完整资料
        for i in range(1, 25):  # 24 个功能的说明文字都必须在上下文里
            self.assertIn(f"这是功能 {i:02d} 的详细说明", result.context,
                          f"功能 {i} 缺失")
        self.assertTrue(all(s["domain"] == "learning" for s in result.sources))

    def test_pool_covers_full_document_at_recall_stage(self):
        """召回池（枚举模式 50）覆盖全文档：24 节碎小节经短小节合并（≤600）压成
        少数几张卡，池子装得下全部卡片。"""
        self.ingest_feature_doc(24)
        # 直接断言召回层（绕过组装）也能取出全部合并后的卡片
        from src.retriever import get_retriever, effective_filters
        items = get_retriever().retrieve("功能", top_k=50,
                                         filters=effective_filters({"domain": "learning"}),
                                         use_hybrid=False)
        self.assertGreaterEqual(len(items), 2)  # 24 节合并后约 2 张卡
        joined = "".join(it.text for it in items)
        for i in range(1, 25):
            self.assertIn(f"功能 {i:02d}", joined, f"功能 {i} 缺失")

    def test_non_enumeration_unchanged_top5_perdoc3(self):
        """普通提问（非列举）：仍是 Top 5、同文档 ≤ 3——现状不劣化。"""
        for i in range(1, 3):  # 两个文档，各 10 张卡
            self.ingest([self.write_note(f"文档{ i }的内容，" * 30, f"d{ i }.md")])
        result = self.ask("这些文档讲了什么内容")
        self.assertEqual(len(result.sources), 2)  # 两个文档各 1 张卡，全部入选

    def test_rerank_skipped_in_enumeration_mode(self):
        """枚举模式跳过精排（省一次 LLM 调用）：rerank_items 不被调用，
        且调试时间线明确显示「跳过（枚举模式）」——不能让精排节点无声消失。"""
        self.ingest_feature_doc(24)
        sentinel = mock.Mock(side_effect=AssertionError("枚举模式不应调用精排"))
        with mock.patch("src.pipeline.rerank_items", sentinel), \
             mock.patch("src.pipeline.get_llm_client") as glc:
            fake_llm = mock.Mock()
            fake_llm.chat_stream.return_value = iter(["回答"])
            fake_llm.chat.return_value = ("回答", 0.1)
            glc.return_value = fake_llm
            with contextlib.redirect_stdout(io.StringIO()):
                result = answer_question("列出该产品的全部功能", top_k=5,
                                         use_llm=True, use_rerank=True,
                                         use_query_understanding=False)
        sentinel.assert_not_called()
        self.assertTrue(result.answer or result.retrieved)
        rerank_nodes = [n for n in result.trace if "Rerank" in str(n.get("name", ""))]
        self.assertEqual(len(rerank_nodes), 1)
        self.assertEqual(rerank_nodes[0].get("status"), "跳过（枚举模式）")


class EnumeratePromptGuardTests(unittest.TestCase):
    """兜底生成的枚举模式提示：没有目标类别条目就如实说，不拿字面关联凑数。"""

    def test_prompt_forbids_padding_missing_items(self):
        from src.prompt import build_messages
        messages = build_messages('资料附页', '知识库里有哪些建模方法？',
                                  enumerate_mode=True,
                                  evidence_meta={'coverage': 'partial', 'route': 'document'})
        system = messages[0]['content']
        self.assertIn('直接如实说明未找到', system)
        self.assertIn('不得把仅有字面关联', system)
        self.assertIn('coverage=partial', system)




class EnumerationRoutingRigorTests(IngestHarness):
    """列举路由严谨性：语义判定为主，词表分级（强/弱信号）兜底。"""

    def setUp(self):
        super().setUp()
        self.use_fakes()

    def _ingest_doc(self, n_sections: int = 2):
        sections = "\n".join(f"### 功能 {i:02d}\n这是功能 {i:02d} 的详细说明。" for i in range(1, n_sections + 1))
        path = self.knowledge / "learning" / "general" / "rigor.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# 测试文档\n\n{sections}\n", encoding="utf-8")
        self.ingest([path])
        return path

    def _ask_with_qu(self, question, qu_enumeration):
        from types import SimpleNamespace
        qu = SimpleNamespace(ok=True, enumeration=qu_enumeration, intent='', vector_query='',
                             keyword_query=[], filters={}, sub_queries=[], time_range='',
                             enumeration_scope='', usage={}, elapsed=0.0,
                             system_prompt='', user_prompt='', raw_output='', error='')
        llm = mock.Mock()
        llm.chat.return_value = (json.dumps({'items': []}), 0.0)
        llm.chat_stream.return_value = iter(['普通回答'])
        with mock.patch('src.pipeline.understand_query', return_value=qu),              mock.patch('src.pipeline.get_llm_client', return_value=llm),              contextlib.redirect_stdout(io.StringIO()):
            return answer_question(question, use_llm=True, use_rerank=False,
                                   use_query_understanding=True, config=self.cfg)

    def test_weak_keyword_with_semantic_negative_stays_ordinary(self):
        """语义明确否定 + 仅弱词（有哪些）命中 → 不进入列举重通道。"""
        self._ingest_doc(2)
        result = self._ask_with_qu('这个方法有哪些缺点？', qu_enumeration=False)
        self.assertFalse(result.enumeration)
        self.assertNotIn('stage2', result.evidence_meta)
        self.assertEqual(result.answer, '普通回答')

    def test_strong_keyword_overrides_semantic_negative(self):
        """强信号（列出）防语义漏判：QU 误判 false 仍进列举通道。"""
        self._ingest_doc(2)
        result = self._ask_with_qu('把使用RAG的工具列出来', qu_enumeration=False)
        self.assertTrue(result.enumeration)
        self.assertEqual(result.evidence_meta.get('stage2', {}).get('status'), 'executed')

    def test_keyword_fallback_when_query_understanding_disabled(self):
        """QU 关闭时词表全量兜底（旧行为保持）：弱词也能触发。"""
        self._ingest_doc(2)
        llm = mock.Mock()
        llm.chat.return_value = (json.dumps({'items': []}), 0.0)
        llm.chat_stream.return_value = iter(['普通回答'])
        with mock.patch('src.pipeline.get_llm_client', return_value=llm),              contextlib.redirect_stdout(io.StringIO()):
            result = answer_question('有哪些项目使用RAG', use_llm=True, use_rerank=False,
                                     use_query_understanding=False, config=self.cfg)
        self.assertTrue(result.enumeration)




if __name__ == "__main__":
    unittest.main()

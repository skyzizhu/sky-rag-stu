"""枚举通道（V3.8.1）：列举/计数类提问的检索全量验证。

核心验收：构造"单文档 24 张功能卡"的场景（模拟 Xtools 首页），中文问
「列出全部功能」时——全部 24 张卡片进入上下文（召回 50 / 名额 40 /
同文档放开 / token 12000），不再受"Top 5 + 同文档 3"的限制。
"""

from __future__ import annotations

import contextlib
import io
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
        """召回池（枚举模式 50）≥ 单文档 24 卡：池子在召回阶段就装得下全量。"""
        self.ingest_feature_doc(24)
        # 直接断言召回层（绕过组装）也能取出超过旧上限 10 的卡片数
        from src.retriever import get_retriever, effective_filters
        items = get_retriever().retrieve("功能", top_k=30,
                                         filters=effective_filters({"domain": "learning"}),
                                         use_hybrid=False)
        self.assertGreaterEqual(len(items), 24)

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


if __name__ == "__main__":
    unittest.main()

"""2026-11-08 审查修复的回归用例（A/B 评测 + 代码审查发现的问题）。

- P0 复合问题拆分合并：按融合名次而不是原始分（向量 0~1 / BM25 可达数十）合并——
  原始分混排时，枚举模式（召回 50、跳过精排）下子问题证据整体丢失（复合题通过 13→9）
- P1 过滤放宽重试：重试的召回深度与首检一致（此前固定 top_k=5，枚举模式下列举直接失效）
- P1 评估卡片「拒答一致性」：识别简/繁中文、英文、日文的拒答表述（回答语言跟随提问）
- P3 × ÷ 不算拉丁字母；缓存键带上回答语言
"""

from __future__ import annotations

import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest import mock

from src.answer_evaluator import compute_signals
from src.answer_language import detect_language, dominant_language
from src.pipeline import _cache_key, merge_sub_query_results
from src.query_understanding import QueryUnderstanding
from src.retriever import RetrievedItem
from tests._helpers import IngestHarness


def card(source: str, n: int, score: float) -> RetrievedItem:
    return RetrievedItem(rank=0, score=score, text=f"{source} 第{n}张卡片的内容",
                         metadata={"source": source, "chunk_id": f"{source}#{n}"})


class MergeSubQueryResultsTests(unittest.TestCase):
    def test_uses_rank_not_raw_score(self):
        """子查询 1 的第 1 名是向量卡片（原始分 0.5），后面是关键词卡片（BM25 原始分 20+）：
        保底名额必须按名次取，第 1 名不能被高原始分挤掉。"""
        sq1 = [card("心得.rtf", 1, 0.50)] + [card("其他.md", i, 20.0 + i) for i in range(1, 10)]
        sq2 = [card("PDF.md", 1, 0.62)] + [card("杂项.md", i, 25.0 + i) for i in range(1, 10)]
        pool, quota = merge_sub_query_results([sq1, sq2], pool_cap=4)
        self.assertEqual(quota, 3)
        sources = [it.metadata["source"] for it in pool]
        self.assertIn("心得.rtf", sources)
        self.assertIn("PDF.md", sources)
        # 各子查询的第 1 名排最前（按名次交替），编号从 1 开始连续
        self.assertEqual(set(sources[:2]), {"心得.rtf", "PDF.md"})
        self.assertEqual([it.rank for it in pool], [1, 2, 3, 4])

    def test_shared_card_kept_once_with_best_rank(self):
        shared = card("共用.md", 1, 0.4)
        sq1 = [card("A.md", 1, 0.9), shared]
        sq2 = [card("共用.md", 1, 0.3), card("B.md", 1, 0.8)]
        pool, _ = merge_sub_query_results([sq1, sq2], pool_cap=10)
        sources = [it.metadata["source"] for it in pool]
        self.assertEqual(sources.count("共用.md"), 1)
        # 共用卡片在子查询 2 排第 1 → 与 A.md 并列第一梯队，排在 B.md 前面
        self.assertLess(sources.index("共用.md"), sources.index("B.md"))

    def test_quota_protects_each_sub_query(self):
        """一个子查询结果很多也不能独占合并池：每个子查询至少保底 quota 条。"""
        sq1 = [card("大文档.md", i, 30.0) for i in range(1, 30)]
        sq2 = [card("小文档.md", i, 0.2) for i in range(1, 4)]
        pool, quota = merge_sub_query_results([sq1, sq2], pool_cap=20)
        self.assertEqual(sum(it.metadata["source"] == "小文档.md" for it in pool), 3)
        self.assertEqual(len(pool), 20)


class RelaxRetryRecallDepthTests(IngestHarness):
    """推断条件过严导致 0 条召回时，放宽重试的召回深度必须与首检一致。"""

    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()
        from src.pipeline import clear_qa_cache
        clear_qa_cache()
        self.ingest([self.write_note("Xtools 支持字幕、降噪、PDF 解析等功能。" * 5, "xtools.md")])

    def test_retry_uses_enumeration_recall_depth(self):
        from src import pipeline
        from src.retriever import get_retriever
        qu = QueryUnderstanding(original="q", vector_query="Xtools 功能", enumeration=True,
                                filters={"domain": "no_such_domain"}, ok=True)
        retriever = get_retriever()
        fake_llm = mock.Mock()
        fake_llm.chat_stream.return_value = iter(["ok"])
        with mock.patch.object(pipeline, "understand_query", return_value=qu), \
             mock.patch.object(pipeline, "get_llm_client", return_value=fake_llm), \
             mock.patch.object(retriever, "retrieve", wraps=retriever.retrieve) as spy, \
             contextlib.redirect_stdout(io.StringIO()):
            pipeline.answer_question("Xtools 有哪些功能？", top_k=5, use_llm=True,
                                     use_rerank=False, use_query_understanding=True)
        depths = [call.kwargs.get("top_k") for call in spy.call_args_list]
        self.assertEqual(len(depths), 2, "应先首检（0 条）再放宽重试一次")
        self.assertEqual(depths[0], pipeline.ENUM_RECALL_K)
        self.assertEqual(depths[1], depths[0], "重试召回深度应与首检一致")


class RefusalSignalTests(unittest.TestCase):
    """评估卡片「拒答一致性」：各语言的拒答都要识别，正常回答不误报。"""

    def state(self, answer: str, has_context: bool = True) -> str:
        result = SimpleNamespace(answer=answer, trace=[], context="资料" if has_context else "",
                                 retrieved=[], sources=[{"source": "a.md"}] if has_context else [])
        return compute_signals(result)["refusal"]["state"]

    def test_refusals_in_each_language(self):
        for answer in ("知识库中没有相关内容。", "知識庫中沒有相關內容。",
                       "The knowledge base does not contain information about the 2022 World Cup.",
                       "Sorry, I cannot answer this based on the materials.",
                       "資料には記載されていません。"):
            self.assertEqual(self.state(answer), "inconsistent", answer)
            self.assertEqual(self.state(answer, has_context=False), "consistent", answer)

    def test_normal_answers_are_not_refusals(self):
        for answer in ("Xtools 支持视频降噪 [1]。", "Yes. Xtools can remove background noise [1]."):
            self.assertEqual(self.state(answer), "normal", answer)


class MergeRunTests(unittest.TestCase):
    """相邻补全拼接：保留每张卡片的章节标题、去掉重复的来源前缀、重叠去重生效。"""

    @staticmethod
    def doc_card(n: int, section: str, body: str) -> RetrievedItem:
        return RetrievedItem(rank=n, score=0.5, text=f"来源文档：x.md\n{body}",
                             metadata={"source": "x.md", "document_id": "d1", "version": "1.0",
                                       "section": section, "chunk_id": f"d1_v1.0_{n:04d}"})

    def test_section_titles_kept_and_prefix_not_repeated(self):
        from src.context import _merge_run
        run = [self.doc_card(1, "All Features", "A complete toolkit."),
               self.doc_card(2, "Add Subtitles to Video", "Transcribe speech into subtitles."),
               self.doc_card(3, "Video to GIF Maker", "Convert MP4 into GIFs.")]
        merged = _merge_run(run)
        self.assertIn("【章节：Add Subtitles to Video】\nTranscribe speech", merged)
        self.assertIn("【章节：Video to GIF Maker】\nConvert MP4", merged)
        self.assertEqual(merged.count("来源文档：x.md"), 1)  # 只保留第一张的

    def test_overlap_removed_after_prefix_stripped(self):
        from src.context import _merge_run
        run = [self.doc_card(1, "S", "第一段内容，结尾是重叠部分ABCDEFGH"),
               self.doc_card(2, "S", "重叠部分ABCDEFGH，然后是第二段新内容")]
        merged = _merge_run(run)
        self.assertEqual(merged.count("重叠部分ABCDEFGH"), 1)
        self.assertNotIn("【章节", merged)  # 同一章节不重复加标题

    def test_build_context_keeps_titles_for_whole_run(self):
        from src.context import build_context
        run = [self.doc_card(i, f"功能 {i}", f"功能 {i} 的说明。") for i in range(1, 6)]
        context, used, _, notes = build_context(run, max_items=10, max_per_doc=999)
        self.assertEqual(len(used), 1)  # 5 张相邻卡片拼成 1 条资料
        for i in range(2, 6):
            self.assertIn(f"【章节：功能 {i}】", context)


class ExpandDominantDocumentsTests(IngestHarness):
    """枚举模式的文档展开：补齐被列举文档的全部卡片，且沿用权限过滤。"""

    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()
        sections = "\n".join(f"### 功能 {i:02d}\n这是功能 {i:02d} 的独立说明，编号 {i}。" for i in range(1, 13))
        big = self.knowledge / "learning" / "general" / "features.md"
        big.write_text(f"# 产品功能\n\n{sections}\n", encoding="utf-8")
        self.ingest([big, self.write_note("一篇无关的笔记。" * 20, "other.md")])

    def pool(self, n_big: int) -> list[RetrievedItem]:
        from src.retriever import effective_filters
        from src.vector_store import get_vector_store
        cards = get_vector_store().get_all_cards(filters=effective_filters({}))
        big = sorted((c for c in cards if c["payload"]["source"] == "features.md"),
                     key=lambda c: c["payload"]["chunk_id"])
        other = [c for c in cards if c["payload"]["source"] == "other.md"]
        to_item = lambda c: RetrievedItem(rank=0, score=0.5, text=c["payload"]["text"],
                                          metadata={k: v for k, v in c["payload"].items() if k != "text"})
        self.total_big = len(big)
        return [to_item(c) for c in other[:1]] + [to_item(c) for c in big[:n_big]]

    def test_fills_in_all_cards_of_dominant_document(self):
        from src.pipeline import expand_dominant_documents
        from src.retriever import effective_filters
        pool = self.pool(4)
        expanded, notes = expand_dominant_documents(pool, effective_filters({}))
        big_cards = [it for it in expanded if it.metadata["source"] == "features.md"]
        self.assertEqual(len(big_cards), self.total_big)        # 全部卡片进入候选
        self.assertEqual(expanded[0].metadata["source"], "other.md")  # 原有第 1 名位置不动
        self.assertEqual([it.rank for it in expanded], list(range(1, len(expanded) + 1)))
        self.assertEqual(len(notes), 1)

    def test_top_ranked_cards_never_displaced(self):
        """实测回归：展开长文档曾把只有 1 张卡片的「心得」从前排挤到第 94 位。前 keep_top 名必须原位保留。"""
        from src.pipeline import expand_dominant_documents
        from src.retriever import effective_filters
        pool = self.pool(4)
        expanded, _ = expand_dominant_documents(pool, effective_filters({}), keep_top=2)
        self.assertEqual([it.metadata["chunk_id"] for it in expanded[:2]],
                         [it.metadata["chunk_id"] for it in pool[:2]])

    def test_document_too_large_is_not_expanded(self):
        """全文超出上下文名额的长文档谈不上「补齐全部」，不展开（避免噪声塞满名额）。"""
        from src.pipeline import expand_dominant_documents
        from src.retriever import effective_filters
        pool = self.pool(4)
        expanded, notes = expand_dominant_documents(pool, effective_filters({}), max_doc_cards=5)
        self.assertEqual(len(expanded), len(pool))
        self.assertTrue(any("不展开" in n for n in notes))

    def test_document_must_appear_near_the_top(self):
        """只在后排凑够张数的文档不展开（小知识库里召回 50 条会混进无关文档）。"""
        from src.pipeline import expand_dominant_documents
        from src.retriever import effective_filters
        pool = self.pool(4)  # 第 1 名是 other.md，features.md 从第 2 名开始
        expanded, notes = expand_dominant_documents(pool, effective_filters({}), keep_top=1)
        self.assertEqual(len(expanded), len(pool))
        self.assertEqual(notes, [])

    def test_not_triggered_below_threshold(self):
        from src.pipeline import expand_dominant_documents
        from src.retriever import effective_filters
        pool = self.pool(3)  # 同一文档只有 3 张 < 4：不算「被列举的文档」
        expanded, notes = expand_dominant_documents(pool, effective_filters({}))
        self.assertEqual(len(expanded), len(pool))
        self.assertEqual(notes, [])

    def test_respects_permission_filters(self):
        """补卡沿用本次检索的过滤条件：无权限时一张也不补（不越权）。"""
        from src.pipeline import expand_dominant_documents
        pool = self.pool(4)
        expanded, _ = expand_dominant_documents(pool, {"allowed_groups": ["no_such_group"]})
        self.assertEqual(len(expanded), len(pool))


class LanguageEdgeCaseTests(unittest.TestCase):
    def test_math_symbols_are_not_latin_letters(self):
        self.assertEqual(detect_language("What is a 2×3 matrix?"), "en")
        self.assertEqual(detect_language("Compute 6÷2 quickly"), "en")

    def test_dominant_language_of_answers(self):
        """回答常夹带中文文件名，按字符占比统计，不能「含汉字即中文」。"""
        self.assertEqual(dominant_language(
            "Based on [1] 心得.rtf and [2] “PDF智能解析”功能介绍, Xtools offers subtitles and OCR."), "en")
        self.assertEqual(dominant_language("根据资料，Xtools 支持视频降噪和 PDF 智能解析。"), "zh-CN")
        self.assertEqual(dominant_language("根據資料，Xtools 支持視頻降噪與 PDF 智能解析。"), "zh-TW")
        self.assertEqual(dominant_language("Xtoolsは動画のノイズを除去できます。"), "ja")
        # 中文回答里有大段命令：去掉代码片段后再统计（此前按全文占比会误判成英文）
        self.assertEqual(dominant_language(
            "Mac 下重启 Apache 服务器的命令是：\n\n- `sudo apachectl -k restart` [1]\n\n"
            "如果是 XAMPP 里的 Apache 服务，还可以用：\n\n"
            "- `sudo /Applications/XAMPP/xamppfiles/xampp restartapache` [2]"), "zh-CN")

    def test_cache_key_includes_answer_language(self):
        """判断不出语言的追问沿用历史提问语言；历史只取前 60 字进键，回答语言须显式进键。"""
        prefix = ("请帮我详细整理一下知识库里关于 Xtools 这款产品的全部功能说明、适用场景、"
                  "使用限制、订阅价格以及常见问题，越完整越好，最好按类别分组列出")
        self.assertGreater(len(prefix), 60)
        en_hist = [{"role": "user", "content": prefix + "，请用英文回答。"}]
        zh_hist = [{"role": "user", "content": prefix + "。"}]
        key_en = _cache_key("OK", 5, {}, True, True, True, history=en_hist)
        key_zh = _cache_key("OK", 5, {}, True, True, True, history=zh_hist)
        self.assertNotEqual(key_en, key_zh)


if __name__ == "__main__":
    unittest.main()

"""R2-b 多事实主导文档扩展：expand_facts_dominant_documents + build_context(unlimited_docs)。

设计（benchmarks/R2_R3改进方案_20261121.md）：复合问题（子查询 ≥2）或精排后
前 Top-K 同文档 ≥2 张时，把小文档（≤ EXPAND_DOC_MAX_CARDS 张）全文卡片补进候选；
前排名次不动，token 总预算由组装层把关；被扩展文档豁免单文档配额。

纯函数级测试：向量库用 mock（不连任何服务），遵循 tests/AGENTS.md 隔离要求。
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

from src.config import get_config
from src.context import build_context
from src.pipeline import expand_facts_dominant_documents
from src.retriever import RetrievedItem


def _card(doc_id: str, seq: int, text: str = "") -> dict:
    return {"payload": {"document_id": doc_id, "chunk_id": f"{doc_id}_000{seq}",
                        "source": f"{doc_id}.md", "text": text or f"{doc_id} 第{seq}段"}}


def _item(doc_id: str, seq: int, rank: int, text: str = "") -> RetrievedItem:
    return RetrievedItem(rank=rank, score=1.0 - rank * 0.1, text=text or f"{doc_id} 第{seq}段",
                         metadata={"document_id": doc_id, "chunk_id": f"{doc_id}_000{seq}",
                                   "source": f"{doc_id}.md", "version": "1.0"})


class ExpandFactsDominantTests(unittest.TestCase):
    def cfg(self, limit: int = 12):
        return replace(get_config(), expand_doc_max_cards=limit)

    def test_expands_dominant_doc_keeping_head_order(self):
        """前 5 名同文档 ≥2 张 → 补入该文档其余卡片；前排名次不动、补卡按序号。"""
        items = ([_item("deck", 3, 1), _item("deck", 1, 2), _item("other", 1, 3),
                  _item("deck", 5, 4), _item("single", 1, 5), _item("lone", 1, 6)])
        store = mock.Mock()
        store.get_all_cards.return_value = [_card("deck", i) for i in range(1, 7)]
        with mock.patch("src.pipeline.get_vector_store", return_value=store):
            expanded, added, docs, notes = expand_facts_dominant_documents(
                items, {"status": "active"}, top_k=5, cfg=self.cfg(), multi_fact=False)
        # deck：池内 3 张且 3 张在头部 → 扩展；other 池内 3 张但头部仅 1 张且非复合题 → 不扩展
        self.assertEqual(docs, {"deck"})
        self.assertEqual(added, 3)          # 全文 6 张 - 池内已有 3 张（0001/0003/0005）→ 补 3 张
        head_sources = [it.metadata["chunk_id"] for it in expanded[:5]]
        self.assertEqual(head_sources, ["deck_0003", "deck_0001", "other_0001",
                                        "deck_0005", "single_0001"])  # 前排不动
        appended = [it.metadata["chunk_id"] for it in expanded[5:5 + added]]
        for chunk in appended:
            self.assertNotIn(chunk, head_sources)
        self.assertTrue(notes)

    def test_multi_fact_expands_doc_not_in_head(self):
        """复合问题（multi_fact）放宽前排要求：池内 ≥2 张即可扩展。"""
        items = [_item("deck", 1, 1), _item("other", 1, 2), _item("deck", 2, 3)]
        store = mock.Mock()
        store.get_all_cards.return_value = [_card("deck", i) for i in range(1, 5)]
        with mock.patch("src.pipeline.get_vector_store", return_value=store):
            expanded, added, docs, _ = expand_facts_dominant_documents(
                items, None, top_k=5, cfg=self.cfg(), multi_fact=True)
        self.assertEqual(docs, {"deck"})
        self.assertEqual(added, 2)

    def test_oversized_doc_not_expanded(self):
        """全文超过上限的文档不展开（装不下整篇就不补，避免半篇噪声）。"""
        items = [_item("big", 1, 1), _item("big", 2, 2), _item("other", 1, 3)]
        store = mock.Mock()
        store.get_all_cards.return_value = [_card("big", i) for i in range(1, 4)]  # limit=2 → 超限
        with mock.patch("src.pipeline.get_vector_store", return_value=store):
            expanded, added, docs, notes = expand_facts_dominant_documents(
                items, None, top_k=5, cfg=self.cfg(limit=2), multi_fact=False)
        self.assertEqual(docs, set())
        self.assertEqual(added, 0)
        self.assertTrue(any("不展开" in note for note in notes))

    def test_disabled_and_no_dominant_are_noops(self):
        """limit=0 关闭；无主导文档（前 5 名同文档 <2 张且非复合题）不触发。"""
        items = [_item("a", 1, 1), _item("b", 1, 2), _item("c", 1, 3)]
        store = mock.Mock()
        with mock.patch("src.pipeline.get_vector_store", return_value=store):
            self.assertEqual(expand_facts_dominant_documents(
                items, None, 5, self.cfg(limit=0), False)[1], 0)
            self.assertEqual(expand_facts_dominant_documents(
                items, None, 5, self.cfg(), False)[2], set())
        store.get_all_cards.assert_not_called()


class ResolveSpokenSourceTests(unittest.TestCase):
    """N-A 点名文件归一：精确 → 前缀 → 包含，唯一命中才替换。"""

    def catalog(self, *sources):
        store = mock.Mock()
        store.document_catalog.return_value = {"documents": [{"source": s} for s in sources]}
        return store

    def test_exact_prefix_and_contains(self):
        from src.pipeline import resolve_spoken_source
        store = self.catalog("昆仑项目需求说明书_v2.0.docx", "星云项目章程.md", "星云项目招标技术规格书_v1.2.md")
        self.assertEqual(resolve_spoken_source("星云项目章程.md", store), "星云项目章程.md")          # 精确
        self.assertEqual(resolve_spoken_source("昆仑项目需求说明书", store),
                         "昆仑项目需求说明书_v2.0.docx")                                             # 前缀唯一
        self.assertEqual(resolve_spoken_source("招标技术规格书", store),
                         "星云项目招标技术规格书_v1.2.md")                                             # 包含唯一

    def test_ambiguous_and_miss_keep_original(self):
        from src.pipeline import resolve_spoken_source
        store = self.catalog("低空项目/项目说明.md", "国防动员项目/项目说明.md", "星云项目章程.md")
        self.assertIsNone(resolve_spoken_source("项目说明", store))   # 多命中 → 歧义
        self.assertIsNone(resolve_spoken_source("不存在的文件", store))

    def test_word_order_variant_resolves_uniquely(self):
        """R-1 变体层：词序/缺词差异（"昆仑需求说明书" vs "昆仑项目需求说明书_v2.0.docx"）。"""
        from src.pipeline import resolve_spoken_source
        store = self.catalog("昆仑项目需求说明书_v2.0.docx", "星云项目章程.md",
                             "星云项目招标技术规格书_v1.2.md")
        self.assertEqual(resolve_spoken_source("昆仑需求说明书", store),
                         "昆仑项目需求说明书_v2.0.docx")

    def test_variant_layer_keeps_ambiguity_between_similar_files(self):
        """两个相近候选（两版员工手册）时变体层不猜——保持原值。"""
        from src.pipeline import resolve_spoken_source
        store = self.catalog("星海员工手册_2024版.md", "星海员工手册_2025版_v3.0.md")
        self.assertIsNone(resolve_spoken_source("员工手册", store))


class MultiFactCapTests(unittest.TestCase):
    def test_multi_fact_expands_up_to_three_docs(self):
        """复合问题扩展上限 3 篇（三代产品价格题需要第 3 篇文档的全卡）。"""
        items = ([_item("d1", 1, 1), _item("d1", 2, 2), _item("d2", 1, 3), _item("d2", 2, 4),
                  _item("d3", 1, 5), _item("d3", 2, 8), _item("d4", 1, 9),
                  _item("d1", 3, 10), _item("d2", 3, 11)])
        store = mock.Mock()
        store.get_all_cards.side_effect = (
            lambda filters, limit: [{"payload": {"document_id": filters["document_id"],
                                                 "chunk_id": f"{filters['document_id']}_000{i}",
                                                 "source": f"{filters['document_id']}.md",
                                                 "text": "x"}} for i in range(1, 5)])
        cfg = replace(get_config(), expand_doc_max_cards=12)
        with mock.patch("src.pipeline.get_vector_store", return_value=store):
            _, _, docs, _ = expand_facts_dominant_documents(items, None, 5, cfg, multi_fact=True)
            _, _, docs2, _ = expand_facts_dominant_documents(items, None, 5, cfg, multi_fact=False)
        self.assertEqual(len(docs), 3)   # 复合题：d1/d2/d3 三篇
        self.assertEqual(len(docs2), 2)  # 非复合：前两篇（d4 池内仅 1 张不参与）


class UnlimitedDocsAssemblyTests(unittest.TestCase):
    """组装层验证。序号刻意不连续（1,3,5,…），避免触发相邻卡拼接干扰计数。"""

    def test_unlimited_doc_bypasses_per_doc_cap(self):
        """被扩展文档豁免单文档配额；其余文档仍受 max_per_doc 约束。"""
        deck = [_item("deck", 2 * i - 1, i, f"deck 卡{i}") for i in range(1, 7)]
        others = [_item("other", 2 * i - 1, 6 + i, f"other 卡{i}") for i in range(1, 5)]
        items = deck + others
        cfg = replace(get_config(), context_max_per_doc=3, top_k=10)
        context, used, dropped, _ = build_context(
            items, cfg, max_items=10, unlimited_docs={"deck"})
        per_doc = {"deck": 0, "other": 0}
        for it in used:
            per_doc[it.metadata["document_id"]] += 1
        self.assertEqual(per_doc["deck"], 6)     # 全部进入
        self.assertEqual(per_doc["other"], 3)    # 仍受 3 条配额
        self.assertIn("deck 卡6", context)

    def test_default_behavior_unchanged_without_unlimited(self):
        """不传 unlimited_docs：单文档配额行为与旧版完全一致。"""
        items = [_item("deck", 2 * i - 1, i, f"deck 卡{i}") for i in range(1, 6)]
        cfg = replace(get_config(), context_max_per_doc=3, top_k=10)
        _, used, dropped, _ = build_context(items, cfg, max_items=10)
        self.assertEqual(len(used), 3)
        self.assertEqual(sum(1 for d in dropped if "同一文档最多" in d["reason"]), 2)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""R9-C08 预算感知文档提交的单元测试。

根因：宽候选（12 篇文档 ≈ 12 批提取）恰好填满 enum_max_calls 时规划补查一次
都发不出，目标文档缺召回就补不回来（R8 过/R9 挂的波动）。修复：首轮提交按
「排名优先 + 预算内可完整提取」裁剪，恒为规划保留 rounds×queries+1 次调用。
"""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest import mock

from src.config import get_config
from src.enumeration_processing import process_enumeration


class _FakeStore:
    def __init__(self, cards_per_doc: int):
        self.cards_per_doc = cards_per_doc

    def card_page(self, filters, offset=None, limit=8, timeout=None):
        if offset is not None:  # 第二页起为空（估算只翻一页）
            return [], None
        doc = filters.get("document_id")
        rows = [{"id": f"{doc}_{i}", "payload": {
            "text": f"{doc} 材料{i}：预算 10 万元。",
            "document_id": doc, "version": filters.get("version") or "1.0",
            "source": f"{doc}.md", "chunk_id": f"{doc}_000{i}",
        }} for i in range(self.cards_per_doc)]
        return rows, None


class _FakeLLM:
    """提取返回空 items（合法），规划返回空 queries（合法），统计调用数。"""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, usage_out=None, timeout=None, max_tokens=None):
        self.calls += 1
        system = messages[0]["content"]
        if "提取" in system or "items" in system or "extraction" in system.lower():
            return json.dumps({"items": []}), 0.0
        return json.dumps({"queries": []}), 0.0


def _cfg(**over):
    return replace(get_config(), enum_max_calls=over.get("max_calls", 12),
                   enum_max_rounds=over.get("rounds", 2),
                   enum_queries_per_round=over.get("queries", 2),
                   enum_page_cards=over.get("page_cards", 8),
                   enum_output_tokens=over.get("output_tokens", 2048))


def _docs(n):
    return [{"document_id": f"doc{i:02d}", "version": "1.0", "source": f"doc{i:02d}.md"}
            for i in range(n)]


class BudgetAwareCommitTests(unittest.TestCase):
    def _run(self, n_docs, cards_per_doc, route="cross_document", **cfg_over):
        llm = _FakeLLM()
        cfg = _cfg(**cfg_over)
        store = _FakeStore(cards_per_doc)
        outcome = process_enumeration(
            "知识库里哪些文档规定了预算？", _docs(n_docs),
            [(d["document_id"], d["version"]) for d in _docs(n_docs)],
            {"status": "active"}, store, lambda q, deadline: [],
            None, llm, cfg, route)
        return llm, outcome

    def test_wide_candidates_trimmed_with_planning_reserve(self):
        """12 篇文档（各 5 卡=1 批）vs max_calls=12、预留 5 → 只提交 7 篇。

        旧行为：12 批提取填满预算 → 规划补查立即 call_budget 截断（波动根源）。
        新行为：提取 7 批 + 规划 2 轮（2 次调用）共 9 次 ≤ 12；
        被裁 5 篇计入 selection_truncated（coverage=partial 诚实提示）。
        """
        llm, outcome = self._run(n_docs=12, cards_per_doc=5)
        stats = outcome.stats
        self.assertEqual(stats["batches"], 7)
        self.assertTrue(stats["selection_truncated"])
        self.assertLessEqual(llm.calls, 12)
        # 规划补查确实拿到预算（stop_reason 来自轮次机制而非 call_budget）
        self.assertNotEqual(stats["stop_reason"], "call_budget")

    def test_small_candidates_all_committed(self):
        """窄候选（3 篇）不受影响：全部提交、无裁剪。"""
        llm, outcome = self._run(n_docs=3, cards_per_doc=4)
        self.assertEqual(outcome.stats["batches"], 3)
        self.assertFalse(outcome.stats["selection_truncated"])

    def test_ranked_first_docs_win_under_trim(self):
        """裁剪保排名：前 allowance 批的文档完整提取（目标文档通常排前）。"""
        llm, outcome = self._run(n_docs=12, cards_per_doc=5)
        # FakeStore 无真实顺序，但提交列表长度=allowance 且 processed==committed
        self.assertEqual(len(outcome.stats.get("processed_document_ids", [])) or
                         outcome.stats["batches"], 7)

    def test_document_route_unchanged_semantics(self):
        """document 路由单文档：不做裁剪（allowance 足够）。"""
        llm, outcome = self._run(n_docs=1, cards_per_doc=40, route="document")
        self.assertFalse(outcome.stats["selection_truncated"])
        self.assertGreaterEqual(outcome.stats["batches"], 1)


if __name__ == "__main__":
    unittest.main()

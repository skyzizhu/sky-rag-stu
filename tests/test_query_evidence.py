"""Evidence 统一查询入口（V3.9）+ MCP 文档工具的验证。

- query_evidence()：answer_stream 检索半程抽出的独立函数——Evidence/Answer 两个出口共用；
  返回 QAResult（sources/trace/elapsed/usage）+ evidence_meta（coverage 粗判定）；
- MCP get_document / list_documents：按 document_id 取全文/片段、按权限枚举目录。
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from src.pipeline import query_evidence
from tests._helpers import IngestHarness


class QueryEvidenceTests(IngestHarness):
    """Evidence 统一入口：检索半程搬出 answer_stream 后行为不变。"""

    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()
        from src.pipeline import clear_qa_cache
        clear_qa_cache()
        self.ingest([self.write_note("Xtools 支持字幕、降噪、PDF 解析等功能。" * 5, "xtools.md")])

    def test_returns_sources_trace_and_elapsed(self):
        result, meta = query_evidence("Xtools 怎么去除噪音？", top_k=5,
                                      use_query_understanding=False, use_rerank=False)
        self.assertGreaterEqual(len(result.sources), 1)
        self.assertGreaterEqual(len(result.trace), 1)          # 检索半程节点（融合/召回等）
        self.assertIn("retrieval", result.elapsed)
        self.assertIn("coverage", meta)
        self.assertFalse(result.enumeration)

    def test_empty_filter_scope_is_not_found(self):
        result, meta = query_evidence("随便问点什么", top_k=5,
                                      filters={"domain": "no_such_domain"},
                                      use_query_understanding=False, use_rerank=False)
        self.assertEqual(len(result.retrieved), 0)
        self.assertEqual(meta["coverage"], "not_found")

    def test_coverage_partial_when_quota_cuts_cards(self):
        """枚举模式名额用满（丢弃原因含「上限」）→ coverage=partial。"""
        # 50 节、每节正文 ~500 字（相邻两节 >600 → 各自成卡）：50 张卡 > 名额 40 → 必有截断
        sections = "\n".join(f"### 功能 {i:02d}\n这是功能 {i:02d} 的独立说明，编号 {i}，"
                             f"包含核心能力、适用场景与使用限制。" * 15 for i in range(1, 51))
        big = self.knowledge / "learning" / "general" / "features.md"
        big.write_text(f"# 产品功能\n\n{sections}\n", encoding="utf-8")
        self.ingest([big])
        result, meta = query_evidence("列出该产品的全部功能", top_k=5,
                                      use_query_understanding=False, use_rerank=False)
        self.assertTrue(result.enumeration)
        self.assertEqual(meta["coverage"], "partial")


class McpDocumentToolsTests(IngestHarness):
    """get_document / list_documents：按权限取全文/分页片段/枚举目录。"""

    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()
        # 每节正文 ~300 字：相邻两节合计 >600 → 短小节合并不生效，6 节各自成卡（可分页）
        md = ("# Xtools 功能\n\n" +
              "\n\n".join(f"### 功能 {i:02d}\n功能 {i:02d} 的说明，编号 {i}。" * 12
                          for i in range(1, 7)))
        p = self.knowledge / "reference" / "general" / "doc.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(md, encoding="utf-8")
        # 默认仅 hr 部门可见：无令牌（个人模式）可见；finance 部门被拒
        self.ingest([p], default_groups=["hr"])
        from src.vector_store import get_vector_store
        self.document_id = next(
            d["document_id"] for d in get_vector_store().list_documents()
            if d["source"] == "doc.md")

    def _call(self, fn, *args, **kwargs):
        from src import mcp_server
        with contextlib.redirect_stdout(io.StringIO()):
            return json.loads(getattr(mcp_server, fn)(*args, **kwargs))

    def test_get_document_full_stitches_all_fragments(self):
        payload = self._call("get_document", self.document_id, mode="full")
        self.assertFalse(payload.get("refused", False))
        self.assertEqual(payload["total_cards"], 6)
        for i in range(1, 7):
            self.assertIn(f"功能 {i:02d}", payload["full_text"], i)
        self.assertEqual(payload["full_text"].count("来源文档："), 1)  # 仅首卡保留来源前缀

    def test_get_document_fragments_paginates(self):
        payload = self._call("get_document", self.document_id, page=2, page_size=2,
                             mode="fragments")
        self.assertEqual(payload["page"], 2)
        self.assertEqual(payload["total_cards"], 6)
        self.assertEqual(payload["total_pages"], 3)
        self.assertEqual(len(payload["fragments"]), 2)

    def test_get_document_refuses_unknown_or_denied(self):
        payload = self._call("get_document", "doc_不存在")
        self.assertTrue(payload.get("refused"))
        # 无权限：令牌可见部门不含文档 allowed_groups
        from src import mcp_server
        with mock.patch.object(mcp_server, "_current_user_groups", return_value=["finance"]), \
             contextlib.redirect_stdout(io.StringIO()):
            denied = json.loads(mcp_server.get_document(self.document_id))
        self.assertTrue(denied.get("refused"))

    def test_list_documents_filters_by_permission_and_domain(self):
        payload = self._call("list_documents")
        self.assertEqual(payload["total"], 1)
        payload = self._call("list_documents", domain="reference")
        self.assertEqual(payload["total"], 1)
        payload = self._call("list_documents", domain="learning")
        self.assertEqual(payload["total"], 0)
        from src import mcp_server
        with mock.patch.object(mcp_server, "_current_user_groups", return_value=["finance"]), \
             contextlib.redirect_stdout(io.StringIO()):
            denied = json.loads(mcp_server.list_documents())
        self.assertEqual(denied["total"], 0)


if __name__ == "__main__":
    unittest.main()

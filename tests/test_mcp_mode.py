"""MCP 双模配置（V3.9）：MCP_MODE 决定本服务暴露哪些工具。

- query（默认）= 仅查询模式：只暴露 knowledge_search / knowledge_status——
  检索+筛选（四道检查）由 RAG 完成，拼装回答归调用方 Agent；
- answer = 独立回答模式：暴露 knowledge_qa / knowledge_digest / knowledge_status；
- 非法值回退 query。
"""

from __future__ import annotations

import importlib
import os
import unittest
from unittest import mock


class McpModeTests(unittest.TestCase):
    """按 MCP_MODE 验证工具暴露；每个用例 reload 模块后恢复环境。"""

    def _load(self, mode: str | None):
        import src.mcp_server as ms

        env = {"MCP_MODE": mode} if mode is not None else {}
        with mock.patch.dict(os.environ, env, clear=False):
            if mode is None:
                os.environ.pop("MCP_MODE", None)
            importlib.reload(ms)
        return ms

    def tearDown(self):
        import src.mcp_server as ms

        importlib.reload(ms)  # 恢复默认环境下的模块状态，避免影响其他用例

    def test_default_is_query_mode(self):
        """不设 MCP_MODE：默认 query——search/status + 文档工具，Agent 负责拼装回答。"""
        ms = self._load(None)
        self.assertEqual(ms.MCP_MODE, "query")
        self.assertEqual(sorted(ms.REGISTERED_TOOLS),
                         ["get_document", "knowledge_search", "knowledge_status",
                          "list_documents"])

    def test_answer_mode_registers_qa_tools(self):
        ms = self._load("answer")
        self.assertEqual(ms.MCP_MODE, "answer")
        self.assertEqual(sorted(ms.REGISTERED_TOOLS),
                         ["get_document", "knowledge_digest", "knowledge_qa",
                          "knowledge_status", "list_documents"])

    def test_invalid_mode_falls_back_to_query(self):
        ms = self._load("Nonsense")
        self.assertEqual(ms.MCP_MODE, "query")
        self.assertEqual(sorted(ms.REGISTERED_TOOLS),
                         ["get_document", "knowledge_search", "knowledge_status",
                          "list_documents"])


if __name__ == "__main__":
    unittest.main()

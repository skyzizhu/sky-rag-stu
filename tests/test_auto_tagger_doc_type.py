"""V3.9.1：自动标签同一次调用顺带提取 doc_type 的纯函数测试（mock LLM，不触服务）。"""

import unittest
from unittest import mock


class _FakeLLM:
    def __init__(self, raw=None, error=None):
        self.raw = raw
        self.error = error
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.raw, {}


RAW_FULL = '{"topic": ["项目管理"], "tags": ["认知培训", "交付标准"], "doc_type": "项目说明"}'
RAW_NO_TYPE = '{"topic": ["项目管理"], "tags": ["认知培训"]}'


class AutoTagDocTypeTests(unittest.TestCase):
    def test_doc_type_extracted_and_vocab_in_prompt(self):
        from src.auto_tagger import auto_tag

        llm = _FakeLLM(RAW_FULL)
        out = auto_tag("手册正文内容……", llm=llm, doc_type_vocab=["管理制度", "项目说明", "合同"])
        self.assertEqual(out["doc_type"], "项目说明")
        self.assertEqual(out["topic"], ["项目管理"])
        system = llm.calls[0][0]["content"]
        self.assertIn("管理制度、项目说明、合同", system)  # 软约束词表进入提示词
        self.assertIn("已登记类型", system)

    def test_no_vocab_prompt_has_no_registry_line(self):
        from src.auto_tagger import auto_tag

        llm = _FakeLLM(RAW_FULL)
        out = auto_tag("手册正文内容……", llm=llm)
        self.assertEqual(out["doc_type"], "项目说明")
        self.assertNotIn("已登记类型", llm.calls[0][0]["content"])

    def test_missing_doc_type_returns_empty(self):
        from src.auto_tagger import auto_tag

        out = auto_tag("手册正文内容……", llm=_FakeLLM(RAW_NO_TYPE), doc_type_vocab=["制度"])
        self.assertEqual(out["doc_type"], "")
        self.assertEqual(out["topic"], ["项目管理"])

    def test_llm_failure_returns_silent_empty(self):
        from src.auto_tagger import auto_tag

        out = auto_tag("正文", llm=_FakeLLM(error=RuntimeError("timeout")))
        self.assertEqual(out, {"topic": [], "tags": [], "doc_type": ""})

    def test_long_doc_type_trimmed(self):
        from src.auto_tagger import auto_tag

        raw = '{"topic": [], "tags": [], "doc_type": "' + "类" * 40 + '"}'
        out = auto_tag("正文", llm=_FakeLLM(raw))
        self.assertEqual(len(out["doc_type"]), 24)


if __name__ == "__main__":
    unittest.main()

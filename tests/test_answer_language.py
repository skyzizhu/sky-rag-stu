"""回答语言只跟随提问语言：语言判定规则 + 提示词组装 + 管线接线。

背景：此前只靠系统提示词"跟随用户的语言"让模型自己判断，提示词/资料/历史几乎全是中文时，
英文提问会被带偏答成中文（同一会话已有中文问答时 10 次有 6 次）。现在由代码判定语言，
在用户消息最末尾追加目标语言写的指令。本文件验证判定规则与指令落点；检索链路不受影响。
"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from src import answer_language as al
from src.answer_language import detect_language, resolve_answer_language
from src.prompt import SYSTEM_PROMPT, build_messages
from tests._helpers import IngestHarness

ENGLISH_Q = "List all the features available in the Xtools app."


class DetectLanguageTests(unittest.TestCase):
    def test_english(self):
        self.assertEqual(detect_language(ENGLISH_Q), "en")
        self.assertEqual(detect_language("Explain RAG"), "en")  # 两个词即可判定

    def test_simplified_and_traditional_chinese(self):
        self.assertEqual(detect_language("Xtools中总共有几种功能？"), "zh-CN")
        self.assertEqual(detect_language("Xtools中總共有幾種功能？"), "zh-TW")
        self.assertEqual(detect_language("功能"), "zh-CN")  # 没有简繁区别字 → 默认简体

    def test_chinese_with_english_terms_is_chinese(self):
        self.assertEqual(detect_language("Xtools 的 PDF parsing 怎么用"), "zh-CN")
        self.assertEqual(detect_language("RAG 检索的完整流程是怎样的？"), "zh-CN")

    def test_japanese_and_guard_against_chinese_slang(self):
        self.assertEqual(detect_language("Xtoolsの機能をすべて教えてください。"), "ja")
        self.assertEqual(detect_language("我の笔记有哪些"), "zh-CN")      # 网络用语「の」
        self.assertEqual(detect_language("ユニクロ的退货规则"), "zh-CN")  # 含简体专用字

    def test_korean_and_other_languages(self):
        self.assertEqual(detect_language("Xtools 기능을 알려주세요"), "ko")
        self.assertEqual(detect_language("¿Qué funciones tiene Xtools?"), al.SAME_AS_QUESTION)
        self.assertEqual(detect_language("Какие функции есть?"), al.SAME_AS_QUESTION)

    def test_ambiguous_returns_none(self):
        for q in ("RAG", "OK", "Xtools?", "ＲＡＧ", "123", "？", ""):
            self.assertIsNone(detect_language(q), q)

    def test_character_table_is_consistent(self):
        s, t = al._SIMPLIFIED, al._TRADITIONAL
        self.assertEqual(len(s), len(t))
        self.assertEqual(len(set(s)), len(s))
        self.assertEqual(len(set(t)), len(t))
        self.assertFalse(set(s) & set(t))


class ResolveAnswerLanguageTests(unittest.TestCase):
    def test_question_language_wins_over_history(self):
        """核心场景：会话里全是中文问答，再用英文提问 → 英文。"""
        history = [{"role": "user", "content": "Xtools中总共有几种功能？"},
                   {"role": "assistant", "content": "根据资料，Xtools 提供以下功能……"}]
        self.assertEqual(resolve_answer_language(ENGLISH_Q, history), "en")

    def test_ambiguous_question_uses_latest_user_question(self):
        history = [{"role": "user", "content": "Xtools中总共有几种功能？"},
                   {"role": "assistant", "content": "……"},
                   {"role": "user", "content": "How do I add subtitles?"},
                   {"role": "assistant", "content": "……"},
                   {"role": "user", "content": "OK"},  # 自身判断不出 → 继续往前找
                   {"role": "assistant", "content": "……"}]
        self.assertEqual(resolve_answer_language("RAG", history), "en")

    def test_ambiguous_ignores_assistant_messages(self):
        """历史回答可能本身就答错了语言，不作为依据。"""
        history = [{"role": "assistant", "content": "Based on the knowledge base, ..."}]
        self.assertEqual(resolve_answer_language("RAG", history), al.DEFAULT_LANGUAGE)

    def test_ambiguous_without_history_defaults_to_simplified_chinese(self):
        self.assertEqual(resolve_answer_language("RAG"), "zh-CN")
        self.assertEqual(resolve_answer_language("RAG", None), "zh-CN")


class BuildMessagesTests(unittest.TestCase):
    def test_directive_is_last_line_of_user_message(self):
        messages = build_messages("[1] 资料", ENGLISH_Q)
        self.assertTrue(messages[-1]["content"].endswith("\n\nAnswer in English."))
        self.assertTrue(build_messages("[1] 资料", "Xtools有哪些功能？")[-1]["content"]
                        .endswith("\n\n请用简体中文回答。"))
        self.assertTrue(build_messages("[1] 資料", "Xtools有哪些功能？請列出。")[-1]["content"]
                        .endswith("\n\n請用繁體中文回答。"))

    def test_system_prompt_no_longer_forces_chinese(self):
        self.assertNotIn("用简洁的中文回答", SYSTEM_PROMPT)
        self.assertIn("回答语言按用户消息最后一行的语言要求执行", SYSTEM_PROMPT)

    def test_history_and_enumeration_unchanged(self):
        history = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好！"}]
        messages = build_messages("[1] 资料", ENGLISH_Q, history=history, enumerate_mode=True)
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])
        self.assertIn("【枚举模式】", messages[0]["content"])
        # 历史消息原样传入，不追加语言指令（只有当前问题带指令）
        self.assertEqual(messages[1]["content"], "你好")
        self.assertIn(ENGLISH_Q, messages[-1]["content"])


class PipelineWiringTests(IngestHarness):
    """管线里真正发给模型的消息：末尾是正确的语言指令（检索部分不受影响）。"""

    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()
        from src.pipeline import clear_qa_cache
        clear_qa_cache()
        self.ingest([self.write_note("Xtools 是一款工具，支持字幕、降噪、PDF 解析等功能。" * 5,
                                     "xtools.md")])

    def sent_messages(self, question: str, history=None) -> list[dict]:
        from src.pipeline import answer_question
        fake_llm = mock.Mock()
        fake_llm.chat_stream.return_value = iter(["ok"])
        with mock.patch("src.pipeline.get_llm_client", return_value=fake_llm), \
             contextlib.redirect_stdout(io.StringIO()):
            answer_question(question, top_k=5, use_llm=True, use_rerank=False,
                            use_query_understanding=False, history=history)
        return fake_llm.chat_stream.call_args.args[0]

    def test_english_question_gets_english_directive(self):
        messages = self.sent_messages("What features does Xtools have?")
        self.assertTrue(messages[-1]["content"].endswith("Answer in English."))

    def test_chinese_question_gets_chinese_directive(self):
        messages = self.sent_messages("Xtools 有哪些功能？")
        self.assertTrue(messages[-1]["content"].endswith("请用简体中文回答。"))

    def test_english_question_after_chinese_history(self):
        history = [{"role": "user", "content": "Xtools 有哪些功能？"},
                   {"role": "assistant", "content": "Xtools 支持字幕、降噪等功能。"}]
        messages = self.sent_messages("Can it remove background noise?", history=history)
        self.assertTrue(messages[-1]["content"].endswith("Answer in English."))


if __name__ == "__main__":
    unittest.main()

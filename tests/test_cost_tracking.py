"""模型消耗统计（B16.1）：token 用量采集（含缓存命中/未命中拆分）与费用估算。

- 采集层 src/llm.py：chat / chat_stream 通过 usage_out 回填用量（返回值不变，旧调用点零改动）；
  服务商返回 usage 用真实值（DeepSeek 与 OpenAI 两种形状都认），拿不到则按本地启发式估算并标记 estimated；
- 管线 src/pipeline.py：Query 理解 / 精排 / 生成三段记入 QAResult.usage（随 QA 缓存与会话持久化）；
  评估的用量单独放 evaluation["usage"]（缓存命中会重跑评估，分开计避免重复）；
- 费用 src/answer_evaluator.compute_cost：三档单价（元/百万 tokens）即时计算。
"""

from __future__ import annotations

import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest import mock

from src.answer_evaluator import compute_cost, merge_usage, run_evaluation
from src.app_settings import _normalize_choices
from src.config import get_config
from src.context import estimate_tokens
from src.llm import LLMClient, new_usage, parse_usage
from src.pipeline import answer_question
from tests._helpers import IngestHarness


def make_client_with(fake_create):
    """构造一个不联网的 LLMClient：合法配置 + 替换底层 client。"""
    cfg = get_config()
    client = LLMClient(config=cfg)
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    client.client = fake
    return client


class ParseUsageTests(unittest.TestCase):
    def test_deepseek_style(self):
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                                prompt_cache_hit_tokens=40)
        self.assertEqual(parse_usage(usage), {"input": 100, "input_cached": 40, "output": 20})

    def test_openai_style(self):
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                                prompt_tokens_details=SimpleNamespace(cached_tokens=60))
        self.assertEqual(parse_usage(usage), {"input": 100, "input_cached": 60, "output": 20})

    def test_no_numbers_returns_none(self):
        self.assertIsNone(parse_usage(None))
        self.assertIsNone(parse_usage(SimpleNamespace(prompt_tokens=None, completion_tokens=None)))

    def test_cached_clamped_into_prompt(self):
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_cache_hit_tokens=99)
        self.assertEqual(parse_usage(usage)["input_cached"], 10)


class ChatUsageTests(unittest.TestCase):
    """chat / chat_stream 把用量写入 usage_out；返回值不变。"""

    def test_chat_fills_usage_out(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="你好"))],
            usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30, prompt_cache_hit_tokens=20))
        client = make_client_with(lambda **kwargs: response)
        usage = new_usage()
        answer, _ = client.chat([{"role": "user", "content": "hi"}], usage_out=usage)
        self.assertEqual(answer, "你好")
        self.assertEqual(usage, {"input": 120, "input_cached": 20, "output": 30, "estimated": False})
        self.assertEqual(new_usage(), {"input": 0, "input_cached": 0, "output": 0, "estimated": False})

    def test_chat_without_usage_out_unchanged(self):
        """旧式调用（不带 usage_out）返回值不变。"""
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
        client = make_client_with(lambda **kwargs: response)
        answer, elapsed = client.chat([{"role": "user", "content": "x"}])
        self.assertEqual(answer, "ok")
        self.assertIsInstance(elapsed, float)

    def test_chat_estimates_when_no_usage(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="答案"))],
                                   usage=None)
        client = make_client_with(lambda **kwargs: response)
        usage = new_usage()
        msgs = [{"role": "user", "content": "问题" * 10}]
        client.chat(msgs, usage_out=usage)
        self.assertTrue(usage["estimated"])
        self.assertEqual(usage["input"], estimate_tokens("问题" * 10))
        self.assertEqual(usage["output"], estimate_tokens("答案"))

    def _stream_chunks(self, usage=None, text=("你", "好")):
        chunks = [SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(content=t))])
                  for t in text]
        chunks.append(SimpleNamespace(usage=usage, choices=[]))  # 末尾 usage-only chunk
        return iter(chunks)

    def test_stream_captures_usage_from_final_chunk(self):
        """用量在 choices 为空的末尾 chunk 里：先取 usage 再跳过，不能反。"""
        usage_obj = SimpleNamespace(prompt_tokens=2000, completion_tokens=300,
                                    prompt_tokens_details=SimpleNamespace(cached_tokens=500))
        client = make_client_with(lambda **kwargs: self._stream_chunks(usage_obj))
        usage = new_usage()
        out = "".join(client.chat_stream([{"role": "user", "content": "q"}], usage_out=usage))
        self.assertEqual(out, "你好")
        self.assertEqual(usage, {"input": 2000, "input_cached": 500, "output": 300, "estimated": False})

    def test_stream_requests_include_usage_and_retries_without(self):
        """建流时带 stream_options；服务商报 TypeError（不支持）时去掉重试一次。"""
        calls = []

        def fake_create(**kwargs):
            calls.append(kwargs)
            if "stream_options" in kwargs:
                raise TypeError("unexpected keyword argument 'stream_options'")
            return self._stream_chunks()

        client = make_client_with(fake_create)
        usage = new_usage()
        out = "".join(client.chat_stream([{"role": "user", "content": "q"}], usage_out=usage))
        self.assertEqual(out, "你好")
        self.assertEqual([("stream_options" in k) for k in calls], [True, False])
        self.assertTrue(usage["estimated"])  # 服务商没给 usage → 本地估算

    def test_stream_estimates_when_no_usage(self):
        client = make_client_with(lambda **kwargs: self._stream_chunks(usage=None))
        usage = new_usage()
        msgs = [{"role": "user", "content": "问题" * 5}]
        "".join(client.chat_stream(msgs, usage_out=usage))
        self.assertTrue(usage["estimated"])
        self.assertEqual(usage["input"], estimate_tokens("问题" * 5))
        self.assertEqual(usage["output"], estimate_tokens("你好"))

    def test_stream_empty_falls_back_and_forwards_usage(self):
        """流式零产出回退一次性调用：用量走 chat 通道，不按估算计。"""
        usage_obj = SimpleNamespace(prompt_tokens=70, completion_tokens=9)
        state = {"n": 0}

        def fake_create(**kwargs):
            state["n"] += 1
            if kwargs.get("stream"):
                return iter([SimpleNamespace(usage=None, choices=[])])  # 空流
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="回退答案"))],
                                   usage=usage_obj)

        client = make_client_with(fake_create)
        usage = new_usage()
        out = "".join(client.chat_stream([{"role": "user", "content": "q"}], usage_out=usage))
        self.assertEqual(out, "回退答案")
        self.assertEqual(state["n"], 2)
        self.assertEqual((usage["input"], usage["output"], usage["estimated"]), (70, 9, False))


class CostMathTests(unittest.TestCase):
    def test_compute_cost_three_tiers(self):
        usage = {"input": 1_000_000, "input_cached": 400_000, "output": 500_000}
        # 未命中 60 万 × 2 元 + 命中 40 万 × 0.2 元 + 输出 50 万 × 8 元
        self.assertAlmostEqual(compute_cost(usage, 2.0, 0.2, 8.0), 1.2 + 0.08 + 4.0)

    def test_compute_cost_zero_prices(self):
        usage = {"input": 1000, "input_cached": 500, "output": 200}
        self.assertEqual(compute_cost(usage, 0.0, 0.0, 0.0), 0.0)

    def test_merge_usage(self):
        total = merge_usage([{"input": 100, "input_cached": 40, "output": 10, "estimated": False},
                             {"input": 50, "input_cached": 0, "output": 5, "estimated": True}])
        self.assertEqual(total, {"input": 150, "input_cached": 40, "output": 15,
                                 "estimated": True, "calls": 2})


class SettingsValidationTests(unittest.TestCase):
    def test_price_keys_kept_when_valid(self):
        data = _normalize_choices({"llm_price_input": 2.5, "llm_price_input_cached": 0,
                                   "llm_price_output": 8})
        self.assertEqual(data["llm_price_input"], 2.5)
        self.assertEqual(data["llm_price_input_cached"], 0)
        self.assertEqual(data["llm_price_output"], 8)

    def test_price_keys_dropped_when_invalid(self):
        for bad in (True, "2.0", -1, 100001, [1]):
            data = _normalize_choices({"llm_price_input": bad})
            self.assertNotIn("llm_price_input", data, bad)


class PipelineUsageTests(IngestHarness):
    """管线聚合：分项与总计；缓存命中携带原始用量且不新增调用。"""

    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()
        from src.pipeline import clear_qa_cache
        clear_qa_cache()
        self.ingest([self.write_note("Xtools 支持字幕、降噪、PDF 解析等功能。" * 5, "xtools.md")])

    class FakeLLM:
        """会填 usage_out 的假客户端：chat 与 chat_stream 各记一次调用。"""

        def __init__(self):
            self.chat_calls = 0
            self.stream_calls = 0

        def chat(self, messages, usage_out=None):
            self.chat_calls += 1
            if usage_out is not None:
                usage_out.update({"input": 500, "input_cached": 100, "output": 50, "estimated": False})
            return '[{"id": "C1", "score": 9}]', 0.1  # 可被精排解析的 JSON

        def chat_stream(self, messages, usage_out=None):
            self.stream_calls += 1
            if usage_out is not None:
                usage_out.update({"input": 2000, "input_cached": 0, "output": 300, "estimated": False})
            yield "根据资料，Xtools 支持字幕、降噪等功能。"

    def _ask(self, fake, question, top_k=5):
        from src.pipeline import answer_stream
        with mock.patch("src.pipeline.get_llm_client", return_value=fake), \
             contextlib.redirect_stdout(io.StringIO()):
            result, deltas = answer_stream(question, top_k=top_k, use_llm=True, use_rerank=True,
                                           use_query_understanding=False)
            for _ in deltas:
                pass
        return result

    # 不含列举词的问法：不触发枚举通道（枚举会跳过精排，精排的 chat 调用就没了）
    QUESTION = "Xtools 怎么去除视频里的背景噪音？"

    def test_usage_aggregated_per_stage(self):
        fake = self.FakeLLM()
        result = self._ask(fake, self.QUESTION)
        self.assertEqual(fake.chat_calls, 1)  # 精排
        self.assertEqual(fake.stream_calls, 1)  # 生成
        self.assertEqual(result.usage["stages"]["rerank"]["input"], 500)
        self.assertEqual(result.usage["stages"]["generation"]["input"], 2000)
        self.assertEqual(result.usage["calls"], 2)
        self.assertEqual(result.usage["input"], 2500)
        self.assertEqual(result.usage["input_cached"], 100)
        self.assertEqual(result.usage["output"], 350)
        self.assertFalse(result.usage["estimated"])

    def test_cache_hit_carries_original_usage_without_new_calls(self):
        fake = self.FakeLLM()
        first = self._ask(fake, self.QUESTION)
        self.assertEqual(fake.chat_calls, 1)
        second = self._ask(fake, self.QUESTION)
        self.assertEqual(fake.chat_calls, 1)  # 缓存命中：没有新调用
        self.assertEqual(second.usage, first.usage)

    def test_zero_usage_not_recorded(self):
        """Mock 式假 LLM 不填 usage_out：用量保持全 0 → 不产生分项与总计。"""
        from types import SimpleNamespace as NS
        fake = NS(chat=lambda *a, **k: ("[{}]", 0.0),
                  chat_stream=lambda *a, **k: iter(["答案"]))
        with mock.patch("src.pipeline.get_llm_client", return_value=fake), \
             contextlib.redirect_stdout(io.StringIO()):
            result = answer_question(self.QUESTION, use_llm=True, use_rerank=True,
                                     use_query_understanding=False)
        self.assertNotIn("calls", result.usage)
        self.assertNotIn("stages", result.usage)


class EvaluationUsageTests(unittest.TestCase):
    def test_run_evaluation_payload_carries_usage(self):
        def fake_create(**kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=(
                    '{"faithfulness": {"score": 9, "reason": "引用正确"},'
                    ' "relevance": {"score": 8, "reason": "答所问"},'
                    ' "completeness": {"score": 7, "reason": "覆盖全"}}')))],
                usage=SimpleNamespace(prompt_tokens=800, completion_tokens=60,
                                      prompt_cache_hit_tokens=0))

        # judge_dimensions 内部通过 llm.chat(messages, usage_out=...) 采集用量
        client = make_client_with(fake_create)
        result = SimpleNamespace(question="q", answer="回答", context="资料", trace=[],
                                 retrieved=[], sources=[{"source": "a.md"}])
        payload = run_evaluation(result, llm=client)
        self.assertTrue(payload["ok"], payload.get("error"))
        self.assertEqual(payload["usage"]["input"], 800)
        self.assertEqual(payload["usage"]["output"], 60)


if __name__ == "__main__":
    unittest.main()

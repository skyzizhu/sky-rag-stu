"""节点 9：LLM API 调用。

产品视角：这一步就是「把考卷交给云端大模型批改」——
把「任务说明书（系统提示词）+ 资料附页 + 用户问题」发给大模型，
拿回答案。任何一家 OpenAI 兼容的服务商都能用，在 .env 里配置即可。
"""

from __future__ import annotations

import time
from functools import lru_cache

from openai import APIConnectionError, APIStatusError, AuthenticationError, NotFoundError, OpenAI, RateLimitError

from src.config import AppConfig, get_config


class LLMError(Exception):
    """大模型调用失败时抛出，错误信息直接给人看。"""


class LLMClient:
    def __init__(self, config: AppConfig | None = None):
        self.cfg = config or get_config()
        self.cfg.validate_llm()  # 没配 Key 就直接给出清晰提示，而不是报一堆天书
        try:
            self.client = OpenAI(
                api_key=self.cfg.llm_api_key,
                base_url=self.cfg.llm_base_url,
                timeout=120,
            )
        except Exception as exc:
            raise LLMError(f"初始化大模型客户端失败：{exc}") from exc

    @staticmethod
    def _translate_error(exc: Exception) -> LLMError:
        """把各类 API 异常翻译成「人话 + 怎么修」。"""
        if isinstance(exc, AuthenticationError):
            return LLMError("API Key 无效（401）。请检查 .env 里的 LLM_API_KEY 是否正确、是否过期。")
        if isinstance(exc, APIConnectionError):
            return LLMError("连不上大模型服务。请检查网络，以及 .env 里的 LLM_BASE_URL 是否正确。")
        if isinstance(exc, NotFoundError):
            return LLMError("找不到配置的模型名。请检查 .env 里的 LLM_MODEL 是否是该服务商的模型名。")
        if isinstance(exc, RateLimitError):
            return LLMError("触发服务商限流（余额不足或请求太频繁），请稍后重试或检查账户余额。")
        status = getattr(exc, "status_code", None)
        if status == 402:
            return LLMError("账户余额不足（402）。请充值后重试，或更换 .env 里的 LLM 服务商。")
        if status and 400 <= status < 500:
            return LLMError(f"大模型服务返回客户端错误（{status}）。请检查 .env 里的配置项是否正确。")
        return LLMError(f"大模型调用失败：{type(exc).__name__}: {exc}")

    def _create(self, messages: list[dict], stream: bool = False):
        return self.client.chat.completions.create(
            model=self.cfg.llm_model,
            messages=messages,
            temperature=self.cfg.llm_temperature,
            max_tokens=self.cfg.llm_max_tokens,
            stream=stream,
        )

    def chat(self, messages: list[dict]) -> tuple[str, float]:
        """发送对话，返回 (完整答案文本, 耗时秒)。"""
        start = time.time()
        try:
            response = self._create(messages)
        except Exception as exc:
            raise self._translate_error(exc) from exc

        elapsed = time.time() - start
        answer = (response.choices[0].message.content or "").strip()
        if self.cfg.debug:
            print(f"    LLM 调用完成：{self.cfg.llm_model}，耗时 {elapsed:.1f} 秒")
        return answer, elapsed

    def chat_stream(self, messages: list[dict]):
        """流式发送对话：逐段产出答案文本（界面里实现「逐字出答案」）。

        返回一个生成器；迭代过程中出现的 API 异常同样翻译成人话后抛出。
        个别情况下服务端流式返回为空内容，此时自动回退到一次性调用重试一次。
        """
        start = time.time()
        produced = False
        try:
            stream = self._create(messages, stream=True)
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    produced = True
                    yield delta
            if not produced:  # 流式没有产出任何内容：一次性重试
                answer, _ = self.chat(messages)
                if answer:
                    yield answer
        except Exception as exc:
            raise self._translate_error(exc) from exc
        finally:
            if self.cfg.debug:
                print(f"    LLM 流式调用完成：{self.cfg.llm_model}，耗时 {time.time() - start:.1f} 秒")


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    return LLMClient()

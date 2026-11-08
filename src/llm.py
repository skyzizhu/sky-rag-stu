"""节点 9：LLM API 调用。

产品视角：这一步就是「把考卷交给云端大模型批改」——
把「任务说明书（系统提示词）+ 资料附页 + 用户问题」发给大模型，
拿回答案。任何一家 OpenAI 兼容的服务商都能用，在 .env 里配置即可。
"""

from __future__ import annotations

import re
import time
from functools import lru_cache

from openai import APIConnectionError, APIStatusError, AuthenticationError, NotFoundError, OpenAI, RateLimitError

from src.config import AppConfig, get_config
from src.context import estimate_tokens


class LLMError(Exception):
    """大模型调用失败时抛出，错误信息直接给人看。"""


def new_usage() -> dict:
    """一次调用的用量字典（传入 chat / chat_stream 的 usage_out 初始值）。"""
    return {"input": 0, "input_cached": 0, "output": 0, "estimated": False}


def parse_usage(usage) -> dict | None:
    """从 API 响应的 usage 对象解析 token 用量；取不到数字返回 None。

    兼容两种格式：
    - DeepSeek：prompt_tokens + prompt_cache_hit_tokens / prompt_cache_miss_tokens；
    - OpenAI：prompt_tokens + prompt_tokens_details.cached_tokens（未命中 = prompt − cached）。
    """
    if usage is None:
        return None
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    cached = getattr(usage, "prompt_cache_hit_tokens", None)  # DeepSeek
    if not isinstance(cached, int):
        details = getattr(usage, "prompt_tokens_details", None)  # OpenAI
        cached = getattr(details, "cached_tokens", None) if details is not None else None
        if not isinstance(cached, int):
            cached = 0
    cached = max(0, min(cached, prompt))
    return {"input": prompt, "input_cached": cached, "output": completion}


def _estimate_usage(messages: list[dict], output_text: str) -> dict:
    """服务商没返回 usage 时的兜底：按本地启发式估算（标记 estimated）。"""
    prompt_text = "\n".join(str(m.get("content") or "") for m in messages or [])
    return {"input": estimate_tokens(prompt_text), "input_cached": 0,
            "output": estimate_tokens(output_text), "estimated": True}


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

    @staticmethod
    def _clean_think_tags(text: str) -> str:
        """去除 Ollama 推理模型（qwen3 等）输出的 <think>...</think> 标签。

        注意：只在确实包含 <think> 标签时才做 strip，
        正常文本（含换行符）必须原样通过——否则会丢掉 LLM 的换行格式。
        """
        if not text or "<think>" not in text:
            return text  # 没有 think 标签，原样返回（保留换行）
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        if "<think>" in cleaned and "</think>" not in cleaned:
            cleaned = cleaned.split("<think>")[-1].strip()
        return cleaned or text

    def _create(self, messages: list[dict], stream: bool = False, include_usage: bool = False):
        """调用 OpenAI 兼容接口。include_usage：流式末尾附带 usage 统计 chunk——
        个别兼容服务不支持该参数（报 400 / TypeError），此时去掉重试一次。"""
        kwargs = dict(
            model=self.cfg.llm_model,
            messages=messages,
            temperature=self.cfg.llm_temperature,
            max_tokens=self.cfg.llm_max_tokens,
            stream=stream,
        )
        if include_usage:
            kwargs["stream_options"] = {"include_usage": True}
        try:
            return self.client.chat.completions.create(**kwargs)
        except (APIStatusError, TypeError) as exc:
            if not include_usage:
                raise
            if isinstance(exc, APIStatusError) and getattr(exc, "status_code", None) != 400:
                raise
            kwargs.pop("stream_options", None)
            return self.client.chat.completions.create(**kwargs)

    def chat(self, messages: list[dict], usage_out: dict | None = None) -> tuple[str, float]:
        """发送对话，返回 (完整答案文本, 耗时秒)。

        usage_out：可选的用量字典——调用后写入本次 token 统计（服务商返回 usage 用真实值，
        否则按本地启发式估算并标记 estimated），返回值保持不变、不影响既有调用方。
        """
        start = time.time()
        try:
            response = self._create(messages)
        except Exception as exc:
            raise self._translate_error(exc) from exc

        elapsed = time.time() - start
        answer = self._clean_think_tags(response.choices[0].message.content or "")
        if usage_out is not None:
            parsed = parse_usage(getattr(response, "usage", None))
            usage_out.update(parsed or _estimate_usage(messages, answer))
            usage_out["finish_reason"] = getattr(response.choices[0], "finish_reason", None)
        if self.cfg.debug:
            print(f"    LLM 调用完成：{self.cfg.llm_model}，耗时 {elapsed:.1f} 秒")
        return answer, elapsed

    def chat_stream(self, messages: list[dict], usage_out: dict | None = None):
        """流式发送对话：逐段产出答案文本（界面里实现「逐字出答案」）。

        usage_out：可选的用量字典——流式结束（含回退重试）后写入本次 token 统计。
        返回一个生成器；迭代过程中出现的 API 异常同样翻译成人话后抛出。
        个别情况下服务端流式返回为空内容，此时自动回退到一次性调用重试一次。
        """
        start = time.time()
        produced = False
        acc_text = ""  # 本地估算输出 token 用：生成器内部积累的已产出文本
        captured: dict | None = None
        finish_reason: str | None = None
        try:
            stream = self._create(messages, stream=True, include_usage=usage_out is not None)
            for chunk in stream:
                # 先取 usage 再跳过空 choices：服务商的用量统计就放在末尾那个
                # choices 为空的 chunk 里，跳过顺序反了就永远拿不到
                if getattr(chunk, "usage", None) is not None:
                    parsed = parse_usage(chunk.usage)
                    if parsed:
                        captured = parsed
                if not chunk.choices:
                    continue
                fr = getattr(chunk.choices[0], "finish_reason", None)
                if fr:
                    finish_reason = fr
                delta = chunk.choices[0].delta.content
                if delta:
                    clean = self._clean_think_tags(delta)
                    if clean:
                        produced = True
                        acc_text += clean
                        yield clean
            if not produced:  # 流式没有产出任何内容：一次性重试
                fallback_usage: dict | None = {} if usage_out is not None else None
                answer, _ = self.chat(messages, usage_out=fallback_usage)
                if fallback_usage:
                    captured = fallback_usage
                if answer:
                    acc_text += answer
                    yield answer
        except Exception as exc:
            raise self._translate_error(exc) from exc
        finally:
            if usage_out is not None:
                usage_out.update(captured or _estimate_usage(messages, acc_text))
                usage_out["finish_reason"] = finish_reason
            if self.cfg.debug:
                print(f"    LLM 流式调用完成：{self.cfg.llm_model}，耗时 {time.time() - start:.1f} 秒")


def strip_think_tags(text: str) -> str:
    """模块级思考标签清洗（pipeline 在答案定稿时做全文终清，跨 chunk 边界不漏）。"""
    return LLMClient._clean_think_tags(text or "")


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    return LLMClient()

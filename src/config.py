"""配置中心：把 .env 里的所有配置读进来，供全项目统一使用。

产品视角：这个文件相当于系统的「设置面板」，
所有可调的参数（模型、切片长度、召回条数等）都在 .env 里改，不用动代码。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    value = os.getenv(key, default).strip()
    return value


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    return _env(key, str(default)).lower() in ("1", "true", "yes", "on")


class ConfigError(Exception):
    """配置不完整或非法时抛出，错误信息直接给人看。"""


@dataclass
class AppConfig:
    # LLM（云端大模型）
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL"))
    llm_temperature: float = field(default_factory=lambda: _env_float("LLM_TEMPERATURE", 0.2))
    llm_max_tokens: int = field(default_factory=lambda: _env_int("LLM_MAX_TOKENS", 2000))

    # 本地向量化（Ollama）
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434"))
    embedding_model: str = field(
        default_factory=lambda: _env("EMBEDDING_MODEL", "qwen3-embedding:4b")
    )

    # 向量数据库（Qdrant）
    qdrant_url: str = field(default_factory=lambda: _env("QDRANT_URL", "http://localhost:6333"))
    qdrant_collection: str = field(
        default_factory=lambda: _env("QDRANT_COLLECTION", "personal_knowledge")
    )

    # 知识切片
    chunk_size: int = field(default_factory=lambda: _env_int("CHUNK_SIZE", 600))
    chunk_overlap: int = field(default_factory=lambda: _env_int("CHUNK_OVERLAP", 100))

    # 检索与回答
    top_k: int = field(default_factory=lambda: _env_int("TOP_K", 5))
    context_max_chars: int = field(default_factory=lambda: _env_int("CONTEXT_MAX_CHARS", 6000))

    # V2：Query 理解 / 改写（每次问答多用一次 LLM 调用，把口语化提问改写成检索友好查询）
    query_understanding: bool = field(default_factory=lambda: _env_bool("QUERY_UNDERSTANDING", True))

    # V2：混合检索（向量通道 + BM25 关键词通道，RRF 融合排序）
    hybrid_search: bool = field(default_factory=lambda: _env_bool("HYBRID_SEARCH", True))

    # V2：Rerank 精排（LLM 对候选逐条相关度打分；先宽召回 RERANK_RECALL_K 条，精排后取 Top K）
    rerank_enabled: bool = field(default_factory=lambda: _env_bool("RERANK_ENABLED", True))
    rerank_recall_k: int = field(default_factory=lambda: _env_int("RERANK_RECALL_K", 10))

    # 其他
    knowledge_dir: Path = field(
        default_factory=lambda: (PROJECT_ROOT / _env("KNOWLEDGE_DIR", "knowledge")).resolve()
    )
    default_category: str = field(default_factory=lambda: _env("DEFAULT_CATEGORY", "未分类"))
    debug: bool = field(default_factory=lambda: _env_bool("DEBUG", True))

    def validate_llm(self) -> None:
        """用到云端大模型前检查配置是否齐全，缺什么直接说清楚。"""
        missing = [
            name
            for name, value in (
                ("LLM_API_KEY", self.llm_api_key),
                ("LLM_BASE_URL", self.llm_base_url),
                ("LLM_MODEL", self.llm_model),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "LLM 配置不完整，缺少：" + "、".join(missing)
                + "。请打开项目根目录的 .env 文件，把这几项填好后再试。"
            )


def get_config() -> AppConfig:
    """获取全局配置（每次调用都读取最新 .env，方便改完立即生效）。"""
    return AppConfig()

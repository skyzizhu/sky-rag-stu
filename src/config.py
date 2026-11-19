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


def _local_service_url(key: str, default: str) -> str:
    """本机服务（Ollama/Qdrant）地址：把 loopback 的 localhost 统一改写为 127.0.0.1。

    Windows 把 localhost 优先解析成 IPv6 ::1，而本机服务只监听 IPv4 时，每个新
    TCP 连接要先等 ::1 的延迟拒绝（实测 ~2 秒才返回 RST），表现为 chat 回车后
    答案气泡长时间空白、检索就绪检查卡顿。localhost 与 127.0.0.1 语义等价
    （都是本机回环），统一走 IPv4 直连零等待；显式配置的其他主机名不受影响。
    """
    value = _env(key, default)
    if value.startswith("http://localhost"):
        value = "http://127.0.0.1" + value[len("http://localhost"):]
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
    # 单次回答的输出 token 上限（含思考模型的内部思考；枚举模式答案长，2000 容易被截断）
    llm_max_tokens: int = field(default_factory=lambda: _env_int("LLM_MAX_TOKENS", 8192))

    # 本地向量化（Ollama）。地址经 _local_service_url 规范化：localhost → 127.0.0.1，
    # 避开 Windows localhost IPv6 解析的 ~2 秒连接延迟（详见函数注释）。
    ollama_url: str = field(default_factory=lambda: _local_service_url(
        "OLLAMA_URL", "http://127.0.0.1:11434"))
    embedding_model: str = field(
        default_factory=lambda: _env("EMBEDDING_MODEL", "qwen3-embedding:4b")
    )

    # 向量数据库（Qdrant）。同上，localhost → 127.0.0.1。
    qdrant_url: str = field(default_factory=lambda: _local_service_url(
        "QDRANT_URL", "http://127.0.0.1:6333"))
    qdrant_collection: str = field(
        default_factory=lambda: _env("QDRANT_COLLECTION", "personal_knowledge")
    )

    # 知识切片
    # V3.9：切片单位从字符换到估算 token（中文≈0.8/字、英文≈4字符/token）——
    # 中英文卡片密度一致；640 token 中文卡 ≈ 800 字（略大于旧 600 字符卡）
    chunk_size: int = field(default_factory=lambda: _env_int("CHUNK_SIZE", 640))
    chunk_overlap: int = field(default_factory=lambda: _env_int("CHUNK_OVERLAP", 80))

    # 检索与回答
    top_k: int = field(default_factory=lambda: _env_int("TOP_K", 5))
    # 资料附页的 token 上限（估算值：中文 ≈ 0.8 token/字，英文 ≈ 1/3.5 token/字符）
    context_max_tokens: int = field(default_factory=lambda: _env_int("CONTEXT_MAX_TOKENS", 4000))

    # V2：Query 理解 / 改写（每次问答多用一次 LLM 调用，把口语化提问改写成检索友好查询）
    query_understanding: bool = field(default_factory=lambda: _env_bool("QUERY_UNDERSTANDING", True))

    # V2：混合检索（向量通道 + BM25 关键词通道，RRF 融合排序）
    hybrid_search: bool = field(default_factory=lambda: _env_bool("HYBRID_SEARCH", True))

    # V2：Rerank 精排（先宽召回 RERANK_RECALL_K 条，精排后取 Top K）
    rerank_enabled: bool = field(default_factory=lambda: _env_bool("RERANK_ENABLED", True))
    rerank_recall_k: int = field(default_factory=lambda: _env_int("RERANK_RECALL_K", 10))
    # 精排后端：local_onnx（本地 bge-reranker-v2-m3 ONNX，CPU 离线）| llm（云端 LLM 逐条打分）
    rerank_backend: str = field(default_factory=lambda: _env("RERANK_BACKEND", "llm"))
    # 0 使用 ONNX Runtime 默认线程策略；正整数限制本地精排的计算线程。
    rerank_threads: int = field(default_factory=lambda: _env_int("RERANK_THREADS", 0))
    rerank_model_dir: Path = field(
        default_factory=lambda: (PROJECT_ROOT / _env("RERANK_MODEL_DIR",
                                                     "models/bge-reranker-v2-m3-onnx")).resolve()
    )

    # 列举第一阶段：文档精排/展开与目录扫描预算；不与普通问答 Top K 绑定。
    enum_rerank_docs: int = field(default_factory=lambda: max(1, _env_int("ENUM_RERANK_DOCS", 24)))
    enum_expand_docs: int = field(default_factory=lambda: max(1, _env_int("ENUM_EXPAND_DOCS", 12)))
    enum_doc_cards: int = field(default_factory=lambda: max(1, _env_int("ENUM_DOC_CARDS", 40)))
    enum_catalog_docs: int = field(default_factory=lambda: max(1, _env_int("ENUM_CATALOG_DOCS", 200)))
    enum_catalog_points: int = field(default_factory=lambda: max(1, _env_int("ENUM_CATALOG_POINTS", 50000)))

    # 列举第二阶段：预算只针对分批提取与补查，不包含首轮检索。
    enum_stage2_enabled: bool = field(default_factory=lambda: _env_bool("ENUM_STAGE2_ENABLED", True))
    # 提取后语义复审：剔除仅字面命中关键词、并未真正回答问题的记录（失败保守放行）
    enum_verify_enabled: bool = field(default_factory=lambda: _env_bool("ENUM_VERIFY_ENABLED", True))
    enum_max_rounds: int = field(default_factory=lambda: max(0, _env_int("ENUM_MAX_ROUNDS", 2)))
    enum_queries_per_round: int = field(default_factory=lambda: max(1, _env_int("ENUM_QUERIES_PER_ROUND", 2)))
    enum_no_new_rounds: int = field(default_factory=lambda: max(1, _env_int("ENUM_NO_NEW_ROUNDS", 2)))
    enum_max_calls: int = field(default_factory=lambda: max(1, _env_int("ENUM_MAX_CALLS", 12)))
    enum_max_seconds: float = field(default_factory=lambda: max(1.0, _env_float("ENUM_MAX_SECONDS", 90)))
    enum_max_tokens: int = field(default_factory=lambda: max(256, _env_int("ENUM_MAX_TOKENS", 32000)))
    enum_batch_tokens: int = field(default_factory=lambda: max(256, _env_int("ENUM_BATCH_TOKENS", 3000)))
    enum_page_cards: int = field(default_factory=lambda: max(1, _env_int("ENUM_PAGE_CARDS", 8)))
    enum_max_cards: int = field(default_factory=lambda: max(1, _env_int("ENUM_MAX_CARDS", 240)))
    enum_max_documents: int = field(default_factory=lambda: max(1, _env_int("ENUM_MAX_DOCUMENTS", 24)))
    enum_max_items: int = field(default_factory=lambda: max(1, _env_int("ENUM_MAX_ITEMS", 120)))
    enum_output_tokens: int = field(default_factory=lambda: max(128, _env_int("ENUM_OUTPUT_TOKENS", 2048)))

    # 单会话历史预算，独立于检索资料上下文；摘要每达到一批新增旧消息才压缩一次。
    chat_context_max_tokens: int = field(default_factory=lambda: max(512, _env_int("CHAT_CONTEXT_MAX_TOKENS", 6000)))
    chat_recent_turns: int = field(default_factory=lambda: max(1, _env_int("CHAT_RECENT_TURNS", 4)))
    chat_summary_tokens: int = field(default_factory=lambda: max(128, _env_int("CHAT_SUMMARY_TOKENS", 1200)))
    chat_summary_trigger_tokens: int = field(default_factory=lambda: max(256, _env_int("CHAT_SUMMARY_TRIGGER_TOKENS", 4000)))
    chat_recall_messages: int = field(default_factory=lambda: max(0, _env_int("CHAT_RECALL_MESSAGES", 2)))

    # V2.6：上下文组装的来源多样性（同一文档最多进入回答的卡片数）
    context_max_per_doc: int = field(default_factory=lambda: _env_int("CONTEXT_MAX_PER_DOC", 3))

    # V3.3：入库时 LLM 自动补全 topic/tags（每个新入库/有变化的文件多用一次 LLM 调用）
    auto_tag: bool = field(default_factory=lambda: _env_bool("AUTO_TAG", True))

    # OCR（图片文件 / 扫描版 PDF / Word 与 PDF 内嵌图）：关闭后跳过所有 OCR 环节，
    # 普通文字文档的入库不受任何影响
    ocr_enabled: bool = field(default_factory=lambda: _env_bool("OCR_ENABLED", True))

    # 其他
    knowledge_dir: Path = field(
        default_factory=lambda: (PROJECT_ROOT / _env("KNOWLEDGE_DIR", "knowledge")).resolve()
    )
    default_category: str = field(default_factory=lambda: _env("DEFAULT_CATEGORY", "未分类"))
    debug: bool = field(default_factory=lambda: _env_bool("DEBUG", True))

    @property
    def llm_is_local(self) -> bool:
        """当前大模型是否为本地端点（Ollama 等）。

        本地模型不产生 API 费用——模型消耗统计只针对云上 LLM（B16.2）。
        判定口径与 validate_llm 保持一致。
        """
        return "localhost" in self.llm_base_url or "127.0.0.1" in self.llm_base_url

    def validate_llm(self) -> None:
        """用到云端大模型前检查配置是否齐全，缺什么直接说清楚。"""
        is_local = self.llm_is_local
        missing = [
            name
            for name, value in (
                ("LLM_BASE_URL", self.llm_base_url),
                ("LLM_MODEL", self.llm_model),
            )
            if not value
        ]
        # 本地 Ollama 不需要 API Key（自动填占位符）
        if not is_local and not self.llm_api_key:
            missing.append("LLM_API_KEY")
        if missing:
            hint = "（本地 Ollama 无需 API Key）" if is_local else ""
            raise ConfigError(
                "LLM 配置不完整，缺少：" + "、".join(missing) + f"{hint}。"
                "请打开项目根目录的 .env 文件，把这几项填好后再试。"
            )
        if is_local and not self.llm_api_key:
            self.llm_api_key = "ollama-local"  # Ollama 不校验 Key，但 OpenAI 客户端需要一个


def get_config() -> AppConfig:
    """获取全局配置（每次调用都读取最新 .env，方便改完立即生效）。"""
    return AppConfig()

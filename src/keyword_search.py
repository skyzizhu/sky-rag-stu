"""V2.3：BM25 关键词检索（全文检索通道）—— 带索引缓存。

产品视角：向量检索擅长「意思相近」，但它有一个天然盲区——
专有名词、编号、缩写这类「必须一字不差」的词，
指纹上未必突出。BM25 关键词检索正好互补。

性能优化：BM25 索引在首次查询时构建后缓存，
后续查询直接复用；知识库发生变化（入库）时自动失效重建。
"""

from __future__ import annotations

import jieba
from rank_bm25 import BM25Okapi

from src import index_epoch
from src.auth_db import retrieval_acl_revision
from src.config import AppConfig
from src.vector_store import VectorStore, VectorStoreError, get_vector_store

_MAX_CARDS = 5000  # BM25 内存索引的卡片上限（个人知识库远小于此）
_MAX_CACHE_ENTRIES = 32


def tokenize(text: str) -> list[str]:
    """中文 + 英文混合分词：jieba 切词，英文/编号转小写。"""
    return [t.lower() for t in jieba.lcut(text or "") if t.strip()]


# ---------- BM25 索引缓存 ----------
# 缓存结构：{cache_key: {"bm25": BM25Okapi, "cards": list, "epoch": str}}
# cache_key 由过滤条件生成；库变更时通过 invalidate_cache() 全部清空
_bm25_cache: dict[tuple[VectorStore, str], dict] = {}
_cache_collection_count = -1  # 上次构建时的集合卡片数，用于检测库变化


def invalidate_cache() -> None:
    """入库/删除后调用，清空全部 BM25 缓存。"""
    global _bm25_cache, _cache_collection_count
    _bm25_cache = {}
    _cache_collection_count = -1


def _get_cached_index(store: VectorStore, filters: dict | None) -> tuple[BM25Okapi, list[dict], bool] | None:
    """获取缓存的 BM25 索引；未命中或库已变化时返回 None。

    库变化的判定 = 卡片总数变了 或 构建后索引纪元变了。只看卡片数不够：改元数据（可见部门、
    分类、领域）卡片数不变；而其他进程（前台 ↔ MCP）的写操作只能通过纪元文件感知。
    """
    global _cache_collection_count
    try:
        current_count = store.count()
    except Exception:
        return None
    if current_count != _cache_collection_count:
        invalidate_cache()
        _cache_collection_count = current_count
        return None
    revision = retrieval_acl_revision()
    if revision is None:
        return None
    key = (store, str(sorted((filters or {}).items())))
    entry = _bm25_cache.get(key)
    if (entry and entry["epoch"] == (index_epoch.current(), revision)
            and revision == retrieval_acl_revision()):
        return entry["bm25"], entry["cards"], entry["truncated"]
    return None


def _index_text(payload: dict) -> str:
    """BM25 索引文本 = 卡片正文 + 来源文件名 + 标题 + 归属部门。

    文件名本身是高频查询入口（如「帮我找心得那篇」），只索引正文会让
    按文件名找文件永远失败——文件名属于必须可检索的档案信息。
    归属部门同理：正文没提部门名时，按部门名（如「系统工程事业部」）找
    该部门的资料也要能命中。
    """
    parts = [payload.get("text", ""), payload.get("source", ""), payload.get("title", ""),
             payload.get("department", "")]
    return " ".join(str(p) for p in parts if p)


def _build_and_cache(store: VectorStore, filters: dict | None) -> tuple[BM25Okapi, list[dict], bool] | None:
    """构建 BM25 索引并写入缓存。"""
    global _cache_collection_count
    epoch = (index_epoch.current(), retrieval_acl_revision())
    cards = store.get_all_cards(filters=filters, limit=_MAX_CARDS + 1)
    if not cards:
        return None
    truncated = len(cards) > _MAX_CARDS
    cards = cards[:_MAX_CARDS]
    corpus = [tokenize(_index_text(card["payload"])) for card in cards]
    bm25 = BM25Okapi(corpus)
    key = (store, str(sorted((filters or {}).items())))
    if epoch[1] is not None and epoch == (index_epoch.current(), retrieval_acl_revision()):
        for oldest in list(_bm25_cache)[:max(0, len(_bm25_cache) - _MAX_CACHE_ENTRIES + 1)]:
            _bm25_cache.pop(oldest, None)
        _bm25_cache[key] = {"bm25": bm25, "cards": cards, "epoch": epoch, "truncated": truncated}
    try:
        _cache_collection_count = store.count()
    except Exception:
        pass
    return bm25, cards, truncated


def bm25_search(
    query: str,
    top_k: int,
    store: VectorStore | None = None,
    filters: dict | None = None,
    config: AppConfig | None = None,
    diagnostics: dict | None = None,
) -> list[dict]:
    """对知识卡片做 BM25 关键词检索（带缓存，首次构建后复用）。"""
    store = store or get_vector_store()
    if not store.collection_exists():
        raise VectorStoreError(
            "知识库还是空的，请先入库（运行 python ingest.py 或在界面里上传文档）。"
        )

    cached = _get_cached_index(store, filters)
    if diagnostics is not None:
        diagnostics["cache_hit"] = cached is not None
    if cached is None:
        cached = _build_and_cache(store, filters)
    if cached is None:
        return []
    bm25, cards, truncated = cached
    if diagnostics is not None:
        diagnostics["truncated"] = truncated
    tokens = tokenize(query)
    terms = set(tokens)
    scores = bm25.get_scores(tokens)
    ranked = sorted(
        ((card, score) for card, score, counts in zip(cards, scores, bm25.doc_freqs)
         if not terms.isdisjoint(counts)),
        key=lambda pair: -pair[1],
    )
    return [
        {"score": float(score), "payload": card["payload"]}
        for card, score in ranked[:top_k]
    ]

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

from src.config import AppConfig, get_config
from src.vector_store import VectorStore, VectorStoreError, get_vector_store

_MAX_CARDS = 5000  # BM25 内存索引的卡片上限（个人知识库远小于此）


def tokenize(text: str) -> list[str]:
    """中文 + 英文混合分词：jieba 切词，英文/编号转小写。"""
    return [t.lower() for t in jieba.lcut(text or "") if t.strip()]


# ---------- BM25 索引缓存 ----------
# 缓存结构：{cache_key: {"bm25": BM25Okapi, "cards": list, "filter_hash": str}}
# cache_key 由过滤条件生成；库变更时通过 invalidate_cache() 全部清空
_bm25_cache: dict[str, dict] = {}
_cache_collection_count = -1  # 上次构建时的集合卡片数，用于检测库变化


def invalidate_cache() -> None:
    """入库/删除后调用，清空全部 BM25 缓存。"""
    global _bm25_cache, _cache_collection_count
    _bm25_cache = {}
    _cache_collection_count = -1


def _get_cached_index(store: VectorStore, filters: dict | None) -> tuple[BM25Okapi, list[dict]] | None:
    """获取缓存的 BM25 索引；未命中或库已变化时返回 None。"""
    global _cache_collection_count
    try:
        current_count = store.count()
    except Exception:
        return None
    if current_count != _cache_collection_count:
        invalidate_cache()
        _cache_collection_count = current_count
        return None
    key = str(sorted((filters or {}).items(), key=lambda kv: kv[0]))
    entry = _bm25_cache.get(key)
    if entry:
        return entry["bm25"], entry["cards"]
    return None


def _build_and_cache(store: VectorStore, filters: dict | None, cfg: AppConfig) -> tuple[BM25Okapi, list[dict]] | None:
    """构建 BM25 索引并写入缓存。"""
    global _cache_collection_count
    cards = store.get_all_cards(filters=filters, limit=_MAX_CARDS)
    if not cards:
        return None
    corpus = [tokenize(card["payload"].get("text", "")) for card in cards]
    bm25 = BM25Okapi(corpus)
    key = str(sorted((filters or {}).items(), key=lambda kv: kv[0]))
    _bm25_cache[key] = {"bm25": bm25, "cards": cards}
    try:
        _cache_collection_count = store.count()
    except Exception:
        pass
    return bm25, cards


def bm25_search(
    query: str,
    top_k: int,
    store: VectorStore | None = None,
    filters: dict | None = None,
    config: AppConfig | None = None,
) -> list[dict]:
    """对知识卡片做 BM25 关键词检索（带缓存，首次构建后复用）。"""
    cfg = config or get_config()
    store = store or get_vector_store()
    if not store.collection_exists():
        raise VectorStoreError(
            "知识库还是空的，请先入库（运行 python ingest.py 或在界面里上传文档）。"
        )

    cached = _get_cached_index(store, filters)
    if cached is None:
        cached = _build_and_cache(store, filters, cfg)
    if cached is None:
        return []
    bm25, cards = cached

    scores = bm25.get_scores(tokenize(query))
    ranked = sorted(zip(cards, scores), key=lambda pair: -pair[1])
    return [
        {"score": float(score), "payload": card["payload"]}
        for card, score in ranked[:top_k]
        if score > 0
    ]

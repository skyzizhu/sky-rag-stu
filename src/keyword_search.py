"""V2.3：BM25 关键词检索（全文检索通道）—— 带索引缓存。

产品视角：向量检索擅长「意思相近」，但它有一个天然盲区——
专有名词、编号、缩写这类「必须一字不差」的词，
指纹上未必突出。BM25 关键词检索正好互补。

性能优化：BM25 索引在首次查询时构建后缓存，
后续查询直接复用；知识库发生变化（入库）时自动失效重建。
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from heapq import nlargest
from threading import Lock

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
@dataclass
class _ScopedIndex:
    bm25: BM25Okapi | None
    cards: list[dict]
    postings: dict[str, list[int]]
    truncated: bool


_bm25_cache: OrderedDict[tuple, _ScopedIndex] = OrderedDict()
_cache_lock = Lock()


def invalidate_cache() -> None:
    """入库/删除后调用，清空全部 BM25 缓存。"""
    with _cache_lock:
        _bm25_cache.clear()


def _get_cached_index(key: tuple) -> _ScopedIndex | None:
    with _cache_lock:
        entry = _bm25_cache.get(key)
        if entry is not None:
            _bm25_cache.move_to_end(key)
        return entry


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


def _build_index(store: VectorStore, filters: dict | None) -> _ScopedIndex:
    cards = store.get_all_cards(filters=filters, limit=_MAX_CARDS + 1)
    truncated = len(cards) > _MAX_CARDS
    cards = cards[:_MAX_CARDS]
    corpus = [tokenize(_index_text(card["payload"])) for card in cards]
    if not any(corpus):
        return _ScopedIndex(None, cards, {}, truncated)
    bm25 = BM25Okapi(corpus)
    postings = defaultdict(list)
    for index, terms in enumerate(bm25.doc_freqs):
        for term in terms:
            postings[term].append(index)
    return _ScopedIndex(bm25, cards, postings, truncated)


def _rank_matches(index: _ScopedIndex, tokens: list[str], top_k: int) -> list[dict]:
    candidates = {position for term in set(tokens) for position in index.postings.get(term, ())}
    if not candidates or index.bm25 is None:
        return []
    positions = list(candidates)
    scores = index.bm25.get_batch_scores(tokens, positions)
    ranked = nlargest(top_k, zip(positions, scores), key=lambda pair: (pair[1], -pair[0]))
    return [{"score": float(score), "payload": index.cards[position]["payload"]}
            for position, score in ranked]


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
    epoch = (index_epoch.current(), retrieval_acl_revision())
    scoped = "__viewer__" in (filters or {})
    if scoped and epoch[1] is None:
        return []
    count_available = True
    try:
        count = store.count_existing()
    except Exception:
        # The exact count is a cache safeguard. An otherwise readable index can
        # still answer from a fresh scoped scan without caching the result.
        count_available, count = False, None
    if count_available and count is None:
        raise VectorStoreError(
            "知识库还是空的，请先入库（运行 python ingest.py 或在界面里上传文档）。"
        )

    # The scope includes the caller's identity. The count also detects out-of-band
    # additions or removals that do not bump the application epoch.
    key = (store, str(sorted((filters or {}).items())), epoch, count)
    cached = _get_cached_index(key) if count_available else None
    if diagnostics is not None:
        diagnostics["cache_hit"] = cached is not None
    index = cached if cached is not None else _build_index(store, filters)
    if diagnostics is not None:
        diagnostics["truncated"] = index.truncated
    results = _rank_matches(index, tokenize(query), top_k)
    if epoch != (index_epoch.current(), retrieval_acl_revision()):
        return []
    if cached is None and count_available and epoch[1] is not None:
        with _cache_lock:
            _bm25_cache[key] = index
            _bm25_cache.move_to_end(key)
            while len(_bm25_cache) > _MAX_CACHE_ENTRIES:
                _bm25_cache.popitem(last=False)
    return results

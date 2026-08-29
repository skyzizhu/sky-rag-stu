"""V2.3：BM25 关键词检索（全文检索通道）。

产品视角：向量检索擅长「意思相近」，但它有一个天然盲区——
专有名词、编号、缩写这类「必须一字不差」的词（如 YYYYMMDD、ReAct），
指纹上未必突出。BM25 关键词检索正好互补：它按「词有没有出现、出现多频繁、
这个词是不是稀有词」来打分，是经典的关键词排序算法。

实现说明：
- 知识卡片量级为个人规模（几百~几千张），这里在查询时直接从向量库
  拉取卡片现建 BM25 索引，保证结果与库内数据实时一致；
- 中文分词用 jieba；英文/编号按词切分。
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


def bm25_search(
    query: str,
    top_k: int,
    store: VectorStore | None = None,
    filters: dict | None = None,
    config: AppConfig | None = None,
) -> list[dict]:
    """对知识卡片做 BM25 关键词检索。

    返回与 VectorStore.search 相同结构的结果：[{"score", "payload"}, ...]，
    score 为 BM25 原始分（只用于排序，与向量相似度分数不可直接比较）。
    过滤条件与向量通道完全一致（两路查的是同一座档案馆）。
    """
    cfg = config or get_config()
    store = store or get_vector_store()
    if not store.collection_exists():
        raise VectorStoreError(
            "知识库还是空的，请先入库（运行 python ingest.py 或在界面里上传文档）。"
        )

    cards = store.get_all_cards(filters=filters, limit=_MAX_CARDS)
    if not cards:
        return []

    corpus = [tokenize(card["payload"].get("text", "")) for card in cards]
    bm25 = BM25Okapi(corpus)
    scores = bm25.get_scores(tokenize(query))

    ranked = sorted(zip(cards, scores), key=lambda pair: -pair[1])
    results = [
        {"score": float(score), "payload": card["payload"]}
        for card, score in ranked[:top_k]
        if score > 0  # 一个关键词都匹配不上的卡片没有检索价值
    ]
    return results

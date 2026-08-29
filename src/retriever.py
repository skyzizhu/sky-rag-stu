"""节点 6：向量检索（Retriever）。

产品视角：这一步就是「查资料」——
把用户的问题也算成指纹，去档案馆里找出最相近的 N 张知识卡片，
并把每张卡片的相关度分数、原文、档案一起交出去。

Knowledge Management V1 新增：
- 默认只检索 status=active 的知识（归档内容不参与回答）；
- 支持手动 Metadata Filter（domain / category / topic / tags / status / source 等），
  值可以是字符串或字符串列表（任一匹配）。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from functools import lru_cache

from src.config import AppConfig, get_config
from src.embedding import EmbeddingClient, get_embedding_client
from src.vector_store import VectorStore, get_vector_store


@dataclass
class RetrievedItem:
    """一条召回结果：相关度分数 + 卡片原文 + 档案。"""

    rank: int
    score: float
    text: str
    metadata: dict


def effective_filters(filters: dict | None) -> dict:
    """合并默认过滤规则与用户过滤器。

    - 默认强制 status=active（归档内容不参与普通检索）；
    - 用户显式传入 status 时以用户为准；
    - 传 {"status": "all"} 或 None 值表示不加该条件。
    """
    eff: dict = {"status": "active"}
    for key, value in (filters or {}).items():
        if value is None or value == "all":
            eff.pop(key, None)
        else:
            eff[key] = value
    return eff


class Retriever:
    def __init__(
        self,
        embedding_client: EmbeddingClient | None = None,
        store: VectorStore | None = None,
        config: AppConfig | None = None,
    ):
        self.cfg = config or get_config()
        self.embedding = embedding_client or get_embedding_client()
        self.store = store or get_vector_store()

    def retrieve(self, query: str, top_k: int | None = None, filters: dict | None = None) -> list[RetrievedItem]:
        """检索：问题 → 问题向量 → 默认过滤 + Metadata 过滤 → 最相近的 top_k 张卡片。"""
        top_k = top_k or self.cfg.top_k
        used_filters = effective_filters(filters)
        start = time.time()
        query_vector = self.embedding.embed_query(query)
        hits = self.store.search(query_vector, top_k=top_k, filters=used_filters)
        if self.cfg.debug:
            print(f"    检索耗时 {time.time() - start:.2f} 秒，召回 {len(hits)} 条，过滤条件：{used_filters}")
        return [
            RetrievedItem(rank=i + 1, score=hit["score"], text=hit["payload"].get("text", ""),
                          metadata={k: v for k, v in hit["payload"].items() if k != "text"})
            for i, hit in enumerate(hits)
        ]


def format_results(items: list[RetrievedItem]) -> str:
    """把召回结果排成方便人看的文本（终端调试用）。"""
    lines: list[str] = []
    for item in items:
        meta = item.metadata
        source_line = f"来源: {meta.get('source', '?')}"
        if meta.get("section"):
            source_line += f" | 章节: {meta['section']}"
        if meta.get("page"):
            source_line += f" | 页码: {meta['page']}"
        source_line += f" | domain: {meta.get('domain')} | status: {meta.get('status')}"
        preview = item.text[:100].replace("\n", " ")
        lines.append(f"Top {item.rank}\nscore: {item.score:.4f}\n{source_line}\ntext: {preview}……")
    return "\n\n".join(lines)


@lru_cache(maxsize=1)
def get_retriever() -> Retriever:
    return Retriever()


# ---------------- 直接运行本文件：体验检索，人工判断召回是否合理 ----------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="检索节点调试")
    parser.add_argument("query", nargs="+", help="你的问题")
    parser.add_argument("--domain", default=None, help="按 domain 过滤，如 learning")
    parser.add_argument("--status", default=None, help="按 status 过滤，如 archive")
    parser.add_argument("--top-k", type=int, default=None)
    args = parser.parse_args()

    filters = {}
    if args.domain:
        filters["domain"] = args.domain
    if args.status:
        filters["status"] = args.status

    question = " ".join(args.query)
    print(f"问题：{question}")
    print(f"过滤条件：{effective_filters(filters) if filters else '默认（status=active）'}\n")
    results = get_retriever().retrieve(question, top_k=args.top_k, filters=filters or None)
    print(format_results(results))
    sys.exit(0)

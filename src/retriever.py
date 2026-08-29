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
from src.keyword_search import bm25_search
from src.trace import make_node, now_str
from src.vector_store import VectorStore, get_vector_store


def _fmt_filters(filters: dict) -> str:
    parts = []
    for key, value in filters.items():
        if isinstance(value, dict) and ("from" in value or "to" in value):
            parts.append(f"{key}: {value.get('from') or '…'} ~ {value.get('to') or '…'}（时间范围）")
        else:
            parts.append(f"{key}: {value}")
    return "；".join(parts) or "（无）"


@dataclass
class RetrievedItem:
    """一条召回结果：相关度分数 + 卡片原文 + 档案 + 命中通道。"""

    rank: int
    score: float
    text: str
    metadata: dict
    channels: str = "向量"  # 命中通道：向量 / 关键词 / 向量 + 关键词（混合检索时）


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

    def retrieve(self, query: str, top_k: int | None = None,
                 filters: dict | None = None, trace: list | None = None,
                 keyword_query: list[str] | None = None,
                 use_hybrid: bool = False) -> list[RetrievedItem]:
        """检索：问题 → 问题向量 → 默认过滤 + Metadata 过滤 → 最相近的 top_k 张卡片。

        use_hybrid=True（V2.4 混合检索）：并行执行 BM25 关键词检索通道，
        用 RRF 融合两路排名。keyword_query 是 Query 理解产出的关键词列表。
        trace 传入列表时，节点时间线（向量化 / 过滤 / 检索 / 召回）会逐节点写入。
        """
        top_k = top_k or self.cfg.top_k
        used_filters = effective_filters(filters)

        # —— 节点：Query Embedding ——
        embed_start = now_str()
        t0 = time.time()
        query_vector = self.embedding.embed_query(query)
        embed_elapsed = time.time() - t0
        if trace is not None:
            trace.append(make_node(
                "🔢", "Query Embedding", time_str=embed_start, elapsed=embed_elapsed,
                summary="把用户问题转成向量（指纹）；文档和问题必须用同一个模型，指纹相近 = 意思相近",
                items=[
                    ("输入（被向量化的文本）", query),
                    ("输出", f"{len(query_vector)} 维向量"),
                    ("模型", f"{self.cfg.embedding_model}（本机 Ollama）"),
                ],
            ))

        # —— 节点：Metadata Filter ——
        if trace is not None:
            trace.append(make_node(
                "🏷", "Metadata Filter", time_str=now_str(),
                summary="检索前先按知识档案圈定范围，再在范围内找相似",
                items=[
                    ("Metadata 是什么", "每张知识卡片附带的档案信息：来源文件、路径、领域、分类、主题、"
                     "标签、章节、页码、版本、状态（active/archive）等"),
                    ("Metadata Filter 是什么", "检索前按档案字段过滤，缩小检索范围——"
                     "本阶段默认强制 status=active，所以归档（archive）知识不参与回答"),
                    ("本次使用的过滤条件", _fmt_filters(used_filters)),
                ],
            ))

        # —— 节点：数据库检索（向量通道）——
        search_start = now_str()
        t1 = time.time()
        hits = self.store.search(query_vector, top_k=top_k, filters=used_filters)
        search_elapsed = time.time() - t1
        if trace is not None:
            trace.append(make_node(
                "🗄", "数据库检索（向量通道）", time_str=search_start, elapsed=search_elapsed,
                summary=f"拿问题指纹到 Qdrant（{self.cfg.qdrant_url}）里做相似度检索",
                items=[
                    ("输入", f"{len(query_vector)} 维问题向量 + 过滤条件 {used_filters}"),
                    ("输出", f"{len(hits)} 条召回结果（每条带相似度分数）"),
                ],
            ))

        items: list[RetrievedItem] = [
            RetrievedItem(rank=i + 1, score=hit["score"], text=hit["payload"].get("text", ""),
                          metadata={k: v for k, v in hit["payload"].items() if k != "text"})
            for i, hit in enumerate(hits)
        ]

        # —— 节点：BM25 关键词检索 + 混合融合（V2.3 / V2.4）——
        if use_hybrid:
            bm25_query = " ".join(keyword_query) if keyword_query else query
            kw_source = "来自 Query 理解 / 改写的关键词" if keyword_query else "原始问题"
            bm25_start = now_str()
            t2 = time.time()
            bm25_hits = bm25_search(bm25_query, top_k=top_k, filters=used_filters,
                                    config=self.cfg)
            bm25_elapsed = time.time() - t2
            keyword_items = [
                RetrievedItem(rank=i + 1, score=hit["score"], text=hit["payload"].get("text", ""),
                              metadata={k: v for k, v in hit["payload"].items() if k != "text"},
                              channels="关键词")
                for i, hit in enumerate(bm25_hits)
            ]
            if trace is not None:
                lines = [f"[{i.rank}] {i.metadata.get('source')}  BM25分={i.score:.2f}  {i.metadata.get('section') or '-'}"
                         for i in keyword_items]
                trace.append(make_node(
                    "🔑", "BM25 关键词检索", time_str=bm25_start, elapsed=bm25_elapsed,
                    summary="关键词通道：jieba 分词后按 BM25 算法打分（词是否命中 / 是否稀有），"
                            "擅长专有名词、编号、缩写这类需要精确匹配的词——正好补向量检索的盲区",
                    items=[
                        ("检索词", f"{bm25_query}　（{kw_source}）"),
                        ("输入", f"过滤范围内全部卡片的分词全文（分词器：jieba）"),
                        ("输出", f"{len(keyword_items)} 条命中（BM25 分数只用于排序，与向量相似度分不可直接比较）"),
                        ("命中清单", "\n".join(lines) or "（无命中）"),
                    ],
                ))

            fuse_start = now_str()
            items, fuse_lines = _rrf_fuse(items, keyword_items, top_k)
            if trace is not None:
                trace.append(make_node(
                    "🔀", "混合检索融合（RRF）", time_str=fuse_start,
                    summary="把向量通道和关键词通道的排名合并：每张卡片的融合分 = Σ 1/(60 + 该通道名次)，"
                            "两路都命中的卡片天然排得更靠前",
                    items=[
                        ("融合公式", "RRF：融合分 = Σ 1/(60 + 通道内名次)"),
                        ("融合明细", "\n".join(fuse_lines) or "（无）"),
                        ("输出", f"融合后 Top {len(items)} 条，每条标注来源通道"),
                    ],
                ))

        # —— 节点：召回 Chunk ——
        if trace is not None:
            lines = []
            for i in items:
                line = f"[{i.rank}] {i.metadata.get('source')}  score={i.score:.4f}  {i.metadata.get('section') or '-'}"
                if i.metadata.get("page"):
                    line += f"  第{i.metadata['page']}页"
                lines.append(line)
            trace.append(make_node(
                "📄", "召回 Chunk", time_str=now_str(),
                summary="数据库返回的原始知识卡片（Top K，按相似度降序）；逐条明细表见本节点下方",
                items=[("召回清单", "\n".join(lines) or "（无结果）")],
            ))
            # 条件节点：空结果分支（只在真的没召回时出现）
            if not items:
                trace.append(make_node(
                    "⚠️", "空结果分支", time_str=now_str(),
                    summary="本次没有召回任何知识卡片。注意：语义检索几乎总能返回「最相近」的 K 条"
                            "（哪怕都不相关），所以空结果通常不是『内容不相关』，而是过滤条件筛空了"
                            "（比如限定了领域/状态但范围内没有数据）。系统仍会把空资料发给大模型，"
                            "预期它按提示词规则回答「知识库中没有相关内容」而不是编造",
                    items=[
                        ("触发条件", "过滤后可检索的卡片数量 = 0"),
                        ("后续走向", "继续走 Prompt → LLM → 后处理，但资料附页为空"),
                        ("排查方向", "检查 Metadata Filter 条件是否过严，或知识库里是否缺整个分类的数据"),
                    ],
                ))

        if self.cfg.debug:
            print(f"    检索耗时 {time.time() - t0:.2f} 秒，召回 {len(hits)} 条，过滤条件：{used_filters}")
        return items


def _rrf_fuse(vector_items: list[RetrievedItem], keyword_items: list[RetrievedItem],
              top_k: int, k: int = 60) -> tuple[list[RetrievedItem], list[str]]:
    """RRF（Reciprocal Rank Fusion）融合两路召回。

    每张卡片的融合分 = Σ 1/(k + 该通道内的名次)，两路都命中天然得分更高。
    返回 (融合后的 Top K, 供调试展示的逐条明细行)。
    """
    fused: dict[str, dict] = {}
    entries: dict[str, RetrievedItem] = {}
    for channel, items in (("向量", vector_items), ("关键词", keyword_items)):
        for rank, item in enumerate(items, start=1):
            key = item.metadata.get("chunk_id") or f"{item.metadata.get('source')}::{item.text[:32]}"
            entry = fused.setdefault(key, {"score": 0.0, "向量": None, "关键词": None})
            entry["score"] += 1 / (k + rank)
            entry[channel] = entry[channel] or rank
            entries.setdefault(key, item)

    ranked = sorted(fused.items(), key=lambda kv: -kv[1]["score"])[:top_k]
    out: list[RetrievedItem] = []
    detail_lines: list[str] = []
    for final_rank, (key, entry) in enumerate(ranked, start=1):
        item = entries[key]
        channels = [c for c in ("向量", "关键词") if entry[c]]
        item.channels = " + ".join(channels)
        item.rank = final_rank
        out.append(item)
        detail_lines.append(
            f"[{final_rank}] {item.metadata.get('source')}  "
            f"向量排名={entry['向量'] or '-'}  关键词排名={entry['关键词'] or '-'}  "
            f"RRF分={entry['score']:.4f}  通道={item.channels}"
        )
    return out, detail_lines


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

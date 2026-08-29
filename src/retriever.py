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
from src.trace import make_node, now_str
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

    def retrieve(self, query: str, top_k: int | None = None,
                 filters: dict | None = None, trace: list | None = None) -> list[RetrievedItem]:
        """检索：问题 → 问题向量 → 默认过滤 + Metadata 过滤 → 最相近的 top_k 张卡片。

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
                    ("本次使用的过滤条件", str(used_filters)),
                ],
            ))

        # —— 节点：数据库检索 ——
        search_start = now_str()
        t1 = time.time()
        hits = self.store.search(query_vector, top_k=top_k, filters=used_filters)
        search_elapsed = time.time() - t1
        if trace is not None:
            trace.append(make_node(
                "🗄", "数据库检索", time_str=search_start, elapsed=search_elapsed,
                summary=f"拿问题指纹到 Qdrant（{self.cfg.qdrant_url}）里做相似度检索",
                items=[
                    ("输入", f"{len(query_vector)} 维问题向量 + 过滤条件 {used_filters}"),
                    ("输出", f"{len(hits)} 条召回结果（每条带相似度分数）"),
                ],
            ))

        items = [
            RetrievedItem(rank=i + 1, score=hit["score"], text=hit["payload"].get("text", ""),
                          metadata={k: v for k, v in hit["payload"].items() if k != "text"})
            for i, hit in enumerate(hits)
        ]

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

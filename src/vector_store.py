"""节点 5：向量数据库（Qdrant）。

产品视角：这一步相当于「知识卡片档案馆」——
每张卡片存三样东西：
  1. 编号（id）
  2. 指纹（vector，向量化算出来的那串数字）
  3. 卡片原文 + 档案卡（text + metadata）
存进去之后，支持「拿一张新指纹，找出馆里最相近的 N 张卡片」。
"""

from __future__ import annotations

import sys
from functools import lru_cache

from qdrant_client import QdrantClient, models

from src.config import AppConfig, get_config
from src.chunker import Chunk

# 建立 Payload Index 的字段：给 Metadata Filter 查询提速
# （topic / tags 是列表字段，Qdrant 的 keyword 索引对「任一元素匹配」生效）
INDEXED_FIELDS = [
    "document_id", "source", "domain", "category",
    "topic", "tags", "status", "version", "file_type",
]


class VectorStoreError(Exception):
    """向量库操作失败时抛出，信息直接给人看。"""


class VectorStore:
    """Qdrant 的封装：建馆、存卡、查卡、盘点、清空。"""

    def __init__(self, config: AppConfig | None = None):
        self.cfg = config or get_config()
        try:
            self.client = QdrantClient(url=self.cfg.qdrant_url, timeout=30)
        except Exception as exc:
            raise VectorStoreError(f"连不上 Qdrant（{self.cfg.qdrant_url}）：{exc}") from exc

    # ---------- 建馆 / 盘点 ----------
    def collection_exists(self) -> bool:
        return self.client.collection_exists(self.cfg.qdrant_collection)

    def ensure_collection(self, vector_dimension: int) -> None:
        """确保档案馆存在、「指纹规格」与模型一致，并为过滤字段建好索引。"""
        if not self.collection_exists():
            self.client.create_collection(
                collection_name=self.cfg.qdrant_collection,
                vectors_config=models.VectorParams(
                    size=vector_dimension, distance=models.Distance.COSINE
                ),
            )
            if self.cfg.debug:
                print(f"    已创建集合 {self.cfg.qdrant_collection}（向量维度 {vector_dimension}）")
        else:
            info = self.client.get_collection(self.cfg.qdrant_collection)
            existing_dim = info.config.params.vectors.size
            if existing_dim != vector_dimension:
                raise VectorStoreError(
                    f"集合里旧数据是 {existing_dim} 维，当前模型算出来是 {vector_dimension} 维，对不上。"
                    "这说明你换过向量化模型，需要重建知识库（界面点「重建知识库」或运行 python ingest.py --rebuild）。"
                )
        for field in INDEXED_FIELDS:
            self.client.create_payload_index(
                collection_name=self.cfg.qdrant_collection,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
                wait=True,
            )

    def count(self) -> int:
        """馆里一共有多少张卡片。"""
        if not self.collection_exists():
            return 0
        return self.client.count(self.cfg.qdrant_collection, exact=True).count

    # ---------- 存卡 ----------
    def upsert_chunks(self, chunks: list[Chunk], vectors: list[list[float]], batch_size: int = 128) -> int:
        """把知识卡片连同指纹批量存入。同 id 覆盖（重复入库不产生重复数据）。"""
        if len(chunks) != len(vectors):
            raise VectorStoreError("卡片数量和向量数量不一致，程序逻辑有误。")
        total = 0
        for begin in range(0, len(chunks), batch_size):
            batch_chunks = chunks[begin : begin + batch_size]
            points = [
                models.PointStruct(
                    id=chunk.point_id,
                    vector=vector,
                    payload={"text": chunk.text, **chunk.metadata},
                )
                for chunk, vector in zip(batch_chunks, vectors[begin : begin + batch_size])
            ]
            self.client.upsert(collection_name=self.cfg.qdrant_collection, points=points, wait=True)
            total += len(points)
            if self.cfg.debug and len(chunks) > batch_size:
                print(f"    入库进度：{total}/{len(chunks)}")
        return total

    def delete_documents(self, document_ids: list[str]) -> int:
        """按 document_id 删除整篇文档的所有卡片（重新入库前先清旧数据）。"""
        if not document_ids or not self.collection_exists():
            return 0
        result = self.client.delete(
            collection_name=self.cfg.qdrant_collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[models.FieldCondition(
                        key="document_id", match=models.MatchAny(any=document_ids)
                    )]
                )
            ),
            wait=True,
        )
        return len(document_ids)

    # ---------- 查卡 ----------
    def search(
        self,
        query_vector: list[float],
        top_k: int,
        filters: dict | None = None,
    ) -> list[dict]:
        """拿一张指纹找最相近的 top_k 张卡片，可叠加 Metadata 过滤条件。

        filters 形如 {"domain": "learning", "topic": ["RAG", "Agent"], "status": "active"}。
        返回 [{score, payload}, ...]，分数越高越相关。
        """
        if not self.collection_exists():
            raise VectorStoreError(
                "知识库还是空的，请先入库（运行 python ingest.py 或在界面里上传文档）。"
            )
        result = self.client.query_points(
            collection_name=self.cfg.qdrant_collection,
            query=query_vector,
            limit=top_k,
            query_filter=self.build_filter(filters),
            with_payload=True,
        )
        return [{"score": point.score, "payload": point.payload or {}} for point in result.points]

    @staticmethod
    def build_filter(filters: dict | None) -> models.Filter | None:
        """把 {字段: 值} 转成 Qdrant 过滤器。值可以是字符串或字符串列表（任一匹配）。"""
        conditions = []
        for key, value in (filters or {}).items():
            if value is None:
                continue
            values = [value] if isinstance(value, str) else [str(v) for v in value]
            if not values:
                continue
            conditions.append(
                models.FieldCondition(key=key, match=models.MatchAny(any=values))
            )
        return models.Filter(must=conditions) if conditions else None

    # ---------- 按 source 查看 / 管理 ----------
    def list_sources(self) -> dict[str, int]:
        """盘点：每个文件各存了多少张卡片。"""
        sources: dict[str, int] = {}
        if not self.collection_exists():
            return sources
        offset = None
        while True:
            points, offset = self.client.scroll(
                collection_name=self.cfg.qdrant_collection,
                limit=256,
                offset=offset,
                with_payload=["source"],
            )
            for point in points:
                source = (point.payload or {}).get("source") or "(未知来源)"
                sources[source] = sources.get(source, 0) + 1
            if offset is None:
                break
        return dict(sorted(sources.items()))

    def sample_payloads_by_source(self, source: str, limit: int = 20) -> list[dict]:
        """查看某个文件的卡片内容（调试用）。"""
        if not self.collection_exists():
            return []
        points, _ = self.client.scroll(
            collection_name=self.cfg.qdrant_collection,
            scroll_filter=models.Filter(
                must=[models.FieldCondition(key="source", match=models.MatchValue(value=source))]
            ),
            limit=limit,
            with_payload=True,
        )
        return [point.payload or {} for point in points]

    # ---------- 知识文件盘点（Knowledge 页面数据源） ----------
    def expire_document(self, document_id: str) -> None:
        """把某文档的全部卡片标记为 status=expired（旧版本留作历史，不再参与默认检索）。"""
        self.client.set_payload(
            collection_name=self.cfg.qdrant_collection,
            payload={"status": "expired"},
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[models.FieldCondition(
                        key="document_id", match=models.MatchValue(value=document_id)
                    )]
                )
            ),
            wait=True,
        )

    def list_documents(self) -> list[dict]:
        """按 document_id 盘点所有入库文件：路径、分类、状态、卡片数。"""
        if not self.collection_exists():
            return []
        docs: dict[tuple, dict] = {}
        offset = None
        while True:
            points, offset = self.client.scroll(
                collection_name=self.cfg.qdrant_collection,
                limit=256,
                offset=offset,
                with_payload=["document_id", "source", "path", "domain", "category",
                              "topic", "version", "status"],
            )
            for point in points:
                payload = point.payload or {}
                key = (payload.get("document_id") or "(未知)",
                       payload.get("version") or "?",
                       payload.get("status") or "?")
                if key not in docs:
                    docs[key] = {
                        "document_id": key[0],
                        "source": payload.get("source") or "?",
                        "path": payload.get("path") or "?",
                        "domain": payload.get("domain") or "?",
                        "category": payload.get("category") or "?",
                        "topic": payload.get("topic") or [],
                        "version": key[1],
                        "status": key[2],
                        "chunks": 0,
                    }
                docs[key]["chunks"] += 1
            if offset is None:
                break
        return sorted(docs.values(), key=lambda d: d["path"])

    def get_all_cards(self, filters: dict | None = None, limit: int = 5000) -> list[dict]:
        """拉取（可按 Metadata 过滤的）全部卡片：[{"id", "payload"}, ...]。供 BM25 通道建索引用。"""
        if not self.collection_exists():
            return []
        cards: list[dict] = []
        offset = None
        while len(cards) < limit:
            points, offset = self.client.scroll(
                collection_name=self.cfg.qdrant_collection,
                scroll_filter=self.build_filter(filters),
                limit=min(256, limit - len(cards)),
                offset=offset,
                with_payload=True,
            )
            cards.extend({"id": point.id, "payload": point.payload or {}} for point in points)
            if offset is None:
                break
        return cards

    def chunks_by_document(self, document_id: str, limit: int = 50) -> list[dict]:
        """查看某篇文档的所有卡片（Knowledge 页面展开用）。"""
        if not self.collection_exists():
            return []
        points, _ = self.client.scroll(
            collection_name=self.cfg.qdrant_collection,
            scroll_filter=models.Filter(
                must=[models.FieldCondition(
                    key="document_id", match=models.MatchValue(value=document_id)
                )]
            ),
            limit=limit,
            with_payload=True,
        )
        return [point.payload or {} for point in points]

    def clear(self) -> None:
        """清空重建：整个档案馆推倒（下次入库会自动重建）。"""
        if self.collection_exists():
            self.client.delete_collection(self.cfg.qdrant_collection)


@lru_cache(maxsize=1)
def get_vector_store() -> VectorStore:
    return VectorStore()


# ---------------- 直接运行本文件：查看档案馆现状 ----------------
if __name__ == "__main__":
    store = get_vector_store()
    print(f"Qdrant 地址：{store.cfg.qdrant_url}")
    print(f"集合：{store.cfg.qdrant_collection}")
    if not store.collection_exists():
        print("状态：集合尚未创建（还没有入过库）")
    else:
        print(f"状态：✅ 共 {store.count()} 张知识卡片")
        print("\n按文件盘点：")
        for source, num in store.list_sources().items():
            print(f"  {source}: {num} 张")
    sys.exit(0)

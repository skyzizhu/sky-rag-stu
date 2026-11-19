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
import math
from functools import lru_cache

from qdrant_client import QdrantClient, models

from src import index_epoch
from src.config import AppConfig, get_config
from src.chunker import Chunk

# 建立 Payload Index 的字段：给 Metadata Filter 查询提速
# （topic / tags / allowed_groups 是列表字段，Qdrant 的 keyword 索引对「任一元素匹配」生效）
INDEXED_FIELDS = [
    "document_id", "source", "domain", "category", "doc_type",
    "topic", "tags", "status", "version", "file_type",
    "allowed_groups",  # V3.7 权限：可见部门列表
]
# 时间字段：建立 datetime 索引，支持范围过滤（如"最近三个月的笔记"）
INDEXED_DATETIME_FIELDS = ["created_at", "updated_at"]


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
        for field in INDEXED_DATETIME_FIELDS:
            self.client.create_payload_index(
                collection_name=self.cfg.qdrant_collection,
                field_name=field,
                field_schema=models.PayloadSchemaType.DATETIME,
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
        index_epoch.bump()
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
        index_epoch.bump()
        return len(document_ids)

    # ---------- 查卡 ----------
    def search(
        self,
        query_vector: list[float],
        top_k: int,
        filters: dict | None = None,
        timeout: float | None = None,
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
            **({"timeout": max(1, math.ceil(timeout))} if timeout is not None else {}),
        )
        return [{"score": point.score, "payload": point.payload or {}} for point in result.points]

    @staticmethod
    def build_filter(filters: dict | None) -> models.Filter | None:
        """把 {字段: 值} 转成 Qdrant 过滤器。

        - 字符串值 → 精确/任一匹配；列表值 → 任一匹配；
        - doc_type="__unclassified__" → 缺失、null 或空字符串；与其他条件保持 AND。
        - time_range 形如 {"from": "2026-06-01", "to": "2026-08-29"} → 时间范围过滤；
          key 必须是 datetime 字段（created_at / updated_at）。
        """
        conditions = []
        for key, value in (filters or {}).items():
            if value is None:
                continue
            if key == "doc_type" and value == "__unclassified__":
                conditions.append(models.Filter(should=[
                    models.IsEmptyCondition(is_empty=models.PayloadField(key="doc_type")),
                    models.FieldCondition(key="doc_type", match=models.MatchValue(value="")),
                ]))
                continue
            if key in INDEXED_DATETIME_FIELDS and isinstance(value, dict):
                date_from, date_to = value.get("from"), value.get("to")
                if not date_from and not date_to:
                    continue
                if date_to and len(date_to) == 10:  # 纯日期补全到当天末尾
                    date_to = date_to + "T23:59:59"
                conditions.append(models.FieldCondition(
                    key=key,
                    range=models.DatetimeRange(
                        gte=date_from or None,
                        lte=date_to or None,
                    ),
                ))
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
        if not self.collection_exists():
            return  # 集合还不存在（首次使用/被手动删除）时无旧版本可过期
        self.client.set_payload(
            collection_name=self.cfg.qdrant_collection,
            payload={"status": "expired"},
            # qdrant-client ≥1.10：set_payload 的过滤选择器参数由 points_selector
            # 统一改名为 points（delete 仍叫 points_selector，勿混淆）
            points=models.FilterSelector(
                filter=models.Filter(
                    must=[models.FieldCondition(
                        key="document_id", match=models.MatchValue(value=document_id)
                    )]
                )
            ),
            wait=True,
        )
        index_epoch.bump()

    def reassign_domain(self, document_ids: list[str], domain: str) -> None:
        """把指定文档全部卡片（含历史版本）的 domain 字段就地改写——只改 payload，
        不重新向量化。用于把「未登记目录」登记为领域（此前被兜底归到 reference）。"""
        if not document_ids or not self.collection_exists():
            return
        self.client.set_payload(
            collection_name=self.cfg.qdrant_collection,
            payload={"domain": domain},
            points=models.FilterSelector(
                filter=models.Filter(
                    must=[models.FieldCondition(
                        key="document_id", match=models.MatchAny(any=document_ids)
                    )]
                )
            ),
            wait=True,
        )
        index_epoch.bump()  # 卡片数不变的写操作：必须显式通知所有进程的检索缓存

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
                with_payload=["document_id", "source", "path", "domain", "category", "doc_type",
                              "topic", "version", "status", "allowed_groups", "file_type",
                              "indexed_at", "owner", "title", "effective_date",
                              "classification", "document_version", "document_version_status",
                              "document_version_source", "document_version_evidence", "version_content_hash"],
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
                        "doc_type": payload.get("doc_type") or "",
                        "topic": payload.get("topic") or [],
                        "allowed_groups": payload.get("allowed_groups") or ["all"],
                        "file_type": payload.get("file_type") or "",
                        "indexed_at": payload.get("indexed_at") or "",
                        "owner": payload.get("owner") or "",
                        "title": payload.get("title") or "",
                        "effective_date": payload.get("effective_date") or "",
                        "classification": payload.get("classification") or "公开",
                        "document_version": payload.get("document_version") or "",
                        "document_version_status": payload.get("document_version_status") or "unavailable",
                        "document_version_source": payload.get("document_version_source") or "",
                        "document_version_evidence": payload.get("document_version_evidence") or [],
                        "version_content_hash": payload.get("version_content_hash") or "",
                        "version": key[1],
                        "status": key[2],
                        "chunks": 0,
                    }
                docs[key]["chunks"] += 1
            if offset is None:
                break
        return sorted(docs.values(), key=lambda d: d["path"])

    def categories(self, filters: dict | None = None) -> list[str]:
        """查询理解只需分类名，使用索引分面，不逐张卡片扫描全库。"""
        if not self.collection_exists():
            return []
        response = self.client.facet(collection_name=self.cfg.qdrant_collection, key="category",
                                     facet_filter=self.build_filter(filters), limit=128)
        return sorted(str(hit.value) for hit in response.hits)

    def document_catalog(self, filters: dict | None = None, max_documents: int = 200,
                         max_points: int = 50000, date_field: str = "updated_at",
                         dates: dict | None = None) -> dict:
        """只滚动读取元数据，按 document_id 去重；达到预算时明确返回不完整标记。"""
        documents = {}
        offset = None
        scanned = 0
        unknown_dates = set()
        if self.collection_exists():
            while scanned < max_points:
                points, offset = self.client.scroll(
                    collection_name=self.cfg.qdrant_collection,
                    scroll_filter=self.build_filter(filters), offset=offset,
                    limit=min(256, max_points - scanned), with_vectors=False,
                    with_payload=["document_id", "source", "path", "title", "domain", "category", "doc_type",
                                  "version", "status", "indexed_at", "updated_at"],
                )
                scanned += len(points)
                for point in points:
                    meta = point.payload or {}
                    meta["doc_type"] = meta.get("doc_type") or ""
                    if dates:
                        date = str(meta.get(date_field) or "")[:10]
                        if not date:
                            unknown_dates.add(meta.get("document_id") or meta.get("path"))
                            continue
                        if not (dates.get("from", "") <= date <= dates.get("to", "9999-12-31")):
                            continue
                    key = meta.get("document_id") or meta.get("path")
                    if key:
                        documents.setdefault(key, meta)
                if offset is None:
                    break
        ordered = sorted(documents.values(), key=lambda m: m.get("path") or m.get("source") or "")
        return {"documents": ordered[:max_documents], "matched_documents": len(ordered),
                "scan_truncated": offset is not None, "scanned_points": scanned,
                "unknown_date_documents": len(unknown_dates),
                "list_truncated": len(ordered) > max_documents}

    def card_page(self, filters: dict, offset=None, limit: int = 8, timeout: float | None = None):
        """第二阶段按游标读取卡片；每一页都在服务端执行相同权限/状态/版本条件。"""
        if not self.collection_exists():
            return [], None
        points, next_offset = self.client.scroll(
            collection_name=self.cfg.qdrant_collection, scroll_filter=self.build_filter(filters),
            offset=offset, limit=limit, with_vectors=False, with_payload=True,
            **({"timeout": max(1, math.ceil(timeout))} if timeout is not None else {}),
        )
        return [{"id": str(p.id), "payload": p.payload or {}} for p in points], next_offset

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
            index_epoch.bump()


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

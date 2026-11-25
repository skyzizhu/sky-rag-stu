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
from collections import Counter
from heapq import nsmallest
from functools import lru_cache
from itertools import chain

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from src import index_epoch
from src.console import console_print
from src.config import AppConfig, get_config
from src.chunker import Chunk
from src.metadata import chunk_sequence

# 建立 Payload Index 的字段：给 Metadata Filter 查询提速
# （topic / tags / allowed_groups 是列表字段，Qdrant 的 keyword 索引对「任一元素匹配」生效）
INDEXED_FIELDS = [
    "document_id", "source", "domain", "doc_type", "department",
    "topic", "tags", "status", "version", "file_type",
    "allowed_groups",  # V3.7 权限：可见部门列表
    "allowed_users",  # V3.9 三级密级：受限名单（员工用户名）
]
# 时间字段：建立 datetime 索引，支持范围过滤（如"最近三个月的笔记"）
INDEXED_DATETIME_FIELDS = ["created_at", "updated_at", "indexed_at"]
INGEST_PENDING = "_ingest_pending"


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

    def _missing_collection(self, exc: Exception) -> bool:
        if isinstance(exc, UnexpectedResponse):
            return exc.status_code == 404
        # QdrantLocal reports a missing collection with ValueError.
        return isinstance(exc, ValueError) and not self.collection_exists()

    def ensure_collection(self, vector_dimension: int) -> None:
        """确保档案馆存在、「指纹规格」与模型一致，并为过滤字段建好索引。"""
        try:
            info = self.client.get_collection(self.cfg.qdrant_collection)
        except (UnexpectedResponse, ValueError) as exc:
            if not self._missing_collection(exc):
                raise
            info = None
        if info is None:
            self.client.create_collection(
                collection_name=self.cfg.qdrant_collection,
                vectors_config=models.VectorParams(
                    size=vector_dimension, distance=models.Distance.COSINE
                ),
            )
            if self.cfg.debug:
                console_print(f"    已创建集合 {self.cfg.qdrant_collection}（向量维度 {vector_dimension}）")
        else:
            info = self.client.get_collection(self.cfg.qdrant_collection)
            existing_dim = info.config.params.vectors.size
            if existing_dim != vector_dimension:
                raise VectorStoreError(
                    f"集合里旧数据是 {existing_dim} 维，当前模型算出来是 {vector_dimension} 维，对不上。"
                    "这说明你换过向量化模型，需要重建知识库（界面点「重建知识库」或运行 python ingest.py --rebuild）。"
                )
        existing = (info.payload_schema or {}) if info is not None else {}
        required = {**dict.fromkeys(INDEXED_FIELDS, models.PayloadSchemaType.KEYWORD),
                    **dict.fromkeys(INDEXED_DATETIME_FIELDS, models.PayloadSchemaType.DATETIME),
                    "project_id": models.PayloadSchemaType.INTEGER,
                    "chunk_index": models.PayloadSchemaType.INTEGER}
        for field, schema in required.items():
            if field in existing and existing[field].data_type == schema:
                continue
            self.client.create_payload_index(
                collection_name=self.cfg.qdrant_collection,
                field_name=field,
                field_schema=schema,
                wait=True,
            )

    def ensure_project_index(self) -> None:
        self.client.create_payload_index(
            collection_name=self.cfg.qdrant_collection,
            field_name="project_id",
            field_schema=models.PayloadSchemaType.INTEGER,
            wait=True,
        )

    def ensure_allowed_users_index(self) -> None:
        self.client.create_payload_index(
            collection_name=self.cfg.qdrant_collection, field_name="allowed_users",
            field_schema=models.PayloadSchemaType.KEYWORD, wait=True,
        )

    def count(self) -> int:
        """馆里一共有多少张卡片。"""
        return self.count_existing() or 0

    def count_existing(self) -> int | None:
        """Count published cards, distinguishing an empty collection from a missing one."""
        try:
            return self.client.count(self.cfg.qdrant_collection,
                                     count_filter=self.published_filter(None), exact=True).count
        except (UnexpectedResponse, ValueError) as exc:
            if self._missing_collection(exc):
                return None
            raise

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
                console_print(f"    入库进度：{total}/{len(chunks)}")
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
        try:
            result = self.client.query_points(
                collection_name=self.cfg.qdrant_collection,
                query=query_vector,
                limit=top_k,
                query_filter=self.published_filter(filters),
                with_payload=True,
                **({"timeout": max(1, math.ceil(timeout))} if timeout is not None else {}),
            )
        except (UnexpectedResponse, ValueError) as exc:
            if not self._missing_collection(exc):
                raise
            raise VectorStoreError(
                "知识库还是空的，请先入库（运行 python ingest.py 或在界面里上传文档）。"
            ) from exc
        return [{"score": point.score, "payload": point.payload or {}} for point in result.points]

    @staticmethod
    def build_filter(filters: dict | None) -> models.Filter | None:
        """把 {字段: 值} 转成 Qdrant 过滤器。

        - 字符串值 → 精确/任一匹配；列表值 → 任一匹配；
        - doc_type="__unclassified__" → 缺失、null 或空字符串；与其他条件保持 AND。
        - department="__no_department__" → 缺失、null 或空字符串；与其他条件保持 AND。
        - "__viewer__"（三级密级）：{"groups": 用户组, "username": 用户名} →
          allowed_groups 命中用户组 OR allowed_users 含本人用户名；与其他条件保持 AND。
        - time_range 形如 {"from": "2026-06-01", "to": "2026-08-29"} → 时间范围过滤；
          key 必须是 datetime 字段（created_at / updated_at）。
        """
        conditions = []
        for key, value in (filters or {}).items():
            if value is None:
                continue
            if key in ('allowed_groups', 'allowed_users') and '__viewer__' in (filters or {}):
                continue  # viewer 下推已经统一权限，不能再 AND 一个部门条件排掉个人授权。
            if key == "__viewer__":
                groups = list(value.get("groups") or [])
                username = str(value.get("username") or "")
                clauses = [models.FieldCondition(
                    key='allowed_groups', match=models.MatchAny(any=groups))]
                if username:
                    clauses.append(models.FieldCondition(
                        key="allowed_users", match=models.MatchAny(any=[username])))
                conditions.append(models.Filter(should=clauses))
                continue
            if key == 'classification':
                no_users = models.IsEmptyCondition(is_empty=models.PayloadField(key='allowed_users'))
                no_groups = models.IsEmptyCondition(is_empty=models.PayloadField(key='allowed_groups'))
                all_group = models.FieldCondition(key='allowed_groups', match=models.MatchValue(value='all'))
                if value == '公开':
                    conditions.append(models.Filter(must=[no_users, all_group]))
                elif value == '内部':
                    conditions.append(models.Filter(must=[no_users], must_not=[no_groups, all_group]))
                elif value == '受限':
                    conditions.append(models.Filter(should=[no_groups, models.Filter(must_not=[no_users])]))
                else:
                    raise ValueError('未知的文档密级')
                continue
            if key == "doc_type" and value == "__unclassified__":
                conditions.append(models.Filter(should=[
                    models.IsEmptyCondition(is_empty=models.PayloadField(key="doc_type")),
                    models.FieldCondition(key="doc_type", match=models.MatchValue(value="")),
                ]))
                continue
            if key == "department" and value == "__no_department__":
                conditions.append(models.Filter(should=[
                    models.IsEmptyCondition(is_empty=models.PayloadField(key="department")),
                    models.FieldCondition(key="department", match=models.MatchValue(value="")),
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
            if key == 'allowed_groups':
                if not value:
                    conditions.append(models.FieldCondition(key=key, match=models.MatchAny(any=[])))
                    continue
            if key == 'project_id':
                ids = list(value) if isinstance(value, (list, tuple)) else [value]
                if not ids or any(type(v) is not int or v < 1 for v in ids):
                    raise ValueError('project_id must contain positive integer IDs')
                conditions.append(models.FieldCondition(key=key, match=models.MatchAny(any=ids)))
                continue
            if isinstance(value, int) and not isinstance(value, bool):
                # 数值标量（如 project_id）：精确匹配，不转字符串
                conditions.append(models.FieldCondition(
                    key=key, match=models.MatchValue(value=value)))
                continue
            if isinstance(value, (list, tuple, set)):
                # 保留数值元素原类型（project_id 等 int 载荷与 int 过滤值才能严格匹配）
                values = [v for v in value]
            else:
                values = [value]
            if not values:
                continue  # 空列表 = 不筛选该字段（如 topic=[]），不生成空 MatchAny 条件
            conditions.append(
                models.FieldCondition(key=key, match=models.MatchAny(any=values))
            )
        return models.Filter(must=conditions) if conditions else None

    @classmethod
    def published_filter(cls, filters: dict | None) -> models.Filter:
        base = cls.build_filter(filters)
        return models.Filter(
            must=[base] if base else None,
            must_not=[models.FieldCondition(key=INGEST_PENDING,
                                            match=models.MatchValue(value=True))])

    # ---------- 按 source 查看 / 管理 ----------
    def _point_pages(self, selector, *, fields=True, limit: int | None = None):
        offset = None
        while limit is None or limit > 0:
            points, offset = self.client.scroll(
                collection_name=self.cfg.qdrant_collection, scroll_filter=selector,
                limit=256 if limit is None else min(256, limit), offset=offset,
                with_vectors=False, with_payload=fields,
            )
            yield points, offset
            if offset is None:
                break
            if limit is not None:
                limit -= len(points)

    def iter_payloads(self, filters: dict | None, fields: list[str]):
        """Stream a projected view of published cards under the supplied filters."""
        if self.collection_exists():
            for points, _ in self._point_pages(self.published_filter(filters), fields=fields):
                yield from (point.payload or {} for point in points)

    def list_sources(self) -> dict[str, int]:
        """盘点：每个文件各存了多少张卡片。"""
        sources = Counter(payload.get("source") or "(未知来源)"
                          for payload in self.iter_payloads(None, ["source"]))
        return dict(sorted(sources.items()))

    def sample_payloads_by_source(self, source: str, limit: int = 20) -> list[dict]:
        """查看某个文件的卡片内容（调试用）。"""
        if not self.collection_exists():
            return []
        points, _ = self.client.scroll(
            collection_name=self.cfg.qdrant_collection,
            scroll_filter=self.published_filter({"source": source}),
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

    def list_documents(self, filters: dict | None = None) -> list[dict]:
        """按 document_id 盘点入库文件，可在扫描前限定元数据范围。"""
        from src.permissions import classify_visibility
        docs: dict[tuple, dict] = {}
        defaults = {"source": "?", "path": "?", "domain": "?", "category": "",
                    "doc_type": "", "department": "",
                    "file_type": "", "indexed_at": "", "owner": "", "title": "", "effective_date": "",
                    "document_version": "", "document_version_status": "unavailable",
                    "document_version_source": "",
                    "version_content_hash": ""}
        array_fields = ("topic", "allowed_users", "document_version_evidence")
        fields = ["document_id", "version", "status", "project_id", "allowed_groups",
                  *defaults, *array_fields]
        for payload in self.iter_payloads(filters, fields):
            key = (payload.get("document_id") or "(未知)",
                   payload.get("version") or "?", payload.get("status") or "?")
            if key not in docs:
                docs[key] = {
                    **{field: payload.get(field) or default for field, default in defaults.items()},
                    **{field: payload.get(field) or [] for field in array_fields},
                    "document_id": key[0], "version": key[1], "status": key[2], "chunks": 0,
                    "project_id": payload.get("project_id"),
                    "allowed_groups": payload.get("allowed_groups", []),
                    "classification": classify_visibility(payload.get("allowed_groups"), payload.get("allowed_users")),
                }
            docs[key]["chunks"] += 1
        return sorted(docs.values(), key=lambda d: d["path"])

    def document_catalog(self, filters: dict | None = None, max_documents: int = 200,
                         max_points: int = 50000, date_field: str = "updated_at",
                         dates: dict | None = None, *, select_document=None) -> dict:
        """只滚动读取元数据，按 document_id 去重；达到预算时明确返回不完整标记。"""
        documents = {}
        offset = None
        from src.permissions import classify_visibility

        scanned = 0
        unknown_dates = set()
        if self.collection_exists():
            for points, offset in self._point_pages(
                self.published_filter(filters), limit=max_points,
                fields=["document_id", "source", "path", "title", "domain", "doc_type",
                        "department", "project_id", "version", "status", "indexed_at", "updated_at",
                        "allowed_groups", "allowed_users"],
            ):
                scanned += len(points)
                for point in points:
                    meta = point.payload or {}
                    if select_document is not None and not select_document(meta):
                        continue
                    key = meta.get("document_id") or meta.get("path")
                    if dates:
                        date = str(meta.get(date_field) or "")[:10]
                        if not date:
                            unknown_dates.add(key)
                            continue
                        if not (dates.get("from", "") <= date <= dates.get("to", "9999-12-31")):
                            continue
                    if not key or key in documents:
                        continue
                    meta["doc_type"] = meta.get("doc_type") or ""
                    meta["department"] = meta.get("department") or ""
                    meta["classification"] = classify_visibility(
                        meta.pop("allowed_groups", None), meta.pop("allowed_users", None))
                    documents[key] = meta
        ordered = sorted(documents.values(), key=lambda m: m.get("path") or m.get("source") or "")
        return {"documents": ordered[:max_documents], "matched_documents": len(ordered),
                "scan_truncated": offset is not None, "scanned_points": scanned,
                "unknown_date_documents": len(unknown_dates),
                "list_truncated": len(ordered) > max_documents}

    def card_page(self, filters: dict, offset=None, limit: int = 8, timeout: float | None = None,
                  *, chunk_ids: list[str] | None = None):
        """第二阶段按游标读取卡片；每一页都在服务端执行相同权限/状态/版本条件。"""
        if not self.collection_exists():
            return [], None
        predicate = self.published_filter(filters)
        if chunk_ids is not None:
            predicate = models.Filter(must=[predicate, models.FieldCondition(
                key="chunk_id", match=models.MatchAny(any=chunk_ids))])
        points, next_offset = self.client.scroll(
            collection_name=self.cfg.qdrant_collection, scroll_filter=predicate,
            offset=offset, limit=limit, with_vectors=False, with_payload=True,
            **({"timeout": max(1, math.ceil(timeout))} if timeout is not None else {}),
        )
        return [{"id": str(p.id), "payload": p.payload or {}} for p in points], next_offset

    def source_ordered_card_page(self, filters: dict, cursor=None, limit: int = 8,
                                 timeout: float | None = None):
        """Page through a document version by chunk index, then read legacy cards."""
        if cursor is not None and not isinstance(cursor, tuple):
            return self.card_page(filters, offset=cursor, limit=limit, timeout=timeout)
        next_index, legacy_offset = cursor if cursor is not None else (0, None)
        collection = self.cfg.qdrant_collection
        predicate = self.published_filter(filters)
        missing = models.IsEmptyCondition(is_empty=models.PayloadField(key="chunk_index"))
        options = {"with_vectors": False, "with_payload": True,
                   **({"timeout": max(1, math.ceil(timeout))} if timeout is not None else {})}
        if next_index is not None:
            try:
                points, _ = self.client.scroll(
                    collection, scroll_filter=predicate, limit=limit,
                    order_by=models.OrderBy(key="chunk_index", start_from=next_index), **options,
                )
            except UnexpectedResponse as exc:
                if self._missing_collection(exc):
                    return [], None
                error = str(exc).lower()
                if (cursor is not None or exc.status_code != 400 or
                        "chunk_index" not in error or "index" not in error.replace("chunk_index", "")):
                    raise
                # An older collection may not have the index required for server-side ordering.
                return self.card_page(filters, limit=limit, timeout=timeout)
            if points:
                next_index = points[-1].payload["chunk_index"] + 1
                rows = [{"id": str(p.id), "payload": p.payload or {}} for p in points]
                return rows, (next_index if len(points) == limit else None, None)
        points, offset = self.client.scroll(collection, scroll_filter=models.Filter(must=[predicate, missing]),
                                            offset=legacy_offset, limit=limit, **options)
        return ([{"id": str(p.id), "payload": p.payload or {}} for p in points],
                (None, offset) if offset is not None else None)

    def get_all_cards(self, filters: dict | None = None, limit: int = 5000) -> list[dict]:
        """拉取（可按 Metadata 过滤的）全部卡片：[{"id", "payload"}, ...]。供 BM25 通道建索引用。"""
        if not self.collection_exists():
            return []
        return [{"id": point.id, "payload": point.payload or {}}
                for points, _ in self._point_pages(self.published_filter(filters), limit=limit)
                for point in points]

    def ordered_document_cards(self, filters: dict, limit: int, *,
                               indices: set[int] | None = None) -> list[dict]:
        """Read a specific document version by source ordinal, including legacy cards."""
        if limit <= 0 or indices == set() or not self.collection_exists():
            return []
        predicate = self.published_filter(filters)
        missing = models.IsEmptyCondition(is_empty=models.PayloadField(key="chunk_index"))
        collection = self.cfg.qdrant_collection

        if indices is not None:
            current = self.get_all_cards({**filters, "chunk_index": sorted(indices)}, limit=len(indices))
        else:
            # Existing collections may predate the integer index. Only use order_by
            # when this version contains cards written with the indexed field.
            indexed_filter = models.Filter(must=[predicate], must_not=[missing])
            probe, _ = self.client.scroll(collection, limit=1, scroll_filter=indexed_filter,
                                          with_payload=False, with_vectors=False)
            points = []
            if probe:
                points, _ = self.client.scroll(collection, limit=limit, scroll_filter=indexed_filter,
                                               order_by="chunk_index", with_vectors=False)
            current = [{"id": point.id, "payload": point.payload or {}} for point in points]
        if indices is not None and len(current) == len(indices):
            return sorted(current, key=lambda row: chunk_sequence(row["payload"]))

        def legacy_cards():
            for points, _ in self._point_pages(models.Filter(must=[predicate, missing])):
                for point in points:
                    payload = point.payload or {}
                    if indices is None or chunk_sequence(payload) in indices:
                        yield {"id": point.id, "payload": payload}

        def order(row):
            payload = row["payload"]
            number = chunk_sequence(payload)
            return number is None, number if number is not None else 0, str(row["id"])

        return nsmallest(limit, chain(current, legacy_cards()), key=order)

    def chunks_by_document(self, document_id: str, limit: int = 50, *,
                           filters: dict | None = None) -> list[dict]:
        """查看某篇文档的所有卡片（Knowledge 页面展开用）。"""
        if not self.collection_exists():
            return []
        points, _ = self.client.scroll(
            collection_name=self.cfg.qdrant_collection,
            scroll_filter=self.published_filter({**(filters or {}), "document_id": document_id}),
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

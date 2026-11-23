"""Publish replacement cards with a recovery record spanning Qdrant and the ledger."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from src import index_epoch, index_state
from src.console import console_print
from src.metadata import chunk_sequence, document_id_for
from src.vector_store import INGEST_PENDING, VectorStoreError


def _journal_path():
    return index_state.STATE_PATH.with_suffix(".publication.json")


def _write_journal(journal):
    path = _journal_path()
    tmp_path = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_text(json.dumps(journal, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


def publication_pending():
    return _journal_path().exists()


def publication_owns_token(token):
    try:
        journal = json.loads(_journal_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    return journal["acl_token"] == token


def _set_payload(store, ids, **payload):
    for begin in range(0, len(ids), 512):
        store.client.set_payload(store.cfg.qdrant_collection, payload,
                                 points=ids[begin:begin + 512], wait=True)


def _delete(store, ids):
    for begin in range(0, len(ids), 512):
        store.client.delete(store.cfg.qdrant_collection,
                            points_selector=ids[begin:begin + 512], wait=True)


def _activate(store, journal):
    _set_payload(store, journal["replaced"], **{INGEST_PENDING: True})
    _set_payload(store, journal["retired"], status="expired")
    _set_payload(store, journal["new"], **{INGEST_PENDING: False})
    index_epoch.bump()


def _clear_journal(path, journal, retire_token):
    retire_token(journal["acl_token"])
    path.unlink()


def _relocation_paths(store, journal):
    root = store.cfg.knowledge_dir.resolve()
    if root != Path(journal["knowledge_dir"]):
        raise VectorStoreError("有一份文档尚待移动恢复，请使用原知识目录配置重试。")
    source, target = (root / journal[key] for key in ("source", "target"))
    if any(not path.is_relative_to(root) or path.resolve() != path for path in (source, target)):
        raise VectorStoreError("待恢复的文档路径超出知识目录。")
    return source, target


def _recover_relocation(store, journal, state):
    from qdrant_client import models

    source, target = _relocation_paths(store, journal)
    committed = state.get(journal["target"], {}).get("ingest_generation") == journal["generation"]
    if not committed:
        if not source.exists() and target.is_file():
            target.rename(source)
        elif not source.is_file() or target.exists():
            raise VectorStoreError("文档移动未提交，但文件位置有冲突，请检查原位置和目标位置。")
        return

    if not target.is_file() or not store.collection_exists():
        raise VectorStoreError("已提交的文档移动尚待恢复，目标文件或原向量库不可用。")
    payload = {"document_id": document_id_for(journal["target"]), "path": journal["target"],
               "source": target.name, "domain": journal["domain"]}
    ids = [point["id"] for point in journal["points"]]
    _set_payload(store, ids, **{INGEST_PENDING: True})
    operations = []
    for point in journal["points"]:
        operations.append(models.SetPayloadOperation(set_payload=models.SetPayload(
            points=[point["id"]],
            payload={**payload, "chunk_id": point["chunk_id"], "status": point["status"]},
        )))
        if len(operations) == 256:
            store.client.batch_update_points(store.cfg.qdrant_collection, operations, wait=True)
            operations.clear()
    if operations:
        store.client.batch_update_points(store.cfg.qdrant_collection, operations, wait=True)
    _set_payload(store, ids, **{INGEST_PENDING: False})


def recover_publication(store, retire_token):
    """Complete a committed publication, or discard staged points after a failed ledger write."""
    path = _journal_path()
    if not path.exists():
        return
    journal = json.loads(path.read_text(encoding="utf-8"))
    if journal["collection"] != store.cfg.qdrant_collection:
        raise VectorStoreError("有一批入库尚待恢复，请使用原向量库配置重试。")
    state = index_state.load_state(strict=True)
    if journal.get("operation") == "relocate":
        _recover_relocation(store, journal, state)
        index_epoch.bump()
        _clear_journal(path, journal, retire_token)
        return

    committed = all(state.get(rel, {}).get("ingest_generation") == journal["generation"]
                    for rel in journal["paths"])
    if store.collection_exists():
        if committed:
            _activate(store, journal)
            _delete(store, journal["replaced"])
        else:
            _delete(store, journal["new"])
    index_epoch.bump()
    _clear_journal(path, journal, retire_token)


def publish_relocation(store, state, source, target, *, entry, domain, current, status, acl_guard):
    """Move the file and ledger, then retarget all published points in place."""
    token, retire_token = acl_guard
    source_entry = state[source]
    new_id = document_id_for(target)
    points = []
    selector = store.published_filter({"document_id": source_entry.get("document_id") or document_id_for(source)})
    for batch, _ in store._point_pages(selector, fields=["version", "status", "chunk_index", "chunk_id", "path"]):
        for point in batch:
            payload = point.payload
            if payload.get("path") != source:
                raise VectorStoreError("源文档在索引中存在路径冲突，已取消移动。")
            ordinal = chunk_sequence(payload)
            suffix = f"{ordinal:04d}" if ordinal is not None else str(point.id)
            same_edition = (payload.get("version"), payload.get("status")) == (current["version"], current["status"])
            points.append({"id": point.id, "chunk_id": f"{new_id}_v{payload['version']}_{suffix}",
                           "status": status if same_edition else "expired"})
    if not points:
        raise VectorStoreError("原索引中没有可移动的卡片，请重新入库后重试。")

    generation = uuid4().hex
    journal = {"operation": "relocate", "acl_token": token, "collection": store.cfg.qdrant_collection,
               "generation": generation, "knowledge_dir": str(store.cfg.knowledge_dir.resolve()),
               "source": source, "target": target, "domain": domain, "points": points}
    origin, destination = _relocation_paths(store, journal)
    if destination.exists():
        raise VectorStoreError("目标位置已有文件，已取消移动。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if index_state.file_hash(origin) != entry["hash"]:
        raise VectorStoreError("源文件内容已变化，请重试移动。")
    _write_journal(journal)
    try:
        origin.rename(destination)
        state[target] = {**entry, "document_id": new_id, "ingest_generation": generation}
        del state[source]
        index_state.save_state(state, strict=True)
    except BaseException:
        recover_publication(store, retire_token)
        raise
    # After a durable ledger update, replay a failed payload write on the next write operation.
    recover_publication(store, retire_token)


def publish(store, chunks, vectors, state, documents, previous_versions, acl_guard):
    """Stage new points under fresh IDs. Keep historical labels addressable by version."""
    token, retire_token = acl_guard
    generation = uuid4().hex
    replaced = []
    retired = []
    offset = None
    selector = store.published_filter({"document_id": list(documents)})
    while True:
        points, offset = store.client.scroll(
            store.cfg.qdrant_collection, scroll_filter=selector, offset=offset, limit=256,
            with_payload=["document_id", "version", "status"], with_vectors=False)
        for point in points:
            payload = point.payload
            doc_id, version = payload["document_id"], payload.get("version", "")
            rel, new_version = documents[doc_id]
            if previous_versions[rel] is None:
                raise VectorStoreError(f"{rel}：向量库有旧卡片，但缺少入库台账，请先恢复台账。")
            if version == new_version:
                if previous_versions[rel] != version:
                    raise VectorStoreError(f"{rel}：版本 {version} 已存在，请指定新的版本号。")
                replaced.append(point.id)
            elif payload.get("status", "active") != "expired":
                retired.append(point.id)
        if offset is None:
            break

    for chunk in chunks:
        chunk.point_id = str(uuid4())
        chunk.metadata[INGEST_PENDING] = True
    new_ids = [chunk.point_id for chunk in chunks]
    paths = [rel for rel, _ in documents.values()]
    for rel in paths:
        state[rel]["ingest_generation"] = generation
    journal = {"acl_token": token, "collection": store.cfg.qdrant_collection,
               "generation": generation, "paths": paths, "new": new_ids,
               "replaced": replaced, "retired": retired}
    path = _journal_path()
    _write_journal(journal)

    try:
        stored = store.upsert_chunks(chunks, vectors)
        if stored != len(new_ids):
            raise VectorStoreError("新版卡片写入不完整，入库已取消。")
        index_state.save_state(state, strict=True)
    except BaseException:
        recover_publication(store, retire_token)
        raise
    # Until the ledger is durable, readers can only see the previous edition.
    # After commit, replay this switch if Qdrant goes offline during activation.
    _activate(store, journal)
    try:
        _delete(store, replaced)
        _clear_journal(path, journal, retire_token)
    except Exception as exc:
        retire_token(token)
        console_print(f"⚠️ 新版已发布，旧卡片将在下次入库前继续清理：{exc}")
    return stored

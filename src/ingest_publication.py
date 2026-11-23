"""Publish replacement cards with a recovery record spanning Qdrant and the ledger."""

from __future__ import annotations

import json
from uuid import uuid4

from qdrant_client import models

from src import index_epoch, index_state
from src.console import console_print
from src.vector_store import INGEST_PENDING, VectorStoreError


def _journal_path():
    return index_state.STATE_PATH.with_suffix(".publication.json")


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


def recover_publication(store, retire_token):
    """Complete a committed publication, or discard staged points after a failed ledger write."""
    path = _journal_path()
    if not path.exists():
        return
    journal = json.loads(path.read_text(encoding="utf-8"))
    if journal["collection"] != store.cfg.qdrant_collection:
        raise VectorStoreError("有一批入库尚待恢复，请使用原向量库配置重试。")
    state = index_state.load_state(strict=True)
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
    tmp_path = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_text(json.dumps(journal, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)

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

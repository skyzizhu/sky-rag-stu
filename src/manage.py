"""知识库管理操作：重新入库 / 归档 / 恢复 / 批量移除。

产品视角：这是「知识管理台」的后台——
- 归档：把文件移进 knowledge/archive/，它自动变为 status=archive，退出日常检索
- 恢复：把归档文件移回原目录，重新变为 active
- 移除知识：从向量库删除，磁盘文件保留，可随时重新入库
- 删除文件：从向量库删除并删除磁盘文件；二级目录无文件时一并清理
"""

from __future__ import annotations

import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from src.config import AppConfig, get_config
from src.document_department import normalize_department
from src.document_type import normalize_doc_type
from src.ingest_audit import IngestAudit
from src.metadata import document_id_for
from src.pipeline import IngestSummary, ingest_files
from src.vector_store import get_vector_store


class ManageError(Exception):
    """管理操作失败时抛出，信息直接给人看。"""


def _resolve(relative_path: str, cfg: AppConfig | None = None) -> tuple[Path, Path, AppConfig]:
    """把相对路径解析成 (绝对路径, 知识目录, 配置)，并检查文件存在。"""
    cfg = cfg or get_config()
    knowledge_dir = cfg.knowledge_dir.resolve()
    path = (knowledge_dir / relative_path).resolve()
    try:
        path.relative_to(knowledge_dir)
    except ValueError as exc:
        raise ManageError(f"非法知识文件路径：{relative_path}") from exc
    if not path.is_file():
        raise ManageError(f"文件不在磁盘上：{relative_path}（可能已被移动或删除）")
    return path, knowledge_dir, cfg


def _safe_knowledge_path(relative_path: str, cfg: AppConfig) -> Path:
    """解析知识目录内路径但不要求文件存在，供仅删除向量的操作使用。"""
    knowledge_dir = cfg.knowledge_dir.resolve()
    path = (knowledge_dir / relative_path).resolve()
    try:
        path.relative_to(knowledge_dir)
    except ValueError as exc:
        raise ManageError(f"非法知识文件路径：{relative_path}") from exc
    return path


def _second_level_directory(relative_path: str, knowledge_dir: Path) -> Path | None:
    """返回领域下的第一级子目录。领域根目录的文件不触发目录清理。"""
    parts = Path(relative_path.replace("\\", "/")).parts
    if len(parts) < 3 or parts[0] in ("", ".", "..") or parts[1] in ("", ".", ".."):
        return None
    directory = (knowledge_dir / parts[0] / parts[1]).resolve()
    try:
        directory.relative_to(knowledge_dir.resolve())
    except ValueError:
        return None
    return directory


def _has_user_files(directory: Path) -> bool:
    """目录内是否仍有用户文件；.DS_Store/.gitkeep 等隐藏占位文件不计入。"""
    return directory.exists() and any(
        path.is_file() and not path.name.startswith(".")
        for path in directory.rglob("*")
    )


def _visibility_snapshot(state, relative_path, store) -> dict:
    from src import index_state
    from src.permissions import normalize_visibility

    info = index_state.visibility_info_for(state, relative_path)
    if info:
        return info
    for doc in store.list_documents({"document_id": document_id_for(relative_path)}):
        if doc["status"] in ("active", "archive"):
            acl = normalize_visibility(doc.get("allowed_groups"), doc.get("allowed_users"),
                                       legacy_default=False)
            return {"manual": acl, "value": acl}
    return {}


@contextmanager
def _document_write():
    from src.auth_db import document_acl_write
    from src.ingest_publication import publication_owns_token, recover_publication
    from src.pipeline import _ingest_lock

    with _ingest_lock, document_acl_write(retain_if=publication_owns_token) as guard:
        recover_publication(get_vector_store(), guard[1])
        yield guard


def revoke_document_user(username: str) -> int:
    """移除所有版本中的个人授权，支持停用后重试；空名单继续封闭。"""
    from qdrant_client import models
    from src import index_epoch, index_state
    from src.keyword_search import invalidate_cache
    from src.permissions import normalize_visibility
    from src.pipeline import clear_qa_cache

    def remove(acl: dict) -> bool:
        users = acl.get("allowed_users") or []
        if username not in users:
            return False
        acl.update(normalize_visibility(acl.get("allowed_groups"),
                                        [user for user in users if user != username],
                                        legacy_default=False))
        return True

    changed_documents = set()
    with _document_write():
        state = index_state.load_state(strict=True)
        changed_ledger = False
        for entry in state.values():
            info = entry.get("visibility_info") or {}
            for key in ("manual", "upload_default", "value"):
                changed_ledger = remove(info.get(key) or {}) or changed_ledger
            changed_ledger = remove(entry.get("overrides") or {}) or changed_ledger
            if username in info.get("granted_revocations", {}):
                del info["granted_revocations"][username]
                changed_ledger = True
        if changed_ledger:
            index_state.save_state(state, strict=True)

        store = get_vector_store()
        target = models.FieldCondition(key="allowed_users", match=models.MatchValue(value=username))
        if not store.collection_exists():
            return 0
        store.ensure_allowed_users_index()
        try:
            for doc in store.list_documents({"allowed_users": [username]}):
                remove(doc)
                fields = {"document_id": doc["document_id"], "status": doc["status"], "version": doc["version"]}
                selector = models.Filter(must=[target, *[
                    models.FieldCondition(key=key, match=models.MatchValue(value=value))
                    for key, value in fields.items()
                ]])
                changed_documents.add(doc["document_id"])
                store.client.set_payload(
                    collection_name=store.cfg.qdrant_collection,
                    payload={key: doc[key] for key in ("allowed_groups", "allowed_users", "classification")},
                    points=models.FilterSelector(filter=selector), wait=True,
                )
        finally:
            if changed_documents:
                index_epoch.bump()
                invalidate_cache()
                clear_qa_cache()
    return len(changed_documents)


def reingest(relative_path: str, owner: str | None = None) -> dict:
    """重新解析并入库单个文件（文件修改后手动刷新用）。owner：操作者（审计/溯源）。"""
    path, _, cfg = _resolve(relative_path)
    summary = ingest_files(paths=[path], owner=owner)
    if summary.ok_files == 0:
        raise ManageError(f"重新入库失败：{summary.failed_files}")
    return {"message": f"已重新入库：{relative_path}（{summary.total_chunks} 张卡片）", "summary": summary}


def _move_destination(relative_path: str, to_archive: bool, entry: dict) -> str:
    if to_archive:
        if relative_path.startswith("archive/"):
            raise ManageError("该文件已经在归档目录里了。")
        return f"archive/{relative_path}"
    parts = relative_path.split("/")
    if len(parts) < 2 or parts[0] != "archive":
        raise ManageError("该文件不在归档目录里。")
    original = entry.get("archive_origin", {}).get("path")
    if not original:
        original = f"reference/documents/{parts[1]}" if len(parts) == 2 else "/".join(parts[1:])
    if "/" in original:
        from src.domains import all_codes

        domain = original.split("/", 1)[0].lower()
        if domain == "archive" or domain not in all_codes():
            raise ManageError(f"无法恢复到领域「{domain}」，原领域已不存在或不可恢复。")
    return original


def _available_move_path(target: Path, root: Path, state, store) -> str:
    stamp = None
    candidate = target
    attempt = 0
    while True:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root) or resolved != candidate:
            raise ManageError("目标路径不可用：文档移动必须保留在知识目录中。")
        relative = candidate.relative_to(root).as_posix()
        if (relative not in state and not candidate.exists()
                and not store.list_documents({"document_id": document_id_for(relative)})):
            return relative
        if stamp is None:
            stamp = time.strftime("%Y%m%d_%H%M%S")
        attempt += 1
        suffix = stamp if attempt == 1 else f"{stamp}_{attempt}"
        candidate = target.with_name(f"{target.stem}_{suffix}{target.suffix}")


def _relocate(relative_path: str, *, to_archive: bool, owner: str | None) -> dict:
    from src import index_state
    from src.domains import all_codes
    from src.ingest_publication import publish_relocation
    from src.keyword_search import invalidate_cache
    from src.metadata import infer_from_path
    from src.pipeline import clear_qa_cache

    status = "archive" if to_archive else "active"
    action = "归档" if to_archive else "恢复"
    for allow_ingest in (True, False):
        with _document_write() as guard:
            path, root, cfg = _resolve(relative_path)
            source = path.relative_to(root).as_posix()
            state = index_state.load_state(strict=True)
            entry = state.get(source, {})
            destination = _move_destination(source, to_archive, entry)
            target = _safe_knowledge_path(destination, cfg)
            if target.relative_to(root).as_posix() != destination or source == destination:
                raise ManageError("目标路径不可用：请检查知识目录中的链接。")
            store = get_vector_store()
            doc_id = entry.get("document_id") or document_id_for(source)
            old_status = entry.get("indexed_status") or infer_from_path(source)["status"]
            current = next((row for row in store.list_documents({"document_id": doc_id})
                            if row["path"] == source and row["version"] == entry.get("version")
                            and row["status"] == old_status), None)
            if current and current["chunks"] == entry.get("chunks") and entry.get("hash") == index_state.file_hash(path):
                target_rel = _available_move_path(target, root, state, store)
                updated = {**entry, "indexed_status": status,
                           "overrides": {**entry.get("overrides", {}), "status": status}}
                visibility = _visibility_snapshot(state, source, store)
                if visibility:
                    updated["visibility_info"] = visibility
                if to_archive:
                    updated["archive_origin"] = {"path": source, "domain": current["domain"]}
                    domain = "archive"
                else:
                    original_domain = updated.pop("archive_origin", {}).get("domain")
                    domain = (original_domain if original_domain in all_codes() and original_domain != "archive"
                              else infer_from_path(target_rel)["domain"])
                publish_relocation(store, state, source, target_rel, entry=updated, domain=domain,
                                   current=current, status=status, acl_guard=guard)
                invalidate_cache()
                clear_qa_cache()
                row = {**current, "path": target_rel, "document_id": document_id_for(target_rel),
                       "source": Path(target_rel).name, "domain": domain, "status": status}
                summary = IngestSummary(total_files=1, ok_files=1, total_chunks=row["chunks"], file_rows=[row])
                return {"message": f"已{action}：{source} → {target_rel}", "summary": summary}
        # Unindexed edits must be published to the original version chain before the move.
        if allow_ingest:
            reingest(source, owner=owner)
    raise ManageError(f"{action}期间文件或索引已变化，请刷新后重试。")


def archive(relative_path: str, owner: str | None = None) -> dict:
    """归档文件及其版本链。"""
    return _relocate(relative_path, to_archive=True, owner=owner)


def restore(archived_relative_path: str, owner: str | None = None) -> dict:
    """将文件和版本链恢复到原目录；旧归档没有原路径时沿用目录约定。"""
    return _relocate(archived_relative_path, to_archive=False, owner=owner)


def confirm_document_version(relative_path: str, value: str, revision: str,
                             expected_hash: str) -> dict:
    """显式确认当前入库文件的正式版本，不改源文件与历史修订。"""
    with _document_write():
        return _confirm_document_version(relative_path, value, revision, expected_hash)


def _confirm_document_version(relative_path: str, value: str, revision: str,
                              expected_hash: str) -> dict:
    from src import index_state as istate, index_epoch
    from src.document_version import normalize_version, apply_confirmation
    from src.keyword_search import invalidate_cache
    from src.pipeline import clear_qa_cache
    from qdrant_client import models

    normalized = normalize_version(value)
    if not normalized:
        raise ManageError("请填写明确的文档版本，例如 1.2 或 V1.2")
    path, _, cfg = _resolve(relative_path)
    state = istate.load_state(strict=True)
    entry = state.get(relative_path)
    if not entry or entry.get("version") != revision:
        raise ManageError("当前入库版本已变化，请刷新文件列表后重新确认")
    if not expected_hash or entry.get("hash") != expected_hash:
        raise ManageError("文件内容代次已变化，请刷新档案后重新确认")
    current_hash = istate.file_hash(path)
    if entry.get("hash") != current_hash:
        raise ManageError("文件内容已变化，请先重新入库再确认文档版本")
    decision = {"value": normalized, "hash": current_hash, "revision": revision}
    metadata = apply_confirmation(entry.get("version_info", {}), decision, current_hash, revision)
    store = get_vector_store()
    selector = models.Filter(must=[
        models.FieldCondition(key="document_id", match=models.MatchValue(value=entry["document_id"])),
        models.FieldCondition(key="version", match=models.MatchValue(value=revision)),
        models.FieldCondition(key="version_content_hash", match=models.MatchValue(value=expected_hash)),
        models.FieldCondition(key="status", match=models.MatchAny(any=["active", "archive"])),
    ])
    if not store.collection_exists() or store.client.count(
            collection_name=cfg.qdrant_collection, count_filter=selector, exact=True).count == 0:
        raise ManageError("没有匹配的当前入库卡片，请重新入库或刷新档案后确认")
    store.client.set_payload(
        collection_name=cfg.qdrant_collection,
        payload={key: metadata[key] for key in
                 ("document_version", "document_version_status", "document_version_source")},
        points=models.FilterSelector(filter=selector), wait=True,
    )
    entry["version_confirmation"] = decision
    entry["version_info"] = metadata
    istate.save_state(state, strict=True)
    index_epoch.bump()
    invalidate_cache()
    clear_qa_cache()
    return {"message": f"已确认文档版本：{normalized}"}


def update_metadata(relative_path: str, updates: dict) -> dict:
    """保存档案，与入库串行写入台账。"""
    with _document_write():
        return _update_metadata(relative_path, updates)


def _update_metadata(relative_path: str, updates: dict) -> dict:
    """编辑文档档案：把可编辑字段写入该文档的全部知识卡片，并记入台账覆盖设置。

    可编辑字段：title / category / topic / tags / version / status / doc_type /
    department / project_id / allowed_groups / allowed_users。
    覆盖设置存入入库台账，重新入库时依然生效；磁盘文件不做改动。
    doc_type / department 的空值是显式清除，与未编辑该字段不同。
    department 是归属部门档案字段（与 allowed_groups 可见权限无关）。
    """
    path, _, cfg = _resolve(relative_path)
    allowed = {"title", "category", "topic", "tags", "version", "status"}
    clean: dict = {}
    for key, value in (updates or {}).items():
        if key in ('allowed_groups', 'allowed_users'):
            clean[key] = value
            continue
        if key == "doc_type":
            clean[key] = normalize_doc_type(value)
            continue
        if key == "department":
            clean[key] = normalize_department(value)
            continue
        if key == "project_id":
            from src.projects import validate_project_id

            clean[key] = validate_project_id(value)
            continue
        if key not in allowed or value in (None, "", "all"):
            continue
        if key in ("topic", "tags"):
            value = [v.strip() for v in (value if isinstance(value, list) else [value]) if str(v).strip()]
            if not value:
                continue
        else:
            value = str(value).strip()
            if not value:
                continue
        clean[key] = value

    document_id = document_id_for(relative_path)
    from src import index_state as istate

    state = istate.load_state(strict=True)
    entry = state.setdefault(relative_path, {})
    acl_info = None
    if 'allowed_groups' in clean or 'allowed_users' in clean:
        from src.permissions import normalize_visibility
        from src.auth_db import document_user_revocations, get_user, normalize_department_groups
        old_info = _visibility_snapshot(state, relative_path, get_vector_store())
        existing = old_info.get('value') or old_info.get('manual') or {}
        acl = normalize_visibility(clean.get('allowed_groups', existing.get('allowed_groups')),
                                   clean.get('allowed_users', existing.get('allowed_users')),
                                   legacy_default=False)
        acl['allowed_groups'] = normalize_department_groups(acl['allowed_groups'])
        if any((get_user(u) or {}).get('status') != 'active' for u in acl['allowed_users']):
            raise ManageError('授权名单只能包含有效员工')
        clean.update(acl)
        revocations = document_user_revocations()
        acl_info = dict(entry.get('visibility_info', {}))
        acl_info.update(manual=dict(acl), value=dict(acl),
                        granted_revocations={u: revocations.get(u) for u in acl['allowed_users']})
    revision_changed = "version" in clean and clean["version"] != entry.get("version")
    payload = dict(clean)
    if revision_changed:
        from src.document_version import FIELDS
        detected = entry.get("version_detection", {})
        payload.update({key: detected.get(key, [] if key == "document_version_evidence" else "")
                        for key in FIELDS})
        payload["document_version_status"] = detected.get("document_version_status", "unavailable")
    store = get_vector_store()
    if store.collection_exists():
        from qdrant_client import models
        if clean.get("project_id") is not None:
            store.ensure_project_index()
        if acl_info is not None and acl_info['value']['allowed_users']:
            store.ensure_allowed_users_index()
        conditions = [models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id))]
        if revision_changed:
            if store.list_documents({"document_id": document_id, "version": clean["version"]}):
                raise ManageError("该版本号已在历史记录中使用，请选择其他版本号")
            conditions.extend([
                models.FieldCondition(key="version", match=models.MatchValue(value=entry.get("version", ""))),
                models.FieldCondition(key="status", match=models.MatchAny(any=["active", "archive"])),
            ])
        selector = models.Filter(must=conditions)
        if revision_changed and store.client.count(
                collection_name=cfg.qdrant_collection, count_filter=selector, exact=True).count == 0:
            raise ManageError("没有匹配的当前入库版本，请刷新档案后重试")
        if revision_changed and acl_info is not None:
            # 版本编辑只更新生效版本；权限仍要覆盖历史版本。
            store.client.set_payload(
                collection_name=cfg.qdrant_collection, payload=acl_info["value"],
                points=models.FilterSelector(filter=models.Filter(must=conditions[:1])), wait=True,
            )
        store.client.set_payload(
            collection_name=cfg.qdrant_collection,
            payload=payload,
            points=models.FilterSelector(filter=selector),
            wait=True,
        )
        # 只改元数据、卡片数不变：必须显式作废检索缓存。漏掉的后果——收窄可见部门后，
        # 其他部门仍能经 BM25 通道检索到该文档（权限问题）；改分类后按分类过滤结果不变。
        # 纪元通知所有进程（含 MCP），本进程再立即清一次。
        from src import index_epoch
        from src.keyword_search import invalidate_cache
        from src.pipeline import clear_qa_cache

        index_epoch.bump()
        invalidate_cache()
        clear_qa_cache()

    # 覆盖设置记入台账：重新入库时由 pipeline 优先应用（优先级高于 Front Matter 与目录）
    if revision_changed:
        entry["version"] = clean["version"]
        entry.pop("version_confirmation", None)
        entry["version_info"] = {key: payload[key] for key in FIELDS}
    overrides = entry.get("overrides", {})
    for key, value in clean.items():
        if key == "doc_type":
            info = dict(entry.get("doc_type_info", {}))
            info.update(manual=value, value=value, source="manual")
            entry["doc_type_info"] = info
        elif key == "department":
            info = dict(entry.get("department_info", {}))
            info.update(manual=value, value=value, source="manual")
            entry["department_info"] = info
        elif key == "project_id":
            entry["project_id"] = value
        elif value:
            overrides[key] = value
        else:
            overrides.pop(key, None)
    entry["overrides"] = overrides
    if "status" in clean:
        entry["indexed_status"] = clean["status"]
    if acl_info is not None:
        entry['visibility_info'] = acl_info
        entry['overrides'].update(acl_info['value'])
    entry["document_id"] = document_id
    istate.save_state(state, strict=True)
    return {"message": f"已更新档案：{relative_path}（{len(clean)} 个字段）"}


def remove_documents(relative_paths: list[str], delete_files: bool = False) -> dict:
    """批量移除知识；可选同时删除磁盘文件并清理空的二级目录。

    所有路径会在执行前完成安全校验。索引移除后清掉指纹，保留磁盘文件的内部或受限授权，
    以免重入库时默认成为公开文件。
    """
    with _document_write():
        return _remove_documents(relative_paths, delete_files)


def _remove_documents(relative_paths: list[str], delete_files: bool) -> dict:
    paths = list(dict.fromkeys(str(path).strip() for path in relative_paths if str(path).strip()))
    if not paths:
        raise ManageError("没有选择需要移除的知识文件。")

    cfg = get_config()
    knowledge_dir = cfg.knowledge_dir.resolve()
    resolved = {relative_path: _safe_knowledge_path(relative_path, cfg) for relative_path in paths}
    if delete_files:
        missing = [relative_path for relative_path, path in resolved.items() if not path.is_file()]
        if missing:
            preview = "、".join(missing[:3])
            suffix = f" 等 {len(missing)} 个文件" if len(missing) > 3 else ""
            raise ManageError(f"磁盘文件不存在，已取消本次操作：{preview}{suffix}")

    store = get_vector_store()
    document_ids = [document_id_for(relative_path) for relative_path in paths]
    from src import index_state as istate

    state = istate.load_state(strict=True)
    visibility = {relative_path: _visibility_snapshot(state, relative_path, store)
                  for relative_path in paths}
    store.delete_documents(document_ids)
    for relative_path in paths:
        istate.remove_entry(state, relative_path)
        info = visibility[relative_path]
        acl = info.get("value") or info.get("manual") or {}
        if resolved[relative_path].is_file() and acl.get("classification") in ("内部", "受限"):
            state[relative_path] = {"visibility_info": info}
    istate.save_state(state, strict=True)

    deleted_files: list[str] = []
    cleaned_directories: list[str] = []
    if delete_files:
        candidate_directories = {
            directory
            for relative_path in paths
            if (directory := _second_level_directory(relative_path, knowledge_dir)) is not None
        }
        for relative_path, path in resolved.items():
            try:
                path.unlink()
            except OSError as exc:
                raise ManageError(
                    f"向量知识已移除，但磁盘文件删除失败：{relative_path}（{exc}）"
                ) from exc
            deleted_files.append(relative_path)
            istate.remove_entry(state, relative_path)
        istate.save_state(state, strict=True)

        # 只清理领域下的空子目录，领域目录保留。
        for directory in sorted(candidate_directories, key=lambda path: len(path.parts), reverse=True):
            if directory.is_dir() and not _has_user_files(directory):
                try:
                    shutil.rmtree(directory)
                except OSError as exc:
                    raise ManageError(
                        f"文件已删除，但空二级目录清理失败："
                        f"{directory.relative_to(knowledge_dir).as_posix()}（{exc}）"
                    ) from exc
                cleaned_directories.append(directory.relative_to(knowledge_dir).as_posix())

    count = len(paths)
    if delete_files:
        message = f"已从知识库移除并删除 {count} 个磁盘文件"
        if cleaned_directories:
            message += f"；已清理 {len(cleaned_directories)} 个空二级目录"
    else:
        message = f"已从向量知识库移除 {count} 个文件（磁盘文件保留，可重新入库）"
    return {
        "message": message,
        "removed": paths,
        "deleted_files": deleted_files,
        "cleaned_directories": cleaned_directories,
    }


def remove_from_index(relative_path: str) -> dict:
    """只从向量库移除单个文档；兼容原有调用。"""
    return remove_documents([relative_path], delete_files=False)


def remove_from_index_and_disk(relative_path: str) -> dict:
    """从向量库移除单个文档并删除磁盘文件；兼容单文件调用。"""
    return remove_documents([relative_path], delete_files=True)


def ingest_web_page(url: str, domain: str = "reference",
                    category: str | None = None, on_progress=None,
                    default_groups: list[str] | None = None,
                    snapshot_path: Path | None = None,
                    owner: str | None = None, *,
                    default_doc_type: str | None = None,
                    default_department: str | None = None,
                    default_project_id: int | None = None,
                    default_allowed_users: list[str] | None = None,
                    blocked_paths: frozenset[Path] = frozenset(),
                    audit: IngestAudit | None = None) -> dict:
    """抓取网页内容并入库为知识卡片（V3 插件：不改核心 RAG 流程）。

    流程：抓取 URL → 提取正文 → 保存到所选领域（已有快照复用原路径）→ 标准入库。
    category 仅供旧调用和历史快照兼容。
    on_progress(msg)：每个阶段开始时回调一次（界面用它显示分步进度）。
    default_groups（V3.7）：上传表单选择的可见部门，透传给入库流水线。
    default_department：非空上传默认归属部门（档案字段，与权限无关）。
    """
    from src.domains import enabled_codes
    from src.project_directories import other_project_paths, upload_directory
    from src.web_fetcher import fetch_and_parse, save_web_snapshot
    from src import index_state as istate
    if default_doc_type is not None:
        default_doc_type = normalize_doc_type(default_doc_type)
    if default_department is not None:
        default_department = normalize_department(default_department)
    if default_project_id is not None:
        from src.projects import validate_project_id

        validate_project_id(default_project_id)
    # 在快照覆写前保存类型与归属部门声明；其余源文件字段仍按抓取结果重新生成。
    type_carry = {}
    department_carry = {}
    visibility_carry = {}
    if snapshot_path is not None:
        rel = Path(snapshot_path).resolve().relative_to(get_config().knowledge_dir.resolve()).as_posix()
        state = istate.load_state()
        type_carry[rel] = istate.snapshot_doc_type(state, rel)
        department_carry[rel] = istate.snapshot_department(state, rel)
        visibility_carry[rel] = _visibility_snapshot(state, rel, get_vector_store())

    # archive 的卡片必须是 archive 状态，而状态由所在目录决定：快照不在 archive/ 下就会
    # 变成「归档领域里的生效卡片」，所以网页不能直接进归档领域（要归档走知识库管理页）
    if domain == "archive" or domain not in enabled_codes():
        raise ManageError(f"网页不能入库到领域「{domain}」（领域不存在、已停用或是归档领域）。")

    def _report(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    _report("fetch")
    if audit:
        audit.stage = "fetch"
    result = fetch_and_parse(url)
    _report("save")
    if audit:
        audit.stage = "save"
    knowledge_dir = get_config().knowledge_dir
    if snapshot_path is not None:
        file_path = save_web_snapshot(url, result["text"], result.get("title", ""),
                                      domain=domain, category=category, knowledge_dir=knowledge_dir,
                                      target=snapshot_path, blocked_paths=blocked_paths)
    else:
        state = istate.load_state()
        blocked_paths = blocked_paths | other_project_paths(state, default_project_id)
        with upload_directory(knowledge_dir, domain, default_project_id) as destination:
            existing_paths = tuple(knowledge_dir / rel for rel, entry in state.items()
                                   if entry.get("project_id") == default_project_id
                                   and not rel.startswith("archive/")
                                   and (default_project_id is None or rel.startswith(f"{domain}/")))
            file_path = save_web_snapshot(url, result["text"], result.get("title", ""),
                                          domain=domain, category=category, knowledge_dir=knowledge_dir,
                                          blocked_paths=blocked_paths, destination=destination,
                                          existing_paths=existing_paths, include_legacy=default_project_id is None)
    if audit:
        audit.saved(0, file_path)
        audit.stage = "index"

    _report("ingest")
    # 入库（走标准流水线）：skip_unchanged 让同 URL 重复抓取时走增量判断，
    # 网页内容没变就跳过（省 LLM 标签、切片和向量化）
    with istate.carry_doc_types(type_carry, department_values=department_carry,
                               visibility_values=visibility_carry):
        summary = ingest_files(paths=[file_path], skip_unchanged=True,
                               default_groups=default_groups, owner=owner,
                               default_doc_type=default_doc_type,
                               default_department=default_department,
                               default_project_id=default_project_id,
                               default_allowed_users=default_allowed_users)
    if audit:
        audit.finish(summary)
    if summary.skipped_files and not summary.ok_files:
        return {
            "message": f"网页内容未变化，已跳过入库：{url}（增量判断命中，省时省钱）",
            "url": url,
            "file_path": str(file_path),
            "chunks": 0,
            "skipped": True,
        }
    if summary.ok_files:
        message = f"网页已入库：{url} → {file_path.name}（{summary.total_chunks} 张卡片）"
        if result.get("cert_skipped"):
            message += "\n⚠️ 注意：该网站 SSL 证书校验失败（常见于证书链不完整的站点），已跳过校验抓取。"
        return {
            "message": message,
            "url": url,
            "file_path": str(file_path),
            "chunks": summary.total_chunks,
            "cert_skipped": result.get("cert_skipped", False),
        }
    raise ManageError(f"网页入库失败：{summary.failed_files}")


def refetch_web_page(relative_path: str, owner: str | None = None) -> dict:
    """重新抓取网页：按快照文件头里的原文网址重抓并写回原快照，内容变了生成新版本、没变就跳过。

    沿用快照的领域和历史文件头声明，授权从台账或已有卡片携带。
    """
    path, knowledge_dir, _ = _resolve(relative_path)
    relative_path = path.relative_to(knowledge_dir).as_posix()
    if relative_path.split("/")[0] == "archive":
        raise ManageError("已归档的网页请先恢复，再重新抓取。")
    from src.web_fetcher import read_snapshot_origin

    origin = read_snapshot_origin(path)
    if origin is None:
        raise ManageError("这个文件不是网页快照（文件头里没有原文网址）。")
    try:
        return ingest_web_page(origin["url"], domain=origin["domain"], category=origin["category"],
                               snapshot_path=path, owner=owner)
    except ManageError:
        raise
    except Exception as exc:  # 网络错误、网站返回异常等：给出可读的提示，不让页面崩溃
        raise ManageError(f"重新抓取失败：{exc}") from exc

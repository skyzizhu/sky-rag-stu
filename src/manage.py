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
from pathlib import Path

from src.config import AppConfig, get_config
from src.document_type import normalize_doc_type
from src.metadata import document_id_for
from src.pipeline import ingest_files
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
    """返回 domain/category 二级目录；根目录或一级目录文件不触发目录清理。"""
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


def reingest(relative_path: str, owner: str | None = None) -> dict:
    """重新解析并入库单个文件（文件修改后手动刷新用）。owner：操作者（审计/溯源）。"""
    path, _, cfg = _resolve(relative_path)
    summary = ingest_files(paths=[path], owner=owner)
    if summary.ok_files == 0:
        raise ManageError(f"重新入库失败：{summary.failed_files}")
    return {"message": f"已重新入库：{relative_path}（{summary.total_chunks} 张卡片）", "summary": summary}


def archive(relative_path: str, owner: str | None = None) -> dict:
    """归档：把文件移到 knowledge/archive/ 下（保留原子目录结构），并刷新向量库。"""
    path, knowledge_dir, cfg = _resolve(relative_path)
    if relative_path.split("/")[0] == "archive":
        raise ManageError("该文件已经在归档目录里了。")

    dest = knowledge_dir / "archive" / relative_path
    if dest.exists():  # 同名冲突：加时间戳
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dest = dest.with_name(f"{dest.stem}_{stamp}{dest.suffix}")
    from src import index_state as istate
    type_info = istate.snapshot_doc_type(istate.load_state(), relative_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest))

    # 文件换路径 = 换 document_id：先删旧知识，再按新路径入库（新路径自动 status=archive）
    from src.metadata import document_id_for

    store = get_vector_store()
    store.delete_documents([document_id_for(relative_path)])
    with istate.carry_doc_types({dest.relative_to(knowledge_dir).as_posix(): type_info}):
        summary = ingest_files(paths=[dest], owner=owner)
    if summary.ok_files == 0:
        raise ManageError(f"归档后重新入库失败：{summary.failed_files}")
    return {"message": f"已归档：{relative_path} → archive/{relative_path}", "summary": summary}


def restore(archived_relative_path: str, owner: str | None = None) -> dict:
    """恢复：把归档文件移回原目录；原本就散落在归档根目录的，移到 reference/documents/。"""
    path, knowledge_dir, cfg = _resolve(archived_relative_path)
    parts = archived_relative_path.split("/")
    if parts[0] != "archive" or len(parts) < 2:
        raise ManageError("该文件不在归档目录里。")

    original = "/".join(parts[1:])
    if len(parts) == 2:  # 直接放在 archive/ 根目录的文件，没有原始目录可回
        target = knowledge_dir / "reference" / "documents" / parts[1]
    else:
        # V3.8：原领域必须仍已登记——否则文件落进未登记目录，入库时会被悄悄兜底归到 reference
        from src.domains import all_codes

        if parts[1].lower() not in all_codes():
            raise ManageError(f"原领域「{parts[1]}」已不存在（可能已在领域管理中删除），"
                              "请先在领域管理中重新创建该领域再恢复。")
        target = knowledge_dir / original
    # 复检目标仍在 knowledge/ 内：构造的 archive/../x 路径不能把文件移出知识库
    try:
        target.resolve().relative_to(knowledge_dir.resolve())
    except (ValueError, OSError):
        raise ManageError("恢复目标路径非法。")
    if target.exists():  # 同名冲突：加时间戳
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = target.with_name(f"{target.stem}_{stamp}{target.suffix}")
    from src import index_state as istate
    type_info = istate.snapshot_doc_type(istate.load_state(), archived_relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(target))

    from src.metadata import document_id_for

    store = get_vector_store()
    store.delete_documents([document_id_for(archived_relative_path)])
    with istate.carry_doc_types({target.relative_to(knowledge_dir).as_posix(): type_info}):
        summary = ingest_files(paths=[target], owner=owner)
    if summary.ok_files == 0:
        raise ManageError(f"恢复后重新入库失败：{summary.failed_files}")
    return {"message": f"已恢复：{archived_relative_path} → {target.relative_to(knowledge_dir).as_posix()}",
            "summary": summary}


def confirm_document_version(relative_path: str, value: str, revision: str,
                             expected_hash: str) -> dict:
    """显式确认当前入库文件的正式版本，不改源文件与历史修订。"""
    from src import index_state as istate, index_epoch
    from src.document_version import normalize_version, apply_confirmation
    from src.keyword_search import invalidate_cache
    from src.pipeline import clear_qa_cache
    from qdrant_client import models

    normalized = normalize_version(value)
    if not normalized:
        raise ManageError("请填写明确的文档版本，例如 1.2 或 V1.2")
    path, _, cfg = _resolve(relative_path)
    state = istate.load_state()
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
    istate.save_state(state)
    index_epoch.bump()
    invalidate_cache()
    clear_qa_cache()
    return {"message": f"已确认文档版本：{normalized}"}


def update_metadata(relative_path: str, updates: dict) -> dict:
    """编辑文档档案：把可编辑字段写入该文档的全部知识卡片，并记入台账覆盖设置。

    可编辑字段：title / category / topic / tags / version / status / allowed_groups / doc_type。
    覆盖设置存入入库台账，重新入库时依然生效；磁盘文件不做改动。
    doc_type 的空值是显式清除，与未编辑该字段不同。
    """
    path, _, cfg = _resolve(relative_path)
    # V3.7：allowed_groups（可见部门列表）可编辑；空列表规范化为 ["all"]（全员可见）
    allowed = {"title", "category", "topic", "tags", "version", "status", "allowed_groups"}
    clean: dict = {}
    for key, value in (updates or {}).items():
        if key == "doc_type":
            clean[key] = normalize_doc_type(value)
            continue
        if key not in allowed or value in (None, "", "all"):
            continue
        if key in ("topic", "tags"):
            value = [v.strip() for v in (value if isinstance(value, list) else [value]) if str(v).strip()]
            if not value:
                continue
        elif key == "allowed_groups":
            value = [v.strip() for v in (value if isinstance(value, list) else [value]) if str(v).strip()]
            value = value or ["all"]  # 空列表 = 全员可见，保证 payload 永不为空列表
            from src.auth_db import normalize_department_groups
            value = normalize_department_groups(value)
        else:
            value = str(value).strip()
            if not value:
                continue
        clean[key] = value

    document_id = document_id_for(relative_path)
    from src import index_state as istate

    state = istate.load_state()
    entry = state.setdefault(relative_path, {})
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
        conditions = [models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id))]
        if revision_changed:
            conditions.extend([
                models.FieldCondition(key="version", match=models.MatchValue(value=entry.get("version", ""))),
                models.FieldCondition(key="status", match=models.MatchAny(any=["active", "archive"])),
            ])
        selector = models.Filter(must=conditions)
        if revision_changed and store.client.count(
                collection_name=cfg.qdrant_collection, count_filter=selector, exact=True).count == 0:
            raise ManageError("没有匹配的当前入库版本，请刷新档案后重试")
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
        elif value:
            overrides[key] = value
        else:
            overrides.pop(key, None)
    entry["overrides"] = overrides
    entry["document_id"] = document_id
    istate.save_state(state)
    return {"message": f"已更新档案：{relative_path}（{len(clean)} 个字段）"}


def remove_documents(relative_paths: list[str], delete_files: bool = False) -> dict:
    """批量移除知识；可选同时删除磁盘文件并清理空的二级目录。

    所有路径会在执行前完成安全校验。无论是否删除磁盘文件，都同步移除增量
    入库台账记录，确保保留在磁盘上的文件之后可以正常重新入库。
    """
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
    store.delete_documents(document_ids)

    from src import index_state as istate

    state = istate.load_state()
    for relative_path in paths:
        istate.remove_entry(state, relative_path)
    istate.save_state(state)

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

        # 只删除 domain/category；一级领域目录永远保留。
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
                    category: str = "webpages", on_progress=None,
                    default_groups: list[str] | None = None,
                    snapshot_path: Path | None = None,
                    owner: str | None = None, *,
                    default_doc_type: str | None = None) -> dict:
    """抓取网页内容并入库为知识卡片（V3 插件：不改核心 RAG 流程）。

    流程：fetch URL → 提取正文 → 保存为 .md 快照（领域 / 分类写进文件头）→ 标准入库
    on_progress(msg)：每个阶段开始时回调一次（界面用它显示分步进度）。
    default_groups（V3.7）：上传表单选择的可见部门，透传给入库流水线。
    """
    from src.domains import enabled_codes
    from src.web_fetcher import fetch_and_parse, save_web_snapshot
    from src import index_state as istate
    if default_doc_type is not None:
        default_doc_type = normalize_doc_type(default_doc_type)
    # 在快照覆写前保存类型声明；其余源文件字段仍按抓取结果重新生成。
    type_carry = {}
    if snapshot_path is not None:
        rel = Path(snapshot_path).resolve().relative_to(get_config().knowledge_dir.resolve()).as_posix()
        type_carry[rel] = istate.snapshot_doc_type(istate.load_state(), rel)

    # archive 的卡片必须是 archive 状态，而状态由所在目录决定：快照不在 archive/ 下就会
    # 变成「归档领域里的生效卡片」，所以网页不能直接进归档领域（要归档走知识库管理页）
    if domain == "archive" or domain not in enabled_codes():
        raise ManageError(f"网页不能入库到领域「{domain}」（领域不存在、已停用或是归档领域）。")

    def _report(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    _report("fetch")
    result = fetch_and_parse(url)
    _report("save")
    file_path = save_web_snapshot(url, result["text"], result.get("title", ""),
                                  domain=domain, category=category,
                                  knowledge_dir=get_config().knowledge_dir, target=snapshot_path)

    _report("ingest")
    # 入库（走标准流水线）：skip_unchanged 让同 URL 重复抓取时走增量判断，
    # 网页内容没变就跳过（省 LLM 标签、切片和向量化）
    with istate.carry_doc_types(type_carry):
        summary = ingest_files(paths=[file_path], skip_unchanged=True,
                               default_groups=default_groups, owner=owner,
                               default_doc_type=default_doc_type)
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

    领域 / 分类沿用文件头里的声明，可见部门沿用当前卡片——入库时表单选的部门只在卡片里、
    不在文件头里，不传回去重抓后会变成全员可见。
    """
    path, knowledge_dir, _ = _resolve(relative_path)
    relative_path = path.relative_to(knowledge_dir).as_posix()
    if relative_path.split("/")[0] == "archive":
        raise ManageError("已归档的网页请先恢复，再重新抓取。")
    from src.web_fetcher import read_snapshot_origin

    origin = read_snapshot_origin(path)
    if origin is None:
        raise ManageError("这个文件不是网页快照（文件头里没有原文网址）。")
    groups = next((d["allowed_groups"] for d in get_vector_store().list_documents()
                   if d["path"] == relative_path and d["status"] == "active"), None)
    try:
        return ingest_web_page(origin["url"], domain=origin["domain"], category=origin["category"],
                               default_groups=groups, snapshot_path=path, owner=owner)
    except ManageError:
        raise
    except Exception as exc:  # 网络错误、网站返回异常等：给出可读的提示，不让页面崩溃
        raise ManageError(f"重新抓取失败：{exc}") from exc

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


def reingest(relative_path: str) -> dict:
    """重新解析并入库单个文件（文件修改后手动刷新用）。"""
    path, _, cfg = _resolve(relative_path)
    summary = ingest_files(paths=[path])
    if summary.ok_files == 0:
        raise ManageError(f"重新入库失败：{summary.failed_files}")
    return {"message": f"已重新入库：{relative_path}（{summary.total_chunks} 张卡片）", "summary": summary}


def archive(relative_path: str) -> dict:
    """归档：把文件移到 knowledge/archive/ 下（保留原子目录结构），并刷新向量库。"""
    path, knowledge_dir, cfg = _resolve(relative_path)
    if relative_path.split("/")[0] == "archive":
        raise ManageError("该文件已经在归档目录里了。")

    dest = knowledge_dir / "archive" / relative_path
    if dest.exists():  # 同名冲突：加时间戳
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dest = dest.with_name(f"{dest.stem}_{stamp}{dest.suffix}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest))

    # 文件换路径 = 换 document_id：先删旧知识，再按新路径入库（新路径自动 status=archive）
    from src.metadata import document_id_for

    store = get_vector_store()
    store.delete_documents([document_id_for(relative_path)])
    summary = ingest_files(paths=[dest])
    if summary.ok_files == 0:
        raise ManageError(f"归档后重新入库失败：{summary.failed_files}")
    return {"message": f"已归档：{relative_path} → archive/{relative_path}", "summary": summary}


def restore(archived_relative_path: str) -> dict:
    """恢复：把归档文件移回原目录；原本就散落在归档根目录的，移到 reference/documents/。"""
    path, knowledge_dir, cfg = _resolve(archived_relative_path)
    parts = archived_relative_path.split("/")
    if parts[0] != "archive" or len(parts) < 2:
        raise ManageError("该文件不在归档目录里。")

    original = "/".join(parts[1:])
    if len(parts) == 2:  # 直接放在 archive/ 根目录的文件，没有原始目录可回
        target = knowledge_dir / "reference" / "documents" / parts[1]
    else:
        target = knowledge_dir / original
    if target.exists():  # 同名冲突：加时间戳
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = target.with_name(f"{target.stem}_{stamp}{target.suffix}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(target))

    from src.metadata import document_id_for

    store = get_vector_store()
    store.delete_documents([document_id_for(archived_relative_path)])
    summary = ingest_files(paths=[target])
    if summary.ok_files == 0:
        raise ManageError(f"恢复后重新入库失败：{summary.failed_files}")
    return {"message": f"已恢复：{archived_relative_path} → {target.relative_to(knowledge_dir).as_posix()}",
            "summary": summary}


def update_metadata(relative_path: str, updates: dict) -> dict:
    """编辑文档档案：把可编辑字段写入该文档的全部知识卡片，并记入台账覆盖设置。

    可编辑字段：title / category / topic / tags / version / status。
    覆盖设置存入入库台账，重新入库时依然生效；磁盘文件不做改动。
    """
    path, _, cfg = _resolve(relative_path)
    allowed = {"title", "category", "topic", "tags", "version", "status"}
    clean: dict = {}
    for key, value in (updates or {}).items():
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
    store = get_vector_store()
    if store.collection_exists():
        from qdrant_client import models
        store.client.set_payload(
            collection_name=cfg.qdrant_collection,
            payload=clean,
            points=models.FilterSelector(
                filter=models.Filter(
                    must=[models.FieldCondition(
                        key="document_id", match=models.MatchValue(value=document_id)
                    )]
                )
            ),
            wait=True,
        )

    # 覆盖设置记入台账：重新入库时由 pipeline 优先应用（优先级高于 Front Matter 与目录）
    from src import index_state as istate

    state = istate.load_state()
    entry = state.setdefault(relative_path, {})
    overrides = entry.get("overrides", {})
    for key, value in clean.items():
        if value:
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

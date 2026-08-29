"""知识库管理操作：单个文件的 重新入库 / 归档 / 恢复 / 移除。

产品视角：这是「知识管理台」的后台——
- 归档：把文件移进 knowledge/archive/，它自动变为 status=archive，退出日常检索
- 恢复：把归档文件移回原目录，重新变为 active
- 移除：只从向量库删掉这张文档的知识（磁盘文件保留，可随时重新入库）
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from src.config import AppConfig, get_config
from src.pipeline import ingest_files
from src.vector_store import get_vector_store


class ManageError(Exception):
    """管理操作失败时抛出，信息直接给人看。"""


def _resolve(relative_path: str, cfg: AppConfig | None = None) -> tuple[Path, Path, AppConfig]:
    """把相对路径解析成 (绝对路径, 知识目录, 配置)，并检查文件存在。"""
    cfg = cfg or get_config()
    path = (cfg.knowledge_dir / relative_path).resolve()
    if not path.is_file():
        raise ManageError(f"文件不在磁盘上：{relative_path}（可能已被移动或删除）")
    return path, cfg.knowledge_dir, cfg


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


def remove_from_index(relative_path: str) -> dict:
    """只从向量库移除该文档的全部知识卡片；磁盘文件保留，可随时重新入库。"""
    _, _, cfg = _resolve(relative_path)
    from src.metadata import document_id_for

    store = get_vector_store()
    removed = store.delete_documents([document_id_for(relative_path)])
    return {"message": f"已从知识库移除：{relative_path}（磁盘文件保留，重新入库即可恢复）"}

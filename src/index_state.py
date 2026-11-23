"""V3.1 增量入库的状态账本（index_state）。

产品视角：这是一本「入库台账」，记着每个文件上次入库时的内容指纹（哈希）。
再次入库时对比指纹：
    指纹没变 → 跳过（省时省钱）
    指纹变了 → 只更新这一个文件
    台账里有、磁盘上没有 → 文件已删除，向量也同步清掉
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from src.console import console_print
from src.config import PROJECT_ROOT

# 路径移动时临时携带独立档案字段，入库成功后再写入新路径。
_doc_type_carry: ContextVar[dict] = ContextVar("doc_type_carry", default={})
_department_carry: ContextVar[dict] = ContextVar("department_carry", default={})
_project_carry: ContextVar[dict] = ContextVar("project_carry", default={})
_visibility_carry: ContextVar[dict] = ContextVar("visibility_carry", default={})


@contextmanager
def carry_doc_types(values: dict, department_values: dict | None = None, *,
                    project_values: dict | None = None, visibility_values: dict | None = None):
    """生命周期操作临时携带文档类型、归属部门和项目关联。"""
    token = _doc_type_carry.set({**_doc_type_carry.get(), **values})
    dep_token = (_department_carry.set({**_department_carry.get(), **department_values})
                 if department_values is not None else None)
    project_token = _project_carry.set({**_project_carry.get(), **(project_values or {})})
    visibility_token = _visibility_carry.set({**_visibility_carry.get(), **(visibility_values or {})})
    try:
        yield
    finally:
        _doc_type_carry.reset(token)
        if dep_token is not None:
            _department_carry.reset(dep_token)
        _project_carry.reset(project_token)
        _visibility_carry.reset(visibility_token)


def visibility_info_for(state: dict, relative_path: str) -> dict:
    entry = state.get(relative_path, {})
    info = dict(entry.get('visibility_info') or {})
    old = entry.get('overrides', {})
    if 'manual' not in info and ('allowed_groups' in old or 'allowed_users' in old):
        from src.permissions import normalize_visibility

        legacy = normalize_visibility(old.get('allowed_groups'), old.get('allowed_users'))
        if legacy != info.get('value'):
            info['manual'] = legacy
    return info or dict(_visibility_carry.get().get(relative_path, {}))


def doc_type_info_for(state: dict, relative_path: str) -> dict:
    return dict(_doc_type_carry.get().get(
        relative_path, state.get(relative_path, {}).get("doc_type_info", {})))


def department_info_for(state: dict, relative_path: str) -> dict:
    return dict(_department_carry.get().get(
        relative_path, state.get(relative_path, {}).get("department_info", {})))


def project_link_for(state: dict, relative_path: str) -> dict:
    entry = _project_carry.get().get(relative_path, state.get(relative_path, {}))
    # 未设置与人工解除关联（None）有别，后者应覆盖上传默认值。
    return {"project_id": entry["project_id"]} if "project_id" in entry else {}


def snapshot_doc_type(state: dict, relative_path: str) -> dict:
    """生命周期操作只保存类型，不恢复原路径的权限、版本等其他档案。"""
    info = doc_type_info_for(state, relative_path)
    if info:
        info["carry"] = {"value": info.get("value", ""), "source": info.get("source", "")}
    return info


def snapshot_department(state: dict, relative_path: str) -> dict:
    """生命周期操作只保存归属部门，不恢复原路径的权限、版本等其他档案。"""
    info = department_info_for(state, relative_path)
    if info:
        info["carry"] = {"value": info.get("value", ""), "source": info.get("source", "")}
    return info


STATE_PATH = PROJECT_ROOT / "storage" / "index_state.json"


def file_hash(path: Path) -> str:
    """文件内容的 MD5 指纹（内容变一个字，指纹就变）。"""
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def load_state(*, strict: bool = False) -> dict:
    """读取台账：{相对路径: {"hash", "document_id", "chunks", "indexed_at"}}。"""
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("入库台账格式无效")
        return data
    except (ValueError, OSError) as exc:
        if strict:
            raise
        # 台账损坏时回退空账本，但必须大声警告：此时全部文件会被当新文件，
        # 库里的旧版本卡片不会走 expire，可能出现新旧两代同时 active
        console_print(f"❌ 入库台账损坏（{exc}），已按空台账处理。"
                      "建议运行 python ingest.py --rebuild 重建以保持台账与向量库一致。")
        return {}


def save_state(state: dict, *, strict: bool = False) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换：避免写一半崩溃/并发写留下半截 JSON
        from tempfile import NamedTemporaryFile

        with NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".tmp",
                                dir=STATE_PATH.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            try:
                json.dump(state, tmp, ensure_ascii=False, indent=2)
                tmp.close()
                tmp_path.replace(STATE_PATH)
            finally:
                tmp.close()
                tmp_path.unlink(missing_ok=True)
    except OSError as exc:
        if strict:
            raise
        console_print(f"⚠️ 入库台账写盘失败：{exc}（不影响本次入库，下次入库会重新比对）")


def update_entry(state: dict, relative_path: str, document_id: str,
                 chunks: int, file_hash_value: str, version: str = "1.0",
                 version_info: dict | None = None,
                 version_detection: dict | None = None,
                 doc_type_info: dict | None = None,
                 department_info: dict | None = None,
                 project_link: dict | None = None,
                 visibility_info: dict | None = None) -> None:
    from datetime import datetime

    # 重新入库更新指纹时保留人工档案（尤其是可见部门），避免下次入库丢失权限。
    previous = state.get(relative_path, {})
    overrides = previous.get("overrides")
    confirmation = previous.get("version_confirmation")
    state[relative_path] = {
        "hash": file_hash_value,
        "document_id": document_id,
        "chunks": chunks,
        "version": version,
        "indexed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if overrides:
        state[relative_path]["overrides"] = overrides
    if version_info is not None:
        state[relative_path]["version_info"] = version_info
    if version_detection is not None:
        state[relative_path]["version_detection"] = version_detection
    type_info = doc_type_info if doc_type_info is not None else previous.get("doc_type_info")
    if type_info is not None:
        state[relative_path]["doc_type_info"] = dict(type_info)
    dep_info = department_info if department_info is not None else previous.get("department_info")
    if dep_info is not None:
        state[relative_path]["department_info"] = dict(dep_info)
    if project_link is not None:
        state[relative_path].update(project_link)
    elif "project_id" in previous:
        state[relative_path]["project_id"] = previous["project_id"]
    acl_info = visibility_info if visibility_info is not None else previous.get('visibility_info')
    if acl_info is not None:
        state[relative_path]['visibility_info'] = dict(acl_info)
        state[relative_path].setdefault('overrides', {}).update(acl_info['value'])
    if confirmation and confirmation.get("hash") == file_hash_value and confirmation.get("revision") == version:
        state[relative_path]["version_confirmation"] = confirmation


def remove_entry(state: dict, relative_path: str) -> None:
    state.pop(relative_path, None)

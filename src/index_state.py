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
from pathlib import Path

from src.config import PROJECT_ROOT

STATE_PATH = PROJECT_ROOT / "storage" / "index_state.json"


def file_hash(path: Path) -> str:
    """文件内容的 MD5 指纹（内容变一个字，指纹就变）。"""
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def load_state() -> dict:
    """读取台账：{相对路径: {"hash", "document_id", "chunks", "indexed_at"}}。"""
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def update_entry(state: dict, relative_path: str, document_id: str,
                 chunks: int, file_hash_value: str, version: str = "1.0") -> None:
    from datetime import datetime

    state[relative_path] = {
        "hash": file_hash_value,
        "document_id": document_id,
        "chunks": chunks,
        "version": version,
        "indexed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def remove_entry(state: dict, relative_path: str) -> None:
    state.pop(relative_path, None)

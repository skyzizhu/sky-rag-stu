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
    except (json.JSONDecodeError, OSError) as exc:
        # 台账损坏时回退空账本，但必须大声警告：此时全部文件会被当新文件，
        # 库里的旧版本卡片不会走 expire，可能出现新旧两代同时 active
        print(f"❌ 入库台账损坏（{exc}），已按空台账处理。"
              f"建议运行 python ingest.py --rebuild 重建以保持台账与向量库一致。")
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换：避免写一半崩溃/并发写留下半截 JSON
        tmp_path = STATE_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp_path.replace(STATE_PATH)
    except OSError as exc:
        print(f"⚠️ 入库台账写盘失败：{exc}（不影响本次入库，下次入库会重新比对）")


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

"""V3：会话持久化——每次问答自动保存，按日期目录归档。

目录结构：
    storage/sessions/
      2026-08-29/
        20260829_143005.json   ← 一个会话一个文件
        20260829_160000.json

JSON 内含会话标题（取第一条用户提问）、消息列表与每条回答的来源快照，
刷新页面、重启服务后都能从「历史会话列表"重新打开继续。
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path

from src.config import PROJECT_ROOT

SESSIONS_DIR = PROJECT_ROOT / "storage" / "sessions"


def _to_plain(value):
    """把 dataclass 等对象转成可 JSON 序列化的普通结构。"""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return {k: _to_plain(v) for k, v in value.__dict__.items()}
    return value


def _session_path(session_id: str) -> Path:
    date_part = session_id[:10]  # YYYY-MM-DD
    return SESSIONS_DIR / date_part / f"{session_id}.json"


def save_session(session_id: str, messages: list[dict], title: str = "") -> str:
    """保存一个会话。消息里的 result 对象转为普通字典。"""
    now = datetime.now()
    if not session_id:
        session_id = now.strftime("%Y%m%d_%H%M%S")
    plain_messages = []
    for m in messages:
        entry = {"role": m["role"], "content": m["content"]}
        if m.get("result") is not None:
            entry["result"] = _to_plain(m["result"])
        plain_messages.append(entry)
    title = title.strip() or next(
        (m["content"][:30] for m in plain_messages if m["role"] == "user"), "新会话"
    )
    data = {
        "session_id": session_id,
        "title": title,
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "messages": plain_messages,
    }
    path = _session_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return session_id


def load_session(session_id: str) -> dict | None:
    path = _session_path(session_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def list_sessions(limit: int = 50) -> list[dict]:
    """列出所有会话（按更新时间倒序）：[{session_id, title, updated_at, date, count}]。"""
    out = []
    if not SESSIONS_DIR.exists():
        return out
    for path in sorted(SESSIONS_DIR.rglob("*.json"), reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "session_id": data.get("session_id", path.stem),
            "title": data.get("title", "未命名会话"),
            "updated_at": data.get("updated_at", ""),
            "date": data.get("session_id", "")[:10],
            "count": len(data.get("messages", [])),
        })
    return out


def new_session_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")

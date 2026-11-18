"""V3：会话持久化——每次问答自动保存，按日期目录归档。

目录结构：
    storage/sessions/
      20261118/                 ← 会话创建日期
        42/                     ← 登录用户的数据库 ID
          20261118_143005-….json ← 一个会话一个文件
      .locks/                   ← 全局会话锁及删除标记（不进入历史列表）

JSON 内含会话标题（取第一条用户提问）、消息列表与每条回答的来源快照，
刷新页面、重启服务后都能从「历史会话列表"重新打开继续。
"""

from __future__ import annotations

import json
import hashlib
import os
import threading
from contextlib import contextmanager
import re
import secrets
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path

from src.config import PROJECT_ROOT

SESSIONS_DIR = PROJECT_ROOT / "storage" / "sessions"

# 合法 session_id：时间戳 + 新的 128 位随机后缀；兼容旧的无后缀/4 位后缀。
# session_id 会被拼进文件路径，绝不能含 / .. 等路径字符（防目录穿越）
_SESSION_ID_RE = re.compile(r"^\d{8}_\d{6}(-(?:[a-z0-9]{4}|[a-f0-9]{32}))?$")


def _to_plain(value):
    """把 dataclass 等对象转成可 JSON 序列化的普通结构。"""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return {k: _to_plain(v) for k, v in value.__dict__.items()}
    return value


def _user_directory(username: str | None) -> str:
    """真实账户用不可变数字 ID；无归属/已删除账户分别隔离，用户名不直接拼路径。"""
    if not username:
        return 'anonymous'
    from src.auth_db import get_user
    user = get_user(username)
    if user and type(user.get('id')) is int and user['id'] > 0:
        return str(user['id'])
    return 'account_' + hashlib.sha256(username.encode()).hexdigest()[:16]


def _owner_matches(data, username, path, directory=None):
    if (data.get('username') or '') != (username or ''):
        return False
    expected = directory if directory is not None else _user_directory(username)
    if data.get('user_id') is not None and str(data['user_id']) != expected:
        return False
    return path.parent.name == expected and path.parent.parent.name == path.stem[:8]


def _session_path(session_id: str, username: str | None = None) -> Path | None:
    """唯一目录规范：创建日期 / 服务端账户 ID / 会话 ID.json。"""
    if not _SESSION_ID_RE.fullmatch(session_id or ''):
        return None
    return SESSIONS_DIR / session_id[:8] / _user_directory(username) / f'{session_id}.json'


class SessionConflict(RuntimeError):
    """同一会话已被其他标签页更新或删除，禁止用旧快照覆盖。"""


_WRITE_LOCK = threading.RLock()


@contextmanager
def _locked_session(path):
    """线程锁 + OS 文件锁，覆盖归属检查、旧版本检查和原子替换的完整事务。"""
    with _WRITE_LOCK:
        locks = SESSIONS_DIR / '.locks'
        locks.mkdir(parents=True, exist_ok=True)
        lock_path = locks / (path.stem + '.lock')
        with lock_path.open('a+b') as handle:
            if os.name == 'nt':
                import msvcrt
                if handle.seek(0, 2) == 0:
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                yield handle
            finally:
                if os.name == 'nt':
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_session(session_id, username=None):
    path = _session_path(session_id, username)
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def save_session(session_id: str, messages: list[dict], title: str = "",
                 username: str | None = None, context_state: dict | None = None) -> str:
    """保存一个会话。消息里的 result 对象转为普通字典。

    username 是服务端登录身份；None 仅可写无归属的个人模式记录，不能绕过已归属记录的权限。
    """
    now = datetime.now()
    if not _SESSION_ID_RE.fullmatch(session_id or ""):
        session_id = new_session_id()
    plain_messages = []
    for m in messages:
        entry = {"role": m.get("role"), "content": m.get("content")}
        if isinstance(m.get("created_at"), str):
            entry["created_at"] = m["created_at"]
        if m.get("result") is not None:
            entry["result"] = _to_plain(m["result"])
        plain_messages.append(entry)
    title = title.strip() or next(
        (m["content"][:30] for m in plain_messages if m["role"] == "user"), "新会话"
    )
    directory = _user_directory(username)
    data = {
        "session_id": session_id,
        "title": title,
        "username": username or "",
        "user_id": int(directory) if directory.isdigit() else None,
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "messages": plain_messages,
    }
    if context_state and context_state.get("session_id") == session_id and context_state.get("owner") == (username or ""):
        data["context"] = context_state
    path = _session_path(session_id, username)
    with _locked_session(path) as marker:
        if marker.read() == b'deleted':
            raise SessionConflict('该会话已删除，不能由旧标签页重新创建')
        # 全局 SID 锁内防止强制碰撞在另一用户目录创建同 ID 的副本。
        if any(other != path for other in path.parent.parent.glob(f'*/{session_id}.json')):
            raise PermissionError('会话 ID 已由其他账户占用')
        existing = _read_session(session_id, username)
        if existing and not _owner_matches(existing, username, path):
            raise PermissionError('不能覆盖其他用户的会话')
        if existing:
            old = [(m.get('role'), m.get('content')) for m in existing.get('messages', [])]
            new = [(m.get('role'), m.get('content')) for m in plain_messages]
            if new[:len(old)] != old:
                raise SessionConflict('会话已更新，不能覆盖或截短现有记录')
        # 每个写请求使用独立临时文件，原子替换只发生在完成全部检查之后。
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
    return session_id


def load_session(session_id: str, username: str | None = None) -> dict | None:
    data = _read_session(session_id, username)
    if data is None or not _owner_matches(data, username, _session_path(session_id, username)):
        return None
    return data


def delete_session(session_id: str, username: str | None = None) -> bool:
    path = _session_path(session_id, username)
    if path is None or not path.exists():
        return False
    with _locked_session(path) as marker:
        path = _session_path(session_id, username)
        data = _read_session(session_id, username)
        if data is None or not _owner_matches(data, username, path):
            return False
        path.unlink()
        # 留删除标记，防止其他仍打开的旧标签页自动保存后使会话复活。
        marker.seek(0)
        marker.truncate()
        marker.write(b'deleted')
        marker.flush()
    return True


def list_sessions(limit: int = 50, username: str | None = None,
                  is_admin: bool = False) -> list[dict]:
    """列出会话（按更新时间倒序）：[{session_id, title, updated_at, date, count}]。

    username=None 仅列出无归属的个人记录，不能列出其他用户的记录。
    传入用户名只显示该用户的会话；is_admin 只额外允许列出无归属的旧记录。
    """
    out = []
    seen = set()
    directory = _user_directory(username)
    if not SESSIONS_DIR.exists():
        return out
    paths = list(SESSIONS_DIR.glob(f'????????/{directory}/*.json'))
    if is_admin and directory != 'anonymous':
        paths.extend(SESSIONS_DIR.glob('????????/anonymous/*.json'))
    for path in sorted(paths, reverse=True):
        if path.suffix != ".json" or path.stem.endswith(".tmp"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        owner = data.get("username") or ""
        if not _owner_matches(data, username, path, directory) and not (
            is_admin and not owner and _owner_matches(data, None, path, 'anonymous')
        ):
            continue
        sid = data.get("session_id", path.stem)
        if not _SESSION_ID_RE.fullmatch(sid or '') or sid != path.stem or sid in seen:
            continue
        seen.add(sid)
        out.append({
            "session_id": sid,
            "title": data.get("title", "未命名会话"),
            "updated_at": data.get("updated_at", ""),
            "date": sid[:8],
            "count": len(data.get("messages", [])),
        })
        if len(out) >= limit:
            break
    return out


def new_session_id() -> str:
    # 时间戳用于创建日期归档，128 位随机后缀降低并发碰撞概率。
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "-" + secrets.token_hex(16)

"""V3.7：用户体系数据层（SQLite）。

FastAPI 管理后台（8700）与 Streamlit 前台共用同一份数据与逻辑：
- departments / users / tokens / user_prefs 四张表
- 每次操作独立短连接 + WAL（多线程两服务并发写安全）
- 密码 pbkdf2_hmac(sha256, 200_000 轮) + secrets 盐；令牌只存 SHA-256 哈希
- 约定：卡片 allowed_groups 存**部门名**列表；部门不支持改名（name 是稳定标识）
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from src.config import PROJECT_ROOT

AUTH_DIR = PROJECT_ROOT / "storage" / "auth"
DB_PATH = AUTH_DIR / "auth.db"
SECRET_PATH = AUTH_DIR / "admin_secret.key"

_PBKDF2_ROUNDS = 200_000
_USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fa5]{2,32}$", re.UNICODE)
_write_lock = threading.Lock()  # 建表等初始化写用；常规读写靠 WAL + 短事务


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    department_id INTEGER REFERENCES departments(id),
    is_admin INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    token_prefix TEXT NOT NULL DEFAULT '',
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_used TEXT NOT NULL DEFAULT '',
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_prefs (
    username TEXT PRIMARY KEY,
    settings_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS login_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,            -- login / logout / register / bootstrap_admin
    username TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL DEFAULT 1,
    detail TEXT NOT NULL DEFAULT '',
    department TEXT NOT NULL DEFAULT '',
    entry TEXT NOT NULL DEFAULT '',  -- web / admin
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    browser TEXT NOT NULL DEFAULT '',
    os_name TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_logs_time ON login_logs(created_at DESC);
CREATE TABLE IF NOT EXISTS qa_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL DEFAULT '',
    department TEXT NOT NULL DEFAULT '',
    entry TEXT NOT NULL DEFAULT '',  -- web / manage_summary / mcp_http / mcp_stdio
    question TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT '',
    sources TEXT NOT NULL DEFAULT '[]',  -- JSON [{source, score}]
    filters TEXT NOT NULL DEFAULT '{}',  -- JSON（含 allowed_groups 权限域）
    top_k INTEGER NOT NULL DEFAULT 0,
    elapsed REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qa_logs_time ON qa_logs(created_at DESC);
"""


def init_db() -> None:
    """建表（幂等）。两服务启动时各调一次。"""
    with _write_lock, _connect() as conn:
        conn.executescript(_SCHEMA)


# ---------------------------------------------------------------- 密码与令牌
def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), _PBKDF2_ROUNDS)
    return digest.hex(), salt


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    digest, _ = hash_password(password, salt)
    return hmac.compare_digest(digest, expected_hash)


def _token_hash(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 部门
def create_department(name: str, description: str = "") -> dict:
    name = (name or "").strip()
    if not 1 <= len(name) <= 32:
        raise ValueError("部门名长度需在 1~32 字符之间")
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO departments(name, description, created_at) VALUES(?,?,?)",
                (name, (description or "").strip(), _now()),
            )
            row = conn.execute("SELECT * FROM departments WHERE name=?",
                               (name,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise ValueError("同名部门已存在") from exc
    return dict(row)


def list_departments(include_disabled: bool = True) -> list[dict]:
    sql = "SELECT * FROM departments"
    if not include_disabled:
        sql += " WHERE disabled=0"
    sql += " ORDER BY id"
    with _connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def department_counts() -> dict[int, int]:
    """各部门员工数（用于删除保护与列表展示）。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT department_id, COUNT(*) AS n FROM users "
            "WHERE department_id IS NOT NULL GROUP BY department_id"
        ).fetchall()
    return {r["department_id"]: r["n"] for r in rows}


def set_department_disabled(dep_id: int, disabled: bool) -> None:
    with _connect() as conn:
        cur = conn.execute("UPDATE departments SET disabled=? WHERE id=?",
                           (1 if disabled else 0, dep_id))
        if cur.rowcount == 0:
            raise ValueError("部门不存在")


def delete_department(dep_id: int) -> None:
    with _connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM users WHERE department_id=?",
                         (dep_id,)).fetchone()["n"]
        if n:
            raise ValueError(f"该部门下仍有 {n} 名员工，请先转移再删除")
        cur = conn.execute("DELETE FROM departments WHERE id=?", (dep_id,))
        if cur.rowcount == 0:
            raise ValueError("部门不存在")


# ---------------------------------------------------------------- 用户
def _validate_username(username: str) -> str:
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        raise ValueError("账户名需为 2~32 位字母/数字/下划线/中文")
    return username


def _validate_password(password: str) -> str:
    if len(password or "") < 6:
        raise ValueError("密码至少 6 位")
    return password


def create_user(username: str, password: str, department_id: int | None = None,
                is_admin: bool = False, status: str = "active") -> dict:
    username = _validate_username(username)
    _validate_password(password)
    pw_hash, salt = hash_password(password)
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO users(username, password_hash, salt, department_id,"
                " is_admin, status, created_at) VALUES(?,?,?,?,?,?,?)",
                (username, pw_hash, salt, department_id,
                 1 if is_admin else 0, status, _now()),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError("该账户名已被注册") from exc
    user = get_user(username)
    assert user is not None
    return user


def get_user(username: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT u.*, d.name AS department FROM users u "
            "LEFT JOIN departments d ON d.id = u.department_id WHERE u.username=?",
            (username,),
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT u.*, d.name AS department FROM users u "
            "LEFT JOIN departments d ON d.id = u.department_id WHERE u.id=?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def list_users(department_id: int | None = None) -> list[dict]:
    sql = ("SELECT u.id, u.username, u.department_id, u.is_admin, u.status, "
           "u.created_at, d.name AS department FROM users u "
           "LEFT JOIN departments d ON d.id = u.department_id")
    params: tuple = ()
    if department_id is not None:
        sql += " WHERE u.department_id=?"
        params = (department_id,)
    sql += " ORDER BY u.id"
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_users() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def update_user(user_id: int, department_id: int | None = ...,
                status: str | None = None) -> None:
    """... 表示不改部门（区别于改为"无部门"）。"""
    with _connect() as conn:
        if department_id is not ...:
            conn.execute("UPDATE users SET department_id=? WHERE id=?",
                         (department_id, user_id))
        if status is not None:
            if status not in ("active", "disabled"):
                raise ValueError("非法状态")
            conn.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))


def reset_password(user_id: int, new_password: str) -> None:
    """管理员重置密码；同时吊销该用户全部令牌（密码属凭证变更）。"""
    _validate_password(new_password)
    pw_hash, salt = hash_password(new_password)
    with _connect() as conn:
        cur = conn.execute("UPDATE users SET password_hash=?, salt=? WHERE id=?",
                           (pw_hash, salt, user_id))
        if cur.rowcount == 0:
            raise ValueError("用户不存在")
        conn.execute("UPDATE tokens SET revoked=1 WHERE user_id=?", (user_id,))


def authenticate(username: str, password: str) -> tuple[dict | None, str]:
    """登录验证。返回 (用户信息, 错误信息)；成功时错误信息为空串。"""
    user = get_user((username or "").strip())
    if user is None:
        return None, "账户或密码不正确"
    if not verify_password(password, user["salt"], user["password_hash"]):
        return None, "账户或密码不正确"
    if user["status"] != "active":
        return None, "该账户已被停用，请联系管理员"
    info = {"username": user["username"], "department": user["department"],
            "department_id": user["department_id"], "is_admin": bool(user["is_admin"])}
    return info, ""


# ---------------------------------------------------------------- 访问令牌
def create_token(user_id: int, name: str = "") -> str:
    """生成 PAT。明文只在返回时出现一次，库里只存哈希。"""
    plaintext = "mcp_pat_" + secrets.token_urlsafe(24)
    prefix = plaintext[:16]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO tokens(token_hash, token_prefix, user_id, name, created_at)"
            " VALUES(?,?,?,?,?)",
            (_token_hash(plaintext), prefix, user_id, (name or "").strip(), _now()),
        )
    return plaintext


def verify_token(plaintext: str) -> dict | None:
    """验证 PAT：返回 {user, department, token_row}；无效/吊销/停用返回 None。"""
    row = None
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tokens WHERE token_hash=?",
                           (_token_hash(plaintext or ""),)).fetchone()
    if row is None or row["revoked"]:
        return None
    user = get_user_by_id(row["user_id"])
    if user is None or user["status"] != "active":
        return None
    with _connect() as conn:
        conn.execute("UPDATE tokens SET last_used=? WHERE token_hash=?",
                     (_now(), row["token_hash"]))
    return {"username": user["username"], "department": user["department"],
            "is_admin": bool(user["is_admin"]), "token_name": row["name"]}


def list_tokens() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT t.token_prefix, t.name, t.created_at, t.last_used, t.revoked,"
            " u.username FROM tokens t LEFT JOIN users u ON u.id=t.user_id "
            "ORDER BY t.created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def revoke_token(token_prefix: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE tokens SET revoked=1 WHERE token_prefix=?",
                     (token_prefix,))


# ---------------------------------------------------------------- 用户偏好
def get_user_prefs(username: str) -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT settings_json FROM user_prefs WHERE username=?",
                           (username,)).fetchone()
    try:
        return json.loads(row["settings_json"]) if row else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def save_user_prefs(username: str, settings: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_prefs(username, settings_json) VALUES(?,?) "
            "ON CONFLICT(username) DO UPDATE SET settings_json=excluded.settings_json",
            (username, json.dumps(settings, ensure_ascii=False)),
        )


# ---------------------------------------------------------------- 登录日志
_BROWSER_RES = [
    ("Edge", re.compile(r"Edg(?:e|A|iOS)?/([\d.]+)")),
    ("Chrome", re.compile(r"(?:Chrome|CriOS)/([\d.]+)")),
    ("Firefox", re.compile(r"Firefox/([\d.]+)")),
    ("Safari", re.compile(r"Version/([\d.]+).*Safari")),
]
_OS_RES = [
    ("Windows", re.compile(r"Windows NT")),
    ("macOS", re.compile(r"Mac OS X|Macintosh")),
    ("iOS", re.compile(r"iPhone|iPad|iPod")),
    ("Android", re.compile(r"Android")),
    ("Linux", re.compile(r"Linux")),
]


def parse_user_agent(ua: str) -> tuple[str, str]:
    """从 User-Agent 解析 (浏览器名+主版本, 操作系统)。解析不出返回 ('unknown','unknown')。"""
    ua = ua or ""
    browser, os_name = "unknown", "unknown"
    for name, pattern in _BROWSER_RES:
        m = pattern.search(ua)
        if m:
            major = (m.group(1) or "").split(".")[0]
            browser = f"{name} {major}" if major else name
            break
    for name, pattern in _OS_RES:
        if pattern.search(ua):
            os_name = name
            break
    return browser, os_name


def make_fingerprint(user_agent: str, accept_language: str = "",
                     extra: str = "") -> str:
    """设备指纹：UA + 接受语言 + 附加串 的 SHA-256 前 16 位。

    不是 Canvas 级指纹，但对内部工具足够识别"同一台设备/同一浏览器配置"。
    """
    raw = "|".join([user_agent or "", accept_language or "", extra or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def log_auth_event(action: str, username: str, *, success: bool = True,
                   detail: str = "", department: str = "", entry: str = "",
                   ip: str = "", user_agent: str = "",
                   accept_language: str = "") -> None:
    """记录一条认证事件（登录成功/失败、注册、登出、初始化管理员）。

    日志失败绝不抛出——认证主流程不能被日志拖垮。
    """
    browser, os_name = parse_user_agent(user_agent)
    fp = make_fingerprint(user_agent, accept_language, ip)
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO login_logs(action, username, success, detail, department,"
                " entry, ip, user_agent, browser, os_name, fingerprint, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (action, username or "", 1 if success else 0, detail or "",
                 department or "", entry or "", ip or "", user_agent or "",
                 browser, os_name, fp, _now()),
            )
    except sqlite3.Error:
        pass


def list_login_logs(limit: int = 200, username: str = "",
                    action: str = "") -> list[dict]:
    sql = "SELECT * FROM login_logs"
    where, params = [], []
    if username:
        where.append("username LIKE ?")
        params.append(f"%{username}%")
    if action:
        where.append("action=?")
        params.append(action)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- RAG 使用日志
def log_qa_event(username: str, department: str, entry: str, question: str,
                 answer: str = "", sources: list | None = None,
                 filters: dict | None = None, top_k: int = 0,
                 elapsed: float = 0.0) -> None:
    """记录一次 RAG 使用（问答/检索/总结）。失败不抛出，不影响回答主流程。"""
    slim_sources = [
        {"source": s.get("source"), "score": round(float(s.get("score", 0)), 4)}
        for s in (sources or [])[:12]
    ]
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO qa_logs(username, department, entry, question, answer,"
                " sources, filters, top_k, elapsed, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (username or "", department or "", entry or "", question or "",
                 answer or "", json.dumps(slim_sources, ensure_ascii=False),
                 json.dumps(filters or {}, ensure_ascii=False),
                 int(top_k or 0), float(elapsed or 0), _now()),
            )
    except (sqlite3.Error, TypeError, ValueError):
        pass


def list_qa_logs(limit: int = 100, username: str = "",
                 entry: str = "") -> list[dict]:
    sql = "SELECT * FROM qa_logs"
    where, params = [], []
    if username:
        where.append("username LIKE ?")
        params.append(f"%{username}%")
    if entry:
        where.append("entry=?")
        params.append(entry)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 300)))
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        try:
            item["sources"] = json.loads(item.get("sources") or "[]")
        except (json.JSONDecodeError, TypeError):
            item["sources"] = []
        try:
            item["filters"] = json.loads(item.get("filters") or "{}")
        except (json.JSONDecodeError, TypeError):
            item["filters"] = {}
        out.append(item)
    return out

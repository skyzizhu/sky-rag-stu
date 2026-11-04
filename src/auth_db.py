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
import os
import re
import secrets
import sqlite3
import threading
import time
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
    permissions TEXT NOT NULL DEFAULT '[]',
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
    """建表（幂等）+ 增量迁移（users.is_super / users.permissions）。两服务启动时各调一次。"""
    with _write_lock, _connect() as conn:
        conn.executescript(_SCHEMA)
        for column_sql in (
            "ALTER TABLE users ADD COLUMN is_super INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN permissions TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE users ADD COLUMN real_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE users ADD COLUMN note TEXT NOT NULL DEFAULT ''",
        ):
            try:
                conn.execute(column_sql)
            except sqlite3.OperationalError:
                pass  # 列已存在
        # 超级管理员迁移：系统已有用户但还没有超级管理员时，
        # 把最早创建的管理员（init_db 引导的首账户）标记为超级管理员
        has_super = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_super=1").fetchone()["n"]
        if has_super == 0:
            first_admin = conn.execute(
                "SELECT id FROM users WHERE is_admin=1 ORDER BY id LIMIT 1").fetchone()
            if first_admin:
                conn.execute("UPDATE users SET is_super=1 WHERE id=?",
                             (first_admin["id"],))


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
                is_admin: bool = False, status: str = "active",
                permissions: list[str] | None = None,
                real_name: str = "", note: str = "") -> dict:
    username = _validate_username(username)
    _validate_password(password)
    pw_hash, salt = hash_password(password)
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO users(username, password_hash, salt, department_id,"
                " is_admin, status, permissions, real_name, note, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (username, pw_hash, salt, department_id,
                 1 if is_admin else 0, status,
                 json.dumps(_valid_permissions(permissions)),
                 (real_name or "").strip(), (note or "").strip(), _now()),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError("该账户名已被注册") from exc
    user = get_user(username)
    assert user is not None
    return user


# 前台可选权限集（员工默认一无所有；查询/通用设置/系统状态/参数总览/学习是人人固有的，
# 不在此列）。检索设置与知识库维护拆分为独立权限（V3.7.2）。
FRONT_PERMISSIONS = ("upload", "manage", "retrieval_settings", "maintenance", "plugins")


def _valid_permissions(perms: list[str] | None) -> list[str]:
    if not perms:
        return []
    return [p for p in perms if p in FRONT_PERMISSIONS]


def get_user(username: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT u.*, d.name AS department FROM users u "
            "LEFT JOIN departments d ON d.id = u.department_id WHERE u.username=?",
            (username,),
        ).fetchone()
    user = dict(row) if row else None
    if user:
        try:
            user["permissions"] = json.loads(user.get("permissions") or "[]")
        except (json.JSONDecodeError, TypeError):
            user["permissions"] = []
    return user


def get_user_by_id(user_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT u.*, d.name AS department FROM users u "
            "LEFT JOIN departments d ON d.id = u.department_id WHERE u.id=?",
            (user_id,),
        ).fetchone()
    user = dict(row) if row else None
    if user:
        try:
            user["permissions"] = json.loads(user.get("permissions") or "[]")
        except (json.JSONDecodeError, TypeError):
            user["permissions"] = []
    return user


def list_users(department_id: int | None = None,
               is_admin: bool | None = None) -> list[dict]:
    """is_admin=True 只返回管理员，False 只返回员工，None 全部。"""
    sql = ("SELECT u.id, u.username, u.department_id, u.is_admin, u.status, "
           "u.created_at, u.permissions, u.real_name, u.note, "
           "d.name AS department FROM users u "
           "LEFT JOIN departments d ON d.id = u.department_id")
    where, params = [], []
    if department_id is not None:
        where.append("u.department_id=?")
        params.append(department_id)
    if is_admin is not None:
        where.append("u.is_admin=?")
        params.append(1 if is_admin else 0)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY u.id"
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        try:
            item["permissions"] = json.loads(item.get("permissions") or "[]")
        except (json.JSONDecodeError, TypeError):
            item["permissions"] = []
        out.append(item)
    return out


def count_users() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def update_user(user_id: int, department_id: int | None = ...,
                status: str | None = None,
                permissions: list[str] | None = None,
                real_name: str | None = None,
                note: str | None = None) -> None:
    """... 表示不改部门（区别于改为"无部门"）。"""
    with _connect() as conn:
        if department_id is not ...:
            conn.execute("UPDATE users SET department_id=? WHERE id=?",
                         (department_id, user_id))
        if status is not None:
            if status not in ("active", "disabled"):
                raise ValueError("非法状态")
            conn.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))
        if permissions is not None:
            conn.execute("UPDATE users SET permissions=? WHERE id=?",
                         (json.dumps(_valid_permissions(permissions)), user_id))
        if real_name is not None:
            conn.execute("UPDATE users SET real_name=? WHERE id=?",
                         ((real_name or "").strip(), user_id))
        if note is not None:
            conn.execute("UPDATE users SET note=? WHERE id=?",
                         ((note or "").strip(), user_id))


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
            "department_id": user["department_id"], "is_admin": bool(user["is_admin"]),
            "permissions": user.get("permissions") or [],
            "real_name": user.get("real_name") or "", "note": user.get("note") or ""}
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


def _login_logs_where(username: str, action: str,
                      scope: str | None) -> tuple[str, list]:
    """登录日志的公共 WHERE 构造（列表与计数共用，保证分页总数一致）。

    scope="admins" 只看管理员的记录，"users" 只看员工/注册者的记录。
    按 username 关联 users.is_admin 判定；日志里不存在于用户表的账户
    （如登录失败的乱输）一律归入 users 侧。
    """
    join = " LEFT JOIN users u ON u.username = l.username"
    where, params = [], []
    if username:
        where.append("l.username LIKE ?")
        params.append(f"%{username}%")
    if action:
        where.append("l.action=?")
        params.append(action)
    if scope == "admins":
        where.append("COALESCE(u.is_admin, 0)=1")
    elif scope == "users":
        where.append("COALESCE(u.is_admin, 0)=0")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    return (join + where_sql), params


def list_login_logs(limit: int = 200, username: str = "",
                    action: str = "", scope: str | None = None,
                    offset: int = 0) -> list[dict]:
    where_sql, params = _login_logs_where(username, action, scope)
    sql = ("SELECT l.* FROM login_logs l" + where_sql)
    sql += " ORDER BY l.id DESC LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_login_logs(username: str = "", action: str = "",
                     scope: str | None = None) -> int:
    where_sql, params = _login_logs_where(username, action, scope)
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM login_logs l" + where_sql, params).fetchone()
    return row["n"]


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


def _qa_logs_where(username: str, entry: str, scope: str | None,
                   department: str = "") -> tuple[str, list]:
    join = " LEFT JOIN users u ON u.username = l.username"
    where, params = [], []
    if username:
        where.append("l.username LIKE ?")
        params.append(f"%{username}%")
    if entry:
        where.append("l.entry=?")
        params.append(entry)
    if department:
        where.append("l.department=?")
        params.append(department)
    if scope == "admins":
        where.append("COALESCE(u.is_admin, 0)=1")
    elif scope == "users":
        where.append("COALESCE(u.is_admin, 0)=0")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    return (join + where_sql), params


def list_qa_logs(limit: int = 100, username: str = "",
                 entry: str = "", scope: str | None = None,
                 department: str = "", offset: int = 0) -> list[dict]:
    """scope="admins"/"users" 按账户角色分离管理员与员工的使用记录。"""
    # JOIN 由 _qa_logs_where 统一拼接（与 count 共用，保证总数一致）
    sql = "SELECT l.* FROM qa_logs l"
    where_sql, params = _qa_logs_where(username, entry, scope, department)
    sql += where_sql
    sql += " ORDER BY l.id DESC LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit), 300)), max(0, int(offset))])
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


def count_qa_logs(username: str = "", entry: str = "", scope: str | None = None,
                  department: str = "") -> int:
    where_sql, params = _qa_logs_where(username, entry, scope, department)
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM qa_logs l" + where_sql, params).fetchone()
    return row["n"]


def count_user_tokens(user_id: int) -> int:
    """该用户的有效（未吊销）令牌数。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tokens WHERE user_id=? AND revoked=0",
            (user_id,)).fetchone()
    return row["n"]


# ---------------------------------------------------------------- 登录态 Cookie（V3.7.2）
# 前台（8501）与管理后台（8700）同域不同端口——Cookie 不区分端口，
# 认证服务签发的 front_auth cookie，浏览器会带给 Streamlit，实现登录态持久化。
_SECRET_CACHE: bytes | None = None


def get_secret() -> bytes:
    """HMAC 签名密钥：落盘 storage/auth/admin_secret.key（0600），进程内缓存。"""
    global _SECRET_CACHE
    if _SECRET_CACHE is not None:
        return _SECRET_CACHE
    if SECRET_PATH.exists():
        _SECRET_CACHE = SECRET_PATH.read_bytes()
    else:
        secret = secrets.token_hex(32).encode()
        AUTH_DIR.mkdir(parents=True, exist_ok=True)
        SECRET_PATH.write_bytes(secret)
        os.chmod(SECRET_PATH, 0o600)
        _SECRET_CACHE = secret
    return _SECRET_CACHE


_SESSION_TTL = 30 * 24 * 3600  # 登录态保持 30 天


def _sign(payload: str) -> str:
    import hmac
    return hmac.new(get_secret(), payload.encode(), hashlib.sha256).hexdigest()


def make_session_cookie(username: str) -> str:
    """登录态 Cookie 值：用户名|过期时间戳|签名。"""
    expires = str(int(time.time()) + _SESSION_TTL)
    return f"{expires}|{username}|{_sign(f'sess|{expires}|{username}')}"


def verify_session_cookie(value: str) -> str | None:
    """校验登录态 Cookie，返回用户名；无效/过期返回 None。"""
    import time
    if not value:
        return None
    parts = value.split("|")
    if len(parts) != 3:
        return None
    expires, username, sig = parts
    if not hmac.compare_digest(_sign(f"sess|{expires}|{username}"), sig):
        return None
    if int(expires) < int(time.time()):
        return None
    return username


def make_login_proof(username: str, ttl: int = 120) -> str:
    """一次性登录凭证：登录成功后由前端 iframe 提交给认证服务换取持久化 Cookie。

    短时效 + 绑定用户名，防止任何人凭空给自己签发登录态 Cookie。
    """
    import time
    expires = str(int(time.time()) + ttl)
    return f"{expires}|{username}|{_sign(f'proof|{expires}|{username}')}"


def verify_login_proof(proof: str) -> str | None:
    """校验 proof 签名与时效（不消费；消费由 consume_login_proof 负责）。"""
    import time
    if not proof:
        return None
    parts = proof.split("|")
    if len(parts) != 3:
        return None
    expires, username, sig = parts
    if not hmac.compare_digest(_sign(f"proof|{expires}|{username}"), sig):
        return None
    if int(expires) < int(time.time()):
        return None
    return username


# 已消费的 proof 记录（防 120 秒时效内的重放）；带时间戳，过期自动清理
_used_proofs: dict[str, int] = {}


def consume_login_proof(proof: str) -> str | None:
    """校验并【一次性消费】登录 proof：返回用户名；无效/过期/已用过返回 None。

    proof 会出现在 persist-login 的 URL 里（可能进访问日志），一次性消费
    保证日志里的旧 proof 无法重放换取 Cookie。
    """
    username = verify_login_proof(proof)
    if not username:
        return None
    now = int(time.time())
    for k in [k for k, exp in _used_proofs.items() if exp < now]:
        _used_proofs.pop(k, None)
    key = hashlib.sha256(proof.encode()).hexdigest()[:16]
    if key in _used_proofs:
        return None  # 已消费
    _used_proofs[key] = now + 600
    return username

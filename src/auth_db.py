"""V3.7：用户体系数据层（SQLite）。

FastAPI 管理后台（8700）与 Streamlit 前台共用同一份数据与逻辑：
- departments / users / admins / tokens / user_prefs 五张表 +
  user_login_logs / admin_login_logs / qa_logs 三张日志表
- V3.8 硬性规则：管理员与员工彻底分表——admins 独立存储，users 只放员工；
  管理员禁止登录前台（authenticate 只查员工表，前台登录入口显式拒绝管理员账户名）；
  登录日志按身份分表（user_login_logs / admin_login_logs，按 entry 路由）
- 每次操作独立短连接 + WAL（多线程两服务并发写安全）
- 密码 pbkdf2_hmac(sha256, 200_000 轮) + secrets 盐；令牌只存 SHA-256 哈希
- 账户名全局唯一：admins 与 users 不允许重名（否则前台登录会被管理员拦截规则挡住）
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


_LOG_COLS = """
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,            -- login / logout / register / bootstrap_admin
    username TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL DEFAULT 1,
    detail TEXT NOT NULL DEFAULT '',
    department TEXT NOT NULL DEFAULT '',
    entry TEXT NOT NULL DEFAULT '',  -- web（员工侧）/ admin（管理后台侧）
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    browser TEXT NOT NULL DEFAULT '',
    os_name TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    is_super INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    real_name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    department_id INTEGER REFERENCES departments(id),
    status TEXT NOT NULL DEFAULT 'active',
    permissions TEXT NOT NULL DEFAULT '[]',
    real_name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
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
CREATE TABLE IF NOT EXISTS user_login_logs (""" + _LOG_COLS + """
);
CREATE INDEX IF NOT EXISTS idx_user_login_logs_time ON user_login_logs(created_at DESC);
CREATE TABLE IF NOT EXISTS admin_login_logs (""" + _LOG_COLS + """
);
CREATE INDEX IF NOT EXISTS idx_admin_login_logs_time ON admin_login_logs(created_at DESC);
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
    """建表（幂等）+ 存量迁移。两服务启动时各调一次。

    V3.8 分表迁移（一次性，事务内原子完成）：
    1. users.is_admin=1 的行 → 迁入 admins 表（密码哈希原样携带，登录不受影响），
       其访问令牌一并清除（管理员不再持有前台/MCP 令牌），随后从 users 删除；
    2. users 表 DROP 掉 is_admin / is_super 列（员工表不再有管理员概念）；
    3. 旧 login_logs 按 entry 拆入 user_login_logs / admin_login_logs 后删表；
    4. qa_logs 中管理员账户的历史记录清除（RAG 使用日志只反映员工行为）。
    """
    with _write_lock, _connect() as conn:
        conn.executescript(_SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "is_admin" in cols:  # 旧库：管理员还在 users 表里
            admins_rows = conn.execute(
                "SELECT * FROM users WHERE is_admin=1 ORDER BY id").fetchall()
            for r in admins_rows:
                conn.execute(
                    "INSERT OR IGNORE INTO admins(username, password_hash, salt,"
                    " is_super, status, real_name, note, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (r["username"], r["password_hash"], r["salt"],
                     r["is_super"] if "is_super" in cols else 0, r["status"],
                     r["real_name"] if "real_name" in cols else "",
                     r["note"] if "note" in cols else "", r["created_at"]))
                conn.execute("DELETE FROM tokens WHERE user_id=?", (r["id"],))
            conn.execute("DELETE FROM users WHERE is_admin=1")
            for column_sql in ("ALTER TABLE users DROP COLUMN is_admin",
                               "ALTER TABLE users DROP COLUMN is_super"):
                try:
                    conn.execute(column_sql)
                except sqlite3.OperationalError:
                    pass
        # 迁移兜底：如果还没有任何超级管理员，把最早的管理员标记为超级管理员
        if conn.execute("SELECT COUNT(*) AS n FROM admins WHERE is_super=1"
                        ).fetchone()["n"] == 0:
            first = conn.execute(
                "SELECT id FROM admins ORDER BY id LIMIT 1").fetchone()
            if first:
                conn.execute("UPDATE admins SET is_super=1 WHERE id=?",
                             (first["id"],))
        # 登录日志拆表：按 entry 归入员工/管理员两张表，完成后删除旧表
        legacy = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name='login_logs'").fetchone()
        if legacy:
            conn.execute(
                "INSERT OR IGNORE INTO user_login_logs SELECT * FROM login_logs "
                "WHERE entry != 'admin'")
            conn.execute(
                "INSERT OR IGNORE INTO admin_login_logs SELECT * FROM login_logs "
                "WHERE entry = 'admin'")
            conn.execute("DROP TABLE login_logs")
        # RAG 使用日志只保留员工行为（管理员已不能登录前台/MCP）
        conn.execute(
            "DELETE FROM qa_logs WHERE username IN (SELECT username FROM admins)")


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


def _ensure_username_free(username: str) -> None:
    """账户名全局唯一（admins 与 users 不允许重名）：

    前台登录会先拦截管理员账户名——若两张表重名，该员工的员工账户将永远无法登录。
    """
    with _connect() as conn:
        clash = conn.execute(
            "SELECT 1 FROM admins WHERE username=?", (username,)).fetchone()
    if clash:
        raise ValueError("该账户名已被管理员占用")


def create_user(username: str, password: str, department_id: int | None = None,
                status: str = "active",
                permissions: list[str] | None = None,
                real_name: str = "", note: str = "") -> dict:
    username = _validate_username(username)
    _validate_password(password)
    _ensure_username_free(username)
    pw_hash, salt = hash_password(password)
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO users(username, password_hash, salt, department_id,"
                " status, permissions, real_name, note, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (username, pw_hash, salt, department_id, status,
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


def list_users(department_id: int | None = None) -> list[dict]:
    """员工列表（users 表只放员工；管理员在 admins 表，见 list_admins）。"""
    sql = ("SELECT u.id, u.username, u.department_id, u.status, "
           "u.created_at, u.permissions, u.real_name, u.note, "
           "d.name AS department FROM users u "
           "LEFT JOIN departments d ON d.id = u.department_id")
    where, params = [], []
    if department_id is not None:
        where.append("u.department_id=?")
        params.append(department_id)
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
    """前台登录验证（只查员工表）。返回 (用户信息, 错误信息)；成功时错误信息为空串。

    管理员不在 users 表里，天然登不上前台——但为给出明确提示，
    /auth/login 入口会用 admin_exists() 显式拦截（见 admin_app）。
    """
    user = get_user((username or "").strip())
    if user is None:
        return None, "账户或密码不正确"
    if not verify_password(password, user["salt"], user["password_hash"]):
        return None, "账户或密码不正确"
    if user["status"] != "active":
        return None, "该账户已被停用，请联系管理员"
    info = {"username": user["username"], "department": user["department"],
            "department_id": user["department_id"],
            "permissions": user.get("permissions") or [],
            "real_name": user.get("real_name") or "", "note": user.get("note") or ""}
    return info, ""


# ---------------------------------------------------------------- 管理员（独立于员工）
def admin_exists(username: str) -> bool:
    """前台登录硬性规则的判定入口：该账户名是否属于管理员。"""
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM admins WHERE username=?",
                            ((username or "").strip(),)).fetchone() is not None


def create_admin(username: str, password: str, is_super: bool = False,
                 real_name: str = "", note: str = "") -> dict:
    username = _validate_username(username)
    _validate_password(password)
    _ensure_username_free_admin_side(username)
    pw_hash, salt = hash_password(password)
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO admins(username, password_hash, salt, is_super,"
                " status, real_name, note, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (username, pw_hash, salt, 1 if is_super else 0, "active",
                 (real_name or "").strip(), (note or "").strip(), _now()),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError("该账户名已被占用") from exc
    admin = get_admin(username)
    assert admin is not None
    return admin


def _ensure_username_free_admin_side(username: str) -> None:
    with _connect() as conn:
        clash = conn.execute("SELECT 1 FROM users WHERE username=?",
                             (username,)).fetchone()
    if clash:
        raise ValueError("该账户名已被员工使用")


def _admin_info(row) -> dict:
    return {"id": row["id"], "username": row["username"],
            "is_super": bool(row["is_super"]), "status": row["status"],
            "real_name": row["real_name"] or "", "note": row["note"] or "",
            "created_at": row["created_at"]}


def get_admin(username: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM admins WHERE username=?",
                           ((username or "").strip(),)).fetchone()
    return _admin_info(row) if row else None


def get_admin_by_id(admin_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM admins WHERE id=?",
                           (admin_id,)).fetchone()
    return _admin_info(row) if row else None


def list_admins() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM admins ORDER BY id").fetchall()
    return [_admin_info(r) for r in rows]


def count_admins() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM admins").fetchone()["n"]


def update_admin(admin_id: int, status: str | None = None,
                 real_name: str | None = None, note: str | None = None) -> None:
    with _connect() as conn:
        if status is not None:
            if status not in ("active", "disabled"):
                raise ValueError("非法状态")
            conn.execute("UPDATE admins SET status=? WHERE id=?", (status, admin_id))
        if real_name is not None:
            conn.execute("UPDATE admins SET real_name=? WHERE id=?",
                         ((real_name or "").strip(), admin_id))
        if note is not None:
            conn.execute("UPDATE admins SET note=? WHERE id=?",
                         ((note or "").strip(), admin_id))


def reset_admin_password(admin_id: int, new_password: str) -> None:
    _validate_password(new_password)
    pw_hash, salt = hash_password(new_password)
    with _connect() as conn:
        cur = conn.execute("UPDATE admins SET password_hash=?, salt=? WHERE id=?",
                           (pw_hash, salt, admin_id))
        if cur.rowcount == 0:
            raise ValueError("管理员不存在")


def authenticate_admin(username: str, password: str) -> tuple[dict | None, str]:
    """管理后台登录验证（只查 admins 表）。"""
    admin = get_admin((username or "").strip())
    if admin is None:
        return None, "账户或密码不正确"
    row_pw = None
    with _connect() as conn:
        row_pw = conn.execute(
            "SELECT password_hash, salt FROM admins WHERE username=?",
            ((username or "").strip(),)).fetchone()
    if not verify_password(password, row_pw["salt"], row_pw["password_hash"]):
        return None, "账户或密码不正确"
    if admin["status"] != "active":
        return None, "该管理员账户已被停用"
    return admin, ""


def delete_admin(admin_id: int) -> None:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM admins WHERE id=?", (admin_id,))
        if cur.rowcount == 0:
            raise ValueError("管理员不存在")


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
    """验证 PAT：返回 {username, department, token_name}；无效/吊销/停用返回 None。

    令牌只发给员工（admins 表没有令牌）——MCP 身份永远按部门过滤。
    """
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
            "token_name": row["name"]}


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

    按身份分表：entry="admin"（管理后台）写 admin_login_logs，其余（前台）
    写 user_login_logs。日志失败绝不抛出——认证主流程不能被日志拖垮。
    """
    browser, os_name = parse_user_agent(user_agent)
    fp = make_fingerprint(user_agent, accept_language, ip)
    table = "admin_login_logs" if entry == "admin" else "user_login_logs"
    try:
        with _connect() as conn:
            conn.execute(
                f"INSERT INTO {table}(action, username, success, detail, department,"
                " entry, ip, user_agent, browser, os_name, fingerprint, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (action, username or "", 1 if success else 0, detail or "",
                 department or "", entry or "", ip or "", user_agent or "",
                 browser, os_name, fp, _now()),
            )
    except sqlite3.Error:
        pass


def _log_table(scope: str | None) -> str:
    """scope → 登录日志表：admins 侧查 admin_login_logs，其余查 user_login_logs。"""
    return "admin_login_logs" if scope == "admins" else "user_login_logs"


def _login_logs_where(username: str, action: str) -> tuple[str, list]:
    """登录日志的公共 WHERE 构造（列表与计数共用，保证分页总数一致）。"""
    where, params = [], []
    if username:
        where.append("username LIKE ?")
        params.append(f"%{username}%")
    if action:
        where.append("action=?")
        params.append(action)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params


def list_login_logs(limit: int = 200, username: str = "",
                    action: str = "", scope: str | None = None,
                    offset: int = 0) -> list[dict]:
    """scope="admins" 读管理员登录日志表，"users"/None 读员工表（分表后无 JOIN）。"""
    where_sql, params = _login_logs_where(username, action)
    sql = f"SELECT * FROM {_log_table(scope)}" + where_sql
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_login_logs(username: str = "", action: str = "",
                     scope: str | None = None) -> int:
    where_sql, params = _login_logs_where(username, action)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {_log_table(scope)}" + where_sql,
            params).fetchone()
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


def _qa_logs_where(username: str, entry: str,
                   department: str = "") -> tuple[str, list]:
    where, params = [], []
    if username:
        where.append("username LIKE ?")
        params.append(f"%{username}%")
    if entry:
        where.append("entry=?")
        params.append(entry)
    if department:
        where.append("department=?")
        params.append(department)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params


def list_qa_logs(limit: int = 100, username: str = "",
                 entry: str = "",
                 department: str = "", offset: int = 0) -> list[dict]:
    """RAG 使用日志（只有员工会产生——管理员已不能登录前台/MCP）。"""
    where_sql, params = _qa_logs_where(username, entry, department)
    sql = "SELECT * FROM qa_logs" + where_sql
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
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


def count_qa_logs(username: str = "", entry: str = "",
                  department: str = "") -> int:
    where_sql, params = _qa_logs_where(username, entry, department)
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM qa_logs" + where_sql, params).fetchone()
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

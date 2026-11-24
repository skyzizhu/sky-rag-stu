"""V3.7：用户体系数据层（SQLite）。

FastAPI 管理后台（8700）与 Streamlit 前台共用同一份数据与逻辑：
- departments / business_directions / users / admins / tokens / user_prefs 主数据表 +
  user_login_logs / admin_login_logs / admin_operation_logs / qa_logs 日志表
- V3.8 硬性规则：管理员与员工彻底分表——admins 独立存储，users 只放员工；
  管理员禁止登录前台（authenticate 只查员工表，前台登录入口显式拒绝管理员账户名）；
  登录日志按身份分表（user_login_logs / admin_login_logs，按 entry 路由）
- 每次操作独立短连接 + WAL（多线程两服务并发写安全）
- 密码 pbkdf2_hmac(sha256, 200_000 轮) + secrets 盐；令牌只存 SHA-256 哈希
- 账户名全局唯一：admins 与 users 不允许重名（否则前台登录会被管理员拦截规则挡住）
- 卡片 allowed_groups 存不可变的部门权限键（初始名称）；部门名称创建后不可编辑，名称保留归属。
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
import unicodedata
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path

from src.config import PROJECT_ROOT

AUTH_DIR = PROJECT_ROOT / "storage" / "auth"
DB_PATH = AUTH_DIR / "auth.db"
SECRET_PATH = AUTH_DIR / "admin_secret.key"

_PBKDF2_ROUNDS = 200_000
_USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fa5]{2,32}$", re.UNICODE)
_write_lock = threading.Lock()  # 建表等初始化写用；常规读写靠 WAL + 短事务
# 账户名全局唯一（admins+users 两张表）：跨表"检查+插入"必须原子，否则并发创建
# 可能在两张表各插入同名账户（各自表内 UNIQUE 拦不住跨表重名）。
# 所有写入都发生在 8700 单进程内，进程级锁即可。
_username_lock = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect(*, timeout: float = 15) -> sqlite3.Connection:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


PROJECT_DIRECTORIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_directories (
    domain TEXT NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    dirname TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (domain, project_id),
    UNIQUE (domain, dirname)
)
"""

_RETRIEVAL_ACL_SCHEMA = """
CREATE TABLE IF NOT EXISTS retrieval_acl_epoch (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


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
    permission_key TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS department_aliases (
    name TEXT PRIMARY KEY,
    department_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS business_directions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    direction_id INTEGER REFERENCES business_directions(id) ON DELETE RESTRICT,
    lead_department_id INTEGER REFERENCES departments(id) ON DELETE RESTRICT,
    note TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_departments (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE RESTRICT,
    PRIMARY KEY (project_id, department_id)
);
CREATE INDEX IF NOT EXISTS idx_projects_direction ON projects(direction_id);
CREATE INDEX IF NOT EXISTS idx_projects_lead_department ON projects(lead_department_id);
CREATE INDEX IF NOT EXISTS idx_project_departments_department ON project_departments(department_id);
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
CREATE TABLE IF NOT EXISTS document_user_revocations (
    username TEXT PRIMARY KEY,
    revoked_at TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS admin_operation_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER,
    username TEXT NOT NULL DEFAULT '',
    authenticated INTEGER NOT NULL DEFAULT 0,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    status_code INTEGER NOT NULL,
    success INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_operations_actor ON admin_operation_logs(username, id DESC);
CREATE INDEX IF NOT EXISTS idx_admin_operations_action ON admin_operation_logs(action, id DESC);
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
    usage TEXT NOT NULL DEFAULT '{}',  -- V3.9 JSON：tokens 输入/命中缓存/输出 + calls + estimated
    mcp_context TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qa_logs_time ON qa_logs(created_at DESC);

CREATE TABLE IF NOT EXISTS doc_access_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL DEFAULT '',
    department TEXT NOT NULL DEFAULT '',
    entry TEXT NOT NULL DEFAULT '',      -- mcp_http / mcp_stdio / web
    action TEXT NOT NULL DEFAULT '',     -- read_document / list_documents
    document_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}',   -- JSON（mode/page/结果规模等）
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_doc_access_time ON doc_access_logs(created_at DESC);
"""

INGEST_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    file_index INTEGER NOT NULL,
    username TEXT NOT NULL,
    actor_department TEXT NOT NULL,
    ip TEXT NOT NULL,
    user_agent TEXT NOT NULL DEFAULT '',
    browser TEXT NOT NULL DEFAULT '',
    os_name TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    entry TEXT NOT NULL CHECK(entry IN ('file', 'directory', 'web')),
    file_name TEXT NOT NULL,
    source_path TEXT NOT NULL,
    path TEXT NOT NULL,
    bytes INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    elapsed_ms INTEGER,
    outcome TEXT NOT NULL CHECK(outcome IN ('running', 'ingested', 'skipped', 'failed')),
    reason TEXT NOT NULL,
    requested TEXT NOT NULL,
    attributes TEXT NOT NULL,
    UNIQUE(batch_id, file_index)
);
CREATE INDEX IF NOT EXISTS idx_ingest_logs_user ON ingest_logs(username, id DESC);
CREATE INDEX IF NOT EXISTS idx_ingest_logs_entry ON ingest_logs(entry, id DESC);
CREATE INDEX IF NOT EXISTS idx_ingest_logs_outcome ON ingest_logs(outcome, id DESC);
CREATE INDEX IF NOT EXISTS idx_ingest_logs_time ON ingest_logs(started_at);
"""


def ensure_ingest_log_schema(conn: sqlite3.Connection) -> None:
    """Create the audit table or add browser fields without rewriting existing rows."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(ingest_logs)")}
    if not columns:
        conn.executescript(INGEST_LOG_SCHEMA)
        return
    fields = ("user_agent", "browser", "os_name", "fingerprint")
    if all(field in columns for field in fields):
        return
    # The admin service and frontend can both be the first to migrate an old database.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(ingest_logs)")}
        for field in fields:
            if field not in columns:
                conn.execute(f"ALTER TABLE ingest_logs ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")


def init_db() -> None:
    """建表（幂等）+ 存量迁移。两服务启动时各调一次。

    V3.9 qa_logs 补 usage 列（云端 LLM 消耗统计，老行默认 '{}'）。

    V3.8 分表迁移（一次性，事务内原子完成）：
    1. users.is_admin=1 的行 → 迁入 admins 表（密码哈希原样携带，登录不受影响），
       其访问令牌一并清除（管理员不再持有前台/MCP 令牌），随后从 users 删除；
    2. users 表 DROP 掉 is_admin / is_super 列（员工表不再有管理员概念）；
    3. 旧 login_logs 按 entry 拆入 user_login_logs / admin_login_logs 后删表；
    4. qa_logs 中管理员账户的历史记录清除（RAG 使用日志只反映员工行为）。
    """
    with _write_lock, _connect() as conn:
        conn.executescript(_SCHEMA)
        conn.execute(PROJECT_DIRECTORIES_SCHEMA)
        conn.execute(_RETRIEVAL_ACL_SCHEMA)
        ensure_ingest_log_schema(conn)
        _init_department_permissions(conn)
        # Add optional diagnostics to existing logs without rewriting old rows.
        qa_cols = {r["name"] for r in conn.execute("PRAGMA table_info(qa_logs)")}
        for column in ("usage", "mcp_context"):
            if column not in qa_cols:
                try:
                    conn.execute(f"ALTER TABLE qa_logs ADD COLUMN {column} TEXT NOT NULL DEFAULT '{{}}'")
                except sqlite3.OperationalError:
                    pass
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
def _department_name(name: str) -> str:
    name = unicodedata.normalize('NFKC', name).strip()
    if not 1 <= len(name) <= 32 or any(unicodedata.category(c).startswith('C') for c in name):
        raise ValueError('部门名长度需在 1～32 字符之间，不能包含控制字符')
    if name.casefold() == 'all':
        raise ValueError('all 是全员可见标识，不能作为部门名称')
    return name


def _department_fields(name: str, description: str) -> tuple[str, str]:
    name = _department_name(name)
    description = description.strip()
    if len(description) > 1000 or any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs'}
                                     for c in description if c not in '\n\r\t'):
        raise ValueError('备注最多 1000 个字符，不能包含无效控制字符')
    return name, description


def _reserve_department_alias(conn, name: str, department_id: int) -> None:
    owner = conn.execute('SELECT department_id FROM department_aliases WHERE name=?', (name,)).fetchone()
    if owner and owner['department_id'] != department_id:
        raise ValueError('部门名称已被其他部门使用（含历史名称），请更换')
    conn.execute('INSERT OR IGNORE INTO department_aliases(name,department_id) VALUES(?,?)', (name, department_id))


def _init_department_permissions(conn) -> None:
    """保留部门权限键和名称归属；移除不参与授权的拼音编号。"""
    conn.execute('BEGIN IMMEDIATE')
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(departments)')}
    if 'permission_key' not in columns:
        conn.execute("ALTER TABLE departments ADD COLUMN permission_key TEXT NOT NULL DEFAULT ''")
    rows = conn.execute('SELECT * FROM departments ORDER BY id').fetchall()
    for row in rows:
        key = row['permission_key'] or row['name']
        # 只移除旧编号映射，名称及实际权限键始终保留。
        if 'code' in columns and row['code'] and row['code'] not in (row['name'], key):
            conn.execute('DELETE FROM department_aliases WHERE name=? AND department_id=?',
                         (row['code'], row['id']))
        _reserve_department_alias(conn, row['name'], row['id'])
        _reserve_department_alias(conn, key, row['id'])
        conn.execute('UPDATE departments SET permission_key=? WHERE id=?', (key, row['id']))
    if 'code' in columns:
        conn.execute('DROP INDEX IF EXISTS idx_department_code')
        conn.execute('ALTER TABLE departments DROP COLUMN code')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_department_permission_key ON departments(permission_key)')


def create_department(name: str, description: str = '') -> dict:
    name, description = _department_fields(name, description)
    try:
        with _connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            cursor = conn.execute(
                'INSERT INTO departments(name,permission_key,description,created_at) VALUES(?,?,?,?)',
                (name, name, description, _now()))
            _reserve_department_alias(conn, name, cursor.lastrowid)
            row = conn.execute('SELECT * FROM departments WHERE id=?', (cursor.lastrowid,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise ValueError('同名部门已存在') from exc
    return dict(row)


def get_department(department_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute('SELECT * FROM departments WHERE id=?', (department_id,)).fetchone()
    return dict(row) if row else None


def department_permission_key(name: str | None = None, department_id: int | None = None) -> str | None:
    """授权按 ID/不可变键解析；无 ID 时按部门名称解析。"""
    if department_id is not None:
        with _connect() as conn:
            row = conn.execute('SELECT permission_key FROM departments WHERE id=?', (department_id,)).fetchone()
        return row['permission_key'] if row else None
    if not name or name == 'all':
        return name
    try:
        with _connect() as conn:
            row = conn.execute('SELECT d.permission_key FROM department_aliases a LEFT JOIN departments d '
                               'ON a.department_id=d.id WHERE a.name=?', (name,)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
        return name  # 首次个人模式 CLI 入库尚未初始化认证库。
    return row['permission_key'] if row else name


def normalize_department_groups(groups: list[str]) -> list[str]:
    # 已删除部门不能回退成全员可见，保留其孤立标识以继续限制访问。
    return list(dict.fromkeys(department_permission_key(group) or group for group in groups))


def department_label(group: str) -> str:
    if group == 'all':
        return group
    with _connect() as conn:
        row = conn.execute('SELECT d.name FROM department_aliases a JOIN departments d '
                           'ON a.department_id=d.id WHERE a.name=?', (group,)).fetchone()
    return row['name'] if row else group


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
        conn.execute("BEGIN IMMEDIATE")
        n = conn.execute("SELECT COUNT(*) AS n FROM users WHERE department_id=?",
                         (dep_id,)).fetchone()["n"]
        if n:
            raise ValueError(f"该部门下仍有 {n} 名员工，请先转移再删除")
        assigned = conn.execute(
            "SELECT 1 FROM projects WHERE lead_department_id=? "
            "UNION ALL SELECT 1 FROM project_departments WHERE department_id=? LIMIT 1",
            (dep_id, dep_id),
        ).fetchone()
        if assigned:
            raise ValueError("该部门仍关联项目，请先在项目管理中调整牵头或参与部门")
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
    pw_hash, salt = hash_password(password)
    with _username_lock:  # 跨表重名检查+插入原子化
        _ensure_username_free(username)
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
# 不在此列）。检索设置与知识库维护拆分为独立权限（V3.7.2）；领域管理为独立权限（V3.8）。
FRONT_PERMISSIONS = ("upload", "manage", "retrieval_settings", "maintenance", "plugins",
                     "domains")


def _valid_permissions(perms: list[str] | None) -> list[str]:
    if not perms:
        return []
    return [p for p in perms if p in FRONT_PERMISSIONS]


def get_user(username: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT u.*, d.name AS department, d.permission_key AS department_group FROM users u "
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
            "SELECT u.*, d.name AS department, d.permission_key AS department_group FROM users u "
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
           "d.name AS department, d.permission_key AS department_group FROM users u "
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


def document_user_revocations() -> dict[str, str]:
    with _connect() as conn:
        rows = conn.execute('SELECT username, revoked_at FROM document_user_revocations').fetchall()
    return {r['username']: r['revoked_at'] for r in rows}


@contextmanager
def document_acl_write(*, retain_if=None):
    """Serialize ACL writes and invalidate cross-process readers before changing Qdrant."""
    from contextlib import closing

    token = secrets.token_hex(16)
    with closing(_connect()) as conn, conn:
        conn.execute(_RETRIEVAL_ACL_SCHEMA)
        conn.execute("INSERT INTO retrieval_acl_epoch VALUES (?, '')", (token,))
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield token, lambda previous: conn.execute(
                "DELETE FROM retrieval_acl_epoch WHERE key=?", (previous,))
        finally:
            # Even a partially failed Qdrant write must retire the old cache generation.
            conn.execute("INSERT OR REPLACE INTO retrieval_acl_epoch VALUES ('revision', ?)", (token,))
            if retain_if is None or not retain_if(token):
                conn.execute("DELETE FROM retrieval_acl_epoch WHERE key=?", (token,))
            conn.commit()


def retrieval_acl_revision() -> str | None:
    """Return None while ACL changes are pending or their revision cannot be checked."""
    from contextlib import closing

    try:
        with closing(_connect()) as conn:
            rows = dict(conn.execute("SELECT key, value FROM retrieval_acl_epoch"))
    except (OSError, sqlite3.Error):
        return None
    revision = rows.pop("revision", "")
    scope = rows.pop("scope", "")
    return None if rows else f"{revision}:{scope}"


def touch_retrieval_scope(conn: sqlite3.Connection) -> None:
    """Invalidate retrieval scopes in the same transaction as a project or direction edit."""
    conn.execute(_RETRIEVAL_ACL_SCHEMA)
    conn.execute("INSERT OR REPLACE INTO retrieval_acl_epoch VALUES ('scope', ?)",
                 (secrets.token_hex(16),))


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
            if status == 'disabled':
                row = conn.execute('SELECT username FROM users WHERE id=?', (user_id,)).fetchone()
                if row:
                    conn.execute('INSERT OR REPLACE INTO document_user_revocations VALUES (?,?)',
                                 (row['username'], _now() + ':' + secrets.token_hex(8)))
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
            "department_id": user["department_id"], "department_group": user.get("department_group"),
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
    pw_hash, salt = hash_password(password)
    with _username_lock:  # 跨表重名检查+插入原子化
        _ensure_username_free_admin_side(username)
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
            "token_name": row["name"], "department_group": user.get("department_group"),
            "department_id": user["department_id"]}


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


# ---------------------------------------- 问答页预设问题（按用户提问次数自动归纳）
def get_frequent_questions(username: str, limit: int = 3, max_length: int = 100) -> list[str]:
    """按提问次数归纳该用户的高频问题（次数降序，同次数取最近一次），供问答页一键再问。

    只统计本人提出的非空问题；过长问题不适合做按钮，不参与归纳。
    无历史时返回空列表，界面回落到内置示例问题。
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT question, COUNT(*) AS n, MAX(id) AS latest FROM qa_logs "
            "WHERE username=? AND question != '' AND length(question) <= ? "
            "GROUP BY question ORDER BY n DESC, latest DESC LIMIT ?",
            (username, max_length, limit),
        ).fetchall()
    return [row["question"] for row in rows]


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


def _login_logs_where(username: str, action: str, login_only: bool = False) -> tuple[str, list]:
    """登录日志的公共 WHERE 构造（列表与计数共用，保证分页总数一致）。"""
    where, params = [], []
    if login_only:
        where.append("action IN ('login','logout')")
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
                    offset: int = 0, login_only: bool = False) -> list[dict]:
    """scope="admins" 读管理员登录日志表，"users"/None 读员工表（分表后无 JOIN）。"""
    where_sql, params = _login_logs_where(username, action, login_only)
    sql = f"SELECT * FROM {_log_table(scope)}" + where_sql
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_login_logs(username: str = "", action: str = "",
                     scope: str | None = None, login_only: bool = False) -> int:
    where_sql, params = _login_logs_where(username, action, login_only)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {_log_table(scope)}" + where_sql,
            params).fetchone()
    return row["n"]


# ---------------------------------------------------------------- RAG 使用日志
def log_qa_event(username: str, department: str, entry: str, question: str,
                 answer: str = "", sources: list | None = None,
                 filters: dict | None = None, top_k: int = 0,
                 elapsed: float = 0.0, usage: dict | None = None,
                 *, mcp_context: dict | None = None) -> None:
    """记录一次 RAG 使用（问答/检索/总结）。失败不抛出，不影响回答主流程。

    usage（V3.9）：云端 LLM 消耗 {"input","input_cached","output","calls","estimated"}。
    """
    slim_sources = [
        {"source": s.get("source"), "score": round(float(s.get("score", 0)), 4),
         **{key: s[key] for key in ("path", "document_id", "version") if s.get(key)}}
        for s in (sources or [])[:12]
    ]
    try:
        values = (username or "", department or "", entry or "", question or "",
                  answer or "", json.dumps(slim_sources, ensure_ascii=False),
                  json.dumps(filters or {}, ensure_ascii=False), int(top_k or 0),
                  float(elapsed or 0), json.dumps(usage or {}, ensure_ascii=False))
        context_column = ""
        if mcp_context is not None:
            context_column = ", mcp_context"
            values += (json.dumps(mcp_context, ensure_ascii=False),)
        placeholders = ",".join("?" for _ in range(len(values) + 1))
        with _connect() as conn:
            conn.execute(
                "INSERT INTO qa_logs(username, department, entry, question, answer,"
                f" sources, filters, top_k, elapsed, usage{context_column}, created_at)"
                f" VALUES({placeholders})",
                (*values, _now()),
            )
    except (sqlite3.Error, TypeError, ValueError):
        pass


# ---------------------------------------------------------------- 文档访问审计（V3.9）
def log_doc_access(username: str, department: str, entry: str, action: str,
                   document_id: str = "", source: str = "",
                   detail: dict | None = None) -> None:
    """记录一次对知识文档的访问（读取全文/分页片段/枚举目录）。失败不抛出。

    合规口径（方案 §9.2）：谁在哪次操作中看到了哪篇知识，必须可追溯——
    knowledge_search/qa 的命中已记录在 qa_logs.sources，这里只补文档级访问。
    """
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO doc_access_logs(username, department, entry, action,"
                " document_id, source, detail, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (username or "", department or "", entry or "", action or "",
                 document_id or "", source or "",
                 json.dumps(detail or {}, ensure_ascii=False), _now()),
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
    sql = ("SELECT id, username, department, entry, question,"
           " substr(answer, 1, 3000) AS answer, sources, filters, top_k, elapsed,"
           " usage, mcp_context, created_at FROM qa_logs" + where_sql)
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
        for field in ("usage", "mcp_context"):
            try:
                item[field] = json.loads(item.get(field) or "{}")
            except (json.JSONDecodeError, TypeError):
                item[field] = {}
        out.append(item)
    return out


def count_qa_logs(username: str = "", entry: str = "",
                  department: str = "") -> int:
    where_sql, params = _qa_logs_where(username, entry, department)
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM qa_logs" + where_sql, params).fetchone()
    return row["n"]


def llm_usage_page(username: str, since: str, page: int, page_size: int,
                   *, entry: str | None = None,
                   username_exact: bool = False) -> tuple[dict, list[dict], int, int]:
    """Aggregate all matching usage; paginate details in one snapshot.

    Username defaults to legacy LIKE matching; exact mode uses literal equality.
    An empty username selects all users. entry=None selects all entries, while
    entry='' selects only historical logs with an empty entry.
    """
    from contextlib import closing

    conditions, params = [], []
    if username:
        conditions.append("username=?" if username_exact else "username LIKE ?")
        params.append(username if username_exact else f"%{username}%")
    if entry is not None:
        conditions.append("entry=?")
        params.append(entry)
    if since:
        conditions.append("created_at >= ?")
        params.append(since)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    usage = """
        WITH matching AS (
            SELECT id, username, entry, created_at, substr(question, 1, 80) AS question,
                   CASE WHEN json_valid(usage) THEN usage ELSE '{}' END AS data
            FROM qa_logs
    """ + where + """
        ), usage_rows AS (
            SELECT id, username, entry, created_at, question,
                   CAST(COALESCE(json_extract(data, '$.calls'), 0) AS INTEGER) AS calls,
                   CAST(COALESCE(json_extract(data, '$.input'), 0) AS INTEGER) AS input,
                   CAST(COALESCE(json_extract(data, '$.input_cached'), 0) AS INTEGER) AS input_cached,
                   CAST(COALESCE(json_extract(data, '$.output'), 0) AS INTEGER) AS output,
                   CAST(COALESCE(json_extract(data, '$.estimated'), 0) AS INTEGER) AS estimated
            FROM matching
        )
    """
    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN")
        totals = dict(conn.execute(usage + """
            SELECT COUNT(*) AS total_rows, COALESCE(SUM(calls), 0) AS calls,
                   COALESCE(SUM(input), 0) AS input,
                   COALESCE(SUM(input_cached), 0) AS input_cached,
                   COALESCE(SUM(output), 0) AS output,
                   COALESCE(SUM(max(0, input - input_cached)), 0) AS uncached
            FROM usage_rows WHERE calls > 0
        """, params).fetchone())
        total_pages = max(1, (totals["total_rows"] + page_size - 1) // page_size)
        page = min(page, total_pages)
        rows = conn.execute(usage + """
            SELECT username, entry, created_at, question, calls, input, input_cached, output, estimated
            FROM usage_rows WHERE calls > 0 ORDER BY id DESC LIMIT ? OFFSET ?
        """, [*params, page_size, (page - 1) * page_size]).fetchall()
    return totals, [dict(row) for row in rows], page, total_pages


def llm_usage_filter_options() -> dict:
    """Return current and historical users plus raw entries from all QA logs."""
    from contextlib import closing

    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN")
        users = conn.execute("""
            WITH usernames AS (
                SELECT username FROM users
                UNION
                SELECT username FROM qa_logs WHERE username != ''
            )
            SELECT usernames.username, COALESCE(users.real_name, '') AS real_name
            FROM usernames LEFT JOIN users ON users.username = usernames.username
            ORDER BY usernames.username
        """).fetchall()
        entries = conn.execute(
            "SELECT DISTINCT entry FROM qa_logs ORDER BY entry").fetchall()
    return {"users": [dict(row) for row in users],
            "entries": [row["entry"] for row in entries]}


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

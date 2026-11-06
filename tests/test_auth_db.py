"""auth_db 数据层：分表、硬规则、全局唯一、令牌、日志路由、旧库迁移。

每个测试用例独立临时库（tests._helpers.TempAuthDB），绝不触碰真实数据。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import auth_db
from tests._helpers import TempAuthDB


class AdminEmployeeSeparationTests(TempAuthDB):
    def test_users_table_has_no_admin_columns(self):
        with auth_db._connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        self.assertNotIn("is_admin", cols)
        self.assertNotIn("is_super", cols)
        self.assertFalse(auth_db.admin_exists("admin"))  # 全新库无管理员

    def test_create_admin_lands_in_admins_table_not_users(self):
        admin = auth_db.create_admin("boss", "secret1", is_super=True)
        self.assertEqual(auth_db.list_admins(), [admin])
        self.assertEqual([u["username"] for u in auth_db.list_users()], [])
        self.assertTrue(auth_db.admin_exists("boss"))
        self.assertFalse(auth_db.admin_exists("nope"))


class GlobalUsernameUniquenessTests(TempAuthDB):
    def test_employee_cannot_take_admin_name(self):
        auth_db.create_admin("admin", "secret1")
        with self.assertRaisesRegex(ValueError, "已被管理员占用"):
            auth_db.create_user("admin", "secret1")

    def test_admin_cannot_take_employee_name(self):
        self.make_employee("alice")
        with self.assertRaisesRegex(ValueError, "已被员工使用"):
            auth_db.create_admin("alice", "secret1")

    def test_same_table_duplicate_still_rejected(self):
        self.make_employee("alice")
        with self.assertRaises(ValueError):
            self.make_employee("alice")


class HardRuleTests(TempAuthDB):
    """前台登录硬性规则：管理员账户一律进不去（authenticate 只查员工表）。"""

    def test_admin_cannot_authenticate_even_with_correct_password(self):
        auth_db.create_admin("boss", "right-pw")
        info, err = auth_db.authenticate("boss", "right-pw")
        self.assertIsNone(info)
        self.assertTrue(err)

    def test_authenticate_admin_only_works_in_admins_table(self):
        admin = auth_db.create_admin("boss", "right-pw")
        info, err = auth_db.authenticate_admin("boss", "right-pw")
        self.assertEqual(err, "")  # 约定：成功时错误信息为空串
        self.assertEqual(info["username"], admin["username"])
        # 员工密码在管理后台同样无效
        self.make_employee("alice", "emp-pw")
        info, err = auth_db.authenticate_admin("alice", "emp-pw")
        self.assertIsNone(info)
        self.assertTrue(err)

    def test_disabled_admin_cannot_authenticate_admin(self):
        admin = auth_db.create_admin("boss", "right-pw")
        auth_db.update_admin(admin["id"], status="disabled")
        info, err = auth_db.authenticate_admin("boss", "right-pw")
        self.assertIsNone(info)
        self.assertIn("停用", err)

    def test_disabled_employee_cannot_authenticate_front(self):
        emp = self.make_employee("alice", "emp-pw")
        auth_db.update_user(emp["id"], status="disabled")
        info, err = auth_db.authenticate("alice", "emp-pw")
        self.assertIsNone(info)
        self.assertIn("停用", err)


class TokenTests(TempAuthDB):
    def test_token_lifecycle_and_employee_only_identity(self):
        emp = self.make_employee("alice")
        plaintext = auth_db.create_token(emp["id"], "测试")
        self.assertTrue(plaintext.startswith("mcp_pat_"))
        ident = auth_db.verify_token(plaintext)
        self.assertEqual(ident["username"], "alice")
        self.assertEqual(ident["department"], "hr")
        self.assertNotIn("is_admin", ident)  # 分表后令牌身份没有管理员概念
        auth_db.revoke_token(plaintext[:16])
        self.assertIsNone(auth_db.verify_token(plaintext))

    def test_password_reset_revokes_tokens(self):
        emp = self.make_employee("alice")
        plaintext = auth_db.create_token(emp["id"])
        auth_db.reset_password(emp["id"], "new-pw-9")
        self.assertIsNone(auth_db.verify_token(plaintext))

    def test_disabled_employee_token_stops_working(self):
        # 管理员停用员工 → 其 MCP 令牌必须立即失效（产品真实路径：后台「停用」）
        emp = self.make_employee("alice")
        plaintext = auth_db.create_token(emp["id"])
        auth_db.update_user(emp["id"], status="disabled")
        self.assertIsNone(auth_db.verify_token(plaintext))
        auth_db.update_user(emp["id"], status="active")
        self.assertIsNotNone(auth_db.verify_token(plaintext))  # 重新启用后恢复


class LoginLogRoutingTests(TempAuthDB):
    def test_entry_routes_to_separate_tables(self):
        auth_db.log_auth_event("login", "alice", entry="web")
        auth_db.log_auth_event("login", "boss", entry="admin")
        web = auth_db.list_login_logs(scope="users")
        adm = auth_db.list_login_logs(scope="admins")
        self.assertEqual([l["username"] for l in web], ["alice"])
        self.assertEqual([l["username"] for l in adm], ["boss"])
        self.assertEqual(auth_db.count_login_logs(scope="users"), 1)
        self.assertEqual(auth_db.count_login_logs(scope="admins"), 1)

    def test_tables_have_time_indexes(self):
        with auth_db._connect() as conn:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_user_login_logs_time", names)
        self.assertIn("idx_admin_login_logs_time", names)

    def test_legacy_login_logs_table_migrated_and_dropped(self):
        auth_db.log_auth_event("login", "x", entry="web")
        with auth_db._connect() as conn:
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("login_logs", tables)
        self.assertIn("user_login_logs", tables)
        self.assertIn("admin_login_logs", tables)


class SessionCookieAndProofTests(TempAuthDB):
    def test_cookie_roundtrip_and_expiry(self):
        cookie = auth_db.make_session_cookie("alice")
        self.assertEqual(auth_db.verify_session_cookie(cookie), "alice")
        self.assertIsNone(auth_db.verify_session_cookie("garbage"))
        self.assertIsNone(auth_db.verify_session_cookie(""))

    def test_proof_is_single_use(self):
        proof = auth_db.make_login_proof("alice", ttl=60)
        self.assertEqual(auth_db.consume_login_proof(proof), "alice")
        self.assertIsNone(auth_db.consume_login_proof(proof))  # 重放被拒
        self.assertIsNone(auth_db.consume_login_proof("forged|123|abc"))


class LegacyMigrationTests(TempAuthDB):
    """V3.8 分表迁移：旧结构（users.is_admin + login_logs）自动升级且数据无损。"""

    def _build_legacy_db(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "legacy.db"
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE departments (id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE, description TEXT NOT NULL DEFAULT '',
                disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
            CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, salt TEXT NOT NULL,
                department_id INTEGER REFERENCES departments(id),
                is_admin INTEGER NOT NULL DEFAULT 0, is_super INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active', permissions TEXT NOT NULL DEFAULT '[]',
                real_name TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL);
            CREATE TABLE tokens (token_hash TEXT PRIMARY KEY,
                token_prefix TEXT NOT NULL DEFAULT '',
                user_id INTEGER NOT NULL REFERENCES users(id),
                name TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                last_used TEXT NOT NULL DEFAULT '', revoked INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE user_prefs (username TEXT PRIMARY KEY, settings_json TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE login_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '', success INTEGER NOT NULL DEFAULT 1,
                detail TEXT NOT NULL DEFAULT '', department TEXT NOT NULL DEFAULT '',
                entry TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '', browser TEXT NOT NULL DEFAULT '',
                os_name TEXT NOT NULL DEFAULT '', fingerprint TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL);
            CREATE TABLE qa_logs (id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL DEFAULT '', department TEXT NOT NULL DEFAULT '',
                entry TEXT NOT NULL DEFAULT '', question TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '', sources TEXT NOT NULL DEFAULT '[]',
                filters TEXT NOT NULL DEFAULT '{}', top_k INTEGER NOT NULL DEFAULT 0,
                elapsed REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
            INSERT INTO departments(name, created_at) VALUES('hr', '2026-01-01');
            INSERT INTO login_logs(action, username, entry, created_at) VALUES
                ('login', 'oldboss', 'admin', '2026-01-03'),
                ('login', 'alice', 'web', '2026-01-04');
            INSERT INTO qa_logs(username, question, created_at) VALUES
                ('oldboss', '管理员的历史提问', '2026-01-05'),
                ('alice', '员工的历史提问', '2026-01-06');
        """)
        # 真实密码哈希：迁移后要能用原密码登录管理后台（"密码原样携带"）
        boss_hash, boss_salt = auth_db.hash_password("old-admin-pw")
        alice_hash, alice_salt = auth_db.hash_password("alice-pw")
        conn.execute(
            "INSERT INTO users(username, password_hash, salt, is_admin, is_super, status, created_at)"
            " VALUES('oldboss', ?, ?, 1, 1, 'active', '2026-01-01')", (boss_hash, boss_salt))
        conn.execute(
            "INSERT INTO users(username, password_hash, salt, department_id, is_admin, status,"
            " permissions, created_at) VALUES('alice', ?, ?, 1, 0, 'active', '[\"upload\"]', '2026-01-02')",
            (alice_hash, alice_salt))
        # 管理员名下的旧令牌（分表后应被清除）+ 员工令牌（应保留）
        conn.execute("INSERT INTO tokens(token_hash, user_id, created_at) VALUES('t-admin', 1, '2026-01-01')")
        conn.execute("INSERT INTO tokens(token_hash, user_id, created_at) VALUES('t-alice', 2, '2026-01-01')")
        conn.commit()
        conn.close()
        return db_path

    def test_migration_moves_admins_splits_logs_cleans_qa(self):
        db_path = self._build_legacy_db()
        # with 语句保证 patch 先于 setUp 的 patch 还原（LIFO），不泄漏到其他测试
        with mock.patch.object(auth_db, "DB_PATH", db_path):
            auth_db.init_db()

            with auth_db._connect() as conn:
                users_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
                token_hashes = {r["token_hash"] for r in conn.execute("SELECT token_hash FROM tokens")}
            self.assertNotIn("is_admin", users_cols)
            self.assertNotIn("is_super", users_cols)

            # 管理员迁入 admins（超管标记保留），users 只剩员工
            admins = auth_db.list_admins()
            self.assertEqual([a["username"] for a in admins], ["oldboss"])
            self.assertTrue(admins[0]["is_super"])
            self.assertEqual([u["username"] for u in auth_db.list_users()], ["alice"])

            # 密码原样携带：迁移后原密码登录管理后台成功；前台被硬规则挡住
            info, err = auth_db.authenticate_admin("oldboss", "old-admin-pw")
            self.assertEqual(err, "")
            self.assertIsNone(auth_db.authenticate("oldboss", "old-admin-pw")[0])
            self.assertIsNotNone(auth_db.authenticate("alice", "alice-pw")[0])

            # 管理员旧令牌清除、员工令牌保留
            self.assertEqual(token_hashes, {"t-alice"})

            # 登录日志按 entry 拆表
            self.assertEqual([l["username"] for l in auth_db.list_login_logs(scope="admins")],
                             ["oldboss"])
            self.assertEqual([l["username"] for l in auth_db.list_login_logs(scope="users")],
                             ["alice"])

            # qa_logs 只保留员工记录
            self.assertEqual([q["question"] for q in auth_db.list_qa_logs()], ["员工的历史提问"])

            # 迁移幂等：再跑一次不重复、不报错
            auth_db.init_db()
            self.assertEqual(len(auth_db.list_admins()), 1)
            self.assertEqual(len(auth_db.list_users()), 1)


if __name__ == "__main__":
    unittest.main()

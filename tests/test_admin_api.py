"""管理后台 / 认证 API（FastAPI TestClient，真实 HTTP 处理链路 + 临时库）。"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from src import admin_app, auth_db
from tests._helpers import TempAuthDB


class ApiTestCase(TempAuthDB):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(admin_app.app)
        self.client.__enter__()  # 触发 startup（init_db / 签名密钥，均落在临时目录）
        self.addCleanup(self.client.__exit__, None, None, None)

    def login_admin(self, username: str = "boss", password: str = "admin-pw", **kw):
        if not auth_db.admin_exists(username):
            auth_db.create_admin(username, password, **kw)
        resp = self.client.post("/admin/login", json={"username": username, "password": password},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        return resp


class FrontLoginHardRuleTests(ApiTestCase):
    def test_admin_rejected_with_correct_password(self):
        auth_db.create_admin("boss", "admin-pw")
        resp = self.client.post("/auth/login", json={"username": "boss", "password": "admin-pw"})
        self.assertEqual(resp.status_code, 403)
        self.assertIn("管理员账户不能登录前台", resp.json()["detail"])

    def test_admin_rejected_even_with_wrong_password(self):
        auth_db.create_admin("boss", "admin-pw")
        resp = self.client.post("/auth/login", json={"username": " boss ", "password": "nope"})
        self.assertEqual(resp.status_code, 403)  # 先拦截，与密码对错无关；用户名前后空格也拦

    def test_employee_login_returns_user_and_single_use_proof(self):
        self.make_employee("alice", "emp-pw")
        resp = self.client.post("/auth/login", json={"username": "alice", "password": "emp-pw"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["user"]["username"], "alice")
        self.assertNotIn("is_admin", body["user"])
        self.assertEqual(auth_db.consume_login_proof(body["login_proof"]), "alice")

    def test_wrong_password_and_disabled_employee(self):
        emp = self.make_employee("alice", "emp-pw")
        self.assertEqual(self.client.post("/auth/login", json={"username": "alice", "password": "x"}).status_code, 401)
        auth_db.update_user(emp["id"], status="disabled")
        self.assertEqual(self.client.post("/auth/login", json={"username": "alice", "password": "emp-pw"}).status_code, 401)

    def test_admin_login_attempt_logged_on_employee_side(self):
        auth_db.create_admin("boss", "admin-pw")
        self.client.post("/auth/login", json={"username": "boss", "password": "admin-pw"})
        logs = auth_db.list_login_logs(scope="users")
        self.assertEqual(logs[0]["username"], "boss")
        self.assertEqual(logs[0]["success"], 0)


class RegisterTests(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.dep = self.make_department("hr")

    def _register(self, username="newbie", p1="secret1", p2="secret1", dep_id=None):
        return self.client.post("/auth/register", json={
            "username": username, "password": p1, "password_confirm": p2,
            "department_id": dep_id or self.dep["id"]})

    def test_success(self):
        resp = self._register()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["department"], "hr")

    def test_admin_name_rejected_and_logged(self):
        auth_db.create_admin("boss", "admin-pw")
        resp = self._register(username="boss")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("管理员占用", resp.json()["detail"])
        self.assertEqual(auth_db.list_login_logs(scope="users")[0]["action"], "register")

    def test_password_mismatch(self):
        self.assertEqual(self._register(p2="different").status_code, 400)

    def test_disabled_department_rejected(self):
        auth_db.set_department_disabled(self.dep["id"], True)
        self.assertEqual(self._register().status_code, 400)

    def test_duplicate_username(self):
        self._register()
        self.assertEqual(self._register().status_code, 400)


class BootstrapTests(ApiTestCase):
    def test_bootstrap_only_once_and_creates_super_admin(self):
        self.assertEqual(self.client.get("/auth/bootstrap").json()["has_admins"], False)
        body = {"username": "first", "password": "secret1", "password_confirm": "secret1"}
        self.assertEqual(self.client.post("/auth/bootstrap-admin", json=body).status_code, 200)
        admin = auth_db.get_admin("first")
        self.assertTrue(admin["is_super"])
        self.assertEqual(auth_db.list_users(), [])  # 进 admins 表，不进员工表
        boot = self.client.get("/auth/bootstrap").json()
        self.assertEqual((boot["has_admins"], boot["has_users"]), (True, False))
        second = dict(body, username="second")
        self.assertEqual(self.client.post("/auth/bootstrap-admin", json=second).status_code, 400)


class AdminConsoleAuthTests(ApiTestCase):
    def test_employee_cannot_login_console(self):
        self.make_employee("alice", "emp-pw")
        resp = self.client.post("/admin/login", json={"username": "alice", "password": "emp-pw"},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 401)

    def test_api_requires_session(self):
        self.assertEqual(self.client.get("/api/overview").status_code, 401)
        self.client.cookies.set("admin_sess", "9999999999|boss|forged-signature")
        self.assertEqual(self.client.get("/api/overview").status_code, 401)

    def test_disabled_admin_session_rejected(self):
        self.login_admin("boss")
        self.assertEqual(self.client.get("/api/overview").status_code, 200)
        auth_db.update_admin(auth_db.get_admin("boss")["id"], status="disabled")
        self.assertEqual(self.client.get("/api/overview").status_code, 401)

    def test_overview_counts_tables_separately(self):
        self.login_admin("boss")
        self.make_employee("alice")
        data = self.client.get("/api/overview").json()
        self.assertEqual((data["admins"], data["users"]), (1, 1))


class AdminManagementTests(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.login_admin("boss", is_super=True)

    def test_create_admin_with_employee_name_rejected(self):
        self.make_employee("alice")
        resp = self.client.post("/api/admins", json={
            "username": "alice", "password": "secret1", "password_confirm": "secret1"})
        self.assertEqual(resp.status_code, 400)

    def test_super_and_self_protections(self):
        boss_id = auth_db.get_admin("boss")["id"]
        self.assertEqual(self.client.post(f"/api/admins/{boss_id}/toggle").status_code, 400)
        self.assertEqual(self.client.delete(f"/api/admins/{boss_id}").status_code, 400)

    def test_reset_admin_password_then_login_with_new(self):
        self.client.post("/api/admins", json={
            "username": "second", "password": "secret1", "password_confirm": "secret1"})
        sid = auth_db.get_admin("second")["id"]
        self.assertEqual(self.client.post(f"/api/admins/{sid}/reset",
                                          json={"new_password": "brand-new"}).status_code, 200)
        info, err = auth_db.authenticate_admin("second", "brand-new")
        self.assertEqual(err, "")

    def test_employee_permissions_whitelisted(self):
        emp = self.make_employee("alice")
        resp = self.client.patch(f"/api/users/{emp['id']}/permissions",
                                 json={"permissions": ["upload", "superpower", "manage"]})
        self.assertEqual(resp.json()["permissions"], ["upload", "manage"])

    def test_login_logs_routed_by_side(self):
        self.make_employee("alice", "emp-pw")
        self.client.post("/auth/login", json={"username": "alice", "password": "emp-pw"})
        users_side = self.client.get("/api/logs/auth?scope=users").json()["logs"]
        admins_side = self.client.get("/api/logs/auth?scope=admins").json()["logs"]
        self.assertEqual({l["username"] for l in users_side}, {"alice"})
        self.assertIn("boss", {l["username"] for l in admins_side})
        self.assertNotIn("alice", {l["username"] for l in admins_side})


class PersistLoginTests(ApiTestCase):
    def test_proof_single_use_and_admin_never_gets_front_cookie(self):
        self.make_employee("alice")
        proof = auth_db.make_login_proof("alice")
        first = self.client.get("/auth/persist-login",
                                params={"username": "alice", "proof": proof, "back": "/"})
        self.assertEqual(first.status_code, 200)
        self.assertIn("front_auth", first.headers.get("set-cookie", ""))
        replay = self.client.get("/auth/persist-login",
                                 params={"username": "alice", "proof": proof, "back": "/"})
        self.assertEqual(replay.status_code, 401)

        auth_db.create_admin("boss", "admin-pw")
        admin_proof = auth_db.make_login_proof("boss")
        resp = self.client.get("/auth/persist-login",
                               params={"username": "boss", "proof": admin_proof, "back": "/"})
        self.assertEqual(resp.status_code, 401)  # 管理员拿不到前台登录态

    def test_open_redirect_blocked(self):
        self.make_employee("alice")
        proof = auth_db.make_login_proof("alice")
        resp = self.client.get("/auth/persist-login",
                               params={"username": "alice", "proof": proof,
                                       "back": "https://evil.example/"})
        self.assertNotIn("evil.example", resp.text)


class CorsAndCsrfTests(ApiTestCase):
    """③ 跨域收紧 + 同站跨端口 CSRF 防护。TestClient 的 Host 为 testserver。"""

    def setUp(self) -> None:
        super().setUp()
        self.login_admin("boss", is_super=True)
        self.dep = self.make_department("hr")

    def _toggle(self, **headers):
        return self.client.post(f"/api/departments/{self.dep['id']}/toggle", headers=headers)

    def _disabled(self) -> bool:
        return bool(next(d for d in auth_db.list_departments() if d["id"] == self.dep["id"])["disabled"])

    def test_ping_keeps_credentialed_cors_for_login_iframe(self):
        resp = self.client.get("/auth/ping", headers={"Origin": "null"})
        self.assertEqual(resp.headers.get("access-control-allow-origin"), "null")
        self.assertEqual(resp.headers.get("access-control-allow-credentials"), "true")

    def test_admin_api_sends_no_cors_headers_to_foreign_origin(self):
        resp = self.client.get("/api/overview", headers={"Origin": "http://evil.example"})
        self.assertNotIn("access-control-allow-origin", resp.headers)

    def test_preflight_for_admin_api_not_granted(self):
        resp = self.client.options("/api/admins", headers={
            "Origin": "http://testserver:9999", "Access-Control-Request-Method": "POST"})
        self.assertNotIn("access-control-allow-origin", resp.headers)

    def test_same_site_other_port_write_blocked(self):
        # 同主机其他端口的网页：同站 → 浏览器会带 Cookie；必须被同源校验挡下
        resp = self._toggle(Origin="http://testserver:9999")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(self._disabled(), "被拒的请求不得产生副作用")

    def test_cross_site_write_blocked(self):
        self.assertEqual(self._toggle(Origin="https://evil.example").status_code, 403)
        self.assertEqual(self._toggle(Origin="null").status_code, 403)

    def test_same_origin_write_allowed(self):
        self.assertEqual(self._toggle(Origin="http://testserver").status_code, 200)
        self.assertTrue(self._disabled())

    def test_no_origin_header_allowed_for_server_side_callers(self):
        self.assertEqual(self._toggle().status_code, 200)

    def test_reverse_proxy_forwarded_host_allowed(self):
        resp = self._toggle(Origin="https://rag.example.com",
                            **{"X-Forwarded-Host": "rag.example.com"})
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()

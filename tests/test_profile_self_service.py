"""个人中心自助接口（改密 / 编辑姓名职责）的隔离测试（Windows 可直跑，不触正式库）。"""

import unittest

from fastapi import HTTPException

from src import admin_app, auth_db
from tests._helpers import TempAuthDB


class ProfileSelfServiceTests(TempAuthDB):
    def setUp(self) -> None:
        super().setUp()
        auth_db.init_db()
        self.dep = auth_db.create_department("hr")
        self.user = auth_db.create_user("worker", "old-pass-1", department_id=self.dep["id"],
                                        real_name="老名字", note="旧职责")
        self.uid = self.user["id"]

    def _register(self, username="worker", **extra):
        body = admin_app.RegisterBody(
            username=username, password="whatever-1", password_confirm="whatever-1",
            department_id=self.dep["id"], client_ip="127.0.0.1", **extra)
        return admin_app.api_register(body, request=None)

    def _change_password(self, old="old-pass-1", new="new-pass-9", confirm=None, username="worker"):
        body = admin_app.ChangePasswordBody(
            username=username, old_password=old, new_password=new,
            new_password_confirm=confirm or new, client_ip="127.0.0.1")
        return admin_app.api_change_password(body, request=None)

    def _profile(self, real_name="新名字", note="新职责", username="worker"):
        body = admin_app.SelfProfileBody(
            username=username, real_name=real_name, note=note, client_ip="127.0.0.1")
        return admin_app.api_self_profile(body, request=None)

    def test_change_password_requires_correct_old_password(self):
        resp = self._change_password(old="wrong-old", new="new-pass-9")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("原密码不正确", resp.body.decode("utf-8"))
        self.assertIsNotNone(auth_db.authenticate("worker", "old-pass-1")[0])  # 旧密码仍有效

    def test_change_password_success_swaps_credentials_and_revokes_tokens(self):
        auth_db.create_token(self.uid, "我的令牌")  # 既有令牌应在改密后被吊销
        resp = self._change_password()
        self.assertEqual(resp["ok"], True)
        self.assertIsNone(auth_db.authenticate("worker", "old-pass-1")[0])
        self.assertIsNotNone(auth_db.authenticate("worker", "new-pass-9")[0])
        with auth_db._connect() as conn:
            active = conn.execute(
                "SELECT COUNT(*) AS n FROM tokens WHERE user_id=? AND revoked=0",
                (self.uid,)).fetchone()["n"]
        self.assertEqual(active, 0)  # 该员工的全部令牌已被吊销

    def test_change_password_mismatch_and_same_as_old_rejected(self):
        self.assertEqual(self._change_password(confirm="different").status_code, 400)
        self.assertEqual(self._change_password(new="old-pass-1", confirm="old-pass-1").status_code, 400)

    def test_change_password_too_short_rejected(self):
        resp = self._change_password(new="abc", confirm="abc")
        self.assertEqual(resp.status_code, 400)

    def test_profile_update_persists_and_strips(self):
        resp = self._profile(real_name="  王五 ", note="  现场工程师 ")
        self.assertEqual(resp["real_name"], "王五")
        self.assertEqual(resp["note"], "现场工程师")
        self.assertEqual(auth_db.get_user("worker")["real_name"], "王五")

    def test_profile_and_password_rejected_for_disabled_user(self):
        auth_db.update_user(self.uid, status="disabled")
        self.assertEqual(self._profile().status_code, 401)
        self.assertEqual(self._change_password().status_code, 401)

    def test_unknown_user_rejected(self):
        self.assertEqual(self._profile(username="ghost").status_code, 401)
        self.assertEqual(self._change_password(username="ghost").status_code, 401)


if __name__ == "__main__":
    unittest.main()

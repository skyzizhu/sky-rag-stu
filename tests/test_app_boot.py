"""② 登录页不依赖向量库：Qdrant 宕机时访客仍能看到登录页、已登录用户看到友好提示。

用 Streamlit 官方 AppTest 在测试进程内真实执行 app.py：
- 向量库初始化被 mock 成抛 VectorStoreError（模拟 Qdrant 宕机）
- 认证服务的 bootstrap / 偏好接口被 mock（测试不依赖 8700 服务在线）
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

from src.i18n import LANGUAGE_ORDER, _CATALOG
from src.vector_store import VectorStoreError

APP = str(Path(__file__).resolve().parent.parent / "app.py")
BOOT = {"has_users": True, "has_admins": True, "departments": [{"id": 1, "name": "hr"}]}
UNAVAILABLE_TEXTS = {_CATALOG[lang]["services.unavailable"] for lang in LANGUAGE_ORDER}


def _qdrant_down(*_args, **_kwargs):
    raise VectorStoreError("连不上 Qdrant（http://localhost:6333）：模拟宕机")


class LoginPageWithoutVectorStoreTests(unittest.TestCase):
    def _run(self, auth_user: dict | None = None) -> AppTest:
        with mock.patch("src.vector_store.VectorStore.__init__", _qdrant_down), \
             mock.patch("src.auth_client.bootstrap", return_value=BOOT), \
             mock.patch("src.auth_client.get_prefs", return_value={}):
            at = AppTest.from_file(APP, default_timeout=90)
            at.query_params["rip"] = "1"  # 跳过登录门的 IP 探测 iframe
            if auth_user:
                at.session_state["auth_user"] = auth_user
            at.run()
        return at

    def test_visitor_sees_login_form_when_qdrant_down(self):
        at = self._run()
        self.assertEqual(len(at.exception), 0,
                         f"不应出现报错栈：{[e.value for e in at.exception]}")
        self.assertGreaterEqual(len(at.text_input), 2)  # 账户名 + 密码
        self.assertFalse(any(e.value in UNAVAILABLE_TEXTS for e in at.error),
                         "未登录时不应触达知识库服务")

    def test_logged_in_user_gets_friendly_error_not_traceback(self):
        at = self._run(auth_user={"username": "alice", "department": "hr",
                                  "department_id": 1, "permissions": [],
                                  "real_name": "", "note": ""})
        self.assertEqual(len(at.exception), 0,
                         f"不应把报错栈暴露给浏览器：{[e.value for e in at.exception]}")
        self.assertTrue(any(e.value in UNAVAILABLE_TEXTS for e in at.error),
                        f"应显示友好提示，实际 error 元素：{[e.value for e in at.error]}")


if __name__ == "__main__":
    unittest.main()

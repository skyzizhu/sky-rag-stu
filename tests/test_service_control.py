"""单服务控制 API：临时认证库，模拟脚本执行，不停止真实服务。"""

from unittest import mock

from src import admin_app
from tests.test_admin_api import ApiTestCase


class ServiceControlTests(ApiTestCase):
    def tearDown(self):
        # SQLite 连接的上下文管理只提交事务；Windows 清理临时库前先回收连接。
        import gc

        gc.collect()
        super().tearDown()

    def test_actions_require_admin_session(self):
        with mock.patch.object(admin_app, "_run_service_script") as run:
            for key in ("front", "admin", "qdrant", "ollama"):
                for action in ("start", "stop"):
                    self.assertEqual(self.client.post(f"/api/services/{key}/{action}").status_code, 401)
            run.assert_not_called()

    def test_service_list_has_stable_keys_and_configured_model(self):
        self.login_admin()
        with mock.patch("requests.get", return_value=mock.Mock(status_code=200)), \
                mock.patch("src.config.get_config", return_value=mock.Mock(embedding_model="configured-model")):
            response = self.client.get("/api/services")
        self.assertEqual(response.status_code, 200)
        services = response.json()["services"]
        self.assertEqual([s["key"] for s in services], ["front", "admin", "qdrant", "ollama"])
        self.assertEqual(services[-1]["name"], "Ollama-向量模型 configured-model（11434）")
        self.assertTrue(all(s["ok"] for s in services))

    def test_down_service_is_reported(self):
        self.login_admin()
        import requests

        with mock.patch("requests.get", side_effect=requests.ConnectionError("offline")):
            services = self.client.get("/api/services").json()["services"]
        self.assertTrue(all(not s["ok"] for s in services))

    def test_each_action_passes_only_allowlisted_arguments(self):
        self.login_admin()
        with mock.patch.object(admin_app, "_run_service_script", return_value={"ok": True}) as run:
            for key in ("front", "admin", "qdrant", "ollama"):
                for action in ("start", "stop"):
                    response = self.client.post(f"/api/services/{key}/{action}")
                    self.assertEqual(response.status_code, 200)
                    run.assert_called_with("service_ctl.bat", [key, action])

    def test_invalid_arguments_never_launch_script(self):
        self.login_admin()
        with mock.patch.object(admin_app, "_run_service_script") as run:
            for path in ("/api/services/unknown/start", "/api/services/front/delete", "/api/services/front/start%26whoami"):
                self.assertEqual(self.client.post(path).status_code, 404)
            run.assert_not_called()

    def test_cross_origin_action_is_rejected(self):
        self.login_admin()
        with mock.patch.object(admin_app, "_run_service_script") as run:
            response = self.client.post("/api/services/front/stop", headers={"Origin": "https://other.example"})
            self.assertEqual(response.status_code, 403)
            run.assert_not_called()

    def test_launch_failure_is_not_reported_as_success(self):
        self.login_admin()
        with mock.patch.object(admin_app, "_run_service_script", return_value={"ok": False, "detail": "脚本不存在"}):
            response = self.client.post("/api/services/front/start")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "脚本不存在")

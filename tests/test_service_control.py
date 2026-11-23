"""单服务控制 API：临时认证库，模拟脚本执行，不停止真实服务。"""

import os
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import admin_app
from tests.test_admin_api import ApiTestCase


def _probe_config(**values):
    return SimpleNamespace(**{'embedding_model': 'configured-model',
                              'qdrant_url': 'http://127.0.0.1:6333',
                              'ollama_url': 'http://127.0.0.1:11434', **values})


class ServiceProbeTests(unittest.TestCase):
    def response(self, status=200, models=None):
        response = mock.Mock(status_code=status)
        response.json.return_value = {'models': models or []}
        return response

    def test_configured_addresses_and_mcp_bind_settings(self):
        cfg = _probe_config(qdrant_url='http://192.0.2.1:7333', ollama_url='http://127.0.0.1:12434/')
        self.assertEqual(admin_app._service_probe_url('qdrant', '', cfg), cfg.qdrant_url)
        self.assertEqual(admin_app._service_probe_url('ollama', '', cfg), 'http://127.0.0.1:12434/api/tags')
        with mock.patch.dict(os.environ, {'MCP_HTTP_HOST': '0.0.0.0', 'MCP_HTTP_PORT': '9760'}):
            self.assertEqual(admin_app._service_probe_url('mcp', '', cfg), 'http://127.0.0.1:9760/mcp')

    def test_local_probes_bypass_proxies_and_mcp_401_is_online(self):
        session = mock.Mock()
        session.get.return_value = self.response(401)
        with mock.patch('requests.Session') as constructor:
            constructor.return_value.__enter__.return_value = session
            result = admin_app._probe_service('mcp', 'MCP', 'http://127.0.0.1:8760/mcp', _probe_config())
        self.assertTrue(result['ok'])
        self.assertTrue(result['running'])
        self.assertFalse(session.trust_env)
        session.get.assert_called_once_with('http://127.0.0.1:8760/mcp', timeout=3)

    def test_missing_model_is_running_but_not_ready(self):
        with mock.patch('requests.Session.get', return_value=self.response(models=[{'name': 'other:latest'}])):
            result = admin_app._probe_service('ollama', 'Ollama', 'http://127.0.0.1:11434/api/tags', _probe_config())
        self.assertTrue(result['running'])
        self.assertFalse(result['ok'])
        self.assertFalse(result['model_available'])
        self.assertIn('未安装向量模型', result['detail'])

    def test_installed_model_and_invalid_model_response(self):
        with mock.patch('requests.Session.get', return_value=self.response(models=[{'model': 'configured-model:latest'}])):
            self.assertTrue(admin_app._probe_service('ollama', 'Ollama', 'http://127.0.0.1:11434/api/tags', _probe_config())['ok'])
        response = self.response()
        response.json.side_effect = ValueError('bad json')
        with mock.patch('requests.Session.get', return_value=response):
            result = admin_app._probe_service('ollama', 'Ollama', 'http://127.0.0.1:11434/api/tags', _probe_config())
        self.assertTrue(result['running'])
        self.assertFalse(result['ok'])
        self.assertIn('模型列表无效', result['detail'])

    def test_http_failure_and_connection_timeout_are_not_ready(self):
        import requests
        for failure in (self.response(503), requests.Timeout(), requests.ConnectionError()):
            with self.subTest(failure=type(failure).__name__), mock.patch('requests.Session.get') as get:
                if isinstance(failure, Exception):
                    get.side_effect = failure
                else:
                    get.return_value = failure
                result = admin_app._probe_service('qdrant', 'Qdrant', 'http://127.0.0.1:6333', _probe_config())
                self.assertFalse(result['ok'])
                self.assertTrue(result['detail'])


class ServiceLauncherTests(unittest.TestCase):
    def test_windows_wrapper_uses_hidden_console_and_captures_output(self):
        output = mock.Mock()
        with mock.patch.object(admin_app, '_service_script_name', return_value='service_ctl.bat'), \
             mock.patch.object(admin_app.os, 'name', 'nt'), \
             mock.patch.object(Path, 'exists', return_value=True), \
             mock.patch.object(Path, 'mkdir'), \
             mock.patch.object(Path, 'open') as open_log, mock.patch('subprocess.Popen') as popen:
            open_log.return_value.__enter__.return_value = output
            result = admin_app._run_service_script('service_ctl.bat', ['mcp', 'start'])
        self.assertTrue(result['ok'])
        args, kwargs = popen.call_args
        self.assertEqual(args[0][1:3], ['/d', '/c'])
        self.assertEqual(args[0][-2:], ['mcp', 'start'])
        self.assertEqual(kwargs['creationflags'], subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertEqual(kwargs['stdout'], output)
        self.assertEqual(kwargs['stderr'], output)
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertIn('control_log', result)

    def test_missing_script_and_spawn_failure_are_reported(self):
        with mock.patch.object(Path, 'exists', return_value=False), mock.patch('subprocess.Popen') as popen:
            result = admin_app._run_service_script('missing.bat')
        self.assertFalse(result['ok'])
        popen.assert_not_called()
        with mock.patch.object(admin_app.os, 'name', 'nt'), mock.patch.object(Path, 'exists', return_value=True), \
             mock.patch.object(Path, 'mkdir'), mock.patch.object(Path, 'open'), \
             mock.patch('subprocess.Popen', side_effect=OSError('spawn failed')):
            with self.assertLogs('src.admin_app', level='ERROR'):
                result = admin_app._run_service_script('service_ctl.bat', ['mcp', 'start'])
        self.assertFalse(result['ok'])
        self.assertIn('控制脚本启动失败', result['detail'])


class ServiceControlTests(ApiTestCase):
    def tearDown(self):
        # SQLite 连接的上下文管理只提交事务；Windows 清理临时库前先回收连接。
        import gc

        gc.collect()
        super().tearDown()

    def test_actions_require_admin_session(self):
        with mock.patch.object(admin_app, "_run_service_script") as run:
            for key in ("front", "admin", "qdrant", "ollama", "mcp"):
                for action in ("start", "stop"):
                    self.assertEqual(self.client.post(f"/api/services/{key}/{action}").status_code, 401)
            run.assert_not_called()

    def test_service_list_has_stable_keys_and_configured_model(self):
        self.login_admin()
        response_stub = mock.Mock(status_code=200)
        response_stub.json.return_value = {"models": [{"name": "configured-model:latest"}]}
        with mock.patch("requests.Session.get", return_value=response_stub), \
                mock.patch.object(admin_app, "get_config", return_value=_probe_config()):
            response = self.client.get("/api/services")
        self.assertEqual(response.status_code, 200)
        services = response.json()["services"]
        self.assertEqual([s["key"] for s in services],
                         ["front", "admin", "qdrant", "ollama", "mcp"])  # V3.9 新增 MCP 探测
        self.assertEqual(services[-2]["name"], "Ollama-向量模型 configured-model（11434）")
        self.assertTrue(all(s["ok"] for s in services))

    def test_down_service_is_reported(self):
        self.login_admin()
        import requests

        with mock.patch("requests.Session.get", side_effect=requests.ConnectionError("offline")):
            services = self.client.get("/api/services").json()["services"]
        self.assertTrue(all(not s["ok"] for s in services))

    def test_each_action_passes_only_allowlisted_arguments(self):
        """调用方固定传 .bat 原名；平台转换在 _run_service_script 内部（被 mock 隔离）。"""
        self.login_admin()
        with mock.patch.object(admin_app, "_run_service_script", return_value={"ok": True}) as run:
            for key in ("front", "admin", "qdrant", "ollama", "mcp"):
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

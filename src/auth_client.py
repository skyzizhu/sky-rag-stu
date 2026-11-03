"""V3.7：Streamlit 前台 → FastAPI 认证 API 的 HTTP 客户端。

管理后台（8700）是用户与令牌数据的唯一写入方；前台只通过这些调用读写。
服务未启动时返回友好错误，不抛异常（登录门会显示提示）。
"""

from __future__ import annotations

import os

import requests

BASE = os.environ.get("AUTH_API_BASE", "http://127.0.0.1:8700")
_TIMEOUT = 5


def _post_json(path: str, body: dict) -> tuple[dict | None, str]:
    """返回 (响应数据, 错误信息)。失败时 data 可能为部分响应（含 detail）。"""
    try:
        resp = requests.post(f"{BASE}{path}", json=body, timeout=_TIMEOUT)
    except requests.RequestException:
        return None, "认证服务未响应（8700 端口），请先运行 bash start.sh 启动管理后台"
    try:
        data = resp.json()
    except ValueError:
        return None, "认证服务返回异常"
    if resp.status_code >= 400 or not data.get("ok", False):
        return data, str(data.get("detail", "操作失败"))
    return data, ""


def _get_json(path: str) -> tuple[dict | None, str]:
    try:
        resp = requests.get(f"{BASE}{path}", timeout=_TIMEOUT)
    except requests.RequestException:
        return None, "认证服务未响应（8700 端口），请先运行 bash start.sh 启动管理后台"
    try:
        data = resp.json()
    except ValueError:
        return None, "认证服务返回异常"
    if resp.status_code >= 400:
        return data, str(data.get("detail", "请求失败"))
    return data, ""


def bootstrap() -> dict | None:
    data, _ = _get_json("/auth/bootstrap")
    return data


def bootstrap_admin(username: str, password: str, password_confirm: str) -> tuple[dict | None, str]:
    return _post_json("/auth/bootstrap-admin", {
        "username": username, "password": password,
        "password_confirm": password_confirm,
    })


def register(username: str, password: str, password_confirm: str,
             department_id: int) -> tuple[dict | None, str]:
    return _post_json("/auth/register", {
        "username": username, "password": password,
        "password_confirm": password_confirm, "department_id": department_id,
    })


def login(username: str, password: str) -> tuple[dict | None, str]:
    return _post_json("/auth/login", {"username": username, "password": password})


def get_prefs(username: str) -> dict | None:
    data, _ = _get_json(f"/auth/prefs?username={requests.utils.quote(username)}")
    return (data or {}).get("prefs") or {}


def save_prefs(username: str, settings: dict) -> None:
    # 偏好保存失败不影响主流程（下次登录会再写）
    try:
        requests.post(f"{BASE}/auth/prefs",
                      json={"username": username, "settings": settings},
                      timeout=_TIMEOUT)
    except requests.RequestException:
        pass

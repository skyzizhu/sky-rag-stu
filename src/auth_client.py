"""V3.7：Streamlit 前台 → FastAPI 认证 API 的 HTTP 客户端。

管理后台（8700）是用户与令牌数据的唯一写入方；前台只通过这些调用读写。
服务未启动时返回友好错误，不抛异常（登录门会显示提示）。
V3.7.1：login/register/bootstrap/logout 附带浏览器上下文（UA/语言），
由认证服务写入登录日志并计算设备指纹——Streamlit 发起请求的是服务进程，
真实浏览器 UA 必须从 st.context.headers 显式转发。
"""

from __future__ import annotations

import os

import requests

BASE = os.environ.get("AUTH_API_BASE", "http://127.0.0.1:8700")
_TIMEOUT = 5


def browser_context() -> dict:
    """当前浏览器会话的 UA/语言/客户端 IP（登录日志用）。

    - UA/语言：st.context.headers（真实浏览器请求头）
    - IP：登录门渲染的隐藏组件会直连认证服务 /auth/ping（CORS 已放行），
      认证服务看到的是浏览器真实局域网 IP，并写入 rip cookie（localhost 域
      不分端口共享），这里从 cookie 读回。没 ping 过（首次进入）则为空。
    """
    try:
        import streamlit as st
        headers = st.context.headers
        ip = st.context.cookies.get("rip", "")
        return {"user_agent": headers.get("user-agent", ""),
                "accept_language": headers.get("accept-language", ""),
                "client_ip": ip}
    except Exception:
        return {"user_agent": "", "accept_language": "", "client_ip": ""}


def ping_html(hostname_part: str, front_host: str) -> str:
    """登录门用的隐藏 iframe：浏览器直连 /auth/ping 写 rip cookie。

    三个跨域要点（都是实测踩过的）：
    1. ping 必须与访问前台的**同一主机名**（cookie 不跨主机名），但 iframe
       （srcdoc，跨域）**读不到** parent.location.href——主机名由服务端从
       请求 Host 头取好后传进来（hostname_part，不含端口）；
    2. iframe 对父页面只能"写"location（href 赋值/reload），"读"会抛
       SecurityError——整个脚本不得读 parent.location；导航目标要带前台
       **完整 host（含端口）**（front_host），否则协议相对 URL 会落到 80 端口；
    3. 防刷新循环的标记放 URL（?rip=1，跨刷新持久），不能放 session_state
       （整页刷新即清零，会无限循环）。
    """
    admin_port = os.environ.get("AUTH_ADMIN_PORT", "8700")
    return (f'<script>'
            f'const go = () => {{ parent.location.href = "//{front_host}/?rip=1"; }};'
            f'fetch("//{hostname_part}:{admin_port}/auth/ping",'
            f' {{credentials: "include"}}).then(go).catch(go);'
            f'</script>')


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
        "password_confirm": password_confirm, **browser_context(),
    })


def register(username: str, password: str, password_confirm: str,
             department_id: int) -> tuple[dict | None, str]:
    return _post_json("/auth/register", {
        "username": username, "password": password,
        "password_confirm": password_confirm, "department_id": department_id,
        **browser_context(),
    })


def login(username: str, password: str) -> tuple[dict | None, str]:
    return _post_json("/auth/login", {"username": username, "password": password,
                                      **browser_context()})


def logout(username: str) -> None:
    try:
        requests.post(f"{BASE}/auth/logout", json={"username": username,
                                                   **browser_context()},
                      timeout=_TIMEOUT)
    except requests.RequestException:
        pass


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

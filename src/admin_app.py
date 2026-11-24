"""V3.7：FastAPI 管理后台（独立服务，默认端口 8700）。

V3.7.1 重构：管理页面从"服务端渲染多页 + 表单跳转"重构为**单页工作台（SPA）+ JSON API**——
所有操作 fetch 即时完成、toast 反馈、不整页刷新；新增登录日志（含浏览器指纹）与 RAG 使用日志。

- 页面：/admin/login（服务端渲染）+ /admin（SPA 壳，前端 fetch 渲染）
- JSON API：/api/departments /api/users /api/tokens /api/logs/... （全部管理员鉴权）
- 认证 API：/auth/register /auth/login /auth/bootstrap* /auth/prefs（Streamlit 前台调用，
  请求体可带浏览器 UA/语言用于登录日志指纹）
- 会话：HMAC 签名 Cookie（secret 落盘 storage/auth/admin_secret.key）
- 与 Streamlit 共用 src/auth_db.py 的同一份 SQLite
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sys
import time
from datetime import datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import sqlite3
import tempfile
from contextlib import closing
from fastapi import Depends, FastAPI, Form, HTTPException, Path as PathParam, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.routing import Match

from src import auth_db, business_directions, admin_audit, ingest_audit, projects
from src.config import get_config
from src.dashboard_stats import ingestion_statistics, knowledge_inventory, usage_statistics

app = FastAPI(title="长城互联网 · RAG管理后台", docs_url=None, redoc_url=None)
# V3.8 安全收紧：不再挂全局 CORS（原 allow_origin_regex=".*" + allow_credentials，
# 等于允许任何来源带着管理员会话调用管理接口）。浏览器端唯一的跨域调用是登录门
# iframe 对 /auth/ping 的探测，CORS 头只在该端点单独下发（见 api_ping）。
templates = Jinja2Templates(directory=str(Path(__file__).parent / "admin_templates"))
# 品牌静态资源：前台 Streamlit 的 ./static 目录，后台同源挂载同一份（logo 等）。
app.mount("/static", StaticFiles(directory=Path(__file__).parent.parent / "static"), name="static")

_STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def _same_origin_guard(request: Request, call_next):
    """管理 API 写操作的同源校验（防同站跨端口 CSRF）。

    SameSite=Lax 只挡「跨站」，不挡「同站」：同一主机其他端口的网页与本服务同站，
    浏览器会带上 admin_sess Cookie。像停用账户、吊销令牌这类无请求体的 POST，
    请求发出即生效，CORS 拦不住。这里要求浏览器带来的 Origin 与本服务 Host 一致；
    没有 Origin 头的请求（curl、Streamlit 服务端调用）不是浏览器 CSRF，放行。
    """
    if request.method in _STATE_CHANGING_METHODS and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin is not None:
            allowed = {h for h in (request.headers.get("host"),
                                   request.headers.get("x-forwarded-host")) if h}
            if urlsplit(origin).netloc not in allowed:
                return JSONResponse({"ok": False, "detail": "跨站请求被拒绝"}, status_code=403)
    response = await call_next(request)
    if request.url.path.startswith('/api/data/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.middleware('http')
async def _admin_operation_audit(request: Request, call_next):
    path = request.url.path
    tracked = path.startswith('/api/') and request.method in _STATE_CHANGING_METHODS
    if not tracked:
        return await call_next(request)
    started = time.perf_counter()
    try:
        actor = await run_in_threadpool(current_admin, request)
    except (ValueError, TypeError):
        actor = None
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get('route')
        endpoint = request.scope.get('endpoint')
        path_params = request.path_params
        if endpoint is None:
            # CSRF 在路由执行前拒绝请求，也应记录原本尝试的管理动作。
            for candidate in app.routes:
                matched, child_scope = candidate.matches(request.scope)
                if matched == Match.FULL:
                    route = candidate
                    endpoint = child_scope.get('endpoint')
                    path_params = child_scope.get('path_params', {})
                    break
        action = admin_audit.OPERATIONS.get(getattr(endpoint, '__name__', ''), ('admin_request', ''))[0]
        event = getattr(request.state, 'admin_audit_event', {})
        # 身份在操作前快照：停用/删除自身或登出之后仍能追溯操作者。
        parts = []
        for key, value in path_params.items():
            if key.endswith('_id') and str(value).isdigit():
                parts.append(f'{key}={value}')
            elif key == 'key' and value in _SERVICE_KEYS:
                parts.append(f'service={value}')
            elif key == 'action' and value in {'start', 'stop'}:
                parts.append(f'action={value}')
        # 只存路由模板，不存查询参数、原始请求体/响应体、Cookie 或令牌。
        route_path = getattr(route, 'path', '[unknown route]')
        await run_in_threadpool(
            admin_audit.record, admin=actor, event=event, action=action, target=', '.join(parts),
            method=request.method, path=route_path, status_code=status,
            duration_ms=int((time.perf_counter() - started) * 1000),
            ip=request.client.host if request.client else '',
            user_agent=request.headers.get('user-agent', ''),
        )

_SESSION_COOKIE = "admin_sess"
_SESSION_TTL = 7 * 24 * 3600


# ---------------------------------------------------------------- 会话签名（共享 auth_db 密钥）
def _sign(payload: str) -> str:
    import hmac
    return hmac.new(auth_db.get_secret(), payload.encode(),
                    auth_db.hashlib.sha256).hexdigest()


def _make_cookie(username: str) -> str:
    """管理后台会话 Cookie（7 天）。"""
    expires = str(int(time.time()) + _SESSION_TTL)
    return f"{expires}|{username}|{_sign(f'admin|{expires}|{username}')}"


def _read_cookie(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.split("|")
    if len(parts) != 3:
        return None
    expires, username, sig = parts
    if not hmac.compare_digest(_sign(f"admin|{expires}|{username}"), sig):
        return None
    if int(expires) < int(time.time()):
        return None
    return username


def current_admin(request: Request) -> dict | None:
    """从 Cookie 解析当前管理员（查 admins 表）；未登录/已停用返回 None。"""
    username = _read_cookie(request.cookies.get(_SESSION_COOKIE))
    if not username:
        return None
    admin = auth_db.get_admin(username)
    if admin is None or admin["status"] != "active":
        return None
    return admin


def require_admin(request: Request) -> dict:
    """页面用：未登录跳登录页。"""
    admin = current_admin(request)
    if admin is None:
        raise HTTPException(status_code=303,
                            headers={"Location": "/admin/login"})
    return admin


def require_admin_api(request: Request) -> dict:
    """JSON API 用：未登录返回 401 JSON。"""
    admin = current_admin(request)
    if admin is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return admin


def _log_from_admin(request: Request, action: str, username: str,
                    success: bool = True, detail: str = "",
                    department: str = "") -> None:
    """保留原认证日志，并为统一审计补充业务摘要（每个请求只记录一次审计）。"""
    request.state.admin_audit_event = dict(username=username, success=success, detail=detail)
    auth_db.log_auth_event(
        action=action, username=username, success=success, detail=detail,
        department=department, entry="admin",
        ip=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
        accept_language=request.headers.get("accept-language", ""),
    )


@app.on_event("startup")
def _startup() -> None:
    auth_db.init_db()
    auth_db.get_secret()  # 首次启动生成签名密钥


# ---------------------------------------------------------------- 页面：登录/登出
@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if current_admin(request):
        return RedirectResponse("/admin", status_code=303)
    return RedirectResponse("/admin/login", status_code=303)


@app.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request, err: str = ""):
    init_mode = auth_db.count_admins() == 0
    return templates.TemplateResponse(request, "login.html",
                                      {"err": err, "msg": "",
                                       "init_mode": init_mode})


@app.post("/admin/login")
async def login_action(request: Request):
    # 兼容两种提交：登录页 fetch JSON（中文账户无乱码）与普通表单
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        body = await request.json()
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
    else:
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
    info, err = auth_db.authenticate_admin(username, password)
    if err or info is None:
        _log_from_admin(request, "login", username, success=False,
                        detail=err or "登录失败")
        return templates.TemplateResponse(request, "login.html",
                                          {"err": err or "登录失败", "msg": ""},
                                          status_code=401)
    _log_from_admin(request, "login", info["username"], success=True)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(_SESSION_COOKIE, _make_cookie(info["username"]),
                    httponly=True, samesite="lax")
    return resp


@app.post("/admin/bootstrap")
async def bootstrap_admin_action(request: Request):
    """首次使用：在这里创建第一个管理员并直接登录后台。

    初始化管理员的唯一入口（前台只提示跳转到这里）；系统已有管理员时拒绝。
    创建成功即写入管理员会话 Cookie，浏览器直接进入 /admin 工作台。
    """
    if auth_db.count_admins() > 0:
        return JSONResponse({"ok": False,
                             "detail": "系统已有管理员，初始化入口已关闭"},
                            status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "detail": "请求格式错误"}, status_code=400)
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    confirm = str(body.get("password_confirm", ""))
    if password != confirm:
        return JSONResponse({"ok": False, "detail": "两次输入的密码不一致"},
                            status_code=400)
    try:
        admin = auth_db.create_admin(username, password, is_super=True)
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    auth_db.log_auth_event("bootstrap_admin", admin["username"], success=True,
                           entry="admin",
                           ip=request.client.host if request.client else "",
                           user_agent=request.headers.get("user-agent", ""),
                           accept_language=request.headers.get("accept-language", ""))
    _log_from_admin(request, "login", admin["username"])
    resp = JSONResponse({"ok": True})
    resp.set_cookie(_SESSION_COOKIE, _make_cookie(admin["username"]),
                    httponly=True, samesite="lax")
    return resp


@app.get("/admin/logout")
def logout(request: Request):
    admin = current_admin(request)
    if admin:
        _log_from_admin(request, "logout", admin["username"])
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(_SESSION_COOKIE)
    return resp


# ---------------------------------------------------------------- 页面：SPA 工作台
@app.get("/admin", response_class=HTMLResponse)
def admin_shell(request: Request, admin: dict = Depends(require_admin)):
    return templates.TemplateResponse(request, "admin.html",
                                      {"admin": admin})


# ---------------------------------------------------------------- JSON API：概览
@app.get("/api/overview")
def api_overview(days: int = Query(30, ge=1, le=90), _: dict = Depends(require_admin_api)):
    deps = auth_db.list_departments()
    users = auth_db.list_users()
    admins = auth_db.list_admins()
    return {
        "departments": len(deps),
        "departments_enabled": len([d for d in deps if not d["disabled"]]),
        "users": len(users),
        "users_active": len([u for u in users if u["status"] == "active"]),
        "admins": len(admins),
        "admins_active": len([a for a in admins if a["status"] == "active"]),
        "business_directions": len(business_directions.list_directions()),
        "ingestion_statistics": ingestion_statistics(days),
        "usage_statistics": usage_statistics(days),
    }


@app.get("/api/overview/inventory")
def api_overview_inventory(_: dict = Depends(require_admin_api)):
    return knowledge_inventory()


# ---------------------------------------------------------------- JSON API：服务状态与控制
_SERVICE_PROBES = [
    ("front", "知识库前台（8501）", "http://127.0.0.1:8501"),
    ("admin", "管理后台（8700）", "http://127.0.0.1:8700/admin/login"),
    ("qdrant", "Qdrant 向量库（6333）", "http://127.0.0.1:6333"),
    ("ollama", "Ollama 向量化模型（11434）", "http://127.0.0.1:11434"),
    # MCP 不带 PAT 的 401 表示鉴权层在线，不等于检索依赖已全部就绪。
    ("mcp", "MCP 知识服务（8760）", "http://127.0.0.1:8760/mcp"),
]


def _service_probe_url(key: str, default: str, cfg) -> str:
    if key == "qdrant":
        return cfg.qdrant_url
    if key == "ollama":
        return cfg.ollama_url.rstrip("/") + "/api/tags"
    if key == "mcp":
        host = os.getenv("MCP_HTTP_HOST", "127.0.0.1")
        host = "127.0.0.1" if host in ("0.0.0.0", "localhost") else host
        host = "[::1]" if host == "::" else f"[{host}]" if ":" in host else host
        return f"http://{host}:{os.getenv('MCP_HTTP_PORT', '8760')}/mcp"
    return default


def _probe_service(key: str, name: str, url: str, cfg) -> dict:
    import requests

    result = {"key": key, "name": name, "ok": False, "running": False, "detail": ""}
    with requests.Session() as session:
        host = urlsplit(url).hostname or ""
        if host in ("localhost", "127.0.0.1", "::1"):
            session.trust_env = False  # 回环服务不经系统代理，远程地址仍沿用部署配置。
        try:
            response = session.get(url, timeout=3)
            result["running"] = (200 <= response.status_code < 400
                                 or key == "mcp" and response.status_code == 401)
            if not result["running"]:
                result["detail"] = f"服务返回 HTTP {response.status_code}，尚未就绪"
            elif key == "ollama":
                models = response.json().get("models", [])
                names = {value for model in models if isinstance(model, dict)
                         for value in (model.get("name"), model.get("model")) if isinstance(value, str)}
                expected = cfg.embedding_model
                available = expected in names or ":" not in expected and expected + ":latest" in names
                result["model_available"] = available
                result["ok"] = available
                result["detail"] = (f"服务在线，向量模型 {expected} 已安装" if available
                                    else f"服务已启动，但未安装向量模型 {expected}；启动按钮不会自动下载模型")
            else:
                result["ok"] = True
                result["detail"] = "服务在线（PAT 鉴权正常）" if key == "mcp" else "服务在线"
        except requests.Timeout:
            result["detail"] = "服务探测超时，请查看启动日志后刷新"
        except requests.RequestException:
            result["detail"] = "无法连接服务，请检查监听地址和启动日志"
        except (ValueError, TypeError, AttributeError):
            result["detail"] = "服务已响应，但模型列表无效，请查看 Ollama 日志"
    return result


@app.get("/api/services")
def api_services(_: dict = Depends(require_admin_api)):
    """按实际配置探测服务；区分运行状态与模型就绪，不改变服务控制权限。"""
    cfg = get_config()
    services = []
    for key, name, default in _SERVICE_PROBES:
        if key == "ollama":
            name = f"Ollama-向量模型 {cfg.embedding_model}（11434）"
        services.append(_probe_service(key, name, _service_probe_url(key, default, cfg), cfg))
    return {"ok": True, "services": services}


def _service_script_name(script_name: str) -> str:
    """按平台返回控制脚本名：Windows 用 .bat，macOS/Linux 用同名 .sh。"""
    if os.name == "nt":
        return script_name
    return script_name.replace(".bat", ".sh")


def _run_service_script(script_name: str, args: list[str] | None = None) -> dict:
    """分离进程执行服务控制脚本（Windows: *.bat / macOS·Linux: 同名 *.sh）。

    Popen 立即返回、不等脚本完成——脚本会停止/重启本管理后台自身（预期行为），
    因此响应必须先于脚本动作完成并送达浏览器。
    """
    import subprocess

    script_name = _service_script_name(script_name)
    script = Path(__file__).resolve().parent.parent / script_name
    if not script.exists():
        return {"ok": False, "detail": f"未找到控制脚本：{script_name}"}
    if os.name == "nt":
        # DETACHED_PROCESS 下 cmd 的端口检查管道在 Windows 会卡住；隐藏控制台
        # 保留标准句柄与立即回包语义，脚本仍由独立进程组执行。
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        command_processor = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
        cmd = [str(command_processor), "/d", "/c", str(script)] + list(args or [])
        log_dir = script.parent / "storage"
        log_path = log_dir / "service_script_control.log"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab") as output:
                output.write(f"\n=== {script_name} {' '.join(args or [])} {datetime.now().isoformat()} ===\n".encode("utf-8"))
                output.flush()
                subprocess.Popen(cmd, cwd=str(script.parent), creationflags=flags,
                                 stdin=subprocess.DEVNULL, stdout=output, stderr=output)
        except OSError as exc:
            logging.getLogger(__name__).exception("Could not launch service control script %s", script_name)
            return {"ok": False, "detail": f"控制脚本启动失败：{exc}"}
        return {"ok": True, "control_log": "storage/service_script_control.log"}
    # macOS / Linux：setsid 分离进程组（脱离管理后台进程树，后台自身被停后脚本继续跑完）
    cmd = ["bash", str(script)] + list(args or [])
    try:
        subprocess.Popen(cmd, cwd=str(script.parent), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as exc:
        return {"ok": False, "detail": f"控制脚本启动失败：{exc}"}
    return {"ok": True}


@app.post("/api/services/restart")
def api_services_restart(_: dict = Depends(require_admin_api)):
    """一键重启全部服务：分离进程执行 restart_services.bat（停止后按序拉起）。"""
    result = _run_service_script("restart_services.bat")
    if result["ok"]:
        result["detail"] = "重启指令已发出：约 15 秒后服务恢复，稍候刷新本页"
    return JSONResponse(result, status_code=200 if result["ok"] else 400)


@app.post("/api/services/stop")
def api_services_stop(_: dict = Depends(require_admin_api)):
    """一键关闭全部服务（分离进程执行 stop_services.bat）。"""
    result = _run_service_script("stop_services.bat")
    if result["ok"]:
        result["detail"] = "关闭指令已发出：全部服务正在停止"
    return JSONResponse(result, status_code=200 if result["ok"] else 400)


_SERVICE_KEYS = {"front", "admin", "qdrant", "ollama", "mcp"}


@app.post("/api/services/{key}/{action}")
def api_service_control(key: str, action: str, _: dict = Depends(require_admin_api)):
    """单服务控制：start / stop（粒度到每个服务，供"服务状态"页的操作列调用）。"""
    if key not in _SERVICE_KEYS or action not in ("start", "stop"):
        raise HTTPException(status_code=404, detail="未知的服务或操作")
    result = _run_service_script("service_ctl.bat", [key, action])
    if result["ok"]:
        result["detail"] = f"指令已发出：{key} {action}"
    return JSONResponse(result, status_code=200 if result["ok"] else 400)


# ---------------------------------------------------------------- 数据管理（只读查看与整库导出）

_DATA_TABLE_REMARKS = {
    'departments': '部门主数据、权限键及停用状态',
    'department_aliases': '部门名称及历史权限键的归属映射',
    'business_directions': '业务方向主数据、编号及备注',
    'projects': '项目主数据、业务方向及牵头部门',
    'project_departments': '项目与参与部门的关联关系',
    'project_directories': '领域内项目与目录名的绑定关系',
    'admins': '独立管理员账户、认证信息及管理身份状态',
    'users': '员工账户、所属部门、认证信息及权限配置',
    'document_user_revocations': '员工停用时记录的文档访问撤销标记',
    'tokens': '员工 MCP 令牌哈希、使用时间及吊销状态',
    'user_prefs': '按账户保存的用户偏好设置',
    'user_login_logs': '员工侧登录、登出等认证事件日志',
    'admin_login_logs': '管理后台登录、登出等认证事件日志',
    'admin_operation_logs': '管理操作审计：操作者、动作、目标、结果及耗时',
    'qa_logs': '问答、检索和总结日志，含答案、来源及模型用量',
    'doc_access_logs': '文档读取、分页片段及目录枚举的访问审计',
    'ingest_logs': '文件入库审计：批次、来源、处理状态及耗时',
    'retrieval_acl_epoch': '授权及检索范围变更的版本与处理中标记',
    'sqlite_sequence': 'SQLite 自动维护的自增序号记录',
}


def _data_table_remark(name: str) -> str:
    return _DATA_TABLE_REMARKS.get(name, 'SQLite 系统维护对象' if name.startswith('sqlite_')
                                   else '未登记用途，请参考字段结构')


def _open_readonly() -> sqlite3.Connection:
    """只读打开现有 SQLite 库，不创建库或运行迁移。"""
    conn = sqlite3.connect(auth_db.DB_PATH.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def _data_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _cell_value(value):
    if isinstance(value, bytes):
        return {'type': 'blob', 'bytes': len(value), 'hex': value.hex()}
    if value is None or isinstance(value, str):
        return value
    return str(value)  # SQLite int64 超出浏览器安全整数范围时也不丢精度。


def _data_columns(conn, table: str) -> list[dict]:
    return [dict(name=c['name'], type=c['type'], not_null=bool(c['notnull']),
                 default=c['dflt_value'], primary_key=bool(c['pk']),
                 primary_key_order=c['pk'], hidden=c['hidden'])
            for c in conn.execute(f'PRAGMA table_xinfo({_data_identifier(table)})')]


def _data_indexes(conn, table: str) -> list[dict]:
    indexes = []
    for row in conn.execute(f'PRAGMA index_list({_data_identifier(table)})'):
        definition = conn.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?',
                                  ('index', row['name'])).fetchone()
        columns = [dict(row) for row in conn.execute(
            f'PRAGMA index_xinfo({_data_identifier(row["name"])} )')]
        indexes.append(dict(name=row['name'], table=table, unique=bool(row['unique']),
                            origin=row['origin'], partial=bool(row['partial']),
                            sql=definition['sql'] if definition else None, columns=columns))
    return indexes


@app.get('/api/data/tables')
def api_data_tables(_: dict = Depends(require_admin_api)):
    """列出全部表、视图、索引（含自动索引）及触发器，不提供 SQL 执行或写接口。"""
    tables, views, indexes, triggers = [], [], [], []
    with closing(_open_readonly()) as conn:
        conn.execute('BEGIN')
        objects = conn.execute("SELECT name, type, tbl_name, sql FROM sqlite_master "
                               "WHERE type IN ('table','view','index','trigger') ORDER BY type, name").fetchall()
        known_indexes = set()
        for obj in objects:
            if obj['type'] in ('table', 'view'):
                entry = dict(name=obj['name'], type=obj['type'], rows=None, columns=None,
                             sql=obj['sql'], internal=obj['name'].startswith('sqlite_'),
                             remark=_data_table_remark(obj['name']))
                try:
                    entry['columns'] = len(_data_columns(conn, obj['name']))
                    if obj['type'] == 'table':
                        entry['rows'] = conn.execute(
                            f'SELECT COUNT(*) FROM {_data_identifier(obj["name"])}').fetchone()[0]
                except sqlite3.Error:
                    entry['unavailable'] = True  # 单个损坏视图不影响其他对象的结构列表。
                tables.append(entry)
                if obj['type'] == 'view':
                    views.append(entry)
                else:
                    table_indexes = _data_indexes(conn, obj['name'])
                    indexes.extend(table_indexes)
                    known_indexes.update(i['name'] for i in table_indexes)
            elif obj['type'] == 'trigger':
                triggers.append(dict(name=obj['name'], table=obj['tbl_name'], sql=obj['sql']))
        for obj in objects:
            if obj['type'] == 'index' and obj['name'] not in known_indexes:
                indexes.append(dict(name=obj['name'], table=obj['tbl_name'], unique=False,
                                    origin='c', partial=False, sql=obj['sql'], columns=[]))
        info = {key: conn.execute('PRAGMA ' + key).fetchone()[0]
                for key in ('page_count', 'page_size', 'user_version', 'schema_version', 'journal_mode')}
    return {'ok': True, 'database': 'storage/auth/auth.db', 'database_info': info,
            'tables': tables, 'views': views, 'indexes': indexes, 'triggers': triggers}


@app.get('/api/data/tables/{table}/schema')
def api_data_table_schema(table: str, _: dict = Depends(require_admin_api)):
    """只读查看结构，不计数或读取数据行；损坏视图仍保留 SQL 定义。"""
    with closing(_open_readonly()) as conn:
        conn.execute('BEGIN')
        obj = conn.execute("SELECT type, sql FROM sqlite_master WHERE type IN ('table','view') AND name=?",
                           (table,)).fetchone()
        if obj is None:
            raise HTTPException(status_code=404, detail='表或视图不存在')
        columns, error = [], ''
        try:
            columns = _data_columns(conn, table)
        except sqlite3.Error:
            error = '字段结构不可用，请检查 SQL 定义及引用对象是否存在'
        indexes = _data_indexes(conn, table) if obj['type'] == 'table' else []
        foreign_keys = [dict(row) for row in conn.execute(
            f'PRAGMA foreign_key_list({_data_identifier(table)})')] if obj['type'] == 'table' else []
    return {'ok': True, 'table': table, 'type': obj['type'], 'sql': obj['sql'],
            'remark': _data_table_remark(table), 'columns': columns, 'indexes': indexes,
            'foreign_keys': foreign_keys, 'unavailable': bool(error), 'error': error}


@app.get('/api/data/tables/{table}/rows')
def api_data_table_rows(table: str, page: int = Query(1, ge=1),
                        page_size: int = Query(50, ge=1, le=500),
                        _: dict = Depends(require_admin_api)):
    """分页查看全部字段与原始数据；只接受库中已存在对象，标识符按 SQLite 规则转义。"""
    with closing(_open_readonly()) as conn:
        conn.execute('BEGIN')
        obj = conn.execute("SELECT type, sql FROM sqlite_master WHERE type IN ('table','view') AND name=?",
                           (table,)).fetchone()
        if obj is None:
            raise HTTPException(status_code=404, detail='表或视图不存在')
        identifier = _data_identifier(table)
        columns = _data_columns(conn, table)
        total = conn.execute(f'SELECT COUNT(*) FROM {identifier}').fetchone()[0]
        total_pages = max(1, -(-total // page_size))
        page = min(page, total_pages)
        primary = sorted((c for c in columns if c['primary_key']), key=lambda c: c['primary_key_order'])
        order = ' ORDER BY ' + ', '.join(_data_identifier(c['name']) for c in primary) if primary else ''
        cursor = conn.execute(f'SELECT * FROM {identifier}{order} LIMIT ? OFFSET ?',
                              (page_size, (page - 1) * page_size))
        result_names = [c[0] for c in cursor.description]
        rows = [[_cell_value(value) for value in row] for row in cursor]
        indexes = _data_indexes(conn, table) if obj['type'] == 'table' else []
        foreign_keys = [dict(row) for row in conn.execute(f'PRAGMA foreign_key_list({identifier})')]
    return {'ok': True, 'table': table, 'type': obj['type'], 'sql': obj['sql'],
            'columns': columns, 'data_columns': result_names, 'indexes': indexes,
            'foreign_keys': foreign_keys, 'rows': rows, 'total': total,
            'page': page, 'page_size': page_size, 'total_pages': total_pages}


@app.get('/api/data/export')
def api_data_export(_: dict = Depends(require_admin_api)):
    """下载包含全部 SQLite 对象与数据的一致性快照，传输结束删除本次临时副本。"""
    export_dir = auth_db.DB_PATH.parent / 'exports'
    export_dir.mkdir(parents=True, exist_ok=True)
    handle, filename = tempfile.mkstemp(prefix='sky_rag_backup_', suffix='.db', dir=export_dir)
    os.close(handle)
    target = Path(filename)
    try:
        with closing(_open_readonly()) as source, closing(sqlite3.connect(target)) as destination:
            source.backup(destination, pages=256, sleep=0.01)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return FileResponse(target, filename=target.name, media_type='application/octet-stream',
                        headers={'Cache-Control': 'no-store'},
                        background=BackgroundTask(target.unlink, missing_ok=True))


# ---------------------------------------------------------------- JSON API：业务方向（与权限部门独立）
class BusinessDirectionBody(BaseModel):
    name: str = Field(max_length=80)
    code: str = Field(default='', max_length=64)
    note: str = Field(default='', max_length=1000)


class BusinessDirectionEditBody(BusinessDirectionBody):
    original: BusinessDirectionBody | None = None


@app.get('/api/business-directions')
def api_business_directions(_: dict = Depends(require_admin_api)):
    return {'ok': True, 'business_directions': business_directions.list_directions()}


@app.get('/api/business-directions/suggest-code')
def api_business_direction_code(name: str = Query(min_length=1, max_length=80),
                                _: dict = Depends(require_admin_api)):
    try:
        return {'ok': True, 'code': business_directions.suggest_code(name)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post('/api/business-directions')
def api_business_direction_create(body: BusinessDirectionBody, request: Request,
                                  admin: dict = Depends(require_admin_api)):
    try:
        direction = business_directions.create_direction(body.name, body.code, body.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _log_from_admin(request, 'business_direction_create', admin['username'],
                    detail=f"{direction['name']} ({direction['code']})")
    return {'ok': True, 'business_direction': direction}


@app.get('/api/business-directions/{direction_id}')
def api_business_direction_detail(direction_id: int, _: dict = Depends(require_admin_api)):
    direction = business_directions.get_direction(direction_id)
    if direction is None:
        raise HTTPException(status_code=404, detail='业务方向不存在')
    return {'ok': True, 'business_direction': direction}


@app.put('/api/business-directions/{direction_id}')
def api_business_direction_update(direction_id: int, body: BusinessDirectionEditBody, request: Request,
                                  admin: dict = Depends(require_admin_api)):
    try:
        before, after = business_directions.update_direction(
            direction_id, body.name, body.code, body.note,
            original=body.original.model_dump() if body.original is not None else None)
    except business_directions.DirectionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    changes = []
    for key, label in [('name', '名称'), ('code', '编号')]:
        if before[key] != after[key]:
            changes.append(f'{label}「{before[key]}」→「{after[key]}」')
    if before['note'] != after['note']:
        changes.append('备注已更新')
    _log_from_admin(request, 'business_direction_update', admin['username'],
                    detail=f"编辑业务方向 #{direction_id}：" + ('；'.join(changes) or '无字段变更'))
    return {'ok': True, 'business_direction': after}


# ---------------------------------------------------------------- JSON API：项目
PositiveId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
ProjectId = Annotated[int, PathParam(gt=0, le=2**63 - 1)]


class ProjectBody(BaseModel):
    name: str = Field(max_length=80)
    direction_id: PositiveId | None = None
    lead_department_id: PositiveId | None = None
    participant_department_ids: list[PositiveId] = Field(default_factory=list)
    note: str = Field(default='', max_length=1000)


class ProjectEditBody(ProjectBody):
    revision: PositiveId


@app.get('/api/projects')
def api_projects(_: dict = Depends(require_admin_api)):
    return {'ok': True, 'projects': projects.list_projects()}


@app.get('/api/projects/{project_id}')
def api_project_detail(project_id: ProjectId, _: dict = Depends(require_admin_api)):
    project = projects.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail='项目不存在')
    return {'ok': True, 'project': project}


@app.post('/api/projects')
def api_project_create(body: ProjectBody, request: Request,
                       admin: dict = Depends(require_admin_api)):
    try:
        project = projects.save_project(projects.ProjectFields(**body.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _log_from_admin(request, 'project_create', admin['username'],
                    detail=f"项目 #{project['id']} · {project['name']}")
    return {'ok': True, 'project': project}


@app.put('/api/projects/{project_id}')
def api_project_update(project_id: ProjectId, body: ProjectEditBody, request: Request,
                       admin: dict = Depends(require_admin_api)):
    try:
        fields = projects.ProjectFields(**body.model_dump(exclude={'revision'}))
        project = projects.save_project(fields, project_id=project_id, revision=body.revision)
    except projects.ProjectConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _log_from_admin(request, 'project_update', admin['username'],
                    detail=f"项目 #{project_id} · {project['name']}")
    return {'ok': True, 'project': project}


# ---------------------------------------------------------------- JSON API：部门
@app.get("/api/departments")
def api_departments(_: dict = Depends(require_admin_api)):
    deps = auth_db.list_departments()
    counts = auth_db.department_counts()
    for d in deps:
        d["employees"] = counts.get(d["id"], 0)
    return {"ok": True, "departments": deps}


class DepartmentBody(BaseModel):
    name: str = Field(max_length=32)
    description: str = Field(default='', max_length=1000)


@app.get('/api/departments/{dep_id}')
def api_department_detail(dep_id: int, _: dict = Depends(require_admin_api)):
    dep = auth_db.get_department(dep_id)
    if dep is None:
        raise HTTPException(status_code=404, detail='部门不存在')
    dep['employees'] = auth_db.department_counts().get(dep_id, 0)
    return {'ok': True, 'department': dep}


@app.post("/api/departments")
def api_department_create(body: DepartmentBody, request: Request,
                          admin: dict = Depends(require_admin_api)):
    try:
        dep = auth_db.create_department(body.name, body.description)
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    _log_from_admin(request, "department_create", admin["username"],
                    department=dep["name"])
    return {"ok": True, "department": dep}


@app.post("/api/departments/{dep_id}/toggle")
def api_department_toggle(dep_id: int, request: Request,
                          admin: dict = Depends(require_admin_api)):
    dep = next((d for d in auth_db.list_departments() if d["id"] == dep_id), None)
    if dep is None:
        return JSONResponse({"ok": False, "detail": "部门不存在"}, status_code=404)
    auth_db.set_department_disabled(dep_id, not bool(dep["disabled"]))
    _log_from_admin(request, "department_toggle", admin["username"],
                    detail=f"{dep['name']} → {'启用' if dep['disabled'] else '停用'}",
                    department=dep["name"])
    return {"ok": True, "disabled": not bool(dep["disabled"])}


@app.delete("/api/departments/{dep_id}")
def api_department_delete(dep_id: int, request: Request,
                          admin: dict = Depends(require_admin_api)):
    dep = next((d for d in auth_db.list_departments() if d["id"] == dep_id), None)
    name = dep["name"] if dep else str(dep_id)
    try:
        auth_db.delete_department(dep_id)
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    _log_from_admin(request, "department_delete", admin["username"], department=name)
    return {"ok": True}


# ---------------------------------------------------------------- JSON API：员工
@app.get("/api/users")
def api_users(dep: int | None = None, q: str = "",
              page: int = Query(1, ge=1), page_size: int = Query(0, le=200),
              _: dict = Depends(require_admin_api)):
    """员工列表（users 表只放员工；管理员走 /api/admins）。
    page_size=0 表示不分页（下拉框等场景取全量）。"""
    users = auth_db.list_users(department_id=dep)
    if q:
        ql = q.strip().lower()
        users = [u for u in users
                 if ql in u["username"].lower()
                 or ql in (u.get("real_name") or "").lower()]
    total = len(users)
    if page_size > 0:
        start = (page - 1) * page_size
        users = users[start:start + page_size]
    return {"ok": True, "users": users, "total": total}


@app.get("/api/users/{user_id}")
def api_user_detail(user_id: int, _: dict = Depends(require_admin_api)):
    user = auth_db.get_user_by_id(user_id)
    if user is None:
        return JSONResponse({"ok": False, "detail": "用户不存在"}, status_code=404)
    return {"ok": True, "user": user,
            "valid_tokens": auth_db.count_user_tokens(user_id)}


class PermissionsBody(BaseModel):
    permissions: list[str]


@app.patch("/api/users/{user_id}/permissions")
def api_user_permissions(user_id: int, body: PermissionsBody,
                         request: Request,
                         admin: dict = Depends(require_admin_api)):
    """给员工分配前台权限（upload/manage/retrieval_settings/maintenance/plugins，默认无）。"""
    user = auth_db.get_user_by_id(user_id)
    if user is None:
        return JSONResponse({"ok": False, "detail": "用户不存在"}, status_code=404)
    perms = [p for p in body.permissions if p in auth_db.FRONT_PERMISSIONS]
    auth_db.update_user(user_id, permissions=perms)
    _log_from_admin(request, "permissions_update", admin["username"],
                    detail=f"{user['username']} 前台权限 → {perms or '无'}")
    return {"ok": True, "permissions": perms}


class UserBody(BaseModel):
    username: str
    password: str
    department_id: int
    real_name: str = ""
    note: str = ""


@app.post("/api/users")
def api_user_create(body: UserBody, request: Request,
                    admin: dict = Depends(require_admin_api)):
    try:
        user = auth_db.create_user(body.username, body.password,
                                   department_id=body.department_id,
                                   real_name=body.real_name, note=body.note)
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    _log_from_admin(request, "employee_create", admin["username"],
                    detail=f"创建员工 {user['username']}"
                           f"{f'（{user["real_name"]}）' if user.get('real_name') else ''}",
                    department=user.get("department", ""))
    return {"ok": True, "user": user}


class ResetPasswordBody(BaseModel):
    new_password: str


@app.post("/api/users/{user_id}/reset")
def api_user_reset(user_id: int, body: ResetPasswordBody, request: Request,
                   admin: dict = Depends(require_admin_api)):
    try:
        auth_db.reset_password(user_id, body.new_password)
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    user = auth_db.get_user_by_id(user_id)
    name = user["username"] if user else str(user_id)
    _log_from_admin(request, "password_reset", admin["username"],
                    detail=f"重置 {name} 的密码")
    return {"ok": True, "detail": f"「{name}」的密码已重置（其全部访问令牌已吊销）"}


@app.post("/api/users/{user_id}/toggle")
def api_user_toggle(user_id: int, request: Request,
                    admin: dict = Depends(require_admin_api)):
    user = auth_db.get_user_by_id(user_id)
    if user is None:
        return JSONResponse({"ok": False, "detail": "用户不存在"}, status_code=404)
    new_status = "disabled" if user["status"] == "active" else "active"
    from src.manage import revoke_document_user

    if new_status == "disabled":
        auth_db.update_user(user_id, status="disabled")
    warning = ""
    removed = 0
    if new_status == "disabled" or user["username"] in auth_db.document_user_revocations():
        try:
            removed = revoke_document_user(user["username"])
        except Exception:
            logging.getLogger(__name__).exception("清理员工文档授权失败")
            if new_status == "active":
                raise HTTPException(status_code=503, detail="受限文档的旧授权清理未完成，账号保持停用，请稍后重试")
            warning = "账号已停用。受限文档授权清理尚未完成，重新启用前会再次尝试。"
    if new_status == "active":
        auth_db.update_user(user_id, status="active")
    state = "启用" if new_status == "active" else "停用"
    _log_from_admin(request, "employee_toggle", admin["username"],
                    detail=f"{user['username']} → {state}。{warning or f'已清理 {removed} 份文档的个人授权。'}")
    return {"ok": True, "status": new_status, "warning": warning}


# ---------------------------------------------------------------- JSON API：管理员（admins 表，与员工分列）
@app.get("/api/admins")
def api_admins_list(_: dict = Depends(require_admin_api)):
    return {"ok": True, "admins": auth_db.list_admins()}


class AdminBody(BaseModel):
    username: str
    password: str
    password_confirm: str
    note: str = ""  # 备注：说明该管理员的用途/背景（可选）


@app.post("/api/admins")
def api_admin_create(body: AdminBody, request: Request,
                     admin: dict = Depends(require_admin_api)):
    if body.password != body.password_confirm:
        return JSONResponse({"ok": False, "detail": "两次输入的密码不一致"}, status_code=400)
    try:
        created = auth_db.create_admin(body.username, body.password, note=body.note)
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    note_part = f"（备注：{body.note.strip()}）" if body.note.strip() else ""
    _log_from_admin(request, "admin_create", admin["username"],
                    detail=f"新增管理员 {created['username']}{note_part}")
    return {"ok": True, "admin": created}


@app.post("/api/admins/{admin_id}/toggle")
def api_admin_toggle(admin_id: int, request: Request,
                     admin: dict = Depends(require_admin_api)):
    if admin_id == admin["id"]:
        return JSONResponse({"ok": False, "detail": "不能停用自己的账户"}, status_code=400)
    target = auth_db.get_admin_by_id(admin_id)
    if target is None:
        return JSONResponse({"ok": False, "detail": "管理员不存在"}, status_code=404)
    if target["is_super"]:
        return JSONResponse({"ok": False,
                             "detail": "超级管理员不能被停用"}, status_code=400)
    new_status = "disabled" if target["status"] == "active" else "active"
    auth_db.update_admin(admin_id, status=new_status)
    state = "启用" if new_status == "active" else "停用"
    _log_from_admin(request, "admin_toggle", admin["username"],
                    detail=f"{target['username']} → {state}")
    return {"ok": True, "status": new_status}


@app.post("/api/admins/{admin_id}/reset")
def api_admin_reset(admin_id: int, body: ResetPasswordBody, request: Request,
                    admin: dict = Depends(require_admin_api)):
    try:
        auth_db.reset_admin_password(admin_id, body.new_password)
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    target = auth_db.get_admin_by_id(admin_id)
    name = target["username"] if target else str(admin_id)
    _log_from_admin(request, "admin_password_reset", admin["username"],
                    detail=f"重置管理员 {name} 的密码")
    return {"ok": True, "detail": f"管理员「{name}」的密码已重置"}


@app.delete("/api/admins/{admin_id}")
def api_admin_delete(admin_id: int, request: Request,
                     admin: dict = Depends(require_admin_api)):
    target = auth_db.get_admin_by_id(admin_id)
    if target is None:
        return JSONResponse({"ok": False, "detail": "管理员不存在"}, status_code=404)
    if target["is_super"]:
        return JSONResponse({"ok": False,
                             "detail": "超级管理员（admin）不能删除"}, status_code=400)
    if admin_id == admin["id"]:
        return JSONResponse({"ok": False, "detail": "不能删除自己的账户"}, status_code=400)
    try:
        auth_db.delete_admin(admin_id)
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    _log_from_admin(request, "admin_delete", admin["username"],
                    detail=f"删除管理员 {target['username']}")
    return {"ok": True}


class TransferBody(BaseModel):
    department_id: int


class ProfileBody(BaseModel):
    real_name: str = ""
    note: str = ""


@app.post("/api/users/{user_id}/profile")
def api_user_profile(user_id: int, body: ProfileBody, request: Request,
                     admin: dict = Depends(require_admin_api)):
    user = auth_db.get_user_by_id(user_id)
    if user is None:
        return JSONResponse({"ok": False, "detail": "用户不存在"}, status_code=404)
    auth_db.update_user(user_id, real_name=body.real_name, note=body.note)
    _log_from_admin(request, "profile_update", admin["username"],
                    detail=f"更新 {user['username']} 的姓名/备注"
                           f"（{body.real_name or '—'} / {body.note or '—'}）")
    return {"ok": True, "real_name": body.real_name.strip(), "note": body.note.strip()}


@app.post("/api/users/{user_id}/department")
def api_user_transfer(user_id: int, body: TransferBody, request: Request,
                      admin: dict = Depends(require_admin_api)):
    user = auth_db.get_user_by_id(user_id)
    if user is None:
        return JSONResponse({"ok": False, "detail": "用户不存在"}, status_code=404)
    auth_db.update_user(user_id, department_id=body.department_id)
    deps = auth_db.list_departments()
    dep_name = next((d["name"] for d in deps if d["id"] == body.department_id),
                    str(body.department_id))
    _log_from_admin(request, "employee_transfer", admin["username"],
                    detail=f"{user['username']} → {dep_name}", department=dep_name)
    return {"ok": True, "department": dep_name}


# ---------------------------------------------------------------- JSON API：访问令牌
@app.get("/api/tokens")
def api_tokens(_: dict = Depends(require_admin_api)):
    return {"ok": True, "tokens": auth_db.list_tokens()}


class TokenBody(BaseModel):
    username: str
    name: str = ""


@app.post("/api/tokens")
def api_token_create(body: TokenBody, request: Request,
                     admin: dict = Depends(require_admin_api)):
    user = auth_db.get_user((body.username or "").strip())
    if user is None:
        return JSONResponse({"ok": False, "detail": "该账户名不存在"}, status_code=400)
    plaintext = auth_db.create_token(user["id"], body.name)
    _log_from_admin(request, "token_create", admin["username"],
                    detail=f"为 {user['username']} 签发令牌（{body.name or '未命名'}）")
    return {"ok": True, "plaintext": plaintext,
            "detail": "令牌只显示这一次，请立即复制保存"}


@app.post("/api/tokens/revoke")
def api_token_revoke(request: Request, admin: dict = Depends(require_admin_api),
                     token_prefix: str = Query(...)):
    auth_db.revoke_token(token_prefix)
    _log_from_admin(request, "token_revoke", admin["username"],
                    detail=f"吊销令牌 {token_prefix}…")
    return {"ok": True}


# ---------------------------------------------------------------- JSON API：日志
@app.get('/api/logs/admin-operations')
def api_admin_operations(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                         username: str = Query('', max_length=80),
                         action: str = Query('', max_length=100), success: bool | None = None,
                         snapshot_id: int = Query(0, ge=0),
                         _: dict = Depends(require_admin_api)):
    return admin_audit.list_operations(page=page, page_size=page_size, username=username,
                                       action=action, success=success, snapshot_id=snapshot_id)


@app.get("/api/logs/auth")
def api_logs_auth(limit: int = Query(20, ge=1, le=200), page: int = Query(1, ge=1),
                  username: str = "", action: str = "",
                  scope: str | None = Query(None),
                  login_only: bool = False,
                  _: dict = Depends(require_admin_api)):
    """登录日志：分页（page 从 1 起）；scope="admins"/"users" 分离管理员与员工。"""
    total = auth_db.count_login_logs(username=username, action=action, scope=scope, login_only=login_only)
    total_pages = max(1, (total + limit - 1) // limit)
    page = min(page, total_pages)
    offset = (page - 1) * limit
    logs = auth_db.list_login_logs(limit=limit, offset=offset,
                                   username=username, action=action, scope=scope, login_only=login_only)
    return {"ok": True, "logs": logs, "total": total,
            "page": page, "page_size": limit, "total_pages": total_pages}


@app.get("/api/logs/qa")
def api_logs_qa(limit: int = Query(20, le=100), page: int = Query(1, ge=1),
                username: str = "", entry: str = "", department: str = "",
                _: dict = Depends(require_admin_api)):
    """RAG 使用日志（只产生于员工——管理员已不能登录前台/MCP）。"""
    offset = (page - 1) * limit
    logs = auth_db.list_qa_logs(limit=limit, offset=offset, username=username,
                                entry=entry, department=department)
    total = auth_db.count_qa_logs(username=username, entry=entry,
                                  department=department)
    return {"ok": True, "logs": logs, "total": total,
            "page": page, "page_size": limit}


def _can_reveal_ingest_path(request: Request) -> bool:
    if sys.platform not in ("win32", "darwin") or request.client is None:
        return False

    def is_loopback(host: str | None) -> bool:
        try:
            address = ip_address(host or "")
            return (getattr(address, "ipv4_mapped", None) or address).is_loopback
        except ValueError:
            return False

    host = request.url.hostname
    return is_loopback(request.client.host) and (host == "localhost" or is_loopback(host))


def _resolve_ingest_path(root: Path, relative: str) -> Path | None:
    try:
        path = Path(relative)
        if not relative or path.anchor or ".." in path.parts:
            return None
        target = (root / path).resolve()
        return target if target != root and target.is_relative_to(root) else None
    except (OSError, RuntimeError, ValueError):
        return None


@app.get("/api/logs/ingest")
def api_logs_ingest(request: Request, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                    username: str = Query("", max_length=80), keyword: str = Query("", max_length=256),
                    batch_id: str = Query("", max_length=32),
                    entry: Literal["", "file", "directory", "web"] = "",
                    outcome: Literal["", "running", "ingested", "skipped", "failed"] = "",
                    since: datetime | None = None, before: datetime | None = None,
                    _: dict = Depends(require_admin_api)):
    if any(date is not None and date.utcoffset() is None for date in (since, before)):
        raise HTTPException(status_code=422, detail="时间范围必须包含时区")
    if since is not None and before is not None and since >= before:
        raise HTTPException(status_code=422, detail="起始时间须早于截止时间")
    result = ingest_audit.list_logs(page=page, page_size=page_size, username=username.strip(),
                                   keyword=keyword.strip(), batch_id=batch_id, entry=entry,
                                   outcome=outcome, since=since, before=before)
    root = get_config().knowledge_dir.resolve()
    reveal_available = _can_reveal_ingest_path(request)
    for log in result["logs"]:
        target = _resolve_ingest_path(root, log["path"])
        log["absolute_path"] = str(target) if target is not None else ""
        log["can_reveal"] = reveal_available and target is not None and target.is_file()
    result["reveal_available"] = reveal_available
    result["department_labels"] = {
        department["permission_key"]: department["name"] for department in auth_db.list_departments()
    } if any(log["attributes"].get("classification") == "内部" for log in result["logs"]) else {}
    return result


@app.post("/api/logs/ingest/{log_id}/reveal")
def api_reveal_ingest_file(request: Request, log_id: int = PathParam(ge=1, le=2**63 - 1),
                           _: dict = Depends(require_admin_api)):
    if not _can_reveal_ingest_path(request):
        raise HTTPException(status_code=403, detail="请在知识库所在电脑上通过 localhost 打开管理后台，再定位文件。")
    relative = ingest_audit.get_log_path(log_id)
    if relative is None:
        raise HTTPException(status_code=404, detail="找不到这条入库记录。")
    target = _resolve_ingest_path(get_config().knowledge_dir.resolve(), relative)
    if target is None or not target.is_file():
        raise HTTPException(status_code=404, detail="记录中的文件已移动、删除或尚未写入知识库。")
    try:
        if sys.platform == "win32":
            os.startfile(str(target.parent), "open")
        else:
            import subprocess

            subprocess.Popen(["/usr/bin/open", "-R", str(target)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        logging.getLogger(__name__).warning("Could not reveal ingestion log file %s", log_id, exc_info=True)
        raise HTTPException(status_code=503, detail="无法打开文件管理器，请复制路径后手动打开。") from exc
    return {"ok": True}


@app.get("/api/llm/costs")
def api_llm_costs(_: dict = Depends(require_admin_api),
                  days: int = Query(0, ge=0, le=365),
                  username: str = "",
                  entry: str | None = None,
                  username_exact: bool = False,
                  page: int = Query(1, ge=1),
                  page_size: int = Query(20, ge=1, le=100)):
    """云端 LLM 消耗统计（V3.9）：从 qa_logs 的 usage 逐条计算费用并汇总。

    费用 = 未命中输入 × 输入单价 + 命中缓存输入 × 缓存单价 + 输出 × 输出单价；
    单价（元/百万 tokens）实时读取 settings.json（与前台「模型配置」页同一来源），
    改单价后历史记录按新价重算。days：最近 N 天（0=全部）；username：按用户过滤。
    username_exact 启用账户精确匹配；entry=None 为全部，空串为历史未记录入口。
    汇总（totals）始终覆盖全部符合条件的数据，分页只影响明细行（rows）。
    """
    from datetime import datetime, timedelta

    from src.app_settings import PRICE_DEFAULTS, load_app_settings

    settings = load_app_settings()
    price_in = float(settings.get("llm_price_input", PRICE_DEFAULTS["llm_price_input"]))
    price_cached = float(settings.get("llm_price_input_cached",
                                      PRICE_DEFAULTS["llm_price_input_cached"]))
    price_out = float(settings.get("llm_price_output", PRICE_DEFAULTS["llm_price_output"]))

    def _cost(uncached: int, cached: int, output: int) -> float:
        return (uncached * price_in + cached * price_cached + output * price_out) / 1_000_000

    cutoff = (datetime.now() - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S") if days else ""
    totals, rows, page, total_pages = auth_db.llm_usage_page(
        username, cutoff, page, page_size, entry=entry, username_exact=username_exact)
    total_items = totals.pop("total_rows")
    totals["cost"] = round(_cost(totals.pop("uncached"), totals["input_cached"], totals["output"]), 2)
    for row in rows:
        row["estimated"] = bool(row["estimated"])
        row["cost"] = round(_cost(max(0, row["input"] - row["input_cached"]),
                                  row["input_cached"], row["output"]), 4)
    return {"ok": True, "totals": totals, "rows": rows,
            "page": page, "page_size": page_size,
            "total_rows": total_items, "total_pages": total_pages,
            "filters": auth_db.llm_usage_filter_options(),
            "prices": {"input": price_in, "input_cached": price_cached,
                       "output": price_out}}


# ---------------------------------------------------------------- 认证 API（Streamlit 前台调用）
class ContextFields(BaseModel):
    """浏览器上下文（Streamlit 转发真实用户的 UA/语言/IP，用于登录日志指纹与溯源）。"""
    user_agent: str = ""
    accept_language: str = ""
    client_ip: str = ""  # 登录门 JS ping 写下的 rip cookie（浏览器真实局域网 IP）


class RegisterBody(ContextFields):
    username: str
    password: str
    password_confirm: str
    department_id: int  # 必填：注册必须归属部门；部门由管理员在后台创建


class LoginBody(ContextFields):
    username: str
    password: str


class BootstrapAdminBody(ContextFields):
    username: str
    password: str
    password_confirm: str


def _web_ip(request: Request) -> str:
    """Streamlit 转发来的真实用户 IP（走反代时是 X-Forwarded-For）；拿不到就留空。"""
    return request.headers.get("x-forwarded-for", "")


@app.post("/auth/register")
def api_register(body: RegisterBody, request: Request):
    # 账户名全局唯一：与管理员重名的注册请求直接拒绝（否则前台登录会被
    # 管理员拦截规则挡住，员工账户形同虚设）
    if auth_db.admin_exists((body.username or "").strip()):
        auth_db.log_auth_event("register", body.username, success=False,
                               detail="该账户名已被管理员占用", entry="web",
                               ip=(body.client_ip or _web_ip(request)),
                               user_agent=body.user_agent,
                               accept_language=body.accept_language)
        return JSONResponse({"ok": False, "detail": "该账户名已被管理员占用"},
                            status_code=400)
    if body.password != body.password_confirm:
        auth_db.log_auth_event("register", body.username, success=False,
                               detail="两次密码不一致", entry="web",
                               ip=(body.client_ip or _web_ip(request)), user_agent=body.user_agent,
                               accept_language=body.accept_language)
        return JSONResponse({"ok": False, "detail": "两次输入的密码不一致"}, status_code=400)
    dep = next((d for d in auth_db.list_departments(include_disabled=False)
                if d["id"] == body.department_id), None)
    if dep is None:
        auth_db.log_auth_event("register", body.username, success=False,
                               detail="部门无效", entry="web",
                               ip=(body.client_ip or _web_ip(request)), user_agent=body.user_agent,
                               accept_language=body.accept_language)
        return JSONResponse({"ok": False, "detail": "请选择一个有效部门"}, status_code=400)
    try:
        user = auth_db.create_user(body.username, body.password,
                                   department_id=dep["id"])
    except ValueError as exc:
        auth_db.log_auth_event("register", body.username, success=False,
                               detail=str(exc), entry="web",
                               ip=(body.client_ip or _web_ip(request)), user_agent=body.user_agent,
                               accept_language=body.accept_language)
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    auth_db.log_auth_event("register", user["username"], success=True,
                           department=user.get("department", ""), entry="web",
                           ip=(body.client_ip or _web_ip(request)), user_agent=body.user_agent,
                           accept_language=body.accept_language)
    return {"ok": True, "username": user["username"], "department": user["department"]}


@app.post("/auth/login")
def api_login(body: LoginBody, request: Request):
    username = (body.username or "").strip()
    # 硬性规则：管理员账户禁止登录前台（无论密码对错，一律拒绝）
    if auth_db.admin_exists(username):
        auth_db.log_auth_event("login", username, success=False,
                               detail="管理员账户禁止在前端登录", entry="web",
                               ip=(body.client_ip or _web_ip(request)),
                               user_agent=body.user_agent,
                               accept_language=body.accept_language)
        return JSONResponse(
            {"ok": False,
             "detail": "管理员账户不能登录前台，请使用管理后台（8700 端口）"},
            status_code=403)
    info, err = auth_db.authenticate(username, body.password)
    ip = _web_ip(request)
    if err or info is None:
        auth_db.log_auth_event("login", body.username, success=False,
                               detail=err or "登录失败", entry="web", ip=(body.client_ip or ip),
                               user_agent=body.user_agent,
                               accept_language=body.accept_language)
        return JSONResponse({"ok": False, "detail": err or "登录失败"}, status_code=401)
    auth_db.log_auth_event("login", info["username"], success=True,
                           department=info.get("department", ""), entry="web",
                           ip=(body.client_ip or ip), user_agent=body.user_agent,
                           accept_language=body.accept_language)
    return {"ok": True, "user": info,
            "login_proof": auth_db.make_login_proof(info["username"])}


class LogoutBody(ContextFields):
    username: str


@app.post("/auth/logout")
def api_logout(body: LogoutBody, request: Request):
    auth_db.log_auth_event("logout", body.username, success=True, entry="web",
                           ip=(body.client_ip or _web_ip(request)),
                           user_agent=body.user_agent,
                           accept_language=body.accept_language)
    return {"ok": True}


@app.get("/auth/bootstrap")
def api_bootstrap():
    """Streamlit 登录门的引导信息：是否有管理员/员工 + 可注册部门。"""
    return {"has_users": auth_db.count_users() > 0,
            "has_admins": auth_db.count_admins() > 0,
            "departments": [{"id": d["id"], "name": d["name"]}
                            for d in auth_db.list_departments(include_disabled=False)]}


class BootstrapAdminBody(ContextFields):
    username: str
    password: str
    password_confirm: str


@app.post("/auth/bootstrap-admin")
def api_bootstrap_admin(body: BootstrapAdminBody, request: Request):
    """仅当系统中还没有任何管理员时可用（首启初始化管理员，写入 admins 表）。

    初始化的管理员不能登录前台（硬性规则）——请到管理后台（8700）使用。
    """
    if auth_db.count_admins() > 0:
        return JSONResponse({"ok": False,
                             "detail": "系统已有管理员，初始化入口已关闭"}, status_code=400)
    if body.password != body.password_confirm:
        return JSONResponse({"ok": False, "detail": "两次输入的密码不一致"}, status_code=400)
    try:
        admin = auth_db.create_admin(body.username, body.password, is_super=True)
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    auth_db.log_auth_event("bootstrap_admin", admin["username"], success=True,
                           entry="admin", ip=(body.client_ip or _web_ip(request)),
                           user_agent=body.user_agent,
                           accept_language=body.accept_language)
    request.state.admin_audit_event = dict(username=admin['username'], success=True, detail='初始化管理员')
    return {"ok": True, "username": admin["username"]}


class PrefsBody(BaseModel):
    username: str
    settings: dict


class PersistLoginBody(BaseModel):
    username: str
    proof: str


def _persist_login_impl(username: str, proof: str, back: str):
    """proof 校验 + Set-Cookie + 返回导航回前台的脚本页。公共实现。"""
    username_ok = auth_db.consume_login_proof(proof)
    if not username_ok or username_ok != username:
        return HTMLResponse("<script>history.back()</script>", status_code=401)
    user = auth_db.get_user(username)
    if user is None or user["status"] != "active":
        return HTMLResponse("<script>history.back()</script>", status_code=401)
    # back 必须以 / 开头（防开放重定向）
    back = back if back.startswith("/") else "/"
    resp = HTMLResponse(f'<script>window.location.href = "{back}";</script>')
    resp.set_cookie("front_auth", auth_db.make_session_cookie(username),
                    samesite="lax", max_age=30 * 24 * 3600)
    return resp


@app.get("/auth/persist-login")
def api_persist_login_get(request: Request, username: str = Query(""),
                          proof: str = Query(""), back: str = Query("")):
    """顶层导航版：浏览器以第一方身份 GET 本端点（无 CORS、无三方 Cookie 限制），
    Set-Cookie 后自动导航回前台。"""
    from urllib.parse import unquote
    return _persist_login_impl(unquote(username), unquote(proof), unquote(back))


@app.post("/auth/persist-login")
def api_persist_login_post(body: PersistLoginBody):
    username = auth_db.verify_login_proof(body.proof)
    if not username or username != body.username:
        return JSONResponse({"ok": False, "detail": "凭证无效"}, status_code=401)
    user = auth_db.get_user(username)
    if user is None or user["status"] != "active":
        return JSONResponse({"ok": False, "detail": "账户不可用"}, status_code=401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("front_auth", auth_db.make_session_cookie(username),
                    samesite="lax", max_age=30 * 24 * 3600)
    return resp


@app.post("/auth/clear-front-cookie")
def api_clear_front_cookie():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("front_auth", samesite="lax")
    return resp


@app.get("/auth/clear-front-cookie")
def api_clear_front_cookie_get(request: Request, back: str = Query("")):
    """顶层导航版（与 persist-login GET 对应）：删除 Cookie 后导航回前台。
    back 须以 / 开头（防开放重定向）；缺省回前台首页。"""
    from urllib.parse import unquote
    front_host = request.headers.get("referer", "").split("/")[2] if "://" \
        in request.headers.get("referer", "") else "localhost:8501"
    target = unquote(back or f"//{front_host}/")
    target = target if target.startswith("/") else "/"
    resp = HTMLResponse(f'<script>window.location.href = "{target}";</script>')
    resp.delete_cookie("front_auth", samesite="lax")
    return resp


@app.get("/auth/ping")
def api_ping(request: Request):
    """登录门的隐藏组件直连本端点：让浏览器记下自己的真实局域网 IP（rip cookie，
    与 Streamlit(8501) 共享 localhost 域），登录请求随后从 cookie 读回传给日志。"""
    ip = request.client.host if request.client else ""
    resp = JSONResponse({"ok": True, "ip": ip})
    resp.set_cookie("rip", ip, samesite="lax", max_age=86400 * 30)
    # CORS 只对本端点开放：调用方是 srcdoc iframe（Origin: null），需带凭证；
    # 响应只含调用者自己的 IP，无敏感信息，回显 Origin 无风险
    origin = request.headers.get("origin")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Vary"] = "Origin"
    return resp


@app.get("/auth/prefs")
def api_get_prefs(username: str):
    return {"ok": True, "prefs": auth_db.get_user_prefs(username)}


@app.post("/auth/prefs")
def api_save_prefs(body: PrefsBody):
    auth_db.save_user_prefs(body.username, body.settings)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0",
                port=int(os.environ.get("AUTH_ADMIN_PORT", "8700")))

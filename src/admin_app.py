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
import os
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src import auth_db

app = FastAPI(title="Sky Personal RAG · 管理后台", docs_url=None, redoc_url=None)
# V3.7：允许知识库前台（8501）的浏览器直接 ping 本服务以记录真实客户端 IP
# （Cookie 不区分端口，ping 写下的 rip cookie 会被浏览器带给 8501，Streamlit 读取后随登录请求传回）
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "admin_templates"))

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
    """管理后台自身发生的认证事件（真实浏览器 UA + IP）。"""
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
    return templates.TemplateResponse(request, "login.html",
                                      {"err": err, "msg": ""})


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
def api_overview(_: dict = Depends(require_admin_api)):
    deps = auth_db.list_departments()
    users = auth_db.list_users()
    admins = auth_db.list_admins()
    logs = auth_db.list_login_logs(limit=10, scope="users")
    return {
        "departments": len(deps),
        "departments_enabled": len([d for d in deps if not d["disabled"]]),
        "users": len(users),
        "users_active": len([u for u in users if u["status"] == "active"]),
        "admins": len(admins),
        "admins_active": len([a for a in admins if a["status"] == "active"]),
        "recent_logins": logs,
    }


# ---------------------------------------------------------------- JSON API：部门
@app.get("/api/departments")
def api_departments(_: dict = Depends(require_admin_api)):
    deps = auth_db.list_departments()
    counts = auth_db.department_counts()
    for d in deps:
        d["employees"] = counts.get(d["id"], 0)
    return {"ok": True, "departments": deps}


class DepartmentBody(BaseModel):
    name: str
    description: str = ""


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
    auth_db.update_user(user_id, status=new_status)
    state = "启用" if new_status == "active" else "停用"
    _log_from_admin(request, "employee_toggle", admin["username"],
                    detail=f"{user['username']} → {state}")
    return {"ok": True, "status": new_status}


# ---------------------------------------------------------------- JSON API：管理员（admins 表，与员工分列）
@app.get("/api/admins")
def api_admins_list(_: dict = Depends(require_admin_api)):
    return {"ok": True, "admins": auth_db.list_admins()}


class AdminBody(BaseModel):
    username: str
    password: str
    password_confirm: str


@app.post("/api/admins")
def api_admin_create(body: AdminBody, request: Request,
                     admin: dict = Depends(require_admin_api)):
    if body.password != body.password_confirm:
        return JSONResponse({"ok": False, "detail": "两次输入的密码不一致"}, status_code=400)
    try:
        created = auth_db.create_admin(body.username, body.password)
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    _log_from_admin(request, "admin_create", admin["username"],
                    detail=f"新增管理员 {created['username']}")
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
@app.get("/api/logs/auth")
def api_logs_auth(limit: int = Query(20, le=200), page: int = Query(1, ge=1),
                  username: str = "", action: str = "",
                  scope: str | None = Query(None),
                  _: dict = Depends(require_admin_api)):
    """登录日志：分页（page 从 1 起）；scope="admins"/"users" 分离管理员与员工。"""
    offset = (page - 1) * limit
    logs = auth_db.list_login_logs(limit=limit, offset=offset,
                                   username=username, action=action, scope=scope)
    total = auth_db.count_login_logs(username=username, action=action, scope=scope)
    return {"ok": True, "logs": logs, "total": total,
            "page": page, "page_size": limit}


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
    department_id: int


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

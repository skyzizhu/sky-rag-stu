"""V3.7：FastAPI 管理后台（独立服务，默认端口 8700）。

职责：部门管理 / 员工管理 / 管理员登录（含重置密码）/ 认证 API（供 Streamlit 前台调用）。
- 页面：Jinja2 极简模板（src/admin_templates/），管理员内部工具不做多语言
- 会话：HMAC 签名 Cookie（secret 落盘 storage/auth/admin_secret.key，仅本服务用）
- 与 Streamlit 共用 src/auth_db.py 的同一份 SQLite
- 启动：uvicorn src.admin_app:app --port 8700（start.sh 自动拉起）
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from src import auth_db

app = FastAPI(title="Sky Personal RAG · 管理后台", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "admin_templates"))

_SESSION_COOKIE = "admin_sess"
_SESSION_TTL = 7 * 24 * 3600


# ---------------------------------------------------------------- 会话签名
_SECRET_CACHE: bytes | None = None


def _secret() -> bytes:
    """HMAC 签名密钥：首启生成并落盘（0600），此后常驻缓存。"""
    global _SECRET_CACHE
    if _SECRET_CACHE is not None:
        return _SECRET_CACHE
    if auth_db.SECRET_PATH.exists():
        _SECRET_CACHE = auth_db.SECRET_PATH.read_bytes()
    else:
        secret = secrets.token_hex(32).encode()
        auth_db.AUTH_DIR.mkdir(parents=True, exist_ok=True)
        auth_db.SECRET_PATH.write_bytes(secret)
        os.chmod(auth_db.SECRET_PATH, 0o600)
        _SECRET_CACHE = secret
    return _SECRET_CACHE


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def _make_cookie(username: str) -> str:
    expires = str(int(time.time()) + _SESSION_TTL)
    return f"{expires}|{username}|{_sign(f'{expires}|{username}')}"


def _read_cookie(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.split("|")
    if len(parts) != 3:
        return None
    expires, username, sig = parts
    if not hmac.compare_digest(_sign(f"{expires}|{username}"), sig):
        return None
    if int(expires) < int(time.time()):
        return None
    return username


def current_admin(request: Request) -> dict | None:
    """从 Cookie 解析当前管理员；非管理员/未登录返回 None。"""
    username = _read_cookie(request.cookies.get(_SESSION_COOKIE))
    if not username:
        return None
    user = auth_db.get_user(username)
    if user is None or not user["is_admin"] or user["status"] != "active":
        return None
    return user


def require_admin(request: Request) -> dict:
    admin = current_admin(request)
    if admin is None:
        raise HTTPRedirect("/admin/login?err=" + _quote("请先以管理员身份登录"))
    return admin


class HTTPRedirect(Exception):
    """表单路由里让 FastAPI 返回重定向的轻量约定（在 handler 内转成 RedirectResponse）。"""

    def __init__(self, location: str):
        self.location = location
        super().__init__(location)


def _quote(text: str) -> str:
    from urllib.parse import quote
    return quote(text)


@app.exception_handler(HTTPRedirect)
async def _http_redirect_handler(_: Request, exc: HTTPRedirect):
    return RedirectResponse(url=exc.location, status_code=303)


@app.on_event("startup")
def _startup() -> None:
    auth_db.init_db()
    _secret()  # 首次启动生成签名密钥


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
def login_action(request: Request, username: str = Form(""), password: str = Form("")):
    info, err = auth_db.authenticate(username, password)
    if err or not info or not info.get("is_admin"):
        msg = err or "该账户不是管理员"
        return templates.TemplateResponse(request, "login.html",
                                          {"err": msg, "msg": ""}, status_code=401)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(_SESSION_COOKIE, _make_cookie(info["username"]),
                    httponly=True, samesite="lax")
    return resp


@app.get("/admin/logout")
def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(_SESSION_COOKIE)
    return resp


# ---------------------------------------------------------------- 页面：工作台
@app.get("/admin", response_class=HTMLResponse)
def dashboard(request: Request, admin: dict = Depends(require_admin)):
    deps = auth_db.list_departments()
    users = auth_db.list_users()
    ctx = {
        "admin": admin, "msg": request.query_params.get("msg", ""),
        "err": request.query_params.get("err", ""),
        "n_departments": len(deps), "n_enabled_deps": len([d for d in deps if not d["disabled"]]),
        "n_users": len(users),
        "n_active_users": len([u for u in users if u["status"] == "active"]),
    }
    return templates.TemplateResponse(request, "dashboard.html", ctx)


# ---------------------------------------------------------------- 页面：部门管理
@app.get("/admin/departments", response_class=HTMLResponse)
def departments_page(request: Request, admin: dict = Depends(require_admin),
                     msg: str = "", err: str = ""):
    deps = auth_db.list_departments()
    counts = auth_db.department_counts()
    for d in deps:
        d["employees"] = counts.get(d["id"], 0)
    return templates.TemplateResponse(request, "departments.html",
                                      {"admin": admin, "departments": deps,
                                       "msg": msg, "err": err})


@app.post("/admin/departments/create")
def department_create(name: str = Form(""), description: str = Form(""),
                      _: dict = Depends(require_admin)):
    try:
        auth_db.create_department(name, description)
    except ValueError as exc:
        raise HTTPRedirect(f"/admin/departments?err={_quote(str(exc))}")
    raise HTTPRedirect(f"/admin/departments?msg={_quote(f'部门「{name.strip()}」已创建')}")


@app.post("/admin/departments/{dep_id}/toggle")
def department_toggle(dep_id: int, _: dict = Depends(require_admin)):
    dep = next((d for d in auth_db.list_departments() if d["id"] == dep_id), None)
    if dep is None:
        raise HTTPRedirect("/admin/departments?err=" + _quote("部门不存在"))
    auth_db.set_department_disabled(dep_id, not bool(dep["disabled"]))
    state = "启用" if dep["disabled"] else "停用"
    msg = f"部门「{dep['name']}」已{state}"
    raise HTTPRedirect(f"/admin/departments?msg={_quote(msg)}")


@app.post("/admin/departments/{dep_id}/delete")
def department_delete(dep_id: int, _: dict = Depends(require_admin)):
    dep = next((d for d in auth_db.list_departments() if d["id"] == dep_id), None)
    name = dep["name"] if dep else str(dep_id)
    try:
        auth_db.delete_department(dep_id)
    except ValueError as exc:
        raise HTTPRedirect(f"/admin/departments?err={_quote(str(exc))}")
    raise HTTPRedirect(f"/admin/departments?msg={_quote(f'部门「{name}」已删除')}")


# ---------------------------------------------------------------- 页面：员工管理
@app.get("/admin/employees", response_class=HTMLResponse)
def employees_page(request: Request, admin: dict = Depends(require_admin),
                   dep: int | None = None, msg: str = "", err: str = ""):
    users = auth_db.list_users(department_id=dep)
    deps = auth_db.list_departments()
    dep_name = next((d["name"] for d in deps if d["id"] == dep), None) if dep else None
    return templates.TemplateResponse(request, "employees.html",
                                      {"admin": admin, "users": users,
                                       "departments": deps, "filter_dep": dep,
                                       "filter_dep_name": dep_name,
                                       "msg": msg, "err": err})


@app.post("/admin/employees/create")
def employee_create(username: str = Form(""), password: str = Form(""),
                    department_id: int = Form(...), _: dict = Depends(require_admin)):
    try:
        user = auth_db.create_user(username, password, department_id=department_id)
    except ValueError as exc:
        raise HTTPRedirect(f"/admin/employees?err={_quote(str(exc))}")
    msg = f"员工「{user['username']}」已创建"
    raise HTTPRedirect(f"/admin/employees?msg={_quote(msg)}")


@app.post("/admin/employees/{user_id}/reset")
def employee_reset(user_id: int, new_password: str = Form(""),
                   _: dict = Depends(require_admin)):
    try:
        auth_db.reset_password(user_id, new_password)
    except ValueError as exc:
        raise HTTPRedirect(f"/admin/employees?err={_quote(str(exc))}")
    user = auth_db.get_user_by_id(user_id)
    name = user["username"] if user else str(user_id)
    msg = (f"「{name}」的密码已重置为：{new_password}"
           f"（请告知本人，此提示仅显示一次）")
    raise HTTPRedirect(f"/admin/employees?msg={_quote(msg)}")


@app.post("/admin/employees/{user_id}/toggle")
def employee_toggle(user_id: int, _: dict = Depends(require_admin)):
    user = auth_db.get_user_by_id(user_id)
    if user is None:
        raise HTTPRedirect("/admin/employees?err=" + _quote("用户不存在"))
    new_status = "disabled" if user["status"] == "active" else "active"
    auth_db.update_user(user_id, status=new_status)
    state = "启用" if new_status == "active" else "停用"
    msg = f"账户「{user['username']}」已{state}"
    raise HTTPRedirect(f"/admin/employees?msg={_quote(msg)}")


@app.post("/admin/employees/{user_id}/department")
def employee_department(user_id: int, department_id: int = Form(...),
                        _: dict = Depends(require_admin)):
    user = auth_db.get_user_by_id(user_id)
    if user is None:
        raise HTTPRedirect("/admin/employees?err=" + _quote("用户不存在"))
    auth_db.update_user(user_id, department_id=department_id)
    deps = auth_db.list_departments()
    dep_name = next((d["name"] for d in deps if d["id"] == department_id),
                    str(department_id))
    msg = f"「{user['username']}」已调入「{dep_name}」"
    raise HTTPRedirect(f"/admin/employees?msg={_quote(msg)}")


# ---------------------------------------------------------------- 页面：访问令牌
@app.get("/admin/tokens", response_class=HTMLResponse)
def tokens_page(request: Request, admin: dict = Depends(require_admin),
                msg: str = "", err: str = ""):
    return templates.TemplateResponse(request, "tokens.html",
                                      {"admin": admin, "tokens": auth_db.list_tokens(),
                                       "new_token": request.query_params.get("new_token", ""),
                                       "msg": msg, "err": err})


@app.post("/admin/tokens/create")
def token_create(request: Request, name: str = Form(""),
                 username: str = Form(...), _: dict = Depends(require_admin)):
    user = auth_db.get_user((username or "").strip())
    if user is None:
        raise HTTPRedirect("/admin/tokens?err=" + _quote("该账户名不存在"))
    plaintext = auth_db.create_token(user["id"], name)
    raise HTTPRedirect("/admin/tokens?new_token=" + _quote(plaintext))


@app.post("/admin/tokens/revoke")
def token_revoke(token_prefix: str = Form(...), _: dict = Depends(require_admin)):
    auth_db.revoke_token(token_prefix)
    raise HTTPRedirect("/admin/tokens?msg=" + _quote("令牌已吊销"))


# ---------------------------------------------------------------- 认证 API（Streamlit 前台调用）
class RegisterBody(BaseModel):
    username: str
    password: str
    password_confirm: str
    department_id: int


class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/auth/register")
def api_register(body: RegisterBody):
    if body.password != body.password_confirm:
        return JSONResponse({"ok": False, "detail": "两次输入的密码不一致"}, status_code=400)
    dep = next((d for d in auth_db.list_departments(include_disabled=False)
                if d["id"] == body.department_id), None)
    if dep is None:
        return JSONResponse({"ok": False, "detail": "请选择一个有效部门"}, status_code=400)
    try:
        user = auth_db.create_user(body.username, body.password, department_id=dep["id"])
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    return {"ok": True, "username": user["username"], "department": user["department"]}


@app.post("/auth/login")
def api_login(body: LoginBody):
    info, err = auth_db.authenticate(body.username, body.password)
    if err or info is None:
        return JSONResponse({"ok": False, "detail": err or "登录失败"}, status_code=401)
    return {"ok": True, "user": info}


@app.get("/auth/bootstrap")
def api_bootstrap():
    """Streamlit 登录门的引导信息：是否需要初始化管理员 + 可注册部门。"""
    return {"has_users": auth_db.count_users() > 0,
            "departments": [{"id": d["id"], "name": d["name"]}
                            for d in auth_db.list_departments(include_disabled=False)]}


class BootstrapAdminBody(BaseModel):
    username: str
    password: str
    password_confirm: str


@app.post("/auth/bootstrap-admin")
def api_bootstrap_admin(body: BootstrapAdminBody):
    """仅当系统中还没有任何用户时可用（首启初始化管理员）。"""
    if auth_db.count_users() > 0:
        return JSONResponse({"ok": False,
                             "detail": "系统已有用户，初始化入口已关闭"}, status_code=400)
    if body.password != body.password_confirm:
        return JSONResponse({"ok": False, "detail": "两次输入的密码不一致"}, status_code=400)
    try:
        user = auth_db.create_user(body.username, body.password, is_admin=True)
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    return {"ok": True, "username": user["username"], "is_admin": True}


class PrefsBody(BaseModel):
    username: str
    settings: dict


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

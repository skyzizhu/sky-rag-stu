"""个人知识库 RAG —— Streamlit 界面（侧边菜单导航版）。

启动方式：在项目根目录运行  streamlit run app.py

布局：左侧是菜单（顶部为 Logo 与 Slogan），右侧是对应页面。菜单分两组：
    知识库：💬 知识库问答（横幅 + 流式回答 + 来源卡片）
            📤 上传文档（按领域目录入库，逐文件结果）
            🗂 知识库管理（统计 / 筛选 / 归档·恢复·移除·重新入库）
    设置：🔍 检索设置 · 🧹 维护操作 · 📊 系统状态 · 🧾 参数总览
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
import dataclasses
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import get_args
from urllib.parse import quote, unquote  # front_auth Cookie 值转 ASCII（中文名兼容）

import streamlit as st
from qdrant_client.http.exceptions import ApiException
from streamlit.elements.lib.dialog import DialogWidth

sys.path.insert(0, str(Path(__file__).parent))

from src.app_settings import PRICE_DEFAULTS, SETTINGS_KEYS, load_app_settings, save_app_settings  # noqa: E402
from src import auth_client  # noqa: E402  # V3.7 登录/注册/偏好（FastAPI 认证服务 8700）
from src import auth_db, projects  # noqa: E402
from src.permissions import scope_filters, visible_groups, write_group_options  # noqa: E402
from src.answer_format import format_answer_markdown  # noqa: E402
from src.config import get_config  # noqa: E402
from src.chat_scroll import scroll_chat_to_latest, scroll_follow_stream  # noqa: E402
from src.directory_uploader import (  # noqa: E402
    decode_directory_batch,
    decode_native_directory_uploads,
    directory_drop_uploader,
)
from src.document_department import normalize_department, normalize_department_filter  # noqa: E402
from src.document_type import normalize_doc_type, normalize_doc_type_filter  # noqa: E402
from src.embedding import EmbeddingClient, EmbeddingError  # noqa: E402
from src.llm import LLMError  # noqa: E402
from src.manage import (  # noqa: E402
    ManageError,
    archive as archive_file,
    refetch_web_page,
    reingest as reingest_file,
    remove_documents,
    restore as restore_file,
    update_metadata,
)
from src.web_fetcher import read_snapshot_origin  # noqa: E402  # 识别网页快照（管理页）
from src import domains as domain_reg  # noqa: E402  # V3.8 领域注册表（编号=数据身份，名称仅显示）
from src.i18n import (  # noqa: E402
    FOLLOW_SYSTEM,
    LANGUAGE_ORDER,
    LANGUAGES,
    browser_language,
    current_language,
    domain_label_i18n,
    t,
    tr,
)
from src.ingest_audit import IngestAudit, IngestItem, source_url_for_log  # noqa: E402
from src.parser import SUPPORTED_EXTENSIONS  # noqa: E402
from src.pipeline import (  # noqa: E402
    QAResult, answer_question, answer_stream, ingest_files,
    ENUM_RECALL_K, ENUM_CTX_ITEMS, ENUM_CTX_TOKENS,
)
from src import index_state as istate  # noqa: E402  # V3.8 上传冲突预检读入库台账
from src.upload_plan import (  # noqa: E402  # V3.8 上传/目录导入的重复文档防护
    COPY, KIND_BATCH_DUPLICATE, KIND_BATCH_SAME_NAME, KIND_DUPLICATE, KIND_SAME_NAME,
    KIND_SAME_PATH, NEW, NEW_VERSION, SKIP, VERSION_OF, content_hash, execute_uploads,
    plan_uploads, write_directory_import,
)
from src.answer_evaluator import compute_cost, merge_usage, run_evaluation, stage_timing  # noqa: E402
from src.usage_reporting import usage_for_log  # noqa: E402
from src.plugin import get_installed_plugins, is_installed, install_plugin, uninstall_plugin, PLUGIN_REGISTRY
from src.sessions import (
    SESSIONS_DIR,
    delete_session,
    SessionConflict,
    list_sessions,
    load_session,
    new_session_id,
    save_session,
)  # noqa: E402
from src.vector_store import VectorStoreError, get_vector_store  # noqa: E402

st.set_page_config(page_title="Sky Personal RAG", page_icon="🧠", layout="wide")
cfg = get_config()

# 上传页格式显示：按类别归组（文档 → 表格 → 演示 → 图片 → 网页），仅影响显示顺序；
# 实际接受范围仍以 parser 白名单（SUPPORTED_EXTENSIONS，带点）为准
_UPLOAD_TYPE_GROUPS = [
    ("文档", [".doc", ".docx", ".wps", ".pdf", ".md", ".txt", ".rtf"]),
    ("表格", [".xls", ".xlsx", ".et"]),
    ("演示", [".ppt", ".pptx", ".dps"]),
    ("图片", [".png", ".jpg", ".jpeg", ".bmp"]),
    ("网页", [".html", ".htm"]),
]
SUPPORTED_UPLOAD_TYPES = [ext.lstrip(".") for _label, exts in _UPLOAD_TYPE_GROUPS
                          for ext in exts if ext in SUPPORTED_EXTENSIONS]


def _is_hidden_relpath(parts: tuple[str, ...]) -> bool:
    """目录导入路径里任何一段以 . 开头（隐藏目录/文件，如 .rag_converted 转换缓存、
    .git）都视为隐藏——CLI 入库对它们视而不见（pipeline._skip_hidden），
    前端目录导入同样跳过，避免转换缓存被当新文档重复入库（幻影）。"""
    return any(seg.startswith(".") for seg in parts if seg)

# 跨页面共享的设置项：在「设置与状态」里改，问答页即时生效
st.session_state.setdefault("top_k", cfg.top_k)
st.session_state.setdefault("domain_choice", "all")
st.session_state.setdefault("doc_type_choice", "")
st.session_state.setdefault("department_choice", "")
st.session_state.setdefault("scope_choice", "active")
st.session_state.setdefault("debug_mode", True)
st.session_state.setdefault("query_understanding", cfg.query_understanding)
st.session_state.setdefault("session_id", new_session_id())
st.session_state.setdefault("theme", "system")  # system / light / dark
st.session_state.setdefault("language", FOLLOW_SYSTEM)  # system / zh-CN / zh-TW / en / ja
# 模型单价（元 / 百万 tokens）：费用估算用，默认 DeepSeek 官方价，可在检索设置里改
for _price_key, _price_default in PRICE_DEFAULTS.items():
    st.session_state.setdefault(_price_key, _price_default)


@st.dialog(t("dialog.delete_session.title"), width="small")
def confirm_session_delete() -> None:
    """在真正删除前要求用户二次确认。"""
    request = st.session_state.get("delete_session_request")
    if not request:
        return

    st.write(t("dialog.delete_session.text", title=request["title"]))
    st.caption(t("dialog.delete_session.caption"))
    cancel_col, confirm_col = st.columns(2)
    if cancel_col.button(t("common.cancel"), width="stretch"):
        st.session_state.pop("delete_session_request", None)
        st.rerun()
    if confirm_col.button(
        t("dialog.delete_session.confirm"),
        type="primary",
        icon=":material/delete:",
        width="stretch",
    ):
        sid = request["session_id"]
        owner = str((st.session_state.get("auth_user") or {}).get("username") or "")
        if not delete_session(sid, username=owner):
            st.error(t("chat.session_unavailable"))
            return
        # 删除的是当前正在聊的会话时，同步重置本地状态——否则下次提问会把
        # 刚删除的文件原样重建（会话"复活"）
        if st.session_state.get("session_id") == sid:
            st.session_state["session_id"] = new_session_id()
            st.session_state.messages = []
            st.session_state["conversation_state"] = {}
            st.query_params.pop("session", None)
        # 删除后会话数量变化会让 Expander 以新组件重建；下一次渲染强制保持展开。
        st.session_state["keep_history_expanded_once"] = True
        st.session_state.pop("delete_session_request", None)
        st.rerun()


@st.dialog(t("auth.logout.confirm.title"), width="small")
def confirm_logout() -> None:
    st.write(t("auth.logout.confirm.msg"))
    _u = st.session_state.get("auth_user") or {}
    c1, c2 = st.columns(2)
    if c1.button(t("auth.logout.confirm.yes"), type="primary", width="stretch"):
        auth_client.logout(str(_u.get("username") or ""))  # 登出写入登录日志
        # V3.7.2 同步清除浏览器里的 front_auth Cookie（与登录写入同一机制：
        # 本域 parent.document.cookie，清除后重载回登录门）。st.stop 让清 Cookie
        # 脚本先挂载执行，避免 rerun 竞态把组件顶掉。
        from streamlit.components.v1 import html as components_html
        components_html(
            '<script>try{'
            'parent.document.cookie = "front_auth=; path=/; max-age=0";'
            'parent.location.reload();'
            '}catch(e){parent.location.reload();}</script>', height=0)
        st.session_state["auth_user"] = None
        st.session_state["messages"] = []
        st.session_state["session_id"] = new_session_id()
        st.session_state["conversation_state"] = {}
        st.query_params.pop("session", None)
        st.query_params.pop("load", None)
        st.stop()
    if c2.button(t("auth.logout.confirm.no"), width="stretch"):
        st.session_state.pop("logout_request", None)
        st.rerun()


def _show_management_error(exc: Exception) -> None:
    vector_errors = (VectorStoreError, ApiException)
    if isinstance(exc, vector_errors) or isinstance(exc.__cause__, vector_errors):
        logging.getLogger(__name__).warning("Management page could not access the vector database", exc_info=True)
        st.warning(t("services.vector_unavailable"))
    else:
        st.error(str(exc))


def _guard_management_database(render):
    @wraps(render)
    def guarded(*args, **kwargs):
        try:
            return render(*args, **kwargs)
        except (VectorStoreError, ApiException) as exc:
            _show_management_error(exc)

    return guarded


@st.dialog(t("dialog.remove.title"), width="small")
@_guard_management_database
def confirm_manage_remove() -> None:
    """确认批量移除向量数据，以及可选的磁盘文件删除。"""
    request = st.session_state.get("manage_remove_request")
    if not request:
        return
    if not require_perm("manage"):
        return
    visible = {doc["path"] for doc in store.list_documents(_viewer_filters())}
    if not set(request["paths"]).issubset(visible):
        st.session_state.pop("manage_remove_request", None)
        st.error(t("perm.denied"))
        return

    count = len(request["paths"])
    delete_files = request["delete_files"]
    if delete_files:
        st.error(t("dialog.remove.disk", n=count))
        st.caption(t("dialog.remove.disk.caption"))
        confirm_label = t("dialog.remove.disk.confirm")
    else:
        st.info(t("dialog.remove.index", n=count))
        st.caption(t("dialog.remove.index.caption"))
        confirm_label = t("dialog.remove.index.confirm")

    with st.expander(t("dialog.remove.files", n=count)):
        for name in request["names"]:
            st.write(f"• {name}")

    cancel_col, confirm_col = st.columns(2)
    if cancel_col.button(t("common.cancel"), width="stretch", key="manage_remove_cancel"):
        st.session_state.pop("manage_remove_request", None)
        st.rerun()
    if confirm_col.button(
        confirm_label,
        type="primary",
        icon=":material/delete_forever:" if delete_files else ":material/delete_sweep:",
        width="stretch",
        key="manage_remove_confirm",
    ):
        try:
            info = remove_documents(request["paths"], delete_files=delete_files)
        except ManageError as exc:
            _show_management_error(exc)
            return
        # 移除了知识必须清问答缓存，否则同问题命中缓存返回已删知识的旧答案
        from src.pipeline import clear_qa_cache as _cqc
        from src.keyword_search import invalidate_cache as _ici
        _cqc()
        _ici()
        st.session_state["manage_action_notice"] = info["message"]
        st.session_state["manage_selected_documents"] = []
        st.session_state["manage_table_revision"] = st.session_state.get("manage_table_revision", 0) + 1
        st.session_state.pop("manage_remove_request", None)
        st.rerun()

# 载入历史会话（侧边栏列表点击后在此处理；支持 URL 参数 ?load=会话ID）
load_target = st.session_state.pop("__load_session", None)
try:
    if not load_target and "load" in st.query_params:
        load_target = st.query_params["load"]
        del st.query_params["load"]
    if not load_target and st.query_params.get("session") != st.session_state["session_id"]:
        load_target = st.query_params.get("session")
except Exception:
    pass
if load_target:
    st.session_state["pending_session_load"] = load_target


def _load_requested_session():
    # 在登录门之后处理，不能通过 ?load= 读取别人的会话。
    load_target = st.session_state.pop("pending_session_load", None)
    if not load_target:
        return
    data = load_session(load_target, username=str((st.session_state.get("auth_user") or {}).get("username") or ""))
    if data:
        loaded_messages = []
        for m in data.get("messages", []):
            # 旧版本/损坏的会话文件：缺 role/content 的消息跳过；QAResult 快照
            # 缺必填字段时只恢复文本不恢复来源面板，不让一条坏消息毁掉整个加载
            if not isinstance(m, dict) or not (m.get("role") and m.get("content") is not None):
                continue
            entry = {"role": m["role"], "content": m["content"]}
            if isinstance(m.get("created_at"), str):
                entry["created_at"] = m["created_at"]
            res = m.get("result")
            if res and isinstance(res, dict):
                fields = dataclasses.fields(QAResult)
                required = {f.name for f in fields
                            if f.default is dataclasses.MISSING
                            and f.default_factory is dataclasses.MISSING}
                if required <= set(res.keys()):
                    res["retrieved"] = []  # 历史消息不再需要原始召回对象列表
                    entry["result"] = QAResult(**{
                        k: v for k, v in res.items() if k in {f.name for f in fields}
                    })
            loaded_messages.append(entry)
        st.session_state["conversation_state"] = data.get("context") if isinstance(data.get("context"), dict) else {}
        st.session_state.messages = loaded_messages
        st.session_state.session_id = load_target
        st.session_state.pop("chat_save_conflict", None)
        st.query_params["session"] = load_target
        st.session_state["scroll_loaded_session_once"] = True
    else:
        st.warning(t("chat.session_unavailable"))
st.session_state.setdefault("hybrid_search", cfg.hybrid_search)
st.session_state.setdefault("rerank", cfg.rerank_enabled)
st.session_state.setdefault("answer_eval", True)  # V3.6 回答质量评估卡片

# 用户保存过的检索设置优先于 .env 默认值（每个浏览器会话只加载一次）
if "app_settings_loaded" not in st.session_state:
    for key, value in load_app_settings().items():
        if key in st.session_state:
            st.session_state[key] = value
    st.session_state["app_settings_loaded"] = True

# 暗色模式组件级覆盖：Streamlit 编译主题（config.toml base=light，textColor=#1E293B）
# 会把正文/展开器/说明文字钉成深色，仅覆盖应用 CSS 变量不够——需要直接规则压回浅色。
# 跟随系统（@media）与强制深色（运行时注入）两处共用本规则集。
DARK_COMPONENT_RULES = (
    "[data-testid='stMarkdownContainer'] :is(p,li,td,th,strong,em,h1,h2,h3,h4,h5,h6,blockquote),"
    "[data-testid='stExpander'] summary,[data-testid='stExpander'] summary p,"
    "[data-testid='stCaptionContainer'],"
    ".user-card .u-name,.user-card .u-sub,.user-card .u-note,"
    ".st-key-sidebar_footer strong,.st-key-sidebar_footer .pill{color:#E2E8F0!important;}"
    "[data-testid='stMarkdownContainer'] :is(a){color:#A5B4FC!important;}"
    "[data-testid='stMarkdownContainer'] .user-bubble,"
    "[data-testid='stMarkdownContainer'] .user-bubble :is(p,li,span,strong,em,b,i){color:#fff!important;}"
    "[data-testid='stExpander'],[data-testid='stExpander'] details{background-color:rgba(17,24,39,.65)!important;}"
    "[data-testid='stAlert'],[data-testid='stAlert'] *{background-color:rgba(17,24,39,.85)!important;}"
    "[data-testid='stAlert'],[data-testid='stAlert'] p,[data-testid='stAlert'] div{color:#FDE68A!important;}"
    "[data-testid='stCaptionContainer']{color:#94A3B8!important;}"
    ".st-key-sidebar_footer{color:#CBD5E1!important;}"
    ".stApp [data-baseweb='text-input'] > div,.stApp [data-baseweb='text-area'] > div,"
    ".stApp [data-baseweb='select'] > div{background:#111827!important;}"
    ".stApp [data-baseweb='select'] svg{fill:#E2E8F0!important;}"
    "[data-testid='stSidebarContent']{overflow-y:auto!important;overflow-x:hidden!important;}"
    "[data-testid='stSidebarContent'] > *{flex-shrink:0!important;}"
)
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
/* ==================== 主题变量系统 ==================== */
:root {
  --bg-primary: #F7F8FC;
  --bg-secondary: #FFFFFF;
  --bg-card: rgba(255,255,255,.82);
  --bg-sidebar: linear-gradient(180deg, #F9FAFF 0%, #F2F4FA 100%);
  --text-primary: #182033;
  --text-secondary: #606B85;
  --text-muted: #94A3B8;
  --border-subtle: rgba(93,105,135,.13);
  --border-hover: rgba(79,70,229,.28);
  --accent: #5145E5;
  --accent-light: rgba(81,69,229,.08);
  --accent-text: #5145E5;
  --shadow-card: 0 12px 36px rgba(30,41,75,.07);
}
@media (prefers-color-scheme: dark) {
  :root:not(.theme-light) {
    --bg-primary: #0B0F1A;
    --bg-secondary: #111827;
    --bg-card: rgba(148,163,184,.03);
    --bg-sidebar: linear-gradient(180deg, #0F1523, #0B1018);
    --text-primary: #E2E8F0;
    --text-secondary: #94A3B8;
    --text-muted: #475569;
    --border-subtle: rgba(148,163,184,.08);
    --border-hover: rgba(129,140,248,.15);
    --accent: #818CF8;
    --accent-light: rgba(99,102,241,.08);
    --accent-text: #A5B4FC;
    --shadow-card: 0 16px 44px rgba(0,0,0,.24);
  [data-testid='stMarkdownContainer'] :is(p,li,td,th,strong,em,h1,h2,h3,h4,h5,h6,blockquote),[data-testid='stExpander'] summary,[data-testid='stExpander'] summary p,[data-testid='stCaptionContainer'],.user-card .u-name,.user-card .u-sub,.user-card .u-note,.st-key-sidebar_footer strong,.st-key-sidebar_footer .pill{color:#E2E8F0!important;}
  [data-testid='stMarkdownContainer'] :is(a){color:#A5B4FC!important;}
  [data-testid='stMarkdownContainer'] .user-bubble,[data-testid='stMarkdownContainer'] .user-bubble :is(p,li,span,strong,em,b,i){color:#fff!important;}
  [data-testid='stExpander'],[data-testid='stExpander'] details{background-color:rgba(17,24,39,.65)!important;}
  [data-testid='stAlert'],[data-testid='stAlert'] *{background-color:rgba(17,24,39,.85)!important;}
  [data-testid='stAlert'],[data-testid='stAlert'] p,[data-testid='stAlert'] div{color:#FDE68A!important;}
  [data-testid='stCaptionContainer']{color:#94A3B8!important;}
  .st-key-sidebar_footer{color:#CBD5E1!important;}
  .stApp [data-baseweb='text-input'] > div,.stApp [data-baseweb='text-area'] > div,.stApp [data-baseweb='select'] > div{background:#111827!important;}
  .stApp [data-baseweb='select'] svg{fill:#E2E8F0!important;}
  [data-testid='stSidebarContent']{overflow-y:auto!important;overflow-x:hidden!important;}
  [data-testid='stSidebarContent'] > *{flex-shrink:0!important;}

  }
}
:root.theme-dark {
  --bg-primary: #0B0F1A;
  --bg-secondary: #111827;
  --bg-card: rgba(148,163,184,.03);
  --bg-sidebar: linear-gradient(180deg, #0F1523, #0B1018);
  --text-primary: #E2E8F0;
  --text-secondary: #94A3B8;
  --text-muted: #475569;
  --border-subtle: rgba(148,163,184,.08);
  --border-hover: rgba(129,140,248,.15);
  --accent: #818CF8;
  --accent-light: rgba(99,102,241,.08);
  --accent-text: #A5B4FC;
  --shadow-card: 0 16px 44px rgba(0,0,0,.24);
}
#MainMenu, footer, [data-testid="stStatusWidget"] {display: none !important;}
[data-testid="stAppDeployButton"] {display: none !important;}
header[data-testid="stHeader"] {background: transparent !important;}
html, body, .stApp, .stMarkdown, input, textarea {
  font-family: 'Inter', -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;}
/* Streamlit 用图标字体的英文连字生成图标。不可对所有 span/div 强制正文字体。 */
[data-testid="stIconMaterial"] {
  font-family: "Material Symbols Rounded" !important;
  font-weight: normal !important;
  font-style: normal !important;
  line-height: 1 !important;
  letter-spacing: normal !important;
  text-transform: none !important;
  white-space: nowrap !important;
  word-wrap: normal !important;
  direction: ltr !important;
  -webkit-font-feature-settings: "liga" !important;
  -webkit-font-smoothing: antialiased !important;
  font-feature-settings: "liga" !important;
}
.stApp {background: var(--bg-primary) !important; color: var(--text-primary) !important;}
[data-testid="stMain"], [data-testid="stMain"] .block-container,
[data-testid="stChatMessage"], [data-testid="stChatMessage"] [data-testid="stExpander"] {
  overflow-anchor:none !important;}
.block-container {padding-top: 1.6rem !important; padding-bottom: 9rem !important; max-width: 1240px !important;}
[data-testid="stSidebar"] {background: var(--bg-sidebar) !important;
  border-right: 1px solid var(--border-subtle) !important; min-width: 272px !important;}
[data-testid="stSidebar"] .block-container {padding: 1.25rem 1rem 1rem !important;}
[data-testid="stSidebarContent"] {display:flex !important;flex-direction:column !important;position:relative !important;
  overflow-y:auto !important;overflow-x:hidden !important;}
[data-testid="stSidebarContent"] > * {flex-shrink:0 !important;}  /* 防止内容超高时 flex 子项被压扁互相重叠 */
[data-testid="stSidebarHeader"] {position:absolute !important;top:18px !important;right:9px !important;
  z-index:20 !important;width:auto !important;height:auto !important;padding:0 !important;}
[data-testid="stSidebarUserContent"] {display:contents !important;}
[data-testid="stSidebarUserContent"] > *,
[data-testid="stSidebarUserContent"] > * > [data-testid="stVerticalBlock"] {display:contents !important;}
[data-testid="stSidebarUserContent"] [data-testid="stLayoutWrapper"]:has(.st-key-sidebar_brand) {order:1 !important;}
/* V3.7.2 用户信息卡：Logo 下方留出呼吸间距，卡片分层样式 */
[data-testid="stSidebarUserContent"] [data-testid="stLayoutWrapper"]:has(.st-key-sidebar_userinfo) {
  order:2 !important;
  margin: 12px 10px 10px 0 !important;
}
[data-testid="stSidebarUserContent"] [data-testid="stLayoutWrapper"]:has(.st-key-sidebar_userinfo)
  [data-testid="stVerticalBlock"] {gap: 0 !important; padding: 0 !important;}
.user-card { display:flex; align-items:center; gap:10px; padding:9px 12px;
  background: var(--bg-secondary, rgba(148,163,184,.10));
  border: 1px solid var(--border-subtle, rgba(148,163,184,.25));
  border-radius: 12px; }
.user-card .avatar { width:38px; height:38px; border-radius:50%; flex:none;
  background:linear-gradient(135deg,#2563EB,#4F46E5); color:#fff;
  font-weight:700; font-size:.95rem; display:flex; align-items:center;
  justify-content:center; box-shadow:0 2px 8px rgba(37,99,235,.30); }
.user-card .u-name { font-weight:700; font-size:.86rem; }
.user-card .u-sub { color:var(--text-muted, #8a93a6); font-size:.72rem; margin-top:1px; }
.user-card .u-note { color:var(--text-muted, #8a93a6); font-size:.72rem; margin-top:3px;
  white-space:normal; word-break:break-all; line-height:1.5; }
/* V3.7.2 表单控件全局加清晰边框：下拉/输入/多选未聚焦时也有明确可点击边界 */
.stApp [data-baseweb="select"],
.stApp [data-baseweb="tag-list"],
.stApp [data-baseweb="text-input"] > div,
.stApp [data-baseweb="text-area"] > div {
  border: 1px solid rgba(100,116,139,.45) !important;
  border-radius: 8px !important;
  background: #fff !important;
}
.stApp [data-baseweb="select"] > div:hover,
.stApp [data-baseweb="select"]:hover,
.stApp [data-baseweb="tag-list"]:hover,
.stApp [data-baseweb="text-input"] > div:hover {
  border-color: #2563EB !important;
}
.user-card .u-dept { display:inline-block; padding:1px 8px; margin-left:6px;
  border-radius:9px; font-size:.68rem; background:rgba(99,102,241,.14);
  color:var(--accent-text, #4F46E5); font-weight:600; vertical-align:2px; }
[data-testid="stSidebarNav"] {order:3 !important; margin-top: 6px !important;}
/* 导航文字颜色跟随主题变量（Streamlit 内置导航用固定主题色，深色下看不清） */
[data-testid="stSidebarNav"] a,
[data-testid="stSidebarNav"] a span,
[data-testid="stSidebarNav"] a p,
[data-testid="stSidebarNav"] p {color: var(--text-secondary) !important;}
[data-testid="stSidebarNav"] a:hover,
[data-testid="stSidebarNav"] a:hover span,
[data-testid="stSidebarNav"] a:hover p {color: var(--text-primary) !important;}
[data-testid="stSidebarNav"] a[aria-current="page"],
[data-testid="stSidebarNav"] a[aria-current="page"] span,
[data-testid="stSidebarNav"] a[aria-current="page"] p {
  color: var(--accent-text) !important; font-weight: 600;}
[data-testid="stSidebarUserContent"] [data-testid="stLayoutWrapper"]:has(.st-key-sidebar_footer) {order:4 !important;}
[data-testid="stSidebarUserContent"] [data-testid="stLayoutWrapper"]:has(.st-key-sidebar_logout) {
  order:5 !important; margin-top:auto !important; padding: 4px 6px 2px !important;
  border-top: 1px solid var(--border-subtle) !important;}
[data-testid="stSidebarUserContent"] [data-testid="stLayoutWrapper"]:has(.st-key-sidebar_logout) button {
  width: 100% !important;}
[data-testid="stSidebar"] [role="radiogroup"] label {border-radius: 10px; padding: 4px 12px;
  margin: 1px 0; transition: all .2s; font-weight: 500; font-size: .82rem; color: var(--text-secondary) !important;}
[data-testid="stSidebar"] [role="radiogroup"] label:hover {background: rgba(99,102,241,.08); color: #C7D2FE !important;}
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) {
  background: linear-gradient(135deg, rgba(99,102,241,.15), rgba(139,92,246,.1));
  color: #818CF8 !important; border-left: 2px solid #818CF8;}
.brand-block {padding: 4px 42px 17px 3px; margin-bottom: 8px; border-bottom: 1px solid var(--border-subtle);}
.brand-name {font-size:1.05rem;font-weight:800;color:var(--text-primary);line-height:1.2;letter-spacing:-.02em;}
.brand-slogan {font-size:.72rem;color:var(--text-secondary);margin-top:4px;}
[data-testid="stExpander"] {border-radius: 12px !important;
  border: 1px solid var(--border-subtle) !important; background: var(--bg-card) !important;}
.stButton > button {border-radius: 10px; border: 1px solid rgba(148,163,184,.12);
  font-weight: 500; font-size: .82rem; transition: all .2s;
  background: var(--bg-card); color: var(--text-primary);}
.stButton > button:hover {border-color: rgba(129,140,248,.3); color: #A5B4FC;
  background: var(--accent-light); transform: translateY(-1px);}
.stButton > button[kind="primary"] {background: linear-gradient(135deg, #6366F1, #8B5CF6) !important;
  color: #fff !important; border: none; font-weight: 600; box-shadow: 0 4px 16px rgba(99,102,241,.25);}
.stButton > button[kind="primary"]:hover {filter: brightness(1.1); transform: translateY(-1px);}
.st-key-manage_remove_disk .stButton button {
  color:#DC2626 !important;border-color:rgba(220,38,38,.25) !important;background:rgba(220,38,38,.055) !important;}
.st-key-manage_remove_disk .stButton button:hover {
  color:#B91C1C !important;border-color:rgba(220,38,38,.42) !important;background:rgba(220,38,38,.1) !important;}
.stTextInput input, .stSelectbox div[data-baseweb="select"] > div, .stTextArea textarea {
  border-radius: 10px !important; border-color: rgba(148,163,184,.1) !important;
  background: rgba(148,163,184,.03) !important; color: var(--text-primary) !important; font-size: .85rem !important;}
.stTextInput input:focus, .stTextArea textarea:focus {
  border-color: #818CF8 !important; box-shadow: 0 0 0 3px rgba(99,102,241,.1) !important;}
div[data-baseweb="radio"] label {color: var(--text-secondary) !important;}
[data-testid="stChatMessage"] {border-radius: 16px; border: 1px solid rgba(148,163,184,.06);
  background: rgba(148,163,184,.02); padding: 8px 14px; margin-bottom: 6px;}
[data-testid="stChatMessage"]:has(.user-bubble) {flex-direction: row-reverse;
  background: transparent; border: none; margin-left: 8%; padding: 2px;}
.user-bubble {background: linear-gradient(135deg, #6366F1, #8B5CF6); color: #fff;
  padding: 10px 18px; border-radius: 20px 20px 6px 20px; font-size: .9rem; line-height: 1.6;
  box-shadow: 0 4px 16px rgba(99,102,241,.2);display:inline-block;width:fit-content;
  max-width:min(78%,680px);white-space:pre-wrap;overflow-wrap:anywhere;text-align:left;}
[data-testid="stChatMessage"]:has(.user-bubble) [data-testid="stMarkdownContainer"] {
  display:flex !important;justify-content:flex-end !important;align-items:flex-start !important;width:100% !important;}
[data-testid="stChatMessage"]:has(.user-bubble) [data-testid="stMarkdown"] {width:100% !important;}
[data-testid="stBottom"], [data-testid="stBottom"] > div {background: transparent !important;
  border: none !important; box-shadow: none !important;}
[data-testid="stBottom"] {position: fixed !important; bottom: 10px !important;
  left: 50% !important; transform: translateX(-50%); width: min(920px, 94vw) !important; z-index: 200;}
.st-key-chat_composer {position:fixed !important;bottom:10px !important;left:50% !important;
  transform:translateX(-50%) !important;width:min(920px,94vw) !important;z-index:200 !important;}
/* 侧边栏展开时（宽 300px），输入框应在内容区内居中：中心 = 50vw + 150px。
   展开判定用「收起按钮存在性」——本版 Streamlit 的侧边栏没有 aria-expanded
   属性，旧选择器 body:has(...[aria-expanded="true"]) 永不命中，导致输入框
   恒按视口中心定位、在内容区内整体偏左。收起时按钮消失、出现
   stExpandSidebarButton，回落到基础 50% 规则（此时内容区即全屏）。 */
body:has([data-testid="stSidebarCollapseButton"]) .st-key-chat_composer {
  left:calc(50% + 150px) !important;width:min(920px,calc(100vw - 340px)) !important;}
.st-key-chat_composer,
.st-key-chat_composer > div,
.st-key-chat_composer [data-testid="stVerticalBlock"],
.st-key-chat_composer [data-testid="stElementContainer"] {
  background:transparent !important;box-shadow:none !important;}
.st-key-chat_composer [data-testid="stChatInput"] {width:100% !important;}
[data-testid="stChatInput"] {border-radius:16px !important;overflow:hidden !important;
  min-height:48px !important;border:1px solid rgba(99,102,241,.48) !important;
  background: var(--bg-secondary) !important;
  box-shadow:0 5px 18px rgba(30,41,75,.11) !important;
  transition:border-color .18s ease,box-shadow .18s ease !important;}
[data-testid="stChatInput"] > div {
  min-height:46px !important;padding:5px 10px !important;}
[data-testid="stChatInput"]:hover {
  border-color:rgba(99,102,241,.7) !important;
  box-shadow:0 6px 20px rgba(30,41,75,.13) !important;}
[data-testid="stChatInput"]:focus-within {
  border-color:var(--accent) !important;
  box-shadow:0 6px 22px rgba(49,46,129,.15) !important;}
[data-testid="stChatInput"] textarea {border-radius:16px !important;
  min-height:36px !important;padding:7px 6px !important;background:transparent !important;
  color:var(--text-primary) !important;font-size:.9rem !important;line-height:1.35 !important;}
[data-testid="stChatInput"] textarea::placeholder {
  color:var(--text-secondary) !important;opacity:.82 !important;}
[data-testid="stChatInputSubmitButton"] {
  width:34px !important;height:34px !important;margin-right:6px !important;border-radius:50% !important;
  color:#FFFFFF !important;background:linear-gradient(135deg,#6366F1,#7C3AED) !important;
  box-shadow:0 3px 10px rgba(99,102,241,.24) !important;}
[data-testid="stChatInputSubmitButton"]:hover {
  filter:brightness(1.08);transform:translateY(-1px);}
.hero {position:relative;overflow:hidden;background:linear-gradient(125deg,#17163A 0%,#302B78 48%,#5046E5 100%);
  border-radius:18px;padding:18px 24px;color:#EEF2FF;margin-bottom:12px;
  border:1px solid rgba(129,140,248,.2);box-shadow:0 12px 32px rgba(49,46,129,.16);}
.hero:after {content:"";position:absolute;width:190px;height:190px;border-radius:50%;right:-55px;top:-120px;
  background:rgba(255,255,255,.1);filter:blur(2px);}
.hero h1 {margin:0 0 4px;padding:0 !important;font-size:1.25rem;font-weight:800;letter-spacing:-.02em;}
.hero p {margin:0 0 9px;opacity:.72;font-size:.78rem;}
.hero .pill {display: inline-block; background: rgba(99,102,241,.15);
  border: 1px solid rgba(129,140,248,.2); border-radius: 999px;
  padding:2px 9px;font-size:.66rem;margin-right:5px;color:#A5B4FC;}
/* 开启新会话按钮并入 hero 蓝卡：有消息时 hero 与按钮同行，整行承担卡片底色，
   .hero 自身退化为透明内容区（空会话时 .hero 仍是独立卡片，样式不变） */
div[data-testid="stHorizontalBlock"]:has(.hero) {
  position:relative;overflow:hidden;margin-bottom:12px;padding:14px 20px 18px 24px;gap:12px;
  background:linear-gradient(125deg,#17163A 0%,#302B78 48%,#5046E5 100%);
  border-radius:18px;border:1px solid rgba(129,140,248,.2);box-shadow:0 12px 32px rgba(49,46,129,.16);}
div[data-testid="stHorizontalBlock"]:has(.hero) .hero {
  background:none;border:none;box-shadow:none;border-radius:0;padding:0;margin-bottom:0;}
/* 卡片内白底按钮：深色渐变上高对比、一眼可见 */
div[data-testid="stHorizontalBlock"]:has(.hero) button[kind="primary"] {
  white-space:nowrap;background:#FFFFFF !important;color:#4338CA !important;
  border:1px solid rgba(255,255,255,.7) !important;
  box-shadow:0 2px 10px rgba(15,23,42,.22) !important;font-weight:600;}
div[data-testid="stHorizontalBlock"]:has(.hero) button[kind="primary"]:hover {
  background:#EEF2FF !important;border-color:#FFFFFF !important;transform:translateY(-1px);}
/* 状态胶囊与卡片底部留出间距（换行成多行时行距同样生效） */
div[data-testid="stHorizontalBlock"]:has(.hero) .hero .pill,
.hero .pill {margin:3px 5px 5px 0;}
[data-testid="stMetric"] {background: var(--bg-card);
  border: 1px solid var(--border-subtle); border-radius: 16px; padding: 16px 18px;box-shadow:var(--shadow-card);}
[data-testid="stMetric"]:hover {border-color: rgba(129,140,248,.15);}
[data-testid="stMetric"] label {color: #94A3B8 !important; font-size: .75rem !important;}
[data-testid="stMetric"] [data-testid="stMetricValue"] {color: var(--text-primary) !important;}
[data-testid="stDataFrame"] {border-radius: 14px !important; overflow: hidden;
  border: 1px solid rgba(148,163,184,.08) !important;}
.tag {display: inline-block; border-radius: 999px; padding: 2px 10px;
  font-size: .72rem; font-weight: 500; margin-right: 5px;}
.tag.blue {background: rgba(99,102,241,.12); color: #A5B4FC; border: 1px solid rgba(99,102,241,.15);}
.tag.green {background: rgba(16,185,129,.1); color: #6EE7B7; border: 1px solid rgba(16,185,129,.12);}
.tag.amber {background: rgba(245,158,11,.1); color: #FCD34D; border: 1px solid rgba(245,158,11,.12);}
.chunk-quote {background: rgba(148,163,184,.03); border-left: 2px solid #6366F1;
  border-radius: 0 12px 12px 0; padding: 10px 14px; color: #94A3B8;
  font-size: .85rem; line-height: 1.7; margin: 4px 0 2px;}
[data-testid="stExpander"]:has(.debug-marker) p,
[data-testid="stExpander"]:has(.debug-marker) summary div div {font-size: .78rem !important; color: #64748B !important;}
.dbg-label {font-size: .72rem; font-weight: 600; color: #475569; margin: 10px 0 2px;
  text-transform: uppercase; letter-spacing: .03em;}
pre.dbg, .dbg-content {font-size: .72rem !important; line-height: 1.6;
  word-break: break-word; background: rgba(11,15,26,.8);
  border: 1px solid rgba(148,163,184,.06); border-radius: 10px;
  padding: 8px 12px; margin: 2px 0 6px; color: #94A3B8;
  font-family: 'SF Mono', ui-monospace, monospace;}
.section-title {font-size: 1.1rem; font-weight: 700; color: #F1F5F9; margin: 4px 0 12px;}
hr {border: none; border-top: 1px solid rgba(148,163,184,.06);}
::-webkit-scrollbar {width: 6px; height: 6px;}
::-webkit-scrollbar-track {background: transparent;}
::-webkit-scrollbar-thumb {background: rgba(148,163,184,.15); border-radius: 3px;}
@keyframes fadeInUp {from {opacity: 0; transform: translateY(8px);} to {opacity: 1; transform: translateY(0);}}
[data-testid="stChatMessage"] {animation: fadeInUp .3s ease-out;}
[data-testid="stMetric"] {animation: fadeInUp .4s ease-out;}
.sidebar-status {margin-top:12px;padding:11px 12px;border:1px solid var(--border-subtle);
  border-radius:12px;background:var(--bg-card);font-size:.75rem;color:var(--text-secondary);}
.sidebar-status-row {display:flex;align-items:center;justify-content:space-between;gap:8px;}
.sidebar-status strong {color:var(--text-primary);font-size:.76rem;}
.sidebar-status-note {margin-top:5px;color:var(--text-muted);font-size:.67rem;line-height:1.4;}

/* ==================== 精致化交互 ==================== */
/* 按钮过渡 */
.stButton > button {transition: all .15s cubic-bezier(.4,0,.2,1) !important;}
.stButton > button:active {transform: scale(.97) !important;}

/* 会话列表按钮 */
[data-testid="stSidebar"] .stButton > button {
  font-size: .76rem !important; font-weight: 500 !important;
  text-align: left !important; padding: 4px 10px !important;
  border: none !important; background: transparent !important;
  color: var(--text-secondary) !important;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}
[data-testid="stSidebar"] .stButton > button:hover {
  background: var(--accent-light) !important; color: var(--accent-text) !important;}
[data-testid="stSidebar"] [class*="st-key-hs_"] {position:static !important;}
[data-testid="stSidebar"] [class*="st-key-hs_"] > [data-testid="stElementContainer"] {
  position:static !important;}
[data-testid="stSidebar"] [class*="st-key-hs_"] [data-testid="stPageLink"] {position:static !important;}
[data-testid="stSidebar"] [class*="st-key-hs_"] .stButton > button,
[data-testid="stSidebar"] [class*="st-key-hs_"] [data-testid="stPageLink"] a {
  position:absolute !important;inset:0 !important;width:100% !important;height:100% !important;
  min-height:100% !important;border-radius:9px !important;padding:7px 40px 22px 10px !important;
  display:flex !important;gap:0 !important;font-size:calc(.64rem + 1px) !important;font-weight:500 !important;line-height:1.35 !important;
  justify-content:flex-start !important;align-items:flex-start !important;text-align:left !important;z-index:1 !important;
  color:var(--text-secondary) !important;background:transparent !important;text-decoration:none !important;}
[data-testid="stSidebar"] [class*="st-key-hs_"] .stButton > button:hover,
[data-testid="stSidebar"] [class*="st-key-hs_"] [data-testid="stPageLink"] a:hover {
  background:transparent !important;transform:none !important;}
[data-testid="stSidebar"] [class*="st-key-hs_"] [data-testid="stPageLink"] [data-testid="stIconEmoji"] {
  display:none !important;}
[data-testid="stSidebar"] [class*="st-key-hs_"] [data-testid="stPageLink"] a > span:first-child:not(:last-child) {
  display:none !important;width:0 !important;min-width:0 !important;margin:0 !important;padding:0 !important;}
[data-testid="stSidebar"] [class*="st-key-hs_"] [data-testid="stPageLink"] a > span:last-child {
  display:block !important;flex:1 1 auto !important;width:100% !important;min-width:0 !important;
  margin:0 !important;padding:0 !important;text-align:left !important;}
[data-testid="stSidebar"] [class*="st-key-hs_"] .stButton > button > div,
[data-testid="stSidebar"] [class*="st-key-hs_"] .stButton > button > div > span,
[data-testid="stSidebar"] [class*="st-key-hs_"] [data-testid="stPageLink"] a > div,
[data-testid="stSidebar"] [class*="st-key-hs_"] [data-testid="stPageLink"] a > div > span,
[data-testid="stSidebar"] [class*="st-key-hs_"] [data-testid="stMarkdownContainer"] {
  width:100% !important;text-align:left !important;justify-content:flex-start !important;}
[data-testid="stSidebar"] [class*="st-key-hs_"] [data-testid="stMarkdownContainer"] p {
  display:block !important;width:100% !important;margin:0 !important;padding:0 !important;
  color:var(--text-secondary) !important;font-size:calc(.64rem + 1px) !important;font-weight:500 !important;
  line-height:1.35 !important;text-align:left !important;white-space:nowrap !important;
  overflow:hidden !important;text-overflow:ellipsis !important;}
[data-testid="stSidebar"] [class*="st-key-del_"] .stButton > button {
  width:28px !important;height:28px !important;min-height:28px !important;text-align:center !important;
  padding:0 !important;border-radius:8px !important;color:var(--text-muted) !important;
  position:relative !important;z-index:3 !important;}
[data-testid="stSidebar"] [class*="st-key-del_"] {
  position:absolute !important;top:6px !important;right:6px !important;width:28px !important;
  z-index:4 !important;}
[data-testid="stSidebar"] [class*="st-key-del_"] [data-testid="stMarkdownContainer"] {display:none !important;}
[data-testid="stSidebar"] [class*="st-key-del_"] .stButton > button > div,
[data-testid="stSidebar"] [class*="st-key-del_"] .stButton > button > div > span {
  width:100% !important;display:flex !important;align-items:center !important;justify-content:center !important;}
[data-testid="stSidebar"] [class*="st-key-del_"] .stButton > button [data-testid="stIconMaterial"] {
  font-size:17px !important;}
[data-testid="stSidebar"] [class*="st-key-del_"] .stButton > button:hover {
  color:#EF4444 !important;background:rgba(239,68,68,.08) !important;}
[data-testid="stSidebar"] [class*="st-key-session_item_"] {
  background:rgba(148,163,184,.055);border-radius:9px;padding:0 !important;margin:0 !important;
  min-height:50px !important;gap:0 !important;position:relative !important;cursor:pointer;
  transition:background .16s ease,transform .16s ease;}
[data-testid="stSidebar"] [class*="st-key-session_item_"] > div,
[data-testid="stSidebar"] [class*="st-key-session_item_"] [data-testid="stHorizontalBlock"] {
  gap:0 !important;min-height:0 !important;}
[data-testid="stSidebar"] [class*="st-key-session_item_"] > [data-testid="stElementContainer"]:has([data-testid="stCaptionContainer"]) {
  position:absolute !important;left:10px !important;bottom:6px !important;width:auto !important;
  z-index:2 !important;pointer-events:none !important;}
[data-testid="stSidebar"] [class*="st-key-session_item_"] [data-testid="stCaptionContainer"] {
  color:var(--text-muted) !important;font-size:calc(.62rem + 1px) !important;line-height:1.2 !important;
  position:static !important;padding:0 !important;margin:0 !important;letter-spacing:.01em;}
[data-testid="stSidebar"] [class*="st-key-session_item_"] [data-testid="stCaptionContainer"] p {
  margin:0 !important;}
[data-testid="stSidebar"] [class*="st-key-session_item_"]:hover {
  background:var(--accent-light);transform:translateX(1px);}

/* 历史会话：无外框，标题在前、折叠箭头在后 */
.st-key-sidebar_footer [data-testid="stExpander"] {
  border:none !important;background:transparent !important;border-radius:0 !important;box-shadow:none !important;}
.st-key-sidebar_footer [data-testid="stExpander"] > details {
  border:none !important;background:transparent !important;border-radius:0 !important;box-shadow:none !important;}
.st-key-sidebar_footer [data-testid="stExpander"] > details > summary {
  padding:8px 2px !important;border:none !important;border-radius:0 !important;box-shadow:none !important;}
.st-key-sidebar_footer [data-testid="stExpander"] > details > summary:focus,
.st-key-sidebar_footer [data-testid="stExpander"] > details > summary:focus-visible {
  outline:none !important;box-shadow:none !important;}
.st-key-sidebar_footer [data-testid="stExpanderDetails"] > [data-testid="stVerticalBlock"] {
  gap:4px !important;}
.st-key-sidebar_footer [data-testid="stExpander"] > details > summary > span {
  display:flex !important;align-items:center !important;width:100% !important;}
.st-key-sidebar_footer [data-testid="stExpander"] > details > summary > span > div {
  order:1 !important;}
.st-key-sidebar_footer [data-testid="stExpander"] > details > summary > span > span:has([data-testid="stIconMaterial"]) {
  order:2 !important;margin-left:auto !important;}
.st-key-sidebar_footer [data-testid="stExpanderDetails"] {padding:2px 0 6px !important;}

/* 文件上传区域 */
[data-testid="stFileUploaderDropzone"] {
  border: 2px dashed var(--border-subtle) !important;
  border-radius: 18px !important; padding: 2.5rem 1rem !important;
  background: var(--bg-card) !important; transition: all .2s !important;box-shadow:var(--shadow-card);}
[data-testid="stFileUploaderDropzone"]:hover {
  border-color: var(--accent) !important; background: var(--accent-light) !important;}
[data-testid="stFileUploaderDropzone"] button {
  background: var(--accent) !important; color: white !important;
  border-radius: 8px !important; border: none !important; font-weight: 600 !important;}

/* Expander 精致化 */
[data-testid="stExpander"] {transition: all .2s !important;}
[data-testid="stExpander"]:hover {border-color: var(--border-hover) !important;}
[data-testid="stExpander"] summary {font-size: .82rem !important; font-weight: 500 !important;}

/* DataFrame 悬停行 */
[data-testid="stDataFrame"] [data-row]:hover {
  background: var(--accent-light) !important;}

/* Toast 通知 */
[data-testid="stToast"] {border-radius: 12px !important;
  border: 1px solid var(--border-hover) !important;}

/* 分隔线 */
hr {margin: 1rem 0 !important;}

/* 空状态提示 */
[data-testid="stAlert"] {border-radius: 12px !important;}

/* Chat 消息入场 */
[data-testid="stChatMessage"] {border: 1px solid var(--border-subtle) !important;
  transition: border-color .2s !important;}
[data-testid="stChatMessage"]:hover {border-color: var(--border-hover) !important;}
[data-testid="stChatMessage"]:has(.user-bubble) {
  background:transparent !important;border:none !important;box-shadow:none !important;}

/* Tab 按钮 */
.stTabs [data-baseweb="tab-list"] {gap: 0 !important;}
.stTabs [data-baseweb="tab"] {
  border-radius: 10px 10px 0 0 !important; font-size: .82rem !important;
  padding: 8px 16px !important; color: var(--text-secondary) !important;}
.stTabs [aria-selected="true"] {color: var(--accent-text) !important;
  border-bottom: 2px solid var(--accent) !important;}

/* Metric 数值字体 */
[data-testid="stMetricValue"] {font-weight: 700 !important; letter-spacing: -.02em !important;}

/* Select 下拉框 */
div[data-baseweb="select"] > div {border-radius: 10px !important;
  font-size: .82rem !important;}

/* Success/Error 消息 */
[data-testid="stAlert"] {font-size: .82rem !important;}

/* ==================== 标题与标签颜色修复 ==================== */
.section-title {color: var(--text-primary) !important; opacity: 1 !important;}
.stMarkdown p strong, .stMarkdown p b {color: var(--text-primary) !important;}
.stRadio label p, .stSelectbox label p, .stTextInput label p,
.stToggle label p, .stSlider label p {
  color: var(--text-primary) !important; font-weight: 500 !important;}
.stRadio label p span, .stSelectbox > label > span {color: var(--text-primary) !important;}
[data-testid="stExpander"] summary div div {
  color: var(--text-primary) !important; font-weight: 600 !important;}
[data-testid="stExpander"] summary svg {fill: var(--text-secondary) !important;}
.stTabs [data-baseweb="tab"] {color: var(--text-primary) !important;}
.caption, .stMarkdown p:has(.stCaption) {color: var(--text-secondary) !important;}
[data-testid="stCaptionContainer"] {color: var(--text-secondary) !important;}
.stAlert {color: var(--text-primary) !important;}
[data-testid="stMetric"] label {color: var(--text-secondary) !important;
  font-weight: 500 !important; opacity: 1 !important;}

/* 知识管理台：压缩汇总区；页码保留键入，翻页统一用上一页/下一页。 */
.st-key-manage_summary [data-testid="stMetric"] {
  padding: 8px 14px; border-radius: 12px; min-height: 0;}
.st-key-manage_summary [data-testid="stMetricValue"] {
  font-size: 1.5rem !important; line-height: 1.25 !important;}
.st-key-manage_pagination [data-testid="stNumberInputStepUp"],
.st-key-manage_pagination [data-testid="stNumberInputStepDown"] {display: none !important;}
.st-key-manage_pagination input[type="number"] {appearance: textfield; -moz-appearance: textfield;}
.st-key-manage_pagination input::-webkit-inner-spin-button,
.st-key-manage_pagination input::-webkit-outer-spin-button {appearance: none; margin: 0;}

[data-testid="stFileUploaderDropzone"] button {
  font-family: 'Inter', sans-serif !important;}
@media (max-width: 760px) {
  .hero {padding:15px 17px;border-radius:15px;}
  div[data-testid="stHorizontalBlock"]:has(.hero) {padding:14px 16px 18px;border-radius:15px;}
  .hero h1 {font-size:1.1rem;}
  .block-container {padding-top:1rem !important;}
  .st-key-chat_composer {left:50% !important;width:94vw !important;}
}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

# 主题覆盖：非"跟随系统"时强制对应主题
_theme = st.session_state.get("theme", "system")
if _theme == "dark":
    st.markdown("<style>:root{--bg-primary:#0B0F1A!important;--bg-secondary:#111827!important;"
                "--bg-card:rgba(17,24,39,.6)!important;--bg-sidebar:linear-gradient(180deg,#0F1523,#0B1018)!important;"
                "--text-primary:#E2E8F0!important;--text-secondary:#94A3B8!important;"
                "--text-muted:#475569!important;--border-subtle:rgba(148,163,184,.08)!important;"
                "--border-hover:rgba(129,140,248,.15)!important;"
                "--accent:#818CF8!important;--accent-light:rgba(99,102,241,.08)!important;--accent-text:#A5B4FC!important;"
                "--shadow-card:0 16px 44px rgba(0,0,0,.24)!important;}"
                ".stApp{background:#0B0F1A!important;color:#E2E8F0!important;}"
                "[data-testid='stSidebar']{background:linear-gradient(180deg,#0F1523,#0B1018)!important;}"
                "[data-testid='stChatInput']{background:#111827!important;}"
                "[data-testid='stChatInput'] textarea{color:#E2E8F0!important;}"
                ".stTextInput input,.stTextArea textarea{background:rgba(148,163,184,.03)!important;"
                "color:#E2E8F0!important;border-color:rgba(148,163,184,.1)!important;}"
                + DARK_COMPONENT_RULES + "</style>", unsafe_allow_html=True)
elif _theme == "light":
    st.markdown("<style>:root{--bg-primary:#F7F8FC!important;--bg-secondary:#FFFFFF!important;"
                "--bg-sidebar:linear-gradient(180deg,#F8FAFF,#F2F5FA)!important;"
                "--text-primary:#182033!important;--text-secondary:#606B85!important;"
                "--text-muted:#94A3B8!important;--border-subtle:rgba(148,163,184,.1)!important;"
                "--accent:#5145E5!important;--accent-text:#5145E5!important;}"
                ".stApp{background:#F7F8FC!important;color:#182033!important;}"
                "[data-testid='stSidebar']{background:linear-gradient(180deg,#F8FAFF,#F2F5FA)!important;}"
                "[data-testid='stChatInput']{background:#FFFFFF!important;}"
                "[data-testid='stChatInput'] textarea{color:#1E293B!important;}"
                ".stTextInput input,.stTextArea textarea{background:rgba(148,163,184,.03)!important;"
                "color:#1E293B!important;}"
                "</style>", unsafe_allow_html=True)


# ---------------------------------------------------------------- 服务与工具
@st.cache_resource
def get_services():
    from src.embedding import get_embedding_client
    from src.vector_store import get_vector_store

    return {"embedding": get_embedding_client(), "store": get_vector_store()}


# V3.8：services / store 在登录门之后才初始化（见文件末尾 auth_gate() 之后）——
# 此前在这里初始化，Qdrant 一挂连登录页都打不开，还会把带服务器路径的报错栈
# 直接显示给访客。所有使用方都在页面函数内，登录后才会被调用。
services: dict = {}
store = None


def persist_app_settings() -> None:
    """统一保存入口：session 里的设置 + 插件安装清单合并写入。

    必须整包带上 installed_plugins（它不在 session_state 里，读取走 src/plugin），
    否则任何一次覆盖式保存都会把它（或 language）从 settings.json 里丢掉。
    V3.7：登录用户额外把偏好按人存入 SQLite（不同用户语言/主题互不影响）。
    """
    data = {k: st.session_state[k] for k in SETTINGS_KEYS if k in st.session_state}
    # 仅初始化空默认值不应扩写已有设置（热重载/其他控件保存可能调用此入口）。
    if not data.get("doc_type_choice") and "doc_type_choice" not in load_app_settings():
        data.pop("doc_type_choice", None)
    if not data.get("department_choice") and "department_choice" not in load_app_settings():
        data.pop("department_choice", None)
    if not data.get("classification_choice") and "classification_choice" not in load_app_settings():
        data.pop("classification_choice", None)
    data["installed_plugins"] = get_installed_plugins()
    save_app_settings(data)
    user = st.session_state.get("auth_user")
    if user:
        prefs = {k: v for k, v in data.items() if k != "installed_plugins"}
        auth_client.save_prefs(user["username"], prefs)


def _sync_setting(canonical_key: str, widget_key: str):
    """控件变化时：同步到规范 session 键（供其他页面读取）+ 持久化到配置文件。"""
    def _sync():
        st.session_state[canonical_key] = st.session_state[widget_key]
        persist_app_settings()
    return _sync


def domain_label(domain: str) -> str:
    """「code · 名称」；名称优先用领域管理里设置的，未设置时用内置多语言标签。

    纯显示：编号才是数据身份（卡片 payload / 过滤条件 / 缓存键都只认编号），
    改名称不影响任何检索。
    """
    name = domain_reg.display_name(domain)
    return f"{domain} · {name}" if name else domain_label_i18n(domain)


def _domain_index(codes: list[str], preferred: str) -> int:
    """下拉框默认选中项：首选领域被停用 / 删除时退回第一项。"""
    return codes.index(preferred) if preferred in codes else 0


def _clean_directory_name(name: str) -> str:
    """生成稳定、可读且不会越出目标目录的顶层文件夹名。"""
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", name).strip(" .-")
    if not cleaned:
        raise ValueError(f"目录名称无效：{name}")
    return cleaned


@st.cache_data(ttl=30)
def system_status(lang: str = "zh-CN") -> dict[str, tuple[bool, str]]:
    """三盏灯检查（30 秒缓存）：Ollama/Qdrant 检查有网络开销，
    不能每次页面交互都实时跑，否则按钮点击会被拖慢好几秒。
    语言进缓存键：切语言后状态名称能立即更新。"""
    status: dict[str, tuple[bool, str]] = {}
    try:
        ok = services["embedding"].model_available()
        status[t("status.embedding")] = (ok, cfg.embedding_model if ok
                                         else t("status.missing", model=cfg.embedding_model))
    except Exception as exc:
        status[t("status.embedding")] = (False, str(exc)[:40])
    try:
        count = store.count()
        status[t("status.vectordb")] = (True, t("status.cards", n=count))
    except Exception as exc:
        status[t("status.vectordb")] = (False, str(exc)[:40])
    is_local_llm = "localhost" in cfg.llm_base_url or "127.0.0.1" in cfg.llm_base_url
    llm_ok = bool(cfg.llm_api_key) or is_local_llm
    llm_note = cfg.llm_model if llm_ok else t("status.not_configured")
    if llm_ok and is_local_llm:
        llm_note = t("status.local_llm", model=cfg.llm_model)
    elif llm_ok:
        llm_note = t("status.cloud_llm", model=cfg.llm_model)
    status[t("status.llm")] = (llm_ok, llm_note)
    # 精排模型灯：local_onnx 检查本地模型文件；llm 后端依赖大模型灯（此处恒绿）
    if (cfg.rerank_backend or "llm").strip().lower() == "local_onnx":
        rerank_ready = ((cfg.rerank_model_dir / "model_quantized.onnx").exists()
                        and (cfg.rerank_model_dir / "tokenizer.json").exists())
        status[t("status.rerank")] = (
            rerank_ready,
            t("status.rerank.local") if rerank_ready else t("status.rerank.missing"),
        )
    else:
        status[t("status.rerank")] = (True, t("status.rerank.llm"))
    # MCP 知识服务灯：streamable-http（8760）健康时对未带令牌的探测返回 401，
    # 401 < 500 即为在线（与鉴权拒绝共用状态码）；连接失败/超时 = 未启动。
    # 探测结果随上方 30 秒缓存一起生效，不拖慢页面交互。
    try:
        import requests as _requests
        resp = _requests.get("http://127.0.0.1:8760/mcp", timeout=2)
        mcp_ok = resp.status_code < 500
    except Exception:
        mcp_ok = False
    status[t("status.mcp")] = (
        mcp_ok,
        t("status.mcp.mode", mode=os.environ.get("MCP_MODE", "query")) if mcp_ok
        else t("status.mcp.down"),
    )
    return status


def status_pills() -> str:
    pills = []
    for name, (ok, note) in system_status(current_language()).items():
        pills.append(f'<span class="pill">{"✅" if ok else "❌"} {name} · {note}</span>')
    return "".join(pills)


def tag(text: str, color: str = "blue") -> str:
    return f'<span class="tag {color}">{text}</span>'


def build_filters() -> dict:
    """把设置页的检索设置翻译成 Metadata Filter（值为语言无关的代码）。"""
    filters: dict = {}
    # V3.8：已保存的领域选择若已被删除，回落为全部——否则过滤条件永远检索不到内容
    domain_choice = domain_reg.sanitize_choice(st.session_state["domain_choice"])
    if domain_choice != "all":
        filters["domain"] = domain_choice
    scope = st.session_state["scope_choice"]
    if scope == "all":
        filters["status"] = "all"
    elif scope == "archive":
        filters["status"] = "archive"
    # 类型只做用户指定的精确过滤，不参与问题理解或排序；坏偏好回落为不过滤。
    try:
        doc_type_choice = normalize_doc_type_filter(st.session_state.get("doc_type_choice", ""))
    except ValueError:
        doc_type_choice = None
    if doc_type_choice is not None:
        filters["doc_type"] = doc_type_choice
    # 归属部门同理：精确过滤档案字段，与可见权限无关；坏偏好回落为不过滤。
    try:
        department_choice = normalize_department_filter(st.session_state.get("department_choice", ""))
    except ValueError:
        department_choice = None
    if department_choice is not None:
        filters["department"] = department_choice
    level = st.session_state.get("classification_choice")
    if level in ("公开", "内部", "受限"):
        filters["classification"] = level
    return filters


# ---------------------------------------------------------------- V3.7 登录门
def require_perm(perm: str) -> bool:
    """页面级权限守卫（纵深防御）：无权限渲染拒绝提示并 st.stop()。

    管理员已不能登录前台（硬性规则）；员工按管理后台分配的前台权限。
    """
    _au = st.session_state.get("auth_user") or {}
    if perm in (_au.get("permissions") or []):
        return True
    st.error(t("perm.denied"))
    st.stop()
    return False


def _viewer_filters() -> dict:
    username = (st.session_state.get("auth_user") or {}).get("username")
    user = auth_db.get_user(username) if username else None
    if not user or user["status"] != "active":
        st.error(t("perm.denied"))
        st.stop()
    return scope_filters(None, visible_groups(user.get("department_group")), user["username"])


def _upload_blocked_paths(ledger: dict) -> frozenset[Path]:
    from src.permissions import document_visible

    viewer = _viewer_filters()["__viewer__"]
    blocked = {
        Path(doc["path"]) for doc in store.list_documents()
        if doc["status"] in ("active", "archive")
        and not document_visible(doc, viewer["groups"], viewer["username"])
    }
    for rel in ledger:
        info = istate.visibility_info_for(ledger, rel)
        acl = info.get("value") or info.get("manual")
        if acl is not None and not document_visible(acl, viewer["groups"], viewer["username"]):
            blocked.add(Path(rel))
    return frozenset(blocked)


def document_department_options(current: str = "") -> list[str]:
    """归属部门选项使用主数据名称，不读取或改变文档可见权限。"""
    names = [d["name"] for d in auth_db.list_departments(include_disabled=False)]
    if current and current not in names:
        names.append(current)  # 旧文档的已停用归属仍可保留或清空。
    return [""] + names


def project_label(project_id: int | None, catalog: dict) -> str:
    if project_id is None:
        return t("project.none")
    return catalog.get(project_id, {}).get("name") or f"#{project_id}"


def _default_project_domain(project_key: str, domain_key: str, codes: list[str]) -> None:
    if st.session_state[project_key] is not None and "projects" in codes:
        st.session_state[domain_key] = "projects"


def _upload_project(catalog: dict, key: str, domain_key: str, codes: list[str]) -> int | None:
    selected = st.selectbox(
        t("common.project"), [None, *catalog],
        format_func=lambda project_id: project_label(project_id, catalog),
        placeholder=t("project.none"),
        help=t("project.upload.help"),
        key=key, on_change=_default_project_domain, args=(key, domain_key, codes),
    )
    if selected is not None:
        direction = catalog[selected].get("direction_name") or t("department.unspecified")
        st.caption(t("project.direction", value=direction))
    return selected


def _upload_domain(codes: list[str], key: str, preferred: str, label: str) -> str:
    if st.session_state.get(key) not in codes:
        st.session_state[key] = codes[_domain_index(codes, preferred)]
    return st.selectbox(label, codes, format_func=domain_label, index=None, key=key)


def default_document_department(options: list[str]) -> str:
    own = str((st.session_state.get("auth_user") or {}).get("department") or "")
    return own if own in options else ""


def group_write_options(current: list[str] | None = None) -> list[str]:
    """写入侧（上传归属 / 管理编辑）允许的可见域选项：全员域 + 自己部门。

    规则本体在 src/permissions.write_group_options（纯函数，有单测）。
    """
    _au = st.session_state.get("auth_user") or {}
    return write_group_options(auth_db.department_permission_key(_au.get("department"), _au.get("department_id")), current)


def _visibility_classification(groups, users) -> str:
    """Display classification from the actual ACL, never a stale metadata label."""
    if users or not groups:
        return "受限"
    return "公开" if "all" in groups else "内部"


def _visibility_label(classification: str) -> str:
    key = {"公开": "public", "内部": "internal", "受限": "restricted"}.get(classification)
    return t(f"visibility.{key}") if key else "—"


def _visibility_employee_label(user: dict) -> str:
    username = user["username"]
    name = str(user.get("real_name") or username)
    return f"{name}（{username}）" if name != username else username


def _visibility_active_employees() -> list[dict]:
    return [u for u in auth_db.list_users() if u.get("status") == "active" and u.get("username")]


def _visibility_employee_changed(checkbox_key: str, username: str) -> None:
    request = st.session_state.get("visibility_picker_request")
    if not request:
        return
    draft = set(request["draft"])
    if st.session_state.get(checkbox_key):
        draft.add(username)
    else:
        draft.discard(username)
    request["draft"] = sorted(draft)


def _visibility_toggle_department(prefix: str, usernames: set[str]) -> None:
    request = st.session_state.get("visibility_picker_request")
    if not request or request["prefix"] != prefix:
        return
    draft = set(request["draft"])
    select = not usernames.issubset(draft)
    request["draft"] = sorted(draft | usernames if select else draft - usernames)
    for username in usernames:
        checkbox_key = f"{prefix}_employee_{username}"
        if checkbox_key in st.session_state:
            st.session_state[checkbox_key] = select


def _open_visibility_employee_picker(key: str) -> None:
    revision = st.session_state.get("visibility_picker_revision", 0) + 1
    st.session_state["visibility_picker_revision"] = revision
    st.session_state["visibility_picker_request"] = {
        "key": key,
        "prefix": f"{key}_employee_draft_{revision}",
        "draft": list(st.session_state.get(f"{key}_allowed_users", [])),
    }
    visibility_employee_picker()


@st.dialog(t("visibility.employees.title"), width=1120 if int in get_args(DialogWidth) else "medium")
def visibility_employee_picker() -> None:
    """Searchable department sections; only Confirm publishes the draft ACL."""
    request = st.session_state.get("visibility_picker_request")
    if not request:
        return
    prefix = request["prefix"]
    employees = _visibility_active_employees()
    query = st.text_input(t("visibility.employees.search"), key=f"{prefix}_search").strip().casefold()
    st.caption(t("visibility.employees.help"))
    sections: dict[int | None, list[dict]] = {}
    for employee in employees:
        sections.setdefault(employee.get("department_id"), []).append(employee)
    draft = set(request["draft"])
    matches_found = False
    for department_id, members in sections.items():
        label = members[0].get("department") or t("department.unspecified")
        matches = [u for u in members if not query or query in " ".join(
            [u["username"], str(u.get("real_name") or ""), label]).casefold()]
        if not matches:
            continue
        matches_found = True
        with st.container(horizontal=True, vertical_alignment="center"):
            st.subheader(label, width="content")
            if department_id is not None:
                usernames = {u["username"] for u in members}
                action = "clear_department" if usernames.issubset(draft) else "select_department"
                st.button(t(f"visibility.employees.{action}", n=len(usernames)),
                          key=f"{prefix}_department_{department_id}",
                          on_click=_visibility_toggle_department, args=(prefix, usernames))
        for employee in matches:
            username = employee["username"]
            checkbox_key = f"{prefix}_employee_{username}"
            selected = st.checkbox(
                _visibility_employee_label(employee),
                value=checkbox_key not in st.session_state and username in draft,
                key=checkbox_key, on_change=_visibility_employee_changed,
                args=(checkbox_key, username),
            )
            if selected:
                draft.add(username)
            else:
                draft.discard(username)
    if not matches_found:
        st.info(t("visibility.employees.no_matches"))
    request["draft"] = sorted(draft)  # Includes selected employees hidden by search.
    st.caption(t("visibility.employees.selected", n=len(draft)))
    cancel_col, confirm_col = st.columns(2)
    if cancel_col.button(t("common.cancel"), key=f"{prefix}_cancel", width="stretch"):
        st.session_state.pop("visibility_picker_request", None)
        st.rerun()
        return
    active_names = {u["username"] for u in employees}
    confirmed_users = sorted(draft & active_names)
    if confirm_col.button(t("visibility.employees.confirm"), key=f"{prefix}_confirm",
                          disabled=not confirmed_users, type="primary", width="stretch"):
        st.session_state[f"{request['key']}_allowed_users"] = confirmed_users
        st.session_state.pop("visibility_picker_request", None)
        st.rerun()


def render_visibility_selector(key: str, current=None) -> dict | None:
    """Reusable upload/edit ACL selector; None means an empty, non-submittable ACL.

    Keys and stored employee values are usernames, not display-label tuples. For
    edits, initialize from the real groups/users, including explicit empty lists.
    """
    groups = list(current.get("allowed_groups", []) or []) if current is not None else []
    users = list(current.get("allowed_users", []) or []) if current is not None else []
    initial = {"公开": "public", "内部": "internal", "受限": "restricted"}[
        _visibility_classification(groups, users)] if current is not None else "internal"
    st.session_state.setdefault(f"{key}_allowed_users", users)
    levels = ["public", "internal", "restricted"]
    level = st.radio(t("visibility.level"), levels, index=levels.index(initial),
                     format_func=lambda value: t(f"visibility.{value}"),
                     key=f"{key}_visibility", horizontal=True, help=t("visibility.help"))
    if level == "public":
        st.caption(t("visibility.public.hint"))
        return {"allowed_groups": ["all"], "allowed_users": [], "classification": "公开"}
    if level == "internal":
        departments = auth_db.list_departments(include_disabled=False)
        options = list(dict.fromkeys(d["permission_key"] for d in departments
                                    if d.get("permission_key") and d["permission_key"] != "all"))
        if current is not None:
            options = list(dict.fromkeys(options + [g for g in groups if g != "all"]))
        own = st.session_state.get("auth_user") or {}
        own_key = auth_db.department_permission_key(own.get("department"), own.get("department_id"))
        defaults = [g for g in groups if g in options] if current is not None else (
            [own_key] if own_key in options else [])
        selected = st.multiselect(
            t("visibility.departments"), options, default=defaults, key=f"{key}_allowed_groups",
            format_func=auth_db.department_label, help=t("upload.groups.internal.help"),
            disabled=not options,
        )
        if not selected:
            st.error(t("visibility.internal.required") if options else t("visibility.internal.no_departments"))
            return None
        return {"allowed_groups": list(selected), "allowed_users": [], "classification": "内部"}
    selected = list(st.session_state.get(f"{key}_allowed_users", []))
    with st.container(horizontal=True, vertical_alignment="center"):
        if st.button(t("visibility.employees.choose"), key=f"{key}_choose_employees"):
            _open_visibility_employee_picker(key)
        with st.container(width="content", gap=None):
            st.caption(t("visibility.employees.selected", n=len(selected)))
            if not selected:
                st.caption(f":red[{t('visibility.restricted.required')}]")
    if not selected:
        return None
    employees = {u["username"]: u for u in _visibility_active_employees()}
    st.write(", ".join(_visibility_employee_label(employees[u]) if u in employees else u for u in selected))
    return {"allowed_groups": [], "allowed_users": selected, "classification": "受限"}


def _validate_upload_visibility(groups, users) -> None:
    # Pure validator is shared with the core; never silently turn [] into [all].
    from src.permissions import normalize_visibility
    normalized = normalize_visibility(groups, users, legacy_default=False)
    if not normalized["allowed_groups"] and not normalized["allowed_users"]:
        raise ValueError(t("visibility.restricted.required"))


def _apply_user_prefs(username: str) -> None:
    """登录后把该用户的界面偏好套到 session（覆盖默认值）。"""
    prefs = auth_client.get_prefs(username) or {}
    for key, value in prefs.items():
        if key in SETTINGS_KEYS and key != "installed_plugins":
            st.session_state[key] = value


front_host_global = "localhost:8501"  # 由 auth_gate 每次渲染时更新


def try_restore_session() -> bool:
    """front_auth Cookie（30 天）里若还有有效登录态，恢复 auth_user（含偏好）。

    必须在侧边栏渲染之前调用——恢复当帧的用户信息卡才能带上身份。
    """
    try:
        cookie_val = st.context.cookies.get("front_auth", "")
    except Exception:
        return False
    # 写入时做了 quote 转纯 ASCII（中文名兼容）——读回先还原再验签
    restored_username = auth_db.verify_session_cookie(unquote(cookie_val))
    if not restored_username:
        return False
    restored = auth_db.get_user(restored_username)
    if not restored or restored["status"] != "active":
        return False
    st.session_state["auth_user"] = {
        "username": restored["username"],
        "department": restored.get("department"),
        "department_id": restored.get("department_id"),
        "permissions": restored.get("permissions") or [],
        "real_name": restored.get("real_name") or "",
        "note": restored.get("note") or "",
    }
    _apply_user_prefs(restored["username"])
    return True


front_host_global = "localhost:8501"  # 由 auth_gate 每次渲染时更新


def auth_gate() -> bool:
    """V3.7 登录门。返回 True = 已登录，继续渲染应用；否则渲染登录/注册页。

    - 系统没有任何用户 → 只显示"初始化管理员"表单（首账户自动成为管理员）
    - 已有用户 → 登录 / 注册 两个标签页（注册需选择部门，密码两次确认）
    - 认证服务（8700）未启动 → 显示提示并停止
    """
    if signed_in := st.session_state.get("auth_user"):
        user = auth_db.get_user(signed_in["username"])
        if user and user["status"] == "active":
            for field in ("department", "department_id", "permissions"):
                signed_in[field] = user.get(field)
            return True
        st.session_state.pop("auth_user", None)

    # 浏览器访问前台的 host（含端口）——供持久化 iframe 拼认证服务地址
    global front_host_global
    try:
        front_host = st.context.headers.get("host", "localhost:8501")
        front_host_global = front_host
    except Exception:
        front_host = front_host_global

    if try_restore_session():
        return True

    def _finish_login(data):
        """登录成功收尾：写入会话；并在浏览器本域写入 30 天 front_auth Cookie
        （前台页面自己写——同主机名无跨域问题），随后页面自动重载一次，
        重载后 Cookie 进入 WebSocket 握手 → 登录门自动恢复登录态。
        此后刷新/重启/重开浏览器都保持登录，直到主动退出或清理浏览器。"""
        st.session_state["auth_user"] = data["user"]
        username = data["user"]["username"]
        cookie_val = auth_db.make_session_cookie(username)
        # Cookie 值里的中文会破坏服务端 Cookie 头解析（非 ASCII 非法值）——
        # 整值 quote 成纯 ASCII，读取端 unquote 还原后再验签
        cookie_val = quote(cookie_val, safe="")
        from streamlit.components.v1 import html as components_html
        # parent（前台主页面）与本组件同域：直接写 cookie + 重载，无跨域依赖
        components_html(
            f'<script>try{{'
            f'parent.document.cookie = "front_auth={cookie_val}; path=/; max-age={30 * 86400}";'
            f'parent.location.reload();'
            f'}}catch(e){{parent.location.reload();}}</script>', height=0)
        st.caption(t("auth.persist.note"))
        st.stop()

    def _render_register_tab(tab, boot: dict) -> None:
        """注册表单（登录门与"只有注册"模式共用）。表单始终渲染，与管理员是否存在
        无关；提交时统一校验：密码一致、长度、必须选择部门（无部门即拦截）。
        """
        with tab:
            departments = boot.get("departments") or []
            with st.form("register_form", clear_on_submit=False):
                u = st.text_input(t("auth.username"), key="reg_user")
                # 部门控件始终显示：没有可选部门时给出"无"选项，提交时统一拦截
                dep = st.selectbox(
                    t("auth.department"),
                    departments or [None],
                    format_func=lambda d: d["name"] if d else t("auth.department.none"),
                )
                p1 = st.text_input(t("auth.password"), type="password", key="reg_pw1")
                p2 = st.text_input(t("auth.password.confirm"), type="password", key="reg_pw2")
                submitted = st.form_submit_button(t("auth.register.btn"), type="primary",
                                                  width="stretch")
                if submitted:
                    if dep is None:
                        st.error(t("auth.no_departments"))
                    elif p1 != p2:
                        st.error(t("auth.pwd.mismatch"))
                    elif len(p1) < 6:
                        st.error(t("auth.pwd.short"))
                    else:
                        data, err = auth_client.register(u.strip(), p1, p2, dep["id"])
                        if err:
                            st.error(err)
                        else:
                            data2, err2 = auth_client.login(u.strip(), p1)
                            if data2:
                                _finish_login(data2)
                            else:
                                st.success(t("auth.register.ok"))

    # V3.7.2 记录真实登录 IP：隐藏组件让浏览器直连认证服务 /auth/ping，认证服务
    # 看到浏览器真实局域网 IP 并写入 rip cookie（localhost 域共享，30 天有效）。
    # 注意 st.context.cookies 读的是页面加载（WebSocket 握手）时的 cookie——
    # 首次渲染时还没有，所以先 ping 后自动重载一次；重载后 cookie 进入握手即可用。
    # 计数器防死循环：ping 不生效（服务没起等）最多重载 2 次后照常登录（IP 留空）。
    try:
        rip_cookie = st.context.cookies.get("rip", "")
        _host_header = st.context.headers.get("host", "localhost:8501")
    except Exception:
        rip_cookie = ""
        _host_header = "localhost:8501"
    # 防刷新循环：用 URL 参数 ?rip=1 标记"ping 已尝试过"（URL 参数跨整页刷新持久；
    # session_state 不行——整页刷新会新建会话把计数器清零，导致无限刷新）。
    # ping 已尝试（无论成败）或已有 cookie，都不再重试，IP 尽力而为。
    ping_done = st.query_params.get("rip") == "1"
    if not rip_cookie and not ping_done:
        from streamlit.components.v1 import html as components_html
        # 主机名从 Host 请求头取（iframe 跨域读不到 parent.location）；
        # 父页面导航目标要带完整 host（含前台端口，避免落到 80）
        host_header = st.context.headers.get("host", "")
        hostname_part = host_header.split(":")[0]
        components_html(auth_client.ping_html(hostname_part, host_header), height=0)
        st.stop()
    boot = auth_client.bootstrap()
    if boot is None:
        st.error(t("auth.api.down"))
        st.stop()
    st.markdown(
        f'<div style="text-align:center;margin:7vh 0 6px;">'
        f'<div style="font-size:40px">🧠</div>'
        f'<h1 style="margin:8px 0 4px;">{t("auth.title")}</h1>'
        f'<div style="color:var(--text-muted)">{t("auth.subtitle")}</div></div>',
        unsafe_allow_html=True,
    )

    # 三个表单全部用 st.form 原子提交：裸 text_input+button 有值同步竞态——
    # 点击按钮时最后一个输入框的值可能还没提交到会话，处理器读到旧值，
    # 出现"两次密码明明一致却提示不一致"（登录偶尔要点两次也是这个根源）。
    # form 的字段值在点击提交按钮那一刻原子落地，竞态不可能发生。
    # 初始化管理员入口只在管理后台（8700/admin/login）；前台不依赖管理员存在，
    # 没有管理员时员工照样可以注册 / 登录（部门可选，见 _render_register_tab）

    if not boot.get("has_users"):
        # 没有员工：只显示注册（注册表单照常渲染，无部门时提交会被拦截）
        _tab_register = st.tabs([t("auth.tab.register")])[0]
        _render_register_tab(_tab_register, boot)
        st.stop()

    tab_login, tab_register = st.tabs([t("auth.tab.login"), t("auth.tab.register")])
    with tab_login:
        with st.form("login_form", clear_on_submit=False):
            u = st.text_input(t("auth.username"), key="login_user")
            p = st.text_input(t("auth.password"), type="password", key="login_pw")
            submitted = st.form_submit_button(t("auth.login.btn"), type="primary",
                                              width="stretch")
            if submitted:
                data, err = auth_client.login(u.strip(), p)
                if err:
                    st.error(err)
                else:
                    _finish_login(data)

    _render_register_tab(tab_register, boot)
    st.stop()

    with tab_register:
        departments = boot.get("departments") or []
        if not departments:
            st.info(t("auth.no_departments"))
        else:
            with st.form("register_form", clear_on_submit=False):
                u = st.text_input(t("auth.username"), key="reg_user")
                dep = st.selectbox(
                    t("auth.department"), departments,
                    format_func=lambda d: d["name"],
                )
                p1 = st.text_input(t("auth.password"), type="password", key="reg_pw1")
                p2 = st.text_input(t("auth.password.confirm"), type="password", key="reg_pw2")
                submitted = st.form_submit_button(t("auth.register.btn"), type="primary",
                                                  width="stretch")
                if submitted:
                    if p1 != p2:
                        st.error(t("auth.pwd.mismatch"))
                    elif len(p1) < 6:
                        st.error(t("auth.pwd.short"))
                    else:
                        data, err = auth_client.register(u.strip(), p1, p2, dep["id"])
                        if err:
                            st.error(err)
                        else:
                            data2, err2 = auth_client.login(u.strip(), p1)
                            if data2:
                                _finish_login(data2)
                            else:
                                st.success(t("auth.register.ok"))
    st.stop()


def _render_eval_card(evaluation: dict, usage: dict | None = None,
                      result: QAResult | None = None) -> None:
    """V3.6 回答质量评估卡片：LLM 两维评审（分档展示）+ 结构信号 + 模型消耗。

    评分只分档不做精确阈值——LLM 打分有波动（与精排同一教训），分档更稳。
    usage：本次回答的管线用量（Query 理解/精排/生成合计，缓存命中=原始运行）；
    评估自身的用量在 evaluation["usage"]（缓存命中会重跑评估，分开计避免重复）。
    """
    with st.expander(t("eval.title"), expanded=False):
        bands = {"good": ("good", "green"), "mid": ("mid", "amber"), "poor": ("poor", "red")}
        overall = evaluation.get("overall")
        if overall in bands:
            label, color = bands[overall]
            score_100 = evaluation.get("overall_score")
            score_part = f"　`{score_100}/100`" if isinstance(score_100, int) else ""
            st.markdown(f"{t('eval.overall')}：{tag(t(f'eval.band.{label}'), color)}{score_part}",
                        unsafe_allow_html=True)
            st.caption(t("eval.score.formula"))
        for dim_key, name_key, desc_key in (
            ("faithfulness", "eval.faithfulness", "eval.faithfulness.desc"),
            ("relevance", "eval.relevance", "eval.relevance.desc"),
            ("completeness", "eval.completeness", "eval.completeness.desc"),
        ):
            item = evaluation.get(dim_key)
            if not item:
                continue
            label, color = bands.get(item.get("band"), ("mid", "amber"))
            st.markdown(
                f"**{t(name_key)}**　{tag(t(f'eval.band.{label}'), color)} "
                f"`{item.get('score', 0)}/10`　·　{t(desc_key)}",
                unsafe_allow_html=True,
            )
            if item.get("reason"):
                safe_reason = html.escape(str(item["reason"]))
                st.markdown(f"<div class='chunk-quote'>{safe_reason}</div>",
                            unsafe_allow_html=True)
        if not evaluation.get("ok") and evaluation.get("error"):
            st.caption(t("eval.failed", err=evaluation["error"]))

        signals = evaluation.get("signals") or {}
        st.markdown(f"**{t('eval.signals.title')}**")
        lines = []
        split = signals.get("split") or {}
        if split.get("triggered"):
            lines.append(t("eval.signal.split.on", n=split.get("sub_queries", 0),
                           pool=split.get("pool", 0)))
        else:
            lines.append(t("eval.signal.split.off"))
        rr = signals.get("rerank") or {}
        if rr.get("ok"):
            lines.append(t("eval.signal.rerank", top=rr.get("top", 0),
                           med=rr.get("median", 0), gap=rr.get("gap", 0),
                           n=rr.get("n", 0)))
        ctx = signals.get("context") or {}
        if ctx.get("pool"):
            lines.append(t("eval.signal.context", pool=ctx.get("pool", 0),
                           final=ctx.get("final", 0)))
        cite = signals.get("cite") or {}
        if cite.get("total"):
            lines.append(t("eval.signal.cite", cited=cite.get("cited", 0),
                           total=cite.get("total", 0)))
        ref = signals.get("refusal") or {}
        lines.append(t(f"eval.signal.refusal.{ref.get('state', 'normal')}"))
        timing = evaluation.get("timing") or {}
        if result is not None:
            timing = stage_timing(result.elapsed or timing, result.trace)
        if timing.get("total"):
            if all(key in timing for key in ("retrieval_only", "rerank", "assembly")):
                lines.append(t("eval.signal.timing.total", total=f"{timing['total']:.2f}"))
                for name, key in (("qu", "query_understanding"), ("retrieval", "retrieval_only"),
                                  ("rerank", "rerank"), ("assembly", "assembly"), ("generation", "llm")):
                    lines.append(t(f"eval.signal.timing.{name}", seconds=f"{timing.get(key, 0.0):.3f}"))
                if "conversation" in timing:
                    lines.append(t("eval.signal.timing.conversation", seconds=f"{timing['conversation']:.3f}"))
                if "enumeration" in timing:
                    lines.append(t("eval.signal.timing.enumeration", seconds=f"{timing['enumeration']:.3f}"))
                if timing.get("mode") == "inferred":
                    lines.append(t("eval.signal.timing.inferred"))
            else:
                lines.append(t("eval.signal.timing", total=f"{timing['total']:.1f}",
                               qu=f"{timing.get('query_understanding', 0.0):.1f}",
                               retrieval=f"{timing.get('retrieval', 0.0):.1f}",
                               llm=f"{timing.get('llm', 0.0):.1f}"))
            if result is not None and any("缓存命中" in str(node.get("name", ""))
                                          for node in result.trace or []):
                lines.append(t("eval.signal.timing.cached"))
        for line in lines:
            st.markdown(line)

        # —— 💰 模型消耗（B16.1）：管线用量 + 评估用量，费用按当前单价即时计算 ——
        # 只统计云上 LLM：本地模型（Ollama 等本机端点）不产生费用，不显示消耗区块
        if cfg.llm_is_local:
            return
        stages: dict = dict(usage.get("stages") or {}) if usage else {}
        if evaluation.get("usage"):
            stages["evaluation"] = evaluation["usage"]
        if not stages:
            return
        total = merge_usage(list(stages.values()))
        prices = tuple(float(st.session_state.get(key, PRICE_DEFAULTS.get(key, 0.0)))
                       for key in ("llm_price_input", "llm_price_input_cached", "llm_price_output"))
        cost = compute_cost(total, *prices)
        st.markdown(f"**{t('eval.cost.title')}**")
        price_part = t("eval.cost.price", cost=f"{cost:.4f}") if any(prices) else ""
        st.markdown(t("eval.cost.total", calls=total["calls"], inp=f"{total['input']:,}",
                      cached=f"{total['input_cached']:,}", out=f"{total['output']:,}",
                      cost_part=price_part),
                    unsafe_allow_html=True)
        display_stages = dict(stages)
        enumeration_parts = [value for key, value in stages.items() if key.startswith("enumeration_")]
        if enumeration_parts:
            display_stages["enumeration"] = merge_usage(enumeration_parts)
        stage_names = (("conversation_summary", "eval.cost.stage.conversation"), ("enumeration", "eval.cost.stage.enumeration"), ("query_understanding", "eval.cost.stage.qu"), ("rerank", "eval.cost.stage.rerank"),
                       ("generation", "eval.cost.stage.generation"), ("evaluation", "eval.cost.stage.evaluation"))
        stage_lines = []
        for stage_key, name_key in stage_names:
            part = display_stages.get(stage_key)
            if not part:
                continue
            part_cost = compute_cost(part, *prices)
            stage_lines.append(t("eval.cost.stage.line", name=t(name_key),
                                 inp=f"{part.get('input', 0):,}", out=f"{part.get('output', 0):,}",
                                 cost=f"{part_cost:.4f}"))
        if stage_lines:
            st.markdown("<div class='chunk-quote'>" + "<br>".join(stage_lines) + "</div>",
                        unsafe_allow_html=True)
        if total.get("estimated"):
            st.caption(t("eval.cost.estimated"))
        if not any(prices):
            st.caption(t("eval.cost.price_hint"))


def show_answer_sources(result) -> None:
    """卡片式参考来源 + 调试面板。"""
    conversation = getattr(result, "conversation_meta", None) or {}
    if conversation:
        st.caption(t("chat.context.status", recent=conversation.get("recent_messages", 0),
                     recalled=len(conversation.get("recalled_indices", [])),
                     status=t("chat.context.summary." + conversation.get("summary_status", "not_needed"))))
        if conversation.get("invalidated"):
            st.caption(t("chat.context.invalidated"))
    coverage = getattr(result, "evidence_meta", None) or {}
    scope = coverage.get("metadata_scope") or {}
    if scope and not coverage.get("scope_issue"):
        actual_type = result.filters.get("doc_type")
        if "doc_type" in scope and isinstance(actual_type, str):
            scope = {**scope, "doc_type": actual_type}
        details = " · ".join(
            f"{t('common.' + field)} {scope[field]}"
            for field in ("project", "business_direction", "department", "doc_type") if field in scope
        )
        st.caption(t("sources.metadata_scope", scope=details))
    if coverage.get("route") in {"catalog", "document", "cross_document"}:
        st.caption(t("sources.coverage.range",
                     n=coverage.get("matched_documents", coverage.get("candidate_documents", 0)),
                     m=coverage.get("processed_documents", 0)))
        if coverage.get("coverage") == "partial":
            st.warning(t("sources.coverage.partial"))
        elif coverage.get("coverage") != "complete":
            st.caption(t("sources.coverage.unknown"))
    stage2 = coverage.get("stage2") or {}
    if stage2.get("status") == "executed":
        st.caption(t("sources.processing", n=stage2.get("items", 0),
                     b=stage2.get("valid_batches", 0), r=stage2.get("rounds", 0)))
        from src.enumeration_processing import stop_reason_text
        st.caption(t("sources.processing.stop", reason=stop_reason_text(
            stage2.get("stop_reason", "processing_error"), current_language())))
        if stage2.get("fallback"):
            st.warning(t("sources.processing.fallback"))
    with st.expander(t("sources.title", n=len(result.sources)), expanded=False):
        if not result.sources:
            st.write(t("sources.empty"))
        for item in result.sources:
            head = f"**[{item['rank']}] {html.escape(str(item['source']))}**　{t('sources.relevance')} `{item['score']:.3f}`"
            extras = []
            # 检索通道（向量/关键词/文档展开/分批提取…）：说明这条来源是怎么找回来的
            channels_raw = str(item.get("channels") or "").strip()
            if channels_raw:
                parts = [p.strip() for p in channels_raw.split("+") if p.strip()]
                extras.append("🧭 " + "+".join(t("sources.channel." + p) for p in parts))
            if item.get("section"):
                extras.append(t("sources.section", v=item["section"]))
            if item.get("page"):
                extras.append(t("sources.page", v=item["page"]))
            if extras:
                head += "　·　" + "　".join(extras)
            st.markdown(head)
            if item.get("path"):
                st.text(str(item["path"]))
            tags = [tag(f"🗂 {html.escape(str(item.get('domain') or ''))}", "blue"),
                    tag(html.escape(t("common.doc_type")) + ": "
                        + html.escape(str(item.get("doc_type") or "").strip()
                                      or t("doc_type.unclassified")), "blue"),
                    tag(html.escape(t("common.department")) + ": "
                        + html.escape(str(item.get("department") or "").strip()
                                      or t("department.unspecified")), "blue")]
            if item.get("topic"):
                tags.append(tag("🏷 " + html.escape(" / ".join(item["topic"]))))
            if item.get("classification"):
                tags.append(tag("🔒 " + html.escape(_visibility_label(item["classification"])), "blue"))
            tags.append(tag(f"v{item.get('version')}", "green" if item.get("status") == "active" else "amber"))
            tags.append(tag("✅ active" if item.get("status") == "active" else "🗄 archive",
                            "green" if item.get("status") == "active" else "amber"))
            st.markdown("".join(tags), unsafe_allow_html=True)
            # 卡片原文可能来自任意网页（存储型 XSS 面），必须转义后才能进 HTML div
            safe_text = html.escape(item["text"][:400]) + ("……" if len(item["text"]) > 400 else "")
            st.markdown(f"<div class='chunk-quote'>{safe_text}</div>", unsafe_allow_html=True)
            st.write("")

        # V3.5 一键复制
        # 下方不再重复显示回答全文（上方回答气泡已有）；仅保留来源清单的复制
        st.code(t("sources.copy_list") + "\n" + "\n".join(
            f"[{s['rank']}] {s['source']}（{t('sources.relevance')} {s['score']:.3f}）"
            + (t("sources.section", v=s["section"]) if s.get("section") else "")
            for s in result.sources), language=None)

    if st.session_state.get("debug_mode"):
        with st.expander(t("debug.title")):
            st.markdown('<span class="debug-marker"></span>', unsafe_allow_html=True)
            st.caption(t("debug.caption"))
            with st.expander(t("debug.pipeline_hint_title")):
                st.markdown(t("debug.pipeline_hint_body"))
            for idx, node in enumerate(result.trace, start=1):
                head = t("debug.node", icon=node["icon"], n=idx,
                         name=tr(node["name"]), time=node["time"])
                if node.get("elapsed"):
                    head += t("debug.elapsed", s=f"{node['elapsed']:.2f}")
                with st.expander(head):
                    if node.get("status") != "已执行":
                        st.info(t("debug.node_status", s=tr(node["status"])))
                    st.markdown(t("debug.what", summary=node["summary"]))
                    for label, value in node.get("items", []):
                        text = str(value)
                        st.markdown(f'<div class="dbg-label">{label}</div>', unsafe_allow_html=True)
                        if text:
                            shown = text if len(text) <= 6000 else text[:6000] + "……"
                            # HTML 转义防止特殊字符破坏渲染，换行转 <br> 保证显示
                            escaped = html.escape(shown).replace("\n", "<br>")
                            st.markdown(f'<div class="dbg-content">{escaped}</div>', unsafe_allow_html=True)
                        else:
                            st.caption(t("debug.empty"))
            if result.sources:
                st.markdown(t("debug.recall_table"))
                st.dataframe(
                    [{t("debug.col.rank"): s["rank"], t("debug.col.score"): round(s["score"], 4),
                      t("debug.col.channel"): s.get("channels", "向量"),
                      "Source": s["source"],
                      "Domain": s.get("domain"),
                      "Topic": ", ".join(s.get("topic") or []),
                      t("debug.col.section"): s.get("section") or "",
                      t("debug.col.page"): s.get("page") or "",
                      "Version": s.get("version"), "Status": s.get("status")}
                     for s in result.sources],
                    width="stretch", hide_index=True,
                )

    # V3.6 回答质量评估卡片：位置在参考来源/调试信息之下；
    # 新回答的评估计算在问答流程里完成并挂到 result.evaluation 后才会出现在这里，
    # 历史消息重绘时直接展示已算好的结果，不重新调用 LLM
    if getattr(result, "evaluation", None):
        _render_eval_card(result.evaluation, usage=getattr(result, "usage", None), result=result)


# ---------------------------------------------------------------- 页面 1：知识库问答
def _save_current_chat():
    try:
        sid = save_session(st.session_state["session_id"], st.session_state.messages,
                           username=str((st.session_state.get("auth_user") or {}).get("username") or ""),
                           context_state=st.session_state.get("conversation_state"))
    except SessionConflict:
        st.session_state["chat_save_conflict"] = True
        return False
    st.session_state["session_id"] = sid
    st.query_params["session"] = sid
    return True


def page_chat():
    _load_requested_session()
    st.session_state.setdefault("conversation_state", {})
    if st.session_state.get("chat_save_conflict"):
        st.warning(t("chat.save_conflict"))
    if "messages" not in st.session_state:
        st.session_state.messages = []
    # 「开启新会话」并入 hero 蓝卡右侧：有消息才显示——空会话点了也只是把空状态重置一遍
    if st.session_state.messages:
        hero_col, new_col = st.columns([5, 1.2], gap="small", vertical_alignment="center")
        with new_col:
            def _new_session_cb():
                st.session_state["session_id"] = new_session_id()
                st.session_state.messages = []
                st.session_state["conversation_state"] = {}
                st.session_state.pop("chat_save_conflict", None)
                st.query_params.pop("session", None)
                st.query_params.pop("load", None)
            st.button(t("chat.new_session"), width="stretch", type="primary",
                      icon=":material/add_comment:", key="new_chat_session",
                      on_click=_new_session_cb)
    else:
        hero_col = st.container()
    with hero_col:
        st.markdown(
            f"""
            <div class="hero">
              <h1>{t("chat.hero.title")}</h1>
              <p>{t("chat.hero.sub")}</p>
              {status_pills()}
            </div>
            """,
            unsafe_allow_html=True,
        )

    # V3.7.1 空会话引导：示例问题一键提问（点击后走与手输完全相同的问答流程）
    if not st.session_state.messages:
        ex_cols = st.columns(3)
        for i, ex_key in enumerate(("chat.ex.1", "chat.ex.2", "chat.ex.3")):
            if ex_cols[i].button(t(ex_key), key=f"chat_example_{i}", width="stretch"):
                st.session_state["chat_ask_example"] = t(ex_key)

    for message in st.session_state.messages:
        with st.chat_message(message["role"], avatar="🙋" if message["role"] == "user" else "🧠"):
            if message["role"] == "user":
                st.markdown(f'<div class="user-bubble">{html.escape(message["content"])}</div>',
                            unsafe_allow_html=True)
            else:
                st.markdown(format_answer_markdown(message["content"]))
                if message.get("result") is not None:
                    show_answer_sources(message["result"])

    # 流式回答结束后强制一次干净重绘：流式过程中前端对长对话的 DOM 复用
    # 可能出现新旧消息错位（旧答案渲染到新问题下方），rerun 从 session_state
    # 完整重画即可归位；随后锚定到底部
    if st.session_state.pop("scroll_answer_once", False):
        scroll_chat_to_latest(
            key=f"scroll_final_{st.session_state['session_id']}_{len(st.session_state.messages)}",
            behavior="auto",
        )

    if st.session_state.pop("scroll_loaded_session_once", False):
        scroll_chat_to_latest(
            key=f"scroll_loaded_{st.session_state['session_id']}",
            behavior="auto",
        )

    def _process_question(question: str) -> None:
        st.session_state.messages.append({"role": "user", "content": question,
                                          "created_at": datetime.now().isoformat(timespec="seconds")})
        with st.chat_message("user", avatar="🙋"):
            st.markdown(f'<div class="user-bubble">{html.escape(question)}</div>',
                        unsafe_allow_html=True)
        scroll_chat_to_latest(
            key=f"scroll_question_{st.session_state['session_id']}_{len(st.session_state.messages)}",
        )

        with st.chat_message("assistant", avatar="🧠"):
            if not cfg.llm_api_key:
                st.error(t("chat.no_api_key"))
                st.session_state.messages.append({"role": "assistant", "content": t("chat.no_api_key.msg")})
                _save_current_chat()
                return
            progress_ph = st.empty()  # 实时进度占位符
            # 提前初始化：流式中途出错时还能拿到占位符与半截内容，
            # 把悬挂的光标换成"回答中断"标注，而不是让 ▌ 一直闪
            answer_ph = None
            full_text = ""

            def _show_progress(msg):
                progress_ph.markdown(f"⏳ {tr(msg)}")

            def _mark_interrupted():
                """流式中断的页面收尾：去掉光标、给半截答案加中断标注。"""
                if answer_ph is not None and full_text:
                    answer_ph.markdown(
                        format_answer_markdown(full_text)
                        + f"\n\n> ⚠️ {t('chat.interrupted')}")

            try:
                # 流式回答期间持续贴底跟随：答案再长也始终看得到最新输出；
                # 用户主动上滑阅读时跟随自动让位。max_seconds 覆盖列举通道的
                # 90 秒预算 + 语义复审——跟随中途断掉会让人以为页面卡死。
                scroll_follow_stream(
                    key=f"follow_{st.session_state['session_id']}_{len(st.session_state.messages)}",
                    max_seconds=240,
                )
                if True:  # 保持缩进层级
                    # 传入历史消息实现多轮对话（Query 理解解析代词 + LLM 上下文连贯）。
                    # 最后一条是刚 append 的当前问题本身，必须排除——否则 LLM 收到两遍，
                    # 且 -6 滑窗实际只剩 2.5 轮历史
                    # 完整记录仅供单会话管理器选择，不再在前端丢掉第 4 轮之前的内容。
                    chat_history = st.session_state.messages[:-1]
                    # V3.7 权限：登录者注入可见部门（服务端强制，UI 改不掉）；
                    # 规则统一在 src/permissions.visible_groups
                    _au = st.session_state.get("auth_user") or {}
                    _user_groups = visible_groups(auth_db.department_permission_key(_au.get("department"), _au.get("department_id")))
                    result, deltas = answer_stream(
                        question,
                        top_k=st.session_state["top_k"],
                        filters=build_filters(),
                        use_query_understanding=st.session_state["query_understanding"],
                        use_hybrid=st.session_state["hybrid_search"],
                        use_rerank=st.session_state["rerank"],
                        history=chat_history,
                        session_id=st.session_state["session_id"],
                        conversation_state=st.session_state["conversation_state"],
                        on_progress=_show_progress,
                        user_groups=_user_groups,
                        username=str((st.session_state.get("auth_user") or {}).get("username") or ""),
                    )
                progress_ph.empty()
                answer_ph = st.empty()
                for delta in deltas:
                    full_text += delta
                    answer_ph.markdown(format_answer_markdown(full_text) + " ▌")
                formatted_answer = format_answer_markdown(result.answer or full_text or "")
                answer_ph.markdown(formatted_answer)
                progress_ph.empty()
                result.answer = formatted_answer or None
                # V3.6 回答质量评估：回答完成后才执行（不阻塞流式输出），
                # 失败降级为仅结构信号，绝不影响问答主流程
                if st.session_state.get("answer_eval", True):
                    eval_ph = st.empty()
                    eval_ph.markdown(f"⏳ {t('eval.pending')}")
                    try:
                        result.evaluation = run_evaluation(result)
                    except Exception as exc:
                        result.evaluation = {"ok": False, "overall": None,
                                             "faithfulness": None, "relevance": None,
                                             "signals": {},
                                             "error": f"{type(exc).__name__}: {exc}",
                                             "elapsed": 0.0}
                    eval_ph.empty()
                # V3.7.1 使用记录：本次问答落 SQLite（回答完成才写，失败不影响主流程）
                _qa_user = st.session_state.get("auth_user") or {}
                auth_db.log_qa_event(
                    username=str(_qa_user.get("username") or "local"),
                    department=str(_qa_user.get("department") or ""),
                    entry="web",
                    question=question,
                    answer=result.answer or "",
                    sources=result.sources,
                    filters=result.filters,
                    top_k=st.session_state.get("top_k", 5),
                    elapsed=float(result.elapsed.get("total", 0)),
                    usage=usage_for_log(result, result.evaluation if st.session_state.get('answer_eval', True) else None),
                )
                show_answer_sources(result)
                st.session_state.messages.append(
                    {"role": "assistant", "content": formatted_answer, "result": result,
                     "created_at": datetime.now().isoformat(timespec="seconds")}
                )
                _save_current_chat()
                scroll_chat_to_latest(
                    key=f"scroll_answer_{st.session_state['session_id']}_{len(st.session_state.messages)}",
                )
                # 回答完成后强制 rerun 重绘整个会话：消除流式过程中前端
                # DOM 复用错位（旧答案内容出现在新问题下方）
                st.session_state["scroll_answer_once"] = True
                st.rerun()
            except (LLMError, VectorStoreError, EmbeddingError) as exc:
                _mark_interrupted()
                st.error(str(exc))
                partial = (format_answer_markdown(full_text) + "\n\n") if full_text else ""
                st.session_state.messages.append(
                    {"role": "assistant", "content": partial + t("chat.error_occurred", err=exc)})
                _save_current_chat()
            except Exception as exc:
                _mark_interrupted()
                st.error(t("chat.unexpected", err=f"{type(exc).__name__}: {exc}"))
                partial = (format_answer_markdown(full_text) + "\n\n") if full_text else ""
                st.session_state.messages.append(
                    {"role": "assistant",
                     "content": partial + t("chat.unexpected", err=f"{type(exc).__name__}: {exc}")})
                _save_current_chat()

    # 嵌套在普通容器中，避免 Streamlit 的底部聊天容器在 Expander 展开时强制滚到底部。
    # CSS 仍将该容器固定在页面底部，交互与原聊天输入框一致。
    with st.container(key="chat_composer"):
        question = st.chat_input(t("chat.input"), key="chat_question")
    # 示例问题与手输入口合并路由（示例按钮先入 session，这里消费）
    question = question or st.session_state.pop("chat_ask_example", None)
    if question:
        _process_question(question)


# ---------------------------------------------------------------- 页面 2：上传文档
def page_upload():
    require_perm('upload')
    st.markdown(f'<div class="section-title">{t("upload.title")}</div>', unsafe_allow_html=True)
    # 归组文案不再放顶部 caption——由下方脚本在上传框内原地改写组件默认的
    # 平铺格式小字（types_grouped 同时供上传框改写与 type= 顺序使用）
    types_grouped = " · ".join(
        f"{label} {'/'.join(ext.lstrip('.') for ext in exts)}"
        for label, exts in _UPLOAD_TYPE_GROUPS
        if any(ext in SUPPORTED_EXTENSIONS for ext in exts)
    )

    _tabs = [t("upload.tab.file"), t("upload.tab.dir")]
    _has_web = is_installed("web_fetcher")
    if _has_web:
        _tabs.append(t("upload.tab.web"))
    _tab_objs = st.tabs(_tabs)
    tab_files = _tab_objs[0]
    tab_dir = _tab_objs[1]
    tab_web = _tab_objs[2] if _has_web else None
    project_catalog = {p["id"]: p for p in projects.list_projects()}

    with tab_files:
        with st.container(border=True):
            st.markdown(f'<div class="section-title">{t("upload.attribution")}</div>',
                        unsafe_allow_html=True)
            _up_codes = [c for c in domain_reg.enabled_codes() if c != "archive"]
            domain_col, project_col = st.columns(2)
            with domain_col:
                upload_domain = _upload_domain(_up_codes, "upload_domain", "learning", t("upload.domain"))
            with project_col:
                upload_project = _upload_project(project_catalog, "upload_project", "upload_domain", _up_codes)
            type_col, department_col = st.columns(2)
            upload_doc_type = type_col.text_input(
                t("doc_type.default"), value="", max_chars=80,
                key="upload_doc_type", help=t("doc_type.default.help"))
            # V3.9 归属部门：资料属于哪个部门（档案字段，与可见权限无关）；默认本人部门
            _dep_choices = document_department_options()
            _dep_default = default_document_department(_dep_choices)
            upload_department = department_col.selectbox(
                t("department.default"), _dep_choices,
                format_func=lambda v: t("department.unspecified") if v == "" else v,
                index=_dep_choices.index(_dep_default), key="upload_department",
                help=t("department.default.help"))
            upload_acl = render_visibility_selector("upload")
            upload_groups = upload_acl["allowed_groups"] if upload_acl else []
            upload_allowed_users = upload_acl["allowed_users"] if upload_acl else []
        uploads = st.file_uploader(t("upload.dropzone"),
                                   type=SUPPORTED_UPLOAD_TYPES, accept_multiple_files=True)
        if uploads:
            st.caption(t("upload.selected", n=len(uploads), kb=f"{sum(u.size for u in uploads) / 1024:.0f}"))
        if st.button(t("upload.start"), disabled=not uploads or upload_acl is None, width="stretch", type="primary"):
            _start_upload(uploads, upload_domain, upload_groups, default_doc_type=upload_doc_type,
                          default_department=normalize_department(upload_department) or None,
                          default_allowed_users=upload_allowed_users,
                          default_project_id=upload_project)
        _render_upload_confirmation(uploads, upload_domain, upload_groups,
                                    default_doc_type=upload_doc_type,
                                    default_department=normalize_department(upload_department) or None,
                                    default_allowed_users=upload_allowed_users,
                                    default_project_id=upload_project)

        # 上传框内的「200MB per file • 平铺扩展名」小字由组件按 type 参数渲染，
        # 无法直接分组——用同源 iframe 脚本原地改写为归组文案。Streamlit 重渲染
        # 会重建 DOM，MutationObserver 负责再次改写；内容已是归组文案时不重写，
        # 不会触发改写循环。
        choose_files = t("upload.browse_files")
        choose_folders = t("upload.browse_folders")
        uploader_labels = json.dumps({"Upload": choose_files, "Browse files": choose_files,
                                      "Upload directories": choose_folders, "Browse folders": choose_folders})
        from streamlit.components.v1 import html as components_html
        components_html(f'''
<script>
(function() {{
  var root = parent.document;
  var GROUPED = {types_grouped!r};
  var SIZE = '单文件不超过 200MB';
  var LABELS = {uploader_labels};
  function fix() {{
    root.querySelectorAll('[data-testid="stFileUploaderDropzone"] button').forEach(function(button) {{
      var walker = root.createTreeWalker(button, NodeFilter.SHOW_TEXT);
      for (var node = walker.nextNode(); node; node = walker.nextNode()) {{
        var source = node.skyUploadLabel || node.nodeValue;
        if (Object.hasOwn(LABELS, source)) {{
          node.skyUploadLabel = source;
          if (node.nodeValue !== LABELS[source]) node.nodeValue = LABELS[source];
        }}
      }}
    }});
    root.querySelectorAll('[data-testid="stFileUploaderDropzoneInstructions"] span').forEach(function(span) {{
      if (/per file/.test(span.textContent) && !/文档/.test(span.textContent)) {{
        span.innerHTML = GROUPED + '<br>' + SIZE;
      }}
    }});
  }}
  if (root.skyUploadObserver) root.skyUploadObserver.disconnect();
  fix();
  var observer = new MutationObserver(fix);
  root.skyUploadObserver = observer;
  observer.observe(root.body, {{childList: true, characterData: true, subtree: true}});
  window.addEventListener('unload', function() {{
    observer.disconnect();
    if (root.skyUploadObserver === observer) delete root.skyUploadObserver;
  }}, {{once: true}});
}})();
</script>
''', height=0)

    with tab_dir:
        st.markdown(t("dir.title"))
        st.caption(t("dir.caption"))
        # V3.7.2 归属设置卡片：领域与可见部门收进带边框容器
        with st.container(border=True):
            _dir_codes = [c for c in domain_reg.enabled_codes() if c != "archive"]
            domain_col, project_col = st.columns(2)
            with domain_col:
                dir_domain = _upload_domain(_dir_codes, "directory_domain", "learning", t("dir.target"))
            with project_col:
                dir_project = _upload_project(project_catalog, "directory_project", "directory_domain", _dir_codes)
            type_col, department_col = st.columns(2)
            dir_doc_type = type_col.text_input(
                t("doc_type.default"), value="", max_chars=80,
                key="dir_doc_type", help=t("doc_type.default.help"))
            # 部门归属只由人工声明/选择确定，不从目录名推断。
            _dir_dep_choices = document_department_options()
            dir_department = department_col.selectbox(
                t("department.default"), _dir_dep_choices,
                format_func=lambda v: t("department.unspecified") if v == "" else v,
                index=_dir_dep_choices.index(default_document_department(_dir_dep_choices)),
                key="dir_department", help=t("department.default.help"))
            dir_acl = render_visibility_selector("dir")
            dir_groups = dir_acl["allowed_groups"] if dir_acl else []
            dir_allowed_users = dir_acl["allowed_users"] if dir_acl else []
        directory_mode = st.segmented_control(
            t("dir.mode"),
            ["drop", "native"],
            format_func=lambda m: t("dir.mode.drop") if m == "drop" else t("dir.mode.native"),
            default="drop",
            key="directory_import_mode",
        )
        directory_batch = None
        native_directory_uploads = None
        batch_id = None
        if directory_mode == "drop":
            directory_batch = directory_drop_uploader(key="directory_drop_uploader")
        else:
            native_limit_mb = st.get_option("server.maxUploadSize")
            native_directory_uploads = st.file_uploader(
                t("dir.pick"),
                accept_multiple_files="directory",
                key="native_directory_picker",
                help=t("dir.native.help", limit=native_limit_mb),
            )
        decoded_entries = []
        if directory_batch or native_directory_uploads:
            try:
                if directory_batch:
                    decoded_entries = decode_directory_batch(directory_batch)
                    batch_id = directory_batch.get("id")
                else:
                    decoded_entries = decode_native_directory_uploads(native_directory_uploads)
                    upload_ids = [
                        str(getattr(upload, "file_id", f"{upload.name}:{upload.size}"))
                        for upload in native_directory_uploads
                    ]
                    batch_id = "native:" + "|".join(upload_ids)
                # 隐藏目录/文件（.rag_converted 转换缓存、.git 等）与 CLI 入库同一规则跳过，
                # 避免转换缓存被当新文档重复入库（幻影）
                from src.project_directories import is_generated_info_content, preview_directory

                decoded_entries = [(parts, content) for parts, content in decoded_entries
                                   if not _is_hidden_relpath(parts)
                                   and not is_generated_info_content(parts[-1], content)]
                roots = []
                for parts, _content in decoded_entries:
                    root = _clean_directory_name(parts[0])
                    if root not in roots:
                        roots.append(root)
                total_kb = sum(len(content) for _parts, content in decoded_entries) / 1024
                root_text = "、".join(roots)
                supported_count = sum(
                    1 for parts, _content in decoded_entries
                    if Path(parts[-1]).suffix.lower() in SUPPORTED_EXTENSIONS
                )
                st.success(t("dir.selected", n=len(decoded_entries),
                             kb=f"{total_kb:.1f}", docs=supported_count))
                target = preview_directory(cfg.knowledge_dir, dir_domain, dir_project).relative_to(cfg.knowledge_dir).as_posix()
                st.caption(t("dir.target_hint", domain=target, roots=root_text))
            except ValueError as exc:
                roots = []
                st.error(str(exc))
        else:
            roots = []
            st.info(t("dir.pick_hint"), icon="📁")

        # V3.8 重复导入防护：同名目录已存在时才出现选项，默认合并（按文件增量更新）
        dir_merge = True
        dir_target = target if roots else dir_domain
        directory_key = f"{batch_id}@{dir_domain}:{dir_project}"
        _dir_existing = [r for r in roots if (cfg.knowledge_dir / dir_target / r).exists()]
        if roots:
            legacy_roots = {
                parts[1] for rel, entry in istate.load_state().items()
                if entry.get("project_id") == dir_project
                and len(parts := Path(rel).parts) > 2 and parts[0] == dir_domain
                and (cfg.knowledge_dir / rel).is_file()
            }
            _dir_existing = [r for r in roots if r in _dir_existing or r in legacy_roots]
        if _dir_existing:
            _dir_mode = st.radio(
                t("dir.exists_mode", roots="、".join(_dir_existing)), ["merge", "copy"],
                format_func=lambda m: t("dir.exists.merge") if m == "merge" else t("dir.exists.copy"),
                key="dir_exists_mode")
            dir_merge = _dir_mode == "merge"

        if st.button(
            t("dir.start"),
            disabled=(
                not decoded_entries
                or dir_acl is None
                or not roots
                or st.session_state.get("processed_directory_batch") == directory_key
            ),
            width="stretch",
            type="primary",
            key="import_directory",
        ):
            _do_directory_upload(decoded_entries, dir_domain, batch_id, dir_groups, merge=dir_merge,
                                 default_doc_type=dir_doc_type,
                                 default_department=dir_department or None,
                                 default_allowed_users=dir_allowed_users,
                                 default_project_id=dir_project)

    # ---------- 🌐 添加网页 Tab ----------
    if _has_web:
        with tab_web:
            st.markdown(f'<div class="section-title">{t("web.title")}</div>', unsafe_allow_html=True)
            st.caption(t("web.caption"))

            web_url = st.text_input(t("web.url"), placeholder="https://example.com/article",
                                    help=t("web.url.help"))
            with st.container(border=True):
                _web_codes = [c for c in domain_reg.enabled_codes() if c != "archive"]
                domain_col, project_col = st.columns(2)
                with domain_col:
                    web_domain = _upload_domain(_web_codes, "web_domain", "reference", t("common.domain"))
                with project_col:
                    web_project = _upload_project(project_catalog, "web_project", "web_domain", _web_codes)
                type_col, department_col = st.columns(2)
                web_doc_type = type_col.text_input(
                    t("doc_type.default"), value="", max_chars=80,
                    key="web_doc_type", help=t("doc_type.default.help"))
                _web_dep_choices = document_department_options()
                web_department = department_col.selectbox(
                    t("department.default"), _web_dep_choices,
                    format_func=lambda v: t("department.unspecified") if v == "" else v,
                    index=_web_dep_choices.index(default_document_department(_web_dep_choices)),
                    key="web_department", help=t("department.default.help"))
                web_acl = render_visibility_selector("web")
                web_groups = web_acl["allowed_groups"] if web_acl else []
                web_allowed_users = web_acl["allowed_users"] if web_acl else []

            if st.button(t("web.ingest"), disabled=not web_url.strip() or web_acl is None,
                         width="stretch", type="primary"):
                from src.manage import ingest_web_page
                try:
                    with _new_ingest_audit(
                        "web", [IngestItem("", source_path=source_url_for_log(web_url.strip()))],
                        web_domain, web_groups, web_allowed_users, web_doc_type, web_department, web_project,
                    ) as audit:
                        _validate_upload_visibility(web_groups, web_allowed_users)
                        with st.status(t("web.status.working"), expanded=True) as status:
                            st.write(t("web.status.fetch"))
                            audit.stage = "preflight"
                            blocked_paths = _upload_blocked_paths(istate.load_state())
                            audit.stage = "validation"
                            result = ingest_web_page(
                                web_url.strip(), domain=web_domain,
                                on_progress=lambda msg: st.write(tr(msg)),
                                default_groups=web_groups,
                                default_doc_type=normalize_doc_type(web_doc_type) or None,
                                default_department=normalize_department(web_department) or None,
                                default_allowed_users=web_allowed_users,
                                blocked_paths=blocked_paths,
                                default_project_id=web_project,
                                owner=str((st.session_state.get("auth_user") or {}).get("username") or ""),
                                audit=audit,
                            )
                    status.update(label=t("web.status.done"), state="complete", expanded=False)
                    st.success(f"🎉 {result['message']}")
                except Exception as exc:
                    st.error(t("web.failed", err=exc))


def _upload_files(uploads) -> list[tuple[str, bytes]]:
    from src.project_directories import is_generated_info_content

    files = [(u.name, u.getvalue()) for u in uploads or []]
    return [(name, data) for name, data in files if not is_generated_info_content(name, data)]


def _new_ingest_audit(entry, items, domain, groups, users, doc_type, department, project_id, **options):
    account = st.session_state.get("auth_user") or {}
    browser = auth_client.browser_context()
    peer_ip = getattr(st.context, "ip_address", None)
    return IngestAudit(entry, items, username=str(account.get("username") or ""),
                       actor_department=str(account.get("department") or ""),
                       ip=browser["client_ip"], peer_ip=peer_ip or "",
                       user_agent=browser["user_agent"], accept_language=browser["accept_language"],
                       knowledge_dir=cfg.knowledge_dir, requested={
                           "domain": domain, "allowed_groups": groups, "allowed_users": users,
                           "doc_type": doc_type, "department": department, "project_id": project_id, **options,
                       })


def _plan_frontend_upload(files, target_dir, groups, users, doc_type, department, project_id):
    from src.project_directories import INFO_NAME, other_project_paths, preview_directory

    try:
        ledger = istate.load_state()
        if any(Path(name).name.casefold() == INFO_NAME for name, _ in files):
            raise ValueError(f"{INFO_NAME} 是入库目录的保留说明文件")
        destination = preview_directory(cfg.knowledge_dir, target_dir, project_id).relative_to(cfg.knowledge_dir).as_posix()
        return plan_uploads(files, destination, ledger, cfg.knowledge_dir,
                            blocked_paths=_upload_blocked_paths(ledger) | other_project_paths(ledger, project_id))
    except (ApiException, VectorStoreError, OSError, ValueError) as exc:
        items = [IngestItem(name, source_path=name, bytes=len(data)) for name, data in files]
        with _new_ingest_audit("file", items, target_dir, groups, users, doc_type, department, project_id) as audit:
            audit.fail("preflight")
        _show_management_error(exc)
        return None


def _upload_signature(files: list[tuple[str, bytes]], target_dir: str,
                      default_doc_type: str | None = None,
                      default_department: str | None = None,
                      default_project_id: int | None = None, *, groups=None, users=None) -> str:
    """Invalidate confirmation when files, attribution, or either ACL channel changes."""
    doc_type = normalize_doc_type("" if default_doc_type is None else default_doc_type)
    department = normalize_department("" if default_department is None else default_department)
    return content_hash(repr([(n, content_hash(d)) for n, d in files]
                             + [target_dir, doc_type, department, default_project_id,
                                None if groups is None else sorted(groups),
                                None if users is None else sorted(users)]).encode())


def _conflict_reason(d) -> str:
    if d.kind == KIND_DUPLICATE:
        return t("upload.conflict.duplicate", paths="、".join(d.existing))
    if d.kind == KIND_SAME_PATH:
        return t("upload.conflict.same_path", path=d.target)
    if d.kind == KIND_SAME_NAME:
        return t("upload.conflict.same_name", paths="、".join(d.existing))
    if d.kind == KIND_BATCH_DUPLICATE:
        return t("upload.conflict.batch_dup")
    if d.kind == KIND_BATCH_SAME_NAME:
        return t("upload.conflict.batch_name")
    return ""


def _action_label(action: str) -> str:
    if action.startswith(VERSION_OF):
        return t("upload.action.version_of", path=action[len(VERSION_OF):])
    return t({SKIP: "upload.action.skip", NEW_VERSION: "upload.action.new_version",
              NEW: "upload.action.new", COPY: "upload.action.copy"}[action])


def _start_upload(uploads, target_dir: str, groups, *, default_doc_type: str | None = None,
                  default_department: str | None = None, default_project_id: int | None = None,
                  default_allowed_users: list[str] | None = None) -> None:
    """V3.8 上传入口：先做冲突预检。全是新文件 → 直接入库（与以前一样不多一步）；
    有重复 / 同名冲突 → 进确认面板，由用户逐个确认（默认值按已定规则预选好）。"""
    files = _upload_files(uploads)
    if not files:
        st.info(t("ingest.nothing_changed"))
        return
    try:
        _validate_upload_visibility(groups, default_allowed_users)
        signature = _upload_signature(files, target_dir, default_doc_type, default_department,
                                      default_project_id, groups=groups, users=default_allowed_users)
    except ValueError as exc:
        st.session_state.pop("upload_pending", None)
        st.error(str(exc))
        return
    plan = _plan_frontend_upload(files, target_dir, groups, default_allowed_users,
                                 default_doc_type, default_department, default_project_id)
    if plan is None:
        return
    if any(d.needs_confirmation for d in plan):
        st.session_state["upload_pending"] = signature
    else:
        _run_upload(files, plan, {}, groups, default_doc_type=default_doc_type,
                    default_department=default_department, default_project_id=default_project_id,
                    default_allowed_users=default_allowed_users)


def _render_upload_confirmation(uploads, target_dir: str, groups, *,
                                default_doc_type: str | None = None,
                                default_department: str | None = None,
                                default_project_id: int | None = None,
                                default_allowed_users: list[str] | None = None) -> None:
    pending = st.session_state.get("upload_pending")
    if not pending:
        return
    files = _upload_files(uploads)
    try:
        _validate_upload_visibility(groups, default_allowed_users)
        signature = _upload_signature(files, target_dir, default_doc_type, default_department,
                                      default_project_id, groups=groups, users=default_allowed_users)
    except ValueError as exc:
        st.session_state.pop("upload_pending", None)
        st.error(str(exc))
        return
    if not files or pending != signature:
        st.session_state.pop("upload_pending", None)  # 选择变了：作废旧的确认
        return
    # 预检是确定性的（同样的文件 + 同样的台账），每次重绘重新计算即可，无需缓存文件内容
    plan = _plan_frontend_upload(files, target_dir, groups, default_allowed_users,
                                 default_doc_type, default_department, default_project_id)
    if plan is None:
        st.session_state.pop("upload_pending", None)
        return
    choices: dict[int, str] = {}
    with st.container(border=True):
        st.markdown(f"**{t('upload.conflict.title')}**")
        for index, d in enumerate(plan):
            if not d.needs_confirmation:
                continue
            st.markdown(f"📄 `{d.name}`")
            st.caption(_conflict_reason(d))
            if len(d.options) == 1:
                st.caption("→ " + _action_label(d.options[0]))
                continue
            choices[index] = st.radio(d.name, d.options, index=d.options.index(d.action),
                                      format_func=_action_label,
                                      key=f"upl_choice_{pending[:12]}_{index}_{d.digest[:12]}",
                                      label_visibility="collapsed")
        n_new = sum(1 for d in plan if not d.needs_confirmation)
        if n_new:
            st.caption(t("upload.conflict.new_files", n=n_new))
        col_ok, col_cancel = st.columns(2)
        confirmed = col_ok.button(t("upload.conflict.confirm"), type="primary",
                                  width="stretch", key="upl_confirm")
        cancelled = col_cancel.button(t("upload.conflict.cancel"),
                                      width="stretch", key="upl_cancel")
    if cancelled:
        st.session_state.pop("upload_pending", None)
        st.rerun()
    if confirmed:
        st.session_state.pop("upload_pending", None)
        _run_upload(files, plan, choices, groups, default_doc_type=default_doc_type,
                    default_department=default_department, default_project_id=default_project_id,
                    default_allowed_users=default_allowed_users)


def _run_upload(files, plan, choices, groups, *, default_doc_type: str | None = None,
                default_department: str | None = None, default_project_id: int | None = None,
                default_allowed_users: list[str] | None = None) -> None:
    items = [IngestItem(name, source_path=name, path=decision.target, bytes=len(data))
             for decision, (name, data) in zip(plan, files)]
    domain = items[0].path.split("/", 1)[0] if items else ""
    with _new_ingest_audit("file", items, domain, groups, default_allowed_users,
                           default_doc_type, default_department, default_project_id) as audit:
        try:
            _validate_upload_visibility(groups, default_allowed_users)
            default_doc_type = normalize_doc_type("" if default_doc_type is None else default_doc_type) or None
            if default_department is not None:
                default_department = normalize_department(default_department) or None
            if default_project_id is not None:
                projects.validate_project_id(default_project_id)
        except ValueError as exc:
            st.error(str(exc))
            return
        try:
            from contextlib import nullcontext
            from src.project_directories import other_project_paths, upload_directory

            audit.stage = "preflight"
            ledger = istate.load_state()
            blocked_paths = _upload_blocked_paths(ledger) | other_project_paths(ledger, default_project_id)
            audit.stage = "save"
            destination = nullcontext()
            if plan:
                destination = upload_directory(
                    cfg.knowledge_dir, domain, default_project_id,
                    expected=(cfg.knowledge_dir / plan[0].target).parent)
            with destination:
                to_ingest, skipped = execute_uploads(
                    plan, files, cfg.knowledge_dir, choices,
                    blocked_paths=blocked_paths,
                    on_saved=audit.saved, on_skipped=audit.skip)
        except (OSError, ValueError) as exc:
            st.error(str(exc))
            return
        if skipped:
            st.info(t("upload.skipped_dup", n=len(skipped), files="、".join(d.name for d in skipped)))
        if not to_ingest:
            st.info(t("ingest.nothing_changed"))
            return
        audit.stage = "index"
        with st.spinner(t("ingest.spinner")):
            try:
                # skip_unchanged：内容没变的不重跑整条流水线（「重新入库」按钮仍是强制重跑入口）
                summary = ingest_files(paths=to_ingest, skip_unchanged=True, default_groups=groups,
                                        default_doc_type=default_doc_type,
                                        default_department=default_department,
                                        default_project_id=default_project_id,
                                        default_allowed_users=default_allowed_users,
                                        owner=str((st.session_state.get("auth_user") or {}).get("username") or ""))
            except (LLMError, VectorStoreError, EmbeddingError) as exc:
                st.error(str(exc))
                return
            except Exception as exc:
                st.error(t("chat.unexpected", err=f"{type(exc).__name__}: {exc}"))
                return
        audit.finish(summary)
        _show_ingest_result(summary)


def _do_directory_upload(entries, dir_domain, batch_id, groups=None, merge: bool = True, *,
                         default_doc_type: str | None = None,
                         default_department: str | None = None,
                         default_project_id: int | None = None,
                         default_allowed_users: list[str] | None = None):
    """把浏览器选中的目录写入领域目录，然后增量入库。

    V3.8：同名目录已存在时默认合并（merge=True）——没变的跳过、改了的升版本、
    新增的入库，源目录里已删除的文件保留；merge=False 为另存副本目录（原行为）。
    """
    from src.project_directories import is_generated_info_content

    entries = [(parts, content) for parts, content in entries if not is_generated_info_content(parts[-1], content)]
    items = [IngestItem(parts[-1], source_path="/".join(parts), bytes=len(content))
             for parts, content in entries]
    with _new_ingest_audit("directory", items, dir_domain, groups, default_allowed_users,
                           default_doc_type, default_department, default_project_id,
                           directory_mode="merge" if merge else "copy") as audit:
        try:
            _validate_upload_visibility(groups, default_allowed_users)
            default_doc_type = normalize_doc_type("" if default_doc_type is None else default_doc_type) or None
            if default_department is not None:
                default_department = normalize_department(default_department) or None
            if default_project_id is not None:
                projects.validate_project_id(default_project_id)
        except ValueError as exc:
            st.error(str(exc))
            return
        grouped: dict[str, list[tuple[tuple[str, ...], bytes]]] = {}
        original_indices: dict[str, list[int]] = {}
        try:
            for index, (parts, content) in enumerate(entries):
                root = _clean_directory_name(parts[0])
                grouped.setdefault(root, []).append((parts[1:], content))
                # 保留用户选择的原始根目录；不要使用领域、清理后的名称或副本目录名。
                original_indices.setdefault(root, []).append(index)
        except ValueError as exc:
            st.error(str(exc))
            return

        order = [index for root in grouped for index in original_indices[root]]
        try:
            from src.project_directories import (
                INFO_NAME, managed_directory_names, other_project_paths, upload_directory, without_storage_directory,
            )

            audit.stage = "preflight"
            ledger = istate.load_state()
            blocked_paths = _upload_blocked_paths(ledger) | other_project_paths(ledger, default_project_id)
            if any(root.casefold() == INFO_NAME for root in grouped):
                raise ValueError(f"{INFO_NAME} 是入库目录的保留说明文件")
            audit.stage = "save"
            registered = managed_directory_names()
            legacy_paths = {
                Path(rel) for rel, entry in ledger.items()
                if entry.get("project_id") == default_project_id and without_storage_directory(rel, registered) == rel
            }
            with upload_directory(cfg.knowledge_dir, dir_domain, default_project_id) as domain_dir:
                saved, copied_roots = write_directory_import(
                    grouped, domain_dir, merge=merge, blocked_paths=blocked_paths,
                    knowledge_dir=cfg.knowledge_dir, legacy_paths=legacy_paths,
                    on_saved=lambda index, path: audit.saved(order[index], path))
        except (OSError, ValueError) as exc:
            st.error(str(exc))
            return
        # writer 按 grouped 的根目录/文件顺序返回实际路径（含另存副本的改名）。
        # 浏览器批次可交错多个根目录，因此先按相同分组顺序展开原始路径再配对。
        doc_type_paths = {str(path.resolve()): items[index].source_path for path, index in zip(saved, order)}

        if not saved:
            st.warning(t("dir.no_docs"))
            return

        ingest_paths = []
        for path, index in zip(saved, order):
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                ingest_paths.append(path)
            else:
                audit.skip(index, "unsupported_type")
        if not ingest_paths:
            st.warning(t("dir.copied_none", types=", ".join(sorted(SUPPORTED_EXTENSIONS))))
            return

        st.info(t("dir.copied", n=len(saved), domain=domain_dir.relative_to(cfg.knowledge_dir).as_posix(),
                  roots="、".join(copied_roots), docs=len(ingest_paths)))
        audit.stage = "index"
        with st.spinner(t("ingest.spinner")):
            try:
                summary = ingest_files(paths=ingest_paths, skip_unchanged=True, default_groups=groups,
                                       default_doc_type=default_doc_type, doc_type_paths=doc_type_paths,
                                       default_department=default_department,
                                       default_project_id=default_project_id,
                                       default_allowed_users=default_allowed_users,
                                       owner=str((st.session_state.get("auth_user") or {}).get("username") or ""))
            except (LLMError, VectorStoreError, EmbeddingError) as exc:
                st.error(str(exc))
                return
            except Exception as exc:
                st.error(t("chat.unexpected", err=f"{type(exc).__name__}: {exc}"))
                return
        audit.finish(summary)
        st.session_state["processed_directory_batch"] = f"{batch_id}@{dir_domain}:{default_project_id}"
        _show_ingest_result(summary)


def _show_ingest_result(summary):
    if summary.ok_files:
        st.success(t("ingest.done", ok=summary.ok_files, total=summary.total_files,
                     chunks=summary.total_chunks, secs=f"{summary.elapsed_seconds:.1f}"))
    elif summary.failed_files:
        st.error(t("ingest.fail"))
    else:
        # V3.8：全部未变化被跳过不是失败（增量入库的正常结果）
        st.info(t("ingest.nothing_changed"))
    if summary.new_files or summary.updated_files or summary.skipped_files:
        st.caption(t("ingest.breakdown", new=len(summary.new_files),
                     updated=len(summary.updated_files), skipped=len(summary.skipped_files)))
    if summary.file_rows:
        project_catalog = ({p["id"]: p for p in projects.list_projects()}
                           if any(r.get("project_id") for r in summary.file_rows) else {})
        st.dataframe(
            [{t("ingest.col.path"): r["path"], t("ingest.col.domain"): domain_label(r["domain"]),
              t("common.project"): project_label(r.get("project_id"), project_catalog),
              t("common.doc_type"): str(r.get("doc_type") or "").strip() or t("doc_type.unclassified"),
              t("common.department"): str(r.get("department") or "").strip() or t("department.unspecified"),
              t("visibility.level"): _visibility_label(r.get("classification")),
              t("ingest.col.status"): r["status"], t("ingest.col.chunks"): r["chunks"],
              t("version.col.declared"): r.get("document_version") or t("version.empty"),
              t("version.col.status"): t("version.state." + r.get("document_version_status", "unavailable")),
              t("version.source"): r.get("document_version_source") or "—"}
             for r in summary.file_rows],
            width="stretch", hide_index=True)
    for f in summary.failed_files:
        st.warning(f)

    st.divider()
    st.markdown(f'<div class="section-title">{t("upload.dir_status")}</div>', unsafe_allow_html=True)
    if cfg.knowledge_dir.exists():
        existing = sorted({cfg.knowledge_dir / doc["path"] for doc in store.list_documents(_viewer_filters())
                           if (cfg.knowledge_dir / doc["path"]).is_file()})
        by_domain: dict[str, int] = {}
        for p in existing:
            first = p.relative_to(cfg.knowledge_dir).parts[0]
            by_domain[first] = by_domain.get(first, 0) + 1
        st.markdown(
            # tag() 不转义、这里又允许 HTML：领域名称是用户填的，必须先转义
            "　".join(tag(f"{html.escape(domain_label(d))} · {n}", "blue") for d, n in sorted(by_domain.items())),
            unsafe_allow_html=True,
        )
        with st.expander(t("ingest.view_all", n=len(existing))):
            for path in existing:
                safe_rel = html.escape(path.relative_to(cfg.knowledge_dir).as_posix())
                st.markdown(f"<code style='font-size:.85rem'>{safe_rel}</code>"
                            f"<span style='color:#94A3B8;font-size:.8rem'>　{path.stat().st_size / 1024:.1f} KB</span>",
                            unsafe_allow_html=True)
    else:
        st.info(t("upload.dir_missing"))


# ---------------------------------------------------------------- 页面 3：知识管理台
@_guard_management_database
def page_manage():
    require_perm('manage')
    st.markdown(f'<div class="section-title">{t("manage.title")}</div>', unsafe_allow_html=True)
    st.caption(t("manage.caption"))
    documents = store.list_documents(_viewer_filters())
    if notice := st.session_state.pop("manage_action_notice", None):
        st.toast(notice, icon="✅")

    # V3.6 跨文档知识总结
    with st.expander(t("manage.sum.title")):
        st.caption(t("manage.sum.caption"))
        sum_topic = st.text_input(
            t("manage.sum.topic"),
            placeholder=t("manage.sum.placeholder"),
            key="sum_topic",
            help=t("manage.sum.help"),
        )
        if st.button(t("manage.sum.button"), disabled=not sum_topic.strip(), type="primary"):
            with st.spinner(t("manage.sum.spinner")):
                # V3.7.2 权限：跨文档总结同样按登录者部门过滤（规则同问答）
                _sum_au = st.session_state.get("auth_user") or {}
                _sum_groups = visible_groups(auth_db.department_permission_key(_sum_au.get("department"), _sum_au.get("department_id")))
                result = answer_question(
                    f"请综合知识库中与「{sum_topic.strip()}」相关的全部内容，"
                    "输出一份结构化知识总结：核心要点 + 对应出处编号。资料不足时明确说明。",
                    top_k=10,
                    user_groups=_sum_groups,
                    username=_sum_au.get("username"),
                )
            # V3.7.1 使用记录：跨文档总结同样落库（管理员操作）
            _sum_user = st.session_state.get("auth_user") or {}
            auth_db.log_qa_event(
                username=str(_sum_user.get("username") or "local"),
                department=str(_sum_user.get("department") or ""),
                entry="manage_summary",
                question=str(sum_topic.strip()),
                answer=result.answer or "",
                sources=result.sources,
                filters=result.filters,
                top_k=10,
                elapsed=float(result.elapsed.get("total", 0)),
                usage=usage_for_log(result),
            )
            st.markdown(result.answer or t("manage.sum.empty"))
            if result.sources:
                st.caption(t("manage.sum.sources",
                             names="、".join(sorted({s["source"] for s in result.sources}))))

    if not documents:
        st.info(t("manage.empty"))
        return

    total_chunks = sum(d["chunks"] for d in documents)
    archived = sum(1 for d in documents if d["status"] == "archive")
    domains_covered = len({d["domain"] for d in documents})
    with st.container(key="manage_summary"):
        m1, m2, m3, m4 = st.columns(4)
        m1.metric(t("manage.metric.files"), len(documents))
        m2.metric(t("manage.metric.chunks"), total_chunks)
        m3.metric(t("manage.metric.domains"), f"{domains_covered} / {len(domain_reg.all_codes())}")
        m4.metric(t("manage.metric.archived"), archived)

    project_catalog = {p["id"]: p for p in projects.list_projects()}
    f1, f2, f3, f4, f5, f6 = st.columns([3, 1.2, 1.2, 1.2, 1.2, 1.3])
    keyword = f1.text_input(t("manage.search"), placeholder=t("manage.search.ph"))
    dom_codes = ["all"] + domain_reg.all_codes()  # 含停用领域：其数据仍可检索与管理
    dom_filter = f2.selectbox(t("common.domain"), dom_codes,
                              format_func=lambda c: t("common.all") if c == "all" else domain_label(c))
    status_codes = ["all", "active", "archive"]
    status_filter = f3.selectbox(t("common.status"), status_codes,
                                 format_func=lambda s: t("common.all") if s == "all" else s,
                                 key="manage_status_select")
    origin_types = ["all", "web", "file"]
    origin_filter = f4.selectbox(t("manage.col.origin"), origin_types,
                                 format_func=lambda o: t("manage.origin.web") if o == "web"
                                 else t("manage.origin.file") if o == "file" else t("common.all"),
                                 key="manage_origin_select")
    # 从现有文档生成自由文本选项，不创建类型注册表；None/旧空值属于未分类。
    doc_types = sorted({str(d.get("doc_type") or "").strip() for d in documents} - {""})
    type_options = [("all", ""), ("unclassified", "")] + [("exact", value) for value in doc_types]
    type_filter = f5.selectbox(
        t("common.doc_type"), type_options,
        format_func=lambda choice: t("common.all") if choice[0] == "all"
        else t("doc_type.unclassified") if choice[0] == "unclassified" else choice[1],
        key="manage_doc_type_select",
    )
    # 归属部门筛选：全部 / 未指定 / 现有归属值（从文档元数据派生，不建部门副表）
    departments = sorted({str(d.get("department") or "").strip() for d in documents} - {""})
    department_options = [("all", ""), ("no_department", "")] + [("exact", value) for value in departments]
    department_filter = f6.selectbox(
        t("common.department"), department_options,
        format_func=lambda choice: t("common.all") if choice[0] == "all"
        else t("department.unspecified") if choice[0] == "no_department" else choice[1],
        key="manage_department_select",
    )
    project_ids = sorted({d["project_id"] for d in documents if d.get("project_id") is not None})
    project_column, direction_column, visibility_column = st.columns(3)
    project_filter = project_column.selectbox(
        t("common.project"), [None, 0, *project_ids], key="manage_project_select",
        format_func=lambda pid: t("common.all") if pid is None
        else project_label(pid or None, project_catalog),
    )
    directions = {p["direction_id"]: p["direction_name"] for pid in project_ids
                  if (p := project_catalog.get(pid)) and p["direction_id"] is not None}
    direction_filter = direction_column.selectbox(
        t("common.business_direction"), [None, *sorted(directions, key=directions.get)],
        format_func=lambda did: t("common.all") if did is None else directions[did],
        key="manage_project_direction_select",
    )
    visibility_filter = visibility_column.selectbox(
        t("visibility.level"), ["", "公开", "内部", "受限"],
        format_func=lambda value: _visibility_label(value) if value else t("common.all"),
        key="manage_classification_select",
    )

    # V3.7.1：网页快照带上原文网址 / 标题，搜索和「来源」筛选都能用（只读快照文件头，不动卡片）
    origins = {d["path"]: read_snapshot_origin(cfg.knowledge_dir / d["path"]) for d in documents}

    def _match(d) -> bool:
        hay = " ".join([d["path"], d["source"],
                        (origins.get(d["path"]) or {}).get("url") or "",
                        (origins.get(d["path"]) or {}).get("title") or ""]).lower()
        is_web = bool(origins.get(d["path"]))
        return ((not keyword or keyword.lower() in hay)
                and (dom_filter == "all" or d["domain"] == dom_filter)
                and (status_filter == "all" or d["status"] == status_filter)
                and (not visibility_filter or d.get("classification") == visibility_filter)
                and (origin_filter == "all" or is_web == (origin_filter == "web"))
                and (project_filter is None or (d.get("project_id") or 0) == project_filter)
                and (direction_filter is None
                     or project_catalog.get(d.get("project_id"), {}).get("direction_id") == direction_filter)
                and (type_filter[0] == "all"
                     or (type_filter[0] == "unclassified" and not str(d.get("doc_type") or "").strip())
                     or (type_filter[0] == "exact" and str(d.get("doc_type") or "").strip() == type_filter[1]))
                and (department_filter[0] == "all"
                     or (department_filter[0] == "no_department" and not str(d.get("department") or "").strip())
                     or (department_filter[0] == "exact" and str(d.get("department") or "").strip() == department_filter[1])))

    filtered = [d for d in documents if _match(d)]

    # 筛选变化回到首页；先清理失效选择，包括筛选结果为空的情况。
    selection_key = "manage_selected_documents"
    visible_ids = {d["document_id"] for d in filtered}
    selected_ids = set(st.session_state.get(selection_key, [])) & visible_ids
    st.session_state[selection_key] = sorted(selected_ids)
    filter_signature = (keyword, dom_filter, status_filter, origin_filter, type_filter,
                        department_filter, project_filter, direction_filter, visibility_filter)
    if st.session_state.get("manage_filter_signature") != filter_signature:
        st.session_state["manage_page"] = 1
        st.session_state["manage_filter_signature"] = filter_signature

    if not filtered:
        st.info(t("manage.no_match"))
        return

    # 列表头部多选工具栏。多选只改变当前选择，不直接触发归档/删除等操作。
    def _reset_manage_table():
        st.session_state["manage_table_revision"] = st.session_state.get("manage_table_revision", 0) + 1

    def _select_visible_documents(ids=tuple(sorted(visible_ids))):
        st.session_state[selection_key] = list(ids)
        _reset_manage_table()

    def _clear_visible_documents():
        st.session_state[selection_key] = []
        _reset_manage_table()

    list_head, select_col, clear_col = st.columns([5, 1.35, 1.2], vertical_alignment="center")
    list_head.markdown(f"{t('manage.list_header')}　<span style='color:var(--text-muted);font-size:.78rem'>"
                       f"{t('manage.list_stats', n=len(filtered), m=len(selected_ids))}</span>",
                       unsafe_allow_html=True)
    select_col.button(
        t("manage.select_all"),
        width="stretch",
        key="manage_select_all",
        disabled=not filtered or len(selected_ids) == len(visible_ids),
        on_click=_select_visible_documents,
    )
    clear_col.button(
        t("manage.clear_selection"),
        width="stretch",
        key="manage_clear_selection",
        disabled=not selected_ids,
        on_click=_clear_visible_documents,
    )

    def _reset_manage_page():
        st.session_state["manage_page"] = 1

    def _move_manage_page(delta):
        st.session_state["manage_page"] += delta

    # 先占列表位置，分页控件在下方渲染，其值仍可用于本次列表切片。
    table_slot = st.empty()
    with st.container(key="manage_pagination"):
        size_col, prev_col, page_col, next_col = st.columns(
            [2, 1, 1.5, 1], vertical_alignment="bottom"
        )
    page_size = size_col.selectbox(t("manage.page_size"), [20, 50, 100],
                                    key="manage_page_size", on_change=_reset_manage_page)
    total_pages = max(1, (len(filtered) + page_size - 1) // page_size)
    # 删除末页文件后自动回到最后一个有效页码。
    st.session_state["manage_page"] = min(
        max(1, st.session_state.get("manage_page", 1)), total_pages
    )
    page = st.session_state["manage_page"]
    prev_col.button(t("manage.prev_page"), key="manage_prev_page", width="stretch",
                    disabled=page == 1, on_click=_move_manage_page, args=(-1,))
    page_col.number_input(t("manage.page_number", n=total_pages),
                          min_value=1, max_value=total_pages, step=1, key="manage_page")
    next_col.button(t("manage.next_page"), key="manage_next_page", width="stretch",
                    disabled=page == total_pages, on_click=_move_manage_page, args=(1,))
    start = (page - 1) * page_size
    page_documents = filtered[start:start + page_size]
    page_ids = {d["document_id"] for d in page_documents}
    st.caption(t("manage.page_stats", start=start + 1, end=start + len(page_documents),
                 n=len(filtered)))

    # 编辑器按行号记录勾选：换页/筛选/刷新后重建，防止旧行号勾到另一份文件。
    table_signature = (filter_signature, page, page_size, current_language(),
                       tuple(d["document_id"] for d in page_documents))
    if st.session_state.get("manage_table_signature") != table_signature:
        _reset_manage_table()
        st.session_state["manage_table_signature"] = table_signature

    def _row(d) -> dict:
        origin = origins.get(d["path"]) or {}
        file_cell = ("🌐 " if origin else "") + str(d["source"])
        project = project_catalog.get(d.get("project_id"), {})
        return {"_id": d["document_id"], t("manage.col.select"): d["document_id"] in selected_ids,
                t("manage.col.file"): file_cell, t("manage.col.type"): "web" if origin
                else str(d.get("file_type") or "").upper(),
                t("common.doc_type"): str(d.get("doc_type") or "").strip() or t("doc_type.unclassified"),
                t("common.department"): str(d.get("department") or "").strip() or t("department.unspecified"),
                t("common.project"): project_label(d.get("project_id"), project_catalog),
                t("common.business_direction"): project.get("direction_name") or "—",
                t("manage.col.indexed_at"): str(d.get("indexed_at") or "")[:16],
                t("manage.col.path"): d["path"],
                "Domain": domain_label(d["domain"]),
                "Topic": ", ".join(d["topic"]) or "-",
                "Groups": ", ".join(d.get("allowed_groups", []) or []),
                "Version": d["version"],
                t("version.col.declared"): d.get("document_version") or t("version.empty"),
                t("version.col.status"): t("version.state." + d.get("document_version_status", "unavailable")),
                t("visibility.level"): _visibility_label(d.get("classification")),
                "Status": d["status"], t("manage.col.chunks"): d["chunks"],
                t("manage.col.url"): (origin.get("url") or None)}

    rows = [_row(d) for d in page_documents]
    edited = table_slot.data_editor(
        rows,
        width="stretch",
        # 大页保留表格滚动，页面只传输当前页的数据。
        height=(min(len(rows), 15) + 1) * 35 + 3,
        hide_index=True,
        key=f"manage_document_table_{st.session_state.get('manage_table_revision', 0)}",
        column_config={
            "_id": None,
            t("manage.col.select"): st.column_config.CheckboxColumn(t("manage.col.select"),
                                                                    help=t("manage.col.select.help")),
            t("manage.col.type"): st.column_config.TextColumn(t("manage.col.type"), width="small"),
            t("common.doc_type"): st.column_config.TextColumn(t("common.doc_type"), width="medium"),
            t("common.department"): st.column_config.TextColumn(t("common.department"), width="medium"),
            t("manage.col.url"): st.column_config.LinkColumn(t("manage.col.url"), width="medium",
                                                             help=t("manage.col.url.help")),
        },
        disabled=[t("manage.col.file"), t("manage.col.path"), t("manage.col.type"), t("common.doc_type"),
                  t("common.department"), t("common.project"), t("common.business_direction"), "Domain",
                  "Topic", "Groups", "Version", t("version.col.declared"),
                  t("version.col.status"), t("visibility.level"), "Status", t("manage.col.chunks"),
                  t("manage.col.url")],
    )
    records = edited.to_dict("records") if hasattr(edited, "to_dict") else list(edited)
    updated_ids = (selected_ids - page_ids) | {
        row["_id"] for row in records if row.get(t("manage.col.select"))
    }
    st.session_state[selection_key] = sorted(updated_ids)
    if updated_ids != selected_ids:
        st.rerun()  # 同步列表头部计数和全选/清空按钮状态。
    selected_ids = updated_ids

    if not selected_ids:
        return

    selected_documents = []
    selected_paths: set[str] = set()
    for item in filtered:
        if item["document_id"] in selected_ids and item["path"] not in selected_paths:
            selected_documents.append(item)
            selected_paths.add(item["path"])

    def _request_manage_remove(delete_files: bool) -> None:
        st.session_state["manage_remove_request"] = {
            "paths": [item["path"] for item in selected_documents],
            "names": [item["source"] for item in selected_documents],
            "delete_files": delete_files,
        }

    st.markdown(t("manage.batch") if len(selected_documents) > 1 else t("manage.single"))
    action_info, remove_index_col, remove_disk_col = st.columns(
        [2.2, 1.35, 1.7], vertical_alignment="center"
    )
    action_info.caption(
        t("manage.selected_info", n=len(selected_documents))
    )
    remove_index_col.button(
        t("manage.remove_index"),
        icon=":material/delete_sweep:",
        width="stretch",
        key="manage_remove_index",
        help=t("manage.remove_index.help"),
        on_click=_request_manage_remove,
        args=(False,),
    )
    remove_disk_col.button(
        t("manage.remove_disk"),
        icon=":material/delete_forever:",
        width="stretch",
        key="manage_remove_disk",
        help=t("manage.remove_disk.help"),
        on_click=_request_manage_remove,
        args=(True,),
    )

    if len(selected_ids) > 1:
        st.info(t("manage.multi_note"))
        return

    selected_id = next(iter(selected_ids))
    choices = [item for item in filtered if item["document_id"] == selected_id]
    d = next((item for item in choices if item["status"] in ("active", "archive")), choices[0])
    st.divider()

    head_l, head_r = st.columns([2.2, 1])
    with head_l:
        origin = origins.get(d["path"]) or {}
        status_tag = tag("✅ active", "green") if d["status"] == "active" else tag("🗄 archive", "amber")
        st.markdown(
            f"### {('🌐' if origin else '📄')} {html.escape(str(d['source']))}　{status_tag}"
            + tag(f"🗂 {html.escape(domain_label(str(d['domain'])))}", "blue")
            + tag(html.escape(t("common.doc_type")) + ": "
                  + html.escape(str(d.get("doc_type") or "").strip() or t("doc_type.unclassified")), "blue")
            + tag(html.escape(t("common.department")) + ": "
                  + html.escape(str(d.get("department") or "").strip() or t("department.unspecified")), "blue")
            + (tag("🏷 " + html.escape(" / ".join(d["topic"]))) if d["topic"] else "")
            + tag(f"v{html.escape(str(d['version']))}", "green")
            + tag("🔒 " + html.escape(_visibility_label(d.get("classification"))), "blue"),
            unsafe_allow_html=True,
        )
        st.caption(t("manage.detail.cards", id=d["document_id"], n=d["chunks"]))
        st.caption(t("version.detail", value=d.get("document_version") or t("version.empty"),
                     source=t("version.manual") if d.get("document_version_source") == "manual"
                     else d.get("document_version_source") or "—"))
        st.caption(t("version.revision_note"))
        if d.get("document_version_status") == "needs_review":
            st.warning(t("version.conflict"))
        if d.get("document_version_evidence"):
            with st.expander(t("version.evidence")):
                st.dataframe(d["document_version_evidence"], hide_index=True, width="stretch")
        if origin:
            if origin.get("title") and origin["title"] != d["source"]:
                st.caption(f"{t('manage.col.url')}：{html.escape(origin['title'])}")
            st.markdown(f'{t("manage.detail.origin")}'
                        f'[({html.escape(origin["url"])})]({html.escape(origin["url"])})',
                        unsafe_allow_html=True)
    with head_r:
        is_archived = d["path"].split("/")[0] == "archive"
        btn1, btn2, btn3 = st.columns(3)
        reingest_btn = btn1.button(t("manage.act.reingest"), width="stretch", key="act_reingest",
                                   help=t("manage.act.reingest.help"))
        refetch_btn = (btn1.button(t("manage.act.refetch"), width="stretch", key="act_refetch",
                                   disabled=is_archived, help=t("manage.act.refetch.help"))
                       if origin else None)
        archive_btn = btn2.button(t("manage.act.archive"), width="stretch", key="act_archive",
                                  disabled=is_archived, help=t("manage.act.archive.help"))
        restore_btn = btn2.button(t("manage.act.restore"), width="stretch", key="act_restore",
                                  disabled=not is_archived, help=t("manage.act.restore.help"))

        _owner = str((st.session_state.get("auth_user") or {}).get("username") or "")
        actions = [
            (reingest_btn, lambda: reingest_file(d["path"], owner=_owner)),
            (archive_btn, lambda: archive_file(d["path"], owner=_owner)),
            (restore_btn, lambda: restore_file(d["path"], owner=_owner)),
        ]
        if refetch_btn:
            actions.append((refetch_btn, lambda: refetch_web_page(d["path"], owner=_owner)))
        for btn, fn in actions:
            if btn:
                try:
                    info = fn()
                    st.toast(info["message"], icon="✅")
                    # 表格数据变了（状态/版本列），必须换新 key 让 data_editor 重建
                    st.session_state["manage_table_revision"] = \
                        st.session_state.get("manage_table_revision", 0) + 1
                    st.rerun()
                except ManageError as exc:
                    _show_management_error(exc)

    # 独立原子表单：仅保存当前文档的类型，绝不连带提交其他档案/权限输入。
    _type_suffix = f"{d['document_id']}_{d['version']}_{st.session_state.get('manage_table_revision', 0)}"
    with st.form(key=f"doc_type_form_{_type_suffix}"):
        new_doc_type = st.text_input(t("common.doc_type"), value=d.get("doc_type") or "",
                                     max_chars=80, key=f"ed_doc_type_{_type_suffix}",
                                     help=t("doc_type.help"))
        save_doc_type = st.form_submit_button(t("doc_type.save"))
    if save_doc_type:
        try:
            info = update_metadata(d["path"], {"doc_type": normalize_doc_type(new_doc_type)})
            st.session_state["manage_action_notice"] = info["message"]
            st.session_state["manage_table_revision"] = st.session_state.get("manage_table_revision", 0) + 1
            st.rerun()
        except (ManageError, ValueError) as exc:
            st.error(str(exc))

    # 独立原子表单：仅保存当前文档的归属部门（清空=未指定），绝不连带提交其他档案/权限输入。
    _current_department = str(d.get("department") or "").strip()
    _dep_edit_choices = document_department_options(_current_department)
    _department_suffix = f"{d['document_id']}_{d['version']}_{st.session_state.get('manage_table_revision', 0)}"
    with st.form(key=f"department_form_{_department_suffix}"):
        new_department = st.selectbox(
            t("common.department"), _dep_edit_choices,
            format_func=lambda v: t("department.unspecified") if v == "" else v,
            index=_dep_edit_choices.index(_current_department),
            key=f"ed_department_{_department_suffix}",
            help=t("department.filter.help"))
        save_department = st.form_submit_button(t("department.save"))
    if save_department:
        try:
            info = update_metadata(d["path"], {"department": new_department})
            st.session_state["manage_action_notice"] = info["message"]
            st.session_state["manage_table_revision"] = st.session_state.get("manage_table_revision", 0) + 1
            st.rerun()
        except (ManageError, ValueError) as exc:
            st.error(str(exc))

    current_project = d.get("project_id")
    project_options = [None, *project_catalog]
    if current_project not in project_options:
        project_options.append(current_project)
    with st.form(key=f"project_form_{_department_suffix}"):
        new_project = st.selectbox(
            t("common.project"), project_options,
            format_func=lambda pid: project_label(pid, project_catalog),
            index=project_options.index(current_project),
        )
        save_project = st.form_submit_button(t("project.save"))
    if save_project:
        try:
            info = update_metadata(d["path"], {"project_id": new_project})
            st.session_state["manage_action_notice"] = info["message"]
            st.session_state["manage_table_revision"] = st.session_state.get("manage_table_revision", 0) + 1
            st.rerun()
        except (ManageError, ValueError) as exc:
            st.error(str(exc))

    # Permission changes are isolated from archive/project/department edits.
    # The selector stays outside st.form so its real dialog can rerun on search.
    with st.container(border=True):
        st.markdown(f"**{t('visibility.edit.title')}**")
        permission_acl = render_visibility_selector(f"permission_{_department_suffix}", current=d)
        with st.form(key=f"permission_form_{_department_suffix}"):
            save_permissions = st.form_submit_button(t("visibility.save"), disabled=permission_acl is None)
        if save_permissions and permission_acl is not None:
            try:
                info = update_metadata(d["path"], {
                    "allowed_groups": permission_acl["allowed_groups"],
                    "allowed_users": permission_acl["allowed_users"],
                })
                st.session_state["manage_action_notice"] = info["message"]
                st.session_state["manage_table_revision"] = st.session_state.get("manage_table_revision", 0) + 1
                st.rerun()
            except (ManageError, ValueError) as exc:
                st.error(str(exc))

    # 档案编辑（V3.4）：key 绑定当前文档——固定 key 会让切换文档后沿用
    # 上一个文档的输入值，保存时把 A 的档案写进 B
    with st.expander(t("manage.edit.title")):
        _ed_suffix = d["document_id"]
        new_title = st.text_input(t("manage.edit.title.label"),
                                  value=d.get("source") or "",
                                  key=f"ed_title_{_ed_suffix}")
        new_topic = st.text_input(t("manage.edit.topic"), value=", ".join(d["topic"]),
                                  key=f"ed_topic_{_ed_suffix}")
        new_version = st.text_input(t("manage.edit.version"), value=d["version"],
                                    key=f"ed_ver_{_ed_suffix}")
        _declared_suffix = f"{_ed_suffix}_{d['version']}_{d.get('version_content_hash', '')}_{st.session_state.get('manage_table_revision', 0)}"
        declared_version = st.text_input(t("version.col.declared"), value=d.get("document_version") or "",
                                         key=f"ed_declared_{_declared_suffix}")
        confirm_version = st.checkbox(t("version.confirm"), key=f"ed_confirm_{_declared_suffix}",
                                      disabled=d["status"] not in ("active", "archive"))
        if st.button(t("manage.edit.save"), type="primary"):
            updates = {"title": new_title,
                       "topic": [t.strip() for t in new_topic.split(",") if t.strip()]}
            if new_version != d["version"]:
                updates["version"] = new_version
            try:
                if confirm_version:
                    if new_version != d["version"]:
                        raise ManageError(t("version.no_revision_change"))
                    __import__("src.manage", fromlist=["confirm_document_version"]).confirm_document_version(
                        d["path"], declared_version, d["version"], d.get("version_content_hash", ""))
                info = __import__("src.manage", fromlist=["update_metadata"]).update_metadata(d["path"], updates)
                st.toast(info["message"], icon="✅")
                st.session_state["manage_table_revision"] = \
                    st.session_state.get("manage_table_revision", 0) + 1
                st.rerun()
            except ManageError as exc:
                st.error(str(exc))

    payloads = store.chunks_by_document(d["document_id"], filters=_viewer_filters())
    st.markdown(t("manage.cards.title", n=len(payloads)))
    for payload in payloads:
        head = t("manage.card.head", chunk_id=payload.get("chunk_id", "?"),
                 section=payload.get("section") or "-", page=payload.get("page") or "-")
        with st.expander(head):
            # 网页抓取的卡片原文不可信，进 HTML div 前必须转义
            safe_chunk = html.escape(payload.get("text", ""))
            st.markdown(f"<div class='chunk-quote'>{safe_chunk}</div>",
                        unsafe_allow_html=True)


# ---------------------------------------------------------------- 页面 4：通用设置
def page_general_settings():
    st.markdown(f'<div class="section-title">{t("set.general.title")}</div>', unsafe_allow_html=True)
    st.caption(t("set.general.caption"))

    # ---------- 界面语言（点击选择，全部平铺；含「跟随系统」） ----------
    current_lang = st.session_state.get("language", FOLLOW_SYSTEM)
    lang_keys = [FOLLOW_SYSTEM] + LANGUAGE_ORDER
    lang_choice = st.segmented_control(
        t("set.language"), lang_keys,
        format_func=lambda c: t("lang.system") if c == FOLLOW_SYSTEM else LANGUAGES[c],
        default=current_lang if current_lang in lang_keys else FOLLOW_SYSTEM,
        key="set_language_ui",
    )
    if lang_choice and lang_choice != current_lang:
        st.session_state["language"] = lang_choice
        persist_app_settings()
        st.rerun()  # 立即以新语言重绘（导航、页面标题同步切换）
    if current_lang == FOLLOW_SYSTEM:
        st.caption(f"{t('set.language.caption')}　·　🌐 {t('lang.system')} → {LANGUAGES[browser_language()]}")
    else:
        st.caption(t("set.language.caption"))

    st.divider()

    # ---------- 外观主题 ----------
    st.markdown(f'<div class="section-title">{t("set.theme.section")}</div>', unsafe_allow_html=True)
    theme_keys = ["system", "light", "dark"]
    current_theme = st.session_state.get("theme", "system")
    theme_choice = st.radio(t("set.theme"), theme_keys,
                            format_func=lambda k: t(f"theme.{k}"),
                            index=theme_keys.index(current_theme) if current_theme in theme_keys else 0,
                            horizontal=True, key="set_theme_ui")
    if theme_choice != current_theme:
        st.session_state["theme"] = theme_choice
        persist_app_settings()
        st.rerun()


# ---------------------------------------------------------------- 页面 5：检索设置
def page_retrieval_settings():
    require_perm('retrieval_settings')
    st.markdown(f'<div class="section-title">{t("set.retrieval.title")}</div>', unsafe_allow_html=True)
    st.caption(t("set.retrieval.caption"))

    domain_codes = ["all"] + domain_reg.all_codes()  # 含停用领域：其数据仍可检索
    scope_codes = ["active", "all", "archive"]

    # 值保存语言无关的代码（all/work/…、active/all/archive），显示时本地化；
    # 显式传入当前生效值，保证第一次打开就显示正确数据；变化时同步回 session
    st.slider(t("set.top_k"), 1, 10,
              value=st.session_state["top_k"], key="set_top_k",
              on_change=_sync_setting("top_k", "set_top_k"))
    st.selectbox(t("set.domain"), domain_codes,
                 format_func=lambda c: t("common.all") if c == "all" else domain_label(c),
                 index=domain_codes.index(st.session_state["domain_choice"])
                 if st.session_state["domain_choice"] in domain_codes else 0,
                 key="set_domain", on_change=_sync_setting("domain_choice", "set_domain"))
    try:
        doc_type_choice = normalize_doc_type_filter(st.session_state.get("doc_type_choice", "")) or ""
    except ValueError:
        doc_type_choice = ""
    st.text_input(t("common.doc_type"), value=doc_type_choice, max_chars=80,
                  key="set_doc_type", on_change=_sync_setting("doc_type_choice", "set_doc_type"),
                  help=t("doc_type.filter.help"), placeholder=t("doc_type.filter.optional"))
    try:
        department_choice = normalize_department_filter(
            st.session_state.get("department_choice", "")) or ""
    except ValueError:
        department_choice = ""
    _reg_departments = document_department_options()[1:]
    _stored_departments = sorted({str(d.get("department") or "").strip()
                                    for d in store.list_documents(_viewer_filters())} - {""})
    department_options = list(dict.fromkeys(
        ["", "__no_department__"] + _reg_departments + _stored_departments
        + ([department_choice] if department_choice else [])))
    st.selectbox(t("common.department"), department_options,
                 format_func=lambda v: t("common.all") if v == ""
                 else t("department.unspecified") if v == "__no_department__" else v,
                 index=department_options.index(department_choice)
                 if department_choice in department_options else 0,
                 key="set_department", on_change=_sync_setting("department_choice", "set_department"),
                 help=t("department.filter.help"))
    visibility_choices = ["", "公开", "内部", "受限"]
    visibility_choice = st.session_state.get("classification_choice", "")
    st.selectbox(t("visibility.level"), visibility_choices,
                 format_func=lambda value: _visibility_label(value) if value else t("common.all"),
                 index=visibility_choices.index(visibility_choice) if visibility_choice in visibility_choices else 0,
                 key="set_classification", on_change=_sync_setting("classification_choice", "set_classification"))
    st.radio(t("set.scope"), scope_codes,
             format_func=lambda s: t(f"scope.{s}"),
             index=scope_codes.index(st.session_state["scope_choice"])
             if st.session_state["scope_choice"] in scope_codes else 0,
             key="set_scope", on_change=_sync_setting("scope_choice", "set_scope"),
             horizontal=True,
             help=t("set.scope.help"))

    st.divider()
    st.markdown(f'<div class="section-title">{t("set.enhance.title")}</div>', unsafe_allow_html=True)
    st.toggle(t("set.qu"),
              value=st.session_state["query_understanding"], key="set_qu",
              on_change=_sync_setting("query_understanding", "set_qu"),
              help=t("set.qu.help"))
    st.toggle(t("set.hybrid"),
              value=st.session_state["hybrid_search"], key="set_hybrid",
              on_change=_sync_setting("hybrid_search", "set_hybrid"),
              help=t("set.hybrid.help"))
    st.toggle(t("set.rerank"),
              value=st.session_state["rerank"], key="set_rerank",
              on_change=_sync_setting("rerank", "set_rerank"),
              help=t("set.rerank.help"))
    st.toggle(t("set.eval"),
              value=st.session_state["answer_eval"], key="set_eval",
              on_change=_sync_setting("answer_eval", "set_eval"),
              help=t("set.eval.help"))
    st.toggle(t("set.debug"),
              value=st.session_state["debug_mode"], key="set_debug",
              on_change=_sync_setting("debug_mode", "set_debug"))


def page_model_config():
    """模型配置页（B16.2）：当前模型信息 + 模型单价（评估卡片的费用估算用）。"""
    require_perm('retrieval_settings')
    st.markdown(f'<div class="section-title">{t("set.model.title")}</div>', unsafe_allow_html=True)
    st.caption(t("set.model.caption"))
    badge = t("set.model.local") if cfg.llm_is_local else t("set.model.cloud")
    st.markdown(t("set.model.current", model=cfg.llm_model or "-", badge=badge),
                unsafe_allow_html=True)
    if cfg.llm_is_local:
        st.caption(t("set.model.local_note"))

    # 模型单价（B16.1 模型消耗统计）：只影响评估卡片里的费用估算，不影响任何调用
    st.markdown(f'<div class="section-title">{t("set.price.title")}</div>', unsafe_allow_html=True)
    st.caption(t("set.price.caption"))
    for canonical, widget, label_key, help_key in (
        ("llm_price_input", "set_price_input", "set.price.input", "set.price.input.help"),
        ("llm_price_input_cached", "set_price_input_cached", "set.price.input_cached", None),
        ("llm_price_output", "set_price_output", "set.price.output", None),
    ):
        st.number_input(t(label_key), min_value=0.0, max_value=100000.0, step=0.1, format="%.2f",
                        value=float(st.session_state[canonical]), key=widget,
                        on_change=_sync_setting(canonical, widget),
                        help=t(help_key) if help_key else None)


# ---------------------------------------------------------------- 页面 6：维护操作
def page_maintenance():
    st.markdown(f'<div class="section-title">{t("maint.title")}</div>', unsafe_allow_html=True)
    st.caption(t("maint.caption"))

    # V3.7.2 权限拆分：知识库维护需要 maintenance 权限；
    # 缓存管理人人可用（普通员工清自己的问答缓存，管理员/维护权限清全局）
    _au = st.session_state.get("auth_user") or {}
    _can_maintain = "maintenance" in (_au.get("permissions") or [])
    _me = str(_au.get("username") or "")

    if _can_maintain:
        st.markdown(t("maint.kb"))
        confirm_rebuild = st.checkbox(t("maint.confirm"), key="confirm_rebuild")
        c1, c2 = st.columns(2)
        if c1.button(t("maint.rebuild"), disabled=not confirm_rebuild,
                     width="stretch", type="primary"):
            with st.spinner(t("maint.rebuilding")):
                try:
                    summary = ingest_files(rebuild=True,
                                           owner=str((st.session_state.get("auth_user") or {}).get("username") or ""))
                except (LLMError, VectorStoreError, EmbeddingError) as exc:
                    st.error(str(exc))
                    st.stop()
            if summary.ok_files:
                st.success(t("maint.rebuild.done", files=summary.ok_files, chunks=summary.total_chunks))
            else:
                st.error(t("maint.rebuild.fail"))
        if c2.button(t("maint.clear_only"), width="stretch"):
            try:
                store.clear()
            except VectorStoreError as exc:
                st.error(str(exc))
                st.stop()
            # 向量库清空后必须清问答缓存：否则同问题仍命中缓存返回已删知识的答案
            from src.pipeline import clear_qa_cache as _cqc
            from src.keyword_search import invalidate_cache as _ici
            _cqc()
            _ici()
            st.success(t("maint.clear_only.done"))
        st.divider()

    # V3.7.2 缓存管理：人人可用。普通员工清自己的问答缓存（按用户名归属）；
    # 有维护权限/管理员额外可见全局缓存（问答全量 + BM25 索引）清理
    st.markdown(t("maint.cache"))
    from src.pipeline import _qa_cache, _qa_cache_owner, clear_qa_cache
    from src.keyword_search import _bm25_cache, invalidate_cache

    if _can_maintain:
        cache_col1, cache_col2 = st.columns(2)
        cache_col1.metric(t("maint.cache.qa"), t("maint.cache.unit", n=len(_qa_cache)))
        cache_col2.metric(t("maint.cache.bm25"), t("maint.cache.unit.group", n=len(_bm25_cache)))
        if st.button(t("maint.cache.clear"), width="stretch", type="primary"):
            clear_qa_cache()
            invalidate_cache()
            st.success(t("maint.cache.clear.done"))
            st.rerun()
    else:
        my_n = sum(1 for owner in _qa_cache_owner.values() if owner == _me)
        st.metric(t("maint.cache.mine"), t("maint.cache.unit", n=my_n))
        if st.button(t("maint.cache.mine.clear"), width="stretch"):
            clear_qa_cache(username=_me)
            st.success(t("maint.cache.mine.clear.done"))
            st.rerun()


# ---------------------------------------------------------------- 页面 7：系统状态
def page_system_status():
    st.markdown(f'<div class="section-title">{t("statuspage.title")}</div>', unsafe_allow_html=True)
    for name, (ok, note) in system_status(current_language()).items():
        dot = "🟢" if ok else "🔴"
        st.markdown(f"{dot} **{name}**　<span style='color:#64748B'>{note}</span>",
                    unsafe_allow_html=True)
    if st.button(t("statuspage.rerun")):
        st.cache_data.clear()
        st.rerun()
    st.caption(t("statuspage.caption"))


# ---------------------------------------------------------------- 页面 8：参数总览
def page_params_overview():
    st.markdown(f'<div class="section-title">{t("params.title")}</div>', unsafe_allow_html=True)
    st.subheader(t("params.group.general"))
    # 高度 = (行数 + 表头) × 行高：按组展开，避免在表格内部滚动。
    params_rows = [
        (t("params.chunk_size"), cfg.chunk_size, t("params.chunk_size.note")),
        (t("params.chunk_overlap"), cfg.chunk_overlap, t("params.chunk_overlap.note")),
        (t("params.top_k"), st.session_state["top_k"], t("params.top_k.note")),
        (t("params.context_max_tokens"), cfg.context_max_tokens, t("params.context_max_tokens.note")),
        (t("params.llm_max_tokens"), cfg.llm_max_tokens, t("params.llm_max_tokens.note")),
        (t("params.rerank_recall_k"), cfg.rerank_recall_k, t("params.rerank_recall_k.note")),
        (t("params.temperature"), cfg.llm_temperature, t("params.temperature.note")),
        (t("params.max_per_doc"), cfg.context_max_per_doc, t("params.max_per_doc.note")),
        (t("params.embedding"), cfg.embedding_model, t("params.embedding.note")),
        (t("params.llm"), cfg.llm_model or t("params.llm.unconfigured"), t("params.llm.note")),
        (t("params.rerank_backend"), cfg.rerank_backend, t("params.rerank_backend.note")),
        (t("params.rerank_model"),
         f"{cfg.rerank_model_dir.name}（model_quantized.onnx + tokenizer.json）",
         t("params.rerank_model.note")),
        (t("params.collection"), cfg.qdrant_collection, t("params.collection.note")),
    ]
    def _show_parameters(rows):
        st.dataframe(
            [{t("params.col.name"): name,
              t("params.col.value"): str(value).lower() if isinstance(value, bool) else str(value),
              t("params.col.note"): note} for name, value, note in rows],
            height=(len(rows) + 1) * 35 + 3,
            width="stretch", hide_index=True,
        )

    _show_parameters(params_rows)
    st.subheader(t("params.group.enumeration"))
    enumeration_values = [
        ("ENUM_RECALL_K", ENUM_RECALL_K),
        ("ENUM_CTX_ITEMS", ENUM_CTX_ITEMS),
        ("ENUM_CTX_TOKENS", ENUM_CTX_TOKENS),
        ("ENUM_RERANK_DOCS", cfg.enum_rerank_docs),
        ("ENUM_EXPAND_DOCS", cfg.enum_expand_docs),
        ("ENUM_DOC_CARDS", cfg.enum_doc_cards),
        ("ENUM_CATALOG_DOCS", cfg.enum_catalog_docs),
        ("ENUM_CATALOG_POINTS", cfg.enum_catalog_points),
    ]
    _show_parameters([(f"{t('params.' + key + '.label')} {key}", value, t("params." + key + ".note"))
                      for key, value in enumeration_values])
    st.caption(t("params.enumeration.caption"))

    st.subheader(t("params.group.processing"))
    processing_keys = (
        "ENUM_STAGE2_ENABLED", "ENUM_VERIFY_ENABLED", "ENUM_MAX_ROUNDS", "ENUM_QUERIES_PER_ROUND",
        "ENUM_NO_NEW_ROUNDS", "ENUM_MAX_CALLS", "ENUM_MAX_SECONDS", "ENUM_MAX_TOKENS",
        "ENUM_BATCH_TOKENS", "ENUM_OUTPUT_TOKENS", "ENUM_PAGE_CARDS", "ENUM_MAX_CARDS",
        "ENUM_MAX_DOCUMENTS", "ENUM_MAX_ITEMS",
    )
    _show_parameters([(f"{t('params.' + key + '.label')} {key}", getattr(cfg, key.lower()),
                       t("params." + key + ".note")) for key in processing_keys])
    st.caption(t("params.processing.caption"))
    st.subheader(t("params.group.conversation"))
    conversation_keys = ("CHAT_CONTEXT_MAX_TOKENS", "CHAT_RECENT_TURNS", "CHAT_SUMMARY_TOKENS",
                         "CHAT_SUMMARY_TRIGGER_TOKENS", "CHAT_RECALL_MESSAGES")
    _show_parameters([(f"{t('params.' + key + '.label')} {key}", getattr(cfg, key.lower()),
                       t("params." + key + ".note")) for key in conversation_keys])
    st.caption(t("params.conversation.caption"))
    st.caption(t("params.caption"))


# ---------------------------------------------------------------- 布局组装：左侧菜单 + 右侧页面
# 品牌区（Logo + Slogan）通过 CSS order 固定在导航上方；
# 系统状态通过 order + margin-top:auto 固定在侧边栏最底部。
# Cookie 恢复必须在侧边栏渲染之前，否则恢复当帧的用户信息卡会缺失
try_restore_session()

with st.sidebar:
    with st.container(key="sidebar_brand"):
        st.markdown(
            f"""
            <div class="brand-block">
              <div style="display:flex;align-items:center;gap:10px;">
                <div style="width:42px;height:42px;border-radius:12px;flex:none;
                            background:linear-gradient(135deg,#2563EB,#4F46E5);
                            display:flex;align-items:center;justify-content:center;
                            font-size:1.35rem;box-shadow:0 4px 14px rgba(37,99,235,.35);">🧠</div>
                <div>
                  <div class="brand-name">Sky Personal RAG</div>
                  <div class="brand-slogan">{t("brand.slogan")}</div>
                </div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    # V3.7.2 用户信息卡：紧跟 Logo 下方一行（姓名/部门 + @账户 + 职责备注）
    _top_user = st.session_state.get("auth_user") or {}
    if _top_user:
        _display_name = str(_top_user.get("real_name") or _top_user.get("username") or "")
        _dep_label = str(_top_user.get("department") or "")
        _role_label = ""
        _avatar_char = html.escape((_display_name or "·")[:1])
        _note_line = (f'<div class="u-note">{html.escape(str(_top_user.get("note") or ""))}</div>'
                      if _top_user.get("note") else "")
        with st.container(key="sidebar_userinfo"):
            st.markdown(
                f'<div class="user-card">'
                f'<div class="avatar">{_avatar_char}</div>'
                f'<div style="min-width:0;">'
                f'<div class="u-name">{html.escape(_display_name)}'
                f'<span class="u-dept">{html.escape(str(_top_user.get("department") or ""))}</span>'
                f'{_role_label}</div>'
                f'<div class="u-sub">@{html.escape(str(_top_user.get("username") or ""))}</div>'
                f'{_note_line}'
                f'</div></div>',
                unsafe_allow_html=True,
            )

# ---------------------------------------------------------------- 页面 9：插件管理
def page_plugins():
    st.markdown(f'<div class="section-title">{t("plugin.title")}</div>', unsafe_allow_html=True)
    # V3.7.2 插件管理权限：人人可查看；安装/卸载需要 plugins 权限
    _pu = st.session_state.get("auth_user") or {}
    can_manage_plugins = "plugins" in (_pu.get("permissions") or [])
    st.caption(t("plugin.caption") if can_manage_plugins else t("plugin.caption.viewonly"))

    installed = get_installed_plugins()

    for pid, info in PLUGIN_REGISTRY.items():
        is_on = pid in installed
        with st.expander(f"{info['name']}　{t('plugin.installed') if is_on else t('plugin.not_installed')}"):
            st.markdown(t("plugin.desc", v=info["description"]))
            st.markdown(t("plugin.version", v=info["version"], m=info["module"]))

            c1, c2 = st.columns(2)
            if is_on:
                if c2.button(t("plugin.uninstall"), key=f"uninstall_{pid}", width="stretch",
                             disabled=not can_manage_plugins):
                    uninstall_plugin(pid)
                    st.rerun()
                if c1.button(t("plugin.installed"), disabled=True, width="stretch", key=f"inst_{pid}"):
                    pass
            else:
                if c1.button(t("plugin.install"), key=f"install_{pid}", width="stretch",
                             type="primary", disabled=not can_manage_plugins):
                    install_plugin(pid)
                    st.rerun()
                if c2.button(t("plugin.installed"), disabled=True, width="stretch", key=f"inst_{pid}"):
                    pass

    st.divider()
    st.caption(t("plugin.footer"))


# ---------------------------------------------------------------- 页面：领域管理（V3.8）
@st.dialog(t("dommgr.delete.confirm.title"), width="small")
@_guard_management_database
def confirm_domain_delete() -> None:
    code = st.session_state.get("domain_delete_request")
    if not code:
        return
    st.write(t("dommgr.delete.confirm.msg", code=code))
    c1, c2 = st.columns(2)
    if c1.button(t("common.cancel"), width="stretch"):
        st.session_state.pop("domain_delete_request", None)
        st.rerun()
    if c2.button(t("dommgr.delete.confirm.yes"), type="primary", width="stretch"):
        # 点确认时重新统计：确认框打开期间若有人往该领域上传了文件，同样拒绝删除
        try:
            stat = domain_reg.usage_by_domain(cfg.knowledge_dir, istate.load_state(),
                                              store.list_documents()).get(code)
            domain_reg.delete_domain(code, stat, cfg.knowledge_dir)
        except domain_reg.DomainError as exc:
            st.error(str(exc))
            return
        st.session_state.pop("domain_delete_request", None)
        st.toast(t("dommgr.delete.ok", code=code), icon="✅")
        st.rerun()


@_guard_management_database
def page_domains():
    """领域管理：增 / 改名称与描述 / 启停 / 删除（只能删空领域）/ 登记未登记目录。

    铁律：编号是数据身份，创建后不可改；名称只用于显示——改名不碰文件、卡片、向量与过滤条件。
    """
    require_perm("domains")
    st.markdown(f'<div class="section-title">{t("dommgr.title")}</div>', unsafe_allow_html=True)
    st.caption(t("dommgr.caption"))

    documents = store.list_documents()
    usage = domain_reg.usage_by_domain(cfg.knowledge_dir, istate.load_state(), documents)

    # ① 新增领域
    with st.expander(t("dommgr.add.title")):
        with st.form("domain_add_form", clear_on_submit=True):
            c1, c2 = st.columns(2)
            new_code = c1.text_input(t("dommgr.field.code"), help=t("dommgr.field.code.help"))
            new_name = c2.text_input(t("dommgr.field.name"))
            new_desc = st.text_input(t("dommgr.field.desc"))
            if st.form_submit_button(t("dommgr.add.btn"), type="primary", width="stretch"):
                try:
                    created = domain_reg.create_domain(new_code, new_name, new_desc, store=store)
                except domain_reg.DomainError as exc:
                    _show_management_error(exc)
                else:
                    st.toast(t("dommgr.add.ok", code=created["code"]), icon="✅")
                    st.rerun()

    # ② 领域列表
    for d in domain_reg.load_domains():
        code = d["code"]
        stat = usage.get(code) or {}
        reserved = code in domain_reg.RESERVED_CODES
        enabled = d.get("enabled", True)
        header = (f"{domain_label(code)}　"
                  f"{t('dommgr.status.on') if enabled else t('dommgr.status.off')}"
                  f"{'　' + t('dommgr.badge.system') if reserved else ''}　"
                  f"{t('dommgr.stat.short', files=stat.get('files', 0), cards=stat.get('cards', 0))}")
        with st.expander(header):
            if d.get("description"):
                st.caption(d["description"])
            st.caption(t("dommgr.stat.detail", files=stat.get("files", 0),
                         archived=stat.get("archived", 0), documents=stat.get("documents", 0),
                         cards=stat.get("cards", 0)))
            with st.form(f"domain_edit_{code}"):
                name_val = st.text_input(
                    t("dommgr.field.name"), value=d.get("name", ""),
                    placeholder=domain_label_i18n(code, with_code=False) if d.get("builtin") else "",
                    help=t("dommgr.field.name.builtin_help") if d.get("builtin") else None)
                desc_val = st.text_input(t("dommgr.field.desc"), value=d.get("description", ""))
                if st.form_submit_button(t("dommgr.edit.btn"), width="stretch"):
                    try:
                        domain_reg.update_domain(code, name=name_val, description=desc_val)
                    except domain_reg.DomainError as exc:
                        st.error(str(exc))
                    else:
                        st.toast(t("dommgr.edit.ok"), icon="✅")
                        st.rerun()
            col_toggle, col_delete = st.columns(2)
            if enabled:
                if col_toggle.button(t("dommgr.disable.btn"), key=f"dom_off_{code}", width="stretch",
                                     disabled=reserved,
                                     help=t("dommgr.reserved.help") if reserved else t("dommgr.disable.help")):
                    domain_reg.update_domain(code, enabled=False)
                    st.rerun()
            elif col_toggle.button(t("dommgr.enable.btn"), key=f"dom_on_{code}", width="stretch"):
                domain_reg.update_domain(code, enabled=True)
                st.rerun()
            blocked = reserved or domain_reg.has_data(stat)
            if col_delete.button(t("dommgr.delete.btn"), key=f"dom_del_{code}", width="stretch",
                                 disabled=blocked,
                                 help=(t("dommgr.reserved.help") if reserved
                                       else t("dommgr.delete.has_data") if blocked else None)):
                st.session_state["domain_delete_request"] = code
                st.rerun()

    # ③ 未登记目录：有文件却没登记为领域的文件夹（入库时被兜底归到 reference）
    unregistered = domain_reg.unregistered_dirs(cfg.knowledge_dir)
    if unregistered:
        st.divider()
        st.markdown(t("dommgr.unreg.title"))
        st.caption(t("dommgr.unreg.caption"))
        for u in unregistered:
            c1, c2 = st.columns([3, 1])
            c1.markdown(t("dommgr.unreg.row", folder=u["folder"], files=u["files"]))
            if c2.button(t("dommgr.unreg.btn"), key=f"dom_reg_{u['folder']}", width="stretch",
                         disabled=not u["valid_code"],
                         help=None if u["valid_code"] else t("dommgr.unreg.invalid")):
                try:
                    created = domain_reg.register_directory(u["folder"], u["folder"], store)
                except domain_reg.DomainError as exc:
                    _show_management_error(exc)
                else:
                    st.toast(t("dommgr.unreg.ok", code=created["code"]), icon="✅")
                    st.rerun()

    if st.session_state.get("domain_delete_request"):
        confirm_domain_delete()


# ---------------------------------------------------------------- 页面 10：学习笔记
def page_learning():
    st.markdown(f'<div class="section-title">{t("learn.title")}</div>', unsafe_allow_html=True)
    st.caption(t("learn.caption"))
    md_path = Path(__file__).parent / "realize.md"
    if md_path.exists():
        st.markdown(md_path.read_text(encoding="utf-8"))
    else:
        st.info(t("learn.missing"))


# ── V3.7 登录门：未登录只渲染登录/注册页，到此为止 ──
auth_gate()

# ── V3.8 知识库服务在登录之后初始化：Qdrant / Ollama 不可用时登录页照常可用，
# 登录后给出友好提示；完整异常只写服务端日志，不把报错栈与服务器路径暴露给浏览器 ──
try:
    services = get_services()
    store = services["store"]
except Exception as _svc_exc:  # VectorStoreError / 连接异常等
    print(f"⚠️ 知识库服务初始化失败：{type(_svc_exc).__name__}: {_svc_exc}")
    st.error(t("services.unavailable"))
    st.stop()

_auth_user = st.session_state.get("auth_user") or {}
# V3.7.2 员工前台权限（管理后台分配，默认无）：检索设置/知识库维护/插件管理需权限；
# 通用设置/系统状态/参数总览/学习人人固有。
# V3.8：管理员已与员工分表且禁止登录前台——前台导航只有员工一种形态。
_user_perms = set(_auth_user.get("permissions") or [])

chat_page = st.Page(page_chat, title=t("nav.chat"), icon="💬", url_path="chat", default=True)
upload_page = st.Page(page_upload, title=t("nav.upload"), icon="📤", url_path="upload")
manage_page = st.Page(page_manage, title=t("nav.manage"), icon="🗂", url_path="manage")
domains_page = st.Page(page_domains, title=t("nav.domains"), icon="🧭", url_path="domains")
general_page = st.Page(page_general_settings, title=t("nav.general"), icon="⚙️", url_path="settings-general")
retrieval_page = st.Page(page_retrieval_settings, title=t("nav.retrieval"), icon="🔍", url_path="settings-retrieval")
model_page = st.Page(page_model_config, title=t("nav.model"), icon="🤖", url_path="settings-model")
maintenance_page = st.Page(page_maintenance, title=t("nav.maintenance"), icon="🧹", url_path="settings-maintenance")
status_page = st.Page(page_system_status, title=t("nav.status"), icon="📊", url_path="settings-status")
params_page = st.Page(page_params_overview, title=t("nav.params"), icon="🧾", url_path="settings-params")
plugins_page = st.Page(page_plugins, title=t("nav.plugins"), icon="🔌", url_path="plugins")
learning_page = st.Page(page_learning, title=t("nav.learning"), icon="📖", url_path="learn")

# 员工：问答固有；其余页面按管理后台分配的前台权限出现。
# 人人固有：通用设置 / 系统状态 / 参数总览 / 学习 / 插件管理（只读）/
#           维护操作页（其中"缓存管理"是个人缓存）；权限只管检索设置/知识库维护/上传/知识管理
employee_nav: dict = {t("nav.group.knowledge"): [chat_page]}
if "upload" in _user_perms:
    employee_nav[t("nav.group.knowledge")].append(upload_page)
if "manage" in _user_perms:
    employee_nav[t("nav.group.knowledge")].append(manage_page)
if "domains" in _user_perms:  # V3.8 领域管理：独立权限（管理后台分配）
    employee_nav[t("nav.group.knowledge")].append(domains_page)
settings_pages = [general_page]
if "retrieval_settings" in _user_perms:
    settings_pages.append(retrieval_page)
    settings_pages.append(model_page)
# 维护操作页人人可达（页内"知识库维护"区块按权限显隐；缓存管理是个人功能）
settings_pages.append(maintenance_page)
settings_pages.extend([status_page, params_page])
employee_nav[t("nav.group.settings")] = settings_pages
employee_nav[t("nav.group.learn")] = [learning_page]
employee_nav[t("nav.group.plugins")] = [plugins_page]
pg = st.navigation(employee_nav)

pg.run()

with st.sidebar:
    with st.container(key="sidebar_footer"):
        _sidebar_user = st.session_state.get("auth_user") or {}
        sessions = list_sessions(10,
                                 username=str(_sidebar_user.get("username") or "") or None)
        keep_history_expanded = st.session_state.pop("keep_history_expanded_once", False)
        with st.expander(
            t("sidebar.history", n=len(sessions)),
            expanded=keep_history_expanded,
        ):
            if sessions:
                for s in sessions:
                    with st.container(key=f"session_item_{s['session_id']}"):
                        title = s["title"].strip() or t("sidebar.untitled")
                        # 当前正在聊的会话加标记，方便区分"继续聊"和"翻旧会话"
                        is_current = s["session_id"] == st.session_state.get("session_id")
                        label = ("▶ " + title[:22]) if is_current else title[:24]
                        with st.container(key=f"hs_{s['session_id']}"):
                            st.page_link(
                                chat_page,
                                label=label,
                                query_params={"load": s["session_id"]},
                                width="stretch",
                            )
                        def _request_del_cb(sid=s["session_id"], session_title=title):
                            st.session_state["delete_session_request"] = {
                                "session_id": sid,
                                "title": session_title,
                            }
                        st.button(
                            t("sidebar.delete"),
                            key=f"del_{s['session_id']}",
                            on_click=_request_del_cb,
                            help=t("sidebar.delete.help", title=title),
                            icon=":material/delete:",
                            type="tertiary",
                        )
                        updated = s["updated_at"]
                        when = updated[5:16] if len(updated) >= 16 else updated
                        st.caption(f"{when}　·　" + t("sidebar.msg_count", n=s["count"]))
            else:
                st.markdown(
                    '<div style="text-align:center;font-size:.72rem;color:var(--text-muted);padding:18px 6px;line-height:1.7;">'
                    f'{t("sidebar.empty")}</div>', unsafe_allow_html=True)

        ok_all = all(ok for ok, _ in system_status(current_language()).values())
        status_emoji = "🟢" if ok_all else "🟡"
        status_text = t("sidebar.status.ok") if ok_all else t("sidebar.status.pending")
        status_detail = " · ".join(
            f"{name}{'✓' if ok else '!'}" for name, (ok, _) in system_status(current_language()).items()
        )
        st.markdown(
            f"""
            <div class="sidebar-status">
              <div class="sidebar-status-row">
                <strong>{status_emoji} {t("sidebar.status.title")}</strong><span>{status_text}</span>
              </div>
              <div class="sidebar-status-note">{status_detail}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # V3.7.2 登出按钮：独立容器吸附侧边栏最底部（CSS margin-top:auto），
    # 点击后弹确认框（confirm_logout），确认才真正退出
    if st.session_state.get("auth_user"):
        with st.container(key="sidebar_logout"):
            if st.button(t("auth.logout"), key="auth_logout_btn",
                         width="stretch", icon=":material/logout:"):
                st.session_state["logout_request"] = True
                st.rerun()

if st.session_state.get("delete_session_request"):
    confirm_session_delete()
elif st.session_state.get("manage_remove_request"):
    confirm_manage_remove()
elif st.session_state.get("logout_request"):
    confirm_logout()

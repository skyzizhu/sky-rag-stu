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
import re
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

from src.app_settings import SETTINGS_KEYS, load_app_settings, save_app_settings  # noqa: E402
from src.answer_format import format_answer_markdown  # noqa: E402
from src.config import get_config  # noqa: E402
from src.chat_scroll import scroll_chat_to_latest  # noqa: E402
from src.directory_uploader import (  # noqa: E402
    decode_directory_batch,
    decode_native_directory_uploads,
    directory_drop_uploader,
)
from src.embedding import EmbeddingClient, EmbeddingError  # noqa: E402
from src.llm import LLMError  # noqa: E402
from src.manage import (  # noqa: E402
    ManageError,
    archive as archive_file,
    reingest as reingest_file,
    remove_documents,
    restore as restore_file,
)
from src.metadata import DOMAINS, DOMAIN_LABELS  # noqa: E402
from src.parser import SUPPORTED_EXTENSIONS  # noqa: E402
from src.pipeline import QAResult, answer_question, answer_stream, ingest_files  # noqa: E402
from src.sessions import SESSIONS_DIR, list_sessions, load_session, new_session_id, save_session  # noqa: E402
from src.vector_store import VectorStoreError, get_vector_store  # noqa: E402

st.set_page_config(page_title="Sky Personal RAG", page_icon="🧠", layout="wide")
cfg = get_config()

SUPPORTED_UPLOAD_TYPES = [ext.lstrip(".") for ext in sorted(SUPPORTED_EXTENSIONS) if ext != ".htm"]

# 跨页面共享的设置项：在「设置与状态」里改，问答页即时生效
st.session_state.setdefault("top_k", cfg.top_k)
st.session_state.setdefault("domain_choice", "全部")
st.session_state.setdefault("scope_choice", "仅 active")
st.session_state.setdefault("debug_mode", True)
st.session_state.setdefault("query_understanding", cfg.query_understanding)
st.session_state.setdefault("session_id", new_session_id())
st.session_state.setdefault("theme", "system")  # system / light / dark


@st.dialog("确认删除历史会话", width="small")
def confirm_session_delete() -> None:
    """在真正删除前要求用户二次确认。"""
    request = st.session_state.get("delete_session_request")
    if not request:
        return

    st.write(f"确定要删除历史会话「{request['title']}」吗？")
    st.caption("删除后无法恢复，但不会影响已经导入的知识库文件。")
    cancel_col, confirm_col = st.columns(2)
    if cancel_col.button("取消", use_container_width=True):
        st.session_state.pop("delete_session_request", None)
        st.rerun()
    if confirm_col.button(
        "确认删除",
        type="primary",
        icon=":material/delete:",
        use_container_width=True,
    ):
        sid = request["session_id"]
        session_file = SESSIONS_DIR / sid[:10] / f"{sid}.json"
        if session_file.exists():
            session_file.unlink()
        # 删除后会话数量变化会让 Expander 以新组件重建；下一次渲染强制保持展开。
        st.session_state["keep_history_expanded_once"] = True
        st.session_state.pop("delete_session_request", None)
        st.rerun()


@st.dialog("确认移除知识文件", width="small")
def confirm_manage_remove() -> None:
    """确认批量移除向量数据，以及可选的磁盘文件删除。"""
    request = st.session_state.get("manage_remove_request")
    if not request:
        return

    count = len(request["paths"])
    delete_files = request["delete_files"]
    if delete_files:
        st.error(f"将从向量知识库移除 {count} 个文件，并永久删除对应磁盘文件。")
        st.caption("删除后无法恢复；如果所属二级目录中已没有其他文件，该目录也会被清理。")
        confirm_label = "确认并删除文件"
    else:
        st.info(f"将从向量知识库移除 {count} 个文件，磁盘文件会保留。")
        st.caption("之后可通过“重新入库”恢复这些知识。")
        confirm_label = "确认移除知识"

    with st.expander(f"查看将处理的 {count} 个文件"):
        for name in request["names"]:
            st.write(f"• {name}")

    cancel_col, confirm_col = st.columns(2)
    if cancel_col.button("取消", width="stretch", key="manage_remove_cancel"):
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
        except (ManageError, VectorStoreError) as exc:
            st.error(str(exc))
            return
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
except Exception:
    pass
if load_target:
    data = load_session(load_target)
    if data:
        loaded_messages = []
        for m in data.get("messages", []):
            entry = {"role": m["role"], "content": m["content"]}
            res = m.get("result")
            if res:
                res["retrieved"] = []  # 历史消息不再需要原始召回对象列表
                entry["result"] = QAResult(**{
                    k: v for k, v in res.items()
                    if k in {f.name for f in QAResult.__dataclass_fields__.values()}
                })
            loaded_messages.append(entry)
        st.session_state.messages = loaded_messages
        st.session_state.session_id = load_target
        st.session_state["scroll_loaded_session_once"] = True
st.session_state.setdefault("hybrid_search", cfg.hybrid_search)
st.session_state.setdefault("rerank", cfg.rerank_enabled)

# 用户保存过的检索设置优先于 .env 默认值（每个浏览器会话只加载一次）
if "app_settings_loaded" not in st.session_state:
    for key, value in load_app_settings().items():
        if key in st.session_state:
            st.session_state[key] = value
    st.session_state["app_settings_loaded"] = True

# ---------------------------------------------------------------- 全局样式
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
[data-testid="stSidebarContent"] {display:flex !important;flex-direction:column !important;position:relative !important;}
[data-testid="stSidebarHeader"] {position:absolute !important;top:18px !important;right:9px !important;
  z-index:20 !important;width:auto !important;height:auto !important;padding:0 !important;}
[data-testid="stSidebarUserContent"] {display:contents !important;}
[data-testid="stSidebarUserContent"] > *,
[data-testid="stSidebarUserContent"] > * > [data-testid="stVerticalBlock"] {display:contents !important;}
[data-testid="stSidebarUserContent"] [data-testid="stLayoutWrapper"]:has(.st-key-sidebar_brand) {order:1 !important;}
[data-testid="stSidebarNav"] {order:2 !important;}
[data-testid="stSidebarUserContent"] [data-testid="stLayoutWrapper"]:has(.st-key-sidebar_footer) {order:3 !important;}
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
body:has([data-testid="stSidebar"][aria-expanded="true"]) .st-key-chat_composer {
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
pre.dbg {font-size: .72rem !important; line-height: 1.6; white-space: pre-wrap;
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
  display:flex !important;gap:0 !important;font-size:.64rem !important;font-weight:500 !important;line-height:1.35 !important;
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
  color:var(--text-secondary) !important;font-size:.64rem !important;font-weight:500 !important;
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
  color:var(--text-muted) !important;font-size:.62rem !important;line-height:1.2 !important;
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

[data-testid="stFileUploaderDropzone"] button {
  font-family: 'Inter', sans-serif !important;}
@media (max-width: 760px) {
  .hero {padding:15px 17px;border-radius:15px;}
  .hero h1 {font-size:1.1rem;}
  .block-container {padding-top:1rem !important;}
  body:has([data-testid="stSidebar"][aria-expanded="true"]) .st-key-chat_composer,
  .st-key-chat_composer {left:50% !important;width:94vw !important;}
}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

# 主题覆盖：非"跟随系统"时强制对应主题
_theme = st.session_state.get("theme", "system")
if _theme == "dark":
    st.markdown("<style>:root{--bg-primary:#0B0F1A!important;--bg-secondary:#111827!important;"
                "--bg-sidebar:linear-gradient(180deg,#0F1523,#0B1018)!important;"
                "--text-primary:#E2E8F0!important;--text-secondary:#94A3B8!important;"
                "--text-muted:#475569!important;--border-subtle:rgba(148,163,184,.08)!important;"
                "--accent:#818CF8!important;--accent-text:#A5B4FC!important;}"
                ".stApp{background:#0B0F1A!important;color:#E2E8F0!important;}"
                "[data-testid='stSidebar']{background:linear-gradient(180deg,#0F1523,#0B1018)!important;}"
                "[data-testid='stChatInput']{background:#111827!important;}"
                "[data-testid='stChatInput'] textarea{color:#E2E8F0!important;}"
                ".stTextInput input,.stTextArea textarea{background:rgba(148,163,184,.03)!important;"
                "color:#E2E8F0!important;border-color:rgba(148,163,184,.1)!important;}"
                "</style>", unsafe_allow_html=True)
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


services = get_services()
store = services["store"]


def _sync_setting(canonical_key: str, widget_key: str):
    """控件变化时：同步到规范 session 键（供其他页面读取）+ 持久化到配置文件。"""
    def _sync():
        st.session_state[canonical_key] = st.session_state[widget_key]
        save_app_settings({k: st.session_state[k] for k in SETTINGS_KEYS})
    return _sync


def domain_label(domain: str) -> str:
    label = DOMAIN_LABELS.get(domain, "")
    return f"{domain} · {label}" if label else domain


def _clean_directory_name(name: str) -> str:
    """生成稳定、可读且不会越出目标目录的顶层文件夹名。"""
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", name).strip(" .-")
    if not cleaned:
        raise ValueError(f"目录名称无效：{name}")
    return cleaned


def _available_directory(parent: Path, preferred_name: str) -> Path:
    """已有同名目录时创建副本目录，避免静默覆盖用户知识文件。"""
    candidate = parent / preferred_name
    if not candidate.exists():
        return candidate
    index = 2
    while (parent / f"{preferred_name}-{index}").exists():
        index += 1
    return parent / f"{preferred_name}-{index}"


@st.cache_data(ttl=5)
def system_status() -> dict[str, tuple[bool, str]]:
    status: dict[str, tuple[bool, str]] = {}
    try:
        ok = services["embedding"].model_available()
        status["向量化模型"] = (ok, cfg.embedding_model if ok else f"缺少 {cfg.embedding_model}")
    except Exception as exc:
        status["向量化模型"] = (False, str(exc)[:40])
    try:
        count = store.count()
        status["向量数据库"] = (True, f"{count} 张卡片")
    except Exception as exc:
        status["向量数据库"] = (False, str(exc)[:40])
    is_local_llm = "localhost" in cfg.llm_base_url or "127.0.0.1" in cfg.llm_base_url
    llm_ok = bool(cfg.llm_api_key) or is_local_llm
    llm_note = cfg.llm_model if llm_ok else "未配置"
    if llm_ok and is_local_llm:
        llm_note = f"🖥 {cfg.llm_model}（本地 Ollama）"
    elif llm_ok:
        llm_note = f"☁️ {cfg.llm_model}（云端）"
    status["大模型"] = (llm_ok, llm_note)
    return status


def status_pills() -> str:
    pills = []
    for name, (ok, note) in system_status().items():
        pills.append(f'<span class="pill">{"✅" if ok else "❌"} {name} · {note}</span>')
    return "".join(pills)


def tag(text: str, color: str = "blue") -> str:
    return f'<span class="tag {color}">{text}</span>'


def build_filters() -> dict:
    """把设置页的检索设置翻译成 Metadata Filter。"""
    filters: dict = {}
    if not st.session_state["domain_choice"].startswith("全部"):
        filters["domain"] = st.session_state["domain_choice"].split(" ")[0]
    scope = st.session_state["scope_choice"]
    if scope == "包含归档":
        filters["status"] = "all"
    elif scope == "仅归档":
        filters["status"] = "archive"
    return filters


def show_answer_sources(result) -> None:
    """卡片式参考来源 + 调试面板。"""
    with st.expander(f"📚 参考来源（{len(result.sources)} 条）", expanded=False):
        if not result.sources:
            st.write("没有召回任何知识片段。")
        for item in result.sources:
            head = f"**[{item['rank']}] {item['source']}**　相关度 `{item['score']:.3f}`"
            extras = []
            if item.get("section"):
                extras.append(f"章节：{item['section']}")
            if item.get("page"):
                extras.append(f"页码：{item['page']}")
            if extras:
                head += "　·　" + "　".join(extras)
            st.markdown(head)
            tags = [tag(f"🗂 {item.get('domain')}", "blue"), tag(f"📁 {item.get('category')}", "blue")]
            if item.get("topic"):
                tags.append(tag("🏷 " + " / ".join(item["topic"])))
            tags.append(tag(f"v{item.get('version')}", "green" if item.get("status") == "active" else "amber"))
            tags.append(tag("✅ active" if item.get("status") == "active" else "🗄 archive",
                            "green" if item.get("status") == "active" else "amber"))
            st.markdown("".join(tags), unsafe_allow_html=True)
            st.markdown(f"<div class='chunk-quote'>{item['text'][:400]}"
                        f"{'……' if len(item['text']) > 400 else ''}</div>", unsafe_allow_html=True)
            st.write("")

        # V3.5 一键复制
        st.code("【回答】\n" + (result.answer or ""), language=None)
        st.code("【来源清单】\n" + "\n".join(
            f"[{s['rank']}] {s['source']}（相关度 {s['score']:.3f}）"
            + (f" 章节: {s['section']}" if s.get("section") else "")
            for s in result.sources), language=None)

    if st.session_state.get("debug_mode"):
        with st.expander("🛠 调试信息 · RAG 节点时间线（每个节点在什么时候做了什么）"):
            st.markdown('<span class="debug-marker"></span>', unsafe_allow_html=True)
            st.caption("按执行顺序展示一次问答经过的每个节点：发生时间、耗时、做了什么、输入与输出。"
                       "标注「直通」的节点是完整 RAG 有、但当前版本未启用的环节。")
            with st.expander("📚 学习提示：两条流水线的关系"):
                st.markdown(
                    "上面展示的是**问答流水线**（提问 → … → 后处理），每次提问都会走一遍。\n\n"
                    "另一条是**入库流水线**：解析 → 清洗 → 切片 → 向量化 → 入库，"
                    "只在上传文档或运行 `python ingest.py` 时执行（V3 起为增量式："
                    "内容没变的文件自动跳过，LLM 自动补全主题/标签）。\n\n"
                    "问答时检索到的知识卡片，就是入库流水线在当初切好、存好的。"
                    "两条流水线在「向量数据库」汇合：入库负责存，问答负责查。")
            for idx, node in enumerate(result.trace, start=1):
                head = f"{node['icon']} 节点 {idx}｜{node['name']}　·　{node['time']}"
                if node.get("elapsed"):
                    head += f"　·　耗时 {node['elapsed']:.2f}s"
                with st.expander(head):
                    if node.get("status") != "已执行":
                        st.info(f"节点状态：{node['status']}")
                    st.markdown(f"**做了什么**　{node['summary']}")
                    for label, value in node.get("items", []):
                        text = str(value)
                        st.markdown(f'<div class="dbg-label">{label}</div>', unsafe_allow_html=True)
                        if text:
                            shown = text if len(text) <= 6000 else text[:6000] + "……"
                            st.markdown(f'<pre class="dbg">{shown}</pre>', unsafe_allow_html=True)
                        else:
                            st.caption("（空）")
            if result.sources:
                st.markdown("**📎 召回 Chunk 逐条明细表**")
                st.dataframe(
                    [{"排名": s["rank"], "分数": round(s["score"], 4), "通道": s.get("channels", "向量"),
                      "Source": s["source"],
                      "Domain": s.get("domain"), "Category": s.get("category"),
                      "Topic": ", ".join(s.get("topic") or []), "章节": s.get("section") or "",
                      "页码": s.get("page") or "", "Version": s.get("version"), "Status": s.get("status")}
                     for s in result.sources],
                    width="stretch", hide_index=True,
                )


# ---------------------------------------------------------------- 页面 1：知识库问答
def page_chat():
    st.markdown(
        f"""
        <div class="hero">
          <h1>🧠 问点什么，让知识库替你记得</h1>
          <p>基于你自己的文档回答，每条答案都标注出处 · 归档知识默认不参与</p>
          {status_pills()}
        </div>
        """,
        unsafe_allow_html=True,
    )

    if "messages" not in st.session_state:
        st.session_state.messages = []

    if st.session_state.messages:
        c1, c2 = st.columns([4, 1])
        with c2:
            def _new_session_cb():
                st.session_state["session_id"] = new_session_id()
                st.session_state.messages = []
            if c2.button(
                "开启新会话",
                width="stretch",
                type="primary",
                icon=":material/add_comment:",
                key="new_chat_session",
                on_click=_new_session_cb,
            ):
                pass

    for message in st.session_state.messages:
        with st.chat_message(message["role"], avatar="🙋" if message["role"] == "user" else "🧠"):
            if message["role"] == "user":
                st.markdown(f'<div class="user-bubble">{html.escape(message["content"])}</div>',
                            unsafe_allow_html=True)
            else:
                st.markdown(format_answer_markdown(message["content"]))
                if message.get("result") is not None:
                    show_answer_sources(message["result"])

    if st.session_state.pop("scroll_loaded_session_once", False):
        scroll_chat_to_latest(
            key=f"scroll_loaded_{st.session_state['session_id']}",
            behavior="auto",
        )

    def _process_question(question: str) -> None:
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user", avatar="🙋"):
            st.markdown(f'<div class="user-bubble">{html.escape(question)}</div>',
                        unsafe_allow_html=True)
        scroll_chat_to_latest(
            key=f"scroll_question_{st.session_state['session_id']}_{len(st.session_state.messages)}",
        )

        with st.chat_message("assistant", avatar="🧠"):
            if not cfg.llm_api_key:
                st.error("还没有配置 LLM API Key：请到「设置 → 系统状态」查看指引，填好 .env 后刷新页面。")
                st.session_state.messages.append({"role": "assistant", "content": "（未配置 LLM API Key）"})
                return
            try:
                progress_ph = st.empty()  # 实时进度占位符
                def _show_progress(msg):
                    progress_ph.markdown(f"⏳ {msg}")
                if True:  # 保持缩进层级
                    # 传入历史消息实现多轮对话（Query 理解解析代词 + LLM 上下文连贯）
                    chat_history = [
                        {"role": m["role"], "content": m["content"]}
                        for m in st.session_state.messages
                        if m.get("role") in ("user", "assistant") and m.get("content")
                    ][-6:]  # 最近 3 轮
                    result, deltas = answer_stream(
                        question,
                        top_k=st.session_state["top_k"],
                        filters=build_filters(),
                        use_query_understanding=st.session_state["query_understanding"],
                        use_hybrid=st.session_state["hybrid_search"],
                        use_rerank=st.session_state["rerank"],
                        history=chat_history,
                        on_progress=_show_progress,
                    )
                progress_ph.empty()
                answer_ph = st.empty()
                full_text = ""
                for delta in deltas:
                    full_text += delta
                    answer_ph.markdown(format_answer_markdown(full_text) + " ▌")
                formatted_answer = format_answer_markdown(result.answer or full_text or "")
                answer_ph.markdown(formatted_answer)
                progress_ph.markdown("✅ 回答生成完成")
                result.answer = formatted_answer or None
                show_answer_sources(result)
                st.session_state.messages.append(
                    {"role": "assistant", "content": formatted_answer, "result": result}
                )
                save_session(st.session_state["session_id"], st.session_state.messages)
                scroll_chat_to_latest(
                    key=f"scroll_answer_{st.session_state['session_id']}_{len(st.session_state.messages)}",
                )
            except (LLMError, VectorStoreError, EmbeddingError) as exc:
                st.error(str(exc))
                st.session_state.messages.append({"role": "assistant", "content": f"（出错：{exc}）"})
            except Exception as exc:
                st.error(f"发生意外错误：{type(exc).__name__}: {exc}")

    # 嵌套在普通容器中，避免 Streamlit 的底部聊天容器在 Expander 展开时强制滚到底部。
    # CSS 仍将该容器固定在页面底部，交互与原聊天输入框一致。
    with st.container(key="chat_composer"):
        question = st.chat_input("输入问题，按 Enter 发送……", key="chat_question")
    if question:
        _process_question(question)


# ---------------------------------------------------------------- 页面 2：上传文档
def page_upload():
    st.markdown('<div class="section-title">导入知识</div>', unsafe_allow_html=True)
    st.caption(f"支持 {', '.join('.' + t for t in SUPPORTED_UPLOAD_TYPES)} · 文件会复制到领域目录后再入库")

    tab_files, tab_dir = st.tabs(["📄 上传文件", "📁 导入目录"])

    with tab_files:
        col1, col2 = st.columns(2)
        with col1:
            upload_domain = st.selectbox("领域", DOMAINS,
                                         format_func=domain_label, index=DOMAINS.index("learning"))
        with col2:
            upload_category = st.text_input("子分类（留空为 general）", value="",
                                            help="自动转小写，如 projects、ai")
        uploads = st.file_uploader("拖拽文件到此处，或点击选择",
                                   type=SUPPORTED_UPLOAD_TYPES, accept_multiple_files=True)
        if uploads:
            st.caption(f"✓ 已选择 {len(uploads)} 个文件 · {sum(u.size for u in uploads) / 1024:.0f} KB")
        if st.button("📦 开始入库", disabled=not uploads, use_container_width=True, type="primary"):
            _do_upload(uploads, upload_domain, upload_category)

    with tab_dir:
        st.markdown("**整个目录导入**")
        st.caption("可拖拽或点击选择目录；两种方式都会保留目录名和全部子目录层级。")
        dir_domain = st.selectbox(
            "导入到领域",
            DOMAINS,
            format_func=domain_label,
            index=DOMAINS.index("learning"),
            key="directory_domain",
        )
        directory_mode = st.segmented_control(
            "导入方式",
            ["拖拽目录 · 合计 64 MB", "点击选择目录 · 单文件 200 MB"],
            default="拖拽目录 · 合计 64 MB",
            key="directory_import_mode",
        )
        directory_batch = None
        native_directory_uploads = None
        batch_id = None
        if directory_mode == "拖拽目录 · 合计 64 MB":
            directory_batch = directory_drop_uploader(key="directory_drop_uploader")
        else:
            native_limit_mb = st.get_option("server.maxUploadSize")
            native_directory_uploads = st.file_uploader(
                "点击下方按钮选择目录",
                accept_multiple_files="directory",
                key="native_directory_picker",
                help=(
                    f"原生目录选择的上限是每个文件 {native_limit_mb} MB。"
                    "请点击按钮选择；如需拖拽，请切换到“拖拽目录”。"
                ),
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
                st.success(
                    f"已选择 {len(decoded_entries)} 个文件 · {total_kb:.1f} KB · "
                    f"其中 {supported_count} 个文档可入库"
                )
                st.caption(f"将复制到：`knowledge/{dir_domain}/` 下的子目录 **{root_text}**")
            except ValueError as exc:
                roots = []
                st.error(str(exc))
        else:
            roots = []
            st.info("选择后可在这里确认文件数量和目标目录。", icon="📁")

        if st.button(
            "复制目录并开始入库",
            disabled=(
                not decoded_entries
                or not roots
                or st.session_state.get("processed_directory_batch") == batch_id
            ),
            use_container_width=True,
            type="primary",
            key="import_directory",
        ):
            _do_directory_upload(decoded_entries, dir_domain, batch_id)


def _do_upload(uploads, upload_domain, upload_category):
    cfg.knowledge_dir.mkdir(exist_ok=True)
    target_dir = cfg.knowledge_dir / upload_domain / (upload_category.strip().lower() or "general")
    target_dir.mkdir(parents=True, exist_ok=True)
    saved_paths = []
    for upload in uploads:
        target = target_dir / upload.name
        target.write_bytes(upload.getvalue())
        saved_paths.append(target)
    with st.spinner("解析 → 清洗 → 切片 → 向量化 → 入库……"):
        summary = ingest_files(paths=saved_paths)
    _show_ingest_result(summary)


def _do_directory_upload(entries, dir_domain, batch_id):
    """把浏览器选中的目录完整复制为领域目录的子目录，然后增量入库。"""
    domain_dir = cfg.knowledge_dir / dir_domain
    domain_dir.mkdir(parents=True, exist_ok=True)

    grouped: dict[str, list[tuple[tuple[str, ...], bytes]]] = {}
    try:
        for parts, content in entries:
            root = _clean_directory_name(parts[0])
            grouped.setdefault(root, []).append((parts[1:], content))
    except ValueError as exc:
        st.error(str(exc))
        return

    saved: list[Path] = []
    copied_roots: list[str] = []
    for root_name, entries in grouped.items():
        target_root = _available_directory(domain_dir, root_name)
        copied_roots.append(target_root.name)
        for relative_parts, content in entries:
            destination = target_root.joinpath(*relative_parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            saved.append(destination)

    if not saved:
        st.warning("所选目录中没有可导入的文档。")
        return

    ingest_paths = [path for path in saved if path.suffix.lower() in SUPPORTED_EXTENSIONS]
    if not ingest_paths:
        st.warning(f"目录已完整复制，但其中没有支持入库的文档（{', '.join(sorted(SUPPORTED_EXTENSIONS))}）。")
        return

    st.info(
        f"已复制 {len(saved)} 个文件到 {dir_domain}/{'、'.join(copied_roots)}，"
        f"正在为其中 {len(ingest_paths)} 个文档入库……"
    )
    with st.spinner("解析 → 清洗 → 切片 → 向量化 → 入库……"):
        summary = ingest_files(paths=ingest_paths)
    st.session_state["processed_directory_batch"] = batch_id
    _show_ingest_result(summary)


def _show_ingest_result(summary):
    if summary.ok_files:
        st.success(f"🎉 成功 {summary.ok_files}/{summary.total_files} · "
                   f"{summary.total_chunks} 张卡片 · {summary.elapsed_seconds:.1f}s")
    else:
        st.error("入库失败，请看终端日志。")
    if summary.file_rows:
        st.dataframe(
            [{"路径": r["path"], "领域": r["domain"], "分类": r["category"],
              "状态": r["status"], "卡片": r["chunks"]} for r in summary.file_rows],
            use_container_width=True, hide_index=True)
    for f in summary.failed_files:
        st.warning(f)

    st.divider()
    st.markdown('<div class="section-title">📁 knowledge/ 目录现状</div>', unsafe_allow_html=True)
    if cfg.knowledge_dir.exists():
        existing = sorted(p for p in cfg.knowledge_dir.rglob("*")
                          if p.is_file() and not p.name.startswith("."))
        by_domain: dict[str, int] = {}
        for p in existing:
            first = p.relative_to(cfg.knowledge_dir).parts[0]
            by_domain[first] = by_domain.get(first, 0) + 1
        st.markdown(
            "　".join(tag(f"{domain_label(d)} · {n}", "blue") for d, n in sorted(by_domain.items())),
            unsafe_allow_html=True,
        )
        with st.expander(f"查看全部 {len(existing)} 个文件"):
            for path in existing:
                st.markdown(f"<code style='font-size:.85rem'>{path.relative_to(cfg.knowledge_dir).as_posix()}</code>"
                            f"<span style='color:#94A3B8;font-size:.8rem'>　{path.stat().st_size / 1024:.1f} KB</span>",
                            unsafe_allow_html=True)
    else:
        st.info("knowledge/ 目录还不存在，上传第一个文件后会自动创建。")


# ---------------------------------------------------------------- 页面 3：知识管理台
def page_manage():
    st.markdown('<div class="section-title">🗂 知识管理台</div>', unsafe_allow_html=True)
    st.caption("勾选一个或多个文件，可批量移除知识；单选时还可以查看档案并执行重新入库、归档或恢复。")
    if notice := st.session_state.pop("manage_action_notice", None):
        st.toast(notice, icon="✅")

    # V3.6 跨文档知识总结
    with st.expander("📝 知识总结（跨文档综合）"):
        st.caption(
            "输入一个想了解的主题，系统会跨多个知识文件检索相关片段，"
            "生成“核心要点 + 对应出处”的综合摘要；不会新建或修改知识文件。"
        )
        sum_topic = st.text_input(
            "总结主题",
            placeholder="例如：RAG、智能客服、卡片笔记法",
            key="sum_topic",
            help="它相当于指定一份跨文档摘要的中心问题，而不是修改文件的主题标签。",
        )
        if st.button("📝 生成总结", disabled=not sum_topic.strip(), type="primary"):
            with st.spinner("综合多个文档生成知识总结……"):
                result = answer_question(
                    f"请综合知识库中与「{sum_topic.strip()}」相关的全部内容，"
                    "输出一份结构化知识总结：核心要点 + 对应出处编号。资料不足时明确说明。",
                    top_k=10,
                )
            st.markdown(result.answer or "（没有生成内容）")
            if result.sources:
                st.caption("📚 参考来源：" + "、".join(sorted({s["source"] for s in result.sources})))

    try:
        documents = store.list_documents()
    except VectorStoreError as exc:
        st.error(str(exc))
        documents = []

    if not documents:
        st.info("知识库还是空的：先去「📤 上传文档」或运行 python ingest.py 入库。")
        return

    total_chunks = sum(d["chunks"] for d in documents)
    archived = sum(1 for d in documents if d["status"] == "archive")
    domains_covered = len({d["domain"] for d in documents})
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("📚 知识文件", len(documents))
    m2.metric("🧩 知识卡片", total_chunks)
    m3.metric("🌍 覆盖领域", f"{domains_covered} / {len(DOMAINS)}")
    m4.metric("🗄 归档文件", archived)

    st.divider()

    f1, f2, f3 = st.columns([3, 1.2, 1.2])
    keyword = f1.text_input("🔍 按文件名 / 路径搜索", placeholder="例如：RAG、projects……")
    dom_filter = f2.selectbox("领域", ["全部"] + [domain_label(d) for d in DOMAINS])
    status_filter = f3.selectbox("状态", ["全部", "active", "archive"])

    filtered = [
        d for d in documents
        if (not keyword or keyword.lower() in d["path"].lower())
        and (dom_filter.startswith("全部") or d["domain"] == dom_filter.split(" ")[0])
        and (status_filter == "全部" or d["status"] == status_filter)
    ]

    if not filtered:
        st.info("没有符合筛选条件的文件。")
        return

    # 列表头部多选工具栏。多选只改变当前选择，不直接触发归档/删除等操作。
    selection_key = "manage_selected_documents"
    visible_ids = {d["document_id"] for d in filtered}
    selected_ids = set(st.session_state.get(selection_key, [])) & visible_ids

    def _select_visible_documents(ids=tuple(sorted(visible_ids))):
        st.session_state[selection_key] = list(ids)

    def _clear_visible_documents():
        st.session_state[selection_key] = []

    list_head, select_col, clear_col = st.columns([5, 1.35, 1.2], vertical_alignment="center")
    list_head.markdown(f"**文件列表**　<span style='color:var(--text-muted);font-size:.78rem'>"
                       f"{len(filtered)} 个结果 · 已选 {len(selected_ids)} 个</span>",
                       unsafe_allow_html=True)
    select_col.button(
        "全选当前结果",
        width="stretch",
        key="manage_select_all",
        disabled=not filtered or len(selected_ids) == len(visible_ids),
        on_click=_select_visible_documents,
    )
    clear_col.button(
        "清空选择",
        width="stretch",
        key="manage_clear_selection",
        disabled=not selected_ids,
        on_click=_clear_visible_documents,
    )

    rows = [{"_id": d["document_id"], "选择": d["document_id"] in selected_ids,
             "文件": d["source"], "路径": d["path"],
             "Domain": d["domain"], "Category": d["category"],
             "Topic": ", ".join(d["topic"]) or "-", "Version": d["version"],
             "Status": d["status"], "卡片数": d["chunks"]} for d in filtered]
    edited = st.data_editor(
        rows,
        width="stretch",
        hide_index=True,
        key=f"manage_document_table_{st.session_state.get('manage_table_revision', 0)}",
        column_config={
            "_id": None,
            "选择": st.column_config.CheckboxColumn("选择", help="勾选一个或多个知识文件"),
        },
        disabled=["文件", "路径", "Domain", "Category", "Topic", "Version", "Status", "卡片数"],
    )
    records = edited.to_dict("records") if hasattr(edited, "to_dict") else list(edited)
    selected_ids = {row["_id"] for row in records if row.get("选择")}
    st.session_state[selection_key] = sorted(selected_ids)

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

    st.markdown("#### 批量操作" if len(selected_documents) > 1 else "#### 文件操作")
    action_info, remove_index_col, remove_disk_col = st.columns(
        [2.2, 1.35, 1.7], vertical_alignment="center"
    )
    action_info.caption(
        f"已选择 {len(selected_documents)} 个文件。两种移除操作都会清除其全部知识卡片。"
    )
    remove_index_col.button(
        "移除向量知识",
        icon=":material/delete_sweep:",
        width="stretch",
        key="manage_remove_index",
        help="只从向量知识库移除，磁盘文件保留，可再次入库",
        on_click=_request_manage_remove,
        args=(False,),
    )
    remove_disk_col.button(
        "移除知识并删除文件",
        icon=":material/delete_forever:",
        width="stretch",
        key="manage_remove_disk",
        help="从向量知识库移除，并永久删除对应磁盘文件",
        on_click=_request_manage_remove,
        args=(True,),
    )

    if len(selected_ids) > 1:
        st.info("批量选择时不显示单文件档案；清空选择或只保留一项即可查看详情。")
        return

    selected_id = next(iter(selected_ids))
    d = next(item for item in filtered if item["document_id"] == selected_id)
    st.divider()

    head_l, head_r = st.columns([2.2, 1])
    with head_l:
        status_tag = tag("✅ active", "green") if d["status"] == "active" else tag("🗄 archive", "amber")
        st.markdown(
            f"### 📄 {d['source']}　{status_tag}"
            + tag(f"🗂 {domain_label(d['domain'])}", "blue")
            + tag(f"📁 {d['category']}", "blue")
            + (tag("🏷 " + " / ".join(d["topic"])) if d["topic"] else "")
            + tag(f"v{d['version']}", "green"),
            unsafe_allow_html=True,
        )
        st.caption(f"`{d['document_id']}`　共 {d['chunks']} 张知识卡片")
    with head_r:
        is_archived = d["path"].split("/")[0] == "archive"
        btn1, btn2 = st.columns(2)
        reingest_btn = btn1.button("📥 重新入库", width="stretch", key="act_reingest",
                                   help="文件内容修改后，重新解析入库")
        archive_btn = btn2.button("🗄 归档", width="stretch", key="act_archive",
                                  disabled=is_archived, help="移入 archive/ 目录，退出日常检索")
        restore_btn = btn2.button("🔄 恢复", width="stretch", key="act_restore",
                                  disabled=not is_archived, help="移回原目录，重新参与检索")

        actions = [
            (reingest_btn, lambda: reingest_file(d["path"])),
            (archive_btn, lambda: archive_file(d["path"])),
            (restore_btn, lambda: restore_file(d["path"])),
        ]
        for btn, fn in actions:
            if btn:
                try:
                    info = fn()
                    st.toast(info["message"], icon="✅")
                    st.rerun()
                except ManageError as exc:
                    st.error(str(exc))

    # 档案编辑（V3.4）
    with st.expander("✏️ 编辑档案"):
        e1, e2 = st.columns(2)
        new_title = e1.text_input("标题", value=d["source"] and (d.get("source") or ""), key="ed_title")
        new_category = e2.text_input("分类 Category", value=d["category"], key="ed_cat")
        new_topic = st.text_input("主题 Topic（逗号分隔）", value=", ".join(d["topic"]), key="ed_topic")
        new_version = st.text_input("版本 Version", value=d["version"], key="ed_ver")
        if st.button("💾 保存档案修改", type="primary"):
            updates = {"title": new_title, "category": new_category,
                       "topic": [t.strip() for t in new_topic.split(",") if t.strip()],
                       "version": new_version}
            try:
                info = __import__("src.manage", fromlist=["update_metadata"]).update_metadata(d["path"], updates)
                st.toast(info["message"], icon="✅")
                st.rerun()
            except ManageError as exc:
                st.error(str(exc))

    payloads = store.chunks_by_document(d["document_id"])
    st.markdown(f"**🧩 知识卡片（{len(payloads)} 张）**")
    for payload in payloads:
        head = (f"`{payload.get('chunk_id', '?')}`　"
                f"章节：{payload.get('section') or '-'}　页码：{payload.get('page') or '-'}")
        with st.expander(head):
            st.markdown(f"<div class='chunk-quote'>{payload.get('text', '')}</div>",
                        unsafe_allow_html=True)


# ---------------------------------------------------------------- 页面 4：检索设置
def page_retrieval_settings():
    st.markdown('<div class="section-title">🎨 外观</div>', unsafe_allow_html=True)
    theme_labels = {"system": "🖥 跟随系统", "light": "☀️ 浅色", "dark": "🌙 深色"}
    current_theme = st.session_state.get("theme", "system")
    theme_choice = st.radio("界面主题", list(theme_labels.keys()),
                            format_func=lambda k: theme_labels[k],
                            index=list(theme_labels.keys()).index(current_theme),
                            horizontal=True, key="set_theme_ui")
    if theme_choice != current_theme:
        st.session_state["theme"] = theme_choice
        from src.app_settings import save_app_settings, SETTINGS_KEYS
        save_app_settings({k: st.session_state.get(k) for k in SETTINGS_KEYS if k in st.session_state})
        st.rerun()

    st.divider()
    st.markdown('<div class="section-title">🔍 检索设置</div>', unsafe_allow_html=True)
    st.caption("这里的设置对「知识库问答」页即时生效")

    domain_labels = ["全部"] + [domain_label(d) for d in DOMAINS]
    scopes = ["仅 active", "包含归档", "仅归档"]

    # 显式传入当前生效值（value=），保证第一次打开就显示正确数据；变化时同步回 session
    st.slider("每次召回知识条数（Top K）", 1, 10,
              value=st.session_state["top_k"], key="set_top_k",
              on_change=_sync_setting("top_k", "set_top_k"))
    st.selectbox("限定知识领域", domain_labels,
                 index=domain_labels.index(st.session_state["domain_choice"]),
                 key="set_domain", on_change=_sync_setting("domain_choice", "set_domain"))
    st.radio("检索范围", scopes,
             index=scopes.index(st.session_state["scope_choice"]),
             key="set_scope", on_change=_sync_setting("scope_choice", "set_scope"),
             horizontal=True,
             help="归档（archive）内容默认不参与回答，除非你明确要求")

    st.divider()
    st.markdown('<div class="section-title">🧠 检索增强开关（V2）</div>', unsafe_allow_html=True)
    st.toggle("🧠 Query 理解 / 改写",
              value=st.session_state["query_understanding"], key="set_qu",
              on_change=_sync_setting("query_understanding", "set_qu"),
              help="先用大模型把口语化提问改写成检索友好查询，并自动推断过滤条件（领域/归档等）。"
                   "开启后每次问答多用一次 LLM 调用")
    st.toggle("🔀 混合检索",
              value=st.session_state["hybrid_search"], key="set_hybrid",
              on_change=_sync_setting("hybrid_search", "set_hybrid"),
              help="向量通道 + BM25 关键词通道两路召回，RRF 融合排序；"
                   "专有名词、编号、缩写类问题更准。本机计算，不多花 API 钱")
    st.toggle("🏆 Rerank 精排",
              value=st.session_state["rerank"], key="set_rerank",
              on_change=_sync_setting("rerank", "set_rerank"),
              help="召回扩宽到 10 条候选，大模型逐条阅读打相关度分后精选 Top K；"
                   "开启后每次问答多用一次 LLM 调用")
    st.toggle("🛠 调试模式（问答页展示节点时间线与检索明细）",
              value=st.session_state["debug_mode"], key="set_debug",
              on_change=_sync_setting("debug_mode", "set_debug"))


# ---------------------------------------------------------------- 页面 5：维护操作
def page_maintenance():
    st.markdown('<div class="section-title">🧹 维护操作</div>', unsafe_allow_html=True)
    st.caption("批量操作，谨慎使用；日常的单文件管理请去「🗂 知识库管理」")

    st.markdown("**知识库维护**")
    confirm_rebuild = st.checkbox("我确认要清空并重建全库", key="confirm_rebuild")
    c1, c2 = st.columns(2)
    if c1.button("🔄 清空并重建知识库", disabled=not confirm_rebuild,
                 width="stretch", type="primary"):
        with st.spinner("重建中……"):
            summary = ingest_files(rebuild=True)
        if summary.ok_files:
            st.success(f"重建完成：{summary.ok_files} 个文件 → {summary.total_chunks} 张卡片")
        else:
            st.error("重建失败，请看终端日志。")
    if c2.button("🗑 仅清空向量库", width="stretch"):
        store.clear()
        st.success("已清空。重新入库即可恢复。")

    st.divider()

    st.markdown("**缓存管理**")
    from src.pipeline import _qa_cache, clear_qa_cache
    from src.keyword_search import _bm25_cache, invalidate_cache

    cache_col1, cache_col2 = st.columns(2)
    cache_col1.metric("⚡ Q→A 问答缓存", f"{len(_qa_cache)} 条")
    cache_col2.metric("🔑 BM25 索引缓存", f"{len(_bm25_cache)} 组")

    if st.button("🧹 清空全部缓存", width="stretch", type="primary"):
        clear_qa_cache()
        invalidate_cache()
        st.success("已清空问答缓存和 BM25 索引缓存。下次提问会重新计算。")
        st.rerun()


# ---------------------------------------------------------------- 页面 6：系统状态
def page_system_status():
    st.markdown('<div class="section-title">📊 系统状态</div>', unsafe_allow_html=True)
    for name, (ok, note) in system_status().items():
        dot = "🟢" if ok else "🔴"
        st.markdown(f"{dot} **{name}**　<span style='color:#64748B'>{note}</span>",
                    unsafe_allow_html=True)
    if st.button("🔄 重新检测"):
        st.cache_data.clear()
        st.rerun()
    st.caption("三盏灯的含义：向量化模型（本机 Ollama）· 向量数据库（本机 Qdrant）· 大模型（云端 API Key）")


# ---------------------------------------------------------------- 页面 7：参数总览
def page_params_overview():
    st.markdown('<div class="section-title">🧾 当前参数总览</div>', unsafe_allow_html=True)
    st.dataframe(
        [{"参数": k, "当前值": str(v), "说明": note} for k, v, note in [
            ("切片长度 CHUNK_SIZE", cfg.chunk_size, "每张知识卡片的目标字数"),
            ("切片重叠 CHUNK_OVERLAP", cfg.chunk_overlap, "相邻卡片重复字数，防语义切断"),
            ("召回条数 TOP_K", st.session_state["top_k"], "每次提问召回的知识卡片数"),
            ("资料上限 CONTEXT_MAX_TOKENS", cfg.context_max_tokens, "发给大模型的资料 token 上限"),
            ("回答发散度 TEMPERATURE", cfg.llm_temperature, "知识库场景建议小值"),
            ("上下文单文档上限", cfg.context_max_per_doc, "同一文档最多进入回答的卡片数，保持来源多样"),
            ("向量化模型", cfg.embedding_model, "本地 Ollama 运行；更换需重建知识库"),
            ("大模型", cfg.llm_model or "未配置", "生成回答；支持云端 API 或本地 Ollama（OpenAI 兼容接口）"),
            ("向量集合", cfg.qdrant_collection, "全部知识共居一库"),
        ]],
        width="stretch", hide_index=True,
    )
    st.caption("切片等入库参数在 .env 中修改，改完需重新入库生效。")


# ---------------------------------------------------------------- 布局组装：左侧菜单 + 右侧页面
# 品牌区（Logo + Slogan）通过 CSS order 固定在导航上方；
# 系统状态通过 order + margin-top:auto 固定在侧边栏最底部。
with st.sidebar:
    with st.container(key="sidebar_brand"):
        st.markdown(
            """
            <div class="brand-block">
              <div style="display:flex;align-items:center;gap:10px;">
                <div style="width:42px;height:42px;border-radius:12px;flex:none;
                            background:linear-gradient(135deg,#2563EB,#4F46E5);
                            display:flex;align-items:center;justify-content:center;
                            font-size:1.35rem;box-shadow:0 4px 14px rgba(37,99,235,.35);">🧠</div>
                <div>
                  <div class="brand-name">Sky Personal RAG</div>
                  <div class="brand-slogan">个人知识库 · 检索增强问答</div>
                </div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

# ---------------------------------------------------------------- 页面 8：学习笔记
def page_learning():
    st.markdown('<div class="section-title">📖 RAG 实现全解</div>', unsafe_allow_html=True)
    st.caption("写给产品经理的节点级学习笔记：每个节点的技术、作用、目标与上下游衔接，基于本项目真实实现。")
    md_path = Path(__file__).parent / "realize.md"
    if md_path.exists():
        st.markdown(md_path.read_text(encoding="utf-8"))
    else:
        st.info("学习笔记文件 realize.md 不存在。")


chat_page = st.Page(page_chat, title="知识库问答", icon="💬", url_path="chat", default=True)
upload_page = st.Page(page_upload, title="上传文档", icon="📤", url_path="upload")
manage_page = st.Page(page_manage, title="知识库管理", icon="🗂", url_path="manage")
retrieval_page = st.Page(page_retrieval_settings, title="检索设置", icon="🔍", url_path="settings-retrieval")
maintenance_page = st.Page(page_maintenance, title="维护操作", icon="🧹", url_path="settings-maintenance")
status_page = st.Page(page_system_status, title="系统状态", icon="📊", url_path="settings-status")
params_page = st.Page(page_params_overview, title="参数总览", icon="🧾", url_path="settings-params")
learning_page = st.Page(page_learning, title="RAG 实现全解", icon="📖", url_path="learn")

pg = st.navigation({
    "知识库": [
        chat_page,
        upload_page,
        manage_page,
    ],
    "设置": [
        retrieval_page,
        maintenance_page,
        status_page,
        params_page,
    ],
    "学习": [
        learning_page,
    ],
})

pg.run()

with st.sidebar:
    with st.container(key="sidebar_footer"):
        sessions = list_sessions(10)
        keep_history_expanded = st.session_state.pop("keep_history_expanded_once", False)
        with st.expander(
            f"🕘 历史会话 · {len(sessions)}",
            expanded=keep_history_expanded,
        ):
            if sessions:
                for s in sessions:
                    with st.container(key=f"session_item_{s['session_id']}"):
                        title = s["title"].strip() or "未命名会话"
                        with st.container(key=f"hs_{s['session_id']}"):
                            st.page_link(
                                chat_page,
                                label=title[:24],
                                query_params={"load": s["session_id"]},
                                width="stretch",
                            )
                        def _request_del_cb(sid=s["session_id"], session_title=title):
                            st.session_state["delete_session_request"] = {
                                "session_id": sid,
                                "title": session_title,
                            }
                        st.button(
                            "删除",
                            key=f"del_{s['session_id']}",
                            on_click=_request_del_cb,
                            help=f"删除会话：{title}",
                            icon=":material/delete:",
                            type="tertiary",
                        )
                        updated = s["updated_at"]
                        when = updated[5:16] if len(updated) >= 16 else updated
                        st.caption(f"{when}　·　{s['count']} 条消息")
            else:
                st.markdown(
                    '<div style="text-align:center;font-size:.72rem;color:var(--text-muted);padding:18px 6px;line-height:1.7;">'
                    '还没有历史会话<br/>完成第一次问答后会自动保存在这里</div>', unsafe_allow_html=True)

        ok_all = all(ok for ok, _ in system_status().values())
        status_emoji = "🟢" if ok_all else "🟡"
        status_text = "正常" if ok_all else "有待处理项"
        status_detail = " · ".join(
            f"{name}{'✓' if ok else '!'}" for name, (ok, _) in system_status().items()
        )
        st.markdown(
            f"""
            <div class="sidebar-status">
              <div class="sidebar-status-row">
                <strong>{status_emoji} 系统状态</strong><span>{status_text}</span>
              </div>
              <div class="sidebar-status-note">{status_detail}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

if st.session_state.get("delete_session_request"):
    confirm_session_delete()
elif st.session_state.get("manage_remove_request"):
    confirm_manage_remove()

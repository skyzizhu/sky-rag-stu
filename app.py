"""个人知识库 RAG —— Streamlit 界面（侧边菜单导航版）。

启动方式：在项目根目录运行  streamlit run app.py

布局：左侧是菜单（顶部为 Logo 与 Slogan），右侧是对应页面。菜单分两组：
    知识库：💬 知识库问答（横幅 + 建议问题 + 流式回答 + 来源卡片）
            📤 上传文档（按领域目录入库，逐文件结果）
            🗂 知识库管理（统计 / 筛选 / 归档·恢复·移除·重新入库）
    设置：🔍 检索设置 · 🧹 维护操作 · 📊 系统状态 · 🧾 参数总览
"""

from __future__ import annotations

import html
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

from src.app_settings import SETTINGS_KEYS, load_app_settings, save_app_settings  # noqa: E402
from src.config import get_config  # noqa: E402
from src.embedding import EmbeddingClient, EmbeddingError  # noqa: E402
from src.llm import LLMError  # noqa: E402
from src.manage import (  # noqa: E402
    ManageError,
    archive as archive_file,
    reingest as reingest_file,
    remove_from_index,
    restore as restore_file,
)
from src.metadata import DOMAINS, DOMAIN_LABELS  # noqa: E402
from src.parser import SUPPORTED_EXTENSIONS  # noqa: E402
from src.pipeline import QAResult, answer_question, answer_stream, ingest_files  # noqa: E402
from src.sessions import list_sessions, load_session, new_session_id, save_session  # noqa: E402
from src.vector_store import VectorStoreError, get_vector_store  # noqa: E402

st.set_page_config(page_title="Sky Personal RAG", page_icon="🧠", layout="wide")
cfg = get_config()

SUPPORTED_UPLOAD_TYPES = [ext.lstrip(".") for ext in sorted(SUPPORTED_EXTENSIONS) if ext != ".htm"]
SUGGESTED_QUESTIONS = [
    "RAG 里的 Metadata 有什么用？",
    "chunk_size 初始设多少？为什么需要 overlap？",
    "智能客服项目的效果怎么样？",
    "Agent 的三大组件是什么？",
]

# 跨页面共享的设置项：在「设置与状态」里改，问答页即时生效
st.session_state.setdefault("top_k", cfg.top_k)
st.session_state.setdefault("domain_choice", "全部")
st.session_state.setdefault("scope_choice", "仅 active")
st.session_state.setdefault("debug_mode", True)
st.session_state.setdefault("query_understanding", cfg.query_understanding)
st.session_state.setdefault("session_id", new_session_id())

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
#MainMenu, footer, [data-testid="stStatusWidget"] {display: none !important;}
[data-testid="stAppDeployButton"] {display: none !important;}
header[data-testid="stHeader"] {background: transparent !important;}
html, body, .stApp, .stMarkdown, p, span, div, button, input, textarea {
  font-family: 'Inter', -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif !important;}
.stApp {background: #0B0F1A !important; color: #E2E8F0 !important;}
.block-container {padding-top: 1.2rem !important; padding-bottom: 10rem !important; max-width: 1280px !important;}
[data-testid="stSidebar"] {background: linear-gradient(180deg, #0F1523, #0B1018) !important;
  border-right: 1px solid rgba(148,163,184,.08) !important; min-width: 250px !important;}
[data-testid="stSidebar"] .block-container {padding-top: 1.2rem !important;}
[data-testid="stSidebar"] [role="radiogroup"] label {border-radius: 10px; padding: 4px 12px;
  margin: 1px 0; transition: all .2s; font-weight: 500; font-size: .82rem; color: #94A3B8 !important;}
[data-testid="stSidebar"] [role="radiogroup"] label:hover {background: rgba(99,102,241,.08); color: #C7D2FE !important;}
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) {
  background: linear-gradient(135deg, rgba(99,102,241,.15), rgba(139,92,246,.1));
  color: #818CF8 !important; border-left: 2px solid #818CF8;}
.brand-block {padding: 4px 0 14px; border-bottom: 1px solid rgba(148,163,184,.06);}
[data-testid="stExpander"] {border-radius: 12px !important;
  border: 1px solid rgba(148,163,184,.08) !important; background: transparent !important;}
.stButton > button {border-radius: 10px; border: 1px solid rgba(148,163,184,.12);
  font-weight: 500; font-size: .82rem; transition: all .2s;
  background: rgba(148,163,184,.04); color: #CBD5E1;}
.stButton > button:hover {border-color: rgba(129,140,248,.3); color: #A5B4FC;
  background: rgba(99,102,241,.06); transform: translateY(-1px);}
.stButton > button[kind="primary"] {background: linear-gradient(135deg, #6366F1, #8B5CF6) !important;
  color: #fff !important; border: none; font-weight: 600; box-shadow: 0 4px 16px rgba(99,102,241,.25);}
.stButton > button[kind="primary"]:hover {filter: brightness(1.1); transform: translateY(-1px);}
.stTextInput input, .stSelectbox div[data-baseweb="select"] > div, .stTextArea textarea {
  border-radius: 10px !important; border-color: rgba(148,163,184,.1) !important;
  background: rgba(148,163,184,.03) !important; color: #E2E8F0 !important; font-size: .85rem !important;}
.stTextInput input:focus, .stTextArea textarea:focus {
  border-color: #818CF8 !important; box-shadow: 0 0 0 3px rgba(99,102,241,.1) !important;}
div[data-baseweb="radio"] label {color: #94A3B8 !important;}
[data-testid="stChatMessage"] {border-radius: 16px; border: 1px solid rgba(148,163,184,.06);
  background: rgba(148,163,184,.02); padding: 8px 14px; margin-bottom: 6px;}
[data-testid="stChatMessage"]:has(.user-bubble) {flex-direction: row-reverse;
  background: transparent; border: none; margin-left: 8%; padding: 2px;}
.user-bubble {background: linear-gradient(135deg, #6366F1, #8B5CF6); color: #fff;
  padding: 10px 18px; border-radius: 20px 20px 6px 20px; font-size: .9rem; line-height: 1.6;
  box-shadow: 0 4px 16px rgba(99,102,241,.2);}
[data-testid="stBottom"], [data-testid="stBottom"] > div {background: transparent !important;
  border: none !important; box-shadow: none !important;}
[data-testid="stBottom"] {position: fixed !important; bottom: 10px !important;
  left: 50% !important; transform: translateX(-50%); width: min(920px, 94vw) !important; z-index: 200;}
[data-testid="stChatInput"] {border-radius: 24px !important;
  border: 1px solid rgba(148,163,184,.12) !important; background: #111827 !important;
  box-shadow: 0 8px 32px rgba(0,0,0,.4) !important;}
[data-testid="stChatInput"] textarea {border-radius: 24px !important;
  background: transparent !important; color: #E2E8F0 !important;}
.hero {background: linear-gradient(135deg, #1E1B4B, #312E81 40%, #4338CA);
  border-radius: 20px; padding: 28px 32px; color: #E0E7FF; margin-bottom: 14px;
  border: 1px solid rgba(129,140,248,.15);}
.hero h1 {margin: 0 0 4px; font-size: 1.35rem; font-weight: 800; letter-spacing: -.01em;}
.hero p {margin: 0 0 12px; opacity: .7; font-size: .82rem;}
.hero .pill {display: inline-block; background: rgba(99,102,241,.15);
  border: 1px solid rgba(129,140,248,.2); border-radius: 999px;
  padding: 3px 12px; font-size: .7rem; margin-right: 6px; color: #A5B4FC;}
[data-testid="stMetric"] {background: rgba(148,163,184,.03);
  border: 1px solid rgba(148,163,184,.08); border-radius: 16px; padding: 16px 18px;}
[data-testid="stMetric"]:hover {border-color: rgba(129,140,248,.15);}
[data-testid="stMetric"] label {color: #94A3B8 !important; font-size: .75rem !important;}
[data-testid="stMetric"] [data-testid="stMetricValue"] {color: #E2E8F0 !important;}
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
.status-block {position: absolute !important; left: 16px !important; right: 16px !important;
  bottom: 12px !important;}
[data-testid="stSidebarUserContent"] div:not(.status-block) {position: static !important;}
[data-testid="stSidebarUserContent"] {display: contents !important;}
[data-testid="stSidebarHeader"] {order: -2 !important;}
[data-testid="stSidebarUserContent"] > * {order: -1 !important;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


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

    chip_cols = st.columns(len(SUGGESTED_QUESTIONS))
    for col, suggestion in zip(chip_cols, SUGGESTED_QUESTIONS):
        with col:
            if st.button(suggestion, width="stretch", key=f"chip_{suggestion}"):
                st.session_state["ask_now"] = suggestion

    if "messages" not in st.session_state:
        st.session_state.messages = []

    if st.session_state.messages:
        c1, c2 = st.columns([4, 1])
        with c2:
            def _new_session_cb():
                st.session_state["session_id"] = new_session_id()
                st.session_state.messages = []
            if c2.button("🆕 开启新会话", width="stretch", on_click=_new_session_cb):
                pass

    for message in st.session_state.messages:
        with st.chat_message(message["role"], avatar="🙋" if message["role"] == "user" else "🧠"):
            if message["role"] == "user":
                st.markdown(f'<div class="user-bubble">{html.escape(message["content"])}</div>',
                            unsafe_allow_html=True)
            else:
                st.markdown(message["content"])
                if message.get("result") is not None:
                    show_answer_sources(message["result"])

    def _process_question(question: str) -> None:
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user", avatar="🙋"):
            st.markdown(f'<div class="user-bubble">{html.escape(question)}</div>',
                        unsafe_allow_html=True)

        with st.chat_message("assistant", avatar="🧠"):
            if not cfg.llm_api_key:
                st.error("还没有配置 LLM API Key：请到「设置 → 系统状态」查看指引，填好 .env 后刷新页面。")
                st.session_state.messages.append({"role": "assistant", "content": "（未配置 LLM API Key）"})
                return
            try:
                with st.spinner("🔍 正在检索知识库……"):
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
                    )
                full_text = st.write_stream(deltas)
                show_answer_sources(result)
                st.session_state.messages.append(
                    {"role": "assistant", "content": result.answer or full_text or "", "result": result}
                )
                save_session(st.session_state["session_id"], st.session_state.messages)
            except (LLMError, VectorStoreError, EmbeddingError) as exc:
                st.error(str(exc))
                st.session_state.messages.append({"role": "assistant", "content": f"（出错：{exc}）"})
            except Exception as exc:
                st.error(f"发生意外错误：{type(exc).__name__}: {exc}")

    pending = st.session_state.pop("ask_now", None)
    if pending:
        _process_question(pending)

    question = st.chat_input("输入你的问题……")
    if question:
        _process_question(question)


# ---------------------------------------------------------------- 页面 2：上传文档
def page_upload():
    st.markdown('<div class="section-title">📤 把文档放进知识库</div>', unsafe_allow_html=True)
    st.caption(f"支持格式：{', '.join('.' + t for t in SUPPORTED_UPLOAD_TYPES)}"
               f"　·　文件会存入 knowledge/<领域>/<分类>/ 目录，放对目录 = 自动打好分类")

    col1, col2 = st.columns([1, 1])
    with col1:
        upload_domain = st.selectbox("存放到哪个领域", DOMAINS,
                                     format_func=domain_label, index=DOMAINS.index("learning"))
    with col2:
        upload_category = st.text_input("子分类（可留空，自动转小写）", value="",
                                        help="例如 projects、ai、books；留空为 general")

    uploads = st.file_uploader("选择一个或多个文件（支持拖拽）",
                               type=SUPPORTED_UPLOAD_TYPES, accept_multiple_files=True)
    if uploads:
        st.caption(f"已选择 {len(uploads)} 个文件，共 {sum(u.size for u in uploads) / 1024:.0f} KB")
    if st.button("📦 开始入库", disabled=not uploads, width="stretch", type="primary"):
        cfg.knowledge_dir.mkdir(exist_ok=True)
        target_dir = cfg.knowledge_dir / upload_domain / (upload_category.strip().lower() or "general")
        target_dir.mkdir(parents=True, exist_ok=True)
        saved_paths = []
        for upload in uploads:
            target = target_dir / upload.name
            target.write_bytes(upload.getvalue())
            saved_paths.append(target)
        with st.spinner("解析 → 元数据 → 清洗 → 切片 → 向量化 → 入库……"):
            summary = ingest_files(paths=saved_paths)
        if summary.ok_files:
            st.success(f"🎉 入库完成：{summary.ok_files}/{summary.total_files} 个文件 · "
                       f"{summary.total_chunks} 张知识卡片 · 向量维度 {summary.vector_dimension} · "
                       f"耗时 {summary.elapsed_seconds:.1f} 秒")
        else:
            st.error("全部文件入库失败，详情见终端日志。")
        if summary.file_rows:
            st.dataframe(
                [{"路径": r["path"], "Domain": r["domain"], "Category": r["category"],
                  "Status": r["status"], "卡片数": r["chunks"]} for r in summary.file_rows],
                width="stretch", hide_index=True,
            )
        for failure in summary.failed_files:
            st.warning(failure)

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
    st.caption("选中任意一行，即可查看详情并执行：重新入库 / 归档 / 恢复 / 移除")

    # V3.6 跨文档知识总结
    with st.expander("📝 知识总结（跨文档综合）"):
        sum_topic = st.text_input("总结主题", placeholder="例如：RAG、智能客服、卡片笔记法", key="sum_topic")
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

    rows = [{"文件": d["source"], "路径": d["path"],
             "Domain": d["domain"], "Category": d["category"],
             "Topic": ", ".join(d["topic"]) or "-", "Version": d["version"],
             "Status": d["status"], "卡片数": d["chunks"]} for d in filtered]
    selection = st.dataframe(rows, width="stretch", hide_index=True,
                             on_select="rerun", selection_mode="single-row")
    selected_rows = list(selection.selection.rows) if selection.selection else []
    if not selected_rows:
        return

    d = filtered[selected_rows[0]]
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
        btn1, btn2, btn3 = st.columns(3)
        reingest_btn = btn1.button("📥 重新入库", width="stretch", key="act_reingest",
                                   help="文件内容修改后，重新解析入库")
        archive_btn = btn2.button("🗄 归档", width="stretch", key="act_archive",
                                  disabled=is_archived, help="移入 archive/ 目录，退出日常检索")
        restore_btn = btn2.button("🔄 恢复", width="stretch", key="act_restore",
                                  disabled=not is_archived, help="移回原目录，重新参与检索")
        remove_confirm = btn3.checkbox("确认移除", key=f"confirm_{d['document_id']}")
        remove_btn = btn3.button("🗑 移除", width="stretch", key="act_remove",
                                 disabled=not remove_confirm,
                                 help="只从向量库删除知识，磁盘文件保留")

        actions = [
            (reingest_btn, lambda: reingest_file(d["path"])),
            (archive_btn, lambda: archive_file(d["path"])),
            (restore_btn, lambda: restore_file(d["path"])),
            (remove_btn, lambda: remove_from_index(d["path"])),
        ]
        for btn, fn in actions:
            if btn:
                try:
                    info = fn()
                    st.toast(info["message"], icon="✅")
                    st.session_state.pop(f"confirm_{d['document_id']}", None)
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
    st.markdown(
        """
        <div class="brand-block">
          <div style="display:flex;align-items:center;gap:10px;">
            <div style="width:42px;height:42px;border-radius:12px;flex:none;
                        background:linear-gradient(135deg,#2563EB,#4F46E5);
                        display:flex;align-items:center;justify-content:center;
                        font-size:1.35rem;box-shadow:0 4px 14px rgba(37,99,235,.35);">🧠</div>
            <div>
              <div style="font-size:1.12rem;font-weight:800;color:#172554;line-height:1.2;">Sky Personal RAG</div>
              <div style="font-size:.76rem;color:#64748B;margin-top:3px;">个人知识库 · 检索增强问答</div>
            </div>
          </div>
          <div style="font-size:.78rem;color:#94A3B8;margin-top:10px;"></div>
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


pg = st.navigation({
    "知识库": [
        st.Page(page_chat, title="知识库问答", icon="💬", url_path="chat", default=True),
        st.Page(page_upload, title="上传文档", icon="📤", url_path="upload"),
        st.Page(page_manage, title="知识库管理", icon="🗂", url_path="manage"),
    ],
    "设置": [
        st.Page(page_retrieval_settings, title="检索设置", icon="🔍", url_path="settings-retrieval"),
        st.Page(page_maintenance, title="维护操作", icon="🧹", url_path="settings-maintenance"),
        st.Page(page_system_status, title="系统状态", icon="📊", url_path="settings-status"),
        st.Page(page_params_overview, title="参数总览", icon="🧾", url_path="settings-params"),
    ],
    "学习": [
        st.Page(page_learning, title="RAG 实现全解", icon="📖", url_path="learn"),
    ],
})

pg.run()

with st.sidebar:
    with st.expander("🕘 历史会话", expanded=False):
        sessions = list_sessions(10)

        if sessions:
            for i, s in enumerate(sessions):
                title_short = s["title"][:22]
                time_short = s["updated_at"][5:16]
                def _load_cb(session_id=s["session_id"]):
                    st.session_state["__load_session"] = session_id
                if st.button(f"📄 {title_short}", width="stretch",
                             key=f"hs_{s['session_id']}", on_click=_load_cb):
                    pass
                st.caption(f"🕒 {time_short} · {s['count']} 条消息")
        else:
            st.caption("暂无历史会话")

    ok_all = all(ok for ok, _ in system_status().values())
    status_emoji = "🟢" if ok_all else "🟡"
    status_text = "正常" if ok_all else "有待处理项"
    st.markdown(
        f"""
        <div class="status-block">
          <div style="border-top:1px solid #E4E9F2;padding:10px 2px 2px;
                      font-size:.8rem;color:#64748B;">{status_emoji} 系统状态：{status_text}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

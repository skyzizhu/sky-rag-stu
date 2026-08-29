"""个人知识库 RAG —— Streamlit 界面（V2 视觉版）。

启动方式：在项目根目录运行  streamlit run app.py

页面结构：
    左侧边栏：品牌区、系统状态、检索设置、调试开关
    标签页 1：💬 知识库问答（渐变首页 + 建议问题 + 逐字流式回答 + 卡片式来源）
    标签页 2：📤 上传文档（选择领域目录 → 入库 → 逐文件结果）
    标签页 3：🗂 知识管理台（统计卡片 / 筛选搜索 / 归档·恢复·重新入库·移除）
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

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
from src.parser import SUPPORTED_EXTENSIONS, ParseError  # noqa: E402
from src.pipeline import answer_stream, ingest_files  # noqa: E402
from src.retriever import effective_filters  # noqa: E402
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

# ---------------------------------------------------------------- 全局样式
CSS = """
<style>
/* 隐藏 Streamlit 自带装饰，页面更干净 */
#MainMenu, footer, [data-testid="stStatusWidget"], [data-testid="stToolbar"],
header[data-testid="stHeader"] [data-testid="stToolbar"] {visibility: hidden; height: 0;}
header[data-testid="stHeader"] {background: transparent;}
.block-container {padding-top: 1.1rem; padding-bottom: 3.5rem; max-width: 1240px;}

/* 全局字体 */
html, body, [class*="css"], .stApp {font-family: -apple-system, "PingFang SC", "Hiragino Sans GB",
  "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif; color: #1E293B;}

/* 侧边栏 */
[data-testid="stSidebar"] {background: linear-gradient(180deg, #F8FAFF 0%, #F2F5FA 100%);
  border-right: 1px solid #E8EDF5;}
[data-testid="stSidebar"] .block-container {padding-top: 1.4rem;}

/* 按钮 */
.stButton > button {border-radius: 10px; border: 1px solid #E2E8F0; font-weight: 600;
  transition: all .16s ease; background: #FFFFFF; color: #334155;}
.stButton > button:hover {border-color: #2563EB; color: #2563EB;
  box-shadow: 0 3px 12px rgba(37, 99, 235, .16); transform: translateY(-1px);}
.stButton > button[kind="primary"], .stButton > button[data-testid="stBaseButton-primary"] {
  background: linear-gradient(135deg, #2563EB, #4F46E5); color: #fff; border: none;
  box-shadow: 0 4px 14px rgba(37, 99, 235, .28);}
.stButton > button[kind="primary"]:hover {filter: brightness(1.06); color: #fff;}

/* 输入控件 */
.stTextInput input, .stSelectbox div[data-baseweb="select"] > div, .stTextArea textarea {
  border-radius: 10px !important;}
div[data-baseweb="radio"] label {margin-bottom: 2px;}

/* 聊天气泡 */
[data-testid="stChatMessage"] {border-radius: 16px; border: 1px solid #ECF1F8;
  background: #FBFCFE; padding: 6px 10px; margin-bottom: 4px;}
[data-testid="stChatInput"] textarea {border-radius: 14px !important;}

/* 折叠面板 / 表格 / 指标卡 */
[data-testid="stExpander"] {border-radius: 12px; border: 1px solid #E8EDF5;
  background: #FFFFFF;}
[data-testid="stDataFrame"] {border-radius: 12px; overflow: hidden;}
[data-testid="stMetric"] {background: linear-gradient(180deg, #F8FAFF, #FFFFFF);
  border: 1px solid #E8EDF5; border-radius: 14px; padding: 14px 16px;
  box-shadow: 0 1px 3px rgba(15, 23, 42, .04);}

/* 首页横幅 */
.hero {background: linear-gradient(135deg, #172554 0%, #1D4ED8 55%, #4F46E5 100%);
  border-radius: 20px; padding: 30px 34px; color: #fff; margin-bottom: 14px;
  box-shadow: 0 10px 30px rgba(29, 78, 216, .25);}
.hero h1 {margin: 0 0 6px; font-size: 1.65rem; font-weight: 800; letter-spacing: .5px;}
.hero p {margin: 0 0 14px; opacity: .88; font-size: .95rem;}
.hero .pill {display: inline-block; background: rgba(255,255,255,.14); border: 1px solid rgba(255,255,255,.25);
  border-radius: 999px; padding: 3px 14px; font-size: .8rem; margin-right: 8px;}

/* 标签胶囊 */
.tag {display: inline-block; background: #EEF2FF; color: #3730A3; border-radius: 999px;
  padding: 2px 11px; font-size: .78rem; font-weight: 600; margin-right: 6px;}
.tag.green {background: #ECFDF5; color: #047857;}
.tag.amber {background: #FFFBEB; color: #B45309;}
.tag.blue {background: #EFF6FF; color: #1D4ED8;}

/* 引用卡片 */
.chunk-quote {background: #F8FAFC; border-left: 3px solid #2563EB; border-radius: 0 12px 12px 0;
  padding: 12px 16px; color: #334155; font-size: .92rem; line-height: 1.65; margin: 6px 0 2px;}

/* 分区标题 */
.section-title {font-size: 1.05rem; font-weight: 800; color: #0F172A; margin: 4px 0 10px;}
hr {border: none; border-top: 1px solid #EEF2F7;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------- 服务与工具
@st.cache_resource
def get_services():
    """全页面共用的一套服务对象。"""
    from src.embedding import get_embedding_client
    from src.retriever import get_retriever
    from src.vector_store import get_vector_store

    return {"embedding": get_embedding_client(), "store": get_vector_store(), "retriever": get_retriever()}


services = get_services()
store = services["store"]


def domain_label(domain: str) -> str:
    label = DOMAIN_LABELS.get(domain, "")
    return f"{domain} · {label}" if label else domain


@st.cache_data(ttl=3)
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
    status["大模型"] = (bool(cfg.llm_api_key),
                      f"{cfg.llm_model}" if cfg.llm_api_key else "未配置 Key")
    return status


def status_pills() -> str:
    """首页横幅里的三个状态胶囊。"""
    pills = []
    for name, (ok, note) in system_status().items():
        icon = "✅" if ok else "❌"
        pills.append(f'<span class="pill">{icon} {name} · {note}</span>')
    return "".join(pills)


def tag(text: str, color: str = "blue") -> str:
    return f'<span class="tag {color}">{text}</span>'


# ---------------------------------------------------------------- 侧边栏
with st.sidebar:
    st.markdown(
        """
        <div style="margin: 4px 0 2px;">
          <div style="font-size:1.35rem;font-weight:800;color:#172554;">🧠 Sky Personal RAG</div>
          <div style="font-size:.82rem;color:#64748B;margin-top:2px;">个人知识库 · 检索增强问答</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.divider()
    st.markdown("**系统状态**")
    for name, (ok, note) in system_status().items():
        dot = "🟢" if ok else "🔴"
        st.markdown(f"{dot} **{name}**　<span style='color:#64748B;font-size:.85rem'>{note}</span>",
                    unsafe_allow_html=True)

    st.divider()
    st.markdown("**🔍 检索设置**")
    top_k = st.slider("每次召回知识条数（Top K）", 1, 10, cfg.top_k)
    domain_choice = st.selectbox("限定知识领域", ["全部"] + [domain_label(d) for d in DOMAINS])
    scope_choice = st.radio(
        "检索范围",
        ["仅 active", "包含归档", "仅归档"],
        horizontal=True,
        help="归档（archive）内容默认不参与回答，除非你明确要求",
    )
    st.session_state["debug_mode"] = st.toggle("🛠 调试模式", value=False,
                                               help="展示每次检索的过滤条件与召回明细")

    st.divider()
    st.markdown(f"<span style='color:#64748B;font-size:.8rem'>🧠 Sky Personal RAG · V1.x</span>",
                unsafe_allow_html=True)


def build_filters_from_sidebar() -> dict:
    filters: dict = {}
    if not domain_choice.startswith("全部"):
        filters["domain"] = domain_choice.split(" ")[0]
    if scope_choice == "包含归档":
        filters["status"] = "all"
    elif scope_choice == "仅归档":
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
            tags = [tag(f"🗂 {item.get('domain')}", "blue"),
                    tag(f"📁 {item.get('category')}", "blue")]
            if item.get("topic"):
                tags.append(tag("🏷 " + " / ".join(item["topic"])))
            tags.append(tag(f"v{item.get('version')}", "green" if item.get("status") == "active" else "amber"))
            tags.append(tag("✅ active" if item.get("status") == "active" else "🗄 archive",
                            "green" if item.get("status") == "active" else "amber"))
            st.markdown("".join(tags), unsafe_allow_html=True)
            st.markdown(f"<div class='chunk-quote'>{item['text'][:400]}"
                        f"{'……' if len(item['text']) > 400 else ''}</div>", unsafe_allow_html=True)
            st.write("")

    if st.session_state.get("debug_mode"):
        with st.expander("🛠 调试信息（为什么召回这些知识？）"):
            st.markdown("**本次查询**")
            st.code(result.question, language=None)
            st.markdown("**实际使用的 Metadata Filter**")
            st.code(result.filters or "{}", language="json")
            st.markdown(f"**召回 Top {len(result.sources)} 明细**")
            if result.sources:
                st.dataframe(
                    [{"排名": s["rank"], "分数": round(s["score"], 4), "Source": s["source"],
                      "Domain": s.get("domain"), "Category": s.get("category"),
                      "Topic": ", ".join(s.get("topic") or []), "章节": s.get("section") or "",
                      "页码": s.get("page") or "", "Version": s.get("version"), "Status": s.get("status")}
                     for s in result.sources],
                    width="stretch", hide_index=True,
                )
            st.markdown("**最终发给大模型的资料（Context）**")
            st.text(result.context[:2600] + ("……" if len(result.context) > 2600 else ""))
            st.markdown("**各环节耗时**")
            st.write({key: f"{value:.2f} 秒" for key, value in result.elapsed.items()})


# ---------------------------------------------------------------- 主区域
tab_chat, tab_upload, tab_manage = st.tabs(["💬 知识库问答", "📤 上传文档", "🗂 知识管理台"])

# ---------------- 标签页 1：问答 ----------------
with tab_chat:
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

    # 建议问题（点击即提问）
    chip_cols = st.columns(len(SUGGESTED_QUESTIONS))
    for col, suggestion in zip(chip_cols, SUGGESTED_QUESTIONS):
        with col:
            if st.button(suggestion, width="stretch", key=f"chip_{suggestion}"):
                st.session_state["ask_now"] = suggestion

    if "messages" not in st.session_state:
        st.session_state.messages = []

    for message in st.session_state.messages:
        with st.chat_message(message["role"], avatar="🙋" if message["role"] == "user" else "🧠"):
            st.markdown(message["content"])
            if message.get("result") is not None:
                show_answer_sources(message["result"])

    def _process_question(question: str) -> None:
        """完整的提问流程：检索（同步）→ 流式回答 → 来源卡片。"""
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user", avatar="🙋"):
            st.markdown(question)

        with st.chat_message("assistant", avatar="🧠"):
            if not cfg.llm_api_key:
                st.error("还没有配置 LLM API Key：请打开 .env 填好 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 三项后刷新页面。")
                st.session_state.messages.append({"role": "assistant", "content": "（未配置 LLM API Key）"})
                return
            try:
                with st.spinner("🔍 正在检索知识库……"):
                    result, deltas = answer_stream(question, top_k=top_k,
                                                   filters=build_filters_from_sidebar())
                full_text = st.write_stream(deltas)
                show_answer_sources(result)
                st.session_state.messages.append(
                    {"role": "assistant", "content": result.answer or full_text or "", "result": result}
                )
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

# ---------------- 标签页 2：上传文档 ----------------
with tab_upload:
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
    if st.button("📦 开始入库", disabled=not uploads,
                 width="stretch", type="primary"):
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
            by_domain[p.relative_to(cfg.knowledge_dir).parts[0]] = \
                by_domain.get(p.relative_to(cfg.knowledge_dir).parts[0], 0) + 1
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

# ---------------- 标签页 3：知识管理台 ----------------
with tab_manage:
    st.markdown('<div class="section-title">🗂 知识管理台</div>', unsafe_allow_html=True)
    st.caption("选中任意一行，即可查看详情并执行：重新入库 / 归档 / 恢复 / 移除")

    try:
        documents = store.list_documents()
    except VectorStoreError as exc:
        st.error(str(exc))
        documents = []

    if documents:
        # 统计卡片
        total_chunks = sum(d["chunks"] for d in documents)
        archived = sum(1 for d in documents if d["status"] == "archive")
        domains_covered = len({d["domain"] for d in documents})
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("📚 知识文件", len(documents))
        m2.metric("🧩 知识卡片", total_chunks)
        m3.metric("🌍 覆盖领域", f"{domains_covered} / {len(DOMAINS)}")
        m4.metric("🗄 归档文件", archived)

        st.divider()

        # 筛选区
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
        else:
            rows = [{"文件": d["source"], "路径": d["path"],
                     "Domain": d["domain"], "Category": d["category"],
                     "Topic": ", ".join(d["topic"]) or "-", "Version": d["version"],
                     "Status": d["status"], "卡片数": d["chunks"]} for d in filtered]
            selection = st.dataframe(
                rows, width="stretch", hide_index=True,
                on_select="rerun", selection_mode="single-row",
            )
            selected_rows = list(selection.selection.rows) if selection.selection else []
            if selected_rows:
                d = filtered[selected_rows[0]]
                st.divider()

                # 详情 + 操作区
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
                    reingest_btn = btn1.button("📥 重新入库", width="stretch",
                                               key="act_reingest", help="文件内容修改后，重新解析入库")
                    archive_btn = btn2.button("🗄 归档", width="stretch", key="act_archive",
                                              disabled=is_archived,
                                              help="移入 archive/ 目录，退出日常检索")
                    restore_btn = btn2.button("🔄 恢复", width="stretch", key="act_restore",
                                              disabled=not is_archived,
                                              help="移回原目录，重新参与检索")
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

                # 知识卡片预览
                payloads = store.chunks_by_document(d["document_id"])
                st.markdown(f"**🧩 知识卡片（{len(payloads)} 张）**")
                for payload in payloads:
                    head = (f"`{payload.get('chunk_id', '?')}`　"
                            f"章节：{payload.get('section') or '-'}　页码：{payload.get('page') or '-'}")
                    with st.expander(head):
                        st.markdown(f"<div class='chunk-quote'>{payload.get('text', '')}</div>",
                                    unsafe_allow_html=True)

        st.divider()
        # 维护操作
        with st.expander("⚙️ 维护操作（批量）"):
            confirm_rebuild = st.checkbox("我确认要清空并重建全库", key="confirm_rebuild")
            c1, c2 = st.columns(2)
            if c1.button("🔄 清空并重建知识库", disabled=not confirm_rebuild, width="stretch"):
                with st.spinner("重建中……"):
                    summary = ingest_files(rebuild=True)
                if summary.ok_files:
                    st.success(f"重建完成：{summary.ok_files} 个文件 → {summary.total_chunks} 张卡片")
                else:
                    st.error("重建失败，请看终端日志。")
            if c2.button("🧹 仅清空向量库", width="stretch"):
                store.clear()
                st.success("已清空。重新入库即可恢复。")
    else:
        st.info("知识库还是空的：先去「📤 上传文档」或运行 python ingest.py 入库。")

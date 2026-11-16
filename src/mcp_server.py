"""MCP Server：把 Sky Personal RAG 暴露为 Agent 可调用的知识工具（V3.2）。

设计原则：
- 零侵入：只 import src.pipeline / src.context 的既有入口，不改动任何核心
  RAG 代码；Streamlit 界面与命令行用法完全不受影响。
- 无状态：会话记忆不在这里维护——Agent 负责传 history；本服务每次调用独立。
- 工具粒度：完整问答（knowledge_qa）、仅检索（knowledge_search）、
  跨文档总结（knowledge_digest）三个粒度，Agent 按需选择。

运行（stdio 模式，供 Claude / Cursor / ZCode 等 MCP 客户端挂载）：
    python src/mcp_server.py

客户端配置示例（如 Claude Desktop / ZCode 的 MCP 配置）：
    {
      "mcpServers": {
        "sky-personal-rag": {
          "command": "/Users/skyzizhu/sky-rag-stu/.venv/bin/python",
          "args": ["/Users/skyzizhu/sky-rag-stu/src/mcp_server.py"]
        }
      }
    }
注意：需在本机依赖可用（Ollama 向量化、Qdrant、LLM API Key）的情况下调用；
服务就绪状态可用 knowledge_status 工具随时检查。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from contextvars import ContextVar
from pathlib import Path

# 本服务只访问本机依赖（Qdrant/Ollama）与 LLM API（已验证无代理可直连）。
# 宿主环境（如 ZCode）可能注入 SOCKS/HTTP 代理变量，会让本机 Qdrant 请求
# 报 502/缺少 socksio——启动前一律摘除。
for _proxy_key in ("http_proxy", "https_proxy", "all_proxy",
                   "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(_proxy_key, None)

# 保证作为脚本直接运行时能 import 项目包（src.*）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("sky-personal-rag")

# V3.7 身份透传：HTTP 模式下中间件验 PAT 后把调用者信息写入 contextvar；
# stdio 个人模式从不写入 → None = 不限制（行为与 V3.6 完全一致）
_request_groups: ContextVar[list[str] | None] = ContextVar("mcp_request_groups",
                                                           default=None)
_request_user: ContextVar[dict | None] = ContextVar("mcp_request_user", default=None)
_ENTRY = ("mcp_http"
          if os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
          in ("streamable-http", "streamable_http", "http")
          else "mcp_stdio")


def _current_user_groups() -> list[str] | None:
    return _request_groups.get()


def _current_username() -> str:
    """HTTP 模式返回令牌属主（缓存归属按人）；stdio 个人模式记 local。"""
    user = _request_user.get()
    return str(user.get("username") or "local") if user else "local"


def _log_qa(question: str, result, top_k: int, elapsed: float) -> None:
    """RAG 使用记录：MCP 调用同样落 SQLite。日志失败不影响回答。"""
    from src import auth_db

    user = _request_user.get() or {}
    auth_db.log_qa_event(
        username=str(user.get("username") or "local"),
        department=str(user.get("department") or ""),
        entry=_ENTRY, question=question, answer=result.answer or "",
        sources=result.sources, filters=result.filters,
        top_k=top_k, elapsed=elapsed,
    )


def _filters(domain: str | None, status: str | None) -> dict | None:
    """组装 Metadata 过滤条件；未提供任何过滤时返回 None（检索默认范围）。

    domain 可传编号或显示名称，这里解析成编号（此后全链路只认编号）。识别不了就抛
    ValueError，由工具层转成明确报错——拿未知值去过滤只会得到空结果，Agent 会误报
    「知识库里没有」。"all" 表示不限领域。
    """
    filters: dict = {}
    if domain and str(domain).strip().lower() != "all":
        from src.domains import all_codes, resolve_code

        code = resolve_code(domain)
        if code is None:
            raise ValueError(f"无法识别领域「{domain}」。可用的领域编号：{'、'.join(all_codes())}")
        filters["domain"] = code
    if status:
        filters["status"] = status
    return filters or None


def _history(history: list[dict] | None) -> list[dict]:
    """透传 Agent 侧会话历史（最近 6 条），本服务不保存任何状态。"""
    return [
        {"role": str(m.get("role", "")), "content": str(m.get("content", ""))}
        for m in (history or [])
        if m.get("role") in ("user", "assistant") and m.get("content")
    ][-6:]


def _sources_payload(sources: list[dict]) -> list[dict]:
    """来源瘦身：只保留 Agent 需要的字段，去掉渲染专用信息。"""
    return [
        {
            "rank": s.get("rank"),
            "source": s.get("source"),
            "score": round(float(s.get("score", 0)), 4),
            "path": s.get("path"),
            "section": s.get("section"),
            "page": s.get("page"),
            "domain": s.get("domain"),
            "category": s.get("category"),
            "version": s.get("version"),
            "status": s.get("status"),
            "excerpt": (s.get("text") or "")[:200],
        }
        for s in sources
    ]


def _with_domain_codes(fn):
    """V3.8：工具说明里的领域编号从注册表生成（服务启动时取值）。

    新增领域需重启 MCP 服务才会出现在说明里；但按编号过滤是实时生效的。
    只放编号——显示名称不进任何检索参数。
    """
    from src.domains import all_codes

    fn.__doc__ = (fn.__doc__ or "").replace("{DOMAIN_CODES}", "/".join(all_codes()))
    return fn


# ---------------------------------------------------------------- V3.9 MCP 双模配置
# MCP_MODE 决定本服务对外暴露哪些工具（读取 .env / 启动环境，修改需重启 MCP）：
#   query（默认）= 仅查询模式：只暴露 knowledge_search / knowledge_status。
#                  检索+筛选（四道检查）由 RAG 完成，拼装回答归调用方 Agent——
#                  零生成成本，适合「Agent 自己的 LLM 负责回答」的架构。
#   answer       = 独立回答模式：暴露 knowledge_qa / knowledge_digest / knowledge_status。
#                  RAG 全管线（含生成带引用回答），适合「直接要成品答案」的场景。
# 非法值一律回退 query。
_raw_mode = os.environ.get("MCP_MODE", "query").strip().lower()
MCP_MODE = _raw_mode if _raw_mode in ("query", "answer") else "query"
_MODE_TOOLS: dict[str, tuple[str, ...]] = {
    "query": ("knowledge_search", "knowledge_status", "get_document", "list_documents"),
    "answer": ("knowledge_qa", "knowledge_digest", "knowledge_status",
               "get_document", "list_documents"),
}
REGISTERED_TOOLS: list[str] = []


@_with_domain_codes
def knowledge_qa(
    question: str,
    top_k: int = 5,
    domain: str | None = None,
    status: str | None = None,
    history: list[dict] | None = None,
) -> str:
    """基于用户的个人知识库（含其简历、面试登记表、学习笔记、技术文档、网页收藏等私有资料）回答问题，返回带来源引用的答案。

    使用时机（满足任一即必须使用本工具，不得凭模型自身知识回答）：
    - 用户询问"我的简历/登记表/笔记/资料/文档里…"的内容
    - 用户询问涉及其个人经历、个人数据的问题（如个人工作经历、项目经验、联系方式）
    - 用户明确要求查询知识库、或问题指向用户曾收录的文档
    禁止：在未查询本工具的情况下，用模型通用知识回答上述个人资料类问题。
    权威声明：知识库检索结果即用户个人资料的权威完整来源——基于检索结果回答即可，
    除非用户明确要求，不要额外搜索用户磁盘/文件系统来"确认完整性"。
    未命中边界：检索结果为空或资料不足时，如实告知用户"知识库中没有找到相关内容"，
    不要用模型通用知识替答、也不要转而搜索磁盘；由用户决定是否换用其他途径
    （补充资料入库、或明确要求你另找途径）。
    多轮追问时请传入 history（最近几轮 user/assistant 消息），可正确解析指代。

    Args:
        question: 用户问题（不可为空）
        top_k: 召回知识卡片数量，默认 5，范围 1~10
        domain: 可选，限定知识领域编号（{DOMAIN_CODES}）
        status: 可选，知识状态过滤（active=生效 / archive=已归档 / expired=历史版本）
        history: 可选，最近对话 [{"role":"user/assistant","content":"..."}]，最多取 6 条
    """
    if not question or not question.strip():
        return json.dumps({"error": "问题不能为空"}, ensure_ascii=False)
    from src.pipeline import answer_question

    try:
        filters = _filters(domain, status)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)

    _t0 = time.time()
    result = answer_question(
        question.strip(),
        top_k=max(1, min(int(top_k), 10)),
        filters=filters,
        history=_history(history),
        user_groups=_current_user_groups(),
        username=_current_username(),
    )
    _log_qa(question.strip(), result, max(1, min(int(top_k), 10)), time.time() - _t0)
    return json.dumps(
        {
            "answer": result.answer or "知识库中没有找到足够的信息回答这个问题。",
            "sources": _sources_payload(result.sources),
            "filters_applied": result.filters,
            "refused": result.answer is None,
        },
        ensure_ascii=False,
        indent=2,
    )


@_with_domain_codes
def knowledge_search(
    question: str,
    top_k: int = 5,
    domain: str | None = None,
    status: str | None = None,
) -> str:
    """在用户的个人知识库中检索相关片段（其简历、登记表、笔记、技术文档、网页收藏等），不调用 LLM 生成答案（零生成成本、更快）。

    使用时机：需要用户私有资料的原文证据、或想在回答前先核实知识库内容时使用；
    返回片段与相关度分数，供 Agent 自行推理。
    权威声明：知识库检索结果即用户个人资料的权威完整来源——基于检索结果回答即可，
    除非用户明确要求，不要额外搜索用户磁盘/文件系统来"确认完整性"。
    枚举类问题（"我有哪些…"）以检索结果为准作答，不要转而搜索磁盘文件。
    未命中边界：检索结果为空或资料不足时，如实告知用户"知识库中没有找到相关内容"，
    不要用模型通用知识替答、也不要转而搜索磁盘；由用户决定是否换用其他途径
    （补充资料入库、或明确要求你另找途径）。

    Args:
        question: 检索问题或关键词
        top_k: 返回片段数量，默认 5，范围 1~10
        domain: 可选，限定知识领域编号（{DOMAIN_CODES}）
        status: 可选，知识状态过滤（active/archive/expired）
    """
    if not question or not question.strip():
        return json.dumps({"error": "问题不能为空"}, ensure_ascii=False)
    from src.pipeline import answer_question

    try:
        filters = _filters(domain, status)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)

    _t0 = time.time()
    result = answer_question(
        question.strip(),
        top_k=max(1, min(int(top_k), 10)),
        use_llm=False,  # 仅检索：不调用生成模型
        filters=filters,
        user_groups=_current_user_groups(),
        username=_current_username(),
    )
    _log_qa(question.strip(), result, max(1, min(int(top_k), 10)), time.time() - _t0)
    return json.dumps(
        {
            "query": question.strip(),
            "matches": [
                {
                    "rank": i,
                    "document_id": item.metadata.get("document_id"),
                    "version": item.metadata.get("version"),
                    "source": item.metadata.get("source"),
                    "path": item.metadata.get("path"),
                    "score": round(float(item.score), 4),
                    "section": item.metadata.get("section"),
                    "text": item.text[:400],
                }
                for i, item in enumerate(result.retrieved, start=1)
            ],
            "refused": not result.retrieved,
        },
        ensure_ascii=False,
        indent=2,
    )


def knowledge_digest(topic: str) -> str:
    """跨文档知识总结：围绕一个主题，综合用户知识库中的多个文档生成结构化摘要（要点+出处）。

    使用时机：用户要求"总结/归纳/梳理某主题在资料中的内容"时使用；不新建或修改知识文件。
    权威声明：知识库检索结果即用户个人资料的权威完整来源——基于检索结果回答即可，
    除非用户明确要求，不要额外搜索用户磁盘/文件系统来"确认完整性"。
    未命中边界：检索结果为空或资料不足时，如实告知用户"知识库中没有找到相关内容"，
    不要用模型通用知识替答、也不要转而搜索磁盘；由用户决定是否换用其他途径
    （补充资料入库、或明确要求你另找途径）。

    Args:
        topic: 要总结的主题（如「RAG」「智能客服」）
    """
    if not topic or not topic.strip():
        return json.dumps({"error": "主题不能为空"}, ensure_ascii=False)
    from src.pipeline import answer_question

    _t0 = time.time()
    result = answer_question(
        f"请综合知识库中与「{topic.strip()}」相关的全部内容，"
        "输出一份结构化知识总结：核心要点 + 对应出处编号。资料不足时明确说明。",
        top_k=10,
        user_groups=_current_user_groups(),
        username=_current_username(),
    )
    _log_qa(topic.strip(), result, 10, time.time() - _t0)
    return json.dumps(
        {
            "topic": topic.strip(),
            "digest": result.answer or "知识库中没有与该主题相关的足够内容。",
            "sources": _sources_payload(result.sources),
        },
        ensure_ascii=False,
        indent=2,
    )


def get_document(document_id: str, page: int = 1, page_size: int = 5,
                 mode: str = "fragments") -> str:
    """按 document_id 获取整篇文档：mode="full" 返回拼接全文（去重叠）；
    mode="fragments"（默认）按卡片分页返回片段。不调用 LLM，零生成成本。

    document_id 来自 knowledge_search 的 matches（每条都带 document_id）。
    检索范围遵循调用者令牌的可见部门；无权限或文档不存在统一返回 refused，不做区分。

    Args:
        document_id: 文档编号（knowledge_search 结果中的 document_id 字段）
        page: 片段模式的页码（从 1 起）
        page_size: 片段模式每页卡片数（1~20，默认 5）
        mode: "fragments"（分页片段）或 "full"（拼接全文）
    """
    import math

    from src.context import _merge_run
    from src.permissions import scope_filters
    from src.retriever import RetrievedItem
    from src.vector_store import get_vector_store

    document_id = (document_id or "").strip()
    if not document_id:
        return json.dumps({"error": "document_id 不能为空"}, ensure_ascii=False)
    mode = (mode or "fragments").strip().lower()
    if mode not in ("fragments", "full"):
        return json.dumps({"error": "mode 仅支持 fragments / full"}, ensure_ascii=False)

    filters = scope_filters({"document_id": document_id, "status": "active"},
                            _current_user_groups())
    try:
        cards = get_vector_store().get_all_cards(filters=filters, limit=500)
    except Exception as exc:
        return json.dumps({"error": f"知识库访问失败：{exc}"}, ensure_ascii=False)
    if not cards:
        return json.dumps({"refused": True,
                           "reason": "无此 document_id，或文档未生效/当前令牌无权限"},
                          ensure_ascii=False)

    def _seq(card) -> int:
        m = re.search(r"(\d{4})$", card["payload"].get("chunk_id") or "")
        return int(m.group(1)) if m else 0

    cards.sort(key=_seq)
    meta = cards[0]["payload"]

    if mode == "full":
        run = [RetrievedItem(rank=i, score=0.0, text=c["payload"]["text"],
                             metadata={k: v for k, v in c["payload"].items() if k != "text"})
               for i, c in enumerate(cards)]
        full = _merge_run(run)
        return json.dumps(
            {"document_id": document_id, "source": meta.get("source"),
             "domain": meta.get("domain"), "category": meta.get("category"),
             "version": meta.get("version"), "total_cards": len(cards),
             "total_chars": len(full), "full_text": full},
            ensure_ascii=False, indent=2)

    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), 20)
    total_pages = math.ceil(len(cards) / page_size)
    page = min(page, total_pages)
    slice_ = cards[(page - 1) * page_size: page * page_size]
    return json.dumps(
        {"document_id": document_id, "source": meta.get("source"),
         "version": meta.get("version"), "page": page, "page_size": page_size,
         "total_cards": len(cards), "total_pages": total_pages,
         "fragments": [
             {"chunk_id": c["payload"].get("chunk_id"),
              "section": c["payload"].get("section"),
              "text": c["payload"].get("text", "")}
             for c in slice_
         ]},
        ensure_ascii=False, indent=2)


def list_documents(domain: str | None = None, status: str = "active") -> str:
    """枚举知识库目录：按权限列出文档（编号/文件名/领域/分类/版本/状态/卡片数）。
    不调用 LLM。用途：生成方案前先了解知识库里有哪些资料可供检索。

    Args:
        domain: 可选，限定知识领域编号（{DOMAIN_CODES}）；"all" 表示不限
        status: 知识状态过滤（active/archive/expired/all，默认 active）
    """
    from src.permissions import scope_filters
    from src.vector_store import get_vector_store

    try:
        filters = _filters(domain, status)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    filters = scope_filters(filters, _current_user_groups())
    allowed = set((filters.get("allowed_groups") or []))
    want_domain = filters.get("domain")
    want_status = filters.get("status")

    docs = []
    for d in get_vector_store().list_documents():
        if allowed and not (set(d.get("allowed_groups") or ["all"]) & allowed):
            continue
        if want_domain and d.get("domain") != want_domain:
            continue
        if want_status and d.get("status") != want_status:
            continue
        docs.append({"document_id": d.get("document_id"), "source": d.get("source"),
                     "domain": d.get("domain"), "category": d.get("category"),
                     "version": d.get("version"), "status": d.get("status"),
                     "chunks": d.get("chunks"), "indexed_at": d.get("indexed_at")})
    return json.dumps({"total": len(docs), "documents": docs},
                      ensure_ascii=False, indent=2)


def knowledge_status() -> str:
    """检查知识库服务就绪状态：向量化模型、向量数据库、卡片总数。

    适用：调用其他知识工具前确认依赖可用；或排查「查不到结果」时先看这里。
    """
    from src.pipeline import answer_question  # noqa: F401  确认核心可导入
    from src.vector_store import get_vector_store

    payload: dict = {"vector_store": "online", "cards": 0, "embedding": "unknown", "llm_key": False}
    try:
        payload["cards"] = get_vector_store().count()
    except Exception as exc:
        payload["vector_store"] = f"offline: {exc}"
    try:
        from src.embedding import get_embedding_client

        payload["embedding"] = (
            "online"
            if get_embedding_client().model_available()
            else "missing model"
        )
    except Exception as exc:
        payload["embedding"] = f"offline: {exc}"
    from src.config import get_config

    payload["llm_key"] = bool(get_config().llm_api_key)
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---- 按 MCP_MODE 注册工具（V3.9 双模配置：见本文件上方 MCP_MODE 说明）----
for _tool_name in _MODE_TOOLS[MCP_MODE]:
    mcp.tool()(globals()[_tool_name])
    REGISTERED_TOOLS.append(_tool_name)


# ---------------------------------------------------------------- V3.7 HTTP 传输 + PAT 身份透传
class _TokenAuthMiddleware:
    """ASGI 中间件：校验 Authorization: Bearer mcp_pat_...

    通过验证 → 把调用者的可见部门写入 contextvar（工具内读取并注入检索过滤）；
    无效/缺失令牌 → 401。令牌只发给员工（admins 表无令牌）——
    MCP 身份永远按部门过滤，没有"看全库"的特例。
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        auth_header = headers.get("authorization", "")
        token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        from src import auth_db

        ident = auth_db.verify_token(token) if token else None
        if ident is None:
            payload = json.dumps(
                {"error": "invalid or missing MCP token",
                 "hint": (f"在管理后台「访问令牌」页生成，配置到 MCP 客户端 Authorization 头；"
                          f"当前服务模式 MCP_MODE={MCP_MODE}"
                          f"（{'仅查询：search/status' if MCP_MODE == 'query' else '独立回答：qa/digest/status'}）")},
                ensure_ascii=False,
            ).encode("utf-8")
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                    (b"content-length", str(len(payload)).encode()),
                                    (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": payload})
            return
        from src.permissions import visible_groups
        groups = visible_groups(ident["department"])
        _request_groups.set(groups)
        _request_user.set({"username": ident["username"],
                           "department": ident["department"]})
        await self.app(scope, receive, send)

    def __init__(self, app):
        self.app = app


def _run_http() -> None:
    """streamable-http 模式：PAT 鉴权 + 团队共享。默认端口 8760（MCP_HTTP_PORT 可改）。"""
    import uvicorn

    auth_db_init = __import__("src.auth_db", fromlist=["init_db"])
    auth_db_init.init_db()
    asgi_app = _TokenAuthMiddleware(mcp.streamable_http_app())
    host = os.environ.get("MCP_HTTP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_HTTP_PORT", "8760"))
    print(f"📡 MCP Server (streamable-http) 监听 http://{host}:{port}/mcp（PAT 鉴权）"
          f"｜模式 MCP_MODE={MCP_MODE}｜工具：{'、'.join(REGISTERED_TOOLS)}")
    uvicorn.run(asgi_app, host=host, port=port)


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
    if transport in ("streamable-http", "streamable_http", "http"):
        _run_http()
    else:
        mcp.run()  # stdio 模式（默认），供 MCP 客户端以子进程方式挂载

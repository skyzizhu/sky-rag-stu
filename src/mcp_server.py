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

import functools
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

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# 关闭 SDK 默认的 DNS 重绑定防护（它只放行 localhost/127.0.0.1 的 Host 头，
# 局域网 IP 访问会 421 Misdirected Request）。真实鉴权由 _TokenAuthMiddleware
# 对每个请求强制校验 PAT——重绑定防护针对的是"无鉴权本机服务"，此处多余。
mcp = FastMCP("sky-personal-rag", transport_security=TransportSecuritySettings(
    enable_dns_rebinding_protection=False,
))

# V3.7 身份透传：HTTP 模式下中间件验 PAT 后把调用者信息写入 contextvar；
# stdio 个人模式从不写入 → None = 不限制（行为与 V3.6 完全一致）
_request_groups: ContextVar[list[str] | None] = ContextVar("mcp_request_groups",
                                                           default=None)
_request_user: ContextVar[dict | None] = ContextVar("mcp_request_user", default=None)
_ENTRY = ("mcp_http"
          if os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
          in ("streamable-http", "streamable_http", "http")
          else "mcp_stdio")


def _identity_from_ctx(ctx) -> tuple[list[str] | None, str, dict]:
    """从当前调用的请求重新验证令牌，派生本次调用的身份（V3.9 修复会话身份冻结）。

    背景：streamable-http 会话任务在 initialize 请求的上下文中启动，此前依赖的
    contextvar 身份会被冻结为会话创建者——同一会话内的后续请求即使换了合法令牌，
    权限范围与审计署名仍是会话创建者。修复：每条消息的元数据都携带当次 HTTP 请求
    （ServerMessageMetadata.request_context），工具经 ctx.request_context.request
    读取 Authorization 头逐次验证。stdio / 直接调用（ctx 为 None 或无 request）
    回退到 contextvar（个人模式为 None = 不限制）。

    返回 (user_groups, username, user_dict)。
    """
    try:
        request = ctx.request_context.request if ctx is not None else None
    except Exception:
        request = None
    if request is not None:
        auth_header = (request.headers.get("authorization") or "")
        token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        if token:
            from src import auth_db
            from src.permissions import visible_groups
            ident = auth_db.verify_token(token)
            if ident:
                groups = visible_groups(ident.get("department_group", ident["department"]))
                user = {"username": ident["username"], "department": ident["department"]}
                return groups, str(ident["username"]), user
    # 兜底：中间件写入的 contextvar（stdio 个人模式 = None/空）
    groups = _request_groups.get()
    user = _request_user.get() or {}
    return groups, str(user.get("username") or "local"), user


def _current_user_groups() -> list[str] | None:
    return _request_groups.get()


def _current_username() -> str:
    """HTTP 模式返回令牌属主（缓存归属按人）；stdio 个人模式记 local。"""
    user = _request_user.get()
    return str(user.get("username") or "local") if user else "local"


def _log_qa(question: str, result, top_k: int, elapsed: float,
            username: str | None = None, department: str | None = None) -> None:
    """RAG 使用记录：MCP 调用同样落 SQLite。日志失败不影响回答。

    username/department：调用方从 ctx 逐次验出的身份（V3.9）；缺省回退 contextvar。
    """
    from src import auth_db
    from src.usage_reporting import usage_for_log

    user = _request_user.get() or {}
    username = username if username is not None else str(user.get("username") or "local")
    department = department if department is not None else str(user.get("department") or "")
    auth_db.log_qa_event(
        username=username,
        department=department,
        entry=_ENTRY, question=question, answer=result.answer or "",
        sources=result.sources, filters=result.filters,
        top_k=top_k, elapsed=elapsed,
        usage=usage_for_log(result),
    )


def _filters(domain: str | None, status: str | None, *,
             doc_type: str | None = None) -> dict | None:
    """组装 Metadata 过滤条件；未提供任何过滤时返回 None（检索默认范围）。

    domain 可传编号或显示名称，这里解析成编号（此后全链路只认编号）。识别不了就抛
    ValueError，由工具层转成明确报错——拿未知值去过滤只会得到空结果，Agent 会误报
    「知识库里没有」。"all" 表示不限领域。
    doc_type 是独立的原始文档类型标签，去首尾空白后精确匹配，不做别名合并；
    留空 / all 表示不限，__unclassified__ 表示缺失 / null / 空字符串。
    """
    filters: dict = {}
    if domain and str(domain).strip().lower() != "all":
        from src.domains import all_codes, resolve_code

        code = resolve_code(domain)
        if code is None:
            raise ValueError(f"无法识别领域「{domain}」。可用的领域编号：{'、'.join(all_codes())}")
        filters["domain"] = code
    if status:
        status = str(status).strip().lower()
        if status != "all":
            from src.metadata import STATUSES
            if status not in STATUSES:
                raise ValueError(f"无法识别状态「{status}」。可用值：{'、'.join(STATUSES)} 或 all")
            filters["status"] = status
    from src.document_type import normalize_doc_type_filter

    normalized_type = normalize_doc_type_filter(doc_type)
    if normalized_type is not None:
        filters["doc_type"] = normalized_type
    return filters or None


def _history(history: list[dict] | None) -> list[dict]:
    """透传 Agent 侧会话历史（最近 6 条），本服务不保存任何状态。"""
    return [
        {"role": str(m.get("role", "")), "content": str(m.get("content", ""))}
        for m in (history or [])
        if m.get("role") in ("user", "assistant") and m.get("content")
    ][-6:]


def _sources_payload(sources: list[dict]) -> list[dict]:
    """来源瘦身：只保留 Agent 需要的字段，去掉渲染专用信息。channels 标明
    来源来自哪个检索通道（向量/关键词/文档展开/分批提取），与相关度同尺可解读。"""
    return [
        {
            "rank": s.get("rank"),
            "source": s.get("source"),
            "score": round(float(s.get("score", 0)), 4),
            "channels": s.get("channels"),
            "path": s.get("path"),
            "section": s.get("section"),
            "page": s.get("page"),
            "domain": s.get("domain"),
            "category": s.get("category"),
            "doc_type": s.get("doc_type") or "",
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
# 非法值一律回退 query（启动日志打印警告）。
from dotenv import load_dotenv
load_dotenv()  # 显式加载 .env——MCP_MODE/_ENTRY 必须在读取环境变量之前生效
_raw_mode = os.environ.get("MCP_MODE", "query").strip().lower()
if _raw_mode in ("query", "answer"):
    MCP_MODE = _raw_mode
else:
    MCP_MODE = "query"
    print(f"⚠️ MCP_MODE={_raw_mode!r} 非法（仅限 query/answer），已回退 query")
_MODE_TOOLS: dict[str, tuple[str, ...]] = {
    "query": ("knowledge_search", "knowledge_status", "get_document", "list_documents"),
    "answer": ("knowledge_qa", "knowledge_digest", "knowledge_status",
               "get_document", "list_documents"),
}
REGISTERED_TOOLS: list[str] = []


@_with_domain_codes
def _tool_knowledge_qa_impl(
    question: str,
    top_k: int = 5,
    domain: str | None = None,
    status: str | None = None,
    history: list[dict] | None = None,
    ctx=None,
    *, doc_type: str | None = None,
) -> str:
    """基于用户的个人知识库（含其简历、面试登记表、学习笔记、技术文档、网页收藏等私有资料）回答问题，返回带来源引用的答案。

    使用时机（满足任一即必须使用本工具，不得凭模型自身知识回答）：
    - 用户询问"我的简历/登记表/笔记/资料/文档里…"的内容
    - 用户询问涉及其个人经历、个人数据的问题（如个人工作经历、项目经验、联系方式）
    - 用户明确要求查询知识库、或问题指向用户曾收录的文档
    禁止：在未查询本工具的情况下，用模型通用知识回答上述个人资料类问题。
    证据边界：知识库检索结果是可引用的私有资料证据；coverage=partial/unknown 时不能声称已列全或给出全库确定总数。
    除非用户明确要求，不要额外搜索用户磁盘/文件系统。
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
        doc_type: 可选，文档类型原始标签（去首尾空白，最多 80 字符，精确匹配、不合并别名）；留空/all 不限，__unclassified__ 仅查未分类
    """
    if not question or not question.strip():
        return json.dumps({"error": "问题不能为空"}, ensure_ascii=False)
    from src.pipeline import answer_question

    groups, username, user = _identity_from_ctx(ctx)

    try:
        filters = _filters(domain, status, doc_type=doc_type)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)

    _t0 = time.time()
    result = answer_question(
        question.strip(),
        top_k=max(1, min(int(top_k), 10)),
        filters=filters,
        history=_history(history),
        user_groups=groups,
        username=username,
    )
    _log_qa(question.strip(), result, max(1, min(int(top_k), 10)), time.time() - _t0,
            username=username, department=str(user.get("department") or ""))
    return json.dumps(
        {
            "answer": result.answer or "知识库中没有找到足够的信息回答这个问题。",
            "coverage": (getattr(result, "evidence_meta", None) or {}).get("coverage", "unknown"),
            "coverage_details": getattr(result, "evidence_meta", None) or {},
            "items": getattr(result, "enumerated_items", []),
            "sources": _sources_payload(result.sources),
            "filters_applied": result.filters,
            "refused": result.answer is None,
        },
        ensure_ascii=False,
        indent=2,
    )


@_with_domain_codes
def _tool_knowledge_search_impl(
    question: str,
    top_k: int = 5,
    domain: str | None = None,
    status: str | None = None,
    ctx=None,
    *, doc_type: str | None = None,
) -> str:
    """在用户的个人知识库中检索相关片段（其简历、登记表、笔记、技术文档、网页收藏等），不调用 LLM 生成答案（零生成成本、更快）。

    使用时机：需要用户私有资料的原文证据、或想在回答前先核实知识库内容时使用；
    返回片段与相关度分数，供 Agent 自行推理。
    分数语义：score 为混合检索（向量+BM25 关键词）经 RRF 融合归一化的相关度，
    0~1 之间可比（双通道同命中更高）；channels 字段标明证据来自哪个检索通道。
    证据边界：知识库检索结果是可引用的私有资料证据；coverage=partial/unknown 时不能声称已列全或给出全库确定总数。
    除非用户明确要求，不要额外搜索用户磁盘/文件系统。
    枚举类问题（"我有哪些…"）以检索结果为准作答，不要转而搜索磁盘文件。
    解释型提问（针对具体主题问"四类痛点/三大特性是什么"）同样适用本工具——
    结果以描述该主题的资料为准。
    未命中边界：检索结果为空或资料不足时，如实告知用户"知识库中没有找到相关内容"，
    不要用模型通用知识替答、也不要转而搜索磁盘；由用户决定是否换用其他途径
    （补充资料入库、或明确要求你另找途径）。

    Args:
        question: 检索问题或关键词
        top_k: 返回片段数量，默认 5，范围 1~10
        domain: 可选，限定知识领域编号（{DOMAIN_CODES}）
        status: 可选，知识状态过滤（active/archive/expired）
        doc_type: 可选，文档类型原始标签（去首尾空白，最多 80 字符，精确匹配、不合并别名）；留空/all 不限，__unclassified__ 仅查未分类
    """
    if not question or not question.strip():
        return json.dumps({"error": "问题不能为空"}, ensure_ascii=False)
    from src.pipeline import answer_question

    try:
        filters = _filters(domain, status, doc_type=doc_type)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)

    groups, username, user = _identity_from_ctx(ctx)
    _t0 = time.time()
    result = answer_question(
        question.strip(),
        top_k=max(1, min(int(top_k), 10)),
        use_llm=False,  # 仅检索：不调用生成模型
        filters=filters,
        user_groups=groups,
        username=username,
    )
    _log_qa(question.strip(), result, max(1, min(int(top_k), 10)), time.time() - _t0,
            username=username, department=str(user.get("department") or ""))
    return json.dumps(
        {
            "query": question.strip(),
            "coverage": (getattr(result, "evidence_meta", None) or {}).get("coverage", "unknown"),
            "coverage_details": getattr(result, "evidence_meta", None) or {},
            "matches": [
                {
                    "rank": i,
                    "document_id": item.metadata.get("document_id"),
                    "version": item.metadata.get("version"),
                    "source": item.metadata.get("source"),
                    "path": item.metadata.get("path"),
                    "doc_type": item.metadata.get("doc_type") or "",
                    "score": round(float(item.score), 4),
                    "channels": item.channels,
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


def _tool_knowledge_digest_impl(topic: str, domain: str | None = None,
                                status: str | None = None, top_k: int = 10,
                                ctx=None, *, doc_type: str | None = None) -> str:
    """跨文档知识总结：围绕一个主题，综合用户知识库中的多个文档生成结构化摘要（要点+出处）。

    使用时机：用户要求"总结/归纳/梳理某主题在资料中的内容"时使用；不新建或修改知识文件。
    证据边界：知识库检索结果是可引用的私有资料证据；coverage=partial/unknown 时不能声称已列全或给出全库确定总数。
    除非用户明确要求，不要额外搜索用户磁盘/文件系统。
    未命中边界：检索结果为空或资料不足时，如实告知用户"知识库中没有找到相关内容"，
    不要用模型通用知识替答、也不要转而搜索磁盘；由用户决定是否换用其他途径
    （补充资料入库、或明确要求你另找途径）。

    Args:
        topic: 要总结的主题（如「RAG」「智能客服」）
        domain: 可选，限定知识领域编号（{DOMAIN_CODES}）
        status: 可选，知识状态过滤（active/archive/expired）
        top_k: 综合的资料卡片数量，默认 10，范围 1~20
        doc_type: 可选，文档类型原始标签（去首尾空白，最多 80 字符，精确匹配、不合并别名）；留空/all 不限，__unclassified__ 仅查未分类
    """
    if not topic or not topic.strip():
        return json.dumps({"error": "主题不能为空"}, ensure_ascii=False)
    from src.pipeline import answer_question

    # V3.9 修复：此前漏调身份提取——groups/username/user 未定义（调用即 NameError），
    # 且部门权限过滤从未注入（跨文档总结同样必须遵循令牌可见范围）
    groups, username, user = _identity_from_ctx(ctx)
    try:
        filters = _filters(domain, status, doc_type=doc_type)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)

    _t0 = time.time()
    result = answer_question(
        f"请综合知识库中与「{topic.strip()}」相关的全部内容，"
        "输出一份结构化知识总结：核心要点 + 对应出处编号。资料不足时明确说明。",
        top_k=max(1, min(int(top_k), 20)),
        filters=filters,
        user_groups=groups,
        username=username,
    )
    _log_qa(topic.strip(), result, max(1, min(int(top_k), 20)), time.time() - _t0,
            username=username, department=str(user.get("department") or ""))
    return json.dumps(
        {
            "topic": topic.strip(),
            "digest": result.answer or "知识库中没有与该主题相关的足够内容。",
            "items": getattr(result, "enumerated_items", []),
            "coverage": (getattr(result, "evidence_meta", None) or {}).get("coverage", "unknown"),
            "coverage_details": getattr(result, "evidence_meta", None) or {},
            "sources": _sources_payload(result.sources),
            "refused": result.answer is None,
        },
        ensure_ascii=False,
        indent=2,
    )


def _tool_get_document_impl(document_ids: list[str], page: int = 1, page_size: int = 5,
                            mode: str = "fragments", section: str | None = None,
                            ctx=None, *, doc_type: str | None = None) -> str:
    """按 document_id 获取一篇或多篇文档：mode="full" 返回每篇拼接全文（去重叠）；
    mode="fragments"（默认）返回卡片片段，单文档时按页分页、多文档时按文档分组全量返回。
    可选 section 过滤：只返回章节名包含该关键词的内容（大小写不敏感）。
    不调用 LLM，零生成成本。

    document_ids 来自 knowledge_search 的 matches（每条都带 document_id），可传一篇或多篇。
    检索范围遵循调用者令牌的可见部门；无权限或文档不存在统一归入 not_found，不做区分。

    Args:
        document_ids: 文档编号列表（knowledge_search 结果中的 document_id 字段，1~10 篇）
        page: fragments 模式、单文档时的页码（从 1 起；多文档时不分页）
        page_size: fragments 模式每页卡片数（1~20，默认 5）
        mode: "fragments"（分页片段）或 "full"（拼接全文）
        section: 可选，章节名关键词——只返回章节名包含它的内容（大小写不敏感）
        doc_type: 可选，文档类型原始标签（去首尾空白，最多 80 字符，精确匹配、不合并别名）；留空/all 不限，__unclassified__ 仅查未分类；始终仅取 active 且有权限的文档
    """
    import math

    from src.context import _merge_run
    from src.permissions import scope_filters
    from src.retriever import RetrievedItem
    from src.vector_store import get_vector_store

    if isinstance(document_ids, str):  # 容错：直接调用时允许传单个字符串
        document_ids = [document_ids]
    wanted = [str(d).strip() for d in (document_ids or []) if str(d or "").strip()]
    wanted = list(dict.fromkeys(wanted))[:10]  # 去重，避免同一篇重复返回/重复审计
    if not wanted:
        return json.dumps({"error": "document_ids 不能为空"}, ensure_ascii=False)
    mode = (mode or "fragments").strip().lower()
    if mode not in ("fragments", "full"):
        return json.dumps({"error": "mode 仅支持 fragments / full"}, ensure_ascii=False)
    section_kw = (section or "").strip().lower()

    try:
        doc_filters = {**(_filters(None, "active", doc_type=doc_type) or {}),
                       "document_id": wanted}
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    groups, username, user = _identity_from_ctx(ctx)
    filters = scope_filters(doc_filters, groups)
    truncated = False
    try:
        cards = get_vector_store().get_all_cards(filters=filters, limit=2000)
        truncated = len(cards) >= 2000  # 触顶：响应可能不完整，明确告知 Agent
    except Exception as exc:
        return json.dumps({"error": f"知识库访问失败：{exc}"}, ensure_ascii=False)

    def _seq(card) -> int:
        m = re.search(r"(\d{4})$", card["payload"].get("chunk_id") or "")
        return int(m.group(1)) if m else 0

    # 按文档分组（保持请求顺序），组内按卡片序号排序
    by_doc: dict[str, list] = {}
    for card in cards:
        by_doc.setdefault(card["payload"].get("document_id"), []).append(card)
    ordered_ids = [d for d in wanted if d in by_doc]
    not_found = [d for d in wanted if d not in by_doc]

    from src import auth_db
    for d in ordered_ids:
        meta = by_doc[d][0]["payload"]
        try:
            auth_db.log_doc_access(username=username,
                                   department=str(user.get("department") or ""),
                                   entry=_ENTRY, action="read_document",
                                   document_id=d,
                                   source=str(meta.get("source") or ""),
                                   detail={"mode": mode,
                                           "section": section_kw or None,
                                           "cards": len(by_doc[d])})
        except Exception:
            pass  # 审计失败不影响取数

    documents = []
    section_missed: list[str] = []
    for d in ordered_ids:
        cards = sorted(by_doc[d], key=_seq)
        if section_kw:  # 章节过滤：只保留章节名含关键词的卡片（大小写不敏感）
            matched = [c for c in cards
                       if section_kw in str(c["payload"].get("section") or "").lower()]
            if not matched:
                section_missed.append(d)  # 文档可见但无匹配章节：单独列出，不算 not_found
                continue
            cards = matched
        meta = cards[0]["payload"]
        cards = sorted(cards, key=_seq)
        base = {"document_id": d, "source": meta.get("source"),
                "title": meta.get("title"), "owner": meta.get("owner"),
                "effective_date": meta.get("effective_date"),
                "domain": meta.get("domain"), "category": meta.get("category"),
                "doc_type": meta.get("doc_type") or "",
                "version": meta.get("version"), "total_cards": len(cards),
                "sections": sorted({str(c["payload"].get("section") or "")
                                    for c in cards if c["payload"].get("section")})}
        if mode == "full":
            run = [RetrievedItem(rank=i, score=0.0, text=c["payload"]["text"],
                                 metadata={k: v for k, v in c["payload"].items() if k != "text"})
                   for i, c in enumerate(cards)]
            full = _merge_run(run)
            documents.append({**base, "full_text": full, "total_chars": len(full)})
        else:
            fragments = [{"chunk_id": c["payload"].get("chunk_id"),
                          "section": c["payload"].get("section"),
                          "text": c["payload"].get("text", "")}
                         for c in cards]
            if len(ordered_ids) == 1:  # 分页仅在单文档时生效；多文档按文档分组全量返回
                try:
                    page_size = min(max(1, int(page_size)), 20)
                except (TypeError, ValueError):
                    page_size = 5
                total_pages = math.ceil(len(fragments) / page_size)
                try:
                    page = min(max(1, int(page)), total_pages)
                except (TypeError, ValueError):
                    page = 1
                documents.append({**base,
                                  "page": page, "page_size": page_size,
                                  "total_pages": total_pages,
                                  "fragments": fragments[(page - 1) * page_size: page * page_size]})
            else:
                documents.append({**base, "fragments": fragments})

    result: dict = {"mode": mode, "total": len(documents), "documents": documents,
                    "not_found": not_found, "truncated": truncated}
    if section_missed:
        result["section_not_matched"] = section_missed  # 可见但无匹配章节的文档
    if not documents:
        result["refused"] = True
        if section_kw and not_found == wanted:
            result["reason"] = "请求的文档均不存在、未生效或当前令牌无权限"
        elif section_kw:
            result["reason"] = ("章节过滤无匹配（文档存在且可见，但没有章节名包含该关键词）；"
                                 "可先不带 section 调用一次查看 sections 可用章节名")
        else:
            result["reason"] = "请求的文档均不存在、未生效或当前令牌无权限"
    if len(ordered_ids) == 1 and documents:
        result.update(documents[0])  # 单文档模式：字段提到顶层（full/fragments 一致），便于阅读
        result.pop("documents")       # 扁平化后移除嵌套，避免同一内容重复两份
    return json.dumps(result, ensure_ascii=False, indent=2)


def _tool_list_documents_impl(domain: str | None = None, status: str = "active",
                              ctx=None, *, doc_type: str | None = None) -> str:
    """枚举知识库目录：按权限列出文档（编号/文件名/领域/分类/文档类型/版本/状态/卡片数）。
    不调用 LLM。用途：生成方案前先了解知识库里有哪些资料可供检索。

    Args:
        domain: 可选，限定知识领域编号（{DOMAIN_CODES}）；"all" 表示不限
        status: 知识状态过滤（active/archive/expired/all，默认 active）
        doc_type: 可选，文档类型原始标签（去首尾空白，最多 80 字符，精确匹配、不合并别名）；留空/all 不限，__unclassified__ 仅查缺失/null/空字符串
    """
    from src.permissions import scope_filters
    from src.vector_store import get_vector_store

    try:
        filters = _filters(domain, status, doc_type=doc_type)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    groups, username, user = _identity_from_ctx(ctx)
    filters = scope_filters(filters, groups) or {}
    allowed = set(filters.get("allowed_groups") or [])
    want_domain = filters.get("domain")
    want_status = filters.get("status")  # "all" 已在 _filters 中剔除，不再过滤
    want_type = filters.get("doc_type")

    docs = []
    for d in get_vector_store().list_documents():
        if allowed and not (set(d.get("allowed_groups") or ["all"]) & allowed):
            continue
        if want_domain and d.get("domain") != want_domain:
            continue
        if want_status and d.get("status") != want_status:
            continue
        stored_type = d.get("doc_type") or ""
        if want_type == "__unclassified__":
            if stored_type != "":
                continue
        elif want_type is not None and stored_type != want_type:
            continue
        docs.append({"document_id": d.get("document_id"), "source": d.get("source"),
                     "title": d.get("title"), "owner": d.get("owner"),
                     "effective_date": d.get("effective_date"),
                     "domain": d.get("domain"), "category": d.get("category"),
                     "doc_type": stored_type,
                     "version": d.get("version"), "status": d.get("status"),
                     "chunks": d.get("chunks"), "indexed_at": d.get("indexed_at")})
    from src import auth_db
    try:
        auth_db.log_doc_access(username=username,
                               department=str(user.get("department") or ""),
                               entry=_ENTRY, action="list_documents",
                               detail={"domain": want_domain, "status": want_status,
                                       "doc_type": want_type, "total": len(docs)})
    except Exception:
        pass  # 审计失败不影响枚举
    return json.dumps({"total": len(docs), "documents": docs},
                      ensure_ascii=False, indent=2)


def _tool_knowledge_status_impl() -> str:
    """检查知识库服务就绪状态：向量化模型、向量数据库、卡片总数、精排模型、MCP 模式与工具集。

    适用：调用其他知识工具前确认依赖可用；或排查「查不到结果」时先看这里。
    rerank 字段说明精排通道：backend=local_onnx 时 ready=false 表示本地模型文件缺失
    （检索会退化为无精排排序）；backend=llm 时 ready 跟随大模型 Key 是否已配置。
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

    cfg = get_config()
    payload["llm_key"] = bool(cfg.llm_api_key)
    payload["embedding_model"] = cfg.embedding_model
    payload["llm_model"] = cfg.llm_model
    # 精排通道就绪状态（对齐前端系统状态灯）：local_onnx 检查模型文件；llm 后端恒可用
    rerank_backend = (cfg.rerank_backend or "llm").strip().lower()
    if rerank_backend == "local_onnx":
        rerank_ready = ((cfg.rerank_model_dir / "model_quantized.onnx").exists()
                        and (cfg.rerank_model_dir / "tokenizer.json").exists())
        payload["rerank"] = {"backend": "local_onnx", "ready": rerank_ready,
                             "model": cfg.rerank_model_dir.name,
                             "hint": "" if rerank_ready else
                             "模型文件缺失——运行 python scripts/download_rerank_model.py"}
    else:
        payload["rerank"] = {"backend": "llm", "ready": bool(cfg.llm_api_key),
                             "model": cfg.llm_model}
    # MCP 自身信息：模式与本次暴露的工具集（Agent 据此决定调用策略）
    payload["mcp_mode"] = MCP_MODE
    payload["tools"] = list(REGISTERED_TOOLS)
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---- V3.9 异步公开工具：async 包装 + 线程池执行 ----
# 修复两个问题：
# ① 同步工具直接在事件循环上执行——一次生成（数十秒）会阻塞所有并发客户端；
# ② HTTP 会话任务的 contextvar 身份冻结在会话创建者——async 包装把 ctx（携带
#    当次请求）传入工作线程，impl 内经 _identity_from_ctx(ctx) 逐次验令牌。
# 工具的 schema/文档由 impl 的签名与 docstring 提供（__doc__ 在下方运行时复制）。

async def knowledge_qa(question: str, top_k: int = 5, domain: str | None = None,
                       status: str | None = None,
                       history: list[dict] | None = None,
                       ctx: Context | None = None, *, doc_type: str | None = None) -> str:
    """基于用户的个人知识库回答问题（带来源引用；详见实现说明）。"""
    return await anyio.to_thread.run_sync(
        functools.partial(_tool_knowledge_qa_impl, question, top_k, domain, status,
                          history, ctx, doc_type=doc_type))


async def knowledge_search(question: str, top_k: int = 5, domain: str | None = None,
                           status: str | None = None,
                           ctx: Context | None = None, *, doc_type: str | None = None) -> str:
    """在用户的个人知识库中检索相关片段（不调用 LLM，详见实现说明）。"""
    return await anyio.to_thread.run_sync(
        functools.partial(_tool_knowledge_search_impl, question, top_k, domain,
                          status, ctx, doc_type=doc_type))


async def knowledge_digest(topic: str, domain: str | None = None,
                           status: str | None = None, top_k: int = 10,
                           ctx: Context | None = None, *, doc_type: str | None = None) -> str:
    """跨文档知识总结：围绕一个主题综合多文档生成结构化摘要（详见实现说明）。"""
    return await anyio.to_thread.run_sync(
        functools.partial(_tool_knowledge_digest_impl, topic, domain, status,
                          top_k, ctx, doc_type=doc_type))


async def get_document(document_ids: list[str], page: int = 1, page_size: int = 5,
                       mode: str = "fragments", section: str | None = None,
                       ctx: Context | None = None, *, doc_type: str | None = None) -> str:
    """按 document_id 获取一篇或多篇文档的全文/分页片段（详见实现说明）。"""
    return await anyio.to_thread.run_sync(
        functools.partial(_tool_get_document_impl, document_ids, page, page_size,
                          mode, section, ctx, doc_type=doc_type))


async def list_documents(domain: str | None = None, status: str = "active",
                         ctx: Context | None = None, *, doc_type: str | None = None) -> str:
    """按权限枚举知识库目录（详见实现说明）。"""
    return await anyio.to_thread.run_sync(
        functools.partial(_tool_list_documents_impl, domain, status, ctx,
                          doc_type=doc_type))


async def knowledge_status(ctx: Context | None = None) -> str:
    """检查知识库服务就绪状态（详见实现说明）。"""
    return await anyio.to_thread.run_sync(_tool_knowledge_status_impl)


# docstring 归位：impl 上维护完整说明（含 {DOMAIN_CODES} 替换），公开函数继承展示
knowledge_qa.__doc__ = _tool_knowledge_qa_impl.__doc__
knowledge_search.__doc__ = _tool_knowledge_search_impl.__doc__
knowledge_digest.__doc__ = _tool_knowledge_digest_impl.__doc__
get_document.__doc__ = _tool_get_document_impl.__doc__
list_documents.__doc__ = _tool_list_documents_impl.__doc__
knowledge_status.__doc__ = _tool_knowledge_status_impl.__doc__


# ---- 按 MCP_MODE 注册工具（V3.9 双模配置：见本文件上方 MCP_MODE 说明）----
_TOOL_REGISTRY = {"knowledge_qa": knowledge_qa, "knowledge_search": knowledge_search,
                  "knowledge_digest": knowledge_digest, "get_document": get_document,
                  "list_documents": list_documents, "knowledge_status": knowledge_status}
for _tool_name in _MODE_TOOLS[MCP_MODE]:
    mcp.tool()(_TOOL_REGISTRY[_tool_name])
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
        groups = visible_groups(ident.get("department_group", ident["department"]))
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
        from src import auth_db as _auth_db  # stdio 也确保审计/日志表存在（幂等）
        _auth_db.init_db()
        mcp.run()  # stdio 模式（默认），供 MCP 客户端以子进程方式挂载

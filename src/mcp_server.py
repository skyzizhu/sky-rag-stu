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
import sys
from pathlib import Path

# 保证作为脚本直接运行时能 import 项目包（src.*）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("sky-personal-rag")


def _filters(domain: str | None, status: str | None) -> dict | None:
    """组装 Metadata 过滤条件；未提供任何过滤时返回 None（检索默认范围）。"""
    filters: dict = {}
    if domain:
        filters["domain"] = domain
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


@mcp.tool()
def knowledge_qa(
    question: str,
    top_k: int = 5,
    domain: str | None = None,
    status: str | None = None,
    history: list[dict] | None = None,
) -> str:
    """基于个人知识库回答问题，返回带来源引用的答案。

    适用：需要利用用户自有文档/网页知识回答事实性问题。
    多轮追问时请传入 history（最近几轮 user/assistant 消息），可正确解析指代。

    Args:
        question: 用户问题（不可为空）
        top_k: 召回知识卡片数量，默认 5，范围 1~10
        domain: 可选，限定知识领域（work/learning/life/projects/reference/archive）
        status: 可选，知识状态过滤（active=生效 / archive=已归档 / expired=历史版本）
        history: 可选，最近对话 [{"role":"user/assistant","content":"..."}]，最多取 6 条
    """
    if not question or not question.strip():
        return json.dumps({"error": "问题不能为空"}, ensure_ascii=False)
    from src.pipeline import answer_question

    result = answer_question(
        question.strip(),
        top_k=max(1, min(int(top_k), 10)),
        filters=_filters(domain, status),
        history=_history(history),
    )
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


@mcp.tool()
def knowledge_search(
    question: str,
    top_k: int = 5,
    domain: str | None = None,
    status: str | None = None,
) -> str:
    """只在个人知识库中检索相关片段，不调用 LLM 生成答案（零生成成本、更快）。

    适用：Agent 自己想先看资料再决定怎么回答/推理；或只需要证据不需要成文答案。

    Args:
        question: 检索问题或关键词
        top_k: 返回片段数量，默认 5，范围 1~10
        domain: 可选，限定知识领域（work/learning/life/projects/reference/archive）
        status: 可选，知识状态过滤（active/archive/expired）
    """
    if not question or not question.strip():
        return json.dumps({"error": "问题不能为空"}, ensure_ascii=False)
    from src.pipeline import answer_question

    result = answer_question(
        question.strip(),
        top_k=max(1, min(int(top_k), 10)),
        use_llm=False,  # 仅检索：不调用生成模型
        filters=_filters(domain, status),
    )
    return json.dumps(
        {
            "query": question.strip(),
            "matches": [
                {
                    "rank": i,
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


@mcp.tool()
def knowledge_digest(topic: str) -> str:
    """跨文档知识总结：围绕一个主题，综合知识库中多个文档生成结构化摘要（要点+出处）。

    适用：需要横向综合多个知识文件的场景；不新建或修改知识文件。

    Args:
        topic: 要总结的主题（如「RAG」「智能客服」）
    """
    if not topic or not topic.strip():
        return json.dumps({"error": "主题不能为空"}, ensure_ascii=False)
    from src.pipeline import answer_question

    result = answer_question(
        f"请综合知识库中与「{topic.strip()}」相关的全部内容，"
        "输出一份结构化知识总结：核心要点 + 对应出处编号。资料不足时明确说明。",
        top_k=10,
    )
    return json.dumps(
        {
            "topic": topic.strip(),
            "digest": result.answer or "知识库中没有与该主题相关的足够内容。",
            "sources": _sources_payload(result.sources),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
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


if __name__ == "__main__":
    mcp.run()  # stdio 模式（默认），供 MCP 客户端以子进程方式挂载

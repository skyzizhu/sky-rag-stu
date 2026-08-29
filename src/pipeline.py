"""流水线总控（Pipeline）：把所有节点按顺序串起来。

两条流水线：

入库（Ingest）：
    文件 → 解析 → 清洗 → 切片 → 向量化 → 存入 Qdrant

问答（Query）：
    问题 → 问题向量化 → 向量检索 → 组装资料 → 提示词 → LLM → 答案 + 来源

产品视角：这个文件相当于「车间调度员」，
每个环节做了什么、花了多久、多少产出，都会打印出来。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from src.chunker import clean_and_chunk
from src.config import AppConfig, get_config
from src.context import build_context
from src.embedding import EmbeddingClient, get_embedding_client
from src.llm import LLMClient, get_llm_client
from src.metadata import build_document_metadata
from src.parser import ParsedDocument, parse_file
from src.prompt import build_messages
from src.retriever import RetrievedItem, effective_filters, get_retriever
from src.trace import make_node, now_str
from src.vector_store import VectorStore, get_vector_store


@dataclass
class IngestSummary:
    """一次入库的体检报告。"""

    total_files: int = 0
    ok_files: int = 0
    failed_files: list[str] = field(default_factory=list)
    total_chunks: int = 0
    vector_dimension: int = 0
    elapsed_seconds: float = 0.0
    rebuilt: bool = False
    file_rows: list[dict] = field(default_factory=list)  # 每个文件的入库结果明细


@dataclass
class QAResult:
    """一次问答的完整记录（调试模式全部展示）。"""

    question: str
    answer: str | None
    retrieved: list[RetrievedItem]
    context: str
    sources: list[dict]
    filters: dict = field(default_factory=dict)  # 本次检索实际使用的过滤条件
    trace: list = field(default_factory=list)    # 节点时间线（调试面板）
    elapsed: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 入库
def ingest_files(
    paths: list[Path] | None = None,
    rebuild: bool = False,
    config: AppConfig | None = None,
    embedding_client: EmbeddingClient | None = None,
    store: VectorStore | None = None,
) -> IngestSummary:
    """把文件送进知识库。paths 为空时扫描整个知识目录。"""
    cfg = config or get_config()
    embedding = embedding_client or get_embedding_client()
    vector_store = store or get_vector_store()
    summary = IngestSummary(rebuilt=rebuild)
    start = time.time()

    # ① 收集文件
    if paths:
        files = [Path(p) for p in paths]
    else:
        files = sorted(
            p for p in cfg.knowledge_dir.rglob("*")
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in
            {".txt", ".md", ".rtf", ".html", ".htm", ".docx", ".pdf"}
        )
    summary.total_files = len(files)
    if not files:
        print(f"⚠️ 没有找到可入库的文件（知识目录：{cfg.knowledge_dir}）")
        return summary
    print(f"① 找到 {len(files)} 个待入库文件")

    # ② 解析 + 生成统一 Metadata（目录推断 → Front Matter 覆盖 → 系统兜底）
    print("② 解析文件 → 统一文本 + Metadata")
    docs: list[ParsedDocument] = []
    for path in files:
        try:
            parsed = parse_file(path)
        except Exception as exc:
            summary.failed_files.append(str(exc))
            continue
        # 相对路径决定 domain / category / topic；knowledge 目录外的文件按根目录规则处理
        try:
            relative_path = path.resolve().relative_to(cfg.knowledge_dir.resolve()).as_posix()
        except ValueError:
            relative_path = path.name
        parsed.metadata = build_document_metadata(
            relative_path=relative_path,
            source=path.name,
            file_type=parsed.metadata["file_type"],
            title_hint=parsed.metadata["title"],
            created_at=parsed.metadata["created_at"],
            updated_at=parsed.metadata["updated_at"],
            front_matter=parsed.front_matter,
        )
        docs.append(parsed)
    summary.ok_files = len(docs)
    print(f"   成功 {len(docs)} 个，失败 {len(summary.failed_files)} 个")
    for failure in summary.failed_files:
        print(f"   ❌ {failure}")
    if not docs:
        return summary

    # ③ 清洗 + ④ 切片
    print("③ 清洗文本，④ 切成知识卡片")
    cleaned_docs, chunks = clean_and_chunk(docs)
    summary.total_chunks = len(chunks)
    chunks_by_file: dict[str, int] = {}
    for chunk in chunks:
        source = chunk.metadata["source"]
        chunks_by_file[source] = chunks_by_file.get(source, 0) + 1
    for doc in cleaned_docs:
        meta = doc.metadata
        summary.file_rows.append({
            "source": meta["source"],
            "path": meta["path"],
            "domain": meta["domain"],
            "category": meta["category"],
            "status": meta["status"],
            "chunks": chunks_by_file.get(meta["source"], 0),
        })
    print(f"   共切出 {len(chunks)} 张知识卡片")
    if not chunks:
        return summary

    # ⑤ 向量化
    print(f"⑤ 向量化（模型：{cfg.embedding_model}）")
    summary.vector_dimension = embedding.dimension()
    print(f"   向量维度：{summary.vector_dimension}")
    vectors = embedding.embed_texts([chunk.text for chunk in chunks])

    # ⑥ 存入向量库
    if rebuild:
        print("⑥ 重建模式：先清空向量库")
        vector_store.clear()
    else:
        # 增量更新语义：本次入库的文档先删掉旧卡片再写入，文件变短也不会留残余
        document_ids = list({doc.metadata["document_id"] for doc in cleaned_docs})
        vector_store.delete_documents(document_ids)
    print("⑥ 存入 Qdrant 向量库")
    vector_store.ensure_collection(summary.vector_dimension)
    stored = vector_store.upsert_chunks(chunks, vectors)
    print(f"   已写入 {stored} 张卡片")

    summary.elapsed_seconds = time.time() - start
    print(f"✅ 入库完成，总耗时 {summary.elapsed_seconds:.1f} 秒")
    print("\n入库明细：")
    for row in summary.file_rows:
        print(f"   {row['path']:<40} domain={row['domain']:<10} category={row['category']:<12} "
              f"status={row['status']:<8} 卡片数={row['chunks']}")
    return summary


# ---------------------------------------------------------------- 问答
def answer_stream(
    question: str,
    top_k: int | None = None,
    use_llm: bool = True,
    filters: dict | None = None,
    config: AppConfig | None = None,
):
    """流式问答：检索同步完成，回答逐段产出。返回 (完整结果记录, 文本块生成器)。

    生成器迭代结束后，result.answer / result.elapsed / result.trace 才是完整值。
    filters 例：{"domain": "learning"}、{"status": "archive"}。
    默认强制 status=active；显式传 status 以传入值为准。
    """
    cfg = config or get_config()
    retriever = get_retriever()
    trace: list = []
    result = QAResult(question=question, answer=None, retrieved=[], context="",
                      sources=[], filters=effective_filters(filters), trace=trace)

    # 节点 ⓪：服务就绪检查
    ready_start = now_str()
    checks = []
    try:
        emb_ok = get_embedding_client().model_available()
        checks.append(("向量化模型（Ollama）", f"{'在线 ✅' if emb_ok else '离线 ❌'} · {cfg.embedding_model}"))
    except Exception as exc:
        checks.append(("向量化模型（Ollama）", f"不可用 ❌ · {str(exc)[:60]}"))
    try:
        from src.vector_store import get_vector_store as _gvs
        checks.append(("向量数据库（Qdrant）", f"在线 ✅ · 已存 {_gvs().count()} 张卡片"))
    except Exception as exc:
        checks.append(("向量数据库（Qdrant）", f"不可用 ❌ · {str(exc)[:60]}"))
    checks.append(("大模型（云端 API）", f"已配置 ✅ · {cfg.llm_model}" if cfg.llm_api_key else "未配置 ❌"))
    trace.append(make_node(
        "🩺", "服务就绪检查", time_str=ready_start,
        summary="问答开始前，先确认三个依赖都可用：本地向量化模型、本机向量数据库、云端大模型 Key",
        items=checks,
    ))

    # 节点 ①：提问
    trace.append(make_node(
        "❓", "提问", time_str=now_str(),
        summary="接收用户问题，问答流程从这里开始",
        items=[("输入（用户问题）", question)],
    ))

    # 节点：会话记忆 / 多轮对话（V1 直通）
    trace.append(make_node(
        "💬", "会话记忆 / 多轮对话", time_str=now_str(), status="直通（多轮能力规划在后续版本）",
        summary="完整 RAG 在多轮对话时会带着历史消息理解当前问题（比如'它多少钱？'要结合上一轮"
                "才知道'它'是谁）；V1 每次问答相互独立，不带历史记忆",
        items=[
            ("输入", f"当前问题 1 条（历史消息 0 条）"),
            ("输出", "与输入相同（未结合历史）"),
        ],
    ))

    # 节点 ②：Query 理解 / 改写（V1 直通，规划 V2 启用）
    trace.append(make_node(
        "🧠", "Query 理解 / 改写", time_str=now_str(), status="直通（规划 V2 启用）",
        summary="完整 RAG 会先用 LLM 把口语化提问改写成更适合检索的查询（如补全指代、提取关键词）；"
                "V1 阶段此节点未启用，原始问题直接向下传递",
        items=[
            ("输入", question),
            ("输出", "与输入相同（未改写）"),
            ("改写 Prompt", "本阶段未启用，因此没有产生改写用的系统/用户 Prompt；"
             "该节点在 V2 启用后，这里会展示它的系统 Prompt 和用户 Prompt 全文"),
        ],
    ))

    # 节点 ③④⑤⑥：Query Embedding → Metadata Filter → 数据库检索 → 召回 Chunk
    t0 = time.time()
    result.retrieved = retriever.retrieve(question, top_k=top_k, filters=filters, trace=trace)
    result.elapsed["retrieval"] = time.time() - t0
    result.sources = [
        {
            "rank": item.rank,
            "score": item.score,
            "source": item.metadata.get("source"),
            "section": item.metadata.get("section"),
            "section_path": item.metadata.get("section_path"),
            "page": item.metadata.get("page"),
            "domain": item.metadata.get("domain"),
            "category": item.metadata.get("category"),
            "topic": item.metadata.get("topic") or [],
            "version": item.metadata.get("version"),
            "status": item.metadata.get("status"),
            "text": item.text,
        }
        for item in result.retrieved
    ]

    # 节点 ⑦：合并 / 去重
    merge_start = now_str()
    t1 = time.time()
    context, used, dropped = build_context(result.retrieved, cfg)
    merge_elapsed = time.time() - t1
    result.context = context
    drop_lines = [f"[{d['rank']}] {d['source']} —— {d['reason']}" for d in dropped]
    trace.append(make_node(
        "🧩", "合并 / 去重", time_str=merge_start, elapsed=merge_elapsed,
        summary="对召回结果做合并与去重：内容相同的卡片只留相关度最高的一条，"
                "放不下资料上限的卡片丢弃（V1 中本步骤在上下文组装时一并完成）",
        items=[
            ("去重 / 合并逻辑", "忽略空白差异后比较卡片全文，重复的只保留第一条；"
             "剩余卡片按相关度依次放入，直到达到资料字数上限"),
            ("被丢弃的卡片", "\n".join(drop_lines) if drop_lines else "（没有需要丢弃的卡片）"),
            ("输出", f"采用 {len(used)} 条，丢弃 {len(dropped)} 条"),
        ],
    ))

    # 节点 ⑧：Rerank（V1 直通，规划 V2 启用）
    trace.append(make_node(
        "🏆", "Rerank（精排）", time_str=now_str(), status="直通（规划 V2 启用）",
        summary="完整 RAG 会用重排模型对召回卡片逐条精细打分再排序；"
                "V1 未启用，直接沿用向量相似度排序",
        items=[
            ("当前排序规则", "按向量相似度分数从高到低（无独立重排模型）"),
            ("排序结果", "\n".join(
                f"[{s['rank']}] {s['source']}  score={s['score']:.4f}" for s in result.sources
            ) or "（无）"),
        ],
    ))

    # 节点 ⑨：上下文组装
    trace.append(make_node(
        "📦", "上下文组装", time_str=now_str(),
        summary="把采用的卡片拼成一份带编号和出处的「资料附页」，随问题一起发给大模型",
        items=[
            ("组装规则", f"相关度降序编号 [1][2]…；每条标注来源/领域/章节/页码；"
             f"总字数上限 {cfg.context_max_chars} 字"),
            ("输出（最终 Context）", context),
            ("规模", f"共 {len(used)} 条资料，合计 {len(context)} 字（上限 {cfg.context_max_chars} 字）"),
        ],
    ))

    if not use_llm:
        return result, iter(())

    # 节点 ⑩：Prompt
    t2 = time.time()
    messages = build_messages(context, question)
    llm = get_llm_client()
    trace.append(make_node(
        "📝", "Prompt", time_str=now_str(), elapsed=time.time() - t2,
        summary="把「任务说明书 + 资料附页 + 用户问题」组装成发给大模型的最终消息",
        items=[
            ("System Prompt（任务说明书）", messages[0]["content"]),
            ("User Prompt（资料附页 + 问题）", messages[1]["content"]),
        ],
    ))

    # 节点 ⑪⑫：LLM 生成 + 后处理（流式结束后写入）
    def _generate():
        llm_start = now_str()
        t3 = time.time()
        text = ""
        try:
            for delta in llm.chat_stream(messages):
                text += delta
                yield delta
        finally:
            result.answer = text.strip() or None
            result.elapsed["llm"] = time.time() - t3
            result.elapsed["total"] = result.elapsed.get("retrieval", 0) + result.elapsed["llm"]
            trace.append(make_node(
                "🤖", "LLM 生成", time_str=llm_start, elapsed=result.elapsed["llm"],
                summary="云端大模型基于资料附页生成回答（流式输出，逐字返回）",
                items=[
                    ("模型", cfg.llm_model or "-"),
                    ("输入规模", f"System {len(messages[0]['content'])} 字 + User {len(messages[1]['content'])} 字"),
                    ("输出", f"{len(result.answer or '')} 字答案"),
                ],
            ))
            trace.append(make_node(
                "🎁", "后处理", time_str=now_str(),
                summary="答案收尾：提取回答文本、组装参考来源列表并对应引用编号、统计总耗时",
                items=[
                    ("做了什么", "提取答案文本 → 组装来源列表（编号/文件/领域/章节/页码/相关度）→ 统计总耗时"),
                    ("输出", f"答案 {len(result.answer or '')} 字 · 来源 {len(result.sources)} 条 · "
                     f"总耗时 {result.elapsed.get('total', 0):.2f} 秒"),
                ],
            ))

    return result, _generate()


def answer_question(
    question: str,
    top_k: int | None = None,
    use_llm: bool = True,
    filters: dict | None = None,
    config: AppConfig | None = None,
) -> QAResult:
    """完整问答链路（一次性返回完整答案）。use_llm=False 时只做检索（不花钱）。"""
    result, deltas = answer_stream(question, top_k=top_k, use_llm=use_llm,
                                   filters=filters, config=config)
    for _ in deltas:  # 消费生成器，把答案收集完整
        pass
    return result


# ---------------- 命令行问答：python -m src.pipeline "问题" ----------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="命令行问答（Streamlit 界面请运行 streamlit run app.py）")
    parser.add_argument("question", nargs="*", help="你的问题")
    parser.add_argument("--retrieve-only", action="store_true", help="只检索不调用大模型（不花钱调试用）")
    parser.add_argument("--top-k", type=int, default=None, help="本次召回条数")
    args = parser.parse_args()

    if not args.question:
        parser.print_help()
    else:
        result = answer_question(
            " ".join(args.question), top_k=args.top_k, use_llm=not args.retrieve_only
        )
        if result.answer:
            print(f"\n【回答】\n{result.answer}\n")
            print("【来源】")
            for source in result.sources:
                line = f"  [{source['rank']}] {source['source']}（相关度 {source['score']:.3f}）"
                if source.get("section"):
                    line += f" 章节: {source['section']}"
                if source.get("page"):
                    line += f" 页码: {source['page']}"
                print(line)
        else:
            from src.retriever import format_results

            print(format_results(result.retrieved))

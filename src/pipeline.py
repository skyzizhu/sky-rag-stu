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
from src.metadata import build_document_metadata, document_id_for
from src.parser import ParsedDocument, parse_file
from src.prompt import build_messages
from src.query_understanding import understand_query
from src.reranker import rerank as rerank_items
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
    new_files: list[str] = field(default_factory=list)      # V3.1 新增入库
    updated_files: list[str] = field(default_factory=list)  # V3.1 内容有变化，重新入库
    skipped_files: list[str] = field(default_factory=list)  # V3.1 指纹未变，跳过
    cleaned_files: list[str] = field(default_factory=list)  # V3.1 文件已删除，向量同步清理


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

    # ①.5 增量判断（V3.1）：内容指纹没变的文件跳过；台账里有但文件已删除的，向量同步清理
    from src import index_state as istate

    state = {} if rebuild else istate.load_state()
    paths_to_process: list[Path] = []
    processed: list[tuple[Path, str, str]] = []  # (路径, 相对路径, 哈希)
    explicit = bool(paths)
    for path in files:
        rel = path.name
        try:
            rel = path.resolve().relative_to(cfg.knowledge_dir.resolve()).as_posix()
        except ValueError:
            pass
        current_hash = istate.file_hash(path)
        if not explicit:
            if state.get(rel, {}).get("hash") == current_hash:
                summary.skipped_files.append(rel)
                continue
            (summary.updated_files if rel in state else summary.new_files).append(rel)
        processed.append((path, rel, current_hash))
        paths_to_process.append(path)

    for rel in list(state.keys()):
        if not (cfg.knowledge_dir / rel).exists():
            document_id = state[rel].get("document_id") or document_id_for(rel)
            vector_store.delete_documents([document_id])
            istate.remove_entry(state, rel)
            summary.cleaned_files.append(rel)
    if summary.cleaned_files:
        print(f"   🧹 已同步清理 {len(summary.cleaned_files)} 个已删除文件的向量数据")
        istate.save_state(state)  # 清理结果立即落盘（该路径可能提前返回）
    if summary.skipped_files:
        print(f"   ⏭️ {len(summary.skipped_files)} 个文件内容未变化，跳过（省时省钱）")
        for rel in summary.skipped_files:
            print(f"      ⏭️ {rel}")
    files = paths_to_process
    summary.total_files = len(files)
    if not files:
        print("✅ 增量入库完成：没有需要处理的文件")
        return summary

    # ② 解析 + 生成统一 Metadata（目录推断 → Front Matter 覆盖 → 系统兜底）
    print("② 解析文件 → 统一文本 + Metadata")
    docs: list[ParsedDocument] = []
    llm = get_llm_client() if cfg.auto_tag else None
    for path in files:
        try:
            parsed = parse_file(path)
        except Exception as exc:
            summary.failed_files.append(str(exc))
            continue
        # 相对路径决定 domain / category / topic；knowledge 目录外的文件按根目录规则处理
        try:
            rel = path.resolve().relative_to(cfg.knowledge_dir.resolve()).as_posix()
        except ValueError:
            rel = path.name
        parsed.metadata = build_document_metadata(
            relative_path=rel,
            source=path.name,
            file_type=parsed.metadata["file_type"],
            title_hint=parsed.metadata["title"],
            created_at=parsed.metadata["created_at"],
            updated_at=parsed.metadata["updated_at"],
            front_matter=parsed.front_matter,
        )
        # V3.2 版本管理：内容有变化的文件版本号自动 +0.1（Front Matter 手动指定优先）
        prev_version = state.get(rel, {}).get("version")
        if prev_version and "version" not in (parsed.front_matter or {}):
            from src.metadata import bump_version
            parsed.metadata["version"] = bump_version(prev_version)
        # V3.3 自动打标签：topic/tags 皆空时由 LLM 补全（失败静默跳过，不阻断入库）
        if cfg.auto_tag and llm and not parsed.metadata["topic"] and not parsed.metadata["tags"]:
            from src.auto_tagger import auto_tag
            tagged = auto_tag(parsed.text[:800], llm=llm, config=cfg)
            parsed.metadata["topic"] = tagged["topic"]
            parsed.metadata["tags"] = tagged["tags"]
            if tagged["topic"] or tagged["tags"]:
                print(f"   🤖 自动标签 [{rel}]: topic={parsed.metadata['topic']} tags={parsed.metadata['tags']}")
        # V3.4 档案编辑：台账里的覆盖设置优先级最高（高于 Front Matter 与目录推断）
        overrides = state.get(rel, {}).get("overrides", {})
        for key, value in overrides.items():
            if key in ("title", "category", "topic", "tags", "version", "status") and value:
                parsed.metadata[key] = value
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

    # ⑦ 更新入库台账（V3.1）：记录每个文件的指纹与卡片数，作为下次增量的对比依据
    for path, rel, file_hash_value in processed:
        doc_id = next(
            (c.metadata["document_id"] for c in chunks if c.metadata["path"] == rel),
            document_id_for(rel),
        )
        chunk_count = sum(1 for c in chunks if c.metadata["path"] == rel)
        istate.update_entry(state, rel, doc_id, chunk_count, file_hash_value)
    istate.save_state(state)

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
        # V3.2 版本管理：内容有变化的文件，旧版本整体转 expired 留作历史（新版本随后写入）
        for doc in cleaned_docs:
            if doc.metadata["path"] in summary.updated_files:
                vector_store.expire_document(doc.metadata["document_id"])
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
    use_query_understanding: bool = True,
    use_hybrid: bool = True,
    use_rerank: bool = True,
    config: AppConfig | None = None,
):
    """流式问答：检索同步完成，回答逐段产出。返回 (完整结果记录, 文本块生成器)。

    生成器迭代结束后，result.answer / result.elapsed / result.trace 才是完整值。
    filters 例：{"domain": "learning"}、{"status": "archive"}（界面手动设置，优先级最高）。
    use_query_understanding=True 时先用 LLM 改写提问并推断过滤条件（V2.1）。
    use_hybrid=True 时并行执行 BM25 关键词检索并用 RRF 融合（V2.3/V2.4）。
    use_rerank=True 时召回扩宽到 RERANK_RECALL_K 条，LLM 打分精排后取 Top K（V2.5）。
    """
    cfg = config or get_config()
    top_k = top_k or cfg.top_k
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

    # 列举/盘点类提问自动扩大召回深度
    enumeration = any(w in question for w in ("有哪些", "列出", "都有什么", "列举", "全部"))
    recall_k = top_k * 2 if enumeration else top_k

    # 列举/盘点类提问自动扩大召回深度和上下文条数上限
    enumeration = any(w in question for w in ("有哪些", "列出", "都有什么", "列举", "全部"))
    recall_k = top_k * 2 if enumeration else top_k
    ctx_max_items = top_k * 2 if enumeration else top_k

    # 节点 ②：Query 理解 / 改写（V2：一次 LLM 调用完成改写 + 关键词 + 过滤条件推断）
    search_query = question
    qu_filters: dict = {}
    qu = None
    if use_query_understanding and use_llm:
        qu_start = now_str()
        llm = get_llm_client()
        try:
            catalog = sorted({d["category"] for d in get_vector_store().list_documents()})
        except Exception:
            catalog = []
        qu = understand_query(question, llm=llm, config=cfg, categories=catalog)
        search_query = qu.vector_query or question
        qu_filters = dict(qu.filters)
        # 列举/盘点类提问自动扩大召回深度，保证"有哪些"类问题能覆盖完整列表
        enumeration = any(w in question for w in ("有哪些", "列出", "都有什么", "列举", "全部"))
        parsed = (f"intent: {qu.intent or '-'}\n"
                  f"vector_query（改写后检索语句）: {qu.vector_query}\n"
                  f"keyword_query（关键词，供关键词检索用）: {', '.join(qu.keyword_query) or '-'}\n"
                  f"filters（推断的过滤条件）: {qu.filters or '{}'}\n"
                  f"time_range（解析的时间范围）: {qu.time_range or '-'}")
        if qu.ok and qu.error:
            parsed += f"\n校验提示: {qu.error}"
        trace.append(make_node(
            "🧠", "Query 理解 / 改写", time_str=qu_start, elapsed=qu.elapsed,
            status="已执行" if qu.ok else "回退（解析失败，使用原始问题检索）",
            summary="用一次 LLM 调用，把口语化提问改写成检索友好的查询语句，"
                    "同时推断过滤条件（领域/分类/状态等）；失败自动回退为原始问题",
            items=[
                ("改写 System Prompt", qu.system_prompt),
                ("改写 User Prompt", qu.user_prompt),
                ("LLM 原始输出", qu.raw_output or qu.error),
                ("解析结果", parsed),
            ],
        ))
    else:
        reason = "仅检索模式" if not use_llm else "未启用（可在 检索设置 或 .env QUERY_UNDERSTANDING 开启）"
        trace.append(make_node(
            "🧠", "Query 理解 / 改写", time_str=now_str(), status=f"直通（{reason}）",
            summary="原始问题直接进入向量化，未做改写与条件推断",
            items=[("输入 / 输出", question)],
        ))

    # 节点 ③④⑤⑥：Query Embedding → Metadata Filter → 数据库检索 → 召回 Chunk
    # 过滤条件优先级：界面手动设置 > Query 理解推断 > 默认 status=active
    # Rerank 开启时先宽召回（默认 10 条候选），精排后再取 Top K
    use_rerank_now = use_rerank and use_llm
    recall_k = max(recall_k, cfg.rerank_recall_k) if use_rerank_now else recall_k
    merged_filters = {**qu_filters, **(filters or {})}
    if qu and qu.ok and qu.time_range:
        merged_filters["updated_at"] = qu.time_range  # V3.2 时间感知：按文档时间过滤
    result.filters = effective_filters(merged_filters)
    t0 = time.time()
    result.retrieved = retriever.retrieve(
        search_query, top_k=recall_k, filters=merged_filters, trace=trace,
        keyword_query=(qu.keyword_query if qu and qu.ok else None),
        use_hybrid=use_hybrid,
    )

    # 渐进式放宽（检索容错）：LLM 推断的过滤条件猜错时可能把正确答案挡在门外——
    # 如果「带推断条件」检索为 0 条，自动去掉推断条件（保留手动设置）重试一次。
    if not result.retrieved and qu_filters and not filters:
        relax_start = now_str()
        t_relax = time.time()
        manual_filters = filters or {}
        result.retrieved = retriever.retrieve(
            search_query, top_k=top_k, filters=manual_filters, trace=None,
            keyword_query=(qu.keyword_query if qu and qu.ok else None),
            use_hybrid=use_hybrid,
        )
        result.filters = effective_filters(manual_filters)
        trace.append(make_node(
            "🔄", "过滤放宽重试", time_str=relax_start, elapsed=time.time() - t_relax,
            status="已触发",
            summary="带推断过滤条件的检索召回了 0 条——推断的条件可能过严，把正确答案挡在了门外。"
                    "自动去掉 LLM 推断的条件（保留手动设置）重试了一次；"
                    "这是检索系统的常见容错策略：宁可范围大一点，也不能漏掉答案",
            items=[
                ("首检条件（0 条召回）", str(effective_filters({**qu_filters, **(filters or {})}))),
                ("放宽后条件", str(result.filters)),
                ("重试结果", f"召回 {len(result.retrieved)} 条"),
            ],
        ))
    result.elapsed["retrieval"] = time.time() - t0
    # 节点 ⑦：Rerank 精排（V2.5：LLM 逐条打分，宽召回精选）
    if use_rerank_now and result.retrieved:
        rr_start = now_str()
        t_rr = time.time()
        outcome = rerank_items(question, result.retrieved, top_k=top_k,
                               llm=get_llm_client(), config=cfg)
        rr_elapsed = time.time() - t_rr
        # 不在这里截断：全部打分候选交给上下文组装，由「多样性 + Top K」规则统一精选
        result.retrieved = outcome.items
        trace.append(make_node(
            "🏆", "Rerank（精排）", time_str=rr_start,
            elapsed=rr_elapsed,
            status="已执行" if outcome.ok else f"回退（粗排顺序：{outcome.error[:50]}）",
            summary=f"召回先扩宽到 {recall_k} 条候选，LLM 逐条阅读并按相关度打 0~10 分，"
                    f"按分精排后取 Top {top_k}——粗排只看文字和指纹，精排才理解内容",
            items=[
                ("精排方式（共 1 种启用）", "✅ 启用：LLM 重排 —— 用已配置的大模型逐条阅读候选内容，"
                 "按相关度打 0~10 分\n"
                 "○ 未启用：专用重排模型（bge-reranker / Cohere Rerank 等）——"
                 "专为此任务训练、打分更稳定更快，可在 src/reranker.py 中替换接入"),
                ("方式 1 · LLM 重排的结果", "\n".join(
                    f"{cid}  {source}  {score:.0f} 分" for cid, source, score in outcome.scores
                ) or "（无候选）"),
                ("排序前后对比", "粗排: " + " → ".join(outcome.before_order)
                 + "\n精排 Top K: " + " → ".join(outcome.after_order)),
                ("方式 1 · 给 LLM 的 System Prompt", outcome.system_prompt),
                ("方式 1 · 给 LLM 的 User Prompt", outcome.user_prompt),
                ("LLM 原始输出", outcome.raw_output or outcome.error),
            ],
        ))
    elif not use_rerank:
        trace.append(make_node(
            "🏆", "Rerank（精排）", time_str=now_str(),
            status="未启用（可在 检索设置 或 .env RERANK_ENABLED 开启）",
            summary="候选卡片按粗排顺序（向量相似度 / RRF 融合）直接使用",
            items=[("说明", "开启后：召回先扩宽（默认 10 条候选），LLM 逐条打分精排，再取 Top K 进入回答")],
        ))

    # 节点 ⑧ 合并 / 去重 + 节点 ⑨ 上下文组装（V2.6：文档多样性 + 相邻补全）
    # 精排后不再截断候选池，由组装规则在全部候选中选出最终进入回答的 Top K
    merge_start = now_str()
    t1 = time.time()
    context, used, dropped, merge_notes = build_context(
        result.retrieved, cfg, max_items=ctx_max_items
    )
    result.retrieved = used
    result.context = context
    if "updated_at" in result.filters:  # 时间过滤已生效，告知大模型无需再纠结时间
        tr = result.filters["updated_at"]
        context = f"【时间说明】以下资料均已按时间范围筛选：{tr['from']} ~ {tr['to']}。\n\n" + context
        result.context = context
    merge_elapsed = time.time() - t1
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
            "channels": item.channels,
            "text": item.text,
        }
        for item in used
    ]
    drop_lines = [f"[{d['rank']}] {d['source']} —— {d['reason']}" for d in dropped]
    trace.append(make_node(
        "🧩", "合并 / 去重", time_str=merge_start, elapsed=merge_elapsed,
        summary="对召回结果做合并与去重：内容相同的卡片只留相关度最高的一条；"
                "同一文档最多采用 3 条（保持来源多样）；达到 Top K 上限后停止选取",
        items=[
            ("去重 / 合并逻辑", "忽略空白差异后比较卡片全文，重复的只保留第一条；"
             "剩余卡片按相关度依次放入，直到达到条数或字数上限"),
            ("多样性限制", f"同一文档最多采用 {cfg.context_max_per_doc} 条"),
            ("被丢弃的卡片", "\n".join(drop_lines) if drop_lines else "（没有需要丢弃的卡片）"),
            ("输出", f"采用 {len(used)} 条，丢弃 {len(dropped)} 条"),
        ],
    ))

    # 节点 ⑨：上下文组装
    trace.append(make_node(
        "📦", "上下文组装", time_str=now_str(),
        summary="把采用的卡片拼成一份带编号和出处的「资料附页」，随问题一起发给大模型；"
                "同文档相邻卡片自动拼接，恢复被切片切断的上下文",
        items=[
            ("组装规则", f"相关度降序编号 [1][2]…；每条标注来源/领域/章节/页码；"
             f"总字数上限 {cfg.context_max_chars} 字"),
            ("相邻补全", "\n".join(merge_notes) if merge_notes else "（没有可拼接的相邻卡片）"),
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
    use_query_understanding: bool = True,
    use_hybrid: bool = True,
    use_rerank: bool = True,
    config: AppConfig | None = None,
) -> QAResult:
    """完整问答链路（一次性返回完整答案）。use_llm=False 时只做检索（不花钱）。"""
    result, deltas = answer_stream(
        question, top_k=top_k, use_llm=use_llm, filters=filters,
        use_query_understanding=use_query_understanding,
        use_hybrid=use_hybrid, use_rerank=use_rerank, config=config,
    )
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

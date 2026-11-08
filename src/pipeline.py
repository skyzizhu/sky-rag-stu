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

import os
import threading
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

from src import index_epoch
from src.chunker import clean_and_chunk
from src.config import AppConfig, get_config
from src.context import build_context, estimate_tokens
from src.embedding import EmbeddingClient, get_embedding_client
from src.llm import LLMClient, get_llm_client, strip_think_tags
from src.metadata import build_document_metadata, document_id_for
from src.parser import ParsedDocument, parse_file
from src.answer_language import resolve_answer_language
from src.prompt import build_messages
from src.query_understanding import understand_query
from src.reranker import rerank as rerank_items
from src.retriever import RetrievedItem, effective_filters, get_retriever
from src.trace import make_node, now_str
from src.vector_store import VectorStore, get_vector_store

# V3.8.1 枚举通道参数（列举/计数类提问专用——目标是"全"而不是"精"）：
# 召回池 50、上下文名额 40（容纳单文档全量卡片）、跳过精排（省一次 LLM 调用）、
# token 预算放宽到 12000（全量卡片进上下文）。支持 .env 覆盖。
ENUM_RECALL_K = int(os.getenv("ENUM_RECALL_K", "50"))
ENUM_CTX_ITEMS = int(os.getenv("ENUM_CTX_ITEMS", "40"))
ENUM_CTX_TOKENS = int(os.getenv("ENUM_CTX_TOKENS", "12000"))


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
    evaluation: dict | None = None  # V3.6 回答质量评估载荷（见 src/answer_evaluator.py）


# ---------------------------------------------------------------- 入库
def ingest_files(
    paths: list[Path] | None = None,
    rebuild: bool = False,
    config: AppConfig | None = None,
    embedding_client: EmbeddingClient | None = None,
    store: VectorStore | None = None,
    skip_unchanged: bool = False,
    default_groups: list[str] | None = None,
) -> IngestSummary:
    """把文件送进知识库。paths 为空时扫描整个知识目录。

    增量判断（V3.1）：全库扫描始终做指纹跳过；显式传 paths 默认是强制
    重新入库（管理台「重新入库」按钮的语义），skip_unchanged=True 可让
    显式路径同样做指纹跳过（网页入库等"内容没变就别重跑"的场景）。
    default_groups（V3.7）：本批文件的默认可见部门（上传表单的"可见部门"选择）。
    优先级：Front Matter 显式声明 > 表单选择 > 默认全员；空列表一律落为 ["all"]。
    """
    # 多标签页并发点入库会互相覆盖台账/向量数据，入库全程串行化（单用户无感知）
    with _ingest_lock:
        return _ingest_files_impl(paths=paths, rebuild=rebuild, config=config,
                                  embedding_client=embedding_client, store=store,
                                  skip_unchanged=skip_unchanged,
                                  default_groups=default_groups)


def _ingest_files_impl(
    paths: list[Path] | None = None,
    rebuild: bool = False,
    config: AppConfig | None = None,
    embedding_client: EmbeddingClient | None = None,
    store: VectorStore | None = None,
    skip_unchanged: bool = False,
    default_groups: list[str] | None = None,
) -> IngestSummary:
    """把文件送进知识库。paths 为空时扫描整个知识目录。

    增量判断（V3.1）：全库扫描始终做指纹跳过；显式传 paths 默认是强制
    重新入库（管理台「重新入库」按钮的语义），skip_unchanged=True 可让
    显式路径同样做指纹跳过（网页入库等"内容没变就别重跑"的场景）。
    """
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
    from src import index_state as istate

    if not files:
        print(f"⚠️ 没有找到可入库的文件（知识目录：{cfg.knowledge_dir}）")
        if rebuild:
            # 重建 + 空目录 = 清空知识库：否则旧卡片永远留在检索里
            vector_store.clear()
            istate.save_state({})
            clear_qa_cache()
            from src.keyword_search import invalidate_cache
            invalidate_cache()
            return summary
        # 不能提前返回：删光目录里最后一个文件时，台账里它的向量仍需在下方
        # 「已删除文件同步清理」中清掉（此前在这里 return，残留向量永远参与检索）
    else:
        print(f"① 找到 {len(files)} 个待入库文件")

    # ①.5 增量判断（V3.1）：内容指纹没变的文件跳过；台账里有但文件已删除的，向量同步清理
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
        if not path.exists():
            summary.failed_files.append(f"{rel}（文件不存在）")
            continue
        current_hash = istate.file_hash(path)
        known = rel in state
        unchanged = known and state[rel].get("hash") == current_hash
        if unchanged and (not explicit or skip_unchanged):
            summary.skipped_files.append(rel)
            continue
        if unchanged:
            # 强制重跑且内容未变：按 new 处理（同 point_id 覆盖旧卡片，
            # 不升版本、不 expire），否则重新入库会让版本号虚涨
            summary.new_files.append(rel)
        else:
            # 分类必须无条件进行：updated_files 驱动旧版本 expire，
            # 漏分类会导致新旧两代卡片同时 active 参与检索
            (summary.updated_files if known else summary.new_files).append(rel)
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
        # 删除了知识就必须清问答缓存：否则同问题命中缓存返回已删除知识的答案
        clear_qa_cache()
        from src.keyword_search import invalidate_cache
        invalidate_cache()
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
        # V3.2 版本管理：只有内容真的变化（updated）才升版本号；
        # 强制重入库（内容未变）不涨版本，否则每次点「重新入库」版本都 +0.1
        prev_version = state.get(rel, {}).get("version")
        if (rel in summary.updated_files and prev_version
                and "version" not in (parsed.front_matter or {})):
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
            if key in ("title", "category", "topic", "tags", "version", "status",
                       "allowed_groups") and value:
                parsed.metadata[key] = value
        # V3.7 权限：上传表单的"可见部门"作为批级默认——只在 Front Matter 未显式
        # 声明（即仍为默认全员）时生效，不覆盖档案的显式设置
        if default_groups is not None and parsed.metadata.get("allowed_groups") == ["all"]:
            groups = [str(g).strip() for g in default_groups if str(g).strip()]
            parsed.metadata["allowed_groups"] = groups or ["all"]
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
        # 按相对路径聚合：不同子目录的同名文件不能混叠卡片数
        rel_path = chunk.metadata["path"]
        chunks_by_file[rel_path] = chunks_by_file.get(rel_path, 0) + 1
    for doc in cleaned_docs:
        meta = doc.metadata
        summary.file_rows.append({
            "source": meta["source"],
            "path": meta["path"],
            "domain": meta["domain"],
            "category": meta["category"],
            "status": meta["status"],
            "chunks": chunks_by_file.get(meta["path"], 0),
        })
    print(f"   共切出 {len(chunks)} 张知识卡片")
    if not chunks:
        return summary

    # ⑦ 台账记录函数：只为「解析且切片成功」的文件记录指纹。
    # 解析失败的文件刻意不记——旧向量保持原状，下次入库自动重试；
    # 若记录了失败文件的新指纹，新内容会被增量判断永久跳过。
    versions_by_rel = {d.metadata["path"]: d.metadata.get("version", "1.0") for d in cleaned_docs}

    def _record_ledger() -> None:
        for path, rel, file_hash_value in processed:
            if rel not in versions_by_rel:
                continue
            doc_id = next(
                (c.metadata["document_id"] for c in chunks if c.metadata["path"] == rel),
                document_id_for(rel),
            )
            chunk_count = sum(1 for c in chunks if c.metadata["path"] == rel)
            istate.update_entry(state, rel, doc_id, chunk_count, file_hash_value,
                                version=versions_by_rel[rel])

    if not chunks:
        # 全部文件都没有切出卡片（空文件等）：也记台账（0 卡片），
        # 否则这些文件每次入库都重跑、重复花 LLM 标签的钱
        _record_ledger()
        istate.save_state(state)
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
    vector_store.ensure_collection(summary.vector_dimension)
    if not rebuild:
        # 台账损坏/丢失时 new 文件在向量库里可能有旧残留，入库前清掉（幂等），
        # 避免「台账说新、库里有旧」时新旧两代卡片同时 active
        for doc in cleaned_docs:
            if doc.metadata["path"] in summary.new_files:
                vector_store.delete_documents([doc.metadata["document_id"]])
        # V3.2 版本管理：内容有变化的文件，旧版本整体转 expired 留作历史（新版本随后写入）
        for doc in cleaned_docs:
            if doc.metadata["path"] in summary.updated_files:
                vector_store.expire_document(doc.metadata["document_id"])
    print("⑥ 存入 Qdrant 向量库")
    stored = vector_store.upsert_chunks(chunks, vectors)
    # 台账落盘放在向量写入成功之后：中途任何一步失败（Ollama 掉线、Qdrant 异常）
    # 都不落盘，下次入库自动重试，不会出现「指纹已记录但内容没进库」的死锁
    _record_ledger()
    istate.save_state(state)
    # 入库后清空 BM25 缓存和 Q→A 缓存
    from src.keyword_search import invalidate_cache
    invalidate_cache()
    clear_qa_cache()
    print(f"   已写入 {stored} 张卡片")

    summary.elapsed_seconds = time.time() - start
    print(f"✅ 入库完成，总耗时 {summary.elapsed_seconds:.1f} 秒")
    print("\n入库明细：")
    for row in summary.file_rows:
        print(f"   {row['path']:<40} domain={row['domain']:<10} category={row['category']:<12} "
              f"status={row['status']:<8} 卡片数={row['chunks']}")
    return summary


# ---------------------------------------------------------------- Q→A 缓存
# 简单内存缓存：同一问题 + 同一配置 → 直接返回上次结果（不重跑流水线）
_qa_cache: dict[str, QAResult] = {}
# V3.7.2 缓存归属：键 → 用户名（多用户下"清空我的缓存"只清自己的条目；
# 未登录的个人模式/离线评测条目 owner 为空串，仅"清空全部"能触及）
_qa_cache_owner: dict[str, str] = {}
_QA_CACHE_MAX = 50  # 最多缓存 50 条问答
# Streamlit 多标签页 = 多线程：入库/缓存读写需要锁保护，
# 否则驱逐点与并发清空可竞态崩溃，台账也会互相覆盖
_ingest_lock = threading.Lock()


def merge_sub_query_results(per_sq_results: list[list[RetrievedItem]],
                            pool_cap: int = 20) -> tuple[list[RetrievedItem], int]:
    """复合问题拆分检索的合并（V3.4 查询拆分）：各子查询结果合并去重成一个候选池。

    口径：按「名次」而不是 item.score 比较——各子查询结果已按融合名次排好序，但
    item.score 是各通道的原始分（向量相似度 0~1、BM25 可达数十），量级不同、不能混排。
    此前按原始分挑保底名额，召回加深（枚举模式 50 条）且跳过精排时，BM25 高分卡片
    挤掉了向量通道第 1 名的证据（复合题「心得」类子问题整体丢失）。

    规则：名次换算可比分 1/(60+名次)（与 RRF 同口径），同卡多子查询命中取最好名次；
    每个子查询先按自身名次保底取前 N 条（N = 合并池上限 // 子查询数，至少 3），
    保证每个子问题的证据不被其他主题挤掉；剩余名额按可比分补足；
    最终按可比分排序（各子查询的第 1 名在前、依次交替）并重新编号。

    返回 (候选池, 每个子查询的保底名额)。
    """
    def card_key(it: RetrievedItem) -> str:
        return (it.metadata.get("chunk_id")
                or f'{it.metadata.get("source", "?")}|{(it.text or "")[:40]}')

    best: dict[str, tuple[float, RetrievedItem]] = {}
    for sub_items in per_sq_results:
        for pos, it in enumerate(sub_items, start=1):
            key, rank_score = card_key(it), 1.0 / (60 + pos)
            if key not in best or rank_score > best[key][0]:
                best[key] = (rank_score, it)

    quota = max(3, pool_cap // max(len(per_sq_results), 1))
    selected_keys: set[str] = set()
    selected: list[RetrievedItem] = []
    for sub_items in per_sq_results:
        picked = 0
        for it in sub_items:  # 已按融合名次排序
            key = card_key(it)
            if key not in selected_keys and len(selected) < pool_cap:
                selected_keys.add(key)
                selected.append(best[key][1])
                picked += 1
            if picked >= quota:
                break
    for key, (_, it) in sorted(best.items(), key=lambda kv: -kv[1][0]):
        if key not in selected_keys and len(selected) < pool_cap:
            selected_keys.add(key)
            selected.append(it)

    pool = sorted(selected, key=lambda it: -best[card_key(it)][0])[:pool_cap]
    for i, it in enumerate(pool, start=1):
        it.rank = i
    return pool, quota


def expand_dominant_documents(items: list[RetrievedItem], filters: dict | None,
                              min_cards: int = 4, max_docs: int = 2,
                              max_doc_cards: int = 40, keep_top: int = 10
                              ) -> tuple[list[RetrievedItem], list[str]]:
    """枚举模式的文档展开（父文档检索思路）：列举题要的是「某一文档的全部条目」。

    按相关度召回（哪怕 50 条）只能接近、不能保证全量——跨语言时（中文问英文网页）
    部分条目卡片会排到召回上限之外（实测 Xtools 15 项功能有 2~3 项落在 50 名外）。
    规则：
    - 在整个候选池里卡片数 ≥ min_cards、且在前 keep_top 名里出现过的文档（同版本）中，取卡片
      最多的 max_docs 篇视为「被列举的文档」——数量按整池统计：跨语言时目标文档名次偏后，
      但数量依然最多；同时要求进过前排，排除小知识库里凑巧卡片多、实则无关的文档；
    - 只展开装得下的文档（全文 ≤ max_doc_cards 张，即上下文名额）：装不下的长文档谈不上
      「补齐全部」，展开只会把噪声塞满名额（实测 79 张的长文档被展开后，把只有 1 张卡片的
      「心得」从前排挤到第 94 位而落选）；
    - 候选池原有的前 keep_top 名位置不动，展开文档的其余卡片排在其后（已召回的保持原顺序，
      补入的按卡片序号），其余候选随后——最相关的证据永远不被展开挤掉；
    - 补卡沿用本次检索的过滤条件（权限 / 状态，不越权），再加 document_id + version。

    返回 (新候选池, 展开说明)；没有满足条件的文档时原样返回。
    """
    def doc_key(it: RetrievedItem) -> tuple:
        return (it.metadata.get("document_id"), it.metadata.get("version"))

    counts = Counter(doc_key(it) for it in items if it.metadata.get("document_id"))
    top_docs = {doc_key(it) for it in items[:keep_top]}
    # 还要在前 keep_top 名里出现过：只展开「确实最相关」的文档，而不是小知识库里凑巧卡片多的文档
    dominant = [key for key, n in counts.most_common() if n >= min_cards and key in top_docs][:max_docs]
    if not dominant:
        return items, []

    store = get_vector_store()
    seen = {it.metadata.get("chunk_id") for it in items}
    head = items[:keep_top]                      # 原有前排：位置不动
    head_ids = {id(it) for it in head}
    middle: list[RetrievedItem] = []             # 展开文档的其余卡片
    notes: list[str] = []
    for doc_id, version in dominant:
        in_pool = [it for it in items if doc_key(it) == (doc_id, version)]
        source = in_pool[0].metadata.get("source", "?")
        doc_filters = {**(filters or {}), "document_id": doc_id}
        if version:
            doc_filters["version"] = version
        cards = store.get_all_cards(filters=doc_filters, limit=max_doc_cards + 1)
        if len(cards) > max_doc_cards:
            notes.append(f"{source}：全文超过 {max_doc_cards} 张、上下文装不下，不展开")
            continue
        added: list[RetrievedItem] = []
        for card in sorted(cards, key=lambda c: str(c["payload"].get("chunk_id") or "")):
            payload = card["payload"]
            if payload.get("chunk_id") in seen:
                continue
            seen.add(payload.get("chunk_id"))
            added.append(RetrievedItem(rank=0, score=0.0, text=payload.get("text", ""),
                                       metadata={k: v for k, v in payload.items() if k != "text"},
                                       channels="文档展开"))
        middle += [it for it in in_pool if id(it) not in head_ids] + added
        notes.append(f"{source}：候选池中 {counts[(doc_id, version)]} 张 → "
                     f"补入其余 {len(added)} 张（全文 {len(cards)} 张进入候选）")

    placed = head_ids | {id(it) for it in middle}
    expanded = head + middle + [it for it in items if id(it) not in placed]
    for i, it in enumerate(expanded, start=1):
        it.rank = i
    return expanded, notes


def _cache_key(question: str, top_k: int, filters: dict, qu: bool, hybrid: bool, rerank: bool,
               history: list[dict] | None = None) -> str:
    # 对话历史参与键：同样的短追问（"继续""展开讲讲"）在不同上下文里答案完全不同，
    # 不带 history 会跨会话/跨轮次串答案
    hist_sig = "|".join(
        f"{m.get('role')}:{(m.get('content') or '')[:60]}" for m in (history or [])
    )
    # 回答语言参与键（B14.1）：判断不出语言的追问会沿用历史提问的语言，而 hist_sig 只取前 60 字，
    # 显式带上判定结果，保证同一键永远对应同一回答语言
    answer_lang = resolve_answer_language(question, history)
    return (f"{question.strip().lower()}|k={top_k}|f={sorted((filters or {}).items())}"
            f"|qu={qu}|hy={hybrid}|rr={rerank}|h={hist_sig}|lang={answer_lang}")


def clear_qa_cache(username: str | None = None) -> None:
    """清空问答缓存。username 传入时只清该用户的条目（"清空我的缓存"）；
    None 清全部（入库/删除知识后的系统级清理）。"""
    if username is None:
        _qa_cache.clear()
        _qa_cache_owner.clear()
        return
    for key in [k for k, owner in _qa_cache_owner.items() if owner == username]:
        _qa_cache.pop(key, None)
        _qa_cache_owner.pop(key, None)


# V3.8：缓存内容对应的索引纪元。知识库被任何进程改过（纪元变了）→ 整个问答缓存作废。
# 此前只靠本进程内的 clear_qa_cache()：MCP 是独立进程，前台入库新版本后它仍返回旧答案。
_qa_cache_epoch = ""


def _sync_qa_cache_epoch() -> str:
    global _qa_cache_epoch
    epoch = index_epoch.current()
    if epoch != _qa_cache_epoch:
        _qa_cache.clear()
        _qa_cache_owner.clear()
        _qa_cache_epoch = epoch
    return epoch


# ---------------------------------------------------------------- 问答
def answer_stream(
    question: str,
    top_k: int | None = None,
    use_llm: bool = True,
    filters: dict | None = None,
    use_query_understanding: bool = True,
    use_hybrid: bool = True,
    use_rerank: bool = True,
    history: list[dict] | None = None,
    on_progress=None,
    config: AppConfig | None = None,
    user_groups: list[str] | None = None,
    username: str | None = None,
):
    def _p(msg):
        if on_progress:
            on_progress(msg)

    """流式问答：history 传入最近对话实现多轮（解析代词+上下文），不增加 LLM 调用。

    生成器迭代结束后，result.answer / result.elapsed / result.trace 才是完整值。
    filters 例：{"domain": "learning"}、{"status": "archive"}（界面手动设置，优先级最高）。
    use_query_understanding=True 时先用 LLM 改写提问并推断过滤条件（V2.1）。
    use_hybrid=True 时并行执行 BM25 关键词检索并用 RRF 融合（V2.3/V2.4）。
    use_rerank=True 时召回扩宽到 RERANK_RECALL_K 条，LLM 打分精排后取 Top K（V2.5）。
    """
    cfg = config or get_config()
    top_k = top_k or cfg.top_k

    # V3.7 权限：把可见部门并入 filters 的本地副本（服务端强制注入——
    # 此后 merged_filters / 渐进式放宽的 manual_filters / 两级缓存键全部自动带上，
    # 调用方在界面上改任何过滤条件都绕不开它）。None = 不限制（个人模式/离线评测）。
    from src.permissions import scope_filters
    filters = scope_filters(filters, user_groups)

    # Q→A 缓存：同一问题+同一配置+同一对话上下文直接返回上次结果；
    # 仅检索模式（use_llm=False）不读缓存——它要的是检索结果而非缓存的 LLM 答案
    cache_key = _cache_key(question, top_k, filters or {}, use_query_understanding,
                           use_hybrid, use_rerank, history=history)
    epoch_at_start = _sync_qa_cache_epoch()  # 知识库已变 → 旧答案作废
    # .get 取一次：判断与读取分两步时，并发清空会让第二步抛 KeyError
    cached_entry = _qa_cache.get(cache_key) if use_llm else None
    if cached_entry is not None:
        import copy
        cached = copy.deepcopy(cached_entry)
        # 缓存命中：加一条时间线节点说明（不重跑流水线）
        cached.trace.append(make_node(
            "⚡", "Q→A 缓存命中", time_str=now_str(), status="已命中",
            summary="同一问题 + 同一检索配置，直接返回上次结果——"
                    "不重跑 Query 理解 / 检索 / Rerank / LLM 生成（0 次 API 调用，0 秒）",
            items=[
                ("缓存条件", f"问题：{question[:50]}……"),
                ("上次耗时", f"{cached.elapsed.get('total', 0):.2f} 秒"),
                ("本次耗时", "≈0 秒（直接返回缓存）"),
            ],
        ))
        return cached, iter([])

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

    # 节点：会话记忆 / 多轮对话（V3：结合历史解析代词 + 上下文生成）
    recent_history = [m for m in (history or []) if m.get("role") in ("user", "assistant") and m.get("content")][-6:]
    if recent_history:
        trace.append(make_node(
            "💬", "会话记忆 / 多轮对话", time_str=now_str(), status="已执行",
            summary="检测到对话历史，本轮结合最近对话理解当前问题：Query 理解时解析代词"
                    "（'它多少钱'→'Qdrant 的价格'），生成时带上历史上下文",
            items=[
                ("输入", f"当前问题 1 条 + 历史消息 {len(recent_history)} 条（最近 {len(recent_history) // 2} 轮）"),
                ("历史摘要", "\n".join(
                    f"{'用户' if m['role'] == 'user' else '助手'}：{m['content'][:80]}……"
                    for m in recent_history[-4:]
                )),
                ("输出", "历史传入 Query 理解（代词消解）+ LLM 生成（上下文连贯），不增加 LLM 调用"),
            ],
        ))
    else:
        trace.append(make_node(
            "💬", "会话记忆 / 多轮对话", time_str=now_str(), status="本轮无历史（首轮提问）",
            summary="当前是新会话的第一轮提问，没有历史可结合。多轮时系统会自动带上最近对话",
            items=[("说明", "传入历史后：Query 理解解析代词 + LLM 上下文连贯")],
        ))

    # 列举/盘点类提问：进入枚举通道（V3.8.1：召回 50 / 名额 40、跳过精排、token 12000）。
    # 双通道判定：Query 理解的语义识别（enumeration 字段）为主，
    # 关键词词表为兜底（Query 理解关闭或解析失败时依然生效）。
    # 注意：判定必须用原始问题——Query 理解改写后的查询已丢失计数语气。
    # 参数在节点②之后统一应用（见 use_rerank_now 处）
    enumeration_mode = any(w in question for w in (
        "有哪些", "列出", "都有什么", "列举", "全部",
        "那些", "哪些", "都有", "所有", "什么功能", "什么工具",
        "功能列表", "几个", "多少个", "多少种",
        # 计数类问法（如「工作经历有几份」）：答案需要凑齐全部条目，
        # 漏一个词就会召回不全导致计数偏小
        "几份", "几段", "几条", "几次", "几轮", "几家", "几个方面",
        "多少份", "多少段", "多少条", "多少次", "多少轮", "多少家",
    ))
    recall_k = top_k            # 基线：非枚举 = top_k（精排开启时在下方过采样）
    ctx_max_items = top_k

    # 节点 ②：Query 理解 / 改写（V2：一次 LLM 调用完成改写 + 关键词 + 过滤条件推断）
    search_query = question
    qu_filters: dict = {}
    qu = None
    if use_query_understanding and use_llm:
        _p("🧠 Query 理解中…")
        qu_start = now_str()
        llm = get_llm_client()
        try:
            catalog = sorted({d["category"] for d in get_vector_store().list_documents()})
        except Exception:
            catalog = []
        qu = understand_query(question, llm=llm, config=cfg, categories=catalog, history=history)
        search_query = qu.vector_query or question
        qu_filters = dict(qu.filters)
        # 语义判定合并：LLM 识别出计数/列举意图而词表没命中时，同样进入枚举通道——
        # 覆盖「经历过几家公司」这类词表外的任意计数问法
        if qu.enumeration:
            enumeration_mode = True
        # 返回字段逐项解析：B3 规范的 7 个字段全部展示（未输出 = LLM 按规范省略，取默认值）
        fields_dump = (
            f"intent（意图概括）: {qu.intent or '（未输出）'}\n"
            f"vector_query（改写后检索语句）: {qu.vector_query}\n"
            f"keyword_query（关键词，供关键词检索用）: {', '.join(qu.keyword_query) or '（未输出）'}\n"
            f"enumeration（计数/列举意图，触发召回扩容）: {qu.enumeration}\n"
            f"sub_queries（复合问题拆分，逐子查询检索）: {('；'.join(qu.sub_queries)) if qu.sub_queries else '（未输出，单信息需求）'}\n"
            f"time_range（解析的时间范围）: {qu.time_range or '（未输出）'}\n"
            f"filters（推断的过滤条件）: {qu.filters or '（未输出）'}")
        if qu.ok and qu.error:
            fields_dump += f"\n校验提示: {qu.error}"
        request_dump = (f"模型: {cfg.llm_model or '-'}　|　温度: {cfg.llm_temperature}　|　"
                        f"消息数: 2 条（System 任务说明书 + User 请求）")
        trace.append(make_node(
            "🧠", "Query 理解 / 改写", time_str=qu_start, elapsed=qu.elapsed,
            status="已执行" if qu.ok else "回退（解析失败，使用原始问题检索）",
            summary="用一次 LLM 调用完成问题理解：改写查询 + 提关键词 + 推断过滤 + "
                    "计数意图判定 + 复合问题拆分；失败自动回退为原始问题",
            items=[
                ("① 调用参数（发给 LLM 的完整请求）", request_dump),
                ("② System Prompt（任务说明书全文）", qu.system_prompt),
                ("③ User Prompt（时间+领域枚举+分类+历史+原始问题）", qu.user_prompt),
                ("④ LLM 原始输出（返回的 JSON 全文）", qu.raw_output or qu.error),
                ("⑤ 返回字段逐项解析（未输出 = 按规范省略）", fields_dump),
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
    # V3.8.1 枚举通道：识别到列举/计数意图后，整条管线切换到枚举参数集——
    # 召回 50（全文档卡片全量进池）、跳过精排（枚举要"全"不要"精"，还省一次 LLM 调用）、
    # 名额 40、token 预算 12000（全量卡片进上下文）。非枚举提问完全不受影响。
    if enumeration_mode:
        recall_k = ENUM_RECALL_K
        ctx_max_items = ENUM_CTX_ITEMS
        ctx_max_per_doc = 999
        use_rerank_now = False
        enum_ctx_cfg = replace(cfg, context_max_tokens=ENUM_CTX_TOKENS)
    else:
        use_rerank_now = use_rerank and use_llm
        recall_k = max(recall_k, cfg.rerank_recall_k) if use_rerank_now else recall_k
        ctx_max_per_doc = None  # 非枚举：组装时回落到 .env 的 CONTEXT_MAX_PER_DOC
        enum_ctx_cfg = cfg
    merged_filters = {**qu_filters, **(filters or {})}
    if qu and qu.ok and qu.time_range:
        merged_filters["updated_at"] = qu.time_range  # V3.2 时间感知：按文档时间过滤
    result.filters = effective_filters(merged_filters)
    _p("🔍 数据检索中…")
    t0 = time.time()

    # 节点 ②.5：查询拆分检索（V3.4 Sub-Query）——复合问题（一个问题多个信息需求）
    # 由 Query 理解拆成多个子查询，逐个独立检索后合并去重。
    # 不拆分时走原始单查询检索；两种路径产出统一为 result.retrieved 供精排。
    sub_q_lists = [s for s in ((qu.sub_queries if (qu and qu.ok) else []) or []) if s and s.strip()][:4]

    if len(sub_q_lists) >= 2:
        sq_start = now_str()
        sq_lines = []
        # 每个子查询独立检索，各自保留原始结果列表
        per_sq_results: list[list] = []
        for i, sq in enumerate(sub_q_lists, start=1):
            sub_items = retriever.retrieve(
                sq, top_k=recall_k, filters=merged_filters, trace=None,
                keyword_query=None, use_hybrid=use_hybrid,
            )
            per_sq_results.append(sub_items)
            top_srcs, seen = [], set()
            for it in sub_items:
                s = it.metadata.get("source", "?")
                if s not in seen:
                    seen.add(s)
                    top_srcs.append(s[:24])
                if len(top_srcs) >= 3:
                    break
            sq_lines.append(f"子查询 {i}：「{sq}」→ {len(sub_items)} 条｜主要来源：{'、'.join(top_srcs)}")

        # 合并去重 + 保底配额（见 merge_sub_query_results）。
        # 枚举模式（列举/计数）合并池放宽到上下文名额，避免拆分后列举不全
        _pool_cap = max(20, ctx_max_items) if enumeration_mode else 20
        pool, _quota = merge_sub_query_results(per_sq_results, _pool_cap)
        result.retrieved = pool

        trace.append(make_node(
            "🔀", "查询拆分检索（Sub-Query）", time_str=sq_start,
            summary=(f"复合问题拆分为 {len(sub_q_lists)} 个子查询，逐个独立检索后合并去重："
                     f"共 {len(pool)} 张不同卡片进入精排——避免混合查询让两个主题互相稀释"),
            items=[
                ("各子查询命中明细", "\n".join(sq_lines)),
                ("保底配额", f"每个子查询保底 {_quota} 条（按子查询内的融合名次）"),
                ("合并策略", "同卡多子查询命中取最好名次；按名次换算可比分 1/(60+名次) 排序"
                             "（各通道原始分量级不同，不可混排）"),
                ("合并后候选池", f"{len(pool)} 条（上限 {_pool_cap} 条）"),
            ],
        ))

        # 查询拆分模式：N 个子查询 = N 个独立信息需求，最终选取同步放宽——
        # 3 个主题竞争 5 个名额时必有一个被挤出，与计数扩容同理
        ctx_max_items = max(ctx_max_items, top_k * 2)
    else:
        result.retrieved = retriever.retrieve(
            search_query, top_k=recall_k, filters=merged_filters, trace=trace,
            keyword_query=(qu.keyword_query if qu and qu.ok else None),
            use_hybrid=use_hybrid,
        )

    # 渐进式放宽（检索容错）：LLM 推断的过滤条件猜错时可能把正确答案挡在门外——
    # 如果「带推断条件」检索为 0 条，自动去掉推断条件（保留手动设置）重试一次。
    # filters 里是手动设置与权限域（V3.7 起登录用户必带），重试时原样保留，不会越权。
    if not result.retrieved and qu_filters:
        relax_start = now_str()
        t_relax = time.time()
        manual_filters = filters or {}
        result.retrieved = retriever.retrieve(
            search_query, top_k=recall_k, filters=manual_filters, trace=None,
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

    # 节点 ⑥.5（枚举模式）：文档展开——列举题要的是「某一文档的全部条目」，按相关度召回只能
    # 接近全量（跨语言时部分条目排在召回上限之外）。只在单查询枚举时展开：
    # 复合问题拆分时各子问题靠保底配额保护，展开某一篇会挤占其他子问题的证据
    if enumeration_mode and len(sub_q_lists) < 2 and result.retrieved:
        expand_start = now_str()
        result.retrieved, expand_notes = expand_dominant_documents(
            result.retrieved, result.filters, max_doc_cards=ctx_max_items)
        if expand_notes:
            trace.append(make_node(
                "📚", "文档展开（枚举模式）", time_str=expand_start,
                status="已执行" if any("补入" in n for n in expand_notes) else "未展开（文档超出上下文名额）",
                summary=f"列举题要「某一文档的全部条目」：候选池中卡片最多的文档（≥4 张，最多 2 篇，"
                        f"全文 ≤{ctx_max_items} 张才展开）按同样的权限与状态条件补齐全部卡片；"
                        f"候选池原有前 10 名位置不动，展开的卡片排在其后",
                items=[("展开明细", "\n".join(expand_notes)),
                       ("展开后候选池", f"{len(result.retrieved)} 条")],
            ))
    result.elapsed["retrieval"] = time.time() - t0
    # 节点 ⑦：Rerank 精排（V2.5：LLM 逐条打分，宽召回精选）
    if use_rerank_now and result.retrieved:
        _p("🏆 Rerank 精排中…")
        rr_start = now_str()
        t_rr = time.time()
        outcome = rerank_items(question, result.retrieved, top_k=top_k,
                               llm=get_llm_client(), config=cfg)
        rr_elapsed = time.time() - t_rr
        # 全部打分候选交给上下文组装，由「多样性 + Top K」规则统一精选
        # 注意：不能按 Rerank 分数过滤 0 分卡片——实测发现 Rerank 可能误判
        # （给含答案的卡片打 0 分），过滤后反而丢失正确内容
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
    elif enumeration_mode and result.retrieved:
        trace.append(make_node(
            "🏆", "Rerank（精排）", time_str=now_str(),
            status="跳过（枚举模式）",
            summary="列举/计数类提问要「全」不要「精」：候选按粗排顺序（RRF 融合）直接进入组装，省一次 LLM 调用",
            items=[("说明", f"枚举模式：召回 {recall_k} 条候选；上下文最多 {ctx_max_items} 条、"
                           f"约 {ENUM_CTX_TOKENS} tokens，同文档不限条数")],
        ))

    _p("📦 整理资料中…")
    # 节点 ⑧ 合并 / 去重 + 节点 ⑨ 上下文组装（V2.6：文档多样性 + 相邻补全）
    # 精排后不再截断候选池，由组装规则在全部候选中选出最终进入回答的 Top K
    merge_start = now_str()
    t1 = time.time()
    context, used, dropped, merge_notes = build_context(
        result.retrieved, enum_ctx_cfg, max_items=ctx_max_items, max_per_doc=ctx_max_per_doc
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
             f"token 上限 {cfg.context_max_tokens} 字"),
            ("相邻补全", "\n".join(merge_notes) if merge_notes else "（没有可拼接的相邻卡片）"),
            ("输出（最终 Context）", context),
            ("规模", f"共 {len(used)} 条资料，合计 {estimate_tokens(context)} tokens（上限约 {cfg.context_max_tokens} tokens）"),
        ],
    ))

    if not use_llm:
        return result, iter(())

    # 节点 ⑩⑪：LLM 调用（Prompt 组装 + 生成回答，合并展示）
    t2 = time.time()
    messages = build_messages(context, question, history=history,
                              enumerate_mode=enumeration_mode)
    llm = get_llm_client()

    def _generate():
        _p("✍️ 生成回答中…")
        llm_start = now_str()
        t3 = time.time()
        text = ""
        completed = False
        try:
            for delta in llm.chat_stream(messages):
                text += delta
                yield delta
            completed = True  # 正常跑完才会置 True；异常/用户中断都不会
        finally:
            # 思考标签跨 chunk 边界时逐段清洗会漏，回答定稿前对全文再清一次
            cleaned = strip_think_tags(text).strip()
            result.answer = cleaned or None
            result.elapsed["llm"] = time.time() - t3
            result.elapsed["total"] = result.elapsed.get("retrieval", 0) + result.elapsed["llm"]
            trace.append(make_node(
                "🤖", "LLM 调用（Prompt 组装 + 生成回答）",
                time_str=llm_start, elapsed=result.elapsed["llm"],
                summary="把「任务说明书 + 资料附页 + 用户问题」组装成消息发给大模型，"
                        "大模型基于资料流式生成回答",
                items=[
                    ("模型", cfg.llm_model or "-"),
                    ("输入 · System Prompt（任务说明书）", messages[0]["content"]),
                    ("输入 · User Prompt（资料附页 + 问题）", messages[-1]["content"]),
                    ("输出 · LLM 生成的回答", result.answer or "（空）"),
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
            # 只缓存「完整且非空」的回答：LLM 失败（异常）或用户中断（半截答案）不缓存，
            # 否则之后同样的问题永远命中这份坏结果
            # V3.8：生成期间知识库被改过（纪元变了）→ 这份答案基于旧数据，不能缓存
            if completed and result.answer and index_epoch.current() == epoch_at_start:
                # list() 快照 + pop(默认值)：并发 clear 时不抛 StopIteration/KeyError
                while len(_qa_cache) >= _QA_CACHE_MAX:
                    for oldest in list(_qa_cache)[: len(_qa_cache) - _QA_CACHE_MAX + 1]:
                        _qa_cache.pop(oldest, None)
                    if not _qa_cache:
                        break
                _qa_cache[cache_key] = result
                _qa_cache_owner[cache_key] = username or ""

    return result, _generate()


def answer_question(
    question: str,
    top_k: int | None = None,
    use_llm: bool = True,
    filters: dict | None = None,
    use_query_understanding: bool = True,
    use_hybrid: bool = True,
    use_rerank: bool = True,
    history: list[dict] | None = None,
    config: AppConfig | None = None,
    user_groups: list[str] | None = None,
    username: str | None = None,
) -> QAResult:
    """完整问答链路（一次性返回完整答案）。use_llm=False 时只做检索（不花钱）。"""
    result, deltas = answer_stream(
        question, top_k=top_k, use_llm=use_llm, filters=filters,
        use_query_understanding=use_query_understanding,
        use_hybrid=use_hybrid, use_rerank=use_rerank,
        history=history, config=config, user_groups=user_groups,
        username=username,
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

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
def _prepare_retrieval(
    question: str,
    top_k: int | None,
    filters: dict | None,
    cfg: AppConfig,
) -> QAResult:
    """问答共用的检索阶段：问题向量化 → 过滤 → 召回 → 组装资料。"""
    retriever = get_retriever()
    result = QAResult(question=question, answer=None, retrieved=[], context="",
                      sources=[], filters=effective_filters(filters))

    t0 = time.time()
    result.retrieved = retriever.retrieve(question, top_k=top_k, filters=filters)
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

    context, used, dropped = build_context(result.retrieved, cfg)
    result.context = context
    if cfg.debug and dropped:
        print(f"    Context 组装：采用 {len(used)} 条，丢弃 {dropped} 条")
    return result


def answer_stream(
    question: str,
    top_k: int | None = None,
    use_llm: bool = True,
    filters: dict | None = None,
    config: AppConfig | None = None,
):
    """流式问答：检索同步完成，回答逐段产出。返回 (完整结果记录, 文本块生成器)。

    生成器迭代结束后，result.answer / result.elapsed 才是完整值。
    filters 例：{"domain": "learning"}、{"status": "archive"}。
    默认强制 status=active；显式传 status 以传入值为准。
    """
    cfg = config or get_config()
    result = _prepare_retrieval(question, top_k, filters, cfg)

    if not use_llm:
        return result, iter(())

    messages = build_messages(result.context, question)
    llm = get_llm_client()

    def _generate():
        t1 = time.time()
        text = ""
        try:
            for delta in llm.chat_stream(messages):
                text += delta
                yield delta
        finally:
            result.answer = text.strip() or None
            result.elapsed["llm"] = time.time() - t1
            result.elapsed["total"] = result.elapsed.get("retrieval", 0) + result.elapsed["llm"]

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

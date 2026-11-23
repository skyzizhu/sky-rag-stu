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
import hashlib
import json
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

from src import index_epoch
from src.answer_evaluator import merge_usage
from src.answer_format import answer_citations, remove_invalid_citations, split_answer_status
from src.enumeration_processing import process_enumeration, render_records
from src.conversation_context import prepare_context
from src.console import console_print
from src.chunker import clean_and_chunk
from src.config import AppConfig, get_config
from src.context import build_context, estimate_tokens
from src.embedding import EmbeddingClient, get_embedding_client
from src.evidence_support import assess_evidence, deadline_constraint, preserve_deadline_wording
from src.document_department import normalize_department, resolve_department
from src.document_type import normalize_doc_type, resolve_doc_type
from src.enumeration import (enumeration_route, is_staff_total_question, catalog_time, document_representatives,
                             document_key, group_documents, expand_documents, coverage_notice)
from src.llm import LLMClient, get_llm_client, new_usage, strip_think_tags
from src.metadata import build_document_metadata, chunk_sequence, document_id_for, version_from_filename
from src.permissions import classify_visibility, normalize_visibility
from src.document_visibility import prune_user_grants, resolve_visibility
from src.parser import ParsedDocument, SUPPORTED_EXTENSIONS, parse_file
from src.project_directories import is_generated_info, managed_directory_names, without_storage_directory
from src.project_comparison import (
    MAX_COMPARISON_PROJECTS, ProjectComparison, parse_project_comparison, render_project_comparison,
)
from src.project_document_evidence import parse_project_mentions, scan_project_mentions
from src.projects import list_projects, resolve_project_filter
from src.answer_language import resolve_answer_language
from src.prompt import build_messages
from src.query_understanding import understand_query
from src.query_scope import (
    conversation_project, extract_scope, inventory_domain_scope, inventory_needs_content,
    inventory_question_text, is_scope_listing, name_project_in_followup,
    project_reference, recover_inventory_project, resolve_scope, selected_project_text, unsupported_scope,
)
from src.reranker import rerank as rerank_items, rerank_local as rerank_documents_local
from src.retriever import RetrievedItem, effective_filters, get_retriever
from src.retrieval_intent import (VERSION_SIDES, compares_versions, comparison_statuses,
                                  enumerated_sub_queries, explicit_file_dates)
from src.trace import make_node, now_str
from src.vector_store import VectorStore, get_vector_store

# 列举召回与上下文预算（不代表全库完整覆盖）；文档精排/展开预算见 AppConfig。
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
    file_errors: dict[str, str] = field(default_factory=dict)
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
    conversation_meta: dict = field(default_factory=dict)  # 本轮单会话上下文的范围、压缩与召回信息
    enumerated_items: list[dict] = field(default_factory=list)  # 带已验证原文引用的提取记录
    enumeration: bool = False  # V3.9：本次提问是否走枚举通道（Evidence/生成共用）
    evidence_meta: dict | None = None  # V3.9：coverage 粗判定等 Evidence 元信息
    usage: dict = field(default_factory=dict)  # 本次回答的 LLM 消耗合计（B16.1；评估的用量在 evaluation["usage"]）
    request_usage: dict | None = None  # 本次实际用量；缓存命中时与原回答展示用量分离。
    cache_hit: bool = False


# ---------------------------------------------------------------- 入库
def _skip_hidden(p: Path, root: Path) -> bool:
    """路径中任一段以点开头（隐藏目录，如 .rag_converted 转换缓存）→ 跳过。
    rglob("*") 会深入点目录，仅检查文件名不含点会让转换缓存被当新文档重复入库。"""
    return any(part.startswith(".") for part in p.relative_to(root).parts)


def ingest_files(
    paths: list[Path] | None = None,
    rebuild: bool = False,
    config: AppConfig | None = None,
    embedding_client: EmbeddingClient | None = None,
    store: VectorStore | None = None,
    skip_unchanged: bool = False,
    default_groups: list[str] | None = None,
    owner: str | None = None,
    *,
    default_doc_type: str | None = None,
    default_department: str | None = None,
    default_project_id: int | None = None,
    default_allowed_users: list[str] | None = None,
    doc_type_paths: dict[str, str] | None = None,
) -> IngestSummary:
    """把文件送进知识库。paths 为空时扫描整个知识目录。

    增量判断（V3.1）：全库扫描始终做指纹跳过；显式传 paths 默认是强制
    重新入库（管理台「重新入库」按钮的语义），skip_unchanged=True 可让
    显式路径同样做指纹跳过（网页入库等"内容没变就别重跑"的场景）。
    default_groups（V3.7）：本批文件的默认可见部门（上传表单的"可见部门"选择）。
    优先级：Front Matter 显式声明 > 表单选择 > 默认全员；空列表一律落为 ["all"]。
    owner（V3.9）：本批文件的入库人（登录用户名），写入每张卡片的元数据。
    default_doc_type：非空上传默认类型；人工档案 > 文件声明 > 默认 > 保守推断。
    doc_type_paths：实际输出文件的绝对 str 路径 → 含选定根目录的原始上传相对路径。
    未提供原始路径只检查非网页文件名，不从入库目标 domain 推断类型。
    default_department：非空上传默认归属部门（档案字段，与权限无关）；
    人工档案 > 文件声明 > 默认，无任何推断。
    default_allowed_users：受限名单（员工用户名）；与 allowed_groups 互补。
    """
    # 多标签页并发点入库会互相覆盖台账/向量数据，入库全程串行化（单用户无感知）
    with _ingest_lock:
        return _ingest_files_impl(paths=paths, rebuild=rebuild, config=config,
                                  embedding_client=embedding_client, store=store,
                                  skip_unchanged=skip_unchanged,
                                  default_groups=default_groups, owner=owner,
                                  default_doc_type=default_doc_type,
                                  default_department=default_department,
                                  default_project_id=default_project_id,
                                  default_allowed_users=default_allowed_users,
                                  doc_type_paths=doc_type_paths)


def _ingest_files_impl(
    paths: list[Path] | None = None,
    rebuild: bool = False,
    config: AppConfig | None = None,
    embedding_client: EmbeddingClient | None = None,
    store: VectorStore | None = None,
    skip_unchanged: bool = False,
    default_groups: list[str] | None = None,
    owner: str | None = None,
    *,
    default_doc_type: str | None = None,
    default_department: str | None = None,
    default_project_id: int | None = None,
    default_allowed_users: list[str] | None = None,
    doc_type_paths: dict[str, str] | None = None,
) -> IngestSummary:
    """把文件送进知识库。paths 为空时扫描整个知识目录。

    增量判断（V3.1）：全库扫描始终做指纹跳过；显式传 paths 默认是强制
    重新入库（管理台「重新入库」按钮的语义），skip_unchanged=True 可让
    显式路径同样做指纹跳过（网页入库等"内容没变就别重跑"的场景）。
    """
    cfg = config or get_config()
    if default_doc_type is not None:
        default_doc_type = normalize_doc_type(default_doc_type)
    if default_department is not None:
        default_department = normalize_department(default_department)
    if default_project_id is not None:
        from src.projects import validate_project_id

        validate_project_id(default_project_id)
    embedding = embedding_client or get_embedding_client()
    vector_store = store or get_vector_store()
    from src.auth_db import (document_acl_write, document_user_revocations, list_users,
                             normalize_department_groups)
    from src.ingest_publication import (publish, publication_owns_token, publication_pending,
                                        recover_publication)
    from src.vector_store import VectorStoreError

    summary = IngestSummary(rebuilt=rebuild)
    start = time.time()
    from src import index_state as istate

    if publication_pending():
        with document_acl_write() as (_, retire_token):
            recover_publication(vector_store, retire_token)

    # ① 收集文件
    if paths:
        files = list({Path(p).resolve(): Path(p) for p in paths}.values())
    else:
        files = sorted(
            p for p in cfg.knowledge_dir.rglob("*")
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in
            SUPPORTED_EXTENSIONS  # 与 parser 白名单同一份，新增格式只改一处
            and not _skip_hidden(p, cfg.knowledge_dir)  # 跳过隐藏目录（.rag_converted 转换缓存等）
        )
    files = [path for path in files if not is_generated_info(path)]
    summary.total_files = len(files)

    if not files:
        console_print(f"⚠️ 没有找到可入库的文件（知识目录：{cfg.knowledge_dir}）")
        if rebuild:
            # 重建 + 空目录 = 清空知识库：否则旧卡片永远留在检索里
            with document_acl_write():
                vector_store.clear()
                istate.save_state({}, strict=True)
            clear_qa_cache()
            from src.keyword_search import invalidate_cache
            invalidate_cache()
            return summary
        # 不能提前返回：删光目录里最后一个文件时，台账里它的向量仍需在下方
        # 「已删除文件同步清理」中清掉（此前在这里 return，残留向量永远参与检索）
    else:
        console_print(f"① 找到 {len(files)} 个待入库文件")

    # ①.5 增量判断（V3.1）：内容指纹没变的文件跳过；台账里有但文件已删除的，向量同步清理
    previous_state = istate.load_state(strict=not rebuild)
    if not rebuild and any(not (cfg.knowledge_dir / rel).exists() for rel in previous_state):
        with document_acl_write() as (_, retire_token):
            recover_publication(vector_store, retire_token)
            previous_state = istate.load_state(strict=True)
            missing = {rel: entry.get("document_id") or document_id_for(rel)
                       for rel, entry in previous_state.items() if not (cfg.knowledge_dir / rel).exists()}
            if missing:
                vector_store.delete_documents(list(missing.values()))
                for rel in missing:
                    istate.remove_entry(previous_state, rel)
                summary.cleaned_files.extend(missing)
                istate.save_state(previous_state, strict=True)
                console_print(f"   已同步清理 {len(summary.cleaned_files)} 个已删除文件的向量数据")
    state = {} if rebuild else previous_state
    processed: list[tuple[Path, str, str]] = []  # (路径, 相对路径, 哈希)
    explicit = bool(paths)
    candidates = []
    knowledge_dir = cfg.knowledge_dir.resolve()
    for path in files:
        rel = path.name
        try:
            rel = path.resolve().relative_to(knowledge_dir).as_posix()
        except ValueError:
            pass
        if not path.exists():
            summary.failed_files.append(f"{rel}（文件不存在）")
            summary.file_errors[rel] = "file_missing"
            continue
        try:
            current_hash = istate.file_hash(path)
        except OSError:
            summary.failed_files.append(f"{rel}（无法读取文件）")
            summary.file_errors[rel] = "file_unreadable"
            continue
        candidates.append((path, rel, current_hash))

    skip_candidates = {rel for _, rel, digest in candidates
                       if (not explicit or skip_unchanged) and state.get(rel, {}).get("hash") == digest}
    observed: Counter[str] = Counter()
    mismatched: set[str] = set()
    skipped_ids = [state[rel].get("document_id") or document_id_for(rel) for rel in sorted(skip_candidates)]
    for start_index in range(0, len(skipped_ids), 256):
        rows = vector_store.iter_payloads(
            {"document_id": skipped_ids[start_index:start_index + 256]},
            ["path", "version", "status", "version_content_hash"])
        for row in rows:
            rel = row.get("path")
            if rel not in skip_candidates:
                continue
            entry = state[rel]
            if row.get("version") != entry.get("version"):
                continue
            if (row.get("status") == entry.get("indexed_status", "active")
                    or ("indexed_status" not in entry and row.get("status") == "archive")):
                observed[rel] += 1
                if row.get("version_content_hash") not in ("", None, entry.get("hash")):
                    mismatched.add(rel)

    for path, rel, current_hash in candidates:
        known = "hash" in state.get(rel, {})
        unchanged = known and state[rel].get("hash") == current_hash
        if (rel in skip_candidates and rel not in mismatched
                and observed.get(rel) == state[rel].get("chunks", 0)):
            summary.skipped_files.append(rel)
            continue
        if unchanged:
            # 强制重跑或修复缺失卡片：沿用现有版本。
            summary.new_files.append(rel)
        else:
            (summary.updated_files if known else summary.new_files).append(rel)
        processed.append((path, rel, current_hash))

    if summary.cleaned_files:
        # 删除了知识就必须清问答缓存：否则同问题命中缓存返回已删除知识的答案
        clear_qa_cache()
        from src.keyword_search import invalidate_cache
        invalidate_cache()
    if summary.skipped_files:
        console_print(f"   ⏭️ {len(summary.skipped_files)} 个文件内容未变化，跳过（省时省钱）")
        for rel in summary.skipped_files:
            console_print(f"      ⏭️ {rel}")
    summary.total_files = len(processed)
    if not processed:
        console_print("✅ 增量入库完成：没有需要处理的文件")
        return summary

    # ② 解析 + 生成统一 Metadata（目录推断 → Front Matter 覆盖 → 系统兜底）
    console_print("② 解析文件 → 统一文本 + Metadata")
    docs: list[ParsedDocument] = []
    detection_by_rel: dict[str, dict] = {}
    doc_types_by_rel: dict[str, dict] = {}
    departments_by_rel: dict[str, dict] = {}
    visibility_by_rel: dict[str, dict] = {}
    llm = get_llm_client() if cfg.auto_tag else None
    registered_directories = managed_directory_names()
    updated_paths = set(summary.updated_files)
    for path, rel, current_hash in processed:
        try:
            parsed = parse_file(path)
        except Exception as exc:
            summary.failed_files.append(str(exc))
            summary.file_errors[rel] = "parse_failed"
            continue
        try:
            stable = istate.file_hash(path) == current_hash
        except OSError:
            stable = False
        if not stable:
            summary.failed_files.append(f"{rel}（解析期间文件内容已变化，请重试）")
            summary.file_errors[rel] = "file_changed"
            continue
        parsed.metadata = build_document_metadata(
            relative_path=rel,
            source=path.name,
            file_type=parsed.metadata["file_type"],
            title_hint=parsed.metadata["title"],
            created_at=parsed.metadata["created_at"],
            updated_at=parsed.metadata["updated_at"],
            front_matter=parsed.front_matter,
            inference_path=without_storage_directory(rel, registered_directories),
        )
        doc_types_by_rel[rel] = resolve_doc_type(
            path.name, parsed.front_matter,
            istate.doc_type_info_for(previous_state, rel),
            default_doc_type=default_doc_type,
            original_path=doc_type_paths.get(str(path.resolve())) if doc_type_paths else None,
            is_web=(parsed.front_matter or {}).get("source_type") == "web",
        )
        parsed.metadata["doc_type"] = doc_types_by_rel[rel]["value"]
        departments_by_rel[rel] = resolve_department(
            istate.department_info_for(previous_state, rel),
            parsed.front_matter,
            default_department=default_department,
        )
        parsed.metadata["department"] = departments_by_rel[rel]["value"]
        project_link = istate.project_link_for(previous_state, rel)
        if not project_link and default_project_id is not None:
            project_link = {"project_id": default_project_id}
        parsed.metadata.update(project_link)
        from src.document_version import detect_document_version, apply_confirmation
        declared = detect_document_version(path.name, parsed.version_regions, parsed.front_matter)
        detection_by_rel[rel] = declared
        parsed.metadata.update(declared)
        # V3.2 版本管理：只有内容真的变化（updated）才升版本号；
        # 强制重入库（内容未变）不涨版本，否则每次点「重新入库」版本都 +0.1
        prev_version = state.get(rel, {}).get("version")
        explicit_version = version_from_filename(path.name) or str(
            (parsed.front_matter or {}).get("version") or ""
        ).strip()
        if prev_version and (not explicit_version or rel not in updated_paths):
            if rel in updated_paths:
                from src.metadata import bump_version
                parsed.metadata["version"] = bump_version(prev_version)
            else:
                # 内容未变的强制重入库必须沿用台账版本，不能回落到默认 1.0。
                parsed.metadata["version"] = prev_version
        # V3.3 自动打标签：topic/tags 皆空时由 LLM 补全（失败静默跳过，不阻断入库）
        if cfg.auto_tag and llm and not parsed.metadata["topic"] and not parsed.metadata["tags"]:
            from src.auto_tagger import auto_tag
            tagged = auto_tag(parsed.text[:800], llm=llm, config=cfg)
            parsed.metadata["topic"] = tagged["topic"]
            parsed.metadata["tags"] = tagged["tags"]
            if tagged["topic"] or tagged["tags"]:
                console_print(f"   🤖 自动标签 [{rel}]: "
                              f"topic={parsed.metadata['topic']} tags={parsed.metadata['tags']}")
        # V3.4 档案编辑：台账里的覆盖设置优先级最高（高于 Front Matter 与目录推断）
        overrides = state.get(rel, {}).get("overrides", {})
        for key, value in overrides.items():
            if key in ("title", "category", "topic", "tags", "status") and value:
                parsed.metadata[key] = value
        if overrides.get("version") and rel not in updated_paths:
            parsed.metadata["version"] = overrides["version"]
        parsed.metadata["version_content_hash"] = current_hash
        parsed.metadata.update(apply_confirmation(
            declared, state.get(rel, {}).get("version_confirmation"),
            current_hash, str(parsed.metadata["version"])))
        docs.append(parsed)
    summary.ok_files = len(docs)
    console_print(f"   成功 {len(docs)} 个，失败 {len(summary.failed_files)} 个")
    for failure in summary.failed_files:
        console_print(f"   ❌ {failure}")
    if not docs:
        return summary

    # ③ 清洗 + ④ 切片
    console_print("③ 清洗文本，④ 切成知识卡片")
    # V3.9：入库人写入文档元数据（切片继承 → 进卡片 payload），供审计与溯源
    for doc in docs:
        doc.metadata["owner"] = (owner or "").strip()
    front_matter_by_rel = {doc.metadata["path"]: doc.front_matter for doc in docs}
    cleaned_docs, chunks = clean_and_chunk(docs, cfg)
    summary.total_chunks = len(chunks)
    chunks_by_file: dict[str, int] = {}
    for chunk in chunks:
        # 按相对路径聚合：不同子目录的同名文件不能混叠卡片数
        rel_path = chunk.metadata["path"]
        chunks_by_file[rel_path] = chunks_by_file.get(rel_path, 0) + 1
    for doc in cleaned_docs:
        meta = doc.metadata
        summary.file_rows.append({
            **{key: meta.get(key) for key in ("document_id", "version", "title", "file_type", "topic", "tags", "owner")},
            "source": meta["source"],
            "path": meta["path"],
            "domain": meta["domain"],
            "doc_type": meta.get("doc_type", ""),
            "department": meta.get("department", ""),
            "project_id": meta.get("project_id"),
            "classification": meta["classification"],
            "allowed_groups": meta["allowed_groups"],
            "allowed_users": meta.get("allowed_users", []),
            "status": meta["status"],
            "chunks": chunks_by_file.get(meta["path"], 0),
            "document_version": meta["document_version"],
            "document_version_status": meta["document_version_status"],
            "document_version_source": meta["document_version_source"],
        })
    console_print(f"   共切出 {len(chunks)} 张知识卡片")
    # 空文本文件（0 卡片，如无文字的扫描件/图片）：显式警告并跳过台账指纹——
    # 记了指纹会永久跳过，之后再开启 OCR 也不会重试；不记则开启后可重新入库
    zero_card_files = [d.metadata["path"] for d in cleaned_docs
                       if chunks_by_file.get(d.metadata["path"], 0) == 0]
    if zero_card_files:
        summary.failed_files.extend(f"{rel}（未提取到可入库的文字）" for rel in zero_card_files)
        console_print(f"   ⚠️ {len(zero_card_files)} 个文件未解析出文字（扫描件未开 OCR 或内容为空）："
                      f"{'、'.join(zero_card_files[:5])}{'…' if len(zero_card_files) > 5 else ''}；"
                      "这些文件本次不会入向量库，开启 OCR 后重新入库即可")
    docs_by_rel = {d.metadata["path"]: d for d in cleaned_docs if d.metadata["path"] in chunks_by_file}
    summary.ok_files = len(docs_by_rel)
    summary.new_files = [rel for rel in summary.new_files if rel in docs_by_rel]
    summary.updated_files = [rel for rel in summary.updated_files if rel in docs_by_rel]
    if not chunks:
        return summary

    # ⑦ 台账记录函数：只为「解析且切片成功」的文件记录指纹。
    # 解析失败的文件刻意不记——旧向量保持原状，下次入库自动重试；
    # 若记录了失败文件的新指纹，新内容会被增量判断永久跳过。
    def without_acl(entry: dict) -> dict:
        return {**{key: value for key, value in entry.items()
                   if key not in ("overrides", "visibility_info")},
                "overrides": {key: value for key, value in entry.get("overrides", {}).items()
                              if key not in ("allowed_groups", "allowed_users", "classification")}}

    # ⑤ 向量化
    console_print(f"⑤ 向量化（模型：{cfg.embedding_model}）")
    summary.vector_dimension = embedding.dimension()
    console_print(f"   向量维度：{summary.vector_dimension}")
    grant_snapshot = document_user_revocations() if default_allowed_users else {}
    vectors = embedding.embed_texts([chunk.text for chunk in chunks])

    # ⑥ 存入向量库
    with document_acl_write(retain_if=publication_owns_token) as acl_guard:
        recover_publication(vector_store, acl_guard[1])
        latest = istate.load_state(strict=not rebuild)
        if rebuild and latest != previous_state:
            raise VectorStoreError("重建期间文档台账已变化，请重试。")
        for path, rel, digest in processed:
            if rel not in docs_by_rel:
                continue
            if without_acl(latest.get(rel, {})) != without_acl(previous_state.get(rel, {})):
                raise VectorStoreError(f"{rel}：准备入库期间文档档案或版本已变化，请重试。")
            try:
                stable = istate.file_hash(path) == digest
            except OSError:
                stable = False
            if not stable:
                raise VectorStoreError(f"{rel}：准备入库期间文件内容已变化，请重试。")

        # 旧台账可能只记了指纹。此时从现存当前卡片继承权限，缺少可验证权限则保持受限。
        legacy_paths = {rel for rel in docs_by_rel if rel in latest
                        and not istate.visibility_info_for(latest, rel)}
        legacy_acl = {}
        if legacy_paths:
            for row in vector_store.list_documents({"document_id": [document_id_for(rel)
                                                                    for rel in legacy_paths]}):
                if (row["path"] in legacy_paths and row["status"] in ("active", "archive")
                        and row["version"] == latest[row["path"]].get("version")):
                    legacy_acl[row["path"]] = {"value": normalize_visibility(
                        row["allowed_groups"], row["allowed_users"], legacy_default=False)}

        revocations = {}
        active = None
        normalized_groups: dict[tuple[str, ...], list[str]] = {}
        state = {} if rebuild else latest.copy()
        previous_versions = {}
        for _, rel, digest in processed:
            if rel not in docs_by_rel:
                continue
            doc = docs_by_rel[rel]
            meta = doc.metadata
            entry = latest.get(rel, {})
            previous_acl = istate.visibility_info_for(latest, rel) or legacy_acl.get(rel)
            if not previous_acl and rel in legacy_paths:
                previous_acl = {"value": normalize_visibility([], [], legacy_default=False)}
            acl, info = resolve_visibility(
                previous_acl, front_matter_by_rel[rel], default_groups=default_groups,
                default_allowed_users=default_allowed_users, legacy_overrides=entry.get("overrides"))
            groups = tuple(acl["allowed_groups"])
            if groups not in normalized_groups:
                normalized_groups[groups] = normalize_department_groups(acl["allowed_groups"])
            acl["allowed_groups"] = normalized_groups[groups].copy()
            if acl["allowed_users"]:
                if active is None:
                    revocations = document_user_revocations()
                    active = {u["username"] for u in list_users() if u["status"] == "active"}
                if default_allowed_users is not None and not previous_acl:
                    info["granted_revocations"] = {u: grant_snapshot.get(u) for u in acl["allowed_users"]}
                prune_user_grants(acl, info.get("granted_revocations", {}), revocations, active)
            acl["classification"] = classify_visibility(acl["allowed_groups"], acl["allowed_users"])
            info["value"] = dict(acl)
            visibility_by_rel[rel] = info
            version_info = {key: meta[key] for key in ("document_version", "document_version_status",
                            "document_version_source", "document_version_evidence")}
            previous_versions[rel] = entry.get("version") or ("1.0" if entry.get("hash") else None)
            istate.update_entry(
                state, rel, meta["document_id"], chunks_by_file[rel], digest, version=meta["version"],
                version_info=version_info, version_detection=detection_by_rel[rel],
                doc_type_info=doc_types_by_rel[rel], department_info=departments_by_rel[rel],
                project_link={"project_id": meta["project_id"]} if "project_id" in meta else None,
                visibility_info=info)
            state[rel]["indexed_status"] = meta["status"]
            if "archive_origin" in entry:
                state[rel]["archive_origin"] = entry["archive_origin"]
            if entry.get("hash") != digest:
                state[rel]["overrides"].pop("version", None)
        for chunk in chunks:
            chunk.metadata.update(visibility_by_rel[chunk.metadata["path"]]["value"])
        for row in summary.file_rows:
            if row["path"] in visibility_by_rel:
                row.update(visibility_by_rel[row["path"]]["value"])

        if rebuild:
            console_print("⑥ 重建模式：先清空向量库")
            vector_store.clear()
        vector_store.ensure_collection(summary.vector_dimension)
        console_print("⑥ 存入 Qdrant 向量库")
        if rebuild:
            stored = vector_store.upsert_chunks(chunks, vectors)
            istate.save_state(state, strict=True)
        else:
            documents = {doc.metadata["document_id"]: (rel, doc.metadata["version"])
                         for rel, doc in docs_by_rel.items()}
            stored = publish(vector_store, chunks, vectors, state, documents, previous_versions, acl_guard)
    # 入库后清空 BM25 缓存和 Q→A 缓存
    from src.keyword_search import invalidate_cache
    invalidate_cache()
    clear_qa_cache()
    console_print(f"   已写入 {stored} 张卡片")

    summary.elapsed_seconds = time.time() - start
    console_print(f"✅ 入库完成，总耗时 {summary.elapsed_seconds:.1f} 秒")
    console_print("\n入库明细：")
    for row in summary.file_rows:
        console_print(f"   {row['path']:<40} domain={row['domain']:<10} "
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


def record_stage_usage(result: QAResult, stage: str, usage: dict) -> None:
    """把一次 LLM 调用的用量记入 result.usage 的分项并重算总计（B16.1 模型消耗统计）。

    usage 形如 {"input", "input_cached", "output", "estimated"}（见 src/llm.py）；
    跳过空用量与全 0 用量（假 LLM / Mock 不填 usage_out 的场景）——真实调用必有正数。
    """
    if not usage or (not usage.get("input") and not usage.get("output")):
        return
    stages = result.usage.setdefault("stages", {})
    stages[stage] = dict(usage)
    parts = list(stages.values())
    total = merge_usage(parts)
    result.usage.clear()
    result.usage.update(total)
    result.usage["stages"] = stages


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


def expand_facts_dominant_documents(items: list[RetrievedItem], filters: dict | None,
                                    top_k: int, cfg: AppConfig, multi_fact: bool
                                    ) -> tuple[list[RetrievedItem], int, set[str], list[str]]:
    """Keep initial head order and expand short versions or bounded neighborhoods."""
    limit = cfg.expand_doc_max_cards
    if limit <= 0 or not items:
        return items, 0, set(), []
    groups = group_documents(it for it in items if it.metadata.get("document_id"))
    head_keys = {document_key(it) for it in items[:top_k]}
    dominant = [key for key, in_pool in sorted(groups.items(), key=lambda entry: -len(entry[1]))
                if (len(in_pool) >= 2 and (key in head_keys or multi_fact))
                or (multi_fact and key in head_keys)][:3 if multi_fact else 2]
    if not dominant:
        return items, 0, set(), []

    store = get_vector_store()
    seen = {it.metadata.get("chunk_id") for it in items}
    head_ids = {id(it) for it in items[:top_k]}
    middle: list[RetrievedItem] = []
    notes: list[str] = []
    doc_ids: set[str] = set()
    added_count = 0
    for doc_id, version in dominant:
        in_pool = groups[doc_id, version]
        source = in_pool[0].metadata.get("source", "?")
        scoped = {**(filters or {}), "document_id": doc_id}
        if version is not None:
            scoped["version"] = version
        if in_pool[0].metadata.get("status"):
            scoped["status"] = in_pool[0].metadata["status"]
        cards = store.ordered_document_cards(scoped, limit + 1) if len(in_pool) > 1 else []
        neighbors = not cards or len(cards) > limit
        if neighbors:
            anchors = [seq for hit in in_pool[:3] if (seq := chunk_sequence(hit.metadata)) is not None]
            wanted = {position for anchor in anchors for position in (anchor - 1, anchor + 1)
                      if position >= 0 and position not in anchors}
            cards = store.ordered_document_cards(scoped, len(wanted), indices=wanted) if wanted else []
            cards = sorted(cards, key=lambda card: next(
                (rank for rank, anchor in enumerate(anchors)
                 if chunk_sequence(card["payload"]) in (anchor - 1, anchor + 1)), len(anchors)))
        added: list[RetrievedItem] = []
        for card in cards:
            payload = card["payload"]
            if not payload.get("chunk_id") or payload["chunk_id"] in seen:
                continue
            seen.add(payload.get("chunk_id"))
            added.append(RetrievedItem(rank=0, score=0.0, text=payload.get("text", ""),
                                       metadata={k: v for k, v in payload.items() if k != "text"},
                                       channels="文档展开"))
            if neighbors and len(added) >= min(2, limit):
                break
        if not added:
            continue
        middle += [it for it in in_pool if id(it) not in head_ids] + added
        added_count += len(added)
        doc_ids.add(doc_id)
        mode = "邻卡" if neighbors else "短文档"
        notes.append(f"{source}：候选池 {len(in_pool)} 张，{mode}净补入 {len(added)} 张")
    if not middle:
        return items, 0, set(), notes
    placed = head_ids | {id(it) for it in middle}
    expanded = items[:top_k] + middle + [it for it in items if id(it) not in placed]
    for i, it in enumerate(expanded, start=1):
        it.rank = i
    return expanded, added_count, doc_ids, notes


_SOURCE_EXT_TAIL = re.compile(r"\.[a-z0-9]{1,6}$")
_SOURCE_VERSION_TAIL = re.compile(r"[_\-][vｖ][0-9][0-9._]*$")


def _source_dice(left: str, right: str) -> float:
    """字符二元组 Dice 相似度：词序无关、容忍缺词/多词（"昆仑需求说明书" vs
    "昆仑项目需求说明书" 高重合；比对前先剥扩展名与 _v2.0 类版本尾巴）。"""
    def bigrams(value: str) -> Counter:
        value = re.sub(r"\s+", "", value)
        return Counter(value[i:i + 2] for i in range(len(value) - 1))

    a, b = bigrams(left), bigrams(right)
    if not a or not b:
        return 0.0
    return 2 * sum((a & b).values()) / (sum(a.values()) + sum(b.values()))


def resolve_spoken_source(spoken: str, store: VectorStore | None = None) -> str | None:
    """把用户口述的文件名归一到库内真实 source（N-A：精确 → 前缀 → 包含 → 变体，唯一命中才替换）。

    病灶（实测 M07）：Query 理解点名文件时可能输出短名——「昆仑项目需求说明书」
    缺 `_v2.0.docx` 后缀；source 过滤是精确匹配，短名直接筛空，且 document 路由
    不做放宽重试，整题空手而归。
    变体层（R-1，实测 S16）：口述名与库内名存在词序/缺词差异（「昆仑需求说明书」
    vs「昆仑项目需求说明书」），前缀/包含都不命中——按字符二元组 Dice 相似度
    匹配。仅当口述名 ≥4 字符、最高分 ≥0.6 且显著高于第二名（领先 ≥0.1）时替换；
    多个相近候选（如两版员工手册）保持歧义不猜。归一在任何层级都只在唯一命中
    时生效；未命中保持原值（精确匹配的既有行为不变）。同名不同目录的文档共享
    同一 source 值，归一按「去重后的 source 值集合」判唯一，不受多目录同名影响。
    """
    import unicodedata

    text = unicodedata.normalize("NFKC", (spoken or "").strip()).casefold()
    if not text:
        return None
    store = store or get_vector_store()
    catalog = store.document_catalog({}, 500, 50000, "updated_at", None)
    folded: dict[str, str] = {}
    for doc in catalog.get("documents") or []:
        source = doc.get("source")
        if source:
            folded.setdefault(unicodedata.normalize("NFKC", source).casefold(), source)
    if text in folded:
        return folded[text]
    prefix = {orig for norm, orig in folded.items() if norm.startswith(text)}
    if len(prefix) == 1:
        return next(iter(prefix))
    contains = {orig for norm, orig in folded.items() if text in norm}
    if len(contains) == 1:
        return next(iter(contains))
    if len(text) >= 4:
        scored = sorted(
            ((_source_dice(text, _SOURCE_VERSION_TAIL.sub("", _SOURCE_EXT_TAIL.sub("", norm))), orig)
             for norm, orig in folded.items()),
            key=lambda pair: pair[0], reverse=True)
        if (scored and scored[0][0] >= 0.6
                and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.1)):
            return scored[0][1]
    return None


def _cache_key(question: str, top_k: int, filters: dict, qu: bool, hybrid: bool, rerank: bool,
               history: list[dict] | None = None) -> str:
    # 对话历史参与键：同样的短追问（"继续""展开讲讲"）在不同上下文里答案完全不同，
    # 不带 history 会跨会话/跨轮次串答案
    hist_sig = hashlib.sha256(json.dumps(
        [{"role": m.get("role"), "content": m.get("content")} for m in (history or [])],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
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
_qa_cache_epoch = None


def _sync_qa_cache_epoch(epoch: tuple[str, str | None]) -> None:
    global _qa_cache_epoch
    if epoch != _qa_cache_epoch:
        _qa_cache.clear()
        _qa_cache_owner.clear()
        _qa_cache_epoch = epoch


def _remember_answer(key: str, result: QAResult, username: str | None) -> None:
    for oldest in list(_qa_cache)[:max(0, len(_qa_cache) - _QA_CACHE_MAX + 1)]:
        _qa_cache.pop(oldest, None)
        _qa_cache_owner.pop(oldest, None)
    _qa_cache[key] = result
    _qa_cache_owner[key] = username or ""


# ---------------------------------------------------------------- 问答
def _catalog_evidence(question, filters, cfg, *, file_dates=None):
    """精确目录查询：服务端权限过滤后扫描元数据，不调用向量化/生成模型。"""
    start = time.time()
    date_field, dates = (next(iter(file_dates.items()), ("updated_at", {}))
                         if file_dates is not None else catalog_time(question))
    effective = effective_filters(filters)
    catalog = get_vector_store().document_catalog(
        effective, cfg.enum_catalog_docs, cfg.enum_catalog_points, date_field, dates)
    docs = catalog.pop("documents")
    partial = catalog["scan_truncated"] or catalog["list_truncated"] or catalog["unknown_date_documents"] > 0
    meta = {**catalog, "route": "catalog", "enumeration": True,
            "coverage": "partial" if partial else "complete",
            "processed_documents": len(docs), "date_field": date_field, "dates": dates}
    items = [RetrievedItem(rank=i, score=0.0,
                          text=f"文件：{d.get('source')}\n路径：{d.get('path')}\n入库时间：{d.get('indexed_at') or '-'}",
                          metadata=d, channels="目录元数据") for i, d in enumerate(docs, 1)]
    context = "\n\n".join(f"[{it.rank}] {it.text}" for it in items)
    notice = coverage_notice(meta)
    answer = notice + (f"\n筛选日期：{dates['from']} 至 {dates['to']}（{'入库时间' if date_field == 'indexed_at' else '文件修改时间'}）。" if dates else "")
    answer += "\n\n" + "\n".join(f"- `{d.get('path')}` [{i}]" for i, d in enumerate(docs, 1))
    elapsed = time.time() - start
    result = QAResult(question=question, answer=answer, retrieved=items, context=context,
                      sources=[{**it.metadata, "doc_type": it.metadata.get("doc_type") or "",
                                "department": it.metadata.get("department") or "",
                                "rank": it.rank, "score": 0.0,
                                "text": it.text, "channels": it.channels} for it in items],
                      filters=effective, enumeration=True, evidence_meta=meta,
                      elapsed={"query_understanding": 0.0, "retrieval_only": elapsed,
                               "rerank": 0.0, "assembly": 0.0, "retrieval": elapsed, "total": elapsed},
                      trace=[make_node("📁", "目录统计", time_str=now_str(), elapsed=elapsed,
                                       summary=notice, items=[("实际过滤条件", str(effective)),
                                                              ("日期条件", f"{date_field}: {dates}")])])
    return result, meta


def _project_mentions_evidence(result, filters, types, terms, cfg):
    from src.auth_db import retrieval_acl_revision

    revision = (index_epoch.current(), retrieval_acl_revision())
    start = time.monotonic()
    result.filters = effective_filters(filters)
    matches, meta = scan_project_mentions(get_vector_store(), result.filters, types, terms, cfg)
    meta.update(route="project_document_mentions", enumeration=True)
    result.enumeration = True
    if (index_epoch.current(), retrieval_acl_revision()) != revision:
        result.answer = "检索期间资料或权限发生变化，请重新查询。"
        meta = {"route": meta["route"], "coverage": "not_found", "enumeration": True,
                "scope_issue": "index_changed", "scope_notice": result.answer}
    else:
        lines = []
        for rank, (name, quote, row) in enumerate(matches, 1):
            metadata = {key: value for key, value in row["payload"].items()
                        if key not in {"text", "allowed_groups", "allowed_users"}}
            metadata["point_id"] = row["id"]
            metadata["classification"] = classify_visibility(
                row["payload"].get("allowed_groups"), row["payload"].get("allowed_users"))
            channels = "正文精确匹配"
            result.retrieved.append(RetrievedItem(rank, 1.0, quote, metadata, channels))
            result.sources.append({**metadata, "rank": rank, "score": 1.0, "text": quote, "channels": channels})
            excerpt = re.sub(r"([\\`*_{}\[\]<>])", r"\\\1", f"{name}：{quote}")
            lines.append(f"- {excerpt} [{rank}]")
        terms_display = "」或「".join(terms)
        if lines:
            result.answer = f"以下已关联项目的{'或'.join(types)}文档正文提到「{terms_display}」。\n\n"
            result.answer += "\n".join(lines)
        else:
            result.answer = f"在已扫描的{'或'.join(types)}文档正文中，未找到「{terms_display}」。"
            meta["scope_notice"] = result.answer
        if meta["coverage"] == "partial":
            result.answer = "本次达到扫描或展示上限，清单可能不完整。\n\n" + result.answer
            if not lines:
                meta["scope_notice"] = result.answer
        result.context = "\n".join(f"[{item.rank}] {item.text}" for item in result.retrieved)
        meta["matched_projects"] = len(matches)
    elapsed = time.monotonic() - start
    result.elapsed.update(retrieval_only=elapsed, rerank=0.0, assembly=0.0, retrieval=elapsed, total=elapsed)
    result.trace.append(make_node("📋", "项目文档正文核验", time_str=now_str(), elapsed=elapsed,
                                  summary=f"定位到 {len(result.sources)} 个有正文证据的项目。"))
    result.evidence_meta = meta
    return result, meta


def _department_project_roster(department, filters, cfg):
    from src.auth_db import list_departments

    dept_id = next((row["id"] for row in list_departments() if row["name"] == department), None)
    if dept_id is None:
        return [], "unresolved_department"
    selected = effective_filters(filters).get("project_id")
    selected = selected if isinstance(selected, (list, tuple)) else [selected]
    names = {p["id"]: p["name"] for p in list_projects()
             if (selected == [None] or p["id"] in selected)
             and (p["lead_department_id"] == dept_id or any(d["id"] == dept_id for d in p["participants"]))}
    if not names:
        return [], "no_visible_projects"
    catalog = get_vector_store().document_catalog(
        effective_filters({**filters, "project_id": list(names)}), cfg.enum_catalog_docs, cfg.enum_catalog_points)
    if catalog["scan_truncated"] or catalog["list_truncated"]:
        return [], "roster_incomplete"
    visible = {doc["project_id"] for doc in catalog["documents"]}
    return [(name, project_id) for project_id, name in names.items() if project_id in visible], ""


def _project_comparison_evidence(result: QAResult, comparison: ProjectComparison, filters: dict | None,
                                 top_k: int, cfg: AppConfig, use_hybrid: bool, use_rerank: bool,
                                 user_groups: list[str] | None, username: str | None, on_progress=None):
    from src.auth_db import retrieval_acl_revision

    base = filters or {}
    issue = ""
    projects = []
    revision = (index_epoch.current(), retrieval_acl_revision())
    if comparison.department:
        projects, issue = _department_project_roster(comparison.department, base, cfg)
        if not issue and not projects:
            issue = "no_visible_projects"
        if not issue and len(projects) > MAX_COMPARISON_PROJECTS:
            issue = "roster_too_large"
        ids = [project_id for _, project_id in projects]
    elif not comparison.valid_count:
        issue = "comparison_count"
    else:
        try:
            for name in comparison.names:
                [project_id] = resolve_project_filter(name, None, allow_name_variants=True)
                projects.append((name, project_id))
        except ValueError:
            # 未登记的项目名与 scope 未解析同族（R6-C02：比较句式里混入只在台账/正文
            # 出现、未登记主档的项目名）——回退普通检索（后续范围回退会给出全库语义
            # 检索与提示行），而不是拦截。仅保留显式筛选冲突 / 数量不符的诚实引导。
            return None
        if not issue:
            ids = [project_id for _, project_id in projects]
            selected = base.get("project_id")
            if len(set(ids)) != len(ids):
                issue = "comparison_count"
            elif selected not in (None, "all"):
                selected = selected if isinstance(selected, (list, tuple)) else [selected]
                if not set(ids).issubset(selected):
                    issue = "conflict"
    if issue == "comparison_count" and not comparison.explicit_scope:
        return None
    result.filters = effective_filters(base)
    elapsed = {"retrieval_only": 0.0, "rerank": 0.0, "assembly": 0.0}
    meta = {"route": "project_comparison", "coverage": "not_found", "enumeration": False}
    if (index_epoch.current(), retrieval_acl_revision()) != revision:
        issue = "index_changed"
    if issue:
        result.answer = {
            "comparison_count": f"请明确列出 2 至 {MAX_COMPARISON_PROJECTS} 个不同的项目，并确认项目数量一致。",
            "unresolved": "无法确认所有要比较的项目，请核对项目名称。",
            "conflict": "至少一个要比较的项目不在已选筛选范围内，请调整项目筛选后重试。",
            "unresolved_department": "无法识别该参与部门，请核对部门名称。",
            "no_visible_projects": "当前筛选范围内没有找到该部门参与且你有权查看资料的项目。",
            "roster_incomplete": "本次扫描未能确认完整的可见项目名单，请缩小筛选范围后重试。",
            "roster_too_large": f"符合条件的项目超过 {MAX_COMPARISON_PROJECTS} 个，请缩小筛选范围后再比较预算。",
            "index_changed": "检索期间资料或权限发生变化，请重新查询。",
        }[issue]
        meta.update(scope_issue=issue, scope_notice=result.answer)
    else:
        result.filters = effective_filters({**base, "project_id": ids})
        grouped = []
        for name, project_id in projects:
            if on_progress:
                on_progress(f"正在检索项目「{name}」…")
            # Each branch uses the shared ACL-aware retrieval and context assembly with its own project ID.
            # The operation and fields were parsed once from the original question.
            branch, branch_meta = query_evidence(
                comparison.search_query, top_k=top_k,
                filters={**base, "project_id": [project_id]},
                use_query_understanding=False, use_hybrid=use_hybrid,
                use_rerank=use_rerank, use_llm=False, config=cfg,
                user_groups=user_groups, username=username,
            )
            offset = len(result.sources)
            sources = [dict(source, rank=offset + rank) for rank, source in enumerate(branch.sources, 1)]
            result.sources.extend(sources)
            result.retrieved.extend(replace(item, rank=offset + rank) for rank, item in enumerate(branch.retrieved, 1))
            grouped.append((name, sources))
            for key in elapsed:
                elapsed[key] += branch.elapsed.get(key, 0.0)
            result.trace.append(make_node(
                "🔀", "项目子查询", time_str=now_str(), elapsed=branch.elapsed.get("total", 0.0),
                summary=f"项目「{name}」采用 {len(sources)} 条证据。",
                items=[("检索字段", comparison.search_query), ("证据覆盖", branch_meta["coverage"])],
            ))
        if (index_epoch.current(), retrieval_acl_revision()) != revision:
            result.sources.clear()
            result.retrieved.clear()
            result.answer = "检索期间资料或权限发生变化，请重新查询。"
            meta.update(scope_issue="index_changed", scope_notice=result.answer)
        else:
            result.answer, missing = render_project_comparison(comparison, grouped)
            if comparison.department:
                result.answer = "以下比较仅覆盖该部门参与且你有权查看资料的项目。\n\n" + result.answer
            result.context = "\n\n".join(f"[{s['rank']}] {s['source']}\n{s['text']}" for s in result.sources)
            meta.update(coverage=("partial" if missing else "unknown") if result.sources else "not_found",
                        comparison={"operations": list(comparison.operations), "missing": missing,
                                    "projects": [{"name": name, "sources": [s["rank"] for s in sources]}
                                                 for name, sources in grouped]})
    result.elapsed.update(elapsed)
    result.elapsed["retrieval"] = sum(elapsed.values())
    result.elapsed["total"] = result.elapsed["retrieval"] + result.elapsed.get("query_understanding", 0.0)
    result.evidence_meta = meta
    return result, meta


def _version_context(items, cfg, max_items, max_per_doc, scopes):
    """Give each allowed version side its own context budget and keep citations unique."""
    side_count = max(1, sum(bool(statuses) for statuses in scopes.values()))
    side_cfg = replace(cfg, context_max_tokens=cfg.context_max_tokens // side_count)
    contexts, used, dropped, notes = [], [], [], []
    missing = []
    for key, label, _ in VERSION_SIDES:
        candidates = [item for item in items if item.metadata.get("status") in scopes[key]]
        context, sources, discarded, merged = build_context(
            candidates, side_cfg, max_items=max(1, max_items // side_count), max_per_doc=max_per_doc,
            rank_offset=len(used), show_version=True,
        )
        if not sources:
            missing.append(key)
            context = "当前检索范围内没有找到可用证据。"
        contexts.append(f"【{label}】\n{context}")
        used.extend(sources)
        dropped.extend(discarded)
        notes.extend(merged)
    return "\n\n".join(contexts), used, dropped, notes, missing


def query_evidence(
    question: str,
    top_k: int | None = None,
    filters: dict | None = None,
    use_query_understanding: bool = True,
    use_hybrid: bool = True,
    use_rerank: bool = True,
    history: list[dict] | None = None,
    use_llm: bool = True,
    on_progress=None,
    config: AppConfig | None = None,
    user_groups: list[str] | None = None,
    conversation_context: str = "",
    username: str | None = None,
    project_context: dict | None = None,
) -> tuple[QAResult, dict]:
    """Evidence 统一查询入口（V3.9）：检索半程（意图→检索→融合→筛选→组装）。

    Web 答案模式（answer_stream）与 MCP 证据模式（knowledge_search）共用本函数，
    保证两个出口的检索规则永远一致。返回 QAResult（retrieved=入选卡片、sources=含相邻
    补全的最终条目、trace=检索半程全部节点、elapsed、usage）+ evidence_meta（coverage
    语义查询粗判定：not_found/partial/unknown；精确目录完整扫描且完整展示才签 complete）。

    filters 应为 scope_filters 之后的条件；本函数内部对 user_groups 再做一次注入
    （直接调用方传 None 即可，MCP 层传令牌属主的可见部门）。
    """
    def _p(msg):
        if on_progress:
            on_progress(msg)

    # 入口守卫（R9-NAC11）：空/纯空白问题直接诚实拒绝，不进 QU/向量化——
    # 否则 EmbeddingError("问题不能为空") 以异常形态抛出，属可预期的输入问题。
    if not (question or "").strip():
        guarded = QAResult(question=question or "",
                           answer="问题内容为空，请输入具体问题后再试。",
                           retrieved=[], context="", sources=[],
                           trace=[make_node("⚠️", "空问题守卫", time_str=now_str(),
                                            summary="问题为空或仅空白，未执行检索。",
                                            items=[("输入", repr(question)[:40])])])
        guarded.evidence_meta = {"coverage": "not_found", "empty_question": True}
        return guarded, guarded.evidence_meta

    cfg = config or get_config()
    top_k = top_k or cfg.top_k

    from src.permissions import scope_filters, classify_visibility
    filters = scope_filters(filters, user_groups, username)

    # 领域文件夹语义（R6-M09 延伸）：「项目文件夹/学习目录…」= 领域范围的口头
    # 说法——注入 domain 过滤让这类问法走零 LLM 目录通道拿完整清单；用户手动
    # 选了领域时以手动为准。import 置于函数内与其他 scope 引入一致。
    from src.query_scope import domain_folder_scope
    folder_domain = domain_folder_scope(question)
    if folder_domain and not (filters or {}).get("domain"):
        filters = {**(filters or {}), "domain": folder_domain}

    if enumeration_route(question, True) == "catalog":
        return _catalog_evidence(question, filters, cfg)

    date_filters = explicit_file_dates(question)
    for field in ("updated_at", "indexed_at"):
        if filters and field in filters:
            date_filters[field] = filters[field]
    trace: list = []
    result = QAResult(question=question, answer=None, retrieved=[], context="",
                      sources=[], filters=effective_filters(filters), trace=trace)
    if project_mentions := parse_project_mentions(question):
        return _project_mentions_evidence(result, {**date_filters, **(filters or {})}, *project_mentions, cfg)

    # 列举/盘点类提问：进入独立预算的文档定位/跨文档精排通道。
    # 判定严谨性（语义为主，词表分级兜底）：
    # - Query 理解成功时以语义判定为准；enumeration=false 时，仅弱信号词
    #   （哪些/几个/多少——普通疑问句常用）不再强制进入重通道，
    #   防止"这个方法有哪些缺点"这类单对象属性提问误入 12 次调用的列举流水线；
    # - 强信号（列出/列举/盘点等明确盘点动词）仍可覆盖语义漏判；
    # - Query 理解关闭或解析失败时，词表全量兜底（彼时是唯一防线，保持旧行为）。
    # 注意：判定必须用原始问题——Query 理解改写后的查询已丢失计数语气。
    # 参数在节点②之后统一应用（见 use_rerank_now 处）
    _enum_strong = any(w in question for w in (
        "列出", "列举", "盘点", "都有什么", "什么功能", "什么工具", "功能列表",
    ))
    _enum_weak = any(w in question for w in (
        "有哪些", "哪些", "全部", "那些", "都有", "所有",
        "几个", "多少个", "多少种",
        # 计数类问法（如「工作经历有几份」）：答案需要凑齐全部条目，
        # 漏一个词就会召回不全导致计数偏小
        "几份", "几段", "几条", "几次", "几轮", "几家", "几个方面",
        "多少份", "多少段", "多少条", "多少次", "多少轮", "多少家",
    ))
    # 词表兜底初值：Query 理解不可用时它就是唯一判定（保持旧行为）
    enumeration_mode = _enum_weak or _enum_strong
    recall_k = top_k            # 基线：非枚举 = top_k（精排开启时在下方过采样）
    ctx_max_items = top_k

    # 节点 ②：Query 理解 / 改写（V2：一次 LLM 调用完成改写 + 关键词 + 过滤条件推断）
    search_query = question
    bare_reference = project_reference(question) == "它"
    selected_project = (filters or {}).get("project_id")
    unique_selection = type(selected_project) is int or (
        isinstance(selected_project, (list, tuple)) and len(selected_project) == 1)
    if bare_reference and not unique_selection:
        if project_context is None:
            project_context = conversation_project(question, history or []) if history else {}
        if project_context.get("project_id"):
            search_query = name_project_in_followup(question, project_context["name"])
    qu_filters: dict = {}
    qu = None
    unresolved_reference = bare_reference and not unique_selection and search_query == question
    if use_query_understanding and use_llm and not unresolved_reference:
        _p("🧠 Query 理解中…")
        qu_start = now_str()
        llm = get_llm_client()
        qu = understand_query(search_query, llm=llm, config=cfg, history=history,
                              conversation_context=conversation_context)
        record_stage_usage(result, "query_understanding", qu.usage)
        result.elapsed["query_understanding"] = qu.elapsed  # 生成半程的 total 依赖此项
        search_query = qu.vector_query or search_query
        qu_filters = dict(qu.filters)
        # 语义判定合并：LLM 识别出计数/列举意图而词表没命中时，同样进入枚举通道——
        # 覆盖「经历过几家公司」这类词表外的任意计数问法。
        # 语义明确否定（qu.enumeration=false）时尊重判定：仅弱词命中不再进入重通道；
        # 强信号（列出/列举/盘点等明确盘点动词）仍可覆盖，防语义漏判清单类提问。
        if qu.enumeration:
            enumeration_mode = True
        elif qu.ok and not _enum_strong:
            enumeration_mode = False
        # 展示用于改写、路由和筛选的字段。
        fields_dump = (
            f"intent（意图概括）: {qu.intent or '（未输出）'}\n"
            f"vector_query（改写后检索语句）: {qu.vector_query}\n"
            f"keyword_query（关键词，供关键词检索用）: {', '.join(qu.keyword_query) or '（未输出）'}\n"
            f"enumeration（计数/列举意图，触发召回扩容）: {qu.enumeration}\n"
            f"inventory_intent（文件盘点）: {qu.inventory_intent}\n"
            f"inventory_domain（明确点名的领域）: {qu.inventory_domain or '（未输出）'}\n"
            f"sub_queries（复合问题拆分，逐子查询检索）: {('；'.join(qu.sub_queries)) if qu.sub_queries else '（未输出，单信息需求）'}\n"
            f"time_range（模型识别的时间范围）: {qu.time_range or '（未输出）'}\n"
            f"文件日期过滤（按问句和手动选择确定）: {date_filters or '（无）'}\n"
            f"filters（推断的过滤条件）: {qu.filters or '（未输出）'}\n"
            f"metadata_scope（明确指定的范围）: {getattr(qu, 'metadata_scope', {}) or '（未输出）'}")
        if qu.ok and qu.error:
            fields_dump += f"\n校验提示: {qu.error}"
        request_dump = (f"模型: {cfg.llm_model or '-'}　|　温度: {cfg.llm_temperature}　|　"
                        f"消息数: 2 条（System 任务说明书 + User 请求）")
        trace.append(make_node(
            "🧠", "Query 理解 / 改写", time_str=qu_start, elapsed=qu.elapsed,
            status="已执行" if qu.ok else "回退（解析失败，使用原始问题检索）",
            summary="用一次 LLM 调用完成查询改写、筛选推断、文件盘点与列举识别、复合问题拆分；失败时回退为原始问题",
            items=[
                ("① 调用参数（发给 LLM 的完整请求）", request_dump),
                ("② System Prompt（任务说明书全文）", qu.system_prompt),
                ("③ User Prompt（时间+领域枚举+历史+原始问题）", qu.user_prompt),
                ("④ LLM 原始输出（返回的 JSON 全文）", qu.raw_output or qu.error),
                ("⑤ 返回字段逐项解析", fields_dump),
            ],
        ))
    else:
        reason = "仅检索模式" if not use_llm else "未启用（可在 检索设置 或 .env QUERY_UNDERSTANDING 开启）"
        if unresolved_reference:
            reason = "待确认代词指向的项目"
        trace.append(make_node(
            "🧠", "Query 理解 / 改写", time_str=now_str(), status=f"直通（{reason}）",
            summary="原始问题直接进入向量化，未做改写与条件推断",
            items=[("输入 / 输出", question)],
        ))

    selected_text = selected_project_text(question)
    scope_question = selected_text if selected_text and extract_scope(selected_text).get("project") else question
    # N-A 点名文件归一：QU 输出的文件短名（缺版本后缀/扩展名）先归一到库内真实
    # source，再进入过滤与路由——否则 source 精确匹配筛空，document 路由空手而归。
    if qu_filters.get("source"):
        resolved_source = resolve_spoken_source(qu_filters["source"])
        if resolved_source and resolved_source != qu_filters["source"]:
            trace.append(make_node(
                "📎", "点名文件归一", time_str=now_str(),
                summary="问题点名的文件名与库内不完全一致，已按唯一命中归一为真实文件名。",
                items=[("口述文件名", qu_filters["source"]), ("归一为", resolved_source)],
            ))
            qu_filters["source"] = resolved_source
    scope_issue = unsupported_scope(scope_question)
    comparison = parse_project_comparison(scope_question) if scope_issue != "negated" else None
    if comparison:
        compared = _project_comparison_evidence(result, comparison, {**date_filters, **(filters or {})},
                                                top_k, cfg, use_hybrid, use_rerank,
                                                user_groups, username, on_progress)
        if compared is not None:
            return compared
    staff_total = is_staff_total_question(question)
    if staff_total:
        enumeration_mode = False
    version_scopes = comparison_statuses(filters) if compares_versions(question) else None
    if version_scopes is not None:
        # One inferred status or source cannot represent both sides of a comparison.
        qu_filters.pop("status", None)
        qu_filters.pop("source", None)
        enumeration_mode = False
    spoken_scope = {} if scope_issue else extract_scope(
        scope_question, getattr(qu, "metadata_scope", None) if qu and qu.ok else None)
    model_inventory = bool(qu and qu.ok and qu.inventory_intent)
    # 推断类范围注入的过滤键（渐进式放宽的剥离范围）：resolve_scope 从问句推断
    # 注入的 project_id/department/doc_type 与 LLM 的 qu_filters 同属"推断"，
    # 0 召回时一并放宽；界面手动设置的键不在此列（保持"手动设置不放宽"规则）。
    pre_scope_filters = dict(filters or {})
    scope_injected_keys: set[str] = set()
    if spoken_scope:
        if qu_filters.get("source") in spoken_scope.values():
            qu_filters.pop("source")
        filters, scope_issue = resolve_scope(scope_question, spoken_scope, filters)
        if scope_issue == "unresolved" and model_inventory and not (qu.sub_queries or qu.enumeration_scope):
            project = recover_inventory_project(scope_question, qu.metadata_scope.get("project"))
            if project and project != spoken_scope.get("project"):
                corrected_scope = {**spoken_scope, "project": project}
                corrected_filters, corrected_issue = resolve_scope(scope_question, corrected_scope, pre_scope_filters)
                if corrected_issue != "unresolved":
                    filters, scope_issue, spoken_scope = corrected_filters, corrected_issue, corrected_scope
        scope_injected_keys = {key for key, value in (filters or {}).items()
                               if key not in pre_scope_filters or pre_scope_filters[key] != value}
    reference = project_reference(question) if "project" not in spoken_scope else ""
    if reference and not scope_issue:
        if bare_reference:
            for field in ("source", "domain", "status"):
                qu_filters.pop(field, None)
        if not unique_selection:
            context_project = project_context if project_context is not None else (
                conversation_project(question, history or []) if history else {})
            if project_id := context_project.get("project_id"):
                filters, scope_issue = resolve_scope(question, {"project": project_id}, filters)
                if not scope_issue:
                    # A resolved follow-up keeps the user's project on retrieval retries.
                    spoken_scope["project"] = context_project["name"]
                    if not bare_reference and not (qu and qu.ok):
                        search_query = f"{context_project['name']} {search_query}"
                    if qu_filters.get("source") == context_project["name"]:
                        qu_filters.pop("source")
            else:
                scope_issue = context_project.get("issue", "project_reference")
    scope_meta = {"metadata_scope": spoken_scope} if spoken_scope else {}
    if scope_issue:
        messages = {
            "negated": "暂不支持用问句排除指定的项目、业务方向、归属部门或文档类型。请改为指定要查的范围。",
            "multiple": "这次查询在同一字段指定了多个值。请先指定一个值，或分开查询。",
            "unresolved": "无法识别所指定的检索范围，请核对项目、业务方向、归属部门或文档类型的名称。",
            "empty": "指定范围内没有匹配的项目。",
            "conflict": "问题中指定的范围与已选筛选条件没有交集，请调整后重试。",
            "project_reference": "请先选择项目，或在问题里写明要查询的项目名称。",
            "project_reference_ambiguous": "这段对话里最近提到了多个项目，请写明这次要查哪一个。",
        }
        if scope_issue in ("unresolved", "multiple"):
            # 回退而非拦截：范围词解析失败（主档未登记 / 同字段多值）不代表内容不存在——
            # 丢弃该元数据过滤，回退全库语义检索；权限、日期与手动设置不受影响。
            # 答案顶部会注明「未按该条件过滤」，不静默忽略用户给定的条件。
            attempted = "、".join(str(value) for value in spoken_scope.values())
            if len(attempted) > 30:
                attempted = attempted[:30] + "…"
            notice = (f"「{attempted}」未匹配到已登记的项目、业务方向、归属部门或文档类型，"
                      "本次未按该条件过滤，以下为全库语义检索结果。" if attempted
                      else "问题在同一筛选字段出现了多个值，本次未按该条件过滤，以下为全库语义检索结果。")
            trace.append(make_node(
                "🔄", "范围解析回退", time_str=now_str(), status=f"已回退（{scope_issue}）",
                summary="问句中的范围词无法解析为元数据过滤条件（主档未登记或同字段多值）。"
                        "已丢弃该过滤并回退为全库语义检索，答案顶部注明未按该条件过滤。",
                items=[("回退原因", messages[scope_issue]),
                       ("提取到的范围", str(spoken_scope) or "（同字段多个值）"),
                       ("保留的条件", "权限、日期与手动筛选设置不受影响")],
            ))
            scope_meta = {"scope_fallback": {"issue": scope_issue,
                                             "attempted": dict(spoken_scope),
                                             "notice": notice}}
            spoken_scope = {}
            scope_issue = ""
        else:
            result.answer = messages[scope_issue]
            result.filters = effective_filters(filters)
            meta = {**scope_meta, "scope_issue": scope_issue, "scope_notice": result.answer,
                    "coverage": "not_found"}
            result.evidence_meta = meta
            return result, meta

    listing_scope = {**spoken_scope, "project": reference} if reference else spoken_scope
    deterministic_listing = is_scope_listing(question, listing_scope)
    inventory_requested = False
    inventory_filters = None
    if model_inventory:
        inventory_text = inventory_question_text(scope_question, spoken_scope)
        pure_inventory = not (staff_total or version_scopes is not None or inventory_needs_content(inventory_text))
        inventory_requested = pure_inventory and not (qu.sub_queries or qu.enumeration_scope or qu_filters.get("source"))
        domain, domain_issue = inventory_domain_scope(inventory_text, qu.inventory_domain) if pure_inventory else (None, "")
        selected_domain = (filters or {}).get("domain")
        if domain and selected_domain not in (None, "all", domain):
            domain_issue = "inventory_domain_conflict"
        domain_notices = {
            "inventory_domain_negated": "文件清单暂不支持按排除条件筛选领域，请指定要盘点的领域。",
            "inventory_domain_multiple": "这次列举涉及多个领域，请指定其中一个领域，或分别查询。",
            "inventory_domain_unresolved": "未找到问句指定的领域，请核对领域名称后再查询文件清单。",
            "inventory_domain_conflict": "问句指定的领域与当前筛选不一致，请调整领域后再查询文件清单。",
        }
        if domain_issue in domain_notices:
            result.answer = domain_notices[domain_issue]
            result.filters = effective_filters(filters)
            meta = {**scope_meta, "scope_issue": domain_issue, "scope_notice": result.answer,
                    "coverage": "not_found"}
            result.evidence_meta = meta
            return result, meta
        if (inventory_requested and "scope_fallback" not in scope_meta
                and not (qu.metadata_scope.keys() - listing_scope.keys())
                and len(date_filters) <= 1 and (not qu.time_range or date_filters)
                and not domain_issue):
            inventory_filters = dict(filters or {})
            if qu_filters.get("status"):
                inventory_filters.setdefault("status", qu_filters["status"])
            if domain:
                inventory_filters["domain"] = domain
    if deterministic_listing or inventory_filters is not None:
        semantic_listing = inventory_filters is not None
        catalog_result, meta = _catalog_evidence(
            question, inventory_filters if semantic_listing else filters, cfg,
            file_dates=date_filters if semantic_listing else None)
        meta.update(scope_meta)
        if semantic_listing:
            meta["inventory_intent"] = True
        catalog_result.trace[:0] = trace
        catalog_result.usage = result.usage
        elapsed = result.elapsed.get("query_understanding", 0.0)
        catalog_result.elapsed["query_understanding"] = elapsed
        catalog_result.elapsed["total"] += elapsed
        return catalog_result, meta

    if date_filters:
        filters = {**date_filters, **(filters or {})}
    route = enumeration_route(question, enumeration_mode,
                              source=(filters or {}).get("source") or qu_filters.get("source"),
                              hint=getattr(qu, "enumeration_scope", "") if qu and qu.ok else "")
    enum_stats = {"route": "version_comparison" if version_scopes is not None else route}
    if inventory_requested:
        enum_stats["inventory_intent"] = True
    enum_truncated = False

    # 节点 ③④⑤⑥：Query Embedding → Metadata Filter → 数据库检索 → 召回 Chunk
    # 过滤条件优先级：界面手动设置 > Query 理解推断 > 默认 status=active
    # Rerank 开启时先宽召回（默认 10 条候选），精排后再取 Top K
    # V3.8.1 枚举通道：识别到列举/计数意图后，整条管线切换到枚举参数集——
    # 召回与上下文预算独立于普通 Top K；文档级精排在召回后单独执行，完整性由 coverage 描述。
    if enumeration_mode:
        recall_k = ENUM_RECALL_K
        ctx_max_items = ENUM_CTX_ITEMS
        ctx_max_per_doc = 999
        use_rerank_now = False
        enum_ctx_cfg = replace(cfg, context_max_tokens=ENUM_CTX_TOKENS)
    else:
        use_rerank_now = use_rerank and (use_llm or cfg.rerank_backend == "local_onnx")
        recall_k = max(recall_k, cfg.rerank_recall_k) if use_rerank_now else recall_k
        ctx_max_per_doc = None  # 非枚举：组装时回落到 .env 的 CONTEXT_MAX_PER_DOC
        enum_ctx_cfg = cfg
    merged_filters = {**qu_filters, **(filters or {})}
    result.filters = effective_filters(merged_filters)
    _p("🔍 数据检索中…")
    t0 = time.time()
    retriever = get_retriever()
    retrieval_diagnostics: dict = {}

    # 节点 ②.5：查询拆分检索（V3.4 Sub-Query）——复合问题（一个问题多个信息需求）
    # 由 Query 理解拆成多个子查询，逐个独立检索后合并去重。
    # 不拆分时走原始单查询检索；两种路径产出统一为 result.retrieved 供精排。
    sub_q_lists = [s for s in ((qu.sub_queries if (qu and qu.ok) else []) or []) if s and s.strip()][:4]
    _sub_top_keys: list[str] = []  # 每个子查询的 Top-1 证据（拆分分支填充，精排后前置）
    mention_keys: list[str] = []  # 项目提及补全卡（方案 A，组装保底前置）
    if len(sub_q_lists) < 2:
        # 确定性拆分兜底（V3.4 的离线补充）：LLM 未拆分时，「A、B、C…的 X 分别…」
        # 高置信并列句按固定规则拆出子查询——实测该句式 LLM 拆分存在波动，不拆时
        # 各文档事实在候选池互相稀释。LLM 已拆分（≥2 个子查询）时以 LLM 结果为准。
        fallback_queries = enumerated_sub_queries(question)
        if len(fallback_queries) >= 2:
            sub_q_lists = fallback_queries
            trace.append(make_node(
                "🔀", "确定性查询拆分", time_str=now_str(),
                status="已执行（LLM 未拆分，离线规则兜底）",
                summary="问题含「并列实体 + 字段 + 分别/各自」的高置信句式，按固定规则拆为"
                        "逐实体子查询，各自独立检索后由保底配额合并——避免各实体事实在"
                        "单一查询的候选池里互相稀释。",
                items=[("拆分结果", "；".join(sub_q_lists)),
                       ("兜底原因", "Query 理解本次未输出 sub_queries")],
            ))

    if version_scopes is not None:
        for key, label, _ in VERSION_SIDES:
            statuses = version_scopes[key]
            if statuses:
                result.retrieved.extend(retriever.retrieve(
                    question, top_k=recall_k, filters={**merged_filters, "status": list(statuses)},
                    trace=None, use_hybrid=use_hybrid, diagnostics=retrieval_diagnostics,
                ))
        searched_statuses = [status for statuses in version_scopes.values() for status in statuses]
        if searched_statuses:
            result.filters["status"] = searched_statuses
        trace.append(make_node("📚", "新旧版本分侧检索", time_str=now_str(),
                               summary="分别召回现行资料与历史资料，两侧沿用当前用户权限和手动筛选。",
                               items=[(label, "、".join(version_scopes[key]) or "当前筛选未包含该侧")
                                      for key, label, _ in VERSION_SIDES]))
    elif len(sub_q_lists) >= 2:
        sq_start = now_str()
        sq_lines = []
        # 每个子查询独立检索，各自保留原始结果列表
        per_sq_results: list[list] = []
        for i, sq in enumerate(sub_q_lists, start=1):
            sub_items = retriever.retrieve(
                sq, top_k=recall_k, filters=merged_filters, trace=None,
                keyword_query=None, use_hybrid=use_hybrid,
                diagnostics=retrieval_diagnostics,
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
        # 最终组装保底（V3.5 候选池配额在组装层的延伸）：记录每个子查询的
        # Top-1 证据，精排后把它们提到候选池最前——多文档对比题里单文档的
        # 全量扩展可能把弱势子问题的关键卡挤出最终名额（实测三文档密码题：
        # 手册的无关章节挤掉了安全制度卡与规格书关键卡）。池配额只保证
        # "进候选池"，这里保证"进上下文"。
        for sub_items in per_sq_results:
            if sub_items:
                key = sub_items[0].metadata.get("chunk_id") or sub_items[0].text[:40]
                if key not in _sub_top_keys:
                    _sub_top_keys.append(key)
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
            diagnostics=retrieval_diagnostics,
        )

    # 渐进式放宽（检索容错）：LLM 推断的过滤条件猜错时可能把正确答案挡在门外——
    # 如果「带推断条件」检索为 0 条，自动去掉推断条件（保留手动设置）重试一次。
    # 重试保留权限、手动设置和已确认的范围（含日期）——口头/显式范围在一级放宽中
    # 始终保留（R2 既定规则，tests/test_query_scope 有断言）。
    # 二级回退（R6-M09）：一级仍 0 召回且带有问句推断的范围键（project_id/
    # department/doc_type）时——跨项目文档（如全公司台账不挂单个项目）会被项目
    # 过滤筛空——去掉范围键再试一次，并在答案顶部注明「已放宽」（不静默忽略
    # 用户指定的范围）。界面手动选择的键从不参与任何一级放宽。
    # document 路由对齐（R6-M09 残余）：QU 推断的点名文档（source 在 qu_filters）
    # 0 召回时同样放宽——归一失败/版本筛空不该让整题空手；手动/MCP 指定的
    # source（在 filters 里）仍不放宽，与"手动设置不放宽"规则一致。
    relaxed_doc_source = str(qu_filters.get("source") or "") if route == "document" else ""
    if (not result.retrieved and (qu_filters or scope_injected_keys)
            and (route != "document" or relaxed_doc_source)):
        relax_start = now_str()
        t_relax = time.time()
        base_filters = filters or {}
        relax_steps: list[str] = []
        if qu_filters:  # 一级：去 LLM 推断 filters（document 路由含推断 source）
            result.retrieved = retriever.retrieve(
                search_query, top_k=recall_k, filters=base_filters, trace=None,
                keyword_query=(qu.keyword_query if qu and qu.ok else None),
                use_hybrid=use_hybrid,
                diagnostics=retrieval_diagnostics,
            )
            result.filters = effective_filters(base_filters)
            relax_steps.append(f"一级（去 LLM filters）→ 召回 {len(result.retrieved)} 条")
        if not result.retrieved and relaxed_doc_source and result.filters.get("source"):
            # document 路由的 source 也可能由范围解析写入 filters（如点名+范围叠加）：
            # 推断意图已被一级剥离，这里兜底去掉 filters 残留的推断 source 再试一次。
            no_source_filters = {k: v for k, v in (filters or {}).items() if k != "source"}
            result.retrieved = retriever.retrieve(
                search_query, top_k=recall_k, filters=no_source_filters, trace=None,
                keyword_query=(qu.keyword_query if qu and qu.ok else None),
                use_hybrid=use_hybrid,
                diagnostics=retrieval_diagnostics,
            )
            result.filters = effective_filters(no_source_filters)
            relax_steps.append(f"一级补充（去推断 source）→ 召回 {len(result.retrieved)} 条")
        if relaxed_doc_source and result.retrieved and "scope_fallback" not in scope_meta:
            scope_meta["scope_fallback"] = {
                "issue": "source_relaxed",
                "attempted": {"source": relaxed_doc_source},
                "notice": (f"按文档「{relaxed_doc_source[:30]}」过滤未找到资料，"
                           "已放宽为全库语义检索结果。"),
            }
        if not result.retrieved and scope_injected_keys:
            # 二级前置校验：项目范围 0 召回且主档在库中无任何关联文档时，尊重范围
            # 诚实空（此时全库捞回的都是无关项目的内容，提示行也救不回语义）——
            # 只有项目确有关联文档、目标事实可能落在未挂项目的跨项目文档（如全公司
            # 台账）时，才值得去范围键找一次（R6-M09 场景）。
            scope_project_ids = base_filters.get("project_id")
            if "project_id" in scope_injected_keys and scope_project_ids:
                pid = (scope_project_ids[0] if isinstance(scope_project_ids, (list, tuple))
                       else scope_project_ids)
                linked = get_vector_store().get_all_cards(
                    filters={"project_id": [pid] if isinstance(pid, int) else pid,
                             "status": "active"}, limit=1)
                if not linked:
                    relax_steps.append("二级跳过：该项目在库中无任何关联文档，保持范围诚实空")
                    scope_injected_keys = set()
            if scope_injected_keys:
                # 二级：口头范围 0 召回，去范围键重试（保留权限/手动/日期）
                relaxed_filters = {key: value for key, value in base_filters.items()
                                   if key not in scope_injected_keys}
                result.retrieved = retriever.retrieve(
                    search_query, top_k=recall_k, filters=relaxed_filters, trace=None,
                    keyword_query=(qu.keyword_query if qu and qu.ok else None),
                    use_hybrid=use_hybrid,
                    diagnostics=retrieval_diagnostics,
                )
                result.filters = effective_filters(relaxed_filters)
                relax_steps.append(f"二级（去范围键 {sorted(scope_injected_keys)}）→ 召回 {len(result.retrieved)} 条")
                if result.retrieved and "scope_fallback" not in scope_meta:
                    # 范围放宽不静默：答案顶部注明（复用 scope_fallback 提示链路）
                    attempted_names = "、".join(str(v) for v in spoken_scope.values()) or "指定范围"
                    if len(attempted_names) > 30:
                        attempted_names = attempted_names[:30] + "…"
                    scope_meta["scope_fallback"] = {
                        "issue": "relaxed_empty",
                        "attempted": dict(spoken_scope),
                        "notice": (f"按「{attempted_names}」过滤未找到资料，"
                                   "已放宽为全库语义检索结果。"),
                    }
        trace.append(make_node(
            "🔄", "过滤放宽重试", time_str=relax_start, elapsed=time.time() - t_relax,
            status="已触发",
            summary="带推断过滤条件的检索召回了 0 条——推断的条件可能过严，把正确答案挡在了门外。"
                    "一级去掉 LLM 推断的条件（保留口头范围与手动设置）重试；仍为 0 且带有"
                    "问句推断的范围键时二级去范围键重试并在答案顶部注明——"
                    "宁可范围大一点，也不能漏掉答案，且绝不静默丢弃用户指定的范围",
            items=[
                ("首检条件（0 条召回）", str(effective_filters(merged_filters))),
                ("放宽步骤", "；".join(relax_steps)),
                ("最终条件", str(result.filters)),
                ("重试结果", f"召回 {len(result.retrieved)} 条"),
            ],
        ))

    # 项目语义补全（方案 A：挂接 OR 正文提及）：问句推断的项目过滤召回的是
    # 「挂了 project_id 的文档」，而全公司台账这类跨项目表格不挂单个项目——
    # 尽管正文写着「凤凰项目 | M1 | 2026-04-30」（BM25/向量都能 top1 命中），
    # 仍被元数据过滤挡在候选外。补一路 BM25 召回「正文确实提及项目名、但未
    # 挂接该项目」的卡片（严格字符串包含验证，最多 5 张），标记「项目提及」
    # 通道并入候选——项目相关性语义完整 = 挂接 ∪ 正文提及。仅对推断型项目
    # 过滤生效（scope_injected_keys），手动 UI 项目筛选行为不变。
    mention_keys = _append_project_mention_cards(
        result, question, filters or {}, scope_injected_keys, spoken_scope, recall_k)
    if mention_keys:
        enum_stats["project_mention"] = len(mention_keys)
        trace.append(make_node(
            "🔗", "项目语义补全", time_str=now_str(),
            summary=(f"项目过滤只覆盖挂接文档；正文提及该项目的跨项目文档（如台账）"
                     f"以 BM25 补入 {len(mention_keys)} 张（已验证正文包含项目名）。"),
            items=[("提及通道", "挂接 ∪ 正文提及")],
        ))

    recalled_cards = len(result.retrieved)
    enumerated_additions = 0
    reranked_cards = 0
    # 文档定位/跨文档精排与普通问答的卡片精排分开，避免 Top K 截掉列举条目。
    if enumeration_mode and result.retrieved:
        if len(sub_q_lists) >= 2:
            route = "cross_document"
            enum_stats["route"] = route
        reps = document_representatives(result.retrieved)
        candidate_documents = len(reps)
        enum_truncated = len(result.retrieved) >= recall_k
        rr_elapsed = 0.0
        if route == "cross_document" and use_rerank and (use_llm or cfg.rerank_backend == "local_onnx"):
            rr_start = time.time()
            ranked = reps[:cfg.enum_rerank_docs]
            # 保留未进入精排预算的文档在尾部，所有预算损失均进入 coverage。
            if use_llm:
                outcome = rerank_items(question, ranked, top_k=len(ranked),
                                       llm=get_llm_client(), config=cfg)
            else:
                # MCP 证据模式只允许本地推理；本地失败不降级为云端 LLM。
                outcome = rerank_documents_local(question, ranked, len(ranked), cfg)
            record_stage_usage(result, "rerank", outcome.usage)
            reps = outcome.items + reps[cfg.enum_rerank_docs:]
            rr_elapsed = time.time() - rr_start
            trace.append(make_node("🏆", "文档级精排", time_str=now_str(), elapsed=rr_elapsed,
                                   status="已执行" if outcome.ok else "回退（原文档顺序）",
                                   summary="按标题、章节和代表性命中片段对文档评分；不按普通问答 Top K 截断。",
                                   items=[("候选文档", str(candidate_documents)),
                                          ("评分文档", str(len(ranked))),
                                          ("后端", str(outcome.backend))]))
        # 明确指定 source 时保留同名但不同路径的文档；未明确时选择最相关的一篇。
        selected = reps[:(cfg.enum_expand_docs if route == "cross_document" or result.filters.get("source") else 1)]
        enum_truncated |= len(selected) < len(reps)
        expanded, expansion_truncated = expand_documents(
            get_vector_store(), selected, result.retrieved, result.filters, cfg.enum_doc_cards)
        recalled_ids = {hit.metadata.get("chunk_id") for hit in result.retrieved}
        enumerated_additions = sum(hit.metadata.get("chunk_id") not in recalled_ids for hit in expanded)
        result.retrieved = expanded
        enum_truncated |= expansion_truncated
        enum_stats.update(candidate_documents=candidate_documents,
                          candidate_document_keys=[list(document_key(r)) for r in reps],
                          selected_documents_meta=[{k: r.metadata.get(k) for k in
                                                    ("document_id", "version", "source", "path")} for r in selected],
                          selected_documents=len(selected), rerank_documents=min(candidate_documents, cfg.enum_rerank_docs)
                          if rr_elapsed else 0, expansion_truncated=expansion_truncated,
                          rerank_elapsed=rr_elapsed)
        trace.append(make_node("📚", "列举文档展开", time_str=now_str(),
                               summary=f"路径：{route}；候选 {candidate_documents} 篇，展开 {len(selected)} 篇，"
                                       f"每篇最多 {cfg.enum_doc_cards} 张；沿用权限、状态与版本过滤。",
                               items=[("展开候选卡片", str(len(expanded))),
                                      ("展开截断", str(expansion_truncated))]))
    result.elapsed["retrieval_only"] = time.time() - t0
    result.elapsed["rerank"] = enum_stats.get("rerank_elapsed", 0.0)
    result.elapsed["retrieval_only"] -= result.elapsed["rerank"]
    # 精排包含模型加载、句对推理和实际发生的后端降级。
    if use_rerank_now and result.retrieved:
        _p("🏆 Rerank 精排中…")
        rr_start = now_str()
        t_rr = time.time()
        if use_llm:
            outcome = rerank_items(question, result.retrieved, top_k=top_k,
                                   llm=get_llm_client(), config=cfg)
        else:
            outcome = rerank_documents_local(question, result.retrieved, top_k, cfg)
        record_stage_usage(result, "rerank", outcome.usage)
        rr_elapsed = time.time() - t_rr
        result.elapsed["rerank"] = rr_elapsed
        # 全部打分候选交给上下文组装，由「多样性 + Top K」规则统一精选
        # 注意：不能按 Rerank 分数过滤 0 分卡片——实测发现 Rerank 可能误判
        # （给含答案的卡片打 0 分），过滤后反而丢失正确内容
        result.retrieved = outcome.items
        reranked_cards = len(outcome.scores)
        rr_backend = getattr(outcome, "backend", "llm")
        rr_status = ("已执行" if outcome.ok else f"回退（粗排顺序：{outcome.error[:50]}）")
        if getattr(outcome, "fallback_reason", ""):
            rr_status = f"本地失败，降级 LLM：{outcome.fallback_reason[:80]}"
        if rr_backend == "local_onnx":
            trace.append(make_node(
                "🏆", "Rerank（精排）", time_str=rr_start,
                elapsed=rr_elapsed,
                status=rr_status,
                summary=f"召回先扩宽到 {recall_k} 条候选，本地精排模型（bge-reranker-v2-m3）"
                        f"对每对「问题+候选」打相关度分（sigmoid 归一 0~10），按分精排后取 Top {top_k}"
                        "——粗排只看文字和指纹，精排才理解内容",
                items=[
                    ("精排方式（共 1 种启用）", "✅ 启用：本地 cross-encoder —— bge-reranker-v2-m3 "
                     "ONNX 量化版（CPU 逐对打分，零 API 费用、离线可用）\n"
                     "○ 未启用：LLM 重排 —— 用已配置的大模型逐条打分（RERANK_BACKEND=llm 可切换）"),
                    ("本地精排模型", f"{cfg.rerank_model_dir}\n"
                     "model_quantized.onnx（约 570MB，动态 int8 量化）\n"
                     "tokenizer.json（分词器，模型自带）\n"
                     f"分词规则：{getattr(outcome, 'tokenizer_type', '') or '模型配套规则'}，"
                     "(query, doc) 句对拼接，截断至 2048 token；无需提示词"),
                    ("精排分项计时", "首次加载" if getattr(outcome, "cold_load", False) else "缓存引擎（热运行）"),
                    ("模型加载 / 分词 / 推理", " / ".join(
                        f"{key}={float(getattr(outcome, 'timings', {}).get(key, 0)):.3f}s"
                        for key in ("load", "queue", "tokenization", "inference"))),
                    ("句对 token 数与耗时", "\n".join(
                        f"{d['id']} · {d['token_count']} tokens · 分词 {d['tokenization']:.3f}s · 推理 {d['inference']:.3f}s"
                        for d in getattr(outcome, "pair_details", []))),
                    ("本地精排的结果", "\n".join(
                        f"{cid}  {source}  {score:.1f} 分" for cid, source, score in outcome.scores
                    ) or "（无候选）"),
                    ("排序前后对比", "粗排: " + " → ".join(outcome.before_order)
                     + "\n精排 Top K: " + " → ".join(outcome.after_order)),
                ],
            ))
        else:
            trace.append(make_node(
                "🏆", "Rerank（精排）", time_str=rr_start,
                elapsed=rr_elapsed,
                status=rr_status,
                summary=f"召回先扩宽到 {recall_k} 条候选，LLM 逐条阅读并按相关度打 0~10 分，"
                        f"按分精排后取 Top {top_k}——粗排只看文字和指纹，精排才理解内容",
                items=[
                    ("精排方式（共 1 种启用）", "✅ 启用：LLM 重排 —— 用已配置的大模型逐条阅读候选内容，"
                     "按相关度打 0~10 分\n"
                     "○ 未启用：专用重排模型（bge-reranker / Cohere Rerank 等）——"
                     "专为此任务训练、打分更稳定更快（RERANK_BACKEND=local_onnx 可切换）"),
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
    elif enumeration_mode and result.retrieved and not enum_stats.get("rerank_documents"):
        trace.append(make_node(
            "🏆", "Rerank（精排）", time_str=now_str(),
            status="跳过（枚举模式）",
            summary="列举路径独立处理：指定文档直接展开；跨文档按文档精排，不再逐卡片精排。",
            items=[("说明", f"枚举模式：召回 {recall_k} 条候选；上下文最多 {ctx_max_items} 条、"
                           f"约 {ENUM_CTX_TOKENS} tokens，同文档不限条数")],
        ))

    # 子查询证据保底：精排重排后，把每个子查询的 Top-1 证据提到候选池最前。
    # 精排按原始整题打分，弱势子问题的最佳证据可能被其他主题的高分卡压到
    # 名额之外；前置后组装层按顺序选取，各子需求的关键证据必进上下文。
    # 项目提及卡（方案 A）同样前置——多事实主导文档扩展会把它们挤出 token 预算
    # （实测：凤凰里程碑在台账提及卡里，被凤凰可研/经营计划的全卡扩展淹没）。
    guarantee_keys = _sub_top_keys + [k for k in mention_keys if k not in _sub_top_keys]
    if guarantee_keys and result.retrieved:
        by_key: dict[str, object] = {}
        for it in result.retrieved:
            by_key.setdefault(it.metadata.get("chunk_id") or it.text[:40], it)
        front = [by_key[key] for key in guarantee_keys if key in by_key]
        front_ids = {id(it) for it in front}
        ordered = front + [it for it in result.retrieved if id(it) not in front_ids]
        if len(front) == len(guarantee_keys) and ordered is not result.retrieved \
                and [id(x) for x in ordered[:len(front)]] != [id(x) for x in result.retrieved[:len(front)]]:
            result.retrieved = ordered
            for i, it in enumerate(ordered, start=1):
                it.rank = i
            trace.append(make_node(
                "📌", "子查询证据保底", time_str=now_str(),
                summary=f"已把 {len(front)} 张关键证据（子查询 Top-1 / 项目提及卡）"
                        "提到候选池最前——精排按整题打分或多事实扩展时，弱势子问题"
                        "与跨项目提及的证据可能被挤出最终名额。",
                items=[("保底证据", "\n".join(
                    f"{it.metadata.get('source')} | {it.metadata.get('section') or '-'}" for it in front))],
            ))

    _p("📦 整理资料中…")
    facts_expansion_added = 0
    facts_expansion_docs: set[str] = set()
    multi_fact = len(sub_q_lists) >= 2
    if not enumeration_mode and version_scopes is None and result.retrieved:
        result.retrieved, facts_expansion_added, facts_expansion_docs, facts_notes = (
            expand_facts_dominant_documents(
                result.retrieved, result.filters, top_k, cfg,
                multi_fact=multi_fact))
        if facts_expansion_docs:
            enum_stats["doc_expansion"] = {"documents": sorted(facts_expansion_docs),
                                           "added": facts_expansion_added}
            trace.append(make_node(
                "📚", "多事实文档扩展", time_str=now_str(),
                summary=(f"在 {len(facts_expansion_docs)} 篇候选文档中净补入 {facts_expansion_added} 张卡片。"
                         "普通问答沿用 Top K，复合题按上下文预算采用补片。"),
                items=[("展开明细", "\n".join(facts_notes))],
            ))
    # 节点 ⑧ 合并 / 去重 + 节点 ⑨ 上下文组装（V2.6：多样性 + 相邻补全）
    # 精排后不再截断候选池，由组装规则在全部候选中选出最终进入回答的 Top K
    merge_start = now_str()
    t1 = time.time()
    missing_sides = []
    if version_scopes is None:
        context, used, dropped, merge_notes = build_context(
            result.retrieved, enum_ctx_cfg,
            max_items=ctx_max_items + (facts_expansion_added if multi_fact else 0),
            max_per_doc=ctx_max_per_doc,
            unlimited_docs=facts_expansion_docs or None,
        )
    else:
        context, used, dropped, merge_notes, missing_sides = _version_context(
            result.retrieved, cfg, ctx_max_items, ctx_max_per_doc, version_scopes,
        )
        scope_meta["version_comparison"] = {"missing_sides": missing_sides}
        if missing_sides:
            labels = "、".join(label for key, label, _ in VERSION_SIDES if key in missing_sides)
            result.answer = f"当前检索范围内缺少可用于比较的{labels}证据，无法确认新旧版本的差异。"
            scope_meta["support_notice"] = result.answer
    support = assess_evidence(
        question, [item.text for item in used],
        tuple(spoken_scope[key] for key in ("project", "business_direction") if key in spoken_scope),
    ) if used else {}
    if support:
        trace.append(make_node(
            "🔎", "证据支持检查", time_str=now_str(),
            summary="逐项检查问题点名的编号或明确事实字段是否出现在采用的资料中。",
            items=[("检查结果", str(support))],
        ))
        scope_meta["evidence_support"] = support
        if support["status"] == "unsupported":
            scope_meta["support_notice"] = "找到了候选资料，但没有足够证据支持这个问题。"
            result.answer = scope_meta["support_notice"]
            used = []
            context = "（没有可支持该问题的证据）"
        elif support["status"] == "partial":
            date_missing = [name for name in support["missing"] if name in support.get("date_fields", ())]
            other_missing = [name for name in support["missing"] if name not in date_missing]
            notes = []
            if date_missing:
                notice = f"日期规则未确认：{'、'.join(date_missing)}。请从原文核对对应事件和日期，无法确认时请说明。"
                scope_meta.setdefault("support_notice", notice)
                notes.append(notice)
            if other_missing:
                notes.append(f"没有找到支持以下信息的依据：{'、'.join(other_missing)}。回答时请标明缺失。")
            context = f"【证据范围】{' '.join(notes)}\n\n{context}"
    result.retrieved = used
    result.context = context
    date_notes = []
    for field, label in (("updated_at", "文件修改时间"), ("indexed_at", "入库时间")):
        tr = result.filters.get(field)
        if isinstance(tr, dict) and (tr.get("from") or tr.get("to")):
            date_notes.append(f"{label}：{tr.get('from') or '不限'} ~ {tr.get('to') or '不限'}")
    if date_notes:
        context = "【时间说明】以下资料已按" + "；".join(date_notes) + "筛选。\n\n" + context
        result.context = context
    merge_elapsed = time.time() - t1
    result.elapsed["assembly"] = merge_elapsed
    result.elapsed["retrieval"] = (result.elapsed["retrieval_only"]
                                   + result.elapsed["rerank"] + result.elapsed["assembly"])
    result.sources = [
        {
            "rank": item.rank,
            "score": item.score,
            "source": item.metadata.get("source"),
            "document_id": item.metadata.get("document_id"),
            "path": item.metadata.get("path"),
            "chunk_id": item.metadata.get("chunk_id"),
            "chunk_ids": item.metadata.get("chunk_ids") or [],
            "section": item.metadata.get("section"),
            "section_path": item.metadata.get("section_path"),
            "sections": item.metadata.get("sections", []),
            "section_paths": item.metadata.get("section_paths", []),
            "page": item.metadata.get("page"),
            "page_kind": item.metadata.get("page_kind"),
            "domain": item.metadata.get("domain"),
            "doc_type": item.metadata.get("doc_type") or "",
            "department": item.metadata.get("department") or "",
            "project_id": item.metadata.get("project_id"),
            "classification": classify_visibility(
                item.metadata.get("allowed_groups"), item.metadata.get("allowed_users")),
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
        summary="对召回结果做合并与去重，按本次路径的条数、单文档配额和 token 预算选取；截断计入覆盖提示。",
        items=[
            ("去重 / 合并逻辑", "忽略空白差异后比较卡片全文，重复的只保留第一条；"
             "剩余卡片按相关度依次放入，直到达到条数或字数上限"),
            ("多样性限制", f"同一文档最多采用 {ctx_max_per_doc if ctx_max_per_doc is not None else cfg.context_max_per_doc} 条；本次总名额 {ctx_max_items}"),
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
             f"token 上限约 {enum_ctx_cfg.context_max_tokens}"),
            ("相邻补全", "\n".join(merge_notes) if merge_notes else "（没有可拼接的相邻卡片）"),
            ("输出（最终 Context）", context),
            ("规模", f"共 {len(used)} 条资料，合计 {estimate_tokens(context)} tokens（上限约 {enum_ctx_cfg.context_max_tokens} tokens）"),
        ],
    ))


    # Evidence 覆盖粗判定（V3.9）：零召回=not_found；任何截断（名额/同文档配额/token/
    # 相邻拼接截断）=partial；其余 unknown。事实级 complete 判定依赖结构化数据层（方案
    # 第三批），V1 不签 complete。
    if not result.retrieved:
        coverage = "not_found"
    elif (any("上限" in str(d.get("reason", ""))
              or "最多采用" in str(d.get("reason", ""))
              or "token" in str(d.get("reason", "")).lower()
              for d in (dropped or []))
          or any("截断" in str(note) for note in (merge_notes or []))):
        coverage = "partial"
    else:
        coverage = "unknown"
    result.enumeration = enumeration_mode
    if (enum_truncated or retrieval_diagnostics.get("bm25_truncated")
            or support.get("status") == "partial" or missing_sides) and result.retrieved:
        coverage = "partial"
    enum_stats["processed_documents"] = len(group_documents(used))
    # Query 理解的意图概括随证据元数据透出：分批提取与语义复审共用同一次类别理解，
    # 避免每一级各自重新解读问题（"建模方法"被当成任何名称含"建模"的东西）。
    evidence_meta = {**enum_stats, **scope_meta, **retrieval_diagnostics,
                     "coverage": coverage, "dropped": len(dropped or []),
                     "enumeration": enumeration_mode,
                     "intent": (qu.intent or "") if (use_query_understanding and use_llm and qu) else "",
                     "ctx_max_items": ctx_max_items, "recall_k": recall_k,
                     "evidence_counts": {"requested_top_k": top_k, "recalled_cards": recalled_cards,
                                         "reranked_cards": reranked_cards,
                                         "added_cards": facts_expansion_added + enumerated_additions,
                                         "adopted_cards": sum(len(row["chunk_ids"]) or 1 for row in result.sources),
                                         "citation_entries": len(result.sources)}}
    if enumeration_mode:
        trace.append(make_node("📋", "列举覆盖范围", time_str=now_str(),
                               summary=coverage_notice(evidence_meta),
                               items=[("覆盖信息", str(evidence_meta))]))
    result.evidence_meta = evidence_meta
    result.elapsed["assembly"] = time.time() - t1
    result.elapsed["retrieval"] = sum(result.elapsed[key] for key in ("retrieval_only", "rerank", "assembly"))
    result.elapsed["total"] = result.elapsed["retrieval"] + result.elapsed.get("query_understanding", 0.0)
    return result, evidence_meta


# 明确的求和意图词（"分别是多少"这类不触发——只认问总数的问法）
_SUM_ASK = re.compile(r"合计|合共|总共|总和|加起来|总计|一共.*(多少|几)")
_SUM_MONEY = re.compile(r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(万|亿)?元")


def _enumeration_sum_note(question: str, records) -> str | None:
    """列举记录的确定性求和（R6-C05）：每条已验证记录取原文首个金额，Decimal 求和。

    触发条件从严：问题含明确求和词、记录 ≥2 条、每条都能从已验证 quote（或条目名）
    提到金额且单位可换算——任一不满足即返回 None（不做任何猜测性计算）。
    展示算式与各条目金额，引用见上方各记录行；不改变 coverage 判定。
    """
    from decimal import Decimal, InvalidOperation

    if not records or len(records) < 2 or not _SUM_ASK.search(question or ""):
        return None
    parts: list[str] = []
    values: list[Decimal] = []
    seen_names: set[str] = set()
    for record in records:
        # 同名重复条目（补查轮为同一项目追加的待核实记录）只计一次——
        # 重复计值会把合计翻倍；名称相同的记录金额取首次出现。
        name_key = str(record.get("name") or "").strip()
        if name_key and name_key in seen_names:
            continue
        quote = ""
        if record.get("evidence"):
            quote = str(record["evidence"][0].get("quote") or "")
        match = _SUM_MONEY.search(quote) or _SUM_MONEY.search(str(record.get("name") or ""))
        if not match:
            return None
        if name_key:
            seen_names.add(name_key)
        try:
            value = Decimal(match.group(1).replace(",", ""))
        except InvalidOperation:
            return None
        unit = match.group(2)
        values.append(value * (Decimal(100_000_000) if unit == "亿"
                               else Decimal(10_000) if unit == "万" else Decimal(1)))
        parts.append(match.group(0).replace(" ", ""))
    if len(values) < 2:
        return None
    total = sum(values, Decimal(0))
    if total >= 10_000:
        total_text = f"{(total / 10_000).normalize():f} 万元"
    else:
        total_text = f"{total.normalize():f} 元"
    return ("**合计**：" + " + ".join(parts) + f" = **{total_text}**"
            f"（由以上 {len(values)} 条已验证记录逐项求和）")


def _append_project_mention_cards(result, question: str, filters: dict,
                                  scope_injected_keys: set[str],
                                  spoken_scope: dict, recall_k: int) -> list[str]:
    """项目语义补全（方案 A）：把「正文提及项目名但未挂接」的卡片并入候选。

    仅当：① 本次项目过滤来自问句推断（scope_injected_keys 含 project_id）；
    ② 能取到项目名（spoken_scope['project']）。补充路用 BM25（查询=项目名），
    过滤条件沿用权限/status 但不带 project_id；命中卡片再做严格字符串包含
    验证（正文真的写着该项目名）且未挂接任何项目（挂他项目的提及属于另一
    项目的文档，不并入）。返回补入卡片的 chunk_id 列表——调用方把它们纳入
    最终组装的保底前置（多事实扩展可能把弱势证据挤出 token 预算）。
    """
    project_name = str(spoken_scope.get("project") or "").strip()
    if (not project_name or len(project_name) < 2
            or "project_id" not in scope_injected_keys
            or not (filters or {}).get("project_id")):
        return []
    base_filters = {k: v for k, v in filters.items() if k != "project_id"}
    try:
        from src.keyword_search import bm25_search
        hits = bm25_search(project_name, top_k=max(10, recall_k), filters=base_filters or None)
    except Exception:
        return []
    seen = {item.metadata.get("chunk_id") for item in result.retrieved}
    added_keys: list[str] = []
    for hit in hits:
        payload = hit.get("payload") or {}
        text = str(payload.get("text") or "")
        card_pid = payload.get("project_id")
        if (payload.get("chunk_id") in seen
                or project_name not in text
                or (card_pid is not None and str(card_pid).strip() not in ("", "None"))):
            continue  # 已在候选 / 正文未写项目名 / 已挂接（含挂到其他项目）
        seen.add(payload.get("chunk_id"))
        result.retrieved.append(RetrievedItem(
            rank=len(result.retrieved) + 1, score=float(hit.get("score") or 0.0),
            text=text, metadata={k: v for k, v in payload.items() if k != "text"},
            channels="项目提及"))
        added_keys.append(payload.get("chunk_id"))
        if len(added_keys) >= 5:
            break
    return added_keys


def _prepend_scope_fallback_notice(result) -> None:
    """范围回退提示：解析失败回退全库检索时，在答案顶部注明未按该条件过滤（不静默忽略）。

    fallback 状态记录在 evidence_meta["scope_fallback"]（含 issue/attempted/notice）；
    applied 标记防止在多个答案落点重复 prepend。evidence 模式（无答案）由调用方
    从 evidence_meta 透出，MCP coverage_details 同样可见。
    """
    fallback = (result.evidence_meta or {}).get("scope_fallback")
    if not fallback or fallback.get("applied") or not result.answer:
        return
    fallback["applied"] = True
    result.answer = f"> ℹ️ {fallback['notice']}\n\n{result.answer}"


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
    session_id: str | None = None,
    conversation_state: dict | None = None,
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
    from src.auth_db import retrieval_acl_revision
    filters = scope_filters(filters, user_groups, username)
    # 领域文件夹语义（R6-M09 延伸，与 query_evidence 注入点一致）：answer_stream 有
    # 自己的早退目录检查，domain 注入必须在此之前完成，两个出口行为才一致。
    from src.query_scope import domain_folder_scope
    folder_domain = domain_folder_scope(question)
    if folder_domain and not (filters or {}).get("domain"):
        filters = {**(filters or {}), "domain": folder_domain}
    epoch_at_start = (index_epoch.current(), retrieval_acl_revision())

    def _invalidate_changed_output(result):
        revision = (index_epoch.current(), retrieval_acl_revision())
        if revision == epoch_at_start and (user_groups is None or revision[1] is not None):
            return False
        result.answer = "检索期间资料或权限发生变化，请重新查询。"
        result.context = ""
        result.sources.clear()
        result.retrieved.clear()
        result.enumerated_items.clear()
        result.trace.clear()
        result.evidence_meta.clear()
        result.evidence_meta.update(coverage="not_found", scope_issue="index_changed",
                                    scope_notice=result.answer, generation_status="invalidated")
        return True

    archive_history = history or []
    prepared = None
    memory_text = ""
    project_context = None
    if session_id:
        # No global session-memory store: only this caller's archive/state is read.
        state = conversation_state if conversation_state is not None else {}
        saved = (state.get("project_reference") if state.get("session_id") == session_id
                 and state.get("owner") == (username or "") else None)
        summary_llm = (get_llm_client() if use_llm and len(archive_history) > cfg.chat_recent_turns * 2
                       and enumeration_route(question, True) != "catalog"
                       and sum(estimate_tokens(m.get("content", "")) for m in archive_history)
                           >= cfg.chat_summary_trigger_tokens else None)
        prepared = prepare_context(question, archive_history,
                                   state,
                                   session_id, username or "", filters or {},
                                   json.dumps(epoch_at_start) if epoch_at_start[1] is not None
                                   else os.urandom(16).hex(), cfg, summary_llm)
        history, memory_text = prepared.history, prepared.memory or "（当前单会话无旧摘要，近期对话已按预算提供。）"
        project_context = conversation_project(question, archive_history, saved if isinstance(saved, dict) else None)
        if project_context and "turn" in project_context:
            state["project_reference"] = project_context
        elif not project_context:
            state.pop("project_reference", None)

    def _attach_conversation(result):
        if prepared is not None:
            result.conversation_meta = prepared.meta
            result.elapsed["conversation"] = prepared.elapsed
            result.elapsed["total"] = result.elapsed.get("total", 0.0) + prepared.elapsed
            record_stage_usage(result, "conversation_summary", prepared.usage)
            result.trace.append(make_node("💬", "单会话上下文", time_str=now_str(), elapsed=prepared.elapsed,
                                          summary="近期对话 + 可追溯摘要 + 当前会话历史召回；旧引用不作为本轮知识证据。",
                                          items=[("范围与预算", str(prepared.meta))]))
        return result

    if enumeration_route(question, True) == "catalog":
        result, _ = _catalog_evidence(question, filters, cfg)
        if _invalidate_changed_output(result):
            return result, iter(())
        return _attach_conversation(result), iter(())

    # Q→A 缓存：同一问题+同一配置+同一对话上下文直接返回上次结果；
    # 仅检索模式（use_llm=False）不读缓存——它要的是检索结果而非缓存的 LLM 答案
    cache_key = _cache_key(question, top_k, filters or {}, use_query_understanding,
                           use_hybrid, use_rerank, history=archive_history)
    cache_key = ("enumeration-processing-v2|" + cache_key
                 + f"|enum={ENUM_RECALL_K},{ENUM_CTX_ITEMS},{ENUM_CTX_TOKENS},"
                   f"{cfg.enum_rerank_docs},{cfg.enum_expand_docs},{cfg.enum_doc_cards}")
    cache_key += "|stage2=" + str(tuple(getattr(cfg, key) for key in (
        "enum_stage2_enabled", "enum_max_rounds", "enum_queries_per_round", "enum_no_new_rounds",
        "enum_max_calls", "enum_max_seconds", "enum_max_tokens", "enum_batch_tokens", "enum_page_cards",
        "enum_max_cards", "enum_max_documents", "enum_max_items", "enum_output_tokens")))
    cache_key += "|session=" + hashlib.sha256(json.dumps(
        [username or "", session_id or "stateless", memory_text,
         cfg.chat_context_max_tokens, cfg.chat_recent_turns, cfg.chat_summary_tokens,
         cfg.chat_summary_trigger_tokens, cfg.chat_recall_messages], ensure_ascii=False).encode()).hexdigest()
    cache_key += f"|date={date.today()}|revision={epoch_at_start!r}"
    cache_allowed = (epoch_at_start[1] is not None and (bool(session_id) or not archive_history)
                     and not project_reference(question))
    _sync_qa_cache_epoch(epoch_at_start)
    # .get 取一次：判断与读取分两步时，并发清空会让第二步抛 KeyError
    cached_entry = _qa_cache.get(cache_key) if use_llm and cache_allowed else None
    if cached_entry is not None and epoch_at_start == (index_epoch.current(), retrieval_acl_revision()):
        import copy
        cached = copy.deepcopy(cached_entry)
        cached.cache_hit = True
        cached.request_usage = {'stages': {}}
        if prepared is not None and (prepared.usage.get('input') or prepared.usage.get('output')):
            cached.request_usage['stages']['conversation_summary'] = dict(prepared.usage)
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

    # 早期节点（就绪/提问/会话记忆）先入 head 列表；检索半程（query_evidence）返回后合并进 result.trace
    head_trace: list = []
    trace = head_trace

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

    # V3.9 Evidence 统一查询入口：检索半程（意图→检索→融合→筛选→组装）抽为独立函数，
    # Web 答案模式与 MCP 证据模式（knowledge_search）共用同一套检索规则
    result, evidence_meta = query_evidence(
        question=question, top_k=top_k, filters=filters,
        use_query_understanding=use_query_understanding, use_hybrid=use_hybrid,
        use_rerank=use_rerank, history=history, use_llm=use_llm,
        on_progress=_p, config=cfg, conversation_context=memory_text,
        user_groups=user_groups, username=username, project_context=project_context)
    _attach_conversation(result)
    result.evidence_meta = evidence_meta   # V3.9：MCP 证据模式透出 coverage
    result.trace[:0] = trace   # 早期节点（就绪/提问/会话记忆）并入时间线头部
    trace = result.trace
    context = result.context  # 生成半程需要的组装资料（Evidence 已含四道检查）
    if _invalidate_changed_output(result):
        return result, iter(())
    if result.answer is not None:
        return result, iter(())
    if evidence_meta.get("metadata_scope") and not result.retrieved:
        result.answer = "指定范围内没有找到你有权查看的匹配文档。"
        return result, iter(())
    if use_llm and not result.retrieved:
        result.answer = "知识库中没有找到可用于回答这个问题的资料。"
        _prepend_scope_fallback_notice(result)
        return result, iter(())
    if not use_llm:
        if result.enumeration:
            evidence_meta["stage2"] = {"status": "not_run", "reason": "evidence_only"}
        return result, iter(())

    if (cfg.enum_stage2_enabled and result.enumeration and result.retrieved
            and evidence_meta.get("route") in {"document", "cross_document"}):
        _p("📋 分批提取与补查中…")
        locked_filters = dict(result.filters)
        def _supplement(query, deadline):
            return get_retriever().retrieve(query, top_k=ENUM_RECALL_K,
                                            filters={"status": "all", **locked_filters},
                                            use_hybrid=use_hybrid, deadline=deadline)
        def _rank_documents(reps, bounded_llm):
            ranked = rerank_items(question, reps, top_k=len(reps), llm=bounded_llm, config=cfg)
            if not ranked.ok:
                state = bounded_llm.outcome.stats
                state["rerank_failures"] = state.get("rerank_failures", 0) + 1
            return ranked.items
        outcome = process_enumeration(
            question, evidence_meta.get("selected_documents_meta") or
                      [it.metadata for it in result.retrieved],
            [tuple(k) for k in evidence_meta.get("candidate_document_keys", [])],
            locked_filters, get_vector_store(), _supplement,
            _rank_documents if use_rerank else None,
            get_llm_client(), cfg, evidence_meta["route"], on_progress=_p,
            conversation_context=memory_text + "\n" + json.dumps(history or [], ensure_ascii=False),
            intent=evidence_meta.get("intent", ""))
        for index, usage in enumerate(outcome.calls, 1):
            record_stage_usage(result, f"enumeration_{index}", usage)
        result.elapsed["enumeration"] = outcome.stats["elapsed"]
        if _invalidate_changed_output(result):
            return result, iter(())
        evidence_meta["stage2"] = {"status": "executed", **outcome.stats}
        evidence_meta["candidate_documents"] = outcome.stats["candidate_documents"]
        evidence_meta["processed_documents"] = outcome.stats["processed_documents"]
        # 语义条目提取不签 complete；达到预算、无效证据或错误均明确 partial。
        limited = (outcome.stats["stop_reason"].endswith("budget") or outcome.stats["errors"]
                   or outcome.stats["rejected_items"] or outcome.stats["selection_truncated"]
                   or outcome.stats.get("rerank_failures") or evidence_meta["coverage"] == "partial")
        evidence_meta["coverage"] = "partial" if limited else "unknown"
        trace.append(make_node("📋", "分批提取与多轮补查", time_str=now_str(),
                               elapsed=outcome.stats["elapsed"], summary=str(outcome.stats),
                               items=[("停止原因", outcome.stats["stop_reason"]),
                                      ("提取记录", str(len(outcome.records))),
                                      ("范围说明", "每轮沿用原权限、状态、日期和文件过滤；停止不证明全库完整。")]))
        if outcome.records or (outcome.stats["valid_batches"] and not outcome.stats["errors"]):
            result.enumerated_items = outcome.records
            result.sources = outcome.sources
            result.retrieved = [RetrievedItem(rank=s["rank"], score=s["score"], text=s["text"],
                                              metadata={k: v for k, v in s.items() if k not in
                                                        ("rank", "score", "text", "channels")},
                                              channels=s["channels"]) for s in outcome.sources]
            result.context = "\n\n".join(f"[{s['rank']}] {s.get('source')} | {s.get('section') or '-'}\n{s['text']}" for s in outcome.sources)
            result.answer = render_records(outcome, resolve_answer_language(question, history),
                                           coverage=evidence_meta["coverage"],
                                           inventory_hint=evidence_meta.get("inventory_intent", False))
            # 确定性求和（R6-C05）：问题明确要合计、且每条已验证记录的原文里都有
            # 金额时，代码层 Decimal 求和并附算式——不靠 LLM 心算，任一条缺金额
            # 或单位不可比就不算（宁缺毋滥，不猜）。
            sum_note = _enumeration_sum_note(question, outcome.records)
            if sum_note:
                result.answer = f"{result.answer}\n\n{sum_note}"
                evidence_meta["enumeration_sum"] = True
            _prepend_scope_fallback_notice(result)
            result.elapsed["llm"] = 0.0  # 提取/规划的模型用量与耗时单列在 enumeration。
            result.elapsed["total"] = sum(result.elapsed.get(k, 0.0) for k in
                                          ("query_understanding", "retrieval_only", "rerank", "assembly", "enumeration", "conversation"))
            if _invalidate_changed_output(result):
                return result, iter(())
            if not limited and cache_allowed and not evidence_meta.get("metadata_scope"):
                _remember_answer(cache_key, result, username)
            return result, iter(())
        # 提取未成功时保留首轮检索证据，回退普通生成并提示不完整。
        evidence_meta["coverage"] = "partial"
        evidence_meta["stage2"]["fallback"] = True
    elif result.enumeration:
        evidence_meta["stage2"] = {"status": "not_run", "reason": "disabled_or_empty"}

    # 节点 ⑩⑪：LLM 调用（Prompt 组装 + 生成回答，合并展示）
    t2 = time.time()
    deadline = deadline_constraint(question, result.sources)
    if deadline:
        evidence_meta["deadline_constraint"] = deadline
    bound_project = (evidence_meta.get("metadata_scope") or {}).get("project")
    generation_question = name_project_in_followup(question, bound_project) if bound_project else question
    messages = build_messages(context, generation_question, history=history,
                              enumerate_mode=result.enumeration, evidence_meta=evidence_meta,
                              conversation_context=memory_text)
    llm = get_llm_client()
    result.elapsed["assembly"] += time.time() - t2
    result.elapsed["retrieval"] = sum(result.elapsed[key] for key in ("retrieval_only", "rerank", "assembly"))

    def _generate():
        if _invalidate_changed_output(result):
            return
        _p("✍️ 生成回答中…")
        llm_start = now_str()
        t3 = time.time()
        text = ""
        pending = []
        header = ""
        answer_status = None
        last_emit = float("-inf")
        completed = False
        invalidated = False
        gen_usage = new_usage()
        stream = llm.chat_stream(messages, usage_out=gen_usage)
        try:
            for delta in stream:
                if answer_status is None:
                    answer_status, delta = split_answer_status(header + delta)
                    if answer_status is None:
                        header = delta
                        continue
                    header = ""
                if not delta:
                    continue
                pending.append(delta)
                # 每次对外发布前检查权限，短时间内到达的增量合并成一批。
                now = time.monotonic()
                if now - last_emit < 0.08:
                    continue
                if invalidated := _invalidate_changed_output(result):
                    break
                visible = "".join(pending)
                pending.clear()
                text += visible
                last_emit = now
                yield visible
            else:
                if not (invalidated := _invalidate_changed_output(result)):
                    completed = True
                    if answer_status is None:
                        answer_status, tail = split_answer_status(header, final=True)
                        if tail:
                            pending.append(tail)
                    if pending:
                        visible = "".join(pending)
                        text += visible
                        yield visible
        except Exception:
            if not (invalidated := _invalidate_changed_output(result)):
                raise
        finally:
            if close := getattr(stream, "close", None):
                close()
            record_stage_usage(result, "generation", gen_usage)
            invalidated = invalidated or _invalidate_changed_output(result)
            finish_reason = gen_usage.get("finish_reason")
            truncated = finish_reason == "length"
            complete = completed and finish_reason in (None, "stop")
            if not invalidated:
                cleaned = strip_think_tags(text).strip()
                if answer_status:
                    evidence_meta["answer_status"] = answer_status
                corrected = preserve_deadline_wording(cleaned, deadline)
                if corrected != cleaned:
                    evidence_meta["deadline_wording_corrected"] = True
                    cleaned = corrected
                ranks = {source["rank"] for source in result.sources}
                if invalid := sorted(answer_citations(cleaned) - ranks):
                    evidence_meta["invalid_citations"] = invalid
                    cleaned = remove_invalid_citations(cleaned, ranks).rstrip()
                    cleaned += "\n\n> 部分引用无法对应本次来源，已移除。"
                evidence_meta["generation_status"] = (
                    "truncated" if truncated else "complete" if complete and cleaned else "interrupted")
                if truncated:
                    cleaned += (f"\n\n> ⚠️ 回答因达到模型输出上限（{cfg.llm_max_tokens} tokens）被截断，内容不完整。"
                                "可在 .env 调大 `LLM_MAX_TOKENS`，或换更聚焦的问法。")
                elif not complete or not cleaned:
                    cleaned += "\n\n> 回答未完整生成，请重试。"
                result.answer = cleaned.strip()
                _prepend_scope_fallback_notice(result)
            result.elapsed["llm"] = time.time() - t3
            # 五个独立阶段求和；retrieval 保留为旧接口的聚合值，不能再次重复相加。
            result.elapsed["query_understanding"] = float(result.elapsed.get("query_understanding", 0.0) or 0.0)
            result.elapsed["total"] = sum(result.elapsed.get(key, 0.0) for key in
                                           ("query_understanding", "retrieval_only", "rerank", "assembly", "enumeration", "conversation", "llm"))
            usage_line = ""
            if result.usage.get("calls"):
                usage_line = (f"｜消耗：{result.usage['input']} tokens 输入"
                              f"（命中缓存 {result.usage['input_cached']}）"
                              f" / {result.usage['output']} tokens 输出"
                              f"{'（部分为估算）' if result.usage.get('estimated') else ''}")
            if not invalidated:
                trace.append(make_node(
                    "🤖", "LLM 调用（Prompt 组装 + 生成回答）",
                    time_str=llm_start, elapsed=result.elapsed["llm"],
                    summary="把「任务说明书 + 资料附页 + 用户问题」组装成消息发给大模型，"
                            f"大模型基于资料流式生成回答{usage_line}",
                    items=[
                        ("模型", cfg.llm_model or "-"),
                        ("输入 · System Prompt（任务说明书）", messages[0]["content"]),
                        ("输入 · User Prompt（资料附页 + 问题）", messages[-1]["content"]),
                        ("输出 · LLM 生成的回答", result.answer or "（空）"),
                        ("Token 消耗（全部调用合计）",
                         f"输入 {result.usage.get('input', 0)}（命中缓存 {result.usage.get('input_cached', 0)}）"
                         f" / 输出 {result.usage.get('output', 0)}"
                         f"{' · 部分为本地估算' if result.usage.get('estimated') else ''}"
                         if result.usage.get("calls") else "（本次未捕获到用量）"),
                    ] + ([("⚠️ 截断", f"finish_reason=length：输出达到 max_tokens 上限（{cfg.llm_max_tokens}），"
                                       "回答不完整；思考模型的内部思考同样计入该上限")] if truncated else []),
                ))
                trace.append(make_node(
                    "🎁", "后处理", time_str=now_str(),
                    summary="核对引用编号，记录回答状态和总耗时。",
                    items=[
                        ("回答状态", evidence_meta["generation_status"]),
                        ("无效引用", str(evidence_meta.get("invalid_citations", []))),
                        ("输出", f"答案 {len(result.answer or '')} 字 · 来源 {len(result.sources)} 条 · "
                         f"总耗时 {result.elapsed.get('total', 0):.2f} 秒"),
                    ],
                ))
            # 只缓存「完整且非空」的回答：LLM 失败（异常）或用户中断（半截答案）不缓存，
            # 否则之后同样的问题永远命中这份坏结果
            # V3.8：生成期间知识库被改过（纪元变了）→ 这份答案基于旧数据，不能缓存
            # 自然语言范围依赖实时项目关系及方向名称，主数据变更不经过索引纪元。
            if (not invalidated and complete and text.strip() and cache_allowed
                    and not (evidence_meta.get("metadata_scope") or evidence_meta.get("invalid_citations"))):
                _remember_answer(cache_key, result, username)

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
    session_id: str | None = None,
    conversation_state: dict | None = None,
) -> QAResult:
    """完整问答链路（一次性返回完整答案）。use_llm=False 时只做检索（不花钱）。"""
    result, deltas = answer_stream(
        question, top_k=top_k, use_llm=use_llm, filters=filters,
        use_query_understanding=use_query_understanding,
        use_hybrid=use_hybrid, use_rerank=use_rerank,
        history=history, config=config, user_groups=user_groups,
        username=username, session_id=session_id, conversation_state=conversation_state,
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

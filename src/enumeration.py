"""列举问题路由与文档候选构造；不把相关度排序当作完整性证明。"""
from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime, timedelta

from src.retriever import RetrievedItem

ROUTES = {"catalog", "document", "cross_document"}


def enumeration_route(question: str, enumeration: bool, source=None, hint: str = "") -> str:
    if not enumeration:
        return "ordinary"
    # 只对明确盘点库内文件的问法走目录，带内容条件的查询必须语义检索。
    catalog = (re.search(r"文件|文档|资料", question)
               and re.search(r"多少|几份|哪些|列出|列举|所有|全部", question))
    semantic = any(w in question for w in ("关于", "涉及", "包含", "提到", "使用过", "用过", "讲了"))
    # 目录路径只接受能确定转换为元数据条件的问法，未知内容条件不静默忽略。
    remaining = question
    catalog_words = (
        "知识库里", "知识库中", "知识库里面", "知识库", "文档库", "库里", "库中",
        "请", "帮我", "现在", "目前", "到底", "一共", "总共", "共有", "有", "多少", "几份",
        "哪些", "列出", "列举", "所有", "全部", "一下", "的", "文件", "文档", "资料", "份", "个",
        "上传", "入库", "今年", "去年", "上个月", "本月", "这个月", "吗", "呢", "？", "?", "。", "，",
    )
    for token in sorted(catalog_words, key=len, reverse=True):
        remaining = remaining.replace(token, "")
    remaining = re.sub(r"20\d{2}年|\s+", "", remaining)
    if catalog and not semantic and not remaining:
        return "catalog"
    if source:
        return "document"
    if hint in {"document", "cross_document"}:
        return hint
    if any(w in question for w in ("这份", "这篇", "该文档", "该产品", "这款", "简历", "工作经历")):
        return "document"
    return "cross_document"


def catalog_time(question: str, time_range: dict | None = None) -> tuple[str, dict]:
    field = "indexed_at" if any(w in question for w in ("上传", "入库")) else "updated_at"
    dates = time_range or {}
    if not dates:
        today = datetime.now().date()
        year = today.year if "今年" in question else today.year - 1 if "去年" in question else None
        if any(w in question for w in ("上个月", "本月", "这个月")):
            first = today.replace(day=1)
            if "上个月" in question:
                last = first - timedelta(days=1)
            else:
                next_month = (first.replace(year=first.year + 1, month=1) if first.month == 12
                              else first.replace(month=first.month + 1))
                last = next_month - timedelta(days=1)
            first = last.replace(day=1)
            dates = {"from": first.isoformat(), "to": last.isoformat()}
        match = re.search(r"(20\d{2})年", question)
        if match:
            year = int(match.group(1))
        if year and not dates:
            dates = {"from": f"{year}-01-01", "to": f"{year}-12-31"}
    return field, dates


def document_key(item: RetrievedItem) -> tuple:
    m = item.metadata
    return (m.get("document_id") or m.get("path") or m.get("source"), m.get("version"))


def group_documents(items: list[RetrievedItem]) -> dict:
    grouped = OrderedDict()
    for item in items:
        grouped.setdefault(document_key(item), []).append(item)
    return grouped


def document_representatives(items: list[RetrievedItem]) -> list[RetrievedItem]:
    reps = []
    for cards in group_documents(items).values():
        meta = dict(cards[0].metadata)
        # 三个代表性片段均留出空间，避免标题或第一张卡独占精排器输入。
        snippets = []
        for card in cards[:3]:
            snippets.append(f"{str(card.metadata.get('section') or '')[:20]}: {card.text[:55]}")
        text = f"{str(meta.get('title') or meta.get('source') or '')[:65]}\n" + "\n".join(snippets)
        reps.append(RetrievedItem(rank=len(reps) + 1, score=cards[0].score,
                                  text=text, metadata=meta, channels="文档候选"))
    return reps


def expand_documents(store, representatives, original, filters, max_cards: int):
    """沿用权限/状态/版本过滤，按文档轮流排入卡片，避免一篇长文档挤走其他来源。"""
    groups = group_documents(original)
    expanded = []
    truncated = False
    for representative in representatives:
        key = document_key(representative)
        m = representative.metadata
        if not m.get("document_id"):
            expanded.append(list(groups.get(key, []))[:max_cards])
            truncated = True
            continue
        doc_filters = {**filters, "document_id": m["document_id"]}
        if m.get("version"):
            doc_filters["version"] = m["version"]
        rows = store.get_all_cards(filters=doc_filters, limit=max_cards + 1)
        truncated |= len(rows) > max_cards
        # 优先保留原始命中卡片，再按章节序号补充；截断时不会丢掉原本找到的证据。
        candidates = list(groups.get(key, []))
        seen = {c.metadata.get("chunk_id") for c in candidates}
        for row in sorted(rows, key=lambda r: str(r['payload'].get('chunk_id') or '')):
            payload = row['payload']
            if payload.get('chunk_id') in seen:
                continue
            candidates.append(RetrievedItem(rank=0, score=representative.score,
                                            text=payload.get('text', ''),
                                            metadata={k: v for k, v in payload.items() if k != 'text'},
                                            channels='文档展开'))
            seen.add(payload.get('chunk_id'))
        truncated |= len(candidates) > max_cards
        expanded.append(candidates[:max_cards])
    ordered = [cards[i] for i in range(max((len(c) for c in expanded), default=0))
               for cards in expanded if i < len(cards)]
    for i, card in enumerate(ordered, 1):
        card.rank = i
    return ordered, truncated


def coverage_notice(meta: dict) -> str:
    route = meta.get('route')
    if route not in ROUTES:
        return ''
    if route == 'catalog':
        noun = '至少识别' if meta.get('scan_truncated') or meta.get('unknown_date_documents') else '匹配'
        return (f"已入库目录统计：在当前权限和筛选范围内{noun} {meta.get('matched_documents', 0)} 份文件，"
                f"本次展示 {meta.get('processed_documents', 0)} 份。"
                + ('已达到扫描/展示预算或存在日期缺失，清单不完整。' if meta.get('coverage') == 'partial' else ''))
    return (f"检索范围：召回候选涉及 {meta.get('candidate_documents', 0)} 份文档，"
            f"本次采用 {meta.get('processed_documents', 0)} 份文档的资料。"
            + ('已达到处理预算或发生截断，结果可能遗漏。' if meta.get('coverage') == 'partial'
               else '结果基于本次检索，尚不能保证覆盖全库所有相关条目。'))

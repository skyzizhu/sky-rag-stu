"""工作台聚合：知识库存量、入库结果与检索使用统计。"""
from __future__ import annotations

import json
import logging
from collections import Counter
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from time import monotonic

from qdrant_client.http.exceptions import ApiException

from src import auth_db, business_directions, index_epoch, projects
from src.vector_store import VectorStoreError, get_vector_store

_LOG = logging.getLogger(__name__)

CHANNELS = {'web': 'Web', 'mcp': 'MCP', 'other': '其他入口'}
STAGES = {'query_understanding': '查询理解', 'rerank': 'LLM 精排', 'generation': '回答生成',
          'conversation_summary': '上下文摘要', 'enumeration': '列举提取与补查',
          'evaluation': '回答评估', 'other': '其他环节'}


def channel(entry: str) -> str:
    if entry in {'web', 'manage_summary'}:
        return 'web'
    if entry in {'mcp_http', 'mcp_stdio'}:
        return 'mcp'
    return 'other'


def _count(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


@lru_cache(maxsize=1)
def _indexed_projects(store, epoch: str, refresh_window: int) -> tuple[int | None, ...]:
    """Read just enough metadata to count each active document once."""
    documents: dict[str, int | None] = {}
    for payload in store.iter_payloads({"status": "active"}, ["document_id", "project_id"]):
        if document_id := payload.get("document_id"):
            project_id = payload.get("project_id")
            documents.setdefault(document_id, project_id if type(project_id) is int else None)
    return tuple(documents.values())


def knowledge_inventory() -> dict:
    project_rows = projects.list_projects()
    directions = {row["id"]: {"name": row["name"], "projects": []}
                  for row in business_directions.list_directions()}
    no_direction = {"name": "未关联业务方向", "projects": []}
    try:
        document_projects = _indexed_projects(get_vector_store(), index_epoch.current(), int(monotonic() // 30))
        counts = Counter(document_projects)
    except (ApiException, VectorStoreError, OSError):
        _LOG.warning("Could not load dashboard knowledge inventory", exc_info=True)
        document_projects = counts = None

    for row in project_rows:
        group = directions.get(row["direction_id"], no_direction)
        group["projects"].append({"name": row["name"],
                                  "documents": counts[row["id"]] if counts is not None else None})
    groups = list(directions.values())
    if no_direction["projects"]:
        groups.append(no_direction)
    for group in groups:
        group["documents"] = (sum(item["documents"] for item in group["projects"])
                              if counts is not None else None)
    total = len(document_projects) if document_projects is not None else None
    linked = sum(group["documents"] for group in groups) if counts is not None else None
    return {
        "available": total is not None,
        "documents": total,
        "linked_documents": linked,
        "unlinked_documents": total - linked if total is not None else None,
        "project_count": len(project_rows),
        "direction_count": len(directions),
        "groups": groups,
    }


def ingestion_statistics(days: int = 30, *, now: datetime | None = None) -> dict:
    if not 1 <= days <= 90:
        raise ValueError("统计范围需为 1～90 天")
    today = (now or datetime.now()).date()
    start = today - timedelta(days=days - 1)

    def boundary(day):
        midnight = datetime.combine(day, time.min, tzinfo=now.tzinfo if now else None)
        return midnight.astimezone(timezone.utc).isoformat(timespec="milliseconds")

    since, before = boundary(start), boundary(today + timedelta(days=1))
    with closing(auth_db._connect()) as conn, conn:
        conn.execute("BEGIN")
        counts = conn.execute("""
            SELECT COUNT(DISTINCT batch_id) AS batches,
                   COUNT(CASE WHEN outcome = 'ingested' THEN 1 END) AS ingested,
                   COUNT(CASE WHEN outcome = 'skipped' THEN 1 END) AS skipped,
                   COUNT(CASE WHEN outcome = 'failed' THEN 1 END) AS failed,
                   COUNT(CASE WHEN outcome = 'running' THEN 1 END) AS running
            FROM ingest_logs WHERE started_at >= ? AND started_at < ?
        """, (since, before)).fetchone()
        first = conn.execute("SELECT MIN(started_at) FROM ingest_logs").fetchone()[0]
    first_day = datetime.fromisoformat(first).astimezone(now.tzinfo if now else None).date() if first else None
    return {**dict(counts), "first_log_date": str(first_day) if first_day else None,
            "period": {"start": str(start), "end": str(today), "since": since, "before": before}}


def usage_statistics(days: int = 30, *, now: datetime | None = None) -> dict:
    if not 1 <= days <= 90:
        raise ValueError('统计范围需为 1～90 天')
    today = (now or datetime.now()).date()
    start, end = today - timedelta(days=days - 1), today + timedelta(days=1)
    where = ("created_at>=? AND created_at<? AND username<>'' "
             "AND NOT (username='local' AND department='') "
             "AND NOT EXISTS (SELECT 1 FROM admins WHERE admins.username=qa_logs.username)")
    params = [start.isoformat(), end.isoformat()]
    daily = {str(start + timedelta(days=i)): dict(date=str(start + timedelta(days=i)), web=0, mcp=0, other=0)
             for i in range(days)}
    totals, llm_channels, stage_totals = Counter(), Counter(), Counter()
    llm_calls = verified_requests = legacy_requests = legacy_calls = malformed_usage = 0
    token_in = token_cached = token_out = 0
    estimated_requests = inconsistent_usage = 0
    cost_total = 0.0
    stage_input, stage_output = Counter(), Counter()
    llm_daily = {str(start + timedelta(days=i)): dict(date=str(start + timedelta(days=i)),
                                                   calls=0, input=0, input_cached=0, output=0, cost=0.0)
                 for i in range(days)}
    # 模型单价（元/百万 tokens）与「LLM 费用」页同一来源（settings.json，前台模型配置页可改）
    from src.app_settings import PRICE_DEFAULTS, load_app_settings

    settings = load_app_settings()
    price_in = float(settings.get("llm_price_input", PRICE_DEFAULTS["llm_price_input"]))
    price_cached = float(settings.get("llm_price_input_cached",
                                      PRICE_DEFAULTS["llm_price_input_cached"]))
    price_out = float(settings.get("llm_price_output", PRICE_DEFAULTS["llm_price_output"]))
    with auth_db._connect() as conn:
        conn.execute('BEGIN')  # 所有图表使用同一读取快照，避免并发请求造成总数不一致。
        summary = conn.execute("SELECT COUNT(*) AS requests, COUNT(DISTINCT username) AS employees, "
                               "COUNT(DISTINCT NULLIF(department,'')) AS departments "
                               'FROM qa_logs WHERE ' + where, params).fetchone()
        dep_counts = Counter()
        for row in conn.execute('SELECT department, COUNT(*) AS n FROM qa_logs WHERE ' + where + ' GROUP BY department', params):
            dep_counts[row['department'] or '未分配部门'] += row['n']
        # 包含零调用部门；部门历史名称采用发生时的日志快照。
        for row in conn.execute('SELECT name FROM departments'):
            dep_counts.setdefault(row['name'], 0)
        for row in conn.execute('SELECT substr(created_at,1,10) AS day, entry, COUNT(*) AS n '
                                'FROM qa_logs WHERE ' + where + ' GROUP BY day, entry', params):
            key = channel(row['entry'])
            totals[key] += row['n']
            if row['day'] in daily:
                daily[row['day']][key] += row['n']
        # 只读取小字段，分批扫描；不拉正文/回答，也不截断为最近 300 条。
        cursor = conn.execute('SELECT entry, substr(created_at,1,10) AS day, usage FROM qa_logs WHERE ' + where, params)
        while rows := cursor.fetchmany(500):
            for row in rows:
                try:
                    usage = json.loads(row['usage'] or '{}')
                    if not isinstance(usage, dict):
                        raise ValueError('invalid usage')
                except (ValueError, TypeError):
                    malformed_usage += 1
                    usage = {}
                if usage.get('report_version') != 2:
                    legacy_requests += 1
                    legacy_calls += _count(usage.get('calls'))
                    continue
                verified_requests += 1
                calls = _count(usage.get('calls'))
                llm_calls += calls
                llm_channels[channel(row['entry'])] += calls
                # 费用公式复用「LLM 费用」页的三档单价；此处范围仍限定员工 v2 本次用量。
                # 日志未标记云端/本地来源，只能给出当前单价估算，不能代替云端账单。
                input_tok = _count(usage.get('input'))
                reported_cached = _count(usage.get('input_cached'))
                cached_tok = min(input_tok, reported_cached)
                output_tok = _count(usage.get('output'))
                token_in += input_tok
                token_cached += cached_tok
                token_out += output_tok
                if usage.get('estimated'):
                    estimated_requests += 1
                row_cost = (max(0, input_tok - cached_tok) * price_in
                            + cached_tok * price_cached + output_tok * price_out) / 1_000_000
                cost_total += row_cost
                day_row = llm_daily.get(row['day'])
                if day_row is not None:
                    day_row['calls'] += calls
                    day_row['input'] += input_tok
                    day_row['input_cached'] += cached_tok
                    day_row['output'] += output_tok
                    day_row['cost'] += row_cost
                stages = usage.get('stages')
                remaining = calls
                attributed_input = attributed_output = 0
                inconsistent = reported_cached > input_tok
                if isinstance(stages, dict):
                    for key, part in stages.items():
                        if not isinstance(part, dict):
                            continue
                        reported_calls = _count(part.get('calls', 1 if part.get('input') or part.get('output') else 0))
                        amount = min(remaining, reported_calls)
                        inconsistent |= reported_calls > remaining
                        label = 'enumeration' if key.startswith('enumeration_') else key
                        bucket = label if label in STAGES else 'other'
                        stage_totals[bucket] += amount
                        part_input = _count(part.get('input'))
                        part_output = _count(part.get('output'))
                        stage_input[bucket] += part_input
                        stage_output[bucket] += part_output
                        attributed_input += part_input
                        attributed_output += part_output
                        remaining -= amount
                stage_totals['other'] += remaining
                stage_input['other'] += max(0, input_tok - attributed_input)
                stage_output['other'] += max(0, output_tok - attributed_output)
                inconsistent |= attributed_input > input_tok or attributed_output > output_tok
                if inconsistent:
                    inconsistent_usage += 1
    return dict(
        period=dict(days=days, start=str(start), end=str(today)),
        requests=summary['requests'], active_employees=summary['employees'],
        active_departments=summary['departments'],
        channels=[dict(key=key, name=name, requests=totals[key]) for key, name in CHANNELS.items()],
        departments=[dict(name=name, requests=value) for name, value in sorted(dep_counts.items(), key=lambda item: (-item[1], item[0]))],
        daily=list(daily.values()),
        llm=dict(calls=llm_calls, verified_requests=verified_requests, legacy_requests=legacy_requests,
                 legacy_reference_calls=legacy_calls, malformed_usage=malformed_usage,
                 average_calls=round(llm_calls / verified_requests, 2) if verified_requests else 0,
                 tokens=dict(input=token_in, input_cached=token_cached, output=token_out),
                 estimated_requests=estimated_requests, inconsistent_usage=inconsistent_usage,
                 cost=round(cost_total, 8),
                 daily_llm=[dict(date=row['date'], calls=row['calls'], input=row['input'],
                                 input_cached=row['input_cached'], output=row['output'], cost=round(row['cost'], 8))
                            for row in llm_daily.values()],
                 price=dict(input=price_in, input_cached=price_cached, output=price_out),
                 channels=[dict(key=key, name=name, calls=llm_channels[key]) for key, name in CHANNELS.items()],
                 stages=[dict(key=key, name=name, calls=stage_totals[key],
                              input=stage_input.get(key, 0), output=stage_output.get(key, 0))
                         for key, name in STAGES.items()]),
    )

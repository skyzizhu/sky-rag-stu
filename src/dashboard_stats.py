"""工作台聚合：读取完整时间范围的检索日志，不受日志列表分页上限影响。"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta

from src import auth_db

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
        cursor = conn.execute('SELECT entry, usage FROM qa_logs WHERE ' + where, params)
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
                stages = usage.get('stages')
                remaining = calls
                if isinstance(stages, dict):
                    for key, part in stages.items():
                        if not isinstance(part, dict):
                            continue
                        amount = min(remaining, _count(part.get('calls', 1 if part.get('input') or part.get('output') else 0)))
                        label = 'enumeration' if key.startswith('enumeration_') else key
                        stage_totals[label if label in STAGES else 'other'] += amount
                        remaining -= amount
                stage_totals['other'] += remaining
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
                 channels=[dict(key=key, name=name, calls=llm_channels[key]) for key, name in CHANNELS.items()],
                 stages=[dict(key=key, name=name, calls=stage_totals[key]) for key, name in STAGES.items()]),
    )

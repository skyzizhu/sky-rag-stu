"""本次检索的可核验 LLM 用量，避免重放缓存里上一次回答的消耗。"""
from __future__ import annotations

from copy import deepcopy


def _number(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def usage_for_log(result, evaluation: dict | None = None) -> dict:
    override = getattr(result, 'request_usage', None)
    usage = deepcopy(override if isinstance(override, dict) else (getattr(result, 'usage', {}) or {}))
    if not isinstance(usage, dict):
        usage = {}
    raw_stages = usage.get('stages') or {}
    stages = {key: dict(value) for key, value in (raw_stages if isinstance(raw_stages, dict) else {}).items()
              if isinstance(value, dict)}
    if not stages and (usage.get('calls') or usage.get('input') or usage.get('output')):
        stages['other'] = {key: value for key, value in usage.items() if key != 'stages'}
    evaluation_usage = (evaluation or {}).get('usage')
    if isinstance(evaluation_usage, dict) and (evaluation_usage.get('input') or evaluation_usage.get('output')):
        stages['evaluation'] = dict(evaluation_usage)
    total = dict(input=0, input_cached=0, output=0, calls=0, estimated=False)
    for part in stages.values():
        for key in ('input', 'input_cached', 'output'):
            total[key] += _number(part.get(key, 0))
        part['calls'] = _number(part.get('calls', 1 if part.get('input') or part.get('output') else 0))
        total['calls'] += part['calls']
        total['estimated'] |= bool(part.get('estimated'))
    total.update(stages=stages, report_version=2, cache_hit=bool(getattr(result, 'cache_hit', False)))
    return total

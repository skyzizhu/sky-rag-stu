"""Post-delivery observations live outside immutable version snapshots."""
import datetime
import hashlib
import json
import pathlib
import re


def fingerprint(content):
    return hashlib.sha256(json.dumps({str(p.relative_to(content)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(content.rglob('*')) if p.is_file() and p.name not in ('spec-data.js', '.DS_Store')}, sort_keys=True).encode()).hexdigest()


def check_payload(value):
    if not isinstance(value, dict):
        raise ValueError('结果必须为对象')
    kind = value.get('kind')
    common = {'kind', 'version', 'actor', 'observed_at', 'evidence', 'notes', 'follow_up', 'supersedes'}
    fields = {'execution': {'requirement_id', 'acceptance_id', 'scope', 'result', 'environment', 'build'},
              'measurement': {'goal', 'metric', 'baseline', 'target', 'actual', 'window', 'data_source', 'decision'}}
    if kind not in fields or set(value) - common - fields[kind]:
        raise ValueError('结果类型或字段无效')
    required = {'version', 'actor', 'observed_at', 'notes'} | fields[kind]
    for key in required:
        if not isinstance(value.get(key), str) or not value[key].strip() or len(value[key]) > 4000:
            raise ValueError(f'{key} 需要 1～4000 字文本')
    try:
        date = datetime.datetime.fromisoformat(value['observed_at'].replace('Z', '+00:00'))
        if date.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise ValueError('observed_at 需要带时区的 ISO 时间')
    evidence = value.get('evidence')
    if not isinstance(evidence, list) or len(evidence) > 20 or any(not isinstance(e, str) or not e.strip() or len(e) > 4000 for e in evidence):
        raise ValueError('evidence 需要最多 20 条证据位置/说明')
    if kind == 'execution':
        if value['scope'] not in ('prototype', 'product') or value['result'] not in ('passed', 'failed', 'blocked', 'not_run'):
            raise ValueError('验收范围或结果无效')
        if value['result'] != 'not_run' and not evidence:
            raise ValueError('已执行/阻塞结果必须有证据，不能用用例已生成代替执行')
    elif not evidence or value['decision'] not in ('met', 'missed', 'inconclusive'):
        raise ValueError('指标观测需要证据及 met/missed/inconclusive 结论')
    follow = value.get('follow_up')
    if follow is not None and (not isinstance(follow, dict) or set(follow) != {'target_version', 'owner', 'action'} or any(not isinstance(v, str) or not v.strip() or len(v) > 4000 for v in follow.values())):
        raise ValueError('跟进需要 target_version、owner、action')
    if value.get('supersedes') is not None and not re.fullmatch('[a-f0-9]{32}', str(value['supersedes'])):
        raise ValueError('更正记录 ID 无效')
    return value


def load_records(root):
    records = []
    directory = root / 'outcomes'
    if not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError('outcomes 路径越界')
    for file in sorted(directory.glob('*.json')):
        if not file.resolve().is_relative_to(directory.resolve()) or not re.fullmatch('[a-f0-9]{32}', file.stem):
            raise ValueError('结果文件路径或 ID 无效')
        record = json.loads(file.read_text(encoding='utf-8'))
        if record.get('id') != file.stem or record.get('schema_version') != 1:
            raise ValueError('结果记录头无效')
        check_payload(record['payload'])
        if not re.fullmatch('[a-f0-9]{64}', str(record.get('baseline'))) or not isinstance(record.get('context'), dict):
            raise ValueError('结果基线或上下文缺失')
        records.append(record)
    return sorted(records, key=lambda r: (r['recorded_at'], r['id']))


def report(root, project):
    records = load_records(root)
    versions = {v['id']: v for v in project['versions']}
    baselines = {v: fingerprint(root / 'versions' / v / 'content') for v in versions}
    superseded = {r['payload'].get('supersedes') for r in records}
    known, replaced = {}, set()
    for record in records:
        vid = record['payload']['version']
        if record.get('project') != project['id'] or vid not in versions:
            raise ValueError('结果记录项目/版本不存在或不匹配')
        previous = record['payload'].get('supersedes')
        if previous:
            old = known.get(previous)
            identity = ('kind', 'version', 'requirement_id', 'acceptance_id', 'scope', 'goal', 'metric')
            if not old or previous in replaced or any(old['payload'].get(k) != record['payload'].get(k) for k in identity):
                raise ValueError('更正链无效或对象不匹配')
            replaced.add(previous)
        known[record['id']] = record
        record['stale'] = record['baseline'] != baselines[vid]
        record['superseded'] = record['id'] in superseded
    return records

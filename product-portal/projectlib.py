"""Project/version storage and deterministic validation. Standard library only."""
import datetime
import hashlib
import json
import pathlib
import re
import outcomelib
import prdlib
import attachmentlib
import materiallib
import notelib
from html.parser import HTMLParser

VERSION_STATES = {'planning', 'frozen', 'developing', 'released'}
# 随 skill 模板发布一并提升；编译进 spec-data 供原型工具栏展示，用于识别模板漂移。
TEMPLATE_VERSION = '1.1.0'
FORMS = {'web', 'desktop', 'mobile', 'h5', 'miniapp', 'tv'}
ID = re.compile(r'^[a-z0-9]+(?:[-.][a-z0-9]+)*$')

PROPERTY_LABELS = {'text': '控件文案', 'placeholder': '占位提示', 'defaultValue': '默认值', 'maxLength': '最大长度', 'required': '必填', 'options': '选项', 'width': '宽度', 'min': '最小值', 'max': '最大值', 'step': '步进', 'unit': '单位', 'pattern': '格式校验', 'accept': '可接受文件类型'}


def property_errors(it, element):
    props = it.get('properties', {})
    if not isinstance(props, dict):
        return ['properties 必须为对象']
    errors = []
    tag = element.get('tag', '')
    for key, value in props.items():
        if key not in PROPERTY_LABELS:
            errors.append('不支持属性：' + key)
        elif key == 'required':
            if type(value) is not bool or tag not in {'input', 'textarea', 'select'}:
                errors.append('必填属性仅支持表单控件和布尔值')
        elif key == 'maxLength':
            if type(value) is not int or not 1 <= value <= 10000 or tag not in {'input', 'textarea'} or (tag == 'input' and element.get('type', 'text') not in {'text', 'search', 'email', 'url', 'tel', 'password'}):
                errors.append('最大长度仅支持文本输入，范围 1～10000')
        elif key == 'options':
            if tag != 'select' or not isinstance(value, list) or not 1 <= len(value) <= 100 or any(not isinstance(o, dict) or set(o) != {'value', 'label'} or any(not isinstance(v, str) or len(v) > 500 for v in o.values()) for o in value):
                errors.append('选项需要 select 和 1～100 个 value/label 对象')
            elif len({o['value'] for o in value}) != len(value):
                errors.append('选项 value 不可重复')
        elif key == 'width':
            if value not in ('auto', 'compact', 'full'):
                errors.append('宽度仅支持 auto/compact/full')
        elif key in {'min', 'max', 'step'}:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or tag != 'input' or element.get('type', 'text') not in {'number', 'range', 'date', 'datetime-local', 'time', 'month', 'week'}:
                errors.append(key + ' 仅支持数值/日期类 input 的数字值')
        elif key == 'unit':
            if not isinstance(value, str) or not 1 <= len(value) <= 20:
                errors.append('单位需要 1～20 字文本')
        elif key == 'pattern':
            if not isinstance(value, str) or not 1 <= len(value) <= 200 or tag != 'input':
                errors.append('格式校验需要 200 字内文本并适用于 input')
        elif key == 'accept':
            if not isinstance(value, str) or not 1 <= len(value) <= 200 or element.get('type') != 'file':
                errors.append('可接受文件类型仅支持 file input')
        elif not isinstance(value, str) or len(value) > 2000:
            errors.append(key + ' 必须为 2000 字以内文本')
        elif key == 'text' and (tag in {'input', 'textarea', 'select', 'script', 'style'} or element.get('_children')):
            errors.append('文案只支持无子元素的文字控件，避免破坏内部图标/结构')
        elif key in {'placeholder', 'defaultValue'} and tag not in ({'input', 'textarea'} if key == 'placeholder' else {'input', 'textarea', 'select'}):
            errors.append(key + ' 不适用于此元素')
        elif key == 'defaultValue' and tag == 'input' and element.get('type', 'text') in {'file', 'checkbox', 'radio', 'submit', 'button', 'reset', 'image'}:
            errors.append('此 input 类型不支持默认值编辑')
    if tag == 'select' and 'options' in props and 'defaultValue' in props and not errors and props['defaultValue'] not in {o['value'] for o in props['options']}:
        errors.append('默认值必须存在于选项中')
    return errors


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def safe(root, relative):
    root = root.resolve()
    path = (root / relative).resolve()
    if pathlib.Path(relative).is_absolute() or not path.is_relative_to(root):
        raise ValueError(f'路径越界：{relative}')
    return path


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()
            and p.name not in {'.snapshot.json', '.baseline.json', '.requirements-baseline.json', '.DS_Store'}
            and '__pycache__' not in p.parts}


def source_hashes(version):
    return {k: v for k, v in hashes(version).items()
            if k != 'content/prototype/assets/spec-data.js'}


def build_inputs(root):
    return {'project': hashlib.sha256((root / 'project.json').read_bytes()).hexdigest(),
            'outcomes': hashes(root / 'outcomes'),
            'attachments': hashes(root / 'attachments'),
            'versions': {v['id']: hashes(safe(root, 'versions/' + v['id'])) for v in read(root / 'project.json')['versions']}}


def check_build(root):
    stamp = root / 'site/js/build-state.json'
    data = root / 'site/js/data.js'
    if not stamp.is_file() or not data.is_file():
        return ['缺少构建产物，请运行 build.py']
    expected = read(stamp)
    if expected.get('inputs') != build_inputs(root) or expected.get('output') != hashlib.sha256(data.read_bytes()).hexdigest():
        return ['构建产物已过期或被修改，请运行 build.py']
    return []


def changes(before, after):
    return {'added': sorted(after.keys() - before.keys()),
            'removed': sorted(before.keys() - after.keys()),
            'modified': sorted(k for k in before.keys() & after.keys() if before[k] != after[k])}


def pages(manifest):
    return [p for m in manifest['modules'] for p in m.get('pages', [])]


def spec_markdown(spec, outcomes=None):
    out = ['# 产品需求文档', '按业务需求组织；编号用于追踪和精确修改，不代替需求说明。', '## 功能清单',
           '由 spec 自动生成，索引与详情同源；优先级缺失标待定。', '',
           '| 编号 | 功能 | 优先级 | 来源 | 状态 |', '|---|---|---|---|---|']
    for r in spec.get('requirements', []):
        out += ['| ' + r['id'] + ' | ' + r['title'] + ' | ' + (r.get('priority') or '待定') + ' | '
                + ('AI 推断' if r['source'] == 'ai-inferred' else '原始需求') + ' | '
                + ('已确认' if r['status'] == 'confirmed' else '待评审') + ' |']
    out += ['## 页面与功能范围']
    requirements = {r['id']: r for r in spec.get('requirements', [])}
    latest_execution = {}
    for record in outcomes or []:
        payload = record.get('payload', {}) if isinstance(record, dict) else {}
        if payload.get('kind') != 'execution' or record.get('superseded') or not payload.get('acceptance_id'):
            continue
        current = latest_execution.get(payload['acceptance_id'])
        if current is None or str(record.get('recorded_at', '')) > str(current.get('recorded_at', '')):
            latest_execution[payload['acceptance_id']] = record
    page_names = {p['id']: p['title'] for p in spec.get('pages', [])}
    category_names = {'validation':'输入校验','permission':'权限规则','state':'状态规则','data':'数据口径','feedback':'操作反馈'}
    actions = {'navigate':'页面跳转','dialog':'打开弹框','close':'关闭弹框','toggle':'显示切换','input':'输入','submit':'提交','custom':'业务操作','inspect':'只读展示'}
    for page in spec.get('pages', []):
        out += [f"### {page['title']}", page.get('purpose', ''), '功能：'+'、'.join(requirements.get(rid,{}).get('title',rid) for rid in page.get('requirement_ids',[]))]
    for r in spec.get('requirements', []):
        out += [f"## {r['title']}", f"追踪编号：{r['id']}", r.get('description', ''),
                f"来源：{'AI 推断' if r['source']=='ai-inferred' else '原始需求'} · 状态：{'已确认' if r['status']=='confirmed' else '待确认'} · 阻塞开发：{'是' if r.get('blocking') else '否'}"]
        if r['source'] == 'ai-inferred':
            out += ['> 🤖 **AI 推断**：本条需求包含待评审的补全内容。']
        if r['status'] == 'pending':
            out += ['> ⚠️ **待确认**：请评审本条规则与验收口径。']
        for name, value in r.get('rules', {}).items():
            out += [f'### {name}', str(value)]
        for rule in r.get('rule_details', []):
            out += [f"### {category_names.get(rule.get('category'),rule.get('category','业务规则'))}", rule['statement'],
                    f"规则编号：{rule['id']} · 来源：{'AI 推断' if rule.get('source')=='ai-inferred' else '原始需求'} · {'已确认' if rule.get('status')=='confirmed' else '待确认'}"]
            for example in rule.get('examples', []):
                out += [f"- 实例：{example['input']} → 预期：{example['expected']}"]
            out += ['关联验收：' + '、'.join(rule.get('acceptance_ids', []))]
        out += ['### 验收标准']
        for ac in r.get('acceptance', []):
            out += [f"- 前提：{ac['given']}\n  - 操作：{ac['when']}\n  - 预期：{ac['then']}\n  - 验收编号：{ac['id']}"]
            record = latest_execution.get(ac['id'])
            if record is not None:
                payload = record['payload']
                result_names = {'passed': '通过', 'failed': '失败', 'blocked': '阻塞', 'not_run': '未执行'}
                scope_name = '原型验证' if payload.get('scope') == 'prototype' else '产品验收'
                stale = ' · 基线已变化，需复核' if record.get('stale') else ''
                out += [f"  - 验证记录：{result_names.get(payload.get('result'), payload.get('result'))}（{scope_name}）· {str(payload.get('observed_at', ''))[:10]}{stale}；取自台账，有记录不代表当前仍通过"]
        controls=[i for i in spec.get('interactions',[]) if r['id'] in i.get('requirement_ids',[])]
        if controls: out += ['### 页面控件与显示说明']
        for it in controls:
            out += [f"- {page_names.get(it['page'],it['page'])}：{it['description']}\n  - 动作：{actions.get(it['action'],it['action'])}" + (f" → {page_names.get(it['target'],it['target'])}" if it.get('target') else '')]
            for key,value in it.get('properties',{}).items():
                out += [f"  - {PROPERTY_LABELS.get(key,key)}：{json.dumps(value,ensure_ascii=False)}"]
            out += [f"  - 技术定位：{it['id']} · {it['selector']}"]
    return '\n\n'.join(out)


class HTMLCheck(HTMLParser):
    def __init__(self):
        super().__init__()
        self.errors, self.ids, self.refs, self.controls = [], set(), [], []
        self.page_id = None
        self.elements = {}
        self.stack = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for _, ancestor in self.stack:
            if ancestor in self.elements:
                self.elements[ancestor]['_children'] = True
        if a.get('id'):
            self.elements[a['id']] = dict(a, tag=tag)
        if tag not in {'input', 'img', 'br', 'hr', 'meta', 'link', 'source', 'area', 'base', 'col', 'embed', 'param', 'track', 'wbr'}:
            self.stack.append((tag, a.get('id')))
        if tag == 'body':
            self.page_id = a.get('data-page-id')
        if tag == 'style' or 'style' in a or any(k.startswith('on') for k in a):
            self.errors.append('存在内联样式或事件处理器')
        if tag == 'script' and not a.get('src'):
            self.errors.append('存在内联脚本')
        if a.get('id'):
            if a['id'] in self.ids:
                self.errors.append(f"重复元素 ID：{a['id']}")
            self.ids.add(a['id'])
        for attr in ('src', 'href'):
            if a.get(attr):
                self.refs.append(a[attr])
        if tag in {'button', 'input', 'select', 'textarea', 'a'}:
            self.controls.append(a)
        elif 'data-business-value' in a:
            self.controls.append(a)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def validate_replay_cases(spec, prototype_ids):
    errors, seen = [], set()
    cases = spec.get('replay_cases', [])
    if not isinstance(cases, list) or len(cases) > 30:
        return ['replay_cases 必须是最多 30 项的列表']
    reqs = {r['id'] for r in spec.get('requirements', [])}
    for case in cases:
        if not isinstance(case, dict):
            errors.append('复现场景必须是对象')
            continue
        cid = case.get('id')
        if not isinstance(cid, str) or not ID.fullmatch(cid) or cid in seen:
            errors.append('复现场景 ID 无效或重复')
        else:
            seen.add(cid)
        for field in ('title', 'role'):
            if not isinstance(case.get(field), str) or not case[field].strip() or len(case[field]) > 100:
                errors.append(f'{cid}: 复现 {field} 必须为 1～100 字')
        if case.get('scenario') not in ('normal', 'empty', 'loading', 'error', 'forbidden'):
            errors.append(f'{cid}: 复现场景状态无效')
        for field, allowed in (('pages', prototype_ids), ('requirement_ids', reqs)):
            refs = case.get(field)
            if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in allowed for ref in refs):
                errors.append(f'{cid}: 复现 {field} 关联无效')
        if case.get('source') not in ('origin', 'ai-inferred') or case.get('status') not in ('pending', 'confirmed'):
            errors.append(f'{cid}: 复现需标记来源与确认状态')
        state = case.get('state')
        try:
            encoded = json.dumps(state, ensure_ascii=False, allow_nan=False)
            if not isinstance(state, dict) or len(encoded.encode('utf-8')) > 32768 or any(k in state for k in ('role', 'scenario')):
                raise ValueError()
        except (ValueError, TypeError):
            errors.append(f'{cid}: 复现 state 必须是 32KB 内的 JSON 对象，角色和场景由独立字段声明')
    return errors


BODY_LAYOUT = re.compile(r'(^|[},])\s*body\b[^{},]*\{[^}]*display\s*:\s*(?:flex|grid)', re.I | re.S)


def validate(root):
    errors, warnings = [], []
    project = read(root / 'project.json')
    if project.get('schema_version') != 2 or not project.get('id') or not project.get('name'):
        errors.append('project.json 需要 schema_version=2、id、name')
    versions = project.get('versions', [])
    ids = [v['id'] for v in versions]
    if not ids or len(ids) != len(set(ids)) or project.get('current_version') not in ids:
        errors.append('版本列表为空、重复或 current_version 无效')
    for version in versions:
        vid = version['id']
        if version.get('handoff_parent'):
            parent = next((v for v in versions if v['id'] == version['handoff_parent']), None)
            if not parent or parent.get('handoff_parent') or vid != parent['id'] + '.' + str(version.get('revision')):
                errors.append(f'{vid}: 交接修订归属无效')
        if version.get('current_handoff'):
            handoff = next((v for v in versions if v['id'] == version['current_handoff']), None)
            if not handoff or handoff.get('handoff_parent') != vid:
                errors.append(f'{vid}: 当前交接指针无效')
        if not ID.fullmatch(vid) or version.get('status') not in VERSION_STATES:
            errors.append(f'{vid}: 版本 ID 或状态无效')
            continue
        vr = safe(root, 'versions/' + vid)
        try:
            lock = vr / '.snapshot.json'
            if version['status'] != 'planning':
                if not lock.exists() or read(lock) != hashes(vr):
                    errors.append(f'{vid}: 冻结快照缺失或历史文件已改变')
            manifest = read(vr / 'content/manifest.json')
            prdlib.compile_sections(vr / 'content', read(vr / 'content/spec.json') if (vr / 'content/spec.json').exists() else None, '')
            if manifest.get('product', {}).get('form') not in FORMS:
                errors.append(f'{vid}: 产品形态无效')
            ps = pages(manifest)
            mids = [m['id'] for m in manifest['modules']]
            pids = [p['id'] for p in ps]
            if any(not isinstance(i, str) or not ID.fullmatch(i) for i in mids + pids):
                errors.append(f'{vid}: 模块及页面 ID 必须为稳定的小写标识')
            has_spec = (vr / 'content/spec.json').exists()
            if len(mids) != len(set(mids)) or (has_spec and len(pids) != len(set(pids))):
                errors.append(f'{vid}: 模块或页面 ID 重复（页面 ID 应在版本内唯一）')
            for module in manifest['modules']:
                local_ids = [p['id'] for p in module.get('pages', [])]
                if len(local_ids) != len(set(local_ids)):
                    errors.append(f'{vid}: 同模块页面 ID 重复')
            parsed = {}
            for p in ps:
                if p.get('source') not in {'origin', 'ai-inferred'} or p.get('status') not in {'pending', 'confirmed'}:
                    errors.append(f"{vid}/{p['id']}: 缺少合法来源或确认状态")
                if p.get('type') not in {'markdown', 'mermaid', 'prototype', 'html-embed', 'spec'}:
                    errors.append(f"{vid}/{p['id']}: 页面类型无效")
                if p.get('type') == 'spec':
                    continue
                file = safe(vr / 'content', p['file'])
                if not file.is_file():
                    errors.append(f'{vid}: 缺失 {p["file"]}')
                    continue
                if p['type'] in {'prototype', 'html-embed'}:
                    h = HTMLCheck()
                    h.feed(file.read_text(encoding='utf-8'))
                    parsed[p['id']] = h
                    errors.extend(f'{vid}/{p["id"]}: {e}' for e in h.errors)
                    for ref in h.refs:
                        if re.match(r'^(https?:|mailto:|tel:|#)', ref):
                            continue
                        target = safe(vr, str((file.parent / ref.split('#')[0].split('?')[0]).relative_to(vr)))
                        if not target.is_file():
                            errors.append(f'{vid}/{p["id"]}: 资源或跳转不存在 {ref}')
            specfile = vr / 'content/spec.json'
            if not specfile.exists():
                warnings.append(f'{vid}: 旧版物料尚未建立结构化需求关联')
                continue
            spec = read(specfile)
            notelib.validate(spec.get('review_notes',{}),spec)
            reqs = {r['id']: r for r in spec.get('requirements', [])}
            if len(reqs) != len(spec.get('requirements', [])):
                errors.append(f'{vid}: 需求 ID 重复')
            acids = set()
            ruleids = set()
            for rid, r in reqs.items():
                if not re.fullmatch(r'[A-Za-z0-9_-]+', rid) or not r.get('title'):
                    errors.append(f'{vid}/{rid}: 需求 ID 或标题无效')
                if r.get('source') not in {'origin', 'ai-inferred'} or r.get('status') not in {'pending', 'confirmed'}:
                    errors.append(f'{vid}/{rid}: 来源或状态无效')
                if 'priority' in r and r.get('priority') not in {'P0', 'P1', 'P2', 'P3'}:
                    errors.append(f'{vid}/{rid}: priority 仅允许 P0～P3 或省略')
                if not r.get('acceptance'):
                    errors.append(f'{vid}/{rid}: 缺少验收口径')
                delivery = r.get('delivery', {})
                if not isinstance(delivery, dict):
                    errors.append(f'{vid}/{rid}: delivery 必须为对象')
                else:
                    roles, dependencies = delivery.get('roles', []), delivery.get('dependencies', [])
                    if not isinstance(roles, list) or any(role not in ('frontend', 'backend', 'qa') for role in roles):
                        errors.append(f'{vid}/{rid}: 交接分工无效')
                    if not isinstance(dependencies, list) or any(not isinstance(dep, str) or not dep.strip() for dep in dependencies):
                        errors.append(f'{vid}/{rid}: 交接依赖需要非空文字列表')
                for a in r.get('acceptance', []):
                    if not all(a.get(k) for k in ('id', 'given', 'when', 'then')) or a['id'] in acids:
                        errors.append(f'{vid}/{rid}: 验收 ID 重复或 Given/When/Then 缺失')
                    acids.add(a['id'])
                details = r.get('rule_details', [])
                if not isinstance(details, list):
                    errors.append(f'{vid}/{rid}: rule_details 必须为数组')
                    details = []
                local_ac = {a['id'] for a in r.get('acceptance', [])}
                for rule in details:
                    if not isinstance(rule, dict) or not all(isinstance(rule.get(k), str) and rule[k].strip() for k in ('id', 'category', 'statement')):
                        errors.append(f'{vid}/{rid}: 规则缺少合法 id/category/statement')
                        continue
                    if rule['id'] in ruleids:
                        errors.append(f'{vid}/{rid}: 规则 ID 重复')
                    ruleids.add(rule['id'])
                    if rule.get('source') not in {'origin', 'ai-inferred'} or rule.get('status') not in {'pending', 'confirmed'}:
                        errors.append(f'{vid}/{rid}: 规则来源或状态无效')
                    examples = rule.get('examples', [])
                    if not isinstance(examples, list) or any(not isinstance(e, dict) or not all(isinstance(e.get(k), str) and e[k].strip() for k in ('input', 'expected')) for e in examples):
                        errors.append(f'{vid}/{rid}: 规则实例格式无效')
                    refs = rule.get('acceptance_ids', [])
                    if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in local_ac for ref in refs):
                        errors.append(f'{vid}/{rid}: 规则验收关联无效')
                    if rule.get('status') == 'pending' and r.get('blocking') and version['status'] != 'planning':
                        errors.append(f'{vid}/{rid}: 阻塞规则尚未确认，不得冻结')
                if r.get('status') == 'pending' and r.get('blocking'):
                    warnings.append(f'{vid}/{rid}: 阻塞开发，尚待确认')
                    if version['status'] != 'planning':
                        errors.append(f'{vid}/{rid}: 阻塞项确认前不得冻结')
            mappings = {p['id']: p for p in spec.get('pages', [])}
            errors.extend(f'{vid}: {message}' for message in validate_replay_cases(spec, {p['id'] for p in ps if p['type'] == 'prototype'}))
            if len(mappings) != len(spec.get('pages', [])):
                errors.append(f'{vid}: 需求页面映射重复')
            for pid, mapping in mappings.items():
                if pid not in pids:
                    errors.append(f'{vid}/{pid}: 需求映射指向不存在的页面')
                if not mapping.get('requirement_ids'):
                    errors.append(f'{vid}/{pid}: 页面没有关联需求')
                for rid in mapping.get('requirement_ids', []):
                    if rid not in reqs:
                        errors.append(f'{vid}/{pid}: 不存在需求 {rid}')
            covered, iids = {}, set()
            for it in spec.get('interactions', []):
                pid, selector = it['page'], it['selector']
                if it['id'] in iids:
                    errors.append(f'{vid}: 交互 ID 重复 {it["id"]}')
                iids.add(it['id'])
                if pid not in parsed or not selector.startswith('#') or selector[1:] not in parsed[pid].ids:
                    errors.append(f'{vid}/{it["id"]}: 元素不存在或 selector 不是稳定 #id')
                covered.setdefault(pid, set()).add(selector[1:])
                if not it.get('description') or not it.get('requirement_ids'):
                    errors.append(f'{vid}/{it["id"]}: 缺少行为说明或关联需求')
                for rid in it.get('requirement_ids', []):
                    if rid not in reqs or rid not in mappings.get(pid, {}).get('requirement_ids', []):
                        errors.append(f'{vid}/{it["id"]}: 需求未关联至本页 {rid}')
                action, target = it.get('action'), it.get('target')
                errors.extend(f'{vid}/{it["id"]}: {error}' for error in property_errors(it, parsed.get(pid, HTMLCheck()).elements.get(selector[1:], {})))
                refs = it.get('rule_ids')
                if refs is not None:
                    allowed = {rule['id'] for rid in it.get('requirement_ids', []) for rule in reqs.get(rid, {}).get('rule_details', []) if isinstance(rule, dict) and isinstance(rule.get('id'), str)}
                    if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in allowed for ref in refs):
                        errors.append(f'{vid}/{it["id"]}: 控件规则关联无效')
                if action not in {'navigate', 'dialog', 'close', 'input', 'submit', 'toggle', 'custom', 'inspect'}:
                    errors.append(f'{vid}/{it["id"]}: 未知动作')
                if action == 'navigate' and target not in mappings:
                    errors.append(f'{vid}/{it["id"]}: 跳转页面不存在')
                if action in {'dialog', 'close', 'toggle'} and target not in parsed.get(pid, HTMLCheck()).ids:
                    errors.append(f'{vid}/{it["id"]}: 弹框或面板不存在')
            for p in ps:
                if p['type'] != 'prototype':
                    continue
                pid = p['id']
                if pid not in mappings:
                    errors.append(f'{vid}/{pid}: 原型未关联本页需求')
                html = parsed.get(pid, HTMLCheck())
                if spec.get('replay_cases'):
                    runtime = vr / 'content/prototype/assets/prototype-runtime.js'
                    if not runtime.exists() or 'PS_REPLAY_V1' not in runtime.read_text(encoding='utf-8'):
                        errors.append(f'{vid}/{pid}: 复现场景需要升级共享 runtime（冻结资源不能原地升级）')
                if any(i.get('properties') for i in spec.get('interactions', []) if i.get('page') == pid):
                    runtime = vr / 'content/prototype/assets/prototype-runtime.js'
                    if not runtime.exists() or 'PS_PROPERTY_RUNTIME_V1' not in runtime.read_text(encoding='utf-8'):
                        errors.append(f'{vid}/{pid}: 原型属性需要升级共享 runtime')
                if html.page_id != pid:
                    errors.append(f'{vid}/{pid}: body data-page-id 与 manifest 不一致')
                for required in ('assets/spec-data.js', 'assets/annotation-store.js', 'assets/prototype-runtime.js', 'assets/prototype-runtime.css'):
                    if required not in html.refs:
                        errors.append(f'{vid}/{pid}: 原型缺少共享需求面板资源 {required}')
                for ref in html.refs:
                    if not ref.endswith('.css') or 'prototype-runtime.css' in ref or re.match(r'^(https?:)?//', ref):
                        continue
                    css_path = safe(vr / 'content/prototype', ref) if ref.startswith('assets/') else None
                    if css_path is None or not css_path.is_file():
                        continue
                    css_text = re.sub(r'/\*.*?\*/', '', css_path.read_text(encoding='utf-8'), flags=re.S)
                    if BODY_LAYOUT.search(css_text):
                        warnings.append(f'{vid}/{pid}: 页面 CSS 将 body 设为 flex/grid，会破坏运行时工具栏布局；画布居中请用容器 margin auto（prototype.md）')
                for control in parsed.get(pid, HTMLCheck()).controls:
                    if control.get('data-out-of-scope'):
                        if 'disabled' not in control and control.get('aria-disabled') != 'true':
                            errors.append(f'{vid}/{pid}: 范围外控件必须禁用')
                    elif control.get('id') not in covered.get(pid, set()):
                        errors.append(f'{vid}/{pid}: 可操作控件缺少交互说明 {control.get("id", control)}')
        except (ValueError, KeyError, TypeError, OSError) as e:
            errors.append(f'{vid}: {e}')
    try:
        outcomelib.report(root, project)
        catalog = attachmentlib.load(root)
        for meta in catalog.values():
            if meta['project'] != project['id'] or meta['version'] not in ids:
                raise ValueError('附件归属项目/版本不存在')
        for v in versions:
            attachmentlib.for_version(root, project, v['id'], catalog)
    except (ValueError, KeyError, TypeError, OSError) as e:
        errors.append('验收/复盘台账或附件：' + str(e))
    return errors, warnings


def compile_project(root):
    project = read(root / 'project.json')
    bundle = {'project': project, 'versions': {}, 'outcomes': outcomelib.report(root, project)}
    catalog = attachmentlib.load(root)
    for v in project['versions']:
        vr = safe(root, 'versions/' + v['id'])
        manifest = read(vr / 'content/manifest.json')
        spec = read(vr / 'content/spec.json') if (vr / 'content/spec.json').exists() else None
        if spec: notelib.validate(spec.get('review_notes',{}),spec)
        version_outcomes = [record for record in bundle['outcomes']
                            if isinstance(record, dict) and record.get('payload', {}).get('version') == v['id']]
        files = {}
        for p in pages(manifest):
            if p['type'] in {'markdown', 'mermaid'}:
                files[p['file']] = safe(vr / 'content', p['file']).read_text(encoding='utf-8')
            elif p['type'] == 'spec':
                files[p['id']] = spec_markdown(spec or {}, version_outcomes)
        bundle['versions'][v['id']] = {'manifest': manifest, 'spec': spec, 'files': files,
                                      'content_hashes': {k: h for k, h in hashes(vr / 'content').items() if k != 'prototype/assets/spec-data.js'}}
        sections = prdlib.compile_sections(vr / 'content', spec, spec_markdown(spec or {}, version_outcomes))
        attachments = attachmentlib.for_version(root, project, v['id'], catalog)
        current = bundle['versions'][v['id']]
        current['prd_sections'] = sections
        current['attachments'] = attachments
        documents={p['file']:safe(vr/'content',p['file']).read_text(encoding='utf-8') for p in pages(manifest) if p['type']=='markdown'}
        for section in sections or []:
            for source in section['sources']: documents[source]=safe(vr/'content',source).read_text(encoding='utf-8')
        current['material_index']=materiallib.index(manifest,spec,documents)
        current['material_index'] += [{'key':'project','kind':'metadata','title':'项目总览与版本计划','source':'project.json'}, {'key':'delivery','kind':'derived','title':'需求范围与交接（修改源需求）','source':'spec.json / project.json'}, {'key':'outcomes','kind':'ledger','title':'验收结果与上线复盘（追加或更正台账）','source':'outcomes/'}]
        current['material_index'] += [{'key':'attachment:'+a['id'],'kind':'attachment','title':a['name'],'source':a['relative_path']} for a in attachments]
        for source in documents:
            if source in files: files[source]=materiallib.MARKER.sub('',files[source])
        if sections:
            for section in sections:
                for source in section['sources']:
                    if source in files:
                        files[source] = prdlib.document(files[source])['markdown']
            labels = {'provided':'已提供（不代表已确认）','pending':'待补充','not_applicable':'不适用'}
            parts = ['# '+project['name']+' · '+v['id']+' 产品需求文档']
            for section in sections:
                parts += ['## '+section['title'], '章节状态：'+labels[section['status']]+'；'+section['reason'], section['markdown']]
                if section['id'] == 'appendix':
                    parts += ['- '+a['name']+'：'+a['description']+'；文件相对项目根路径：'+a['relative_path']+('（冻结后补充）' if a['late_addition'] else '') for a in attachments]
            current['full_prd_markdown'] = '\n\n'.join(parts)
        if spec and v['status'] == 'planning':
            data = {'project': project['id'], 'version': v['id'], 'build': {'template': TEMPLATE_VERSION, 'generated_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}, 'spec': spec,
                    'noteRevisions': {pid:notelib.digest(note) for pid,note in spec.get('review_notes',{}).items()},
                    'pageRoutes': {p['id']: '/v/' + v['id'] + '/' + m['id'] + '/' + p['id'] for m in manifest['modules'] for p in m['pages'] if p['type'] == 'prototype'},
                    'pageFiles': {p['id']: p['file'] for p in pages(manifest) if p['type'] == 'prototype'}}
            out = vr / 'content/prototype/assets/spec-data.js'
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text('window.PS_SPEC = ' + json.dumps(data, ensure_ascii=False).replace('</', '<\\/') + ';\n', encoding='utf-8')
    errors, warnings = validate(root)
    if errors:
        raise ValueError('\n'.join(errors))
    out = root / 'site/js/data.js'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('window.__PS_PROJECT = ' + json.dumps(bundle, ensure_ascii=False).replace('</', '<\\/') + ';\n', encoding='utf-8')
    write(root / 'site/js/build-state.json', {'inputs': build_inputs(root), 'output': hashlib.sha256(out.read_bytes()).hexdigest()})
    return warnings

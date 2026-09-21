"""Read-only attachment integrity and version membership; no executable previews."""
import hashlib
import json
import pathlib
import re

MAX_BYTES = 10 * 1024 * 1024
EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.txt', '.md', '.csv', '.docx', '.xlsx', '.pptx'}


def load(root):
    directory = root / 'attachments'
    if directory.is_symlink(): raise ValueError('附件目录不能是符号链接')
    result = {}
    for folder in sorted(directory.iterdir()) if directory.exists() else []:
        if folder.is_symlink() or not folder.is_dir() or not re.fullmatch('[a-f0-9]{32}', folder.name): raise ValueError('附件目录 ID 无效')
        meta_path = folder / 'meta.json'
        if meta_path.is_symlink(): raise ValueError('附件元数据不能是符号链接')
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        suffix = pathlib.Path(meta.get('stored_name', '')).suffix
        if meta.get('id') != folder.name or meta.get('stored_name') != 'file'+suffix or suffix not in EXTENSIONS:
            raise ValueError('附件元数据无效')
        file = folder / meta['stored_name']
        if file.is_symlink() or not file.is_file() or not 0 < file.stat().st_size <= MAX_BYTES or file.stat().st_size != meta.get('size'):
            raise ValueError('附件文件缺失、越界或大小不符')
        if hashlib.sha256(file.read_bytes()).hexdigest() != meta.get('sha256'): raise ValueError('附件内容校验失败')
        for key in ('name','description','version','project','created_at'):
            if not isinstance(meta.get(key), str): raise ValueError('附件元数据字段缺失')
        if not isinstance(meta.get('requirement_ids'), list) or type(meta.get('post_freeze')) is not bool: raise ValueError('附件关联无效')
        result[meta['id']] = dict(meta, relative_path='attachments/'+meta['id']+'/'+meta['stored_name'])
    return result


def refs(content):
    path = content / 'attachment-refs.json'
    if path.is_symlink(): raise ValueError('附件引用不能是符号链接')
    value = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if not isinstance(value, dict) or any(not re.fullmatch('[a-f0-9]{32}', k) or not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v) for k,v in value.items()):
        raise ValueError('版本附件引用无效')
    return value


def for_version(root, project, version, catalog=None):
    catalog = load(root) if catalog is None else catalog
    linked = refs(root / 'versions' / version / 'content')
    ids = set(linked) | {key for key,m in catalog.items() if m['version']==version and m['post_freeze']}
    result = []
    for key in sorted(ids):
        if key not in catalog: raise ValueError('引用的附件不存在：'+key)
        meta = catalog[key]
        if meta['project'] != project['id'] or key in linked and linked[key] != meta['sha256']:
            raise ValueError('附件项目或历史校验值不匹配')
        result.append(dict(meta, late_addition=key not in linked))
    return result

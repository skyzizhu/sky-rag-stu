"""统一的文档可见权限：公开全员、内部部门、受限员工；与资料归属无关。"""
from __future__ import annotations

import unicodedata

ALL_GROUP = "all"


def _names(values, field: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{field} 必须是名单")
    names = []
    for value in values:
        if not isinstance(value, str) or len(value) > 80 or any(
                unicodedata.category(c).startswith('C') for c in value):
            raise ValueError(f"{field} 包含非法名称")
        value = value.strip()
        if value and value not in names:
            names.append(value)
    return names


def normalize_visibility(allowed_groups=None, allowed_users=None, *, legacy_default: bool = True) -> dict:
    """生成互斥授权配置；显式空名单永不回落全员（停用最后一名员工后仍封闭）。"""
    groups = _names(allowed_groups, 'allowed_groups')
    users = _names(allowed_users, 'allowed_users')
    if users:
        groups = []
    elif ALL_GROUP in groups:
        groups = [ALL_GROUP]
    elif allowed_groups is None and allowed_users is None and legacy_default:
        groups = [ALL_GROUP]
    return {'allowed_groups': groups, 'allowed_users': users,
            'classification': classify_visibility(groups, users)}


def classify_visibility(allowed_groups, allowed_users) -> str:
    if _names(allowed_users, 'allowed_users'):
        return '受限'
    groups = _names(allowed_groups, 'allowed_groups')
    if ALL_GROUP in groups:
        return '公开'
    return '内部' if groups else '受限'


def visible_groups(department: str | None) -> list[str]:
    return [department] if department else [ALL_GROUP]


def scope_filters(filters: dict | None, user_groups: list[str] | None,
                  username: str | None = None) -> dict | None:
    """强制身份范围。保留其他筛选，覆盖调用方伪造的部门、员工或 viewer 条件。

    只有显式 user_groups=None 才是个人/离线模式，不是用户名缺失就解除部门权限。
    viewer 条件由向量库下推；它取代独立的 ACL must，不能再额外 AND 部门名单。
    """
    if user_groups is None:
        return filters
    groups = sorted({*user_groups, ALL_GROUP})
    result = dict(filters or {})
    result.pop('allowed_users', None)
    result['allowed_groups'] = groups
    result['__viewer__'] = {'groups': groups, 'username': username or ''}
    return result


def document_visible(document: dict, user_groups: list[str] | None, username: str = '') -> bool:
    """列表本地过滤与数据库可见性等价；缺失授权字段不自动开放。"""
    if user_groups is None:
        return True
    groups = set(document.get('allowed_groups') or [])
    users = set(document.get('allowed_users') or [])
    return bool(groups & {*user_groups, ALL_GROUP} or username and username in users)


def write_group_options(department: str | None, current: list[str] | None = None) -> list[str]:
    """兼容旧权限控件；新的三级选择器从部门主数据读取可授权部门。"""
    opts = [ALL_GROUP] + ([department] if department else [])
    return list(dict.fromkeys(opts + [g for g in (current or []) if g]))

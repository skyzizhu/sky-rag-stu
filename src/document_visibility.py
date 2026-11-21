"""文档授权解析；显式入库权限优先于文件声明，权限编辑优先于入库默认。"""
from src.permissions import classify_visibility, normalize_visibility


def resolve_visibility(previous, front_matter, *, default_groups=None, default_allowed_users=None,
                       legacy_overrides=None):
    info = dict(previous or {})
    fm = front_matter or {}
    overrides = legacy_overrides or {}
    explicit = default_allowed_users is not None and not info.get('value')
    if explicit:
        info['upload_default'] = normalize_visibility(default_groups, default_allowed_users,
                                                       legacy_default=False)
    elif default_groups is not None and not info.get('value'):
        info['upload_default'] = normalize_visibility(default_groups, [], legacy_default=False)
    if 'manual' in info:
        acl = dict(info['manual'])
    elif 'value' in info:
        acl = dict(info['value'])
    elif 'allowed_groups' in overrides or 'allowed_users' in overrides:
        acl = normalize_visibility(overrides.get('allowed_groups'), overrides.get('allowed_users'))
    elif explicit:
        acl = dict(info['upload_default'])
    elif 'allowed_groups' in fm or 'allowed_users' in fm:
        acl = normalize_visibility(fm.get('allowed_groups'), fm.get('allowed_users'))
    elif 'upload_default' in info:
        acl = dict(info['upload_default'])
    else:
        acl = normalize_visibility()
    acl = normalize_visibility(acl.get('allowed_groups'), acl.get('allowed_users'), legacy_default=False)
    info['value'] = acl
    return acl, info


def prune_user_grants(acl: dict, granted: dict, revocations: dict, active_users: set[str]) -> None:
    """去掉无效或已撤销的个人授权；原授权为空时保持受限。"""
    acl['allowed_users'] = [
        user for user in acl['allowed_users']
        if user in active_users and granted.get(user) == revocations.get(user)
    ]
    acl['classification'] = classify_visibility(acl['allowed_groups'], acl['allowed_users'])

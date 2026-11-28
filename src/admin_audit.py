"""后台请求统一审计：只保存身份、动作和安全摘要，不保存原始请求/响应。"""
from __future__ import annotations

import logging

from src import auth_db

# 覆盖后台变更入口；查看及认证请求不记录，未来新增写接口仍由 middleware 兜底记录。
OPERATIONS = {
    'api_services_restart': ('service_restart_all', '重启全部服务'),
    'api_services_stop': ('service_stop_all', '关闭全部服务'),
    'api_service_control': ('service_control', '控制单个服务'),
    'api_business_direction_create': ('business_direction_create', '创建业务领域'),
    'api_business_direction_update': ('business_direction_update', '编辑业务领域'),
    'api_project_create': ('project_create', '创建项目'),
    'api_project_update': ('project_update', '编辑项目'),
    'api_reveal_ingest_file': ('ingest_file_reveal', '定位入库文件'),
    'api_department_create': ('department_create', '创建部门'),
    'api_department_toggle': ('department_toggle', '停用/启用部门'),
    'api_department_delete': ('department_delete', '删除部门'),
    'api_user_permissions': ('permissions_update', '修改员工权限'),
    'api_user_create': ('employee_create', '创建员工'),
    'api_user_reset': ('password_reset', '重置员工密码'),
    'api_user_toggle': ('employee_toggle', '停用/启用员工'),
    'api_admin_create': ('admin_create', '创建管理员'),
    'api_admin_toggle': ('admin_toggle', '停用/启用管理员'),
    'api_admin_reset': ('admin_password_reset', '重置管理员密码'),
    'api_admin_delete': ('admin_delete', '删除管理员'),
    'api_user_profile': ('profile_update', '修改员工资料'),
    'api_user_transfer': ('employee_transfer', '调整员工部门'),
    'api_token_create': ('token_create', '签发访问令牌'),
    'api_token_revoke': ('token_revoke', '吊销访问令牌'),
}
ACTION_LABELS = dict(OPERATIONS.values())
ACTION_LABELS['admin_request'] = '其他管理操作'

# 已存在的查看类记录保留在数据库中，但不再进入操作日志列表和统计。
_CHANGE_LOG_WHERE = "(method IN ('POST','PUT','PATCH','DELETE') AND action NOT IN ('login','logout','bootstrap_admin') AND path NOT IN ('/admin/login','/admin/logout','/admin/bootstrap','/auth/bootstrap-admin'))"


def record(*, admin: dict | None, event: dict, action: str, target: str, method: str,
           path: str, status_code: int, duration_ms: int, ip: str, user_agent: str) -> None:
    if action in {'login', 'logout', 'bootstrap_admin'} or path in {'/admin/login', '/admin/logout', '/admin/bootstrap', '/auth/bootstrap-admin'}:
        return
    username = (admin or {}).get('username') or event.get('username', '')
    detail = event.get('detail', '')
    # 撤销接口的 token_prefix 属于查询参数，不进入审计摘要。
    if action == 'token_revoke':
        detail = '吊销访问令牌' if status_code < 400 else ''
    if not detail and status_code >= 400:
        detail = {
            400: '请求参数无效或操作被拒绝', 401: '未登录、会话过期或认证失败',
            403: '权限或同源校验拒绝请求', 404: '操作对象或接口不存在',
            422: '请求参数未通过校验',
            409: '记录已被其他管理员修改，请重新加载后编辑',
        }.get(status_code, '服务端处理失败' if status_code >= 500 else '请求失败')
    if not detail and action in {'service_control', 'service_restart_all', 'service_stop_all'}:
        detail = '服务控制指令已受理，实际运行结果见服务状态'
    try:
        with auth_db._connect() as conn:
            conn.execute(
                'INSERT INTO admin_operation_logs(admin_id,username,authenticated,action,target,detail,'
                'method,path,status_code,success,duration_ms,ip,user_agent,created_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ((admin or {}).get('id'), str(username)[:80], int(bool(admin)), action[:100],
                 target[:200], str(detail)[:1000], method, path[:200], status_code,
                 int(status_code < 400), max(0, duration_ms), ip[:100], user_agent[:512], auth_db._now()),
            )
    except Exception:
        logging.getLogger(__name__).exception('写入管理员操作日志失败')


def list_operations(*, page: int = 1, page_size: int = 20, username: str = '',
                    action: str = '', success: bool | None = None, snapshot_id: int = 0) -> dict:
    with auth_db._connect() as conn:
        if not snapshot_id:
            snapshot_id = conn.execute('SELECT COALESCE(MAX(id),0) FROM admin_operation_logs').fetchone()[0]
        where, params = ['id<=?', _CHANGE_LOG_WHERE], [snapshot_id]
        if username:
            where.append('instr(username,?)>0')
            params.append(username)
        if action:
            where.append('action=?')
            params.append(action)
        if success is not None:
            where.append('success=?')
            params.append(int(success))
        clause = ' WHERE ' + ' AND '.join(where)
        total = conn.execute('SELECT COUNT(*) FROM admin_operation_logs' + clause, params).fetchone()[0]
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        rows = conn.execute('SELECT * FROM admin_operation_logs' + clause + ' ORDER BY id DESC LIMIT ? OFFSET ?',
                            [*params, page_size, (page - 1) * page_size]).fetchall()
        actions = [row[0] for row in conn.execute(
            'SELECT DISTINCT action FROM admin_operation_logs WHERE id<=? AND ' + _CHANGE_LOG_WHERE + ' ORDER BY action', [snapshot_id])]
    logs = [dict(row) for row in rows]
    for log in logs:
        log['action_label'] = ACTION_LABELS.get(log['action'], log['action'])
    return dict(ok=True, logs=logs, total=total, page=page, page_size=page_size,
                total_pages=total_pages, snapshot_id=snapshot_id,
                actions=[dict(value=value, label=ACTION_LABELS.get(value, value)) for value in actions])

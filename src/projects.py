"""项目主档及跨部门关系，复用管理员后台的 SQLite。"""
from __future__ import annotations

import logging
import sqlite3
import unicodedata
from dataclasses import dataclass

from src import auth_db


PROJECT_SUFFIXES = ("项目", "項目", "专案", "專案")


class ProjectConflict(RuntimeError):
    """编辑表单中的版本号已过期。"""


@dataclass(frozen=True)
class ProjectFields:
    name: str
    direction_id: int | None
    lead_department_id: int | None
    participant_department_ids: list[int]
    note: str


def _read_projects(conn: sqlite3.Connection, project_id: int | None = None) -> list[dict]:
    rows = conn.execute(
        """SELECT p.*, d.name AS direction_name, lead.name AS lead_department_name,
                  participant.id AS participant_id, participant.name AS participant_name
           FROM projects p
           LEFT JOIN business_directions d ON d.id = p.direction_id
           LEFT JOIN departments lead ON lead.id = p.lead_department_id
           LEFT JOIN project_departments pd ON pd.project_id = p.id
           LEFT JOIN departments participant ON participant.id = pd.department_id
           WHERE (? IS NULL OR p.id = ?)
           ORDER BY p.id DESC, participant.id""",
        (project_id, project_id),
    )
    projects = {}
    for row in rows:
        record = dict(row)
        participant_id = record.pop('participant_id')
        participant_name = record.pop('participant_name')
        project = projects.setdefault(record['id'], {**record, 'participants': []})
        if participant_id is not None:
            project['participants'].append({'id': participant_id, 'name': participant_name})
    return list(projects.values())


def list_projects() -> list[dict]:
    with auth_db._connect() as conn:
        return _read_projects(conn)


def get_project(project_id: int) -> dict | None:
    with auth_db._connect() as conn:
        return next(iter(_read_projects(conn, project_id)), None)


def validate_project_id(project_id: int | None) -> int | None:
    """接受空关联，或校验所选项目确实存在。"""
    if project_id is None:
        return None
    if type(project_id) is not int or not 0 < project_id < 2**63:
        raise ValueError('项目编号无效')
    with auth_db._connect() as conn:
        if not conn.execute('SELECT 1 FROM projects WHERE id=?', (project_id,)).fetchone():
            raise ValueError('所选项目不存在，请刷新后重试')
    return project_id


def resolve_project_filter(project: str | int | None,
                           business_direction: str | int | None, *,
                           allow_name_variants: bool = False) -> list[int] | None:
    """解析项目、方向及其交集。None 表示无筛选，空列表表示无匹配项目。"""
    if isinstance(project, str):
        project = unicodedata.normalize('NFKC', project).strip() or None
    if isinstance(business_direction, str):
        business_direction = unicodedata.normalize('NFKC', business_direction).strip() or None
    if project is None and business_direction is None:
        return None
    for value, label in ((project, '项目'), (business_direction, '主业务方向')):
        if value is None or isinstance(value, str):
            continue
        if type(value) is not int or not 0 < value < 2**63:
            raise ValueError(f'{label} ID 必须为正整数')

    with auth_db._connect() as conn:
        # 在同一读事务中解析主档，避免并发编辑时把不同时间的项目关系混在一起。
        conn.execute('BEGIN')
        selected = None
        if project is not None:
            selected = conn.execute(
                'SELECT id, direction_id FROM projects WHERE id=?' if isinstance(project, int)
                else 'SELECT id, direction_id FROM projects WHERE name=?',
                (project,),
            ).fetchone()
            if selected is None and allow_name_variants and isinstance(project, str):
                suffix = next((suffix for suffix in PROJECT_SUFFIXES if project.endswith(suffix)), "")
                base_name = project.removesuffix(suffix).rstrip()
                if base_name:
                    matches = conn.execute(
                        'SELECT id, direction_id FROM projects WHERE name IN (?, ?, ?, ?, ?)',
                        (base_name, *(base_name + suffix for suffix in PROJECT_SUFFIXES)),
                    ).fetchall()
                    if len(matches) > 1:
                        raise ValueError(f'项目「{project}」匹配多个主档，请使用准确的项目名称或 ID')
                    selected = next(iter(matches), None)
            if selected is None:
                raise ValueError(f'找不到项目「{project}」，请使用准确的项目名称或 ID')
        if business_direction is None:
            return [selected['id']]

        directions = conn.execute(
            'SELECT id FROM business_directions WHERE id=?' if isinstance(business_direction, int)
            else 'SELECT id FROM business_directions WHERE name=? OR code=?',
            (business_direction,) if isinstance(business_direction, int)
            else (business_direction, business_direction),
        ).fetchall()
        if not directions:
            raise ValueError(f'找不到主业务方向「{business_direction}」，请使用准确的名称、编号或 ID')
        if len(directions) > 1:
            raise ValueError(f'主业务方向「{business_direction}」同时匹配多个主档，请使用 ID')
        direction_id = directions[0]['id']
        if selected is not None:
            return [selected['id']] if selected['direction_id'] == direction_id else []
        return [row['id'] for row in conn.execute(
            'SELECT id FROM projects WHERE direction_id=? ORDER BY id', (direction_id,),
        )]


def save_project(fields: ProjectFields, *, project_id: int | None = None,
                 revision: int | None = None) -> dict:
    name = unicodedata.normalize('NFKC', fields.name).strip()
    if not 1 <= len(name) <= 80 or any(unicodedata.category(c).startswith('C') for c in name):
        raise ValueError('项目名称需为 1～80 个字符，不能包含控制字符')
    note = fields.note.strip()
    if len(note) > 1000:
        raise ValueError('项目备注最多 1000 个字符')
    participants = set(fields.participant_department_ids)
    participants.discard(fields.lead_department_id)
    department_ids = participants.copy()
    if fields.lead_department_id is not None:
        department_ids.add(fields.lead_department_id)

    updating = project_id is not None
    with auth_db._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing_departments = set()
        if project_id is not None:
            before = next(iter(_read_projects(conn, project_id)), None)
            if before is None:
                raise LookupError('项目不存在')
            if before['revision'] != revision:
                raise ProjectConflict('项目已被其他管理员修改，请关闭后重新编辑')
            existing_departments = {dep['id'] for dep in before['participants']}
            existing_departments.add(before['lead_department_id'])

        if fields.direction_id is not None and not conn.execute(
            'SELECT 1 FROM business_directions WHERE id=?', (fields.direction_id,),
        ).fetchone():
            raise ValueError('所选业务方向不存在')
        departments = {row['id']: row['disabled'] for row in conn.execute(
            'SELECT id, disabled FROM departments'
        )}
        if not department_ids.issubset(departments):
            raise ValueError('所选部门不存在')
        if any(departments[dep_id] for dep_id in department_ids - existing_departments):
            raise ValueError('不能新增已停用的部门')
        duplicate = conn.execute('SELECT id FROM projects WHERE name=?', (name,)).fetchone()
        if duplicate and duplicate['id'] != project_id:
            raise ValueError('项目名称已存在')

        values = (name, fields.direction_id, fields.lead_department_id, note)
        if project_id is None:
            project_id = conn.execute(
                'INSERT INTO projects(name, direction_id, lead_department_id, note, created_at) '
                'VALUES(?,?,?,?,?)', (*values, auth_db._now()),
            ).lastrowid
        else:
            conn.execute(
                'UPDATE projects SET name=?, direction_id=?, lead_department_id=?, note=?, '
                'revision=revision+1 WHERE id=?', (*values, project_id),
            )
            conn.execute('DELETE FROM project_departments WHERE project_id=?', (project_id,))
        conn.executemany(
            'INSERT INTO project_departments(project_id, department_id) VALUES(?,?)',
            [(project_id, dep_id) for dep_id in sorted(participants)],
        )
        auth_db.touch_retrieval_scope(conn)
        project = _read_projects(conn, project_id)[0]
    if updating:
        from src.config import get_config
        from src.project_directories import refresh_project_info

        try:
            refresh_project_info(get_config().knowledge_dir, project_id)
        except (OSError, sqlite3.Error):
            logging.getLogger(__name__).warning("Could not sync project directory info", exc_info=True)
    return project

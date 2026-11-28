"""业务领域主数据：独立于部门权限，沿用后台的 SQLite 事务与连接。"""
from __future__ import annotations

import re
import sqlite3
import unicodedata

from src.name_codes import pinyin_code

from src import auth_db


def _name(value: str) -> str:
    value = unicodedata.normalize('NFKC', value).strip()
    if not 1 <= len(value) <= 80 or any(unicodedata.category(c).startswith('C') for c in value):
        raise ValueError('业务领域名称需为 1～80 个字符，不能包含控制字符')
    return value


def suggest_code(name: str) -> str:
    """无声调小写拼音，保留英文字母和数字；允许用户确认或修改多音字结果。"""
    name = _name(name)
    return pinyin_code(name)


def _fields(name: str, code: str, note: str) -> tuple[str, str, str]:
    name = _name(name)
    code = code.strip().lower() or suggest_code(name)
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', code):
        raise ValueError('字母编号需为 1～64 位，以字母开头，仅允许英文字母、数字和下划线')
    note = note.strip()
    if len(note) > 1000 or any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs'}
                             for c in note if c not in '\n\r\t'):
        raise ValueError('备注最多 1000 个字符，不能包含无效控制字符')
    return name, code, note


def create_direction(name: str, code: str = '', note: str = '') -> dict:
    name, code, note = _fields(name, code, note)
    try:
        with auth_db._connect() as conn:
            cursor = conn.execute(
                'INSERT INTO business_directions(name,code,note,created_at) VALUES(?,?,?,?)',
                (name, code, note, auth_db._now()),
            )
            row = conn.execute('SELECT * FROM business_directions WHERE id=?',
                               (cursor.lastrowid,)).fetchone()
            auth_db.touch_retrieval_scope(conn)
    except sqlite3.IntegrityError as exc:
        raise ValueError('业务领域名称或字母编号已存在，请使用其他名称或编号') from exc
    return dict(row)


def list_directions() -> list[dict]:
    with auth_db._connect() as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM business_directions ORDER BY id DESC')]


class DirectionConflict(RuntimeError):
    """编辑期间记录已被其他管理员修改，禁止旧表单静默覆盖。"""


def get_direction(direction_id: int) -> dict | None:
    with auth_db._connect() as conn:
        row = conn.execute('SELECT * FROM business_directions WHERE id=?', (direction_id,)).fetchone()
    return dict(row) if row else None


def update_direction(direction_id: int, name: str, code: str, note: str,
                     original: dict | None = None) -> tuple[dict, dict]:
    name, code, note = _fields(name, code, note)
    try:
        with auth_db._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM business_directions WHERE id=?', (direction_id,)).fetchone()
            if row is None:
                raise LookupError('业务领域不存在')
            before = dict(row)
            if original is not None and any(before[key] != original.get(key) for key in ('name', 'code', 'note')):
                raise DirectionConflict('业务领域已被其他管理员修改，请关闭后重新编辑')
            conn.execute('UPDATE business_directions SET name=?,code=?,note=? WHERE id=?',
                         (name, code, note, direction_id))
            auth_db.touch_retrieval_scope(conn)
            after = dict(conn.execute('SELECT * FROM business_directions WHERE id=?', (direction_id,)).fetchone())
    except sqlite3.IntegrityError as exc:
        raise ValueError('业务领域名称或字母编号已存在，请使用其他名称或编号') from exc
    return before, after

# -*- coding: utf-8 -*-
"""创建评测用部门与员工（走系统自身的 auth_db 服务层，与后台管理页同一实现）。

部门：研发部 / 财务部 / 人力资源部 / 市场部（保留已有"系统工程事业部"不动）
员工：wangfang(研发) zhaocai(财务) lili(人力) liuye(市场)
幂等：已存在则跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import auth_db  # noqa: E402

DEPARTMENTS = ["研发部", "财务部", "人力资源部", "市场部"]
USERS = [
    ("wangfang", "Eval#2026", "研发部", "王芳"),
    ("zhaocai", "Eval#2026", "财务部", "赵彩"),
    ("lili", "Eval#2026", "人力资源部", "李莉"),
    ("liuye", "Eval#2026", "市场部", "刘烨"),
]


def main():
    dep_ids = {}
    for name in DEPARTMENTS:
        existing = [d for d in auth_db.list_departments() if d["name"] == name]
        if existing:
            dep_ids[name] = existing[0]["id"]
            print(f"  = 部门已存在 {name} (id={existing[0]['id']})")
        else:
            dep = auth_db.create_department(name, "评测自动创建部门")
            dep_ids[name] = dep["id"]
            print(f"  + 创建部门 {name} (id={dep['id']})")
    for username, password, dep, real_name in USERS:
        if auth_db.get_user(username):
            print(f"  = 用户已存在 {username}")
        else:
            auth_db.create_user(username, password,
                                department_id=dep_ids[dep], real_name=real_name)
            print(f"  + 创建用户 {username}（{dep}）")
    print("当前部门：", [(d['id'], d['name']) for d in auth_db.list_departments()])
    print("当前用户：", [(u['username'], u['department']) for u in auth_db.list_users()])


if __name__ == "__main__":
    main()

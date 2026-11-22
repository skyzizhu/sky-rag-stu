# -*- coding: utf-8 -*-
"""第二轮评测准备：
1) 恢复星云章程磁盘内容到与向量库 v1.1 一致（第一轮遗留：语料生成器把磁盘重置回了 v1.0）；
2) 登记业务方向 + 5 个项目主档（星云/天枢/北斗/昆仑/沧海），走系统自身服务层；
3) 恢复第一轮被删除的边界文件（gen_corpus 重跑后 .xyz 仍在，空文件已恢复，此处兜底）。
幂等：已存在则跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.projects import ProjectFields, list_projects, save_project  # noqa: E402
from src import business_directions, auth_db  # noqa: E402

# 星云章程 v1.1 内容（与 Qdrant active 版本一致：135 万 + 刘洋 + M4）
CHARTER_V11 = """---
title: 星云项目章程
doc_type: 项目章程
department: 研发部
---

# 星云项目章程

## 项目概况

星云项目为 X-2000 量产与数据平台建设项目，立项预算 135 万元，
项目周期 2024-01 至 2024-09。项目负责人：陈明。
核心成员：陈明（项目负责人）、林岚（架构）、徐凯（后端）、刘洋（测试）。

## 项目里程碑

- M1 需求确认：2024-03；
- M2 开发完成：2024-06；
- M3 系统上线：2024-09；M4 项目验收：2024-12。

## 协作约定

天枢项目与本项目共享测试环境。北斗项目为本项目的前置预研项目，
其遗留的设计文档问题在本项目整改。

## 数据安全

项目数据平台按等级保护三级标准建设，涉及的数据安全测评费用已列入预算。
"""

DIRECTION = ("智能检测方向", "zhinengjiancexiang")
# (名称, 参与部门名列表)——牵头部门统一研发部
PROJECTS = [
    ("星云项目", []),
    ("天枢项目", ["市场部"]),
    ("北斗项目", []),
    ("昆仑项目", ["市场部"]),
    ("沧海项目", []),
]


def main():
    charter = ROOT / "knowledge" / "projects" / "星云项目" / "星云项目章程.md"
    charter.write_text(CHARTER_V11, encoding="utf-8")
    print("  ✓ 星云章程磁盘内容已对齐 v1.1（135 万 + 刘洋 + M4）")

    deps = {d["name"]: d["id"] for d in auth_db.list_departments()}
    existing_dirs = {d["name"]: d["id"] for d in business_directions.list_directions()}
    if DIRECTION[0] in existing_dirs:
        direction_id = existing_dirs[DIRECTION[0]]
        print(f"  = 业务方向已存在 {DIRECTION[0]} (id={direction_id})")
    else:
        direction = business_directions.create_direction(DIRECTION[0], DIRECTION[1], "评测用业务方向")
        direction_id = direction["id"]
        print(f"  + 创建业务方向 {DIRECTION[0]} (id={direction_id})")

    existing = {p["name"]: p["id"] for p in list_projects()}
    for name, participants in PROJECTS:
        if name in existing:
            print(f"  = 项目已存在 {name} (id={existing[name]})")
            continue
        fields = ProjectFields(
            name=name,
            direction_id=direction_id,
            lead_department_id=deps.get("研发部"),
            participant_department_ids=[deps[p] for p in participants if p in deps],
            note="评测自动创建",
        )
        project = save_project(fields)
        print(f"  + 创建项目 {name} (id={project['id']}, 牵头=研发部, 参与={participants or '无'})")
    print("当前项目：", [(p["id"], p["name"], p["direction_name"], p["lead_department_name"],
                       [x["name"] for x in p["participants"]]) for p in list_projects()])


if __name__ == "__main__":
    main()

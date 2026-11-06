"""V3.8：部门权限规则的纯函数（无 Streamlit / 无 IO，可单测）。

前台问答、跨文档总结、MCP 三个入口共用同一份规则——此前三处各写一遍，
改规则时漏改一处就是权限漏洞。所有入口统一从这里取，测试也只需覆盖这里。

约定：卡片 payload 的 allowed_groups 存部门名列表，"all" 表示全员可见。
"""

from __future__ import annotations

ALL_GROUP = "all"


def visible_groups(department: str | None) -> list[str]:
    """检索侧可见域：有部门 → [部门]；无部门 → [all]。

    pipeline 注入时会再并上 all（见 scope_filters）：员工永远能看到全员可见的知识。
    """
    return [department] if department else [ALL_GROUP]


def scope_filters(filters: dict | None, user_groups: list[str] | None) -> dict | None:
    """把可见域强制并入检索过滤条件（服务端注入，调用方在界面上改不掉）。

    user_groups=None 表示不限制（个人模式 / 离线评测），原样返回 filters。
    注入后 allowed_groups 覆盖调用方传入的同名条件——防止伪造更宽的可见域。
    """
    if user_groups is None:
        return filters
    return {**(filters or {}), "allowed_groups": sorted({*user_groups, ALL_GROUP})}


def write_group_options(department: str | None,
                        current: list[str] | None = None) -> list[str]:
    """写入侧（上传归属 / 知识编辑）允许选择的可见域：全员 + 自己部门。

    检索侧按部门强制过滤，写入侧不能成为绕过口——若员工能把文档标成他部门
    可见，就开了条横向数据外泄通道。current 传文档现有可见域（编辑场景）：
    并入选项以免下拉框丢值，但员工只能保留、不能新增别的部门。
    """
    opts = [ALL_GROUP] + ([department] if department else [])
    return list(dict.fromkeys(opts + [g for g in (current or []) if g]))

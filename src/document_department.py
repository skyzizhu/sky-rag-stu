"""独立、可选的文档归属部门：自由文本元数据，不读取正文、不推断、不影响检索可见性。

与 allowed_groups（登录用户的可见权限部门）完全无关：department 只是文档的
「归属部门」档案字段，供筛选、检索与展示使用，永远不参与权限判定。
"""
from __future__ import annotations

import unicodedata


def _normalize(value, *, allow_sentinel: bool = False) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("department must be a string or None")
    if any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} or c in "\u2028\u2029" for c in value):
        raise ValueError("department must not contain control characters")
    value = value.strip()
    if value == "__no_department__" and not allow_sentinel:
        raise ValueError("department uses a reserved value")
    if len(value) > 80:
        raise ValueError("department must be at most 80 characters")
    return value


def normalize_department(value) -> str:
    """可选自由文本：仅去首尾空格，不限制用户定义的部门名称。"""
    return _normalize(value)


def normalize_department_filter(value) -> str | None:
    """筛选专用：未归属哨兵不可存储；all/空白表示不筛选。"""
    value = _normalize(value, allow_sentinel=True)
    return None if not value or value.casefold() == "all" else value


def resolve_department(previous: dict | None, front_matter: dict | None = None,
                       *, default_department: str | None = None) -> dict:
    """部门优先级与小型台账状态：manual 键存在即为人工设置，空值表示清除。

    upload_default 随重建、路径移动保留；文件新声明优先于默认。
    carry 仅用于生命周期操作（如网页覆写、归档/恢复路径移动），普通源文件
    编辑不冻结旧声明。部门没有目录/文件名/URL 推断：任何来源都给不出时为空。
    """
    info = {key: value for key, value in (previous or {}).items()
            if key in {"manual", "upload_default", "carry"}}
    if default_department is not None:
        default = normalize_department(default_department)
        if default:
            info["upload_default"] = default
    fm = front_matter or {}
    if "manual" in info:
        value, source = normalize_department(info["manual"]), "manual"
    else:
        declared = None
        if "department" in fm:
            try:
                declared = normalize_department(fm["department"])
            except ValueError:
                pass  # 非法声明忽略，不能强制转成字符串或混入正文。
        if declared:
            info.pop("carry", None)  # 文件保留/更新了声明，不能继续冻结移动前的旧声明。
        carry = info.get("carry", {})
        if not declared and carry.get("source") == "front_matter":
            declared = normalize_department(carry.get("value"))
        if declared:
            value, source = declared, "front_matter"
        elif info.get("upload_default"):
            value, source = normalize_department(info["upload_default"]), "upload_default"
        elif "carry" in info:
            carry = info["carry"]
            value, source = normalize_department(carry["value"]), carry.get("source", "carried")
        else:
            value, source = "", ""
    info.update(value=value, source=source)
    return info

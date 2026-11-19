"""独立、可选的文档类型：只识别目录/文件名，不读取正文或调用 LLM。"""
from __future__ import annotations

import re
import unicodedata


# 自动识别的明确文档短语；不是用户可填类型的注册表/白名单。
_PHRASES = (
    "总体方案", "一期方案", "二期方案", "三期方案", "四期方案", "五期方案",
    "需求文档", "需求规格说明书", "需求说明书", "需求分析报告",
    "会议纪要", "会议记录", "测试报告", "测试方案", "测试计划", "测试用例",
    "验收文档", "验收报告", "验收方案", "验收材料",
    "技术方案", "实施方案", "建设方案", "设计方案", "解决方案", "项目方案",
    "可行性研究报告", "调研报告", "分析报告", "总结报告", "工作报告",
    "概要设计", "详细设计", "设计文档", "设计说明书", "接口文档",
    "操作手册", "使用手册", "用户手册", "培训材料", "招标文件", "投标文件",
    "合同", "协议", "通知", "公告", "制度", "规范", "标准", "流程",
    "学习笔记", "外部资料", "项目说明", "产品说明",
)
_PHASE_PLAN = re.compile(r"[一二三四五六七八九十百零〇两\d]+期方案")
_STORAGE_FOLDER = re.compile(r"附件|图片|素材|备份|参考资料")
# 简短的类型名仅用于目录；文件名中的裸关键词不猜测。
_DIRECTORY_LABELS = {"方案", "需求", "设计", "测试", "验收", "报告", "手册", "会议"}
_SHORT_LABELS = {"合同", "协议", "通知", "公告", "制度", "规范", "标准", "流程"}
_KNOWN_EXTENSION = re.compile(r"\.(?:md|txt|docx?|pdf|pptx?|xlsx?|csv|tsv|rtf|html?)$", re.I)
_DECORATION = re.compile(
    r"(?:\b(?:version|ver|v)\s*[:=_-]?\s*\d+(?:[._-]\d+)*\b"
    r"|(?:版本号?|第)\s*\d+(?:[._-]\d+)*\s*版?"
    r"|\d{4}[-_.年/]\d{1,2}[-_.月/]\d{1,2}日?|(?<!\d)\d{8}(?!\d))", re.I,
)


def _normalize(value, *, allow_unclassified: bool = False) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("doc_type must be a string or None")
    if any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} or c in "\u2028\u2029" for c in value):
        raise ValueError("doc_type must not contain control characters")
    value = value.strip()
    if value == "__unclassified__" and not allow_unclassified:
        raise ValueError("doc_type uses a reserved value")
    if len(value) > 80:
        raise ValueError("doc_type must be at most 80 characters")
    return value


def normalize_doc_type(value) -> str:
    """可选自由文本：仅去首尾空格，不限制用户定义的类型名称。"""
    return _normalize(value)


def normalize_doc_type_filter(value) -> str | None:
    """筛选专用：未分类哨兵不可存储；all/空白表示不筛选。"""
    value = _normalize(value, allow_unclassified=True)
    return None if not value or value.casefold() == "all" else value


def _matched_phrases(value: str) -> set[str]:
    matches = [(m.start(), m.end(), m.group()) for m in _PHASE_PLAN.finditer(value)]
    for phrase in _PHRASES:
        for match in re.finditer(re.escape(phrase), value):
            if phrase in _SHORT_LABELS and not (
                    match.end() == len(value) or value[match.end()] in " _-）)]】"):
                continue
            matches.append((match.start(), match.end(), phrase))
    # 嵌套短语不制造冲突；工作报告等完整短语优先。
    return {phrase for start, end, phrase in matches
            if not any(a <= start and end <= b and b - a > end - start
                       for a, b, _ in matches)}


def infer_doc_type_from_path(original_path: str) -> str:
    """仅检查目录，自近及远；匹配后保留完整目录名。"""
    if not isinstance(original_path, str):
        return ""
    parts = [p for p in original_path.replace("\\", "/").split("/") if p and p != "."]
    for folder in reversed(parts[:-1]):
        try:
            label = normalize_doc_type(folder)
        except ValueError:
            continue
        if _STORAGE_FOLDER.search(label):
            continue
        if label in _DIRECTORY_LABELS or len(_matched_phrases(label)) == 1:
            return label
    return ""


def infer_doc_type_from_filename(filename: str) -> str:
    """仅返回一个明确文档短语；前缀、日期、关键词不混入类型。"""
    if not isinstance(filename, str):
        return ""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = _KNOWN_EXTENSION.sub("", name)
    name = _DECORATION.sub("", name)
    matches = _matched_phrases(name)
    return next(iter(matches)) if len(matches) == 1 else ""


def resolve_doc_type(filename: str, front_matter: dict | None = None,
                     previous: dict | None = None, *, default_doc_type: str | None = None,
                     original_path: str | None = None, is_web: bool = False) -> dict:
    """类型优先级与小型台账状态：manual 键存在即为人工设置，空值表示清除。

    upload_default/original_path 随重建、路径移动保留；文件新声明优先于默认。
    carry 仅用于生命周期操作（如网页覆写），普通源文件编辑不冻结旧声明。
    """
    info = {key: value for key, value in (previous or {}).items()
            if key in {"manual", "upload_default", "original_path", "is_web", "carry"}}
    if default_doc_type is not None:
        default = normalize_doc_type(default_doc_type)
        if default:
            info["upload_default"] = default
    if original_path is not None:
        info["original_path"] = original_path
    if is_web:
        info["is_web"] = True
    fm = front_matter or {}
    if "manual" in info:
        value, source = normalize_doc_type(info["manual"]), "manual"
    else:
        declared = None
        if "doc_type" in fm:
            try:
                declared = normalize_doc_type(fm["doc_type"])
            except ValueError:
                pass  # 非法声明忽略，不能强制转成字符串或混入正文。
        if declared:
            info.pop("carry", None)  # 文件保留/更新了声明，不能继续冻结移动前的旧声明。
        carry = info.get("carry", {})
        if not declared and carry.get("source") == "front_matter":
            declared = normalize_doc_type(carry.get("value"))
        if declared:
            value, source = declared, "front_matter"
        elif info.get("upload_default"):
            value, source = normalize_doc_type(info["upload_default"]), "upload_default"
        elif "carry" in info:
            carry = info["carry"]
            value, source = normalize_doc_type(carry["value"]), carry.get("source", "carried")
        elif info.get("is_web"):
            value, source = "", ""
        else:
            value = infer_doc_type_from_path(info.get("original_path", ""))
            source = "directory" if value else "filename"
            value = value or infer_doc_type_from_filename(filename)
            if not value:
                source = ""
    info.update(value=value, source=source)
    return info

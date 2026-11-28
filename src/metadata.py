"""知识库管理：目录 → 元数据（Metadata）。

产品视角：这一层回答的是「这条知识是谁、从哪来、属于哪个知识板块」——
- 目录负责物理管理：一级目录 = domain，三级目录提示 topic
- Front Matter 负责人工声明：Markdown 文件可以在文件头声明知识板块、主题等元数据
- 历史 category 声明仍可读取，目录和默认值不再生成该字段

合并优先级（高 → 低）：
    系统强制字段 > Front Matter > 目录推断 > 默认值

Domain 固定枚举（系统内部一律英文，界面可显示中文）：
    work / learning / life / projects / reference / archive
"""

from __future__ import annotations

import hashlib
import unicodedata
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

from src.console import console_print
from src.domains import all_codes as domain_codes
from src.document_department import resolve_department
from src.document_type import resolve_doc_type

# ---------------- 规范常量 ----------------
# V3.8：知识板块不再写死——编号由 src/domains.py 的注册表维护（前台「知识板块管理」页增删改），
# 这里只认编号；显示名称只在界面层使用，不进任何元数据。

STATUSES = ["active", "archive", "expired"]


def chunk_sequence(metadata: dict) -> int | None:
    """Return a stored source ordinal, with support for older chunk IDs."""
    ordinal = metadata.get("chunk_index")
    if type(ordinal) is int and ordinal >= 0:
        return ordinal
    tail = str(metadata.get("chunk_id") or "").rsplit("_", 1)[-1]
    return int(tail) if tail.isascii() and tail.isdigit() and len(tail) >= 4 else None


def bump_version(version: str) -> str:
    """内容变化时版本号自动递增。

    两段版本（主.次）走浮点进位（1.9→2.0，继承旧规则）；
    三段及以上只递增末段（2.0.9→2.0.10）。解析失败回退 1.1。
    """
    parts = version.split(".")
    if len(parts) <= 2:
        try:
            return f"{float(version) + 0.1:.1f}"
        except (TypeError, ValueError):
            return "1.1"
    try:
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    except (TypeError, ValueError, IndexError):
        return "1.1"

# ---------------- 文件名版本识别（V3.9：显式标记优先，模糊数字不猜测） ----------------
# 各段使用同一种分隔符，避免把版本后面的日期/副本编号并入版本。
_VERSION_NUMBER = (
    r"(?:[0-9]+(?:\.[0-9]+)+|[0-9]+(?:_[0-9]+)+|[0-9]+(?:-[0-9]+)+|[0-9]+)"
)
_EXPLICIT_FILENAME_VERSION_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:version|ver|v|版本号?|版次)\s*[:=_-]?\s*"
    r"(?P<number>" + _VERSION_NUMBER + r")(?![A-Za-z0-9.]|-(?:alpha|beta|rc|dev))",
    re.IGNORECASE,
)
_CHINESE_FILENAME_VERSION_RE = re.compile(
    r"(?<![A-Za-z0-9.])(?:第\s*)?(?P<number>" + _VERSION_NUMBER + r")\s*版"
)
_BARE_FILENAME_VERSION_RE = re.compile(
    r"(?:^|[_\-\s(【\[])(?P<number>" + _VERSION_NUMBER + r")\s*[)】\]]?$"
)


def _normalize_filename_version(raw: str, *, bare: bool = False) -> str | None:
    parts = re.split(r"[._-]", raw)
    # 裸数字只在末尾至少两段时识别；长数字通常是日期/编号，不推断。
    if not (2 if bare else 1) <= len(parts) <= 4 or any(len(p) > 3 for p in parts):
        return None
    if bare and "." in raw:
        # 裸的「数字.数字」无 v 标记：很可能是章节号（说明_1.2 = 第1.2节），不猜测。
        return None
    if bare and len(parts) == 3 and len(parts[0]) == 2:
        # 裸的 YY_MM_DD / YY.MM.DD 也可能是日期，宁可交回默认规则。
        from datetime import date
        try:
            date(2000 + int(parts[0]), int(parts[1]), int(parts[2]))
            return None
        except ValueError:
            pass
    normalized = ".".join(str(int(p)) for p in parts)
    return normalized if len(parts) > 1 else normalized + ".0"


def version_from_filename(source: str) -> str | None:
    """显式标记/中文版次优先，其次末尾的多段数字；冲突或日期不猜测。"""
    stem = unicodedata.normalize("NFKC", Path(source).stem)
    matches = list(_EXPLICIT_FILENAME_VERSION_RE.finditer(stem))
    matches += list(_CHINESE_FILENAME_VERSION_RE.finditer(stem))
    if matches:
        versions = set()
        for match in matches:
            raw = match.group("number")
            tail = stem[match.end("number"):]
            # 不接受从不完整或混合分隔符数字串中截出一个整数版本。
            if not re.search(r"[._-]", raw) and re.match(r"[._-][0-9]", tail):
                return None
            value = _normalize_filename_version(raw)
            if value is None:
                return None
            versions.add(value)
        return next(iter(versions)) if len(versions) == 1 else None
    match = _BARE_FILENAME_VERSION_RE.search(stem)
    if not match:
        return None
    return _normalize_filename_version(match.group("number"), bare=True)


# 已知「目录名 → Topic 展示名」对照；不在表里的目录名原样作为 topic
TOPIC_NAME_MAP = {
    "rag": "RAG",
    "agent": "Agent",
    "llm": "LLM",
    "prompt": "Prompt",
    "ai": "AI",
    "mcp": "MCP",
}

# 系统强制字段：即使 Front Matter 里写了这些字段，也会被系统忽略
FORCED_FIELDS = {"document_id", "chunk_id", "source", "path", "file_type", "indexed_at"}

# 各字段默认值（Knowledge Management V1 规范 §26）
DEFAULTS = {
    "domain": "reference",
    "doc_type": "",
    # V3.9 四层标签：归属部门（资料属于哪个部门），与 allowed_groups 可见权限无关；空 = 未指定
    "department": "",
    "topic": [],
    "tags": [],
    "version": "1.0",
    "status": "active",
    # V3.7 权限：可见部门名列表；["all"] = 全员可见；空值一律落为 ["all"]
    "allowed_groups": ["all"],
    # V3.9 三级密级：受限名单（员工用户名）；公开/内部不使用该字段
    "allowed_users": [],
    # 密级（建档要求）：默认"公开"；实际密级由 pipeline 按可见配置自动派生
    "classification": "公开",
}

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


# ---------------- ID 生成 ----------------
def document_id_for(relative_path: str) -> str:
    """根据相对路径生成稳定的 document_id（doc_ + 8 位哈希）。

    同一路径无论入库多少次，id 不变；文件改名/移动 = 新文档（V3 再解决迁移）。
    """
    normalized = relative_path.replace("\\", "/").lstrip("/")
    digest = hashlib.md5(normalized.encode("utf-8")).hexdigest()[:8]
    return f"doc_{digest}"


def chunk_id_for(document_id: str, index: int) -> str:
    """可读、稳定的 chunk id：doc_a913bc12_0001"""
    return f"{document_id}_{index:04d}"


# ---------------- Front Matter ----------------
def parse_front_matter(text: str) -> tuple[dict, str]:
    """从 Markdown 文本中拆出 YAML Front Matter。

    返回 (front_matter字典, 去掉声明后的正文)。没有声明时正文原样返回；
    声明存在但 YAML 解析失败时，正文去掉声明块（否则 YAML 原文会被当正文入库可检索）。
    """
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text
    body = text[match.end():]
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        console_print(f"⚠️ Front Matter 解析失败，已忽略声明块（{exc}）")
        return {}, body
    if not isinstance(data, dict):
        return {}, body
    return data, body


# ---------------- 目录推断 ----------------
def normalize_category(raw) -> str:
    """category 规范化：转小写、去首尾空格；空值 → 空字符串。"""
    if raw is None:
        return ""
    cleaned = str(raw).strip().lower()
    return cleaned


def infer_from_path(relative_path: str) -> dict:
    """从相对路径推断 domain / topic / status。

    规则：
        learning/ai/rag/x.md  → domain=learning, topic=[RAG]
        work/projects/x.md    → domain=work, topic=[]
        archive/x.md          → domain=archive, status=archive（强制）
        x.md（根目录散落文件）→ domain=reference（默认）
    """
    parts = [p for p in relative_path.replace("\\", "/").split("/") if p and p != "."]
    if len(parts) <= 1:  # 直接放在 knowledge/ 根目录的文件
        return {"domain": DEFAULTS["domain"],
                "topic": [], "status": DEFAULTS["status"]}

    dirs = parts[:-1]  # 最后一段是文件名，其余才是目录链
    domain = dirs[0].strip().lower()
    known = domain_codes()  # 注册表里的全部编号（含停用：停用只拦新上传，已有数据照常有效）
    if domain not in known:
        # 目录名不是已登记知识板块时回退默认值：否则这些文档按知识板块筛选永远筛不出来
        # （前台「知识板块管理」页会把这类目录列为「未登记目录」，可一键登记）
        console_print(f"⚠️ 目录「{dirs[0]}」不是已登记知识板块（{'/'.join(known)}），按 {DEFAULTS['domain']} 处理")
        domain = DEFAULTS["domain"]

    topic = []
    if len(dirs) >= 3:  # 保留历史目录的主题推断
        topic_dir = dirs[2].strip()
        topic = [TOPIC_NAME_MAP.get(topic_dir.lower(), topic_dir)] if topic_dir else []

    status = "archive" if domain == "archive" else DEFAULTS["status"]
    return {"domain": domain, "topic": topic, "status": status}


# ---------------- 字段校验 ----------------
def _valid_domain(raw) -> str | None:
    """校验 domain；非法值返回 None（由调用方决定回退）。"""
    if raw is None:
        return None
    value = str(raw).strip().lower()
    return value if value in domain_codes() else None


def _valid_status(raw) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip().lower()
    return value if value in STATUSES else None


def _as_list(raw) -> list[str]:
    """topic / tags 统一成字符串列表（兼容单个字符串写法）。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [str(raw).strip()] if str(raw).strip() else []


# ---------------- 元数据组装 ----------------
def build_document_metadata(
    relative_path: str,
    source: str,
    file_type: str,
    title_hint: str | None,
    created_at: str | None,
    updated_at: str | None,
    front_matter: dict | None = None,
    *,
    inference_path: str | None = None,
) -> dict:
    """生成符合统一 Schema 的完整文档级 Metadata。

    优先级：系统强制字段 > Front Matter > 目录推断 > 默认值。
    注意 archive 目录的 status 是「目录强制」，Front Matter 也改不掉。
    """
    fm = front_matter or {}
    path_info = infer_from_path(inference_path or relative_path)

    # domain / topic / tags：FM > 目录 > 默认
    domain = ("archive" if path_info["status"] == "archive"
              else _valid_domain(fm.get("domain")) or path_info["domain"])
    category = normalize_category(fm.get("category"))  # 兼容旧文档显式声明
    topic = _as_list(fm.get("topic")) or path_info["topic"] or []
    tags = _as_list(fm.get("tags"))

    # 未声明的旧文档默认全员；显式空授权不能回落 all，防受限文档被开放。
    from src.permissions import normalize_visibility
    acl = normalize_visibility(fm.get("allowed_groups"), fm.get("allowed_users"))
    allowed_groups, allowed_users = acl["allowed_groups"], acl["allowed_users"]

    # status：archive 目录强制；其余 FM > 目录默认
    if path_info["status"] == "archive":
        status = "archive"
    else:
        status = _valid_status(fm.get("status")) or DEFAULTS["status"]

    # title：FM > 解析器提示（H1 / Word 标题）> 文件名
    title = (str(fm.get("title")).strip() if fm.get("title") else None) \
        or (title_hint or "").strip() or Path(source).stem
    # effective_date（V3.9）：FM 声明的生效日期（企业文档的版本有效性），缺省为空不造假
    effective_date = str(fm.get("effective_date") or "").strip()
    # version：FM 可手动指定，默认 1.0
    version = (version_from_filename(source)
               or str(fm.get("version") or "").strip() or DEFAULTS["version"])
    # 密级（建档要求）：FM 声明 > 默认"公开"；档位白名单（公开/内部/受限）由
    # pipeline 按最终可见配置派生并覆盖，此处仅保留 FM 标注用于告警比对
    classification = acl["classification"]

    warn = []
    if fm.get("domain") is not None and _valid_domain(fm.get("domain")) is None:
        warn.append(f"Front Matter domain 非法：{fm.get('domain')}（已忽略，可用编号：{domain_codes()}）")
    if fm.get("status") is not None and _valid_status(fm.get("status")) is None:
        warn.append(f"Front Matter status 非法：{fm.get('status')}（已忽略，枚举仅限 {STATUSES}）")
    for message in warn:
        console_print(f"⚠️ {message}")

    return {
        # 系统强制字段（Front Matter 无权修改）
        "document_id": document_id_for(relative_path),
        "chunk_id": None,  # 切片时才生成
        "source": source,
        "path": relative_path,
        "file_type": file_type,
        "indexed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # 内容字段
        "title": title,
        "effective_date": effective_date,  # V3.9：FM 声明的生效日期（空 = 未声明）
        "section": None,       # 切片时填写
        "section_path": None,  # 切片时填写
        "page": None,          # 切片时填写
        # 分类字段
        "domain": domain,
        **({"category": category} if category else {}),
        "doc_type": resolve_doc_type(source, fm, is_web=fm.get("source_type") == "web")["value"],
        # V3.9 四层标签：归属部门声明（pipeline 会再叠加台账人工设置与上传默认）
        "department": resolve_department(None, fm)["value"],
        "topic": topic,
        "tags": tags,
        # 密级（建档要求）：此处为 FM 标注；实际密级由 pipeline 按可见配置派生
        "classification": classification,
        # V3.9 三级密级：受限名单（员工用户名）
        "allowed_users": allowed_users,
        # V3.7 权限字段
        "allowed_groups": allowed_groups,
        # 生命周期字段
        "version": version,
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
    }


# ---------------- 直接运行本文件：查看知识目录的元数据推断结果 ----------------
if __name__ == "__main__":
    from src.config import get_config
    from src.parser import SUPPORTED_EXTENSIONS as SUPPORTED_PARSE_EXTENSIONS

    cfg = get_config()
    files = sorted(
        p for p in cfg.knowledge_dir.rglob("*")
        if p.is_file() and not p.name.startswith(".")
        and p.suffix.lower() in SUPPORTED_PARSE_EXTENSIONS
        and not any(part.startswith(".") for part in p.relative_to(cfg.knowledge_dir).parts)
    )
    print(f"知识目录：{cfg.knowledge_dir}\n")
    header = f"{'相对路径':<44} {'domain':<10} {'topic':<14} status"
    print(header)
    print("-" * len(header.expandtabs()))
    for path in files:
        rel = path.relative_to(cfg.knowledge_dir).as_posix()
        info = infer_from_path(rel)
        topic = ",".join(info["topic"]) or "-"
        print(f"{rel:<44} {info['domain']:<10} {topic:<14} {info['status']}")
    sys.exit(0)

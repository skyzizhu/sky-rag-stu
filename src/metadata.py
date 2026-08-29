"""知识库管理：目录 → 元数据（Metadata）。

产品视角：这一层回答的是「这条知识是谁、从哪来、属于哪个领域」——
- 目录负责物理管理：一级目录 = domain，二级目录 = category，三级目录提示 topic
- Front Matter 负责人工声明：Markdown 文件可以在文件头声明自己的分类
- 系统负责兜底：任何字段都不允许缺失，哪怕是空值

合并优先级（高 → 低）：
    系统强制字段 > Front Matter > 目录推断 > 默认值

Domain 固定枚举（系统内部一律英文，界面可显示中文）：
    work / learning / life / reference / archive
"""

from __future__ import annotations

import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

# ---------------- 规范常量 ----------------
DOMAINS = ["work", "learning", "life", "reference", "archive"]
DOMAIN_LABELS = {
    "work": "工作",
    "learning": "学习",
    "life": "生活",
    "reference": "参考资料",
    "archive": "归档",
}

STATUSES = ["active", "archive"]

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
    "category": "general",
    "topic": [],
    "tags": [],
    "version": "1.0",
    "status": "active",
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

    返回 (front_matter字典, 去掉声明后的正文)。没有声明或解析失败时返回 ({}, 原文)。
    """
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        print(f"⚠️ Front Matter 解析失败，已忽略（{exc}）")
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, text[match.end():]


# ---------------- 目录推断 ----------------
def normalize_category(raw) -> str:
    """category 规范化：转小写、去首尾空格；空值 → general。"""
    if raw is None:
        return DEFAULTS["category"]
    cleaned = str(raw).strip().lower()
    return cleaned or DEFAULTS["category"]


def infer_from_path(relative_path: str) -> dict:
    """从相对路径推断 domain / category / topic / status。

    规则：
        learning/ai/rag/x.md  → domain=learning, category=ai, topic=[RAG]
        work/projects/x.md    → domain=work,     category=projects, topic=[]
        archive/x.md          → domain=archive, category=general, status=archive（强制）
        x.md（根目录散落文件）→ domain=reference（默认）, category=general
    """
    parts = [p for p in relative_path.replace("\\", "/").split("/") if p and p != "."]
    if len(parts) <= 1:  # 直接放在 knowledge/ 根目录的文件
        return {"domain": DEFAULTS["domain"], "category": DEFAULTS["category"],
                "topic": [], "status": DEFAULTS["status"]}

    dirs = parts[:-1]  # 最后一段是文件名，其余才是目录链
    domain = dirs[0].strip().lower()
    category = normalize_category(dirs[1]) if len(dirs) >= 2 else DEFAULTS["category"]

    topic = []
    if len(dirs) >= 3:  # category 下的下一级目录提示主题
        topic_dir = dirs[2].strip()
        topic = [TOPIC_NAME_MAP.get(topic_dir.lower(), topic_dir)] if topic_dir else []

    status = "archive" if domain == "archive" else DEFAULTS["status"]
    return {"domain": domain, "category": category, "topic": topic, "status": status}


# ---------------- 字段校验 ----------------
def _valid_domain(raw) -> str | None:
    """校验 domain；非法值返回 None（由调用方决定回退）。"""
    if raw is None:
        return None
    value = str(raw).strip().lower()
    return value if value in DOMAINS else None


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
) -> dict:
    """生成符合统一 Schema 的完整文档级 Metadata。

    优先级：系统强制字段 > Front Matter > 目录推断 > 默认值。
    注意 archive 目录的 status 是「目录强制」，Front Matter 也改不掉。
    """
    fm = front_matter or {}
    path_info = infer_from_path(relative_path)

    # domain / category / topic / tags：FM > 目录 > 默认
    domain = _valid_domain(fm.get("domain")) or path_info["domain"] or DEFAULTS["domain"]
    category = normalize_category(fm.get("category") or path_info["category"])
    topic = _as_list(fm.get("topic")) or path_info["topic"] or []
    tags = _as_list(fm.get("tags"))

    # status：archive 目录强制；其余 FM > 目录默认
    if path_info["status"] == "archive":
        status = "archive"
    else:
        status = _valid_status(fm.get("status")) or DEFAULTS["status"]

    # title：FM > 解析器提示（H1 / Word 标题）> 文件名
    title = (str(fm.get("title")).strip() if fm.get("title") else None) \
        or (title_hint or "").strip() or Path(source).stem
    # version：FM 可手动指定，默认 1.0
    version = str(fm.get("version")).strip() if fm.get("version") is not None else DEFAULTS["version"]

    warn = []
    if fm.get("domain") is not None and _valid_domain(fm.get("domain")) is None:
        warn.append(f"Front Matter domain 非法：{fm.get('domain')}（已忽略，枚举仅限 {DOMAINS}）")
    if fm.get("status") is not None and _valid_status(fm.get("status")) is None:
        warn.append(f"Front Matter status 非法：{fm.get('status')}（已忽略，枚举仅限 {STATUSES}）")
    for message in warn:
        print(f"⚠️ {message}")

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
        "section": None,       # 切片时填写
        "section_path": None,  # 切片时填写
        "page": None,          # 切片时填写
        # 分类字段
        "domain": domain,
        "category": category,
        "topic": topic,
        "tags": tags,
        # 生命周期字段
        "version": version,
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
    }


# ---------------- 直接运行本文件：查看知识目录的元数据推断结果 ----------------
if __name__ == "__main__":
    from src.config import get_config

    cfg = get_config()
    files = sorted(
        p for p in cfg.knowledge_dir.rglob("*")
        if p.is_file() and not p.name.startswith(".")
        and p.suffix.lower() in {".txt", ".md", ".rtf", ".html", ".htm", ".docx", ".pdf"}
    )
    print(f"知识目录：{cfg.knowledge_dir}\n")
    header = f"{'相对路径':<44} {'domain':<10} {'category':<12} {'topic':<14} status"
    print(header)
    print("-" * len(header.expandtabs()))
    for path in files:
        rel = path.relative_to(cfg.knowledge_dir).as_posix()
        info = infer_from_path(rel)
        topic = ",".join(info["topic"]) or "-"
        print(f"{rel:<44} {info['domain']:<10} {info['category']:<12} {topic:<14} {info['status']}")
    sys.exit(0)

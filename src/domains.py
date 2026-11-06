"""V3.8：领域注册表——编号（code）是数据身份，名称（name）只用于显示。

编号写进：knowledge/ 第一层文件夹名、每张卡片 payload 的 domain 字段、检索过滤条件、
Q→A 缓存键、Front Matter、MCP 的 domain 参数、交给 LLM 的出处行——创建后永不改变。
名称只存在于本注册表、只出现在界面上：改名不碰任何文件、卡片、向量与过滤条件，
因此对检索零影响（tests/test_domains.py 有专门的回归测试守住这一点）。

存储：storage/domains.json（前台、入库流水线、MCP、命令行入库共用）。文件不存在时
等价于 6 个内置领域，编号与旧版写死的枚举完全一致——存量数据无需任何迁移。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from src.config import PROJECT_ROOT

REGISTRY_PATH = PROJECT_ROOT / "storage" / "domains.json"
BUILTIN_CODES = ("work", "learning", "life", "projects", "reference", "archive")
# 系统领域：archive 承载归档 / 恢复；reference 是未知目录的兜底 + 网页入库的落盘位置
RESERVED_CODES = frozenset({"archive", "reference"})
FORBIDDEN_CODES = frozenset({"all"})  # 「全部」已被检索设置 / 知识库管理的筛选项占用
_CODE_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_NAME_MAX = 20
_DESC_MAX = 120
_JUNK_FILES = (".DS_Store", ".gitkeep")  # 删除空领域时可一并清掉的系统占位文件

_lock = threading.Lock()
_cache: dict = {"key": None, "items": None}


class DomainError(ValueError):
    """领域操作被拒绝（信息直接给人看）。"""


def _builtin_entry(code: str) -> dict:
    return {"code": code, "name": "", "description": "", "enabled": True,
            "builtin": True, "created_at": ""}


# ---------------------------------------------------------------- 读取
def load_domains() -> list[dict]:
    """全部已登记领域（含停用），按登记顺序。文件不存在 / 损坏时回落到 6 个内置领域。"""
    path = REGISTRY_PATH
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return [_builtin_entry(c) for c in BUILTIN_CODES]
    if _cache["key"] == key:
        return [dict(d) for d in _cache["items"]]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = [dict(d) for d in data.get("domains", []) if isinstance(d, dict) and d.get("code")]
    except (OSError, ValueError, AttributeError):
        items = []
    if not items:
        items = [_builtin_entry(c) for c in BUILTIN_CODES]
    # 系统领域兜底：注册表被手工改坏时，归档 / 网页入库也不能失效
    codes = {d["code"] for d in items}
    for code in BUILTIN_CODES:
        if code in RESERVED_CODES and code not in codes:
            items.append(_builtin_entry(code))
    for d in items:
        if d["code"] in RESERVED_CODES:
            d["enabled"] = True
    _cache.update(key=key, items=items)
    return [dict(d) for d in items]


def all_codes() -> list[str]:
    """全部已登记编号（含停用）。数据层校验用——停用只拦新上传，已有数据照常有效。"""
    return [d["code"] for d in load_domains()]


def enabled_codes() -> list[str]:
    """可供上传 / 导入目录 / 网页入库选择的编号。"""
    return [d["code"] for d in load_domains() if d.get("enabled", True)]


def get_domain(code: str) -> dict | None:
    return next((d for d in load_domains() if d["code"] == code), None)


def display_name(code: str) -> str:
    """自定义名称；未设置返回空串（界面回落到内置多语言标签，再回落到编号）。"""
    entry = get_domain(code)
    return (entry or {}).get("name") or ""


def sanitize_choice(choice: str | None) -> str:
    """检索设置里保存的领域选择：编号已不存在时回落为 all，否则过滤条件永远检索不到内容。"""
    return choice if choice in all_codes() else "all"


def resolve_code(raw: str | None) -> str | None:
    """把外部传入的领域参数解析成编号：编号（不分大小写）、显示名称、内置领域的多语言名称。

    解析不出或名称有歧义时返回 None，由调用方明确报错——直接拿去过滤只会得到空结果，
    被 Agent 误报成「知识库里没有」。只在入口处用一次，此后全链路只认编号。
    """
    value = str(raw or "").strip().lower()
    if not value:
        return None
    entries = load_domains()
    if any(d["code"] == value for d in entries):
        return value
    from src.i18n import domain_labels_all_languages

    matches = [d["code"] for d in entries
               if value == (d.get("name") or "").lower()
               or value in {n.lower() for n in domain_labels_all_languages(d["code"])}]
    return matches[0] if len(matches) == 1 else None


# ---------------------------------------------------------------- 写入
def _save(items: list[dict]) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"domains": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, REGISTRY_PATH)
    _cache.update(key=None, items=None)


def validate_code(code: str) -> str:
    code = (code or "").strip()
    if not _CODE_RE.match(code):
        raise DomainError("领域编号需为 2~32 位：小写字母开头，只能包含小写字母、数字、下划线、连字符")
    if code in FORBIDDEN_CODES:
        raise DomainError(f"「{code}」是系统保留字，不能用作领域编号")
    return code


def _clean_name(name: str | None) -> str:
    name = (name or "").strip()
    if len(name) > _NAME_MAX:
        raise DomainError(f"领域名称最多 {_NAME_MAX} 个字")
    return name


def create_domain(code: str, name: str, description: str = "", *, store=None) -> dict:
    """新增领域。传入 store 时顺带纠正已入库卡片的领域字段（见 reconcile_domain_cards）。

    纠正失败会撤销这次新增：否则领域已存在、卡片却没改对，而且没有重试的入口。
    """
    code = validate_code(code)
    name = _clean_name(name)
    if not name:
        raise DomainError("请填写领域名称")
    with _lock:
        items = load_domains()
        if any(d["code"] == code for d in items):
            raise DomainError(f"领域编号「{code}」已存在")
        entry = {"code": code, "name": name, "description": (description or "").strip()[:_DESC_MAX],
                 "enabled": True, "builtin": False,
                 "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        items.append(entry)
        _save(items)
    if store is not None:
        try:
            reconcile_domain_cards(code, store)
        except Exception as exc:
            with _lock:
                _save([d for d in load_domains() if d["code"] != code])
            raise DomainError(f"向量库暂不可用，领域未创建，请稍后重试：{exc}") from exc
    return dict(entry)


def reconcile_domain_cards(code: str, store) -> int:
    """把「按当前注册表本该属于 code」的已入库文档的 domain 字段就地改过来，返回文档数。

    这些文档入库时 code 还没登记：目录推断被兜底成 reference，文件头声明被当成非法值忽略；
    不纠正的话，按新领域过滤永远检索不到它们。判定沿用入库时的优先级（文件头声明 > 目录），
    所以文件头明确声明了别的领域的不会被改，归档文档（archive/…）也保持原样。
    只改 payload，不重新向量化——路径、document_id、版本、卡片内容都不变。
    """
    from src.metadata import _valid_domain, infer_from_path, parse_front_matter

    knowledge_dir = Path(store.cfg.knowledge_dir)
    targets: set[str] = set()
    for doc in store.list_documents():
        doc_id, rel = doc.get("document_id"), doc.get("path") or ""
        if not doc_id or doc.get("domain") == code:
            continue
        front_matter: dict = {}
        path = knowledge_dir / rel
        if path.suffix.lower() == ".md":  # 只有 Markdown 有文件头声明（与解析器一致）
            try:
                front_matter, _ = parse_front_matter(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
        if (_valid_domain(front_matter.get("domain")) or infer_from_path(rel)["domain"]) == code:
            targets.add(doc_id)
    store.reassign_domain(sorted(targets), code)
    return len(targets)


def update_domain(code: str, *, name: str | None = None, description: str | None = None,
                  enabled: bool | None = None) -> dict:
    """只改显示信息与启停。编号不可改——它是数据身份（见模块说明）。"""
    with _lock:
        items = load_domains()
        entry = next((d for d in items if d["code"] == code), None)
        if entry is None:
            raise DomainError(f"领域「{code}」不存在")
        if name is not None:
            name = _clean_name(name)
            if not name and not entry.get("builtin"):
                raise DomainError("请填写领域名称")  # 内置领域可清空名称，恢复默认多语言标签
            entry["name"] = name
        if description is not None:
            entry["description"] = description.strip()[:_DESC_MAX]
        if enabled is not None:
            if not enabled and code in RESERVED_CODES:
                raise DomainError("系统领域（archive / reference）不能停用")
            entry["enabled"] = bool(enabled)
        _save(items)
    return dict(entry)


# ---------------------------------------------------------------- 数据量统计 / 删除
def _owner(rel: str) -> tuple[str, bool] | None:
    """路径属于哪个领域：(编号, 是否为从该领域归档出去的)。根目录散落文件返回 None。

    与入库时的领域推断一致：第一层文件夹名转小写即编号；archive/<编号>/… 是从该领域
    归档出去的文件——恢复时会回到原领域，所以也算该领域的数据。
    """
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    if len(parts) < 2:
        return None
    if parts[0].lower() == "archive" and len(parts) >= 3:
        return parts[1].lower(), True
    return parts[0].lower(), False


def _empty_stat() -> dict:
    return {"files": 0, "archived": 0, "ledger": 0, "documents": 0, "cards": 0}


def usage_by_domain(knowledge_dir: Path, ledger: dict, documents: list[dict]) -> dict[str, dict]:
    """每个编号下的数据量（删除前必查）。三处来源可能不一致，任何一处有数据都不能删：

    - files / archived：磁盘上的文件 / 从该领域归档出去的文件
    - ledger：入库台账里的记录
    - documents / cards：向量库里的文档版本与卡片（含 expired 历史版本）。按卡片自身的
      domain 字段统计——它才决定检索归属（文件头可声明与所在目录不同的领域）；从某领域
      归档出去的文档恢复时会回到原领域，也算原领域的数据
    """
    stats: dict[str, dict] = {}

    def bump(rel: str, field: str, n: int = 1) -> None:
        owner = _owner(rel)
        if owner is None:
            return
        code, archived = owner
        stat = stats.setdefault(code, _empty_stat())
        stat["archived" if (field == "files" and archived) else field] += n

    if knowledge_dir.exists():
        for p in knowledge_dir.rglob("*"):
            if p.is_file() and not p.name.startswith("."):
                bump(p.relative_to(knowledge_dir).as_posix(), "files")
    for rel in ledger:
        bump(rel, "ledger")
    for d in documents:
        owner = _owner(d.get("path") or "")
        codes = {d.get("domain")} | ({owner[0]} if owner and owner[1] else set())
        for code in codes - {None, "", "?"}:
            stat = stats.setdefault(code, _empty_stat())
            stat["documents"] += 1
            stat["cards"] += int(d.get("chunks") or 0)
    return stats


def has_data(stat: dict | None) -> bool:
    return bool(stat) and any(stat.values())


def delete_domain(code: str, stat: dict | None, knowledge_dir: Path | None = None) -> None:
    """删除领域：系统领域不可删；有任何数据都不可删（用户确认的规则）。

    删除只移除注册表条目，并清理该领域留下的空文件夹（绝不删除任何知识文件）。
    """
    if code in RESERVED_CODES:
        raise DomainError("系统领域（archive / reference）不能删除")
    if has_data(stat):
        raise DomainError("领域下仍有数据，不能删除：请先在知识库管理页移除其中的文档"
                          "（已归档的文档也算，因为恢复时会回到该领域）")
    with _lock:
        items = load_domains()
        if not any(d["code"] == code for d in items):
            raise DomainError(f"领域「{code}」不存在")
        _save([d for d in items if d["code"] != code])
    if knowledge_dir is not None:
        _remove_empty_tree(knowledge_dir / code)


def _remove_empty_tree(folder: Path) -> None:
    """只删空目录树（连同 .DS_Store / .gitkeep 这类占位文件）；有任何其他文件就原样保留。"""
    if not folder.is_dir():
        return
    for sub in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if sub.is_file() and sub.name in _JUNK_FILES:
            sub.unlink(missing_ok=True)
        elif sub.is_dir():
            try:
                sub.rmdir()
            except OSError:
                pass
    try:
        folder.rmdir()
    except OSError:
        pass


# ---------------------------------------------------------------- 未登记目录
def unregistered_dirs(knowledge_dir: Path) -> list[dict]:
    """knowledge/ 下有文件、却没登记为领域的第一层文件夹（入库时会被兜底归到 reference）。"""
    codes = set(all_codes())
    out: list[dict] = []
    if not knowledge_dir.exists():
        return out
    for folder in sorted(knowledge_dir.iterdir()):
        if not folder.is_dir() or folder.name.startswith(".") or folder.name.lower() in codes:
            continue
        files = sum(1 for p in folder.rglob("*") if p.is_file() and not p.name.startswith("."))
        if files:
            code = folder.name.lower()
            valid = bool(_CODE_RE.match(code)) and code not in FORBIDDEN_CODES
            out.append({"folder": folder.name, "code": code, "files": files, "valid_code": valid})
    return out


def register_directory(folder: str, name: str, store) -> dict:
    """把未登记目录登记为领域（已入库卡片的领域字段随之纠正，见 create_domain）。"""
    return create_domain(validate_code(folder.strip().lower()), name or folder, store=store)

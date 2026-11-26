"""Stable, lazily allocated directories for projects within knowledge domains."""
from __future__ import annotations

import logging
import re
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from src import auth_db, projects
from src.name_codes import pinyin_code

INFO_NAME = "_info.md"
# V3.9.2：未关联文件的托管目录由 public 改为 basic_knowledge，并按入库用户名再分一层。
# 旧约定的 public / public-unassigned 成为历史桶名——历史文件仍在其中，路径推断照常剥离。
BASIC_KNOWLEDGE_DIR = "basic_knowledge"
LEGACY_BUCKETS = ("public-unassigned", "public")
_PROJECT_MARKER = "<!-- rag-managed-directory: project -->"
_PUBLIC_MARKER = "<!-- rag-managed-directory: public -->"
_INFO_MARKERS = {_PROJECT_MARKER.encode("ascii"), _PUBLIC_MARKER.encode("ascii")}
_LOG = logging.getLogger(__name__)


def is_generated_info_content(name: str, content: bytes) -> bool:
    return (name.casefold() == INFO_NAME
            and content[:128].partition(b"\n")[0].rstrip() in _INFO_MARKERS)


def is_generated_info(path: Path) -> bool:
    if path.name.casefold() != INFO_NAME:
        return False
    try:
        with path.open("rb") as file:
            return is_generated_info_content(path.name, file.readline(128))
    except OSError:
        return False


def other_project_paths(ledger: dict, project_id: int | None) -> frozenset[Path]:
    return frozenset(Path(rel) for rel, entry in ledger.items()
                     if entry.get("project_id") != project_id)


def _directories(conn, domain: str) -> dict[int, str]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='project_directories'").fetchone():
        return {}
    return dict(conn.execute("SELECT project_id, dirname FROM project_directories WHERE domain=?", (domain,)))


def _basic_directory(knowledge_dir: Path, domain: str, registered: dict[int, str],
                     username: str | None = None) -> tuple[Path, Path]:
    """未关联文件的托管目录：<领域>/basic_knowledge/<入库用户名>/。

    basic_knowledge 为保留名（被项目历史占用时回退 -unassigned 后缀）。
    返回 (桶根, 落盘目录)——桶根放说明文件，落盘目录带用户名层；
    未关联上传必带登录用户，防御性兜底：用户名缺失时直接落桶根。"""
    name = BASIC_KNOWLEDGE_DIR
    if name in registered.values():
        name = f"{name}-unassigned"
    bucket = knowledge_dir / domain / name
    user = (username or "").strip()
    return bucket, (bucket / user if user else bucket)


def managed_directory_names() -> set[tuple[str, str]]:
    with closing(auth_db._connect()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='project_directories'").fetchone():
            return set()
        return {(domain, dirname) for domain, dirname in
                conn.execute("SELECT domain, dirname FROM project_directories")}


def without_storage_directory(relative_path: str, registered: set[tuple[str, str]]) -> str:
    """推断元数据时剥掉系统合成的托管目录层，保留用户原始层级。

    basic_knowledge 下还有一层入库用户名目录（同为合成层），一并剥离；
    旧约定的 public / public-unassigned 只有一层；项目登记目录同样剥离。"""
    parts = relative_path.split("/")
    domain_index = 1 if parts[0] == "archive" else 0
    domain = parts[domain_index]
    bucket_index = domain_index + 1
    if len(parts) > bucket_index + 1:
        bucket = parts[bucket_index]
        if bucket == BASIC_KNOWLEDGE_DIR:
            del parts[bucket_index]          # 托管桶层
            if len(parts) > bucket_index + 1:
                del parts[bucket_index]      # 入库用户名层
        elif bucket in LEGACY_BUCKETS or (domain, bucket) in registered:
            del parts[bucket_index]
    return "/".join(parts)


def _upload_domain_code(domain: str) -> str:
    from src.domains import validate_code

    domain = validate_code(domain)
    if domain == "archive":
        raise ValueError("请先入库，再通过知识库管理归档文件")
    return domain


def _destination(conn, knowledge_dir: Path, domain: str, project_id: int) -> tuple[Path, dict, bool]:
    parent = knowledge_dir / domain
    project = next(iter(projects._read_projects(conn, project_id)), None)
    if project is None:
        raise ValueError("项目不存在")
    registered = _directories(conn, domain)
    if project_id in registered:
        return parent / registered[project_id], project, True
    try:
        stem = pinyin_code(project["name"], prefix="project")
    except ValueError:
        stem = f"project-{project_id}"
    if re.fullmatch(r"con|prn|aux|nul|(?:com|lpt)[0-9]", stem):
        stem = f"project_{stem}"
    occupied = {BASIC_KNOWLEDGE_DIR, *LEGACY_BUCKETS,
                *(name.casefold() for name in registered.values())}
    if parent.is_dir():
        occupied.update(path.name.casefold() for path in parent.iterdir())
    name, suffix = stem, 1
    while name in occupied:
        name = f"{stem}-{suffix}"
        suffix += 1
    return parent / name, project, False


def preview_directory(knowledge_dir: Path, domain: str, project_id: int | None,
                      *, username: str | None = None) -> Path:
    domain = _upload_domain_code(domain)
    with closing(auth_db._connect()) as conn:
        if project_id is None:
            _, target = _basic_directory(knowledge_dir, domain, _directories(conn, domain), username)
            return target
        return _destination(conn, knowledge_dir, domain, project_id)[0]


def _write_project_info(directory: Path, project: dict) -> None:
    path = directory / INFO_NAME
    if path.is_symlink() or (path.exists() and not is_generated_info(path)):
        raise ValueError(f"项目目录中的 {INFO_NAME} 已被占用")
    participants = "、".join(dep["name"] for dep in project["participants"])
    text = (
        f"{_PROJECT_MARKER}\n\n# {project['name']}\n\n"
        f"- 项目 ID：{project['id']}\n"
        f"- 业务方向：{project['direction_name'] or '未设置'}\n"
        f"- 牵头部门：{project['lead_department_name'] or '未设置'}\n"
        f"- 参与部门：{participants or '未设置'}\n\n"
        f"## 项目说明\n\n{project['note'] or '暂无说明'}\n"
    )
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    _write_info(path, text)


def _write_public_info(directory: Path) -> None:
    path = directory / INFO_NAME
    if directory.is_dir() and not path.exists() and not path.is_symlink():
        _write_info(path, f"{_PUBLIC_MARKER}\n\n# 未关联项目的资料\n\n"
                          "本目录存放入库时未选择项目的资料。可见范围由各文件的权限设置决定。\n")


def _write_info(path: Path, text: str) -> None:
    with NamedTemporaryFile(dir=path.parent, prefix=".info-", delete=False) as file:
        temp = Path(file.name)
        try:
            file.write(text.encode("utf-8"))
            file.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def _validate_directory(knowledge_dir: Path, directory: Path) -> None:
    if directory.resolve() != knowledge_dir.resolve() / directory.relative_to(knowledge_dir):
        raise ValueError("入库目录不能通过符号链接访问")
    if directory.exists() and not directory.is_dir():
        raise ValueError(f"入库目录 {directory.name} 已被同名文件占用")


@contextmanager
def upload_directory(knowledge_dir: Path, domain: str, project_id: int | None,
                     *, expected: Path | None = None, username: str | None = None):
    """Serialize allocation and writes. The caller creates folders only when saving."""
    domain = _upload_domain_code(domain)
    with closing(auth_db._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if project_id is None:
            bucket, directory = _basic_directory(
                knowledge_dir, domain, _directories(conn, domain), username)
            _validate_directory(knowledge_dir, directory)
            if expected is not None and directory != expected:
                raise ValueError("上传目录已变更，请重新确认上传")
            yield directory
            _write_public_info(bucket)  # 说明文件放桶根，描述整桶用途
            return
        directory, project, registered = _destination(conn, knowledge_dir, domain, project_id)
        if expected is not None and directory != expected:
            raise ValueError("项目上传目录已变更，请重新确认上传")
        _validate_directory(knowledge_dir, directory)
        info = directory / INFO_NAME
        if info.is_symlink() or (info.exists() and not is_generated_info(info)):
            raise ValueError(f"项目目录中的 {INFO_NAME} 已被占用")
        try:
            yield directory
        finally:
            if directory.is_dir() and (registered or any(directory.iterdir())):
                if not registered:
                    conn.execute(auth_db.PROJECT_DIRECTORIES_SCHEMA)
                    conn.execute("INSERT INTO project_directories VALUES (?, ?, ?)",
                                 (domain, project_id, directory.name))
                # Save ownership even when a batch partially writes or the info file fails.
                try:
                    _write_project_info(directory, project)
                finally:
                    conn.commit()


def refresh_project_info(knowledge_dir: Path, project_id: int) -> None:
    with closing(auth_db._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='project_directories'").fetchone():
            return
        project = next(iter(projects._read_projects(conn, project_id)), None)
        if project is None:
            return
        for domain, dirname in conn.execute(
            "SELECT domain, dirname FROM project_directories WHERE project_id=?", (project_id,)
        ):
            path = knowledge_dir / domain / dirname
            try:
                _validate_directory(knowledge_dir, path)
                if path.is_dir():
                    _write_project_info(path, project)
            except (OSError, ValueError):
                _LOG.warning("Could not refresh project info for %s", path, exc_info=True)

"""V3.8：上传 / 目录导入的「重复文档」防护——冲突预检 + 执行（纯逻辑，可单测）。

用户确认的规则：
1. 内容与知识库已有文档完全相同（任意位置，MD5 比对入库台账）→ 默认跳过；
   相同内容在别的位置时，可选「仍作为独立新文档入库」。
2. 同一位置已有同名文件、内容不同 → 默认作为新版本：覆盖源文件，入库流水线据此
   识别为 updated（旧版本转 expired、版本号 +0.1）；可选「另存为副本」。
3. 知识库别处有同名文件、内容不同 → 只有一个候选时默认作为它的新版本；多个候选
   不猜，默认独立新文档。可选任一候选的新版本 / 独立新文档。
4. 全新文件 → 直接入库（无冲突时上传流程与以前完全一样，不多一步）。
5. 同一批次内：内容重复的后者跳过；同名不同内容的后者另存为副本，避免互相覆盖。

目录导入：同名目录已存在时默认「合并」——文件写回原目录（内容未变的不重写），
入库走增量判断（没变跳过 / 改了升版本 / 新增入库）；源目录里已删除的文件保留不动。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# ---- 动作 ----
SKIP = "skip"                # 不写盘、不入库
NEW_VERSION = "new_version"  # 覆盖同一位置的文件 → 新版本
VERSION_OF = "version_of:"   # 前缀 + 已有文档相对路径：覆盖该文档 → 它的新版本
NEW = "new"                  # 独立新文档（目标位置被占时自动改名）
COPY = "copy"                # 另存为副本（总是改名，不碰原文件）

# ---- 冲突类型 ----
KIND_NEW = "new"
KIND_DUPLICATE = "duplicate"            # 知识库里已有完全相同的内容
KIND_SAME_PATH = "same_path"            # 同一位置有同名文件，内容不同
KIND_SAME_NAME = "same_name"            # 别处有同名文件，内容不同
KIND_BATCH_DUPLICATE = "batch_duplicate"  # 本批次内容重复
KIND_BATCH_SAME_NAME = "batch_same_name"  # 本批次同名不同内容


def content_hash(data: bytes) -> str:
    """与入库台账一致的内容指纹（index_state.file_hash 同为 MD5）。"""
    return hashlib.md5(data).hexdigest()


@dataclass
class UploadDecision:
    name: str                 # 上传的文件名
    target: str               # 表单所选目标位置（相对知识目录，posix 风格）
    digest: str
    kind: str
    action: str               # 默认动作
    options: list[str]        # 可选动作（第一个不一定是默认）
    existing: list[str] = field(default_factory=list)  # 相关的已有文档（相对路径）

    @property
    def needs_confirmation(self) -> bool:
        """全新文件不打扰用户；其余都进确认面板。"""
        return self.kind != KIND_NEW


def plan_uploads(files: list[tuple[str, bytes]], target_dir: str,
                 ledger: dict, knowledge_dir: Path) -> list[UploadDecision]:
    """为每个上传文件给出冲突类型与默认动作（不写盘）。

    files：[(文件名, 内容)]，顺序即执行顺序；target_dir：表单选择的目标目录
    （相对知识目录，如 "learning/general"）；ledger：入库台账 {相对路径: {"hash", …}}。
    """
    target_dir = target_dir.strip("/")
    by_hash: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = {}
    for rel, entry in ledger.items():
        if entry.get("hash"):
            by_hash.setdefault(entry["hash"], []).append(rel)
        by_name.setdefault(PurePosixPath(rel).name, []).append(rel)

    decisions: list[UploadDecision] = []
    batch_hashes: set[str] = set()
    batch_targets: set[str] = set()  # 本批次将写到「表单目标位置」的文件
    for name, data in files:
        digest = content_hash(data)
        target = f"{target_dir}/{name}" if target_dir else name

        if digest in batch_hashes:
            decisions.append(UploadDecision(name, target, digest, KIND_BATCH_DUPLICATE,
                                            SKIP, [SKIP]))
            continue
        batch_hashes.add(digest)

        same_content = sorted(by_hash.get(digest, []))
        if same_content:
            # 同一位置同内容：只能跳过（再入库也是原样）；别处同内容：可强制作为新文档
            options = [SKIP] if target in same_content else [SKIP, NEW]
            decisions.append(UploadDecision(name, target, digest, KIND_DUPLICATE,
                                            SKIP, options, same_content))
            continue

        if target in batch_targets:
            decisions.append(UploadDecision(name, target, digest, KIND_BATCH_SAME_NAME,
                                            COPY, [COPY]))
            continue

        if (knowledge_dir / target).exists() or target in ledger:
            decisions.append(UploadDecision(name, target, digest, KIND_SAME_PATH,
                                            NEW_VERSION, [NEW_VERSION, COPY], [target]))
            batch_targets.add(target)
            continue

        others = sorted(r for r in by_name.get(name, []) if r != target)
        if others:
            options = [VERSION_OF + r for r in others] + [NEW]
            default = options[0] if len(others) == 1 else NEW
            decisions.append(UploadDecision(name, target, digest, KIND_SAME_NAME,
                                            default, options, others))
            if default == NEW:
                batch_targets.add(target)
            continue

        decisions.append(UploadDecision(name, target, digest, KIND_NEW, NEW, [NEW]))
        batch_targets.add(target)
    return decisions


def available_path(parent: Path, stem: str, suffix: str = "") -> Path:
    """不覆盖任何已有文件/目录的落盘位置：已存在时依次尝试 stem-2、stem-3 …"""
    candidate = parent / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    index = 2
    while (parent / f"{stem}-{index}{suffix}").exists():
        index += 1
    return parent / f"{stem}-{index}{suffix}"


def execute_uploads(decisions: list[UploadDecision], files: list[tuple[str, bytes]],
                    knowledge_dir: Path,
                    choices: dict[int, str] | None = None) -> tuple[list[Path], list[UploadDecision]]:
    """按决策落盘。返回 (需要入库的文件路径, 被跳过的决策)。

    choices：{序号: 动作}，用户在确认面板上的选择；不在该文件可选项里的值一律
    回落到默认动作（防止伪造选择写到任意路径——VERSION_OF 的路径只能来自台账）。
    """
    to_ingest: list[Path] = []
    skipped: list[UploadDecision] = []
    for index, (decision, (_name, data)) in enumerate(zip(decisions, files)):
        action = (choices or {}).get(index, decision.action)
        if action not in decision.options:
            action = decision.action
        if action == SKIP:
            skipped.append(decision)
            continue
        target = knowledge_dir / decision.target
        if action == NEW_VERSION:
            dest = target
        elif action.startswith(VERSION_OF):
            dest = knowledge_dir / action[len(VERSION_OF):]
        else:  # NEW / COPY：都不覆盖已有文件；NEW 在目标空闲时就用原名
            dest = available_path(target.parent, target.stem, target.suffix)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        to_ingest.append(dest)
    return to_ingest, skipped


def write_directory_import(grouped: dict[str, list[tuple[tuple[str, ...], bytes]]],
                           domain_dir: Path, merge: bool) -> tuple[list[Path], list[str]]:
    """把浏览器选中的目录写入领域目录。返回 (导入涉及的全部文件路径, 实际顶层目录名)。

    merge=True：写回同名目录；内容未变的文件不重写（保留修改时间），交给入库的
    增量判断跳过。源目录里已删除、但知识库目录里还在的文件不动（默认保留）。
    merge=False：已有同名目录时另存为副本目录（原行为）。
    """
    saved: list[Path] = []
    roots: list[str] = []
    for root_name, entries in grouped.items():
        root = domain_dir / root_name if merge else available_path(domain_dir, root_name)
        roots.append(root.name)
        for parts, content in entries:
            dest = root.joinpath(*parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not (dest.exists() and dest.read_bytes() == content):
                dest.write_bytes(content)
            saved.append(dest)
    return saved, roots

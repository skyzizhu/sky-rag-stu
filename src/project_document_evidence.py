"""Find literal mentions in registered projects' requested document types."""
from __future__ import annotations

import re
import time
import unicodedata
from pathlib import PurePosixPath

from src.document_type import infer_doc_type_from_filename
from src.projects import list_projects


_ASK = re.compile(
    r"(?:请(?:帮我)?|帮我)?(?:知识库[里中内])?(?:有)?哪些项目的"
    r"(?P<types>[^，,。？！?!；;\n]{2,60}?)"
    r"(?:[里中内])?(?:提到|提及)(?:了)?(?P<topic>[^？?。！，,；;\n]{2,100})[？?。]?")
_TERM = re.compile(r"[\w·-]{2,32}(?: [\w·-]{1,32}){0,3}")
_UNSUPPORTED = re.compile(r"不含|没有|未提|不涉及|不(?:包含|包括)|除外|排除|同时|以及|和|与|且|什么|哪些")
_PROJECT_FAMILIES = {"立项": r"(?:项目)?立项(?:报告|申请书|申请|文件|文档|材料|资料)?",
                     "章程": r"(?:项目)?章程(?:文件|文档)?"}
_PROJECT_TYPE = re.compile(r"(?:项目)?(立项(?:报告|申请书|申请)?|章程)(?:文件|文档|材料|资料)?")
_FILE_END = r"(?:[\s_.（(-]*(?:v\d+(?:\.\d+)*|\d{4}[-_]\d{2}[-_]\d{2})[)）]?)?$"
_METADATA_LINE = re.compile(r"^\s*(?:来源文档\s*[:：]|#{1,6}\s)")


def parse_project_mentions(question: str) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Limit exact scans to a complete literal question with an optional OR."""
    match = _ASK.fullmatch(unicodedata.normalize("NFKC", question).strip())
    if not match:
        return None
    topic = re.sub(r"(?:的(?:相关)?|相关)(?:要求|规定|内容)$", "", match["topic"])
    terms = tuple(part.strip(' 「」『』“”"') for part in re.split(r"或(?:者)?", topic))
    if any(not _TERM.fullmatch(term) or _UNSUPPORTED.search(term) for term in terms):
        return None
    types = []
    for part in re.split(r"或(?:者)?", match["types"]):
        label = part.strip(' 「」『』“”"')
        if project_type := _PROJECT_TYPE.fullmatch(label):
            label = project_type[1]
        elif not label or infer_doc_type_from_filename(label) != label:
            return None
        types.append(label)
    return tuple(dict.fromkeys(types)), tuple(dict.fromkeys(terms))


def _body_quote(text: str, targets: re.Pattern) -> str:
    for line in text.splitlines():
        if _METADATA_LINE.match(line):
            continue
        for sentence in re.split(r"(?<=[。！？；])", line):
            if targets.search(unicodedata.normalize("NFKC", sentence)):
                return sentence.strip()
    return ""


def scan_project_mentions(store, filters, types, terms, cfg):
    """Reuse the same visibility filter for document discovery and body reads."""
    names = {project["id"]: project["name"] for project in list_projects()}
    targets = re.compile("|".join(
        (r"(?<![a-z0-9_])" if term[0].isascii() else "") + re.escape(term) +
        (r"(?![a-z0-9_])" if term[-1].isascii() else "") for term in terms), re.I)
    labels = "|".join(_PROJECT_FAMILIES.get(label) or
                      (r"(?:项目)?" if label.startswith("立项") else "") + re.escape(label)
                      for label in types)
    declared_type = re.compile(rf"(?:{labels})$")
    filename_type = re.compile(rf"(?:{labels}){_FILE_END}", re.I)

    def eligible(meta):
        if meta.get("project_id") not in names or not meta.get("document_id"):
            return False
        label = str(meta.get("doc_type") or "").strip()
        path = (meta.get("path") or meta.get("source") or "").replace("\\", "/")
        stem = PurePosixPath(path).stem
        return bool(declared_type.fullmatch(label) or
                    not label and filename_type.search(unicodedata.normalize("NFKC", stem)))

    deadline = time.monotonic() + cfg.enum_max_seconds
    catalog = store.document_catalog(filters, cfg.enum_catalog_docs, cfg.enum_catalog_points,
                                     select_document=eligible)
    documents = catalog.pop("documents")
    expected_projects = {doc["project_id"] for doc in documents}
    matches = {}
    scanned = 0
    offset = None
    limited = catalog["scan_truncated"] or catalog["list_truncated"]
    if documents:
        scoped = {**filters, "document_id": [doc["document_id"] for doc in documents]}
        while scanned < cfg.enum_max_cards:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                limited = True
                break
            rows, offset = store.card_page(scoped, offset,
                                           min(128, cfg.enum_max_cards - scanned), timeout=remaining)
            scanned += len(rows)
            for row in rows:
                payload = row["payload"]
                project_id = payload.get("project_id")
                if project_id not in matches and eligible(payload):
                    if quote := _body_quote(payload.get("text") or "", targets):
                        matches[project_id] = (names[project_id], quote, row)
            if offset is None or matches.keys() == expected_projects:
                break
        limited |= offset is not None and matches.keys() != expected_projects
    ordered = sorted(matches.values(), key=lambda match: match[0])
    limited |= len(ordered) > cfg.enum_max_items
    return ordered[:cfg.enum_max_items], {
        "candidate_documents": catalog["matched_documents"], "scanned_cards": scanned,
        "coverage": "partial" if limited else "complete",
    }

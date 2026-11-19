"""正式文档版本的保守识别：明确声明、可核对来源，不改变入库修订编号。"""
from __future__ import annotations

import re
import unicodedata

from src.metadata import (
    _CHINESE_FILENAME_VERSION_RE, _EXPLICIT_FILENAME_VERSION_RE,
    _normalize_filename_version,
)

# 必须独立成行或表格单元格；正文中的产品版本、引用版本、日期不作为文档声明。
_DECLARATION_RE = re.compile(
    r"^\s*(?:当前\s*)?(?:文档版本(?:号)?|版本号|版本|版次|document\s+version)"
    r"\s*[:：=]\s*(?:v(?:ersion)?\s*)?(?P<number>\d+(?:[._-]\d+){0,3})\s*$",
    re.IGNORECASE,
)
_LABEL_RE = re.compile(
    r"^\s*(?:当前\s*)?(?:文档版本(?:号)?|版本号|版本|版次|document\s+version)\s*[:：=]\s*(\S.*)$",
    re.IGNORECASE,
)
_VALUE_RE = re.compile(r"(?:v(?:ersion)?\s*)?(\d+(?:[._-]\d+){0,3})", re.IGNORECASE)
FIELDS = ("document_version", "document_version_status", "document_version_source",
          "document_version_evidence")


def normalize_version(value: str) -> str | None:
    value = unicodedata.normalize("NFKC", str(value)).strip()
    match = _VALUE_RE.fullmatch(value)
    return _normalize_filename_version(match.group(1)) if match else None


def _candidate(value: str | None, source: str, location: str, text: str,
               authoritative: bool = True) -> dict:
    return {"value": value, "source": source, "location": location,
            "text": text[:200], "authoritative": authoritative}


def detect_document_version(source: str, regions: list[dict], front_matter: dict | None = None) -> dict:
    """只合并明确候选；多值冲突不选最大值，OCR线索必须人工确认。"""
    from pathlib import Path

    evidence: list[dict] = []
    stem = unicodedata.normalize("NFKC", Path(source).stem)
    for pattern in (_EXPLICIT_FILENAME_VERSION_RE, _CHINESE_FILENAME_VERSION_RE):
        for match in pattern.finditer(stem):
            raw = match.group("number")
            value = _normalize_filename_version(raw)
            if re.match(r"[._-]\d", stem[match.end("number"):]):
                value = None
            item = _candidate(value, "filename", "文件名", match.group(0))
            if item not in evidence:
                evidence.append(item)
    fm = front_matter or {}
    value = fm.get("document_version", fm.get("version"))
    if value is not None:
        evidence.append(_candidate(normalize_version(str(value)), "front_matter", "文件头",
                                   f"version: {value}"))
    for region in regions:
        # 不扫描全篇；表格单元格可单独声明，也支持“版本号 | 1.2”两格。
        fence = ""
        fence_length = 0
        for line in str(region.get("text", ""))[:4000].splitlines():
            line = unicodedata.normalize("NFKC", line).strip()
            marker = re.match(r"^(`{3,}|~{3,})", line)
            if marker:
                if not fence:
                    fence, fence_length = marker.group(1)[0], len(marker.group(1))
                elif marker.group(1)[0] == fence and len(marker.group(1)) >= fence_length:
                    fence = ""
                continue
            if fence:
                continue
            # 支持规范标题中的版本声明，不将任意标题数字当版本。
            line = re.sub(r"^#{1,6}\s+", "", line)
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line) if c.strip()]
            candidates = [line] + cells
            for index in range(len(cells) - 1):
                if (re.fullmatch(r"(?:当前\s*)?(?:文档版本(?:号)?|版本号|版本|版次|document\s+version)", cells[index], re.I)
                        and re.match(r"(?:v(?:ersion)?\s*)?\d", cells[index + 1], re.I)):
                    candidates.append(cells[index] + ": " + cells[index + 1])
            for text in candidates:
                match = _DECLARATION_RE.fullmatch(text)
                label_match = _LABEL_RE.fullmatch(text) if "|" not in text else None
                if match or label_match:
                    item = _candidate(_normalize_filename_version(match.group("number")) if match else None,
                                      region.get("source", "content"), region.get("location", "开头文本"),
                                      text, bool(region.get("authoritative", True)))
                    if item not in evidence:
                        evidence.append(item)
    values = {e["value"] for e in evidence if e["value"] is not None}
    certain = {e["value"] for e in evidence if e["authoritative"] and e["value"] is not None}
    if evidence and (len(values) != 1 or any(e["value"] is None for e in evidence)):
        version, status = "", "needs_review"
    elif not certain and evidence:
        version, status = "", "needs_review"
    elif certain:
        version = next(iter(certain))
        status = "filename_only" if all(e["source"] == "filename" for e in evidence if e["authoritative"]) else "declared"
    else:
        version, status = "", "undeclared" if regions or fm else "unavailable"
    sources = list(dict.fromkeys(e["location"] for e in evidence))
    return {"document_version": version, "document_version_status": status,
            "document_version_source": " / ".join(sources),
            "document_version_evidence": evidence[:32]}


def apply_confirmation(metadata: dict, confirmation: dict | None,
                       file_hash: str, revision: str) -> dict:
    """确认仅对当前内容指纹和入库版本有效，修改后自动重新核对。"""
    if confirmation and confirmation.get("hash") == file_hash and confirmation.get("revision") == revision:
        version = normalize_version(confirmation.get("value", ""))
        if version:
            return {**metadata, "document_version": version, "document_version_status": "confirmed",
                    "document_version_source": "manual"}
    return metadata

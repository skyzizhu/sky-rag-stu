"""把大模型偶尔连写的编号内容整理成可读的 Markdown 列表。"""

from __future__ import annotations

import re

_FENCED_CODE_RE = re.compile(r"(```[\s\S]*?```)")
_NUMBERED_MARKER_RE = re.compile(r"(?<!\d)(\d{1,2})(?:[、）)]|[.．](?!\d))\s*")
_ANSWER_CITATION_RE = re.compile(
    r"```[\s\S]*?(?:```|$)|`[^`\n]*`|(?<![\\!\[])\[(?P<rank>[0-9]{1,9})\](?![\](])"
)
ANSWER_STATUS_PREFIX = "[[answer_status:"
_ANSWER_STATUS_LINE = re.compile(r"\[\[answer_status:(answered|partial|insufficient_evidence)\]\]")


def split_answer_status(text: str, *, final: bool = False) -> tuple[str | None, str]:
    """Read an optional first-line status without exposing it as answer text."""
    prefix = text.lstrip()
    if not final and "\n" not in prefix and len(text) < 80 and (
            ANSWER_STATUS_PREFIX.startswith(prefix) or prefix.startswith(ANSWER_STATUS_PREFIX)):
        return None, text
    line, _, body = prefix.partition("\n")
    match = _ANSWER_STATUS_LINE.fullmatch(line.strip())
    return (match[1], body) if match else ("", text)


def answer_citations(text: str) -> set[int]:
    return {int(match["rank"]) for match in _ANSWER_CITATION_RE.finditer(text) if match["rank"]}


def remove_invalid_citations(text: str, ranks: set[int]) -> str:
    return _ANSWER_CITATION_RE.sub(
        lambda match: "" if match["rank"] and int(match["rank"]) not in ranks else match[0], text)


def _format_text_block(text: str) -> str:
    matches = list(_NUMBERED_MARKER_RE.finditer(text))
    valid_indices: set[int] = set()
    index = 0
    while index < len(matches):
        if int(matches[index].group(1)) != 1:
            index += 1
            continue
        end = index + 1
        expected = 2
        while end < len(matches) and int(matches[end].group(1)) == expected:
            expected += 1
            end += 1
        if end - index >= 2:
            valid_indices.update(range(index, end))
        index = max(end, index + 1)

    if not valid_indices:
        return text

    output: list[str] = []
    cursor = 0
    for match_index, match in enumerate(matches):
        if match_index not in valid_indices:
            continue
        output.append(text[cursor:match.start()])
        # 清掉编号前多余的横向空格，避免生成带尾空格的 Markdown 行。
        if output:
            output[-1] = output[-1].rstrip(" \t")
        before = "".join(output)
        if not before or before.endswith("\n"):
            separator = ""
        elif int(match.group(1)) == 1:
            separator = "\n\n"
        else:
            separator = "\n"
        output.append(f"{separator}{match.group(1)}. ")
        cursor = match.end()
    output.append(text[cursor:])
    return "".join(output)


def format_answer_markdown(text: str) -> str:
    """格式化连续编号列表，同时保持代码块和小数版本号不变。"""
    if not text:
        return ""
    blocks = _FENCED_CODE_RE.split(text)
    return "".join(
        block if block.startswith("```") else _format_text_block(block)
        for block in blocks
    )

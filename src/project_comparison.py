"""Parse explicit project comparisons and compute results from cited evidence."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

from src.projects import PROJECT_SUFFIXES


_PROJECT = "(?:" + "|".join(PROJECT_SUFFIXES) + ")"
_QUOTE = re.compile(r'「([^」]+)」|『([^』]+)』|“([^”]+)”|"([^"]+)"')
_INTRO = re.compile(r"^\s*(?:请|請)?\s*(?:帮我|幫我)?\s*(?:对比|對比|比较|比較|查询|查詢|查一下|查|计算|計算)?\s*")
_COUNT = r"(?:[0-9]+|[一二两兩三四五六七八九十]+)\s*[个個]"
_FACT_START = r"(?:各自的|分别的|分別的)?(?:项目|項目)?(?:负责人|負責人|预算|預算)"
_PROJECT_LOCATIVE = r"里面|裡面|之间|之間|里|裡|中|内|內"
_LIST_END = re.compile(
    rf"(?P<count>{_COUNT})\s*{_PROJECT}\s*(?:{_PROJECT_LOCATIVE}|的\s*(?={_FACT_START}))"
    rf"|(?<={_PROJECT})(?:{_PROJECT_LOCATIVE})"
    rf"|的\s*(?={_FACT_START})"
)
_JOIN = re.compile(r"、|和|与|與|及|以及|(?<=\S),\s*(?=\S)")
_NAME = re.compile(r"[\w .·-]{1,80}")
_SUM = re.compile(r"加起来|加起來|合计|合計|合共|总和|總和|总共|總共")
_MAX = re.compile(r"最高|最大|最多|更高")
_MIN = re.compile(r"最低|最小|最少|更低")
_OWNER_ASK = re.compile(r"[负負][责責]人")
_BUDGET_ASK = re.compile(r"[预預]算")
_REQUEST_WORDS = re.compile(
    rf"\s|[，,。？！?!；;：:]|(?:这|這|该|該|上述)\s*(?:{_COUNT}\s*)?{_PROJECT}"
    rf"|(?:哪[个個一])?{_PROJECT}|[负負][责責]人|[预預]算(?:[总總][额額]|金[额額])?"
    r"|分别|分別|各自|对比|對比|比较|比較|加起来|加起來|合计|合計|合共|总和|總和|总共|總共"
    r"|最高|最大|最多|最低|最小|最少|更高|更低|多少|是|的|为|為|谁|誰|哪[个個一]|[万萬]元|元|呢"
)
_AMOUNT = r"(?:[0-9]{1,3}(?:,[0-9]{3}){1,3}|[0-9]{1,12})(?:\.[0-9]{1,6})?"
_FIELD_START = r"(?:^|[，,。；;：:、|#])\s*(?:[-*+]\s+)?"
_BUDGET = re.compile(
    rf"{_FIELD_START}(?:项目|項目|立项|立項|当前|當前|最新)?[总總]?[预預]算"
    rf"(?:[总總][额額]|金[额額])?\s*(?:[为為是]\s*)?[:：=]?\s*(?:(?:[￥¥]|人民币|人民幣)\s*)?"
    rf"(?P<amount>{_AMOUNT})\s*(?P<unit>[亿億万萬]?元)(?![\w.])"
)
_PERSON = r"(?:[\u3400-\u9fff]{2,6}|[A-Za-z][A-Za-z .'-]{1,48})"
_OWNER_LABEL = re.compile(
    rf"{_FIELD_START}(?:项目|項目)?[负負][责責]人\s*(?:[为為是]\s*[:：=]?|[:：=])\s*"
    rf"(?P<name>{_PERSON}(?:\s*、\s*{_PERSON}){{0,3}})(?=\s*(?:[，,。；;|\n(（]|$))"
)
_OWNER_ROLE = re.compile(rf"{_FIELD_START}(?P<name>{_PERSON})\s*\((?:项目|項目)[负負][责責]人\)")
_MARKDOWN = re.compile(r"([\\`*_{}\[\]<>])")
_CHUNK_END = re.compile(r"[\r\n。；;！？!?]")
MAX_COMPARISON_PROJECTS = 4
_DEPARTMENT_COMPARISON = re.compile(
    rf"(?P<department>[^，,。？！?!；;的]{{1,60}}?)(?:参与|參與)了?(?:哪些|哪[几幾]个){_PROJECT}"
    rf"[？！?。；;,，]\s*(?:(?:参与|參與)的|[这這]些){_PROJECT}(?:{_PROJECT_LOCATIVE})"
    r"(?P<request>[^。？！?!]+)[。？！?!]?"
)


@dataclass(frozen=True)
class ProjectComparison:
    names: tuple[str, ...]
    budget: bool
    owner: bool
    operations: tuple[str, ...]
    valid_count: bool
    explicit_scope: bool
    department: str = ""

    @property
    def search_query(self) -> str:
        return " ".join(label for selected, label in
                        ((self.budget, "项目预算金额"), (self.owner, "项目负责人")) if selected)


def parse_project_comparison(question: str) -> ProjectComparison | None:
    """Accept named conjunctions with supported facts; leave other scopes to the regular validator."""
    text = _INTRO.sub("", unicodedata.normalize("NFKC", question), count=1)
    if roster := _DEPARTMENT_COMPARISON.fullmatch(text.strip()):
        request = roster["request"]
        operations = tuple(key for pattern, key in ((_SUM, "sum"), (_MAX, "max"), (_MIN, "min"))
                           if pattern.search(request))
        if operations and _BUDGET_ASK.search(request) and not _REQUEST_WORDS.sub("", request):
            return ProjectComparison((), True, False, operations, True, True, roster["department"])
    masked = _QUOTE.sub(lambda m: "@" * len(m[0]), text)
    boundary = _LIST_END.search(masked)
    if boundary is None:
        return None
    head, request = text[:boundary.start()], text[boundary.end():]
    separators = list(_JOIN.finditer(masked[:boundary.start()]))
    if not separators or _REQUEST_WORDS.sub("", request):
        return None
    names = []
    explicit_scope = bool(boundary["count"]) or bool(re.search(_PROJECT, masked[:boundary.start()]))
    start = 0
    for end, following in [(m.start(), m.end()) for m in separators] + [(len(head), len(head))]:
        value = head[start:end].strip()
        quote = _QUOTE.match(value)
        if quote and value[quote.end():].strip() in ("", *PROJECT_SUFFIXES):
            value = next(group for group in quote.groups() if group is not None).strip()
        elif not _NAME.fullmatch(value):
            return None
        if not 0 < len(value) <= 80:
            return None
        names.append(value)
        start = following
    budget, owner = bool(_BUDGET_ASK.search(request)), bool(_OWNER_ASK.search(request))
    operations = tuple(key for pattern, key in ((_SUM, "sum"), (_MAX, "max"), (_MIN, "min"))
                       if pattern.search(request))
    if not (budget or owner) or operations and not budget:
        return None
    # A comma may just separate introductory context from an ordinary question.
    if not explicit_scope and not (
            operations or re.search(r"对比|對比|比较|比較|分别|分別|各自", question)
            or any(not match[0].startswith(",") for match in separators)):
        return None
    count = boundary["count"]
    declared = None
    if count:
        counts = dict(zip("一二两兩三四", (1, 2, 2, 2, 3, 4)))
        number = count[:-1].strip()
        declared = int(number) if number.isdecimal() and len(number) <= 2 else counts.get(number, -1)
    valid_count = 2 <= len(names) <= MAX_COMPARISON_PROJECTS and declared in (None, len(names))
    return ProjectComparison(tuple(names), budget, owner, operations, valid_count, explicit_scope)


def project_facts(sources: list[dict]) -> tuple[dict[Decimal, set[int]], dict[str, set[int]]]:
    """Collect explicit RMB budgets and project owners with their source numbers."""
    budgets: dict[Decimal, set[int]] = {}
    owners: dict[str, set[int]] = {}
    for source in sources:
        text = unicodedata.normalize("NFKC", source.get("text") or "")
        for line in _CHUNK_END.split(text):
            for match in _BUDGET.finditer(line):
                unit = match["unit"]
                scale = 100_000_000 if unit[0] in "亿億" else 10_000 if unit[0] in "万萬" else 1
                value = Decimal(match["amount"].replace(",", "")) * scale
                budgets.setdefault(value, set()).add(source["rank"])
            for pattern in (_OWNER_LABEL, _OWNER_ROLE):
                for match in pattern.finditer(line):
                    owners.setdefault(match["name"].strip(), set()).add(source["rank"])
    return budgets, owners


def _yuan(value: Decimal) -> str:
    amount, unit = (value / 10_000, " 万元") if value >= 10_000 else (value, " 元")
    return format(amount.normalize(), "f") + unit


def _citations(ranks: set[int]) -> str:
    return " " + "".join(f"[{rank}]" for rank in sorted(ranks))


def render_project_comparison(comparison: ProjectComparison, projects: list[tuple[str, list[dict]]]) -> tuple[str, list[str]]:
    lines = []
    missing = []
    budgets: dict[str, Decimal] = {}
    budget_ranks: set[int] = set()
    for name, sources in projects:
        amounts, owners = project_facts(sources)
        fields = []
        if comparison.owner:
            if len(owners) == 1:
                owner, ranks = next(iter(owners.items()))
                fields.append(f"负责人 {owner}" + _citations(ranks))
            else:
                missing.append(f"{name}的负责人")
                fields.append("负责人证据有冲突" if owners else "找不到可确认的负责人")
        if comparison.budget:
            if len(amounts) == 1:
                amount, ranks = next(iter(amounts.items()))
                budgets[name] = amount
                budget_ranks.update(ranks)
                fields.append(f"预算 {_yuan(amount)}" + _citations(ranks))
            else:
                missing.append(f"{name}的预算")
                fields.append("预算证据有冲突" if amounts else "找不到可确认的预算")
        display_name = _MARKDOWN.sub(r"\\\1", name)
        lines.append(f"- {display_name}：{'，'.join(fields)}。")
    if comparison.operations and len(budgets) != len(projects):
        lines.append("\n部分项目缺少可确认的预算，无法计算全部项目的合计或排名。")
    elif comparison.operations:
        if "sum" in comparison.operations:
            lines.append(f"\n**预算合计 {_yuan(sum(budgets.values(), Decimal(0)))}**" + _citations(budget_ranks) + "。")
        for operation, label, find in (("max", "最高", max), ("min", "最低", min)):
            if operation in comparison.operations:
                amount = find(budgets.values())
                names = "、".join(_MARKDOWN.sub(r'\\\1', name) for name, budget in budgets.items() if budget == amount)
                lines.append(f"\n**预算{label}：{names}，{_yuan(amount)}**" + _citations(budget_ranks) + "。")
    return "\n".join(lines), missing

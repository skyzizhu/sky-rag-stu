"""Check high-confidence factual requirements against the selected evidence."""
from __future__ import annotations

import re
import unicodedata


_ID = re.compile(
    r"(?<![a-z0-9-])[a-z][a-z0-9]{0,21}(?:-[a-z0-9]{1,24})*"
    r"-[a-z]*[0-9][a-z0-9]{0,15}(?![a-z0-9-])", re.I,
)
_DASHES = str.maketrans("‐‑‒–—−", "------")
_SENTENCE = re.compile(r"[\r\n。；;！？!?]+|(?<!\d),|,(?!\d)")
_VALUE_QUESTION = re.compile(r"多少|几次|幾次|多大|多长|多長|什么值|什麼值|何值|how (?:many|much|long)")
_POLICY_QUESTION = re.compile(r"是否|有没有|有沒有|如何|怎么|怎麼|办法|辦法|条件|條件|标准|標準|规则|規則|规定|規定|流程")
_NUMBER = r"(?:[0-9]+(?:[,，][0-9]{3})*(?:\.[0-9]+)?|[零〇一二两兩三四五六七八九十百千万萬亿億点點]+)"
_MONEY = re.compile(rf"(?:(?:￥|¥|\$)\s*{_NUMBER}|{_NUMBER}\s*(?:元|万元|萬元|亿元|億元|人民币|人民幣|美元|欧元|歐元|港元)|(?:为|為|[:=|])\s*[0-9][0-9,.]*(?![0-9,.年月日]))")
_THRESHOLD_VALUE = re.compile(rf"(?:{_NUMBER}\s*(?:毫秒|秒|分钟|分鐘|小时|小時|摄氏度|攝氏度|℃|°c|ntu|%|ppm|mg/l|分贝|分貝)|(?:为|為|[:=|])\s*[0-9][0-9,.]*)", re.I)
_RUN_COUNT = re.compile(rf"{_NUMBER}\s*(?:次|轮|輪|场|場|批)")
_MONTH = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
          r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
_DATE = re.compile(
    rf"(?<!\d)(?:\d{{4}}\s*[年/.-]\s*\d{{1,2}}\s*[月/.-]\s*\d{{1,2}}(?:\s*[日号號])?"
    rf"|\d{{1,2}}\s*月\s*\d{{1,2}}\s*[日号號]|\b(?:{_MONTH})\.?\s+{_DAY}(?:,\s*|\s+)\d{{4}}"
    rf"|{_DAY}\s+(?:{_MONTH})\.?\s+\d{{4}})(?!\d)", re.I,
)
_DATE_ASK = re.compile(r"何时|何時|哪天|几号|幾號|什么时候|什麼時候|日期|时间|時間|\bwhen\b|\bdate\b|\bdeadline\b")
_DATES = (
    ("验收日期", re.compile(r"[验驗]收|\baccept(?:ance|ed)\b")),
    ("交付日期", re.compile(r"交付|交接|\bdeliver(?:y|ies|ed|s|ing)?\b|\bhandover\b|\bhanded over\b")),
    ("签署日期", re.compile(r"[签簽](?:署|订|訂|约|約)|\bsign(?:ed|ing|ature)?\b")),
    ("生效日期", re.compile(r"生效|\beffective\b|\b(?:takes? effect|came into effect|in force)\b")),
    ("结算日期", re.compile(r"[结結]算|\bsettl(?:ement|ed|e)\b")),
)
_OTHER_DATE_EVENTS = re.compile(r"[发發]布|更新|修改|[终終]止|到期|\b(?:publish(?:ed)?|updated|expires?|expired)\b")
_DATE_CLAUSE_END = re.compile(r"[。；;，,!?！？]|\.(?=\s|$)")
_SOURCE_LINE = re.compile(r"(?m)^\s*(?:来源文档:|#{1,6}[ \t]).*$")
_BEFORE_DATE = re.compile(
    r"(?:日期|时间|時間|截止|不[迟遲]于|[:=为為于]|\b(?:on|by|before|until|after|prior to|no later than)\b)\s*$")
_AFTER_DATE = re.compile(r"\s*(?:(?:已)?通过(?:了)?(?:客户)?|(?:已)?通過(?:了)?(?:客戶)?|(?:已)?完成|前须|前須)\s*")
_DEADLINE_ASK = re.compile(r"截止|期限|[约約]定|\b(?:deadline|due date|contractual)\b", re.I)
_DEADLINE_EVIDENCE = re.compile(
    r"截止|期限|最[迟遲]|[约約]定|[须須]|应在|應在|应于|應於|不得|不[迟遲][于於]"
    r"|\b(?:shall|must|required|due|deadline)\b", re.I,
)
_DEADLINE_PREFIX = re.compile(
    r"(?P<inclusive>不[迟遲][于於]|不得晚[于於]|\b(?:on or before|no later than|by)\b)"
    r"|(?P<strict>\b(?:strictly before|before|prior to)\b)", re.I,
)
_DEADLINE_TAIL = re.compile(r"[\s*`_]*$")
_DEADLINE_SUFFIX = re.compile(
    r"[\s*`_]*(?:(?P<inclusive>(?:之前|以前|前)\s*[（(]\s*含(?:当天|當天|当日|當日)\s*[）)]"
    r"|[（(]\s*含(?:当天|當天|当日|當日)\s*[）)]|(?:当天|當天|当日|當日)(?:及|或)(?:之前|以前))"
    r"|(?P<strict>(?:之前|以前|前)(?!后|後|往)))",
)
_NUMERIC_DATE = re.compile(r"(?:(\d{4})\s*[年/.-]\s*)?(\d{1,2})\s*[月/.-]\s*(\d{1,2})(?:\s*[日号號])?")
_MONTH_INDEX = {month: number for number, month in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_NEGATED_DEADLINE = re.compile(r"(?:不是|并非|並非|不能说|不能寫成|不能写成|\bnot|\brather than)\s*[“‘「『\"']?\s*$", re.I)
_UNRELATED_DEADLINE = re.compile(r"发票|發票|付款|退款|保修|\b(?:invoice|payment|refund|warranty)\b", re.I)


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_DASHES).casefold()


def _date_clauses(text: str):
    """Keep dates whole, including English comma dates, inside their evidence clause."""
    for block in re.split(r"\n\s*\n", _SOURCE_LINE.sub("\n", text)):
        start = 0
        dates = [match.span() for match in _DATE.finditer(block)]
        for boundary in _DATE_CLAUSE_END.finditer(block):
            if any(left <= boundary.start() < right for left, right in dates):
                continue
            yield block[start:boundary.start()]
            start = boundary.end()
        yield block[start:]


def _dated_events(evidence: list[str]) -> set[str]:
    """Associate each date with its closest unambiguous event within the clause."""
    found = set()
    for passage in evidence:
        for clause in _date_clauses(passage):
            dates = list(_DATE.finditer(clause))
            if not dates:
                continue
            events = [(label, match) for label, pattern in (*_DATES, ("", _OTHER_DATE_EVENTS))
                      for match in pattern.finditer(clause)]
            for day in dates:
                best_key = None
                nearest = set()
                for label, event in events:
                    if event.end() <= day.start():
                        gap = clause[event.end():day.start()]
                        linked = _BEFORE_DATE.search(gap)
                    else:
                        gap = clause[day.end():event.start()]
                        linked = _AFTER_DATE.fullmatch(gap)
                    key = (not linked, len(gap))
                    if best_key is None or key < best_key:
                        best_key, nearest = key, {label}
                    elif key == best_key:
                        nearest.add(label)
                if len(nearest) == 1:
                    found.update(nearest)
    return found


def _deadline_parts(text: str, day: re.Match) -> tuple[re.Match | None, re.Match | None]:
    prefix = next((match for match in reversed(list(_DEADLINE_PREFIX.finditer(text, 0, day.start())))
                   if _DEADLINE_TAIL.fullmatch(text[match.end():day.start()])), None)
    suffix = _DEADLINE_SUFFIX.match(text, day.end())
    return prefix, suffix


def _numeric_date(day: str) -> tuple[int | None, int, int] | None:
    day = _normalize(day)
    match = _NUMERIC_DATE.fullmatch(day)
    if match:
        return int(match[1]) if match[1] else None, int(match[2]), int(match[3])
    if re.search(r"[a-z]", day):
        month = re.search(rf"\b(?:{_MONTH})\b", day)
        parts = re.findall(r"\d+", day)
        if month and len(parts) == 2 and len(parts[1]) == 4:
            return int(parts[1]), _MONTH_INDEX[month[0][:3]], int(parts[0])
    return None


def deadline_constraint(question: str, sources: list[dict]) -> dict:
    """Capture one unambiguous, year-specific deadline from cited evidence."""
    question = _normalize(question)
    events = [(label, event) for label, event in _DATES if event.search(question)]
    if len(events) != 1 or not _DEADLINE_ASK.search(question):
        return {}
    deadlines = {}
    for source in sources:
        for clause in _date_clauses(_normalize(source["text"])):
            if not events[0][1].search(clause) or not _DEADLINE_EVIDENCE.search(clause):
                continue
            for day in _DATE.finditer(clause):
                value = _numeric_date(day[0])
                if not value or value[0] is None:
                    continue
                prefix, suffix = _deadline_parts(clause, day)
                for qualifier in (prefix, suffix):
                    if qualifier:
                        operator = "by" if qualifier["inclusive"] else "before"
                        deadlines.setdefault((value, operator), source["rank"])
    if len(deadlines) != 1:
        return {}
    ((year, month, day), operator), rank = next(iter(deadlines.items()))
    return {"date": f"{year:04d}-{month:02d}-{day:02d}", "operator": operator, "rank": rank, "event": events[0][0]}


def preserve_deadline_wording(answer: str, constraint: dict) -> str:
    """Repair only contradictory explicit modifiers of the verified date."""
    if not constraint:
        return answer
    year, month, day = _numeric_date(constraint["date"])
    inclusive = constraint["operator"] == "by"
    current = dict(_DATES)[constraint["event"]]
    for match in reversed(list(_DATE.finditer(answer))):
        if _numeric_date(match[0]) not in {(year, month, day), (None, month, day)}:
            continue
        prefix, suffix = _deadline_parts(answer, match)
        if not any(bool(part["inclusive"]) != inclusive for part in (prefix, suffix) if part):
            continue
        start = prefix.start() if prefix else match.start()
        if _NEGATED_DEADLINE.search(answer[:start]):
            continue
        clause = _normalize(re.split(r"[\r\n。；;，,！？!?]+", answer[:start])[-1] +
                            re.split(r"[\r\n。；;，,！？!?]+", answer[start:], maxsplit=1)[0])
        if not current.search(clause) and (_UNRELATED_DEADLINE.search(clause) or any(
                label != constraint["event"] and event.search(clause) for label, event in _DATES)):
            continue
        tail = suffix.start(suffix.lastgroup) if suffix else match.end()
        end = suffix.end() if suffix else match.end()
        date_text = answer[prefix.end() if prefix else match.start():tail]
        if prefix and prefix[0].isascii():
            replacement = ("no later than" if inclusive else "before") + date_text
        else:
            replacement = ("在" if prefix else "") + date_text + ("当日及之前" if inclusive else "之前")
        answer = answer[:start] + replacement + answer[end:]
    return answer


def assess_evidence(question: str, passages: list[str], scope_terms: tuple[str, ...] = ()) -> dict:
    """Report checked and missing anchors. Leave unsupported question shapes unjudged."""
    question = _normalize(question)
    for term in scope_terms:
        question = question.replace(_normalize(term), "")
    evidence = [_normalize(text) for text in passages]
    lines = [line for text in evidence for line in _SENTENCE.split(text)
             if not line.lstrip().startswith("来源文档:")]
    checks: dict[str, bool] = {}

    identifiers = []
    for match in _ID.finditer(question):
        code = match[0]
        literal = re.compile(rf"(?<![a-z0-9-]){re.escape(code)}(?![a-z0-9-])")
        identifiers.append(code.upper())
        checks[code.upper()] = any(literal.search(text) for text in evidence)

    def field_value(name, request, anchor, value, *, before=0):
        if re.search(request, question):
            checks[name] = any(
                value.search(line[max(0, match.start() - before):match.end() + 36])
                for line in lines for match in re.finditer(anchor, line)
            )

    if _VALUE_QUESTION.search(question):
        field_value("预算金额", r"[预預]算", r"[预預]算", _MONEY)
        field_value("结算金额", r"[结結]算(?:金[额額]|[总總][额額]|款[项項])", r"[结結]算", _MONEY)
        measured = bool(re.search(r"[实實][测測].{0,6}[阈閾][值値]", question))
        threshold = r"[实實][测測].{0,6}[阈閾][值値]|[阈閾][值値].{0,6}[实實][测測]" if measured else r"[阈閾][值値]"
        field_value("实测阈值" if measured else "阈值", r"[阈閾][值値]", threshold, _THRESHOLD_VALUE)
        field_value("验证次数", r"(?:[验驗][证證]|[测測][试試]|演[练練])次[数數]",
                    r"[验驗][证證]|[测測][试試]|演[练練]", _RUN_COUNT)

    if _DATE_ASK.search(question):
        date_fields = [name for name, event in _DATES if event.search(question)]
        if date_fields:
            dated = _dated_events(evidence)
            checks.update((name, name in dated) for name in date_fields)
    else:
        date_fields = []

    if _POLICY_QUESTION.search(question):
        for name, terms in (("赔付", r"[赔賠](?:付|[偿償])|[违違]约金"),
                            ("退款", r"退款|退[还還]款[项項]|退回款[项項]")):
            if re.search(terms, question):
                checks[name] = any(re.search(terms, line) for line in lines)

    if not checks:
        return {}
    missing = [label for label, supported in checks.items() if not supported]
    has_required_id = not identifiers or any(checks[code] for code in identifiers)
    blocking = not has_required_id or (len(missing) == len(checks)
                                       and any(label not in date_fields for label in checks))
    result = {"status": "unsupported" if blocking else ("partial" if missing else "supported"),
              "checked": list(checks), "missing": missing}
    if date_fields:
        result["date_fields"] = date_fields
    return result

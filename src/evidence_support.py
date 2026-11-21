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
_DATE = re.compile(r"(?:\d{4}\s*[年/-]\s*\d{1,2}\s*[月/-]\s*\d{1,2}(?:日|号|號)?|\d{1,2}月\d{1,2}[日号號])")
_DATE_ASK = re.compile(r"何时|何時|哪天|几号|幾號|什么时候|什麼時候|日期")
_DATES = (
    ("验收日期", r"[验驗]收"),
    ("交付日期", r"交付|交接"),
    ("签署日期", r"签署|簽署|签订|簽訂"),
    ("生效日期", r"生效"),
    ("结算日期", r"[结結]算"),
)


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_DASHES).casefold()


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
        for name, event in _DATES:
            field_value(name, rf"(?:{event})(?:完成|通过|通過|确认|確認)?(?:的)?(?:日期|时间|時間|是哪天|是什么时候)?",
                        event, _DATE, before=14)

    if _POLICY_QUESTION.search(question):
        for name, terms in (("赔付", r"[赔賠](?:付|[偿償])|[违違]约金"),
                            ("退款", r"退款|退[还還]款[项項]|退回款[项項]")):
            if re.search(terms, question):
                checks[name] = any(re.search(terms, line) for line in lines)

    if not checks:
        return {}
    missing = [label for label, supported in checks.items() if not supported]
    has_required_id = not identifiers or any(checks[code] for code in identifiers)
    return {"status": "unsupported" if not has_required_id or len(missing) == len(checks)
            else ("partial" if missing else "supported"),
            "checked": list(checks), "missing": missing}

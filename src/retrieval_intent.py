"""Ground file-date and version scopes in the original retrieval question."""
from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from datetime import date, timedelta


_ABSOLUTE = r"(?:\d{4}-\d{2}-\d{2}|\d{4}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号號])?)?)"
_RELATIVE = r"(?:今年|去年|上[个個]月|本月|这[个個]月|這[个個]月|今天|昨天|最近(?:\s*\d{1,4}\s*天)?|过去\s*\d{1,4}\s*天|過去\s*\d{1,4}\s*天)"
_DATE = rf"(?:{_ABSOLUTE}|{_RELATIVE})"
_RANGE = rf"{_DATE}(?:\s*(?:至|到|~|～)\s*{_DATE})?"
_MUTATION = r"(?:修改|更新|上[传傳]|入[库庫])"
_DATE_PATTERNS = (
    re.compile(rf"(?P<period>{_RANGE})\s*(?:内|內|期间|期間)?\s*(?P<event>{_MUTATION})"
               r"(?:过的|過的|过|過|的|了?(?=(?:有)?(?:哪些|什么|甚麼|多少|几份|幾份)(?:份|个|個)?(?:文件|文档|文檔|资料|資料))"
               r"|(?=了?[？?。!！\s]*$))"),
    re.compile(rf"(?P<event>{_MUTATION})(?:时间|時間|日期)\s*(?:在|为|為|是|[:：])?\s*(?P<period>{_RANGE})"),
)
_QUOTED = re.compile(r'「[^」]*」|『[^』]*』|“[^”]*”|《[^》]*》|"[^"]*"')
_CURRENT = re.compile(r"现在|現在|现行|現行|当前|當前|新版|最新|生效版")
_HISTORY = re.compile(r"旧版|舊版|历史版|歷史版|归档版|歸檔版|原版|上一版|最初(?:立项|立項|版本)|旧版本|舊版本")
_COMPARE = re.compile(r"对比|對比|比较|比較|相比|相较|相較|比|差异|差異|异同|異同|变化|變化|分别|分別|涨了|漲了|降了")
_ONLY_ONE = re.compile(r"(?:只|仅|僅)(?:想)?(?:查|看|找|比较|比較)(?:一下)?(?:现行|現行|最新|旧版|舊版|归档|歸檔|历史|歷史)"
                       r"|(?:不|别|別|排除|不要)(?:查|看|找|比较|比較)?(?:现行|現行|最新|旧版|舊版|归档|歸檔|历史|歷史)")


def _period(value: str, today: date) -> tuple[date, date]:
    value = re.sub(r"\s+", "", value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        day = date.fromisoformat(value)
        return day, day
    literal = re.fullmatch(r"(\d{4})年(?:(\d{1,2})月(?:(\d{1,2})[日号號])?)?", value)
    if literal or value in ("今年", "去年"):
        year = int(literal[1]) if literal else today.year - (value == "去年")
        month = int(literal[2]) if literal and literal[2] else 0
        if literal and literal[3]:
            day = date(year, month, int(literal[3]))
            return day, day
        if month:
            return date(year, month, 1), date(year, month, monthrange(year, month)[1])
        return date(year, 1, 1), date(year, 12, 31)
    if value in ("今天", "昨天"):
        day = today - timedelta(days=value == "昨天")
        return day, day
    if value.endswith("月"):
        month = today.replace(day=1)
        if value.startswith("上"):
            month = (month - timedelta(days=1)).replace(day=1)
        return month, month.replace(day=monthrange(month.year, month.month)[1])
    count = re.search(r"\d+", value)
    days = int(count[0]) if count else 90
    return today - timedelta(days=max(1, days) - 1), today


def explicit_file_dates(question: str, *, today: date | None = None) -> dict[str, dict[str, str]]:
    """Return only dates tied to a file event, so content years stay in semantic recall."""
    today = today or date.today()
    text = _QUOTED.sub(" ", unicodedata.normalize("NFKC", question))
    intervals: dict[str, set[tuple[date, date]]] = {}
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            values = re.split(r"\s*(?:至|到|~|～)\s*", match["period"], maxsplit=1)
            try:
                start = _period(values[0], today)[0]
                end = _period(values[-1], today)[1]
            except (ValueError, OverflowError):
                continue
            if start > end:
                continue
            field = "indexed_at" if match["event"] in ("上传", "上傳", "入库", "入庫") else "updated_at"
            intervals.setdefault(field, set()).add((start, end))
    return {field: {"from": start.isoformat(), "to": end.isoformat()}
            for field, ranges in intervals.items() if len(ranges) == 1
            for start, end in ranges}


VERSION_SIDES = (
    ("current", "现行版本", ("active",)),
    ("historical", "历史版本", ("archive", "expired")),
)


def compares_versions(question: str) -> bool:
    text = unicodedata.normalize("NFKC", question)
    wording = _QUOTED.sub(" ", text)
    return bool(_COMPARE.search(wording) and not _ONLY_ONE.search(wording)
                and (_CURRENT.search(text) and _HISTORY.search(text)
                     or re.search(r"新[旧舊](?:两|兩)?版", wording)))


def comparison_statuses(filters: dict | None) -> dict[str, tuple[str, ...]]:
    """Partition visible statuses while respecting any explicit selection."""
    filters = filters or {}
    selected = filters.get("status", "archive" if filters.get("domain") == "archive" else "all")
    if selected is None:
        selected = "all"
    allowed = (selected,) if isinstance(selected, str) else tuple(selected or ())
    return {key: tuple(status for status in statuses if "all" in allowed or status in allowed)
            for key, _, statuses in VERSION_SIDES}

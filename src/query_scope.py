"""Translate explicit document scopes into the existing retrieval filters."""
from __future__ import annotations

import hashlib
import re
import unicodedata

from src.document_department import normalize_department
from src.document_type import infer_doc_type_from_filename, normalize_doc_type
from src.projects import PROJECT_SUFFIXES, list_projects, resolve_project_filter


SCOPE_FIELDS = ("project", "business_direction", "department", "doc_type")

# A generic request for acceptance materials spans the existing acceptance labels.
_ACCEPTANCE = (
    "验收", "验收资料", "验收材料", "验收文档", "验收文件", "验收报告", "验收方案", "验收记录", "验收清单",
    "驗收", "驗收資料", "驗收材料", "驗收文檔", "驗收文件", "驗收報告", "驗收方案", "驗收紀錄", "驗收清單",
)
_TYPE_FAMILIES = {name: _ACCEPTANCE for name in ("验收资料", "驗收資料", "验收材料", "驗收材料")}
_QUOTED = r'[「『“《"]([^」』”》"\n]{1,80})[」』”》"]'
_VALUE = _QUOTED + r"|((?:(?!(?:项目|項目|方向)(?:下|内|內|的)|归属|歸屬|以及|或者)[\w ·.-]){1,80}?)"
_LEAD = (r"(?:^\s*(?:请|請)?(?:帮我|幫我)?\s*"
         r"(?:(?:查找|查询|查詢|查看|查|找|列出|列舉|列举|盘点|盤點|检索|檢索)(?:一下)?\s*)?"
         r"|(?<=[，、,])\s*|(?<=方向下)|(?<=方向内)|(?<=方向內)|(?<=项目下)|(?<=項目下))")
_SCOPE_FOLLOW = r"的|下|内|內|中|里|裡|有(?:哪些|多少|几|幾)|\s|[，、。！？?!]|$"
_SCOPE_END = rf"(?={_SCOPE_FOLLOW})"
_PROJECT_WORD = "|".join(PROJECT_SUFFIXES)
_PROJECT_END = rf"(?=目前|当前|當前|现在|現在|章程|{_SCOPE_FOLLOW})"
_PATTERNS = {
    "project": re.compile(_LEAD + rf"(?:{_VALUE})\s*({_PROJECT_WORD})" + _PROJECT_END),
    "business_direction": re.compile(_LEAD + rf"(?:{_VALUE})\s*(?:主?(?:业务|業務))?方向" + _SCOPE_END),
    "department": re.compile(rf"(?:归属(?:于)?|歸屬(?:於)?|所属|所屬|属于|屬於)\s*(?:部门|部門)?"
                             rf"\s*(?:为|為|是|[:：=])?\s*(?:{_VALUE})(?=的|[，、。！？?!]|$)"),
    "doc_type": re.compile(rf"(?:文档类型|文件类型|文件類型|文書タイプ|document type)\s*(?:为|是|為|[:：=])\s*(?:{_VALUE})(?=的|[，、。！？?!]|$)", re.I),
}
_ACCEPTANCE_REQUEST = re.compile(r"(?:^|[的内內中\s]|查|找|列出)([验驗]收(?:资料|資料|材料|文档|文件|报告|報告|方案|记录|紀錄|清单|清單))(?=$|[,，。？！?！]|有|包括|包含|清单|清單|里|裡|中)")
_TYPE_REQUEST = re.compile(r"(?:的\s*|查找|查询|查詢|查看|查|找|列出)\s*([^的\s，。！？?!]{2,24}?)"
                           r"(?:\s*(?:有哪些|有多少|有[几幾]份))?[。！？?!]?$")
_DIRECT_TYPE_REQUEST = re.compile(r"^\s*(?:请|請)?(?:帮我|幫我)?\s*(?:查找|查询|查詢|查看|查|找|列出)\s*")
_BROAD_TYPES = {"制度", "规定", "規定", "规范", "規範", "流程", "政策", "资料", "資料", "文件", "文档", "文檔", "报告", "報告", "纪要", "紀要"}
_CONTENT_MENTION = re.compile(r"提到|提及|谈到|談到|涉及|引用|包含|含有|mention(?:s|ed)?\b|contains?\b", re.I)
_CLAUSE = re.compile(r"[，,。；;！？?!\n]")
_JOIN = re.compile(r"\s*(?:以及|或者|或是|和|与|與|或|及|、)\s*")
_EXPLICIT_JOIN = re.compile(r"以及|或(?!许|許|然|有|可|缺)|、|@\s*(?:和|与|與|及)\s*@")
_DEPARTMENT_END = re.compile(r"(?:部|中心|办公室|辦公室|处|處|室|组|組|科|团队|團隊)(?:的|$)")
_NEGATED_SCOPE = re.compile(
    r"(?:不|非|并非|並非|不要|排除|除去|剔除)\s*(?:归属|歸屬|所属|所屬|属于|屬於|文档类型|文件類型)"
    r"|(?:不查|不找|不要|排除|除去|剔除)\s*(?:查|找)?[^的，。；]{1,80}(?:项目|項目|專案|方向)"
    r"|(?:文档类型|文件类型|文件類型)\s*(?:不为|不為|不是|不等于|不等於)"
)
_NON_NAMES = re.compile(
    r"哪些|哪一|哪几|哪幾|什么|甚麼|什麼|多少|几个|幾個|有没有|有沒有"
    r"|^(?:所有|全部|每个|每個|各个|各個|这些|這些|那些|本次|这份|這份)$"
)
_PROJECT_REF = re.compile(
    r"(?:(?:那(?:么|麼)?|在|关于|關於|请问|請問|想问|想問|问|問)\s*)*"
    r"(?:这个|這個|这|這|该|該|此|本|当前|當前|所选|所選|选中的|選中的|已选|已選)"
)
_PROJECT_SUFFIX = re.compile(rf"(?:{_PROJECT_WORD})$")
_BARE_PROJECT_REF = re.compile(
    r"^\s*(?:那(?:么|麼)?[，,]?\s*)?(?:请问|請問)?\s*(?P<pronoun>[它牠])的\s*"
    r"(?:立[项項](?:[预預]算|时间|時間|日期|背景|报告|報告)|里程碑"
    r"|(?:牵头|牽頭|参与|參與)部[门門]|(?:项目|項目|专案|專案)(?:[预預]算|[负負][责責]人|周期)"
    r"|[验驗]收(?:资料|資料|材料|报告|報告|方案|日期|时间|時間|标准|標準))"
)
_PRESENTATION_FOLLOWUP = re.compile(
    r"\s*(?:(?:请|請|可以)\s*)?(?:简短一点|簡短一點|简单一点|簡單一點|再详细一点|再詳細一點|保持[简簡][洁潔]"
    r"|(?:用|按)表格(?:展示|呈现|呈現)?|用中文(?:回答)?|用英文(?:回答)?)\s*"
)
_OTHER_REFERENT = re.compile(r"(?:这份|這份|该份|該份)(?:文件|报告|報告|合同)|(?:这个|這個|该|該)(?:产品|產品|供应商|供應商|型号|型號)")
_PROJECT_SELECTION = re.compile(
    r"(?:^|[，,。；;！？?!\n])\s*(?:请|請)?\s*(?:(?:现在|現在|接下来|接下來|这次|這次)\s*)?"
    r"(?:(?:只|仅|僅)\s*(?:查询|查詢|查看|查找|检索|檢索|查|看|找)"
    r"|(?:更正为|更正為|改为|改為|改成|改查|切换到|切換到|换成|換成)"
    r"\s*(?:查询|查詢|查看|查|看)?)\s*"
)


def _in_content(text: str, start: int) -> bool:
    """Check the clause leading into a value, treating quoted names as literals."""
    prefix = text[:start]
    return bool(_CONTENT_MENTION.search(_CLAUSE.split(re.sub(_QUOTED, "", prefix))[-1]))


def project_reference(question: str) -> str:
    """Return an unquoted reference to the already selected project."""
    for match in _PATTERNS["project"].finditer(question):
        if match[2] and _PROJECT_REF.fullmatch(match[2].strip()) and not _in_content(question, match.start(2)):
            return match[2].strip()
    return "它" if _BARE_PROJECT_REF.match(question) else ""


def name_project_in_followup(question: str, name: str) -> str:
    match = _BARE_PROJECT_REF.match(question)
    if not match:
        return question
    start, end = match.span("pronoun")
    return question[:start] + name + question[end:]


def _spoken_project_name(question: str, project: str) -> str:
    """Keep the name the user typed, including an unquoted project suffix."""
    for match in _PATTERNS["project"].finditer(question):
        group = 1 if match[1] else 2
        if match[group].strip() == project and not _in_content(question, match.start(group)):
            return project if group == 1 else question[match.start(2):match.end(3)].strip()
    return project


def unsupported_scope(question: str) -> str:
    """Flag explicit exclusions and multiple values requiring a richer filter."""
    masked = re.sub(_QUOTED, lambda match: "@" * len(match[0]), question)
    clauses = re.split(r"[，,]", masked)
    if len(clauses) > 1:
        offset = 0
        values: dict[str, str] = {}
        for clause in clauses:
            end = offset + len(clause)
            for field, value in extract_scope(question[offset:end]).items():
                if values.setdefault(field, value) != value:
                    return "multiple"
            offset = end + 1
    for clause in _CLAUSE.split(re.sub(r"@+", "@", masked)):
        expression = _CONTENT_MENTION.split(clause, maxsplit=1)[0]
        if _NEGATED_SCOPE.search(expression):
            return "negated"
        parts = _JOIN.split(expression)
        if len(parts) < 2:
            continue
        for role in (r"项目|項目|專案|专案", r"方向", r"归属|歸屬|所属|所屬",
                     r"文档类型|文件类型|文件類型"):
            if sum(bool(re.search(role, part)) for part in parts) > 1:
                return "multiple"
        if sum(bool(infer_doc_type_from_filename(part)) for part in parts) > 1:
            return "multiple"
        if re.search(r"归属|歸屬|所属|所屬", parts[0]) and all(_DEPARTMENT_END.search(part) for part in parts):
            return "multiple"
        if _EXPLICIT_JOIN.search(expression) and (
            re.search(r"(?:项目|項目|專案|方向)" + _SCOPE_END, parts[-1])
            or re.search(r"(?:归属|歸屬|所属|所屬|文档类型|文件类型|文件類型)[^的]*$", parts[0])
        ):
            return "multiple"
    return ""


def extract_scope(question: str, proposed: dict | None = None) -> dict[str, str]:
    """Resolve explicit file scopes and keep incidental mentions available for semantic search."""
    original = unicodedata.normalize("NFKC", question)
    scope = {}
    for field in SCOPE_FIELDS:
        for match in _PATTERNS[field].finditer(question):
            group = 1 if match[1] else 2
            value = match[group].strip()
            if field == "project" and not match[1] and _PROJECT_REF.fullmatch(value):
                continue
            if (value and not _in_content(question, match.start(group))
                    and (match[1] or not (_CONTENT_MENTION.search(value) or _NON_NAMES.search(value)))):
                scope[field] = value
                break
    if "doc_type" not in scope:
        match = _ACCEPTANCE_REQUEST.search(original.rstrip())
        if match and not _in_content(original, match.start(1)):
            scope["doc_type"] = match[1]
        elif (match := _TYPE_REQUEST.search(question.rstrip())) and not _in_content(question, match.start(1)):
            candidate = match[1]
            proposed_type = proposed.get("doc_type") if isinstance(proposed, dict) else None
            direct = _DIRECT_TYPE_REQUEST.fullmatch(question[:match.start(1)])
            if candidate not in _BROAD_TYPES and (infer_doc_type_from_filename(candidate) == candidate
                                                 or direct and proposed_type == candidate):
                scope["doc_type"] = candidate
    if isinstance(proposed, dict):
        # A department or a type mentioned in the question does not alone set a file filter.
        for field in ("project", "business_direction"):
            value = proposed.get(field)
            if isinstance(value, str):
                literal = unicodedata.normalize("NFKC", value.strip())
                if field == "project" and _PROJECT_REF.fullmatch(_PROJECT_SUFFIX.sub("", literal).strip()):
                    continue
                if 0 < len(literal) <= 80 and any(
                    not _in_content(original, match.start())
                    for match in re.finditer(re.escape(literal), original, re.I)
                ):
                    scope.setdefault(field, value.strip())
    return scope


def selected_project_text(text: str) -> str | None:
    """Isolate the target of one explicit focus change, preserving the quoted project name."""
    masked = re.sub(_QUOTED, lambda match: "@" * len(match[0]), text)
    selections = list(_PROJECT_SELECTION.finditer(masked))
    return text[selections[0].end():] if len(selections) == 1 else None


def _project_turn(text: str, catalog: list[dict]) -> dict:
    """Find literal master-data names in a user turn, including names without a 项目 suffix."""
    selected = selected_project_text(text)
    if selected is not None:
        text = selected
    text_nfkc = unicodedata.normalize("NFKC", text)
    spans = []
    identities = set()
    for project in catalog:
        name = unicodedata.normalize("NFKC", project["name"])
        before = r"(?<![a-z0-9_])" if name[0].isascii() and name[0].isalnum() else ""
        after = r"(?![a-z0-9_])" if name[-1].isascii() and name[-1].isalnum() else ""
        for match in re.finditer(before + re.escape(name) + after, text_nfkc, re.I):
            start, end = match.span()
            if _in_content(text_nfkc, start) or any(a <= start and end <= b for a, b in spans):
                continue
            spans.append((start, end))
            identities.add(project["id"])
    explicit = extract_scope(text).get("project")
    if not identities and not explicit:
        return {"issue": "project_reference"} if selected is not None else {}
    issue = unsupported_scope(text)
    if len(identities) > 1 or issue == "multiple":
        return {"issue": "project_reference_ambiguous"}
    if explicit and not issue:
        try:
            project_ids = resolve_project_filter(_spoken_project_name(text, explicit), None,
                                                 allow_name_variants=True)
            return {"project_id": project_ids[0]}
        except ValueError:
            return {"issue": "project_reference"}
    if not identities or issue:
        return {"issue": "project_reference"}
    return {"project_id": next(iter(identities))}


def conversation_project(question: str, history: list[dict], saved: dict | None = None) -> dict:
    """Resolve from raw user turns, preserving a verified project ID across compaction/rename."""
    catalog = sorted(list_projects(), key=lambda row: -len(unicodedata.normalize("NFKC", row["name"])))
    names = {row["id"]: row["name"] for row in catalog}
    nearest = project_reference(question) == "它"
    for turn in range(len(history), -1, -1):
        if turn == len(history):
            text = question
        else:
            message = history[turn]
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            text = message.get("content")
        if not isinstance(text, str) or not text:
            continue
        if turn < len(history) and nearest and _OTHER_REFERENT.search(selected_project_text(text) or text):
            return {"issue": "project_reference"}
        signature = hashlib.sha256(text.encode()).hexdigest()
        if (saved and type(saved.get("project_id")) is int and saved["project_id"] in names and turn < len(history)
                and saved.get("turn") == turn and saved.get("digest") == signature):
            record = saved
        else:
            record = _project_turn(text, catalog)
        if record:
            project_id = record.get("project_id")
            if type(project_id) is int and project_id in names:
                scope = {"project_id": project_id, "name": names[project_id]}
            else:
                issue = record.get("issue")
                scope = {"issue": issue if issue == "project_reference_ambiguous" else "project_reference"}
            return {**scope, "turn": turn, "digest": signature}
        if turn < len(history) and nearest and not (
                project_reference(text) or all(_PRESENTATION_FOLLOWUP.fullmatch(clause)
                                               for clause in _CLAUSE.split(text) if clause.strip())):
            return {"issue": "project_reference"}
    return {}


def resolve_scope(question: str, scope: dict[str, str | int], filters: dict | None) -> tuple[dict, str]:
    """Intersect spoken and manually selected scopes, leaving identity filters intact."""
    combined = dict(filters or {})
    project = scope.get("project")
    if isinstance(project, str):
        project = _spoken_project_name(question, project)
    try:
        project_ids = resolve_project_filter(project, scope.get("business_direction"), allow_name_variants=True)
        department = normalize_department(scope.get("department"))
        doc_type = normalize_doc_type(scope.get("doc_type"))
    except ValueError:
        return combined, "unresolved"
    if department.casefold() == "all" or doc_type.casefold() == "all":
        return combined, "unresolved"

    types = doc_type or None
    if types and not _PATTERNS["doc_type"].search(question):
        types = _TYPE_FAMILIES.get(doc_type, doc_type)
        if project is not None and doc_type in ("章程", "項目章程", "项目章程"):
            types = ("章程", "项目章程", "項目章程")
    spoken = {"project_id": project_ids, "department": department or None, "doc_type": types}
    for key, value in spoken.items():
        if value is None:
            continue
        if value == []:
            return combined, "empty"
        current = combined.get(key)
        values = list(value) if isinstance(value, (tuple, list)) else [value]
        if current not in (None, "all"):
            selected = current if isinstance(current, (list, tuple)) else [current]
            values = [item for item in values if item in selected]
            if not values:
                return combined, "conflict"
        combined[key] = values if key == "project_id" or len(values) > 1 else values[0]
    return combined, ""


def is_scope_listing(question: str, scope: dict[str, str]) -> bool:
    """Use the catalog only when the question contains scope and listing words alone."""
    if not scope or not re.search(r"查|找|列出|列举|列舉|盘点|盤點|哪些|几份|幾份|多少", question):
        return False
    text = unicodedata.normalize("NFKC", question)
    for value in sorted(scope.values(), key=len, reverse=True):
        text = re.sub(re.escape(unicodedata.normalize("NFKC", value)), "", text, flags=re.I)
    return not re.sub(
        r"(?:请|請|帮我|幫我|查找|查询|查詢|查看|查|找|列出|列举|列舉|盘点|盤點|一下|所有|全部|有|"
        r"哪些|几份|幾份|多少|项目|項目|專案|专案|主业务方向|主要業務方向|业务方向|業務方向|方向|"
        r"归属于?|歸屬於?|所属|所屬|文档类型|文件类型|文件類型|为|為|是|的|下|内|內|中|里|裡|"
        r"文件|文档|文檔|资料|資料|和|及|且|[「」『』“”《》\"\s:：=,，、。!?？！])+", "", text)

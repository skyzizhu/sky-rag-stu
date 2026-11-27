"""文档内指令性文字检测：给证据卡片打标（V3.9.2）。

知识库里的文档可能夹带写给"助手/检索系统"看的指令（提示词注入的攻击原文、
演练样本、维护告示等）。MCP 的证据类工具在出证时逐卡调用本模块，把命中的
类别与原文片段放进 content_flags，供下游 Agent 识别。

只检测、只标注：不修改卡片原文，不参与召回、排序与过滤——指令文字常与正当
业务内容同卡共生，也会出现在正当的安全培训材料里（引用攻击原文作示例），
删除或排除都会连带丢掉证据。
"""

from __future__ import annotations

import re

MAX_FLAGS = 6              # 单张卡片最多报告的片段数
MAX_QUOTE_CHARS = 200      # 单个引文片段的最大长度

KIND_ROLE_MARKER = "role_marker"                  # 冒充系统消息/角色的格式标记
KIND_ASSISTANT_DIRECTIVE = "assistant_directive"  # 指使助手、检索系统执行某动作
KIND_OVERRIDE = "override"                        # 要求忽略既有规则或指令
KIND_PROMPT_DISCLOSURE = "prompt_disclosure"      # 要求交出系统提示词

# 冒充系统消息的格式标记：行首的方头括号标签、对话模板的角色标签、行首 SYSTEM PROMPT:。
# 限定行首，避免把正文里引用界面元素名称的写法（如"点击【系统通知】"）当成冒充。
_ROLE_MARKER = re.compile(
    r"^[ \t>*\-]*[\[【［]\s*(?:SYSTEM(?:\s+(?:NOTICE|MESSAGE|PROMPT|INSTRUCTIONS?|COMMAND))?|"
    r"系[统統](?:指令|通知|消息|提示[词詞]?)|(?:开发|開發|管理员|管理員)(?:消息|指令))\s*[\]】］]"
    r"|<\|?\s*(?:system|im_start|im_end)\s*\|?>"
    r"|^\s*SYSTEM\s+(?:PROMPT|NOTICE|MESSAGE|INSTRUCTIONS?)\s*[:：]",
    re.I | re.M)

# 指使助手/检索系统：称谓 + 情态词 + 动作，三者邻近出现才算。
_ADDRESSEE = (r"(?:[检檢]索系[统統]|[检檢]索引擎|知[识識][库庫]助手|AI\s*助手|智能助手|智能客服|"
              r"问答助手|问答系[统統]|大模型|[语語]言模型|助手|"
              r"(?<![A-Za-z])(?:AI|LLM|GPT|assistant|chatbot)(?![A-Za-z]))")
_MODAL = r"(?:必须|必須|[须須]|应当|應當|应该|應該|[务務]必|不得|不能|不可|\b(?:must|shall|should)\b)"
_ACTION = (r"(?:直接)?(?:回答|执行|執行|遵守|遵循|输出|輸出|返回|透露|泄露|泄漏|忽略|扮演|"
           r"\b(?:ignore|obey|follow|output|reveal|answer|comply|execute)\b)")
_ASSISTANT_DIRECTIVE = re.compile(
    rf"{_ADDRESSEE}[^。；;！!？?\n]{{0,10}}?{_MODAL}[^。；;！!？?\n]{{0,8}}?{_ACTION}", re.I)

# 覆盖既有规则：忽略/无视 + 规则/指令/提示词/设定。宾语不含"限制/要求"——
# 制度原文里"经审批可忽略上述限制"是正当的豁免条款。
_OVERRIDE = re.compile(
    r"(?:忽[略視]|[无無][视視]|忘掉|忘[记記]|抛开|拋开)[^。；;！!？?\n]{0,8}?的?"
    r"(?:[规規][则則]|指令|指示|提示[词詞]|[设設]定)"
    r"|\b(?:ignore|disregard|forget)\b[^.\n]{0,40}?\b(?:instructions?|rules?|guidelines?|prompts?|policies)\b",
    re.I)

# 索要系统提示词：输出/打印/泄露 + 系统提示词。
_PROMPT_DISCLOSURE = re.compile(
    r"(?:输出|輸出|打印|显示|顯示|展示|复述|複述|泄露|泄漏|透露|返回)[^。；;！!？?\n]{0,12}?系[统統]提示[词詞]"
    r"|\b(?:reveal|print|output|repeat|show|leak|disclose)\b[^.\n]{0,30}?\b(?:system prompt|instructions)\b",
    re.I)

_PATTERNS = (
    (KIND_ROLE_MARKER, _ROLE_MARKER),
    (KIND_ASSISTANT_DIRECTIVE, _ASSISTANT_DIRECTIVE),
    (KIND_OVERRIDE, _OVERRIDE),
    (KIND_PROMPT_DISCLOSURE, _PROMPT_DISCLOSURE),
)

_SENTENCE_END = "。！？!?"
_BLOCK_STARTS = ("#", "来源文档")


def _block_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """命中所在的文字块：空行、标题行、来源行都是块边界（Markdown 软换行不切断）。"""
    left = text.rfind("\n", 0, start) + 1
    while left > 0:
        prev_end = left - 1
        prev_left = text.rfind("\n", 0, prev_end) + 1
        line = text[prev_left:prev_end]
        if not line.strip() or line.lstrip().startswith(_BLOCK_STARTS):
            break
        left = prev_left
    right = text.find("\n", end)
    if right < 0:
        return left, len(text)
    while True:
        next_right = text.find("\n", right + 1)
        if next_right < 0:
            return left, len(text)
        line = text[right + 1:next_right]
        if not line.strip() or line.lstrip().startswith("#"):
            return left, right
        right = next_right


def _quote_around(text: str, start: int, end: int) -> str:
    """命中所在的句子：标题行以行为界，其余在块内按句末标点切分；空白折叠，超长截断。"""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line = text[line_start:line_end if line_end >= 0 else len(text)]
    if line.lstrip().startswith("#"):
        excerpt = line  # 标题自成一句，不带入紧随其后的正文
    else:
        block_start, block_end = _block_bounds(text, start, end)
        left = start
        while left > block_start and text[left - 1] not in _SENTENCE_END:
            left -= 1
        right = end
        while right < block_end and text[right] not in _SENTENCE_END:
            right += 1
        if right < block_end:
            right += 1  # 含句末标点
        excerpt = text[left:right]
    quote = " ".join(excerpt.split()).lstrip("# ").strip()
    if len(quote) > MAX_QUOTE_CHARS:
        return quote[:MAX_QUOTE_CHARS - 1] + "…"
    return quote


def flag_instruction_like(text: str | None) -> list[dict]:
    """返回卡片中疑似写给助手的指令片段：[{"kind": 类别, "quote": 原文片段}]。

    只认"指令形态"的文字——冒充系统消息、指使助手、要求忽略规则、索要系统提示词；
    单纯讨论这些话题的内容不命中。"""
    flags: list[dict] = []
    seen: set[tuple[str, str]] = set()
    if not text:
        return flags
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            quote = _quote_around(text, match.start(), match.end())
            key = (kind, quote)
            if key in seen:
                continue
            seen.add(key)
            flags.append({"kind": kind, "quote": quote})
            if len(flags) >= MAX_FLAGS:
                return flags
    return flags

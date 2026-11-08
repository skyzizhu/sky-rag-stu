"""回答语言判定：回答语言只跟随提问语言。

规则（依次判定，命中即停）：
  0. 提问里明确要求了回答语言 → 按要求（「请用英文回答」「中文回答我」「Please answer in Chinese」）；
     同一句里有多个要求时以最后一个为准；只说「中文」时简繁跟随提问本身的写法。
     不算明确要求：询问语言现象的句子（「它会用英文回答吗」「Why does it answer in Chinese?」）、
     话题词（「英文写作」「中文说明书」）、翻译请求（「翻译成英文」——译文是内容，回答仍跟随提问）；
  1. 日文：含 2 个以上假名，且没有简体专用字（「我の笔记」这类网络用语仍按中文）；
  2. 韩文：含 2 个以上韩文字母；
  3. 中文：含汉字即为中文（夹杂英文术语也算，如「Xtools 的 PDF parsing 怎么用」）；
     繁体专用字多于简体专用字 → 繁体中文，否则简体中文；
  4. 其他文字（俄文、阿拉伯文等）→ 与提问同语言；
  5. 拉丁字母 2 个词以上 → 英文（含 é / ñ / ü 等非英文字母 → 与提问同语言）；
  6. 判断不出（只有一个词、缩写、数字或符号，如「RAG」「OK」「Xtools?」）
     → 沿用本会话最近一个能判断出语言的提问 → 仍没有则简体中文。

不看界面语言、不看资料语言、不看历史回答的语言。

为什么由代码判定，而不是只在系统提示词里写"跟随用户的语言"：
系统提示词、资料标签、历史对话几乎全是中文时，模型自己判断语言容易被带偏——
实测同一会话已有中文问答时，英文提问 10 次有 6 次答成中文。改为代码判定、
在用户消息最末尾追加一句用目标语言写的指令后，实测 0 跑偏（见 realize.md B14.1）。
"""

from __future__ import annotations

import re

DEFAULT_LANGUAGE = "zh-CN"  # 判断不出、且本会话也没有可参考的提问时
SAME_AS_QUESTION = "same"   # 确定不是中日韩英、但说不出具体语种 → 让模型与提问保持一致

# 指令用目标语言本身书写：英文写的 "Answer in English." 对英文回答的约束力最强。
# 日文与中文共用汉字，在中文为主的提示词里更容易滑回中文，所以额外明确"不要用中文"
# （实测同一请求各 25 次：普通写法跑偏 2 次 → 加强写法 0 次）
DIRECTIVES = {
    "zh-CN": "请用简体中文回答。",
    "zh-TW": "請用繁體中文回答。",
    "ja": "必ず日本語で回答してください。中国語で回答しないでください。",
    "ko": "한국어로 답변해 주세요.",
    "en": "Answer in English.",
    "fr": "Réponds en français.",
    "de": "Bitte antworte auf Deutsch.",
    "es": "Responde en español.",
    "ru": "Отвечай на русском языке.",
    SAME_AS_QUESTION: "Answer in the same language as the user's question.",
}

# 简繁对照：常用且字形不同的字，逐字一一对应（只用于判定简繁，不做转换）
_SIMPLIFIED = (
    "这们个说为会来时国学与对还开关问题东车书长门见页体处档设检发"
    "经动现实点应样种务业产员电话视频网络机数据库识记录读写语译资"
    "过进运营计项级线简单杂总结报导图标签类别链无吗让给从两几当间"
    "区买卖钱价费号码输换转传载编辑脑软统维护测试验证权认选择优习"
    "课师职层观质觉响声乐么后历复云请谁该帮谢规则达际览订详细场广"
)
_TRADITIONAL = (
    "這們個說為會來時國學與對還開關問題東車書長門見頁體處檔設檢發"
    "經動現實點應樣種務業產員電話視頻網絡機數據庫識記錄讀寫語譯資"
    "過進運營計項級線簡單雜總結報導圖標簽類別鏈無嗎讓給從兩幾當間"
    "區買賣錢價費號碼輸換轉傳載編輯腦軟統維護測試驗證權認選擇優習"
    "課師職層觀質覺響聲樂麼後歷復雲請誰該幫謝規則達際覽訂詳細場廣"
)
_SIMPLIFIED_SET = set(_SIMPLIFIED)
_TRADITIONAL_SET = set(_TRADITIONAL)
# 日文里也使用的同形字：不能作为"这句是中文"的证据
_ZH_ONLY = _SIMPLIFIED_SET - set("会来国学与体点机数据写当区号声后云")

_KANA = re.compile(r"[\u3041-\u3096\u30a1-\u30fa]")       # 平假名 + 片假名（不含 ・ ー）
_KANA_ANY = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_HANGUL = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# 拉丁字母：Latin-1 补充区里的 ×（U+00D7）、÷（U+00F7）是运算符号不是字母，必须排除——
# 否则「2×3 matrix」这类英文提问会被误判为「其他语言」
_LATIN_LETTERS = r"A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f"
_LATIN = re.compile(r"[" + _LATIN_LETTERS + r"\uff21-\uff3a\uff41-\uff5a]")  # 含全角字母
_LATIN_NON_ENGLISH = re.compile(r"[\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f]")  # é ñ ü ß 等


def detect_language(text: str) -> str | None:
    """判定一段提问的语言代码（zh-CN / zh-TW / ja / ko / en / same）；判断不出返回 None。"""
    text = text or ""
    if len(_KANA.findall(text)) >= 2 and not (_ZH_ONLY & set(text)):
        return "ja"
    if len(_HANGUL.findall(text)) >= 2:
        return "ko"
    if _HAN.search(text):
        simplified = sum(1 for c in text if c in _SIMPLIFIED_SET)
        traditional = sum(1 for c in text if c in _TRADITIONAL_SET)
        return "zh-TW" if traditional > simplified else "zh-CN"
    other_letters = sum(
        1 for c in text
        if c.isalpha() and not _LATIN.match(c) and not _KANA_ANY.match(c) and not _HANGUL.match(c)
    )
    if other_letters >= 2:
        return SAME_AS_QUESTION
    latin_words = [w for w in text.split() if _LATIN.search(w)]
    if len(latin_words) >= 2:
        return SAME_AS_QUESTION if _LATIN_NON_ENGLISH.search(text) else "en"
    return None


# ---------- 规则 0：提问里明确要求的回答语言 ----------
# 语种说法 → 语言代码（"zh" = 只说了中文，简繁再看提问本身）
_ZH_NAMES = {
    "简体中文": "zh-CN", "簡體中文": "zh-CN", "简体": "zh-CN", "簡體": "zh-CN",
    "繁体中文": "zh-TW", "繁體中文": "zh-TW", "繁体": "zh-TW", "繁體": "zh-TW",
    "中文": "zh", "汉语": "zh", "漢語": "zh", "普通话": "zh", "普通話": "zh",
    "英文": "en", "英语": "en", "英語": "en",
    "日文": "ja", "日语": "ja", "日語": "ja", "日本语": "ja", "日本語": "ja",
    "韩文": "ko", "韩语": "ko", "韓文": "ko", "韓語": "ko",
    "法语": "fr", "法文": "fr", "德语": "de", "德文": "de",
    "西班牙语": "es", "西班牙文": "es", "俄语": "ru", "俄文": "ru",
}
_EN_NAMES = {
    "simplified chinese": "zh-CN", "traditional chinese": "zh-TW", "chinese": "zh",
    "mandarin": "zh", "english": "en", "japanese": "ja", "korean": "ko",
    "french": "fr", "german": "de", "spanish": "es", "russian": "ru",
}


def _alternation(names) -> str:
    """长的说法排前面（「简体中文」先于「中文」），英文说法里的空格允许多个空白。"""
    ordered = sorted(names, key=len, reverse=True)
    return "|".join(re.escape(n).replace(r"\ ", r"\s+") for n in ordered)


_ZH_LANG = _alternation(_ZH_NAMES)
_EN_LANG = _alternation(_EN_NAMES)
# 要求句的起点：句首/标点之后，或紧跟「请/麻烦/你/可以…」——
# 「它会用英文回答吗」「为什么总是用中文回答」前面是「会/是」，不算要求
_ZH_START = (r"(?:^|[，。！？；：、,.!?;:\s]|请|請|麻烦|麻煩|帮我|幫我|给我|給我|你|您"
             r"|可以|能否|能不能|并|並|再|然后|然後)")
_ZH_VERB = (r"(?:回答|回复|回覆|作答|答复|答覆|输出|輸出|写(?!作)|寫(?!作)|说明(?!书)|說明(?!書)"
            r"|解释|解釋|介绍|介紹|总结|總結|表达|表達|讲|講|说|說)")
_EN_START = (r"(?:^|[.?!;:,\n]\s*|\b(?:please|pls|kindly)\s+"
             r"|\b(?:can|could|would|will)\s+you\s+(?:please\s+)?)")
_EXPLICIT_PATTERNS = [
    # 「请用英文回答」「可以用英文写吗」：用/以 + 语种 + 动词
    (re.compile(_ZH_START + r"(?:用|以|使用|改用|换用|換用)(?P<lang>" + _ZH_LANG + r")(?:来|來)?"
                + _ZH_VERB), _ZH_NAMES),
    # 「中文回答我」「……？英文回答。」：语种 + 回答类动词
    (re.compile(_ZH_START + r"(?P<lang>" + _ZH_LANG + r")(?:回答|回复|回覆|作答)"), _ZH_NAMES),
    # 「用英文，谢谢」「请用中文。」：用 + 语种 + 标点/结尾
    (re.compile(_ZH_START + r"(?:用|以)(?P<lang>" + _ZH_LANG + r")(?=\s*(?:[。！!.？?，,；;]|$))"),
     _ZH_NAMES),
    # "Please answer in Chinese" / "Can you reply in Japanese?" / "Explain RAG in English"
    (re.compile(_EN_START + r"(?:answer|reply|respond|write|explain|summari[sz]e|describe|list|tell\s+me)"
                r"\b[^.?!\n]{0,40}?\bin\s+(?P<lang>" + _EN_LANG + r")\b", re.I), _EN_NAMES),
    # "in English please"
    (re.compile(r"\bin\s+(?P<lang>" + _EN_LANG + r")\s*,?\s*(?:please|pls)\b", re.I), _EN_NAMES),
]


def explicit_language_request(text: str) -> str | None:
    """提问里明确要求的回答语言代码；没有明确要求返回 None。"""
    text = text or ""
    found: list[tuple[int, str]] = []
    for pattern, names in _EXPLICIT_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group("lang")
            key = name if names is _ZH_NAMES else re.sub(r"\s+", " ", name.lower())
            found.append((match.start("lang"), names[key]))
    if not found:
        return None
    language = max(found)[1]  # 多个要求时以最后一个为准
    if language == "zh":      # 只说了「中文」：简繁跟随提问本身的写法
        return "zh-TW" if detect_language(text) == "zh-TW" else "zh-CN"
    return language


def question_language(text: str) -> str | None:
    """一句提问自身决定的回答语言：明确要求优先，其次按文字判定；判断不出返回 None。"""
    return explicit_language_request(text) or detect_language(text)


def resolve_answer_language(question: str, history: list[dict] | None = None) -> str:
    """本次回答用的语言：先看当前提问；判断不出看本会话最近的提问（只看用户消息，
    不看历史回答——历史回答本身可能就答错了语言）；仍没有则用默认简体中文。"""
    language = question_language(question)
    if language:
        return language
    for message in reversed(history or []):
        if message.get("role") == "user":
            language = question_language(str(message.get("content") or ""))
            if language:
                return language
    return DEFAULT_LANGUAGE


def language_directive(language: str) -> str:
    """追加在用户消息最末尾的语言指令（用目标语言书写）。"""
    return DIRECTIVES.get(language, DIRECTIVES[SAME_AS_QUESTION])


_CODE_RE = re.compile(r"```[\s\S]*?```|`[^`\n]*`")  # 代码块 / 行内代码（命令、路径）
_ASCII_LETTER = re.compile(r"[A-Za-z]")


def dominant_language(text: str) -> str:
    """一段较长文本（如模型的回答）实际使用的语言，按字符占比统计——供评测与验收使用。

    与 detect_language 的区别：回答里常夹带中文文件名、专有名词，不能「含汉字即中文」；
    中文回答里也常有大段命令。先去掉代码片段，再比较「汉字 ÷（汉字 + 英文字母）」：
    ≥ 25% → 中文（简繁按专用字多少）；假名 ≥ 5 个 → ja；韩文 ≥ 5 个 → ko；否则 en。
    阈值依据 2026-11-08 实测约 200 个回答：中文回答该比例最低 0.40，英文回答约 0.0。
    """
    text = _CODE_RE.sub(" ", text or "")
    if len(_KANA.findall(text)) >= 5:
        return "ja"
    if len(_HANGUL.findall(text)) >= 5:
        return "ko"
    han = len(_HAN.findall(text))
    if han and han / (han + len(_ASCII_LETTER.findall(text))) >= 0.25:
        simplified = sum(1 for c in text if c in _SIMPLIFIED_SET)
        traditional = sum(1 for c in text if c in _TRADITIONAL_SET)
        return "zh-TW" if traditional > simplified else "zh-CN"
    return "en"

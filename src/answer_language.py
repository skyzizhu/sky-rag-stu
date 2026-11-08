"""回答语言判定：回答语言只跟随提问语言。

规则（依次判定，命中即停）：
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

# 指令用目标语言本身书写：英文写的 "Answer in English." 对英文回答的约束力最强
DIRECTIVES = {
    "zh-CN": "请用简体中文回答。",
    "zh-TW": "請用繁體中文回答。",
    "ja": "日本語で回答してください。",
    "ko": "한국어로 답변해 주세요.",
    "en": "Answer in English.",
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
_LATIN = re.compile(r"[A-Za-z\u00c0-\u024f\uff21-\uff3a\uff41-\uff5a]")  # 含全角字母
_LATIN_NON_ENGLISH = re.compile(r"[\u00c0-\u024f]")       # é ñ ü ß 等


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


def resolve_answer_language(question: str, history: list[dict] | None = None) -> str:
    """本次回答用的语言：先看当前提问；判断不出看本会话最近的提问（只看用户消息，
    不看历史回答——历史回答本身可能就答错了语言）；仍没有则用默认简体中文。"""
    language = detect_language(question)
    if language:
        return language
    for message in reversed(history or []):
        if message.get("role") == "user":
            language = detect_language(str(message.get("content") or ""))
            if language:
                return language
    return DEFAULT_LANGUAGE


def language_directive(language: str) -> str:
    """追加在用户消息最末尾的语言指令（用目标语言书写）。"""
    return DIRECTIVES.get(language, DIRECTIVES[SAME_AS_QUESTION])

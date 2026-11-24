"""表单输入提示（Streamlit 内置 InputInstructions）的多语言化。

Streamlit 前端把「Press Enter to submit form」等提示硬编码在 InputInstructions
组件里（输入框有改动时出现），没有任何语言钩子。这里用轻量 v2 组件挂一个
MutationObserver：按当前界面语言把已渲染的提示文本替换掉，React 重绘后再替换、
不会反弹；计数器（12/80 之类）等其他内容一律不动。
"""

from __future__ import annotations

import streamlit as st

_TRANSLATIONS = {
    "zh-CN": {
        "Press Enter to submit form": "按 Enter 提交表单",
        "Press Ctrl+Enter to submit form": "按 Ctrl+Enter 提交表单",
        "Press ⌘+Enter to submit form": "按 ⌘+Enter 提交表单",
        "Press Enter to apply": "按 Enter 应用",
    },
    "zh-TW": {
        "Press Enter to submit form": "按 Enter 送出表單",
        "Press Ctrl+Enter to submit form": "按 Ctrl+Enter 送出表單",
        "Press ⌘+Enter to submit form": "按 ⌘+Enter 送出表單",
        "Press Enter to apply": "按 Enter 套用",
    },
    # 英文界面：原文即英文，无需替换（空映射 = 仅挂监听不做改写）
    "en": {},
    "ja": {
        "Press Enter to submit form": "Enterキーでフォームを送信",
        "Press Ctrl+Enter to submit form": "Ctrl+Enterキーでフォームを送信",
        "Press ⌘+Enter to submit form": "⌘+Enterキーでフォームを送信",
        "Press Enter to apply": "Enterキーで適用",
    },
}

_HTML = '<span class="form-hint-i18n-marker" aria-hidden="true"></span>'
_CSS = """
.form-hint-i18n-marker { display:block; width:1px; height:1px; pointer-events:none; }
"""

_JS = """
export default function(component) {
  const { data } = component;
  const map = data.map || {};
  const translate = (root) => {
    if (!root || !root.querySelectorAll) return;
    const nodes = root.matches && root.matches('[data-testid="InputInstructions"]')
      ? [root] : [...root.querySelectorAll('[data-testid="InputInstructions"]')];
    for (const node of nodes) {
      const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
      let n = walker.nextNode();
      while (n) {
        if (map[n.textContent]) n.textContent = map[n.textContent];
        n = walker.nextNode();
      }
    }
  };
  let scheduled = false;
  const scan = () => { scheduled = false; translate(document.body); };
  const observer = new MutationObserver(() => {
    if (!scheduled) { scheduled = true; setTimeout(scan, 60); }
  });
  observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  scan();
}
"""

_COMPONENT = st.components.v2.component(
    "sky_form_hint_i18n",
    html=_HTML,
    css=_CSS,
    js=_JS,
)


def apply_form_hint_language(language: str) -> None:
    """按界面语言翻译内置表单提示；每个页面渲染调用一次（侧边栏引导区）。"""
    _COMPONENT(key=f"form_hint_{language}",
               data={"map": _TRANSLATIONS.get(language, {})},
               height=1)

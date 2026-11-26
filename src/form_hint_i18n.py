"""Streamlit 内置英文文案的多语言化（通用精确匹配机制）。

Streamlit 前端把部分文案硬编码在前端 JS 里（表单提示 InputInstructions、
multiselect 下拉的「Select all」/「No results」、上传控件的「Drag and drop…」
/「Add files」/「Upload」等），没有任何语言钩子。这里用轻量 v2 组件挂一个
MutationObserver：按当前界面语言对全文档做「整段文本节点精确匹配」替换，
React 重绘后再替换、不会反弹；映射外的文本一律不动。
"""

from __future__ import annotations

import streamlit as st

_TRANSLATIONS = {
    "zh-CN": {
        # 表单输入提示（InputInstructions）
        "Press Enter to submit form": "按 Enter 提交表单",
        "Press Ctrl+Enter to submit form": "按 Ctrl+Enter 提交表单",
        "Press ⌘+Enter to submit form": "按 ⌘+Enter 提交表单",
        "Press Enter to apply": "按 Enter 应用",
        # multiselect / dataframe 内置
        "Select all": "全选",
        "No results": "无结果",
        # 文件上传控件内置
        "Drag and drop a file here": "将文件拖拽到此处",
        "Drag and drop files here": "将文件拖拽到此处",
        "Drag and drop directories here": "将文件夹拖拽到此处",
        "Add files": "添加文件",
        "Upload directories": "上传文件夹",
        "Upload": "上传",
    },
    "zh-TW": {
        "Press Enter to submit form": "按 Enter 送出表單",
        "Press Ctrl+Enter to submit form": "按 Ctrl+Enter 送出表單",
        "Press ⌘+Enter to submit form": "按 ⌘+Enter 送出表單",
        "Press Enter to apply": "按 Enter 套用",
        "Select all": "全選",
        "No results": "無結果",
        "Drag and drop a file here": "將檔案拖放到此處",
        "Drag and drop files here": "將檔案拖放到此處",
        "Drag and drop directories here": "將資料夾拖放到此處",
        "Add files": "新增檔案",
        "Upload directories": "上傳資料夾",
        "Upload": "上傳",
    },
    # 英文界面：原文即英文，无需替换（空映射 = 仅挂监听不做改写）
    "en": {},
    "ja": {
        "Press Enter to submit form": "Enterキーでフォームを送信",
        "Press Ctrl+Enter to submit form": "Ctrl+Enterキーでフォームを送信",
        "Press ⌘+Enter to submit form": "⌘+Enterキーでフォームを送信",
        "Press Enter to apply": "Enterキーで適用",
        "Select all": "すべて選択",
        "No results": "該当なし",
        "Drag and drop a file here": "ここにファイルをドラッグ＆ドロップ",
        "Drag and drop files here": "ここにファイルをドラッグ＆ドロップ",
        "Drag and drop directories here": "ここにフォルダをドラッグ＆ドロップ",
        "Add files": "ファイルを追加",
        "Upload directories": "フォルダをアップロード",
        "Upload": "アップロード",
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
  // 单例守卫：Streamlit 每次 rerun 都会重新调用本函数，旧观察器不清理会随
  // 交互次数线性累积（每次 DOM 变化都重复扫描）。en（空映射）无需监听，直接断开。
  if (globalThis.skyFormHintObserver) globalThis.skyFormHintObserver.disconnect();
  if (!Object.keys(map).length) return;
  const translate = (root) => {
    if (!root) return;
    // 全文档精确匹配：只替换「整段文本与映射完全相等」的节点——内置文案
    // （Select all / Drag and drop… 等）都是完整文本节点；我们自己的 UI 文案
    // 走 t() 不会与英文映射相撞。替换后的节点不再命中映射，不会震荡。
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n = walker.nextNode();
    while (n) {
      if (Object.prototype.hasOwnProperty.call(map, n.textContent)) n.textContent = map[n.textContent];
      n = walker.nextNode();
    }
  };
  let scheduled = false;
  const scan = () => { scheduled = false; translate(document.body); };
  const observer = new MutationObserver(() => {
    if (!scheduled) { scheduled = true; setTimeout(scan, 60); }
  });
  observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  globalThis.skyFormHintObserver = observer;
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
    """按界面语言翻译内置英文文案；每个页面渲染调用一次（侧边栏引导区）。"""
    _COMPONENT(key=f"form_hint_{language}",
               data={"map": _TRANSLATIONS.get(language, {})},
               height=1)

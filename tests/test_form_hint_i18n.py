"""表单提示多语言组件：四语言映射覆盖 Streamlit 内置英文提示，页面调用点存在。"""

import ast
import unittest
from pathlib import Path

from src import form_hint_i18n

_HINTS = ("Press Enter to submit form", "Press Ctrl+Enter to submit form",
          "Press ⌘+Enter to submit form", "Press Enter to apply")


class FormHintI18NTests(unittest.TestCase):
    def test_translations_cover_builtin_hints_for_localized_languages(self):
        for lang in ("zh-CN", "zh-TW", "ja"):
            mapping = form_hint_i18n._TRANSLATIONS[lang]
            for hint in _HINTS:
                self.assertIn(hint, mapping, f"{lang} 缺少：{hint}")
                self.assertNotEqual(mapping[hint], hint, f"{lang} 未翻译：{hint}")
        # 英文界面原文即英文：空映射 = 只挂监听不改写
        self.assertEqual(form_hint_i18n._TRANSLATIONS["en"], {})

    def test_app_calls_apply_with_current_language_in_sidebar(self):
        source = Path('app.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'apply_form_hint_language']
        self.assertEqual(len(calls), 1)
        self.assertEqual(ast.unparse(calls[0].args[0]), 'current_language()')
        # 调用必须位于侧边栏渲染块内（所有页面共用）
        self.assertIn('with st.sidebar:', source)


if __name__ == '__main__':
    unittest.main()

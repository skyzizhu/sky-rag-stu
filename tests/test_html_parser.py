from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.cleaner import clean_document
from src.parser import ParsedDocument, _read_html


class HtmlParserTests(unittest.TestCase):
    def test_preserves_structure_and_removes_page_chrome(self):
        source = """<!doctype html><html><head><meta charset="gb18030"><title>站点标题</title></head>
        <body><header>网站导航噪声</header><main><article>
        <h1>中文正文标题</h1><p>这是一段<strong>连续正文</strong>，不应被行内标签切断。</p>
        <ol><li>第一项</li><li>第二项</li></ol>
        <table><tr><th>名称</th><th>数量</th></tr><tr><td>苹果</td><td>2</td></tr></table>
        <blockquote>重要引用</blockquote><pre>print('ok')</pre>
        <p><a href="https://example.com/docs">参考文档</a></p>
        </article></main><footer>版权与页脚噪声</footer></body></html>"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.html"
            path.write_bytes(source.encode("gb18030"))
            text, title, _ = _read_html(path)

        self.assertEqual(title, "中文正文标题")
        self.assertIn("# 中文正文标题", text)
        self.assertIn("这是一段连续正文，不应被行内标签切断。", text)
        self.assertIn("1. 第一项\n2. 第二项", text)
        self.assertIn("| 名称 | 数量 |", text)
        self.assertIn("> 重要引用", text)
        self.assertIn("```\nprint('ok')\n```", text)
        self.assertIn("[参考文档](https://example.com/docs)", text)
        self.assertNotIn("网站导航噪声", text)
        self.assertNotIn("版权与页脚噪声", text)

    def test_html_cleaning_keeps_short_markdown_structure(self):
        doc = ParsedDocument(
            text="# 标题\n\n- 甲\n- 乙\n\n| A | B |\n| --- | --- |\n| 1 | 2 |",
            metadata={"file_type": "html"},
        )
        cleaned = clean_document(doc).text
        self.assertIn("# 标题", cleaned)
        self.assertIn("- 甲\n- 乙", cleaned)
        self.assertIn("| A | B |", cleaned)


if __name__ == "__main__":
    unittest.main()

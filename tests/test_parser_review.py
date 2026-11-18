"""解析层审查修复（V3.9）的针对性测试。

对应审查发现的 6 项修复：
- 隐藏目录（.rag_converted 转换缓存）不再被入库扫描发现（幻影重复入库）
- docx：表格按文档流顺序输出（不再挪到文档尾部）、嵌套表内容递归读出
- pptx：组合形状（GroupShape）内的文字不再静默丢失
- txt/md：GBK 编码文件回退 GB18030 解码（此前变乱码）
- md 标题：代码块内的 # 行不再被当成标题
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.parser import parse_directory, parse_file
from tests._helpers import IngestHarness


def _make_legacy_samples(base: Path) -> None:
    """造一个隐藏目录里的 docx（模拟 .rag_converted 转换缓存）。"""
    from docx import Document
    hidden = base / ".rag_converted"
    hidden.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_paragraph("PHANTOM_CONTENT_隐藏目录幻影")
    doc.save(hidden / "phantom.docx")


class HiddenDirectoryTests(IngestHarness):
    """幻影入库修复：隐藏目录里的转换缓存不被扫描/解析/元数据调试发现。"""

    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()
        self.tmp = self.knowledge
        self.real = self.tmp / "real.md"
        self.real.write_text("# 真实文档\n\n正文 MD_REAL_FILE\n", encoding="utf-8")
        _make_legacy_samples(self.tmp)

    def test_ingest_scan_skips_hidden_directory(self):
        """入库扫描走 pipeline._skip_hidden：只发现真实文件，幻影 docx 被跳过。"""
        from src.pipeline import _skip_hidden
        kept = sorted(
            p for p in self.tmp.rglob("*")
            if p.is_file() and not p.name.startswith(".")
            and p.suffix.lower() in {".md", ".docx"}
            and not _skip_hidden(p, self.tmp)
        )
        self.assertEqual([p.name for p in kept], ["real.md"])

    def test_ingest_end_to_end_ignores_hidden_converts(self):
        """端到端：ingest_files 扫描 knowledge 目录时忽略 .rag_converted 缓存。"""
        import contextlib
        import io
        from src.pipeline import ingest_files
        with contextlib.redirect_stdout(io.StringIO()):
            summary = ingest_files(rebuild=True, config=self.cfg,
                                   embedding_client=self.embedding, store=self.store)
        sources = [row["source"] for row in summary.file_rows]
        self.assertEqual(sources, ["real.md"])

    def test_parse_directory_skips_hidden_directory(self):
        docs, failures = parse_directory(self.tmp)
        names = [d.metadata["source"] for d in docs]
        self.assertEqual(names, ["real.md"])

    def test_metadata_debug_scan_skips_hidden_directory(self):
        """元数据调试脚本同一过滤口径。"""
        files = sorted(
            p for p in self.tmp.rglob("*")
            if p.is_file() and not p.name.startswith(".")
            and p.suffix.lower() in {".md", ".docx"}
            and not any(part.startswith(".") for part in p.relative_to(self.tmp).parts)
        )
        self.assertEqual([p.name for p in files], ["real.md"])


class DocxReaderTests(unittest.TestCase):
    """docx：阅读顺序 / 嵌套表 / 页眉页脚。"""

    def setUp(self) -> None:
        from docx import Document
        self.doc = Document()
        self.doc.add_heading("一级标题", level=1)
        self.doc.add_paragraph("表格前的段落 BEFORE_TABLE")
        table = self.doc.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "A1"
        table.cell(0, 1).text = "B1"
        # 嵌套表：单元格内再放一张表
        nested = table.cell(1, 0).add_table(rows=1, cols=1)
        nested.cell(0, 0).text = "NESTED_CONTENT"
        table.cell(1, 1).text = "B2"
        self.doc.add_paragraph("表格后的段落 AFTER_TABLE")
        header = self.doc.sections[0].header
        header.paragraphs[0].text = "文档页眉 HEADER_TEXT"

    def test_reading_order_preserved(self):
        """表格按文档流位置出现（修复前全部挪到文档尾部）。"""
        doc = parse_file(self._save("order.docx"))
        i_before = doc.text.index("BEFORE_TABLE")
        i_table = doc.text.index("A1")
        i_after = doc.text.index("AFTER_TABLE")
        self.assertLess(i_before, i_table)
        self.assertLess(i_table, i_after)

    def test_nested_table_content_extracted(self):
        doc = parse_file(self._save("nested.docx"))
        self.assertIn("NESTED_CONTENT", doc.text)

    def test_header_text_extracted(self):
        doc = parse_file(self._save("header.docx"))
        self.assertIn("HEADER_TEXT", doc.text)

    def _save(self, name: str) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="sky-docx-")) / name
        self.doc.save(tmp)
        self.addCleanup(lambda: tmp.parent.exists() and __import__("shutil").rmtree(tmp.parent, True))
        return tmp


class PptxGroupShapeTests(unittest.TestCase):
    """pptx 组合形状内的文字不再丢失。"""

    def test_grouped_text_extracted(self):
        from pptx import Presentation
        from pptx.util import Inches as PI
        pr = Presentation()
        slide = pr.slides.add_slide(pr.slide_layouts[6])  # 空白版式
        # 手工构造组合：python-pptx 1.0+ 支持 shapes.add_group_shape
        group = slide.shapes.add_group_shape()
        tb1 = group.shapes.add_textbox(PI(0), PI(0), PI(2), PI(1))
        tb1.text_frame.text = "GROUP_TEXT_组合内文字"
        tb2 = group.shapes.add_textbox(PI(0), PI(2), PI(2), PI(1))
        tb2.text_frame.text = "GROUP_TEXT_第二个"
        tmp = Path(tempfile.mkdtemp(prefix="sky-pptx-")) / "group.pptx"
        pr.save(tmp)
        self.addCleanup(lambda: tmp.parent.exists() and __import__("shutil").rmtree(tmp.parent, True))
        doc = parse_file(tmp)
        self.assertIn("GROUP_TEXT_组合内文字", doc.text)
        self.assertIn("GROUP_TEXT_第二个", doc.text)


class TextEncodingTests(unittest.TestCase):
    """GBK/GB2312 文本文件回退 GB18030 解码（修复前变 U+FFFD 乱码）。"""

    def test_gbk_file_decodes_correctly(self):
        tmp = Path(tempfile.mkdtemp(prefix="sky-enc-")) / "gbk.txt"
        tmp.write_bytes("中文内容编码测试：GBK_SAMPLE".encode("gb18030"))
        self.addCleanup(lambda: tmp.parent.exists() and __import__("shutil").rmtree(tmp.parent, True))
        doc = parse_file(tmp)
        self.assertIn("GBK_SAMPLE", doc.text)
        self.assertNotIn("\ufffd", doc.text)

    def test_utf8_file_unaffected(self):
        tmp = Path(tempfile.mkdtemp(prefix="sky-enc-")) / "utf8.txt"
        tmp.write_text("UTF8_SAMPLE 内容", encoding="utf-8")
        self.addCleanup(lambda: tmp.parent.exists() and __import__("shutil").rmtree(tmp.parent, True))
        doc = parse_file(tmp)
        self.assertIn("UTF8_SAMPLE", doc.text)


class MdTitleTests(unittest.TestCase):
    """md 标题：代码块内的 # 行不算标题。"""

    def test_hash_in_code_block_not_title(self):
        from src.parser import _read_markdown
        tmp = Path(tempfile.mkdtemp(prefix="sky-md-")) / "code.md"
        tmp.write_text("# 真标题\n\n```bash\n# 这是代码注释不是标题\n```\n", encoding="utf-8")
        self.addCleanup(lambda: tmp.parent.exists() and __import__("shutil").rmtree(tmp.parent, True))
        _, title, _ = _read_markdown(tmp)
        self.assertEqual(title, "真标题")

    def test_title_inside_code_block_when_no_real_h1(self):
        """无真 H1 时：不把代码块注释误当标题（宁可空标题）。"""
        from src.parser import _read_markdown
        tmp = Path(tempfile.mkdtemp(prefix="sky-md-")) / "code_only.md"
        tmp.write_text("```python\n# 只有代码块里的井号\nprint(1)\n```\n", encoding="utf-8")
        self.addCleanup(lambda: tmp.parent.exists() and __import__("shutil").rmtree(tmp.parent, True))
        _, title, _ = _read_markdown(tmp)
        self.assertEqual(title, "")


if __name__ == "__main__":
    unittest.main()

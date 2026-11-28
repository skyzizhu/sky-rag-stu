"""2026-11-27 入库格式体检（benchmarks/ingest_format_audit_20261127）修复的回归测试。

覆盖报告编号：M1 M2 W1 W2 W3 W4 W5 W6 W7 E1 E2 E3 E4 E5 E6 G1 G2 G3 G7 I1 I2 T2 U2 U4 U5。
样例全部程序构造（与体检探测同源），不触碰正式 knowledge/ 与 storage/。
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from src.parser import ParseError, parse_file
from tests._helpers import IngestHarness


def _save_doc(doc, name: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="sky-fmt-"))
    path = tmp / name
    doc.save(str(path))
    return path


def _write(path_dir: Path, name: str, data, encoding: str | None = None) -> Path:
    path_dir.mkdir(parents=True, exist_ok=True)
    path = path_dir / name
    if isinstance(data, str):
        path.write_text(data, encoding=encoding)
    else:
        path.write_bytes(data)
    return path


# ---------------------------------------------------------------- 文本编码（M1 M2）
class TextEncodingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sky-enc2-"))

    def test_utf16_bom_decoded(self):
        doc = parse_file(_write(self.tmp, "u16.txt", "潜力统计调查制度", "utf-16"))
        self.assertIn("潜力统计调查制度", doc.text)

    def test_utf16_without_bom_detected(self):
        doc = parse_file(_write(self.tmp, "u16nb.txt", "ABC12测试".encode("utf-16-le")))
        self.assertIn("ABC12测试", doc.text)

    def test_pdf_bytes_named_txt_rejected(self):
        with self.assertRaises(ParseError) as ctx:
            parse_file(_write(self.tmp, "fake.txt", b"%PDF-1.4 pretend"))
        self.assertIn("后缀", str(ctx.exception))

    def test_binary_named_txt_rejected(self):
        payload = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(range(256)) * 8
        with self.assertRaises(ParseError):
            parse_file(_write(self.tmp, "bin.txt", payload))

    def test_markdown_bom_keeps_front_matter(self):
        raw = "---\ntitle: 前言标题\n---\n\n# 正文标题\n\n内容".encode("utf-8-sig")
        doc = parse_file(_write(self.tmp, "bom.md", raw))
        self.assertEqual(doc.front_matter.get("title"), "前言标题")
        self.assertEqual(doc.metadata["title"], "正文标题")


# ---------------------------------------------------------------- Word（W1-W7，G3）
class DocxFixTests(unittest.TestCase):
    def _document(self):
        from docx import Document

        return Document()

    def test_tracked_changes_kept_as_accepted(self):
        from docx.oxml import parse_xml
        from docx.oxml.ns import nsdecls

        doc = self._document()
        para = doc.add_paragraph()
        para._p.append(parse_xml(
            f'<w:ins {nsdecls("w")} w:id="1" w:author="审阅人" w:date="2026-01-01T00:00:00Z">'
            '<w:r><w:t>REVISE_INSERT_新增要求</w:t></w:r></w:ins>'))
        para._p.append(parse_xml(
            f'<w:del {nsdecls("w")} w:id="2" w:author="审阅人" w:date="2026-01-01T00:00:00Z">'
            '<w:r><w:delText>REVISE_DELETE_旧要求</w:delText></w:r></w:del>'))
        path = _save_doc(doc, "tracked.docx")
        parsed = parse_file(path)
        self.assertIn("REVISE_INSERT_新增要求", parsed.text)
        self.assertNotIn("REVISE_DELETE_旧要求", parsed.text)
        self.assertIn("修订", parsed.front_matter.get("source_note", ""))

    def test_content_control_text_extracted(self):
        from docx.oxml import parse_xml
        from docx.oxml.ns import nsdecls, qn

        doc = self._document()
        body = doc._body._element
        sect_pr = body.find(qn("w:sectPr"))
        sect_pr.addprevious(parse_xml(
            f'<w:sdt {nsdecls("w")}><w:sdtPr/><w:sdtContent>'
            '<w:p><w:r><w:t>SDT_BLOCK_目录域文字</w:t></w:r></w:p></w:sdtContent></w:sdt>'))
        inline = doc.add_paragraph()
        inline._p.append(parse_xml(
            f'<w:sdt {nsdecls("w")}><w:sdtContent><w:r><w:t>SDT_INLINE_控件文字</w:t></w:r></w:sdtContent></w:sdt>'))
        text = parse_file(_save_doc(doc, "sdt.docx")).text
        self.assertIn("SDT_BLOCK_目录域文字", text)
        self.assertIn("SDT_INLINE_控件文字", text)

    def test_list_number_and_bullet_markers(self):
        doc = self._document()
        doc.add_paragraph("NUM_STEP_下达通知", style="List Number")
        doc.add_paragraph("NUM_STEP_上报数据", style="List Number")
        doc.add_paragraph("BULLET_无序要点", style="List Bullet")
        text = parse_file(_save_doc(doc, "lists.docx")).text
        lines = [line.strip() for line in text.splitlines()]
        self.assertTrue(any(line.startswith("1. ") and "NUM_STEP_下达通知" in line for line in lines))
        self.assertTrue(any(line.startswith("2. ") and "NUM_STEP_上报数据" in line for line in lines))
        self.assertTrue(any(line.startswith("- ") and "BULLET_无序要点" in line for line in lines))

    def test_outline_level_becomes_heading(self):
        from docx.oxml import parse_xml
        from docx.oxml.ns import nsdecls

        doc = self._document()
        para = doc.add_paragraph("OUTLINE_仅设大纲级别的标题")
        para._p.get_or_add_pPr().append(parse_xml(f'<w:outlineLvl {nsdecls("w")} w:val="1"/>'))
        lines = parse_file(_save_doc(doc, "outline.docx")).text.splitlines()
        self.assertTrue(any(line.startswith("## ") and "OUTLINE_" in line for line in lines))

    def test_merged_cells_not_repeated(self):
        doc = self._document()
        table = doc.add_table(rows=3, cols=3)
        table.cell(0, 0).text = "MHDR"
        table.cell(0, 0).merge(table.cell(0, 1))
        table.cell(0, 2).text = "r1c3"
        table.cell(1, 0).text = "VMERGE"
        table.cell(1, 0).merge(table.cell(2, 0))
        for row, col, value in ((1, 1, "r2c2"), (1, 2, "r2c3"), (2, 1, "r3c2"), (2, 2, "r3c3")):
            table.cell(row, col).text = value
        text = parse_file(_save_doc(doc, "merged.docx")).text
        self.assertEqual(text.count("MHDR"), 1)
        self.assertEqual(text.count("VMERGE"), 1)
        self.assertIn("r2c2", text)

    def test_linked_header_deduplicated(self):
        from docx.enum.section import WD_SECTION

        doc = self._document()
        doc.add_paragraph("SEC1_第一节")
        doc.sections[0].header.paragraphs[0].text = "HEADER_A_公司文件"
        doc.add_section(WD_SECTION.NEW_PAGE)
        doc.add_paragraph("SEC2_第二节")
        self.assertEqual(parse_file(_save_doc(doc, "sec.docx")).text.count("HEADER_A_公司文件"), 1)

    def test_core_property_title_fallback(self):
        doc = self._document()
        doc.add_paragraph("没有标题样式的正文。")
        doc.core_properties.title = "CORE_TITLE_属性标题"
        self.assertEqual(parse_file(_save_doc(doc, "plain.docx")).metadata["title"], "CORE_TITLE_属性标题")

    def test_embedded_image_ocr_placed_inline(self):
        from PIL import Image

        img = self.tmp_dir() / "flow.png"
        Image.new("RGB", (600, 200), "white").save(img)
        doc = self._document()
        doc.add_paragraph("IMG_BEFORE_下图是设备连接示意")
        doc.add_paragraph().add_run().add_picture(str(img))
        doc.add_paragraph("IMG_AFTER_如图所示")
        with mock.patch("src.parser._ocr_image", return_value="IMGOCR_图内文字"):
            text = parse_file(_save_doc(doc, "img.docx")).text
        before, ocr, after = text.find("IMG_BEFORE"), text.find("IMGOCR_图内文字"), text.find("IMG_AFTER")
        self.assertTrue(0 <= before < ocr < after, f"OCR 文字应在图片所在位置：{before} <= {ocr} < {after}")

    def test_image_placeholder_when_ocr_disabled(self):
        from PIL import Image

        from src.config import get_config

        img = self.tmp_dir() / "p.png"
        Image.new("RGB", (600, 200), "white").save(img)
        doc = self._document()
        doc.add_paragraph().add_run().add_picture(str(img))
        with mock.patch("src.parser.get_config", return_value=replace(get_config(), ocr_enabled=False)):
            text = parse_file(_save_doc(doc, "alt.docx")).text
        self.assertIn("[图片：", text)

    def tmp_dir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="sky-fmt-img-"))


# ---------------------------------------------------------------- Excel（E1-E6）
class XlsxFixTests(unittest.TestCase):
    def _workbook(self):
        from openpyxl import Workbook

        return Workbook()

    def test_number_formats_rendered_as_displayed(self):
        import datetime

        wb = self._workbook()
        ws = wb.active
        ws.append(["指标", "数值"])
        for label, value, fmt in (("占比", 0.15, "0%"), ("金额", 1234567.89, "¥#,##0.00"),
                                  ("布尔", True, "General"), ("编号", 123456789012345678, "0")):
            ws.append([label, value])
            ws.cell(row=ws.max_row, column=2).number_format = fmt
        ws.append(["日期", datetime.datetime(2026, 5, 6)])
        text = ParsedXlsxText(wb, "fmt.xlsx").text
        self.assertIn("占比 | 15%", text)
        self.assertIn("金额 | ¥1,234,567.89", text)
        self.assertIn("布尔 | 是", text)
        self.assertIn("编号 | 123456789012345696", text)  # 存储值就是它，但不再是科学计数法
        self.assertIn("日期 | 2026-05-06", text)

    def test_title_row_and_multilevel_header_with_fill(self):
        wb = self._workbook()
        ws = wb.active
        ws["A1"] = "某市潜力统计表"
        ws.merge_cells("A1:C1")
        ws.merge_cells("A2:A3")
        ws["A2"] = "类别"
        ws.merge_cells("B2:C2")
        ws["B2"] = "资源情况"
        ws["B3"], ws["C3"] = "名称", "数量"
        for row, values in ((4, ("医疗类", "执业医师", "人")), (5, (None, "注册护士", "人")),
                            (6, ("交通类", "平板车", "辆")), (7, (None, "大客车", "辆"))):
            for col, value in zip("ABC", values):
                ws[f"{col}{row}"] = value
        ws.merge_cells("A4:A5")
        ws.merge_cells("A6:A7")
        from src.parser import _read_xlsx

        path = Path(tempfile.mkdtemp(prefix="sky-fmt-x-")) / "gov.xlsx"
        wb.save(str(path))
        text = _read_xlsx(path)[0]
        self.assertIn("某市潜力统计表", text.splitlines()[1])  # 标题行单独出现
        self.assertIn("类别 | 名称 | 数量", text)  # 多级表头拼接
        self.assertIn("医疗类 | 注册护士 | 人", text)  # 类别列向下补值
        self.assertIn("交通类 | 大客车 | 辆", text)
        self.assertNotIn("资源情况", text)

    def test_hidden_sheet_row_column_skipped(self):
        wb = self._workbook()
        ws = wb.active
        ws.append(["项目", "预算", "内部列"])
        ws.append(["甲项目", 100, "HIDDEN_COL_内容"])
        ws.append(["乙项目", 200, "普通"])
        ws.column_dimensions["C"].hidden = True
        ws.row_dimensions[3].hidden = True
        hidden = wb.create_sheet("内部计算")
        hidden.sheet_state = "hidden"
        hidden["A1"] = "HIDDEN_SHEET_内容"
        text = ParsedXlsxText(wb, "hidden.xlsx").text
        self.assertNotIn("HIDDEN_COL_内容", text)
        self.assertNotIn("乙项目", text)
        self.assertNotIn("HIDDEN_SHEET_内容", text)
        self.assertIn("甲项目 | 100", text)

    def test_uncached_formula_marked(self):
        wb = self._workbook()
        ws = wb.active
        ws.append(["项目", "合计"])
        ws.append(["乙项目", "=SUM(100,50)"])
        text = ParsedXlsxText(wb, "formula.xlsx").text
        self.assertIn("乙项目 | [公式]", text)

    def test_cell_newline_normalized(self):
        wb = self._workbook()
        ws = wb.active
        ws.append(["项目", "备注"])
        ws.append(["甲项目", "第一行\n第二行"])
        text = ParsedXlsxText(wb, "nl.xlsx").text
        self.assertIn("甲项目 | 第一行 / 第二行", text)

    def test_truncation_reported_in_source_note(self):
        import src.parser as parser_module

        wb = self._workbook()
        ws = wb.active
        ws.append(["序号", "内容"])
        for index in range(1, 9):
            ws.append([index, f"第{index}行"])
        path = Path(tempfile.mkdtemp(prefix="sky-fmt-x-")) / "cap.xlsx"
        wb.save(str(path))
        with mock.patch.object(parser_module, "XLSX_MAX_ROWS", 4):
            text, _, front_matter = parser_module._read_xlsx(path)
        self.assertIn("超过 4 行", text)
        self.assertIn("source_note", front_matter)


class ParsedXlsxText:
    """wb → 解析文本的小助手（保存到临时目录后走真实解析器）。"""

    def __init__(self, workbook, name: str):
        from src.parser import _read_xlsx

        path = Path(tempfile.mkdtemp(prefix="sky-fmt-x-")) / name
        workbook.save(str(path))
        self.text = _read_xlsx(path)[0]


# ---------------------------------------------------------------- OCR（G2 I1 I2 G3）
class OcrFixTests(unittest.TestCase):
    def _patch_config(self, min_confidence: float = 0.6):
        from src.config import get_config

        patcher = mock.patch("src.parser.get_config", return_value=replace(
            get_config(), ocr_enabled=True, ocr_min_confidence=min_confidence))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_low_confidence_lines_filtered_and_duplicates_cached(self):
        from PIL import Image

        self._patch_config(0.6)
        image = Image.new("RGB", (600, 300), "white")
        path = Path(tempfile.mkdtemp(prefix="sky-fmt-ocr-")) / "a.png"
        image.save(path)
        calls = {"count": 0}

        def fake_engine(pixels):
            calls["count"] += 1
            box = [[0, 0], [10, 10]]
            return ([[box, "GOOD_高置信", 0.95], [box, "BAD_低置信噪声", 0.30]], 0.0)

        with mock.patch("src.parser._get_ocr_engine", return_value=fake_engine):
            from src.parser import _ocr_image

            blob = path.read_bytes()
            first = _ocr_image(blob)
            second = _ocr_image(blob)
        self.assertIn("GOOD_高置信", first)
        self.assertNotIn("BAD_低置信噪声", first)
        self.assertEqual(second, first)
        self.assertEqual(calls["count"], 1)  # 相同图片第二次命中缓存

    def test_transparent_png_composited_on_white(self):
        import numpy as np
        from PIL import Image

        self._patch_config()
        image = Image.new("RGBA", (600, 300), (0, 0, 0, 0))
        path = Path(tempfile.mkdtemp(prefix="sky-fmt-ocr-")) / "t.png"
        image.save(path)
        seen = {}

        def fake_engine(pixels):
            seen["corner"] = pixels[0, 0].tolist()
            return ([[[0, 0], "ANY", 0.99]], 0.0)  # (result, 耗时)；result=[item]；item=[坐标框, 文本, 置信度]

        with mock.patch("src.parser._get_ocr_engine", return_value=fake_engine):
            from src.parser import _ocr_image

            _ocr_image(path.read_bytes())
        self.assertTrue(all(channel >= 250 for channel in seen["corner"]),
                        f"透明底应铺白：{seen['corner']}")

    def test_multipage_tiff_each_frame_ocrd(self):
        from PIL import Image, ImageDraw

        self._patch_config()
        frames = []
        for index in range(2):
            frame = Image.new("RGB", (900, 300), "white")
            ImageDraw.Draw(frame).rectangle([10, 10, 200, 60], fill="black")
            frames.append(frame)
        path = Path(tempfile.mkdtemp(prefix="sky-fmt-ocr-")) / "m.tif"
        frames[0].save(str(path), save_all=True, append_images=frames[1:])
        calls = {"count": 0}

        def fake_engine(pixels):
            calls["count"] += 1
            return [[[0, 0], f"FRAME{calls['count']}_页", 0.99]], 0.0

        with mock.patch("src.parser._get_ocr_engine", return_value=fake_engine):
            from src.parser import _read_image

            text = _read_image(path)[0]
        self.assertEqual(calls["count"], 2)
        self.assertIn("FRAME1_页", text)
        self.assertIn("FRAME2_页", text)

    def test_image_title_from_first_ocr_line(self):
        from PIL import Image

        self._patch_config()
        path = Path(tempfile.mkdtemp(prefix="sky-fmt-ocr-")) / "t.png"
        Image.new("RGB", (600, 300), "white").save(path)
        with mock.patch("src.parser._ocr_image", return_value="设备巡检记录表\n巡检人：张三"):
            parsed = parse_file(path)
        self.assertEqual(parsed.metadata["title"], "设备巡检记录表")


# ---------------------------------------------------------------- PPT（T2）
class PptxHiddenSlideTests(unittest.TestCase):
    def test_hidden_slide_skipped(self):
        from pptx import Presentation
        from pptx.util import Inches as PI

        deck = Presentation()
        for index in (1, 2):
            slide = deck.slides.add_slide(deck.slide_layouts[6])
            slide.shapes.add_textbox(PI(0), PI(0), PI(4), PI(1)).text_frame.text = f"SLIDE{index}_正文"
        deck.slides[1]._element.set("show", "0")
        text = parse_file(_save_doc(deck, "hidden.pptx")).text
        self.assertIn("SLIDE1_正文", text)
        self.assertNotIn("SLIDE2_正文", text)


# ---------------------------------------------------------------- 网页（U2 U4 U5）
class WebFetcherTests(unittest.TestCase):
    def test_non_html_content_type_rejected(self):
        from src.web_fetcher import _ensure_web_content

        with self.assertRaises(ValueError) as ctx:
            _ensure_web_content(b"%PDF-1.4", "application/pdf")
        self.assertIn("PDF", str(ctx.exception))
        _ensure_web_content(b"%PDF-1.4 fake", "text/html")  # 服务器标错但内容是网页 → 放行不报错

    def test_decode_html_prefers_header_then_meta_then_detection(self):
        from src.web_fetcher import _decode_html

        phrase = "国防动员潜力统计调查制度"
        gbk_body = phrase.encode("gb18030")
        # 响应头声明优先
        self.assertEqual(_decode_html(gbk_body, "text/html; charset=gbk"), phrase)
        # 响应头缺失 → 页面 meta 声明
        html_body = f'<html><head><meta charset="gbk"></head><body>{phrase}</body></html>'.encode("gb18030")
        decoded = _decode_html(html_body, "text/html")
        self.assertIn(phrase, decoded)
        self.assertNotIn("\ufffd", decoded)
        # 都没有 → 自动探测（长文本才可靠；断言不出坏字、认得出中文即可）
        detected = _decode_html(phrase.encode("gb18030") * 200, "text/html")
        self.assertNotIn("\ufffd", detected)
        self.assertIn("国防动员", detected)

    def test_extract_text_uses_shared_html_parser(self):
        from src.web_fetcher import extract_text

        page = ("<html><body><main><h1>通知</h1><p>正文说明文字，长度足够不被合并。</p>"
                "<table><tr><th>单位</th><th>数量</th></tr><tr><td>第一医院</td><td>1200</td></tr></table>"
                "</main></body></html>")
        result = extract_text(page)
        self.assertIn("# 通知", result["text"])
        self.assertIn("| 单位 | 数量 |", result["text"])
        self.assertIn("第一医院 | 1200", result["text"])

    def test_snapshot_title_with_colon_keeps_origin(self):
        from src.web_fetcher import read_snapshot_origin, save_web_snapshot

        root = Path(tempfile.mkdtemp(prefix="sky-fmt-web-"))
        target = root / "work"
        snapshot = save_web_snapshot("https://example.com/faq", "网页正文内容。" * 20, "FAQ: 常见问题解答",
                                     domain="work", knowledge_dir=root, destination=target)
        origin = read_snapshot_origin(snapshot)
        self.assertIsNotNone(origin)
        self.assertEqual(origin["url"], "https://example.com/faq")
        self.assertEqual(origin["title"], "FAQ: 常见问题解答")
        parsed = parse_file(snapshot)
        self.assertEqual(parsed.front_matter.get("source_url"), "https://example.com/faq")


# ---------------------------------------------------------------- 其他（G7 G3-pdf G1）
class MiscFixTests(unittest.TestCase):
    def test_doc_type_inference_for_new_extensions(self):
        from src.document_type import infer_doc_type_from_filename

        self.assertEqual(infer_doc_type_from_filename("合同.jpg"), "合同")
        self.assertEqual(infer_doc_type_from_filename("需求文档.tiff"), "需求文档")

    def test_pdf_title_metadata_used(self):
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.add_metadata({"/Title": "PDF_META_文档属性标题"})
        path = Path(tempfile.mkdtemp(prefix="sky-fmt-pdf-")) / "blank.pdf"
        with open(path, "wb") as handle:
            writer.write(handle)
        self.assertEqual(parse_file(path).metadata["title"], "PDF_META_文档属性标题")


class ExtractionGateTests(IngestHarness):
    """报告 G1：疑似提取失败的文件默认不入库、不记台账指纹；关闭体检恢复旧行为。"""

    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()

    def _garbage_file(self) -> Path:
        path = self.knowledge / "learning" / "general" / "garbage.txt"
        path.write_text("正常开头。" * 10 + "\ufffd" * 60, encoding="utf-8")
        return path

    def test_mojibake_file_blocked_and_not_ledgered(self):
        path = self._garbage_file()
        summary = self.ingest([path])
        self.assertTrue(any("疑似提取失败" in failure for failure in summary.failed_files))
        self.assertNotIn("learning/general/garbage.txt", self.ledger())

    def test_gate_disabled_restores_old_behavior(self):
        path = self._garbage_file()
        from dataclasses import replace as dc_replace

        import contextlib
        import io

        from src.pipeline import ingest_files

        cfg = dc_replace(self.cfg, extraction_gate=False)
        with contextlib.redirect_stdout(io.StringIO()):
            summary = ingest_files(paths=[path], config=cfg, embedding_client=self.embedding,
                                   store=self.store)
        self.assertFalse(any("疑似提取失败" in failure for failure in summary.failed_files))

    def test_clean_file_passes_gate(self):
        path = self.knowledge / "learning" / "general" / "good.txt"
        path.write_text("# 正常文档\n\n这是一段完全正常的中文内容，没有任何乱码字符。\n", encoding="utf-8")
        summary = self.ingest([path])
        self.assertEqual(summary.ok_files, 1)
        self.assertNotIn("learning/general/good.txt", [f for f in summary.failed_files])


# ---------------------------------------------------------------- CSV/TSV（E7）
class CsvTsvTests(unittest.TestCase):
    def _parse(self, name: str, data, encoding: str | None = None):
        tmp = Path(tempfile.mkdtemp(prefix="sky-fmt-csv-"))
        doc = parse_file(_write(tmp, name, data, encoding))
        return doc

    def test_csv_basic_grouping_and_header_repeat(self):
        rows = ["项目,预算", *[f"P{i},{i * 10}" for i in range(1, 7)]]
        doc = self._parse("t.csv", "\n".join(rows), "utf-8")
        self.assertIn("## Sheet：t", doc.text)
        self.assertIn("# t（第 1~4 行）", doc.text)
        self.assertIn("# t（第 5~6 行）", doc.text)
        self.assertEqual(doc.text.count("项目 | 预算"), 2)  # 每组重复表头
        self.assertIn("P6 | 60", doc.text)

    def test_csv_quoted_pipe_and_newline(self):
        content = '名称,备注\n"管道| внутри","第一行\n第二行"'
        doc = self._parse("quoted.csv", content, "utf-8")
        self.assertIn("管道\\| внутри | 第一行 / 第二行", doc.text)

    def test_csv_gbk_encoding(self):
        doc = self._parse("gbk.csv", "项目,数量\n医疗队,5".encode("gb18030"))
        self.assertIn("医疗队 | 5", doc.text)
        self.assertNotIn("\ufffd", doc.text)

    def test_tsv_tab_delimited(self):
        doc = self._parse("t.tsv", "单位\t数量\n平板车\t80", "utf-8")
        self.assertIn("单位 | 数量", doc.text)
        self.assertIn("平板车 | 80", doc.text)

    def test_title_line_before_header(self):
        doc = self._parse("title.csv", "某市潜力统计表\n项目,数量\n医疗队,5", "utf-8")
        self.assertIn("某市潜力统计表", doc.text.splitlines()[1])
        self.assertIn("项目 | 数量", doc.text)

    def test_truncation_reported_in_source_note(self):
        import src.parser as parser_module

        rows = ["项目,预算", *[f"P{i},{i}" for i in range(1, 7)]]
        tmp = Path(tempfile.mkdtemp(prefix="sky-fmt-csv-"))
        path = _write(tmp, "cap.csv", "\n".join(rows), "utf-8")
        with mock.patch.object(parser_module, "XLSX_MAX_ROWS", 3):
            text, _, front_matter = parser_module._read_csv(path)
        self.assertIn("超过 3 行", text)
        self.assertIn("source_note", front_matter)


# ---------------------------------------------------------------- OCR 时间预算（决策3）
class OcrBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        import src.parser as parser_module

        parser_module._OCR_CACHE.clear()  # 缓存跨文件共享（预期行为），测试间清掉避免串扰

    def _docx_with_images(self, count: int) -> Path:
        from docx import Document
        from PIL import Image

        img_dir = Path(tempfile.mkdtemp(prefix="sky-fmt-budget-"))
        doc = Document()
        for index in range(count):
            img = img_dir / f"i{index}.png"
            Image.new("RGB", (600, 200), (255 - index, 255, 255)).save(img)  # 颜色不同 → 字节不同，避开缓存
            doc.add_paragraph().add_run().add_picture(str(img))
        return _save_doc(doc, "budget.docx")

    def _patch_config(self, budget: float):
        from src.config import get_config

        patcher = mock.patch("src.parser.get_config", return_value=replace(
            get_config(), ocr_enabled=True, ocr_min_confidence=0.6, ocr_time_budget=budget))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_budget_exhausted_skips_remaining_images(self):
        self._patch_config(1.0)  # 预算 1 秒；每张图耗时 1.2 秒 → 第二张起必超时
        import time as time_module

        def fake_engine(pixels):
            time_module.sleep(1.2)
            return ([[[0, 0], "OCR_BUDGET_图内文字", 0.99]], 0.0)

        with mock.patch("src.parser._get_ocr_engine", return_value=fake_engine):
            parsed = parse_file(self._docx_with_images(2))
        self.assertEqual(parsed.text.count("OCR_BUDGET_图内文字"), 1)  # 第一张识别，第二张跳过
        self.assertIn("1 张图片未识别", parsed.front_matter.get("source_note", ""))

    def test_zero_budget_means_unlimited(self):
        self._patch_config(0.0)

        def fake_engine(pixels):
            return ([[[0, 0], "OCR_FREE_图内文字", 0.99]], 0.0)

        with mock.patch("src.parser._get_ocr_engine", return_value=fake_engine):
            parsed = parse_file(self._docx_with_images(2))
        self.assertEqual(parsed.text.count("OCR_FREE_图内文字"), 2)
        self.assertNotIn("未识别", parsed.front_matter.get("source_note", ""))


# ---------------------------------------------------------------- 目录导入过滤（决策6/G5）
class DirectoryFilterTests(unittest.TestCase):
    def test_split_supported_entries(self):
        from src.upload_plan import split_supported_entries

        entries = [(("资料", "a.md"), b"1"), (("资料", "b.exe"), b"2"),
                   (("资料", "c.csv"), b"3"), (("资料", "子目录", "d.txt"), b"4")]
        supported, unsupported = split_supported_entries(entries)
        self.assertEqual([entry[0][-1] for entry in supported], ["a.md", "c.csv", "d.txt"])
        self.assertEqual(unsupported, [1])


if __name__ == "__main__":
    unittest.main()

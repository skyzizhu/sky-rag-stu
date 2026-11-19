"""版本声明候选区域：只读已有正文/OCR，不推断版本、不访问外部服务。"""
from __future__ import annotations

import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from docx import Document
from docx.enum.section import WD_SECTION_START
from docx.enum.text import WD_BREAK
from PIL import Image
from pptx import Presentation
from pptx.util import Inches
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from src import parser
from tests._helpers import IngestHarness


def _region(text: str, location: str, source: str = "content") -> dict:
    return {"source": source, "location": location, "text": text,
            "authoritative": source == "content"}


def _image() -> io.BytesIO:
    stream = io.BytesIO()
    Image.new("RGB", (240, 240), "white").save(stream, format="PNG")
    stream.seek(0)
    return stream


def _write_pdf(path: Path, texts: list[str]) -> None:
    """无需 PDF 渲染器的最小有效文字层样例。"""
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        if not text:
            continue
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = stream
    with path.open("wb") as output:
        writer.write(output)


class VersionRegionTests(unittest.TestCase):
    """仅解析样例；系统临时目录 + 配置副本 + 全局 OCR 桩。"""

    def setUp(self) -> None:
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="sky-version-regions-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        cfg = replace(parser.get_config(), knowledge_dir=self.root, ocr_enabled=False)
        config_patch = mock.patch.object(parser, "get_config", return_value=cfg)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        ocr_patch = mock.patch.object(parser, "_ocr_image", return_value="")
        self.ocr = ocr_patch.start()
        self.addCleanup(ocr_patch.stop)
        engine_patch = mock.patch.object(
            parser, "_get_ocr_engine", side_effect=AssertionError("No real OCR models in tests")
        )
        engine_patch.start()
        self.addCleanup(engine_patch.stop)

    def save(self, document, filename: str) -> Path:
        path = self.root / filename
        document.save(path)
        return path

    def parse(self, path: Path):
        document = parser.parse_file(path)
        self.assertNotIn("_version_regions", document.front_matter)
        self.assertNotIn("_version_regions", document.metadata)
        for region in document.version_regions:
            self.assertEqual(set(region), {"source", "location", "text", "authoritative"})
            self.assertLessEqual(len(region["text"]), 4000)
            self.assertEqual(region["authoritative"], region["source"] == "content")
        return document

    def test_default_region_lists_are_independent(self):
        first = parser.ParsedDocument("first")
        second = parser.ParsedDocument("second")
        first.version_regions.append(_region("v2.0", "开头文本"))
        self.assertEqual(second.version_regions, [])

    def test_generic_readers_capture_first_4000_without_changing_text(self):
        body = "Version: v2.3\n" + "x" * 4100 + "\nLATE_VERSION v99"
        cases = {
            ".txt": body,
            ".md": body,
            ".rtf": "{\\rtf1\\ansi " + body.replace("\n", "\\par ") + "}",
            ".html": "<html><title>Title</title><body><p>" + body + "</p></body></html>",
            ".htm": "<html><body><p>" + body + "</p></body></html>",
        }
        for suffix, raw in cases.items():
            with self.subTest(suffix=suffix):
                path = self.root / ("generic" + suffix)
                path.write_bytes(raw.encode("utf-8"))
                document = self.parse(path)
                self.assertEqual(document.text, body)
                self.assertEqual(document.version_regions,
                                 [_region(body[:4000], "开头文本")])
                self.assertEqual(document.metadata["version"], "1.0")
        self.ocr.assert_not_called()

    def test_markdown_front_matter_kept_separate_from_body_region(self):
        path = self.root / "declared.md"
        body = "# Release\n\nBody version v2.0\n"
        path.write_text("---\nversion: '7.2'\ncategory: reference\n---\n" + body,
                        encoding="utf-8")
        document = self.parse(path)
        self.assertEqual(document.front_matter, {"version": "7.2", "category": "reference"})
        self.assertEqual(document.version_regions, [_region(document.text, "开头文本")])
        self.assertNotIn("7.2", document.version_regions[0]["text"])
        self.assertEqual(document.metadata["title"], "Release")
        self.assertEqual(document.metadata["version"], "1.0")

    def test_empty_generic_text_still_has_an_authoritative_opening_region(self):
        cases = {".txt": "", ".md": "", ".rtf": "{\\rtf1\\ansi }",
                 ".html": "<html><body></body></html>",
                 ".htm": "<html><body></body></html>"}
        for suffix, raw in cases.items():
            with self.subTest(suffix=suffix):
                path = self.root / ("empty" + suffix)
                path.write_bytes(raw.encode("utf-8"))
                document = self.parse(path)
                self.assertEqual(document.text, "")
                self.assertEqual(document.version_regions, [_region("", "开头文本")])

    def test_pdf_native_page_one_only_and_text_preserved(self):
        path = self.root / "native.pdf"
        first = "Version v2.0 " + "x" * 4100
        _write_pdf(path, [first, "PAGE_TWO Version v99"])
        document = self.parse(path)
        self.assertEqual(document.version_regions, [_region(first[:4000], "第1页")])
        self.assertEqual(document.text,
                         f"<<<__RAG_PAGE__=1>>>\n{first}\n"
                         "<<<__RAG_PAGE__=2>>>\nPAGE_TWO Version v99")

    def test_pdf_empty_page_one_does_not_fall_forward_to_page_two(self):
        path = self.root / "blank-cover.pdf"
        _write_pdf(path, ["", "PAGE_TWO Version v9.0"])
        document = self.parse(path)
        self.assertEqual(document.version_regions, [])
        self.assertEqual(document.text,
                         "<<<__RAG_PAGE__=1>>>\n\n"
                         "<<<__RAG_PAGE__=2>>>\nPAGE_TWO Version v9.0")

    def test_pdf_existing_ocr_is_separate_non_authoritative_and_not_rerun(self):
        path = self.root / "ocr.pdf"
        path.write_bytes(b"reader mocked; no renderer")
        for native in ("Native version v2.0", ""):
            with self.subTest(native=native):
                self.ocr.reset_mock()
                pages = [mock.Mock(), mock.Mock()]
                pages[0].extract_text.return_value = native
                pages[0].images = [SimpleNamespace(data=b"cover-a"),
                                   SimpleNamespace(data=b"cover-b")]
                pages[1].extract_text.return_value = "PAGE_TWO version v99"
                pages[1].images = [SimpleNamespace(data=b"later")]
                self.ocr.side_effect = ["OCR cover v3.0 " + "o" * 4100,
                                        "SECOND_COVER_IMAGE", "LATER_IMAGE_OCR"]
                reader = SimpleNamespace(is_encrypted=False, pages=pages)
                with mock.patch.object(parser, "PdfReader", return_value=reader):
                    document = self.parse(path)
                expected = [_region(native, "第1页")] if native else []
                expected.append(_region(("OCR cover v3.0 " + "o" * 4100 +
                                         "\nSECOND_COVER_IMAGE")[:4000], "第1页", "ocr"))
                self.assertEqual(document.version_regions, expected)
                self.assertEqual(self.ocr.call_count, 3)
                for page in pages:
                    page.extract_text.assert_called_once_with()
                label = "页面内图片" if native else "扫描页"
                page_one = (native + "\n") if native else ""
                self.assertEqual(document.text,
                                 "<<<__RAG_PAGE__=1>>>\n" + page_one +
                                 f"[{label} · OCR 识别]\nOCR cover v3.0 " + "o" * 4100 +
                                 "\nSECOND_COVER_IMAGE\n<<<__RAG_PAGE__=2>>>\n"
                                 "PAGE_TWO version v99\n[页面内图片 · OCR 识别]\nLATER_IMAGE_OCR")

    def test_ppt_first_slide_visible_text_table_group_only_no_notes(self):
        presentation = Presentation()
        cover = presentation.slides.add_slide(presentation.slide_layouts[1])
        cover.shapes.title.text = "Release v2.0"
        cover.placeholders[1].text = "Native cover text"
        table = cover.shapes.add_table(1, 2, Inches(0), Inches(2), Inches(4), Inches(1)).table
        table.cell(0, 0).text = "版本"
        table.cell(0, 1).text = "v2.1"
        group = cover.shapes.add_group_shape()
        textbox = group.shapes.add_textbox(Inches(0), Inches(4), Inches(2), Inches(1))
        textbox.text_frame.text = "GROUP_VISIBLE v2.2"
        cover.shapes.add_picture(_image(), Inches(5), Inches(1))
        cover.notes_slide.notes_text_frame.text = "NOTES_VERSION v88"
        later = presentation.slides.add_slide(presentation.slide_layouts[1])
        later.shapes.title.text = "LATER_SLIDE v99"
        later.shapes.add_picture(_image(), Inches(5), Inches(1))
        self.ocr.side_effect = ["COVER_OCR v3.0", "LATER_OCR v100"]
        document = self.parse(self.save(presentation, "cover.pptx"))
        native = "# Release v2.0\nNative cover text\n版本 | v2.1\nGROUP_VISIBLE v2.2"
        self.assertEqual(document.version_regions,
                         [_region(native, "第1张幻灯片"),
                          _region("COVER_OCR v3.0", "第1张幻灯片", "ocr")])
        self.assertEqual(document.text,
                         "<<<__RAG_PAGE__=1>>>\n" + native +
                         "\n[页面图片 1 · OCR 识别]\nCOVER_OCR v3.0"
                         "\n> 演讲者备注：NOTES_VERSION v88\n<<<__RAG_PAGE__=2>>>\n"
                         "# LATER_SLIDE v99\n[页面图片 1 · OCR 识别]\nLATER_OCR v100")
        self.assertEqual(self.ocr.call_count, 2)

    def test_ppt_empty_first_slide_does_not_capture_notes_or_later_title(self):
        presentation = Presentation()
        cover = presentation.slides.add_slide(presentation.slide_layouts[6])
        cover.notes_slide.notes_text_frame.text = "ONLY_NOTES v9.0"
        later = presentation.slides.add_slide(presentation.slide_layouts[1])
        later.shapes.title.text = "LATER_TITLE v10"
        document = self.parse(self.save(presentation, "empty-cover.pptx"))
        self.assertEqual(document.version_regions, [])
        self.assertEqual(document.metadata["title"], "LATER_TITLE v10")
        self.assertEqual(document.text,
                         "<<<__RAG_PAGE__=1>>>\n> 演讲者备注：ONLY_NOTES v9.0\n"
                         "<<<__RAG_PAGE__=2>>>\n# LATER_TITLE v10")

    def test_ppt_image_only_first_slide_has_only_non_authoritative_ocr(self):
        presentation = Presentation()
        cover = presentation.slides.add_slide(presentation.slide_layouts[6])
        cover.shapes.add_picture(_image(), Inches(0), Inches(0))
        cover.notes_slide.notes_text_frame.text = "NOTES v99"
        self.ocr.return_value = "OCR version v2.0 " + "x" * 4100
        document = self.parse(self.save(presentation, "image-cover.pptx"))
        self.assertEqual(document.version_regions, [_region(
            self.ocr.return_value[:4000], "第1张幻灯片", "ocr"
        )])
        self.assertEqual(document.text,
                         "<<<__RAG_PAGE__=1>>>\n[页面图片 1 · OCR 识别]\n" +
                         self.ocr.return_value + "\n> 演讲者备注：NOTES v99")
        self.ocr.assert_called_once()

    def test_ppt_region_length_is_bounded_without_truncating_body(self):
        presentation = Presentation()
        cover = presentation.slides.add_slide(presentation.slide_layouts[6])
        cover.shapes.add_textbox(Inches(0), Inches(0), Inches(4), Inches(2)).text = "x" * 4100
        document = self.parse(self.save(presentation, "long.pptx"))
        self.assertEqual(document.version_regions, [_region("x" * 4000, "第1张幻灯片")])
        self.assertEqual(document.text, "<<<__RAG_PAGE__=1>>>\n" + "x" * 4100)

    def test_docx_ordered_body_not_headers_footers_or_image_appendix(self):
        word = Document()
        word.add_heading("Release v2.0", level=1)
        word.add_paragraph("BEFORE_TABLE")
        table = word.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "CELL_A v2.1"
        table.cell(0, 1).text = "CELL_B v2.2"
        word.add_paragraph("AFTER_TABLE")
        word.sections[0].header.paragraphs[0].text = "FIRST_HEADER v88"
        word.sections[0].footer.paragraphs[0].text = "FIRST_FOOTER v89"
        section = word.add_section(WD_SECTION_START.NEW_PAGE)
        section.header.is_linked_to_previous = False
        section.footer.is_linked_to_previous = False
        section.header.paragraphs[0].text = "LATER_HEADER v99"
        section.footer.paragraphs[0].text = "LATER_FOOTER v100"
        word.add_picture(_image())
        self.ocr.side_effect = lambda data, **kwargs: (
            "IMAGE_APPENDIX v101" if data.startswith(b"\x89PNG") else ""
        )
        document = self.parse(self.save(word, "ordered.docx"))
        self.assertEqual(document.version_regions, [_region(
            "Release v2.0\nBEFORE_TABLE\nCELL_A v2.1 | CELL_B v2.2\nAFTER_TABLE", "文档开头"
        )])
        self.assertEqual(document.text,
                         "# Release v2.0\nBEFORE_TABLE\nCELL_A v2.1 | CELL_B v2.2\n"
                         "AFTER_TABLE\n\n\n[页眉] FIRST_HEADER v88\n[页脚] FIRST_FOOTER v89\n"
                         "[页眉] LATER_HEADER v99\n[页脚] LATER_FOOTER v100\n"
                         "[文档内图片 1 · OCR 识别]\nIMAGE_APPENDIX v101")
        png_calls = [call for call in self.ocr.call_args_list
                     if call.args[0].startswith(b"\x89PNG")]
        self.assertEqual(len(png_calls), 1)

    def test_docx_table_version_label_value_preserves_row_pairing(self):
        word = Document()
        word.add_paragraph("文档说明")
        table = word.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "版本号"
        table.cell(0, 1).text = "V1.2"
        table.cell(1, 0).text = "发布日期"
        table.cell(1, 1).text = "2026-11-19"
        word.add_paragraph("正文")
        document = self.parse(self.save(word, "version-row.docx"))
        expected = "文档说明\n版本号 | V1.2\n发布日期 | 2026-11-19\n正文"
        self.assertEqual(document.version_regions, [_region(expected, "文档开头")])
        self.assertEqual(document.text, expected)

    def test_docx_table_pairing_keeps_character_limit_and_page_break_stop(self):
        word = Document()
        word.add_paragraph("x" * 3970)
        table = word.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "版本号"
        table.cell(0, 1).text = "V1.2 " + "y" * 40
        document = self.parse(self.save(word, "row-limit.docx"))
        expected = "x" * 3970 + "\n版本号 | V1.2 " + "y" * 40
        self.assertEqual(document.version_regions, [_region(expected[:4000], "文档开头")])
        self.assertEqual(document.text, expected)

        word = Document()
        table = word.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "版本号"
        paragraph = table.cell(0, 1).paragraphs[0]
        paragraph.add_run("V1.2")
        paragraph.add_run().add_break(WD_BREAK.PAGE)
        paragraph.add_run("AFTER_BREAK V99")
        table.cell(1, 0).text = "LATER_LABEL"
        table.cell(1, 1).text = "V100"
        document = self.parse(self.save(word, "row-break.docx"))
        self.assertEqual(document.version_regions, [_region("版本号 | V1.2", "文档开头")])
        self.assertIn("AFTER_BREAK V99", document.text)
        self.assertIn("LATER_LABEL | V100", document.text)

    def test_docx_max_twenty_nonempty_paragraphs_and_cells_in_document_order(self):
        word = Document()
        for index in range(18):
            word.add_paragraph("")
            word.add_paragraph(f"PARA_{index:02}")
        table = word.add_table(rows=1, cols=3)
        table.cell(0, 0).text = "CELL_19"
        table.cell(0, 1).text = "CELL_20"
        table.cell(0, 2).text = "CELL_21 v99"
        word.add_paragraph("PARA_AFTER_LIMIT v100")
        document = self.parse(self.save(word, "twenty.docx"))
        expected = "\n".join([f"PARA_{index:02}" for index in range(18)] +
                             ["CELL_19 | CELL_20"])
        self.assertEqual(document.version_regions, [_region(expected, "文档开头")])
        self.assertIn("CELL_21 v99", document.text)
        self.assertIn("PARA_AFTER_LIMIT v100", document.text)

    def test_docx_4000_character_limit_does_not_truncate_body(self):
        word = Document()
        word.add_paragraph("x" * 4100)
        word.add_paragraph("LATER v99")
        document = self.parse(self.save(word, "long.docx"))
        self.assertEqual(document.version_regions, [_region("x" * 4000, "文档开头")])
        self.assertEqual(document.text, "x" * 4100 + "\nLATER v99")

    def test_docx_manual_break_keeps_only_text_before_break(self):
        word = Document()
        word.add_paragraph("OPENING v2.0")
        paragraph = word.add_paragraph("PREFIX v2.1")
        paragraph.add_run().add_break(WD_BREAK.PAGE)
        paragraph.add_run("AFTER_BREAK v99")
        word.add_paragraph("NEXT_PAGE v100")
        expected_text = "\n".join(paragraph.text for paragraph in word.paragraphs)
        document = self.parse(self.save(word, "break.docx"))
        self.assertEqual(document.version_regions,
                         [_region("OPENING v2.0\nPREFIX v2.1", "文档开头")])
        self.assertEqual(document.text, expected_text)
        self.assertIn("AFTER_BREAK v99", document.text)

    def test_docx_page_break_before_stops_before_paragraph(self):
        for empty_opening in (False, True):
            with self.subTest(empty_opening=empty_opening):
                word = Document()
                if not empty_opening:
                    word.add_paragraph("OPENING v2.0")
                paragraph = word.add_paragraph("AFTER_BREAK v99")
                paragraph.paragraph_format.page_break_before = True
                word.add_paragraph("LATER v100")
                document = self.parse(self.save(word, "break-before.docx"))
                expected = [] if empty_opening else [_region("OPENING v2.0", "文档开头")]
                self.assertEqual(document.version_regions, expected)
                self.assertEqual(document.text,
                                 "\n".join(paragraph.text for paragraph in word.paragraphs))

    def test_docx_break_inside_table_excludes_later_cells_but_not_body(self):
        word = Document()
        word.add_paragraph("OPENING v2.0")
        table = word.add_table(rows=1, cols=2)
        paragraph = table.cell(0, 0).paragraphs[0]
        paragraph.add_run("CELL_PREFIX v2.1")
        paragraph.add_run().add_break(WD_BREAK.PAGE)
        paragraph.add_run("CELL_AFTER_BREAK v99")
        table.cell(0, 1).text = "NEXT_CELL v100"
        document = self.parse(self.save(word, "table-break.docx"))
        self.assertEqual(document.version_regions,
                         [_region("OPENING v2.0\nCELL_PREFIX v2.1", "文档开头")])
        self.assertIn("CELL_AFTER_BREAK v99", document.text)
        self.assertIn("NEXT_CELL v100", document.text)

    def test_fake_legacy_suffix_uses_native_reader_and_original_metadata(self):
        word = Document()
        word.add_paragraph("Native Word v2.0")
        presentation = Presentation()
        presentation.slides.add_slide(presentation.slide_layouts[1]).shapes.title.text = "Native PPT v3.0"
        with mock.patch.object(parser, "_convert_via_soffice",
                               side_effect=AssertionError("No LibreOffice conversion")) as convert:
            for suffix, fixture, text, location in (
                (".doc", word, "Native Word v2.0", "文档开头"),
                (".wps", word, "Native Word v2.0", "文档开头"),
                (".ppt", presentation, "# Native PPT v3.0", "第1张幻灯片"),
                (".dps", presentation, "# Native PPT v3.0", "第1张幻灯片"),
            ):
                with self.subTest(suffix=suffix):
                    path = self.save(fixture, "renamed" + suffix)
                    document = self.parse(path)
                    self.assertEqual(document.version_regions, [_region(text, location)])
                    self.assertEqual(document.metadata["file_type"], suffix.lstrip("."))
                    self.assertEqual(document.metadata["source"], path.name)
            convert.assert_not_called()

    def test_legacy_delegate_preserves_regions_and_original_source(self):
        word = Document()
        word.add_paragraph("Converted Word v2.0")
        converted = self.save(word, "converted.docx")
        original = self.root / "original.doc"
        original.write_bytes(b"legacy fixture")
        with mock.patch.object(parser, "_convert_via_soffice", return_value=converted) as convert:
            document = self.parse(original)
        convert.assert_called_once_with(original, "docx", "writer")
        self.assertEqual(document.version_regions, [_region("Converted Word v2.0", "文档开头")])
        self.assertEqual(document.text, "Converted Word v2.0")
        self.assertEqual(document.metadata["source"], "original.doc")
        self.assertEqual(document.metadata["file_type"], "doc")
        self.assertIn("LibreOffice", document.front_matter["source_note"])

    def test_xlsx_business_cells_have_no_version_region(self):
        from openpyxl import Workbook
        workbook = Workbook()
        workbook.active["A1"] = "版本 v99"
        path = self.save(workbook, "business.xlsx")
        document = self.parse(path)
        self.assertEqual(document.version_regions, [])
        self.assertEqual(document.text, "## Sheet：Sheet\n版本 v99")
        self.assertEqual(parser._read_xlsx(path)[2], {})


class VersionRegionHarnessTests(IngestHarness):
    """解析目录回归采用共享临时知识/认证/台账隔离夹具。"""

    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()
        self._patch("src.parser.get_config", self.cfg)
        patch = mock.patch.object(parser, "_ocr_image", return_value="")
        patch.start()
        self.addCleanup(patch.stop)

    def test_parse_directory_carries_regions_without_including_hidden_cache(self):
        self.write_note("Version v2.0", name="release.md")
        hidden = self.knowledge / ".rag_converted"
        hidden.mkdir()
        (hidden / "cache.txt").write_text("IGNORE v99", encoding="utf-8")
        documents, failures = parser.parse_directory(self.knowledge)
        self.assertEqual(failures, [])
        self.assertEqual([document.metadata["source"] for document in documents], ["release.md"])
        self.assertEqual(documents[0].version_regions,
                         [_region(documents[0].text, "开头文本")])
        self.assertEqual(self.ledger(), {})


if __name__ == "__main__":
    unittest.main()

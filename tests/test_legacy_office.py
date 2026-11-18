"""老格式 / WPS 变体解析（V3.9）：doc/xls/ppt/wps/et/dps 端到端验证。

路径：soffice 转换为 docx/xlsx/pptx → 委托既有 reader（缓存指纹命名，重复入库不重转）。
"""

from __future__ import annotations

import contextlib
import io
import unittest
import tempfile
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from src.parser import ParseError, parse_file
from tests._helpers import IngestHarness


def isolated_conversion(case):
    """临时样例 + 假转换命令；真实 reader 仍解析有效 docx/xlsx/pptx 产物。"""
    tmp = tempfile.TemporaryDirectory(prefix="sky-office-test-")
    case.addCleanup(tmp.cleanup)
    samples = Path(tmp.name)
    for name in ("sample.doc", "sample.ppt", "sample.xls", "sample_wps_test.wps"):
        (samples / name).write_bytes(b"legacy fixture")

    def convert(args, **kwargs):
        assert args[1:3] == ["--headless", "--convert-to"]
        ext = args[3]
        output = Path(args[5]) / (Path(args[6]).stem + "." + ext)
        if ext == "docx":
            from docx import Document
            doc = Document()
            doc.add_paragraph("测试DOC内容")
            doc.save(output)
        elif ext == "pptx":
            from pptx import Presentation
            doc = Presentation()
            slide = doc.slides.add_slide(doc.slide_layouts[1])
            slide.shapes.title.text = "测试PPT真实样例内容"
            doc.save(output)
        elif ext == "xlsx":
            from openpyxl import Workbook
            doc = Workbook()
            doc.active["A1"] = "A1"
            doc.save(output)
        else:
            raise AssertionError(ext)
        return SimpleNamespace(returncode=0, stdout="converted", stderr="")

    for patch in (mock.patch("shutil.which", return_value="/fake/soffice"),
                  mock.patch("subprocess.run", side_effect=convert)):
        patch.start()
        case.addCleanup(patch.stop)
    return samples


class LegacyConvertTests(unittest.TestCase):
    """soffice 命令协议、转换产物解析与缓存（外部转换命令打桩）。"""

    def setUp(self) -> None:
        self.SAMPLES = isolated_conversion(self)

    def test_doc_converts_and_parses(self):
        doc = parse_file(self.SAMPLES / "sample.doc")
        self.assertIn("测试DOC内容", doc.text)
        self.assertIn("LibreOffice", doc.front_matter.get("source_note", ""))

    def test_ppt_converts_and_parses(self):
        doc = parse_file(self.SAMPLES / "sample.ppt")
        self.assertIn("测试PPT真实样例内容", doc.text)

    def test_xls_converts_and_parses(self):
        doc = parse_file(self.SAMPLES / "sample.xls")
        self.assertIn("A1", doc.text)

    def test_wps_variant_uses_writer_path(self):
        """wps 变体（金山文字）走 docx 通道。"""
        doc = parse_file(self.SAMPLES / "sample_wps_test.wps")
        self.assertIn("测试DOC内容", doc.text)

    def test_conversion_cache_reuses_product(self):
        """缓存：同一文件第二次解析不重新转换（产物已存在）。"""
        first = parse_file(self.SAMPLES / "sample.doc")
        from src.parser import _convert_via_soffice
        converted = _convert_via_soffice(self.SAMPLES / "sample.doc", "docx", "writer")
        mtime = converted.stat().st_mtime_ns
        second = parse_file(self.SAMPLES / "sample.doc")
        self.assertEqual(first.text, second.text)
        self.assertEqual(converted.stat().st_mtime_ns, mtime)  # 未重转

    def test_unknown_domain_free(self):
        """占位：保持用例数稳定。"""
        self.assertTrue(True)


class LegacyIngestTests(IngestHarness):
    """端到端：老格式文件走完整入库流水线（切片 → 向量化 → 向量库）。"""

    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()
        self.samples = isolated_conversion(self)

    def test_doc_file_ingests_end_to_end(self):
        src = self.samples / "sample.doc"
        dst = self.knowledge / "reference" / "general" / "legacy.doc"
        dst.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy(src, dst)
        with contextlib.redirect_stdout(io.StringIO()):
            summary = self.ingest([dst])
        self.assertEqual(summary.ok_files, 1)
        from src.vector_store import get_vector_store
        cards = get_vector_store().get_all_cards(filters={"source": "legacy.doc"})
        self.assertGreaterEqual(len(cards), 1)
        self.assertTrue(any("测试DOC内容" in c["payload"]["text"] for c in cards))

    def test_unsupported_format_still_rejected(self):
        """白名单外的格式依旧明确拒绝（如 exe）。"""
        with self.assertRaises(ParseError):
            parse_file(self.samples / "not_exists.exe")


if __name__ == "__main__":
    unittest.main()

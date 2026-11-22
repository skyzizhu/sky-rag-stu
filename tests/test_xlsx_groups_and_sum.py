"""R6 缺口修复的单元测试：xlsx 行组切卡 + 列举记录确定性求和。

- 行组（R6-M07/C03）：parser._read_xlsx 每 XLSX_ROW_GROUP 数据行一组、
  H1 组标题、每组重复表头；小表不切。
- 求和（R6-C05）：pipeline._enumeration_sum_note 仅在问题含明确求和词且
  每条已验证记录都有可比金额时输出 Decimal 算式；任一缺失即 None。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from src.parser import XLSX_ROW_GROUP, _read_xlsx
from src.pipeline import _enumeration_sum_note


def _record(name: str, quote: str) -> dict:
    return {"name": name, "identity": None, "evidence": [{"source_id": "1",
                                                           "quote": quote, "metadata": {}}]}


class XlsxRowGroupTests(unittest.TestCase):
    def _make(self, rows: list[list[str]], sheet: str = "项目台账") -> Path:
        workbook = Workbook()
        sheet_obj = workbook.active
        sheet_obj.title = sheet
        for row in rows:
            sheet_obj.append(row)
        path = Path(tempfile.mkdtemp()) / "t.xlsx"
        workbook.save(str(path))
        return path

    def test_rows_are_grouped_with_header_repeated(self):
        rows = [["项目", "预算（万元）"]] + [[f"P{i}", str(i * 10)] for i in range(1, 8)]
        text, _, _ = _read_xlsx(self._make(rows))
        self.assertIn("# 项目台账（第 1~4 行）", text)
        self.assertIn("# 项目台账（第 5~7 行）", text)
        # 每组重复表头，列名自足（切组时不再输出冗余的 Sheet 首块）
        self.assertEqual(text.count("项目 | 预算（万元）"), 2)
        # 行不丢
        for i in range(1, 8):
            self.assertIn(f"P{i} | {i * 10}", text)

    def test_small_sheet_stays_single_block(self):
        rows = [["名称", "值"], ["甲", "1"], ["乙", "2"]]
        text, _, _ = _read_xlsx(self._make(rows))
        self.assertNotIn("（第", text)
        self.assertEqual(XLSX_ROW_GROUP, 4)

    def test_multiple_sheets_group_independently(self):
        workbook = Workbook()
        workbook.active.title = "A"
        workbook.active.append(["h"])
        for i in range(6):
            workbook.active.append([f"a{i}"])
        sheet_b = workbook.create_sheet("B")
        sheet_b.append(["h2"])
        for i in range(3):
            sheet_b.append([f"b{i}"])
        path = Path(tempfile.mkdtemp()) / "two.xlsx"
        workbook.save(str(path))
        text, _, _ = _read_xlsx(path)
        self.assertIn("# A（第 1~4 行）", text)
        self.assertIn("# A（第 5~6 行）", text)
        self.assertIn("## Sheet：B", text)
        self.assertNotIn("# B（第", text)  # B 只有 3 行 ≤ 组行数，不切


class EnumerationSumNoteTests(unittest.TestCase):
    def test_sums_when_all_records_have_money(self):
        records = [_record("凤凰项目", "凤凰项目（预算 260 万元，X-4000 软件平台）"),
                   _record("星云二期", "星云二期（预算 180 万元，数据平台商业化改造）"),
                   _record("昆仑二期", "昆仑二期（预算 150 万元，设备管理平台增强）")]
        note = _enumeration_sum_note("三大重点投入项目的预算合计是多少万元？", records)
        self.assertIsNotNone(note)
        compact = note.replace(" ", "")
        self.assertIn("260万元+180万元+150万元", compact)
        self.assertIn("590万元", compact)
        self.assertIn("3条已验证记录", compact)

    def test_mixed_units_converted(self):
        records = [_record("甲", "甲预算 2 万元"),
                   _record("乙", "乙预算 30,000 元")]
        note = _enumeration_sum_note("两者合计多少？", records)
        self.assertIsNotNone(note)
        self.assertIn("5万元", note.replace(" ", ""))  # 20000 + 30000 = 50000 元 = 5 万元

    def test_no_sum_word_or_missing_money_returns_none(self):
        records = [_record("甲", "甲预算 260 万元"), _record("乙", "乙预算 180 万元")]
        self.assertIsNone(_enumeration_sum_note("两者分别是多少？", records))  # 非求和问法
        self.assertIsNone(_enumeration_sum_note(
            "合计多少？", [_record("甲", "甲项目按期推进"), _record("乙", "乙预算 180 万元")]))  # 缺金额
        self.assertIsNone(_enumeration_sum_note("合计多少？", [_record("甲", "甲 1 万元")]))  # 记录不足 2 条


if __name__ == "__main__":
    unittest.main()

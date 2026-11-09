"""切片规则（2026-11-08）：相邻短小节合并 + 标题段绑定正文段。

病灶（实测）：网页清洗器把每个视觉块当独立小节——Xtools 价格区 5 个碎块
（53~159 字）各自成卡，问年费漏答；列表页条目标题段被留在上一张卡尾，
命令段进下一张卡，模型只见标题不见内容。

- 规则① _merge_short_sections：相邻小节合计 ≤ 单卡容量就合成一个切片单元，
  子节标题行回注正文；单节超容量的保持独立；
- 规则② _bind_title_paragraphs：标题段（短+冒号结尾）与下一段合成打包单元，
  封卡时不再把标题留在卡尾。
"""

from __future__ import annotations

import unittest

from src.chunker import (_bind_title_paragraphs, _is_title_paragraph,
                         _merge_short_sections, _split_by_sections,
                         _split_by_sections_raw, chunk_document)
from src.config import get_config
from src.parser import ParsedDocument
from tests._helpers import IngestHarness


def _parsed(markdown: str, source: str = "test.md") -> ParsedDocument:
    return ParsedDocument(metadata={"source": source, "document_id": "doc_test"},
                          text=markdown, pages=None)


class MergeShortSectionsTests(unittest.TestCase):
    """规则①：相邻短小节合并（合计 ≤ 容量）。"""

    def test_price_table_sections_merge_into_one(self):
        """复刻 Xtools 价格区：5 个碎小节（53~159 字）应合并为 1 个切片单元，
        各子节标题行回注正文。"""
        md = """# 订阅方案

选择适合你的方案，随时取消。

## 月度会员

$3.99 / 月，300 AI 积分，包含全部基础功能。

## 季度会员

$8.99 / 季，500 AI 积分，包含全部基础功能与进阶工具。

## 年度会员

$29.99 / 年，800 AI 积分/月，包含全部功能，免广告体验。
"""
        raw = _split_by_sections_raw(md)
        merged = _merge_short_sections(raw, max_chars=600)
        self.assertEqual(len(merged), 1, "5 个碎小节应合并为 1 个单元")
        self.assertEqual(merged[0][0], "订阅方案")            # 章节名取第一节
        for title in ("月度会员", "季度会员", "年度会员"):
            self.assertIn(f"## {title}", merged[0][3], "合并后子节标题不能丢")

    def test_no_merge_when_combined_exceeds_capacity(self):
        """相邻两节合计超容量：封组开新组，不硬塞。"""
        md = "## 甲\n\n" + "甲" * 400 + "\n\n## 乙\n\n" + "乙" * 400
        merged = _merge_short_sections(_split_by_sections_raw(md), max_chars=600)
        self.assertEqual(len(merged), 2)

    def test_large_section_stays_independent(self):
        """单节自己超容量：不参与合并，保持独立（交给打包阶段硬切）。"""
        md = "## 大节\n\n" + "内容" * 400 + "\n\n## 小节\n\n短内容"
        merged = _merge_short_sections(_split_by_sections_raw(md), max_chars=600)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0][0], "大节")

    def test_single_section_unchanged(self):
        """只有一节、没发生合并：正文保持不含标题行（旧行为不变）。"""
        md = "# 唯一小节\n\n正文内容。"
        merged = _merge_short_sections(_split_by_sections_raw(md), max_chars=600)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0][3], "正文内容。")  # 未合并 → 不回注标题行


class TitleBindingTests(unittest.TestCase):
    """规则②：标题段（短+冒号结尾）与下一段合成打包单元。"""

    def test_title_paragraph_detection(self):
        self.assertTrue(_is_title_paragraph("18，mac笔记本查看电池耗损度：终端："))
        self.assertTrue(_is_title_paragraph("举个例子："))
        self.assertFalse(_is_title_paragraph("这是一段很长的正文内容。" * 10))  # 超过 60 字
        self.assertFalse(_is_title_paragraph("普通句子，以句号结尾。"))

    def test_title_binds_with_following_content(self):
        blocks = ["17，apache 重启：", "sudo apachectl restart 命令说明。",
                  "18，查看电池耗损度：", "ioreg -rn AppleSmartBattery 命令说明。"]
        bound = _bind_title_paragraphs(blocks, size=600)
        self.assertEqual(len(bound), 2)  # 两对「标题+正文」
        self.assertIn("18，查看电池耗损度", bound[1])
        self.assertIn("ioreg", bound[1])

    def test_consecutive_titles_bind_forward(self):
        """连续两个标题段：各自向后绑定（17 绑 18 的位置内容属罕见结构，验证不崩溃）。"""
        blocks = ["标题甲：", "标题乙：", "正文内容一段。"]
        bound = _bind_title_paragraphs(blocks, size=600)
        self.assertEqual(len(bound), 2)  # 甲绑定乙；剩正文独立

    def test_stranded_title_scenario_fixed(self):
        """复现电池案例：正文 560 字 + 标题段 + 命令段——
        旧打包把标题留在第一张卡尾；绑定后标题与命令同卡。"""
        body = "前一条目的命令与说明。" * 43            # ≈ 559 字
        title = "18，mac笔记本查看电池耗损度：终端："
        command = "ioreg -rn AppleSmartBattery | grep -i capacity。" * 5
        chunks = _bind_title_paragraphs([body, title, command], size=600)
        packed_titles = [c for c in chunks if "18，mac笔记本" in c and "ioreg" in c]
        self.assertEqual(len(packed_titles), 1, "标题与命令应在同一打包单元")


class ChunkDocumentIntegrationTests(IngestHarness):
    """端到端：短小节合并 + 标题绑定落在真实切片产物上。"""

    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()

    def test_price_table_becomes_one_card(self):
        """价格表 5 碎节 → 1 张卡：年费与月费同卡，问年费不再依赖名额运气。"""
        md = """# 订阅方案

选择适合你的方案，随时取消。

## 月度会员

$3.99 / 月，300 AI 积分，包含全部基础功能。

## 季度会员

$8.99 / 季，500 AI 积分，包含全部基础功能与进阶工具。

## 年度会员

$29.99 / 年，800 AI 积分/月，包含全部功能，免广告体验。
"""
        path = self.knowledge / "reference" / "general" / "pricing.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(md, encoding="utf-8")
        self.ingest([path])
        from src.vector_store import get_vector_store
        cards = sorted(get_vector_store().get_all_cards(
            filters={"source": "pricing.md"}), key=lambda c: c["payload"]["chunk_id"])
        self.assertEqual(len(cards), 1, "价格表应合并为一张卡")
        text = cards[0]["payload"]["text"]
        for price in ("$3.99", "$8.99", "$29.99"):
            self.assertIn(price, text)
            self.assertIn("## ", text)  # 子节标题行回注

    def test_battery_title_and_command_in_same_card(self):
        """列表页条目：标题段与命令段应落在同一张卡（旧规则标题会被留在卡尾）。"""
        md = "## Mac 快捷操作\n\n" + "前一条目的命令与说明。" * 43 + \
             "\n\n18，mac笔记本查看电池耗损度：终端：\n\n" + \
             "ioreg -rn AppleSmartBattery | grep -i capacity。" * 5 + \
             "\n\n19，下一条目：\n\n下一条目的内容说明。"
        path = self.knowledge / "reference" / "general" / "mac.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(md, encoding="utf-8")
        self.ingest([path])
        from src.vector_store import get_vector_store
        cards = get_vector_store().get_all_cards(filters={"source": "mac.md"})
        together = [c for c in cards
                    if "18，mac笔记本查看电池耗损度" in c["payload"]["text"]
                    and "ioreg" in c["payload"]["text"]]
        self.assertEqual(len(together), 1, "标题与命令应同卡")


if __name__ == "__main__":
    unittest.main()

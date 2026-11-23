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
from dataclasses import replace

from src.chunker import (_bind_title_paragraphs, _is_title_paragraph,
                         _merge_short_sections, _split_by_sections,
                         _split_by_sections_raw, chunk_document)
from src.config import get_config
from src.parser import ParsedDocument
from tests._helpers import IngestHarness


def _parsed(markdown: str, source: str = "test.md") -> ParsedDocument:
    return ParsedDocument(metadata={"source": source, "document_id": "doc_test"},
                          text=markdown)


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
        merged = _merge_short_sections(raw, max_tokens=640)
        self.assertEqual(len(merged), 1, "5 个碎小节应合并为 1 个单元")
        self.assertEqual(merged[0][0], "订阅方案")            # 章节名取第一节
        for title in ("月度会员", "季度会员", "年度会员"):
            self.assertIn(f"## {title}", merged[0][3], "合并后子节标题不能丢")

    def test_no_merge_when_combined_exceeds_capacity(self):
        """相邻两节合计超容量：封组开新组，不硬塞。"""
        md = "## 甲\n\n" + "甲" * 500 + "\n\n## 乙\n\n" + "乙" * 500
        merged = _merge_short_sections(_split_by_sections_raw(md), max_tokens=640)
        self.assertEqual(len(merged), 2)

    def test_large_section_stays_independent(self):
        """单节自己超容量：不参与合并，保持独立（交给打包阶段硬切）。"""
        md = "## 大节\n\n" + "内容" * 500 + "\n\n## 小节\n\n短内容"
        merged = _merge_short_sections(_split_by_sections_raw(md), max_tokens=640)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0][0], "大节")

    def test_single_section_unchanged(self):
        """只有一节、没发生合并：正文保持不含标题行（旧行为不变）。"""
        md = "# 唯一小节\n\n正文内容。"
        merged = _merge_short_sections(_split_by_sections_raw(md), max_tokens=640)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0][3], "正文内容。")  # 未合并 → 不回注标题行

    def test_merged_group_prefers_first_named_section(self):
        """R3-a：组首是无标题的文档开头段时，章节名取组内第一个带标题小节。

        病灶：docx 手册的标题块（无标题样式）常在首章之前，整篇小手册合并成
        一卡时 section=None，丢失章节定位。（用 H2 小节验证——H1 章边界见 R3-b 用例。）
        """
        md = ("星海新员工手册\n版本：V2.0\n\n"
              "## 一、总则\n\n短内容甲。\n\n## 二、流程\n\n短内容乙。")
        merged = _merge_short_sections(_split_by_sections_raw(md), max_tokens=640)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0][0], "一、总则")          # 回退到首个带标题小节
        self.assertIn("一、总则", merged[0][1])
        self.assertEqual(merged[0][4], ["一、总则", "二、流程"])  # 覆盖章节列表
        # 各子节标题行仍回注正文（旧行为不变）
        for title in ("## 一、总则", "## 二、流程"):
            self.assertIn(title, merged[0][3])

    def test_merged_group_sections_metadata_in_chunk(self):
        """R3-a：合并组卡片元数据带 sections 列表；单节卡片不带。"""
        doc = ParsedDocument(metadata={"source": "manual.docx", "document_id": "doc_test"},
                             text="手册标题行\n\n## 甲\n\n短内容。\n\n## 乙\n\n短内容。")
        chunks = chunk_document(doc, get_config())
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].metadata["section"], "甲")
        self.assertEqual(chunks[0].metadata["sections"], ["甲", "乙"])
        single = chunk_document(ParsedDocument(
            metadata={"source": "note.md", "document_id": "doc_one"}, text="# 唯一\n\n正文。"),
            get_config())
        self.assertNotIn("sections", single[0].metadata)

    def test_h1_chapters_are_not_merged(self):
        """R3-b：相邻两节都是 H1（章）→ 不跨章合并，每章独立成卡。"""
        md = "# 一、总则\n\n短甲。\n\n# 二、流程\n\n短乙。\n\n# 三、附则\n\n短丙。"
        merged = _merge_short_sections(_split_by_sections_raw(md), max_tokens=640)
        self.assertEqual([m[0] for m in merged], ["一、总则", "二、流程", "三、附则"])

    def test_h2_subsections_merge_into_chapter(self):
        """R3-b：章内 H2 小节并入所属 H1 章；开头段（无标题）也并入首章。"""
        md = ("标题块\n\n# 一、总则\n\n章首。\n\n## 1.1 适用范围\n\n小节内容。\n\n# 二、流程\n\n短乙。")
        merged = _merge_short_sections(_split_by_sections_raw(md), max_tokens=640)
        self.assertEqual([m[0] for m in merged], ["一、总则", "二、流程"])
        self.assertIn("1.1 适用范围", merged[0][3])   # H2 并入首章
        self.assertEqual(merged[0][4], ["一、总则", "1.1 适用范围"])

    def test_chunk_text_carries_section_context_prefix(self):
        """R2-c：卡片正文首行 = 来源文档 + 章节路径（确定性上下文前缀）。"""
        doc = ParsedDocument(metadata={"source": "manual.docx", "document_id": "doc_ctx"},
                             text="# 一、总则\n\n制度内容若干。")
        chunks = chunk_document(doc, get_config())
        self.assertTrue(chunks[0].text.startswith("来源文档：manual.docx｜章节：一、总则\n"))


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


class TokenDensityTests(unittest.TestCase):
    """V3.9 换制的核心性质：中英文同 token → 同卡片数（字符制下英文卡只有约 1/3 密度）。"""

    def test_long_sentence_preserves_pending_text_and_budget(self):
        from src.chunker import est_tokens

        cfg = replace(get_config(), chunk_size=100, chunk_overlap=20)
        text = "验收编号 ZX-410。" + "甲" * 220 + "。\n\n" + "乙" * 118 + "\n\n" + "丙" * 118
        chunks = chunk_document(_parsed(text, "报告.md"), cfg)
        self.assertTrue(any("ZX-410" in chunk.text for chunk in chunks))
        self.assertTrue(all(est_tokens(chunk.text) <= cfg.chunk_size for chunk in chunks))
        self.assertEqual([c.metadata["chunk_index"] for c in chunks], list(range(len(chunks))))

    def test_same_tokens_same_card_count_regardless_of_language(self):
        from src.chunker import chunk_document, est_tokens
        zh_text = "这是一段中文正文，用于验证切片密度在不同语言下保持一致。" * 130
        en_unit = "English body text for chunk density verification. "
        tz = est_tokens(zh_text)
        # 英文按估算 token 对齐到与中文相同的总量（构造而非假设）
        k = max(1, round(tz / est_tokens(en_unit)))
        en_text = en_unit * k
        te = est_tokens(en_text)
        self.assertLess(abs(tz - te) / max(tz, te), 0.03, f"tz={tz} te={te}")

        def parse(text: str, source: str) -> ParsedDocument:
            return ParsedDocument(metadata={"source": source, "document_id": "doc_density"},
                                  text=text)

        cfg = get_config()
        cards_zh = chunk_document(parse(zh_text, "zh.md"), cfg)
        cards_en = chunk_document(parse(en_text, "en.md"), cfg)
        # 边界取整噪声允许 ±1 张（估算差 0.2% 在 640 边界上可翻转一张卡）；
        # 字符制下这个差距是 3~4 张（英文密度只有中文的 1/3）
        self.assertLessEqual(abs(len(cards_zh) - len(cards_en)), 1,
                             f"同 token 的中英文文档卡片数差距过大：中文 {len(cards_zh)} vs 英文 {len(cards_en)}")
        # 且都切成了多张卡（证明 size 真的在起作用）
        self.assertGreaterEqual(len(cards_zh), 3)

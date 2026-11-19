"""类型自由文本与保守自动推断：不读取正文，不依赖注册表/LLM。"""
import unittest

from src.document_type import (
    infer_doc_type_from_filename, infer_doc_type_from_path, normalize_doc_type,
    normalize_doc_type_filter, resolve_doc_type,
)


class DocumentTypeTests(unittest.TestCase):
    def test_optional_free_text_is_trimmed_not_canonicalized(self):
        for value, expected in ((None, ""), ("", ""), ("  ", ""),
                                ("  一期方案  ", "一期方案"),
                                ("  My custom TYPE / 专项材料  ", "My custom TYPE / 专项材料"),
                                ("all", "all"), ("类" * 80, "类" * 80)):
            with self.subTest(value=value):
                self.assertEqual(normalize_doc_type(value), expected)

    def test_invalid_values_rejected_not_coerced(self):
        for value in (False, 42, [], {}, "类" * 81, " __unclassified__ ",
                      "\n", "\t总体方案", "方案\x00", "方案\x7f", "方\u200b案", "方\u2028案"):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                normalize_doc_type(value)

    def test_filter_aliases_are_only_for_filtering(self):
        for value in (None, "", "  ", "all", " ALL ", "All"):
            self.assertIsNone(normalize_doc_type_filter(value))
        self.assertEqual(normalize_doc_type_filter(" __unclassified__ "), "__unclassified__")
        self.assertEqual(normalize_doc_type_filter(" 一期方案 "), "一期方案")
        for value in (False, 3, [], "all\n", "__unclassified__\t", "a" * 81):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                normalize_doc_type_filter(value)

    def test_closest_complete_directory_and_root_are_retained(self):
        cases = {
            "总体方案/一期方案/附件/a.docx": "一期方案",
            "一期方案/a.docx": "一期方案",
            "总体方案/二期方案/20261119/图片/a.docx": "二期方案",
            r"项目A\六期方案\十期方案\附件\a.docx": "十期方案",
            "项目A/专项一期方案/未知/a.docx": "专项一期方案",
            "会议纪要/2026-11-19/a.md": "会议纪要",
            "总体方案/一期方案/一期方案附件/a.docx": "一期方案",
            "总体方案/二期方案备份/a.docx": "总体方案",
            "需求文档/参考资料/图片素材/a.txt": "需求文档",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(infer_doc_type_from_path(path), expected)

    def test_unknown_directories_and_filename_are_not_directory_types(self):
        for path in ("项目A/附件/2026/a.docx", "learning/general/总体方案.md",
                     "2026-11-19/图片/说明.md", "合同管理系统/标准化项目/流程引擎/a.docx",
                     "总体方案附件/二期方案图片/a.docx", "a.docx", "总体方案.md"):
            with self.subTest(path=path):
                self.assertEqual(infer_doc_type_from_path(path), "")

    def test_filename_extracts_only_one_explicit_phrase(self):
        for name, expected in (
            ("项目A_总体方案_V1.2_20261119.docx", "总体方案"),
            ("某某项目一期方案（第2版）.PDF", "一期方案"),
            ("项目B_二期方案_2026-11-19.md", "二期方案"),
            ("专项六期方案.doc", "六期方案"),
            ("项目A需求文档_version_2.0.txt", "需求文档"),
            ("会议纪要_会议纪要.docx", "会议纪要"),
            ("工作报告_20261119.pdf", "工作报告"),
            ("学习笔记.md", "学习笔记"), ("项目A_合同_v1.0.pdf", "合同"),
            ("path/总体方案/未知说明.md", ""),
            ("项目A_说明.md", ""), ("方案.md", ""),
            ("合同管理系统.md", ""), ("标准化项目.md", ""),
            ("一期方案_二期方案.docx", ""),
            ("需求文档_会议纪要.md", ""),
            ("测试报告和验收文档.docx", ""),
        ):
            with self.subTest(name=name):
                self.assertEqual(infer_doc_type_from_filename(name), expected)

    def test_current_file_declaration_releases_old_lifecycle_declaration(self):
        carried = {"carry": {"value": "旧声明", "source": "front_matter"}}
        info = resolve_doc_type("unknown.md", {"doc_type": "新声明"}, carried)
        self.assertEqual(info["value"], "新声明")
        self.assertNotIn("carry", info)
        self.assertEqual(resolve_doc_type("unknown.md", {}, info)["value"], "")

    def test_blank_declarations_defaults_do_not_block_auto_but_manual_clear_does(self):
        for fm in ({}, {"doc_type": None}, {"doc_type": ""}, {"doc_type": "  "},
                   {"doc_type": 23}, {"doc_type": "__unclassified__"}):
            for previous in ({}, {"upload_default": ""}):
                info = resolve_doc_type("总体方案.md", fm, previous,
                                        default_doc_type="", original_path="一期方案/a.md")
                self.assertEqual(info["value"], "一期方案")
        clear = resolve_doc_type("总体方案.md", {"doc_type": "需求文档"}, {"manual": ""},
                                 default_doc_type="会议纪要", original_path="一期方案/a.md")
        self.assertEqual(clear["value"], "")
        self.assertEqual(clear["source"], "manual")


if __name__ == "__main__":
    unittest.main()

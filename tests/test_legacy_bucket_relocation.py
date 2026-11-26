"""V3.9.2：旧未关联桶（public）文件重新入库搬迁的纯函数测试（不触真实服务）。"""

import unittest

from src.manage import _legacy_unassigned_bucket


class LegacyBucketDetectionTests(unittest.TestCase):
    def test_legacy_public_paths_detected(self):
        self.assertEqual(_legacy_unassigned_bucket("learning/public/笔记.md"),
                         ("learning", "笔记.md"))
        self.assertEqual(
            _legacy_unassigned_bucket("learning/public/子目录/深层/文件.md"),
            ("learning", "子目录/深层/文件.md"))
        self.assertEqual(
            _legacy_unassigned_bucket("projects/public/old/合同.pdf"),
            ("projects", "old/合同.pdf"))
        self.assertEqual(
            _legacy_unassigned_bucket("learning/public-unassigned/old.md"),
            ("learning", "old.md"))

    def test_new_scheme_and_other_paths_not_relocated(self):
        # 新约定 basic_knowledge（含用户名层）不搬迁
        self.assertIsNone(_legacy_unassigned_bucket("learning/basic_knowledge/liqiang/文件.md"))
        self.assertIsNone(_legacy_unassigned_bucket("learning/basic_knowledge/文件.md"))
        # 项目目录/普通目录/归档不搬迁
        self.assertIsNone(_legacy_unassigned_bucket("projects/fenghuangxiangmu/手册.md"))
        self.assertIsNone(_legacy_unassigned_bucket("learning/自定义目录/文件.md"))
        self.assertIsNone(_legacy_unassigned_bucket("archive/2024/public/文件.md"))
        # 太浅（领域/桶名即文件，无文件段）不匹配
        self.assertIsNone(_legacy_unassigned_bucket("learning/public"))


if __name__ == "__main__":
    unittest.main()

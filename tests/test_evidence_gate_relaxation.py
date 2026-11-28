"""V3.9.1（评测 W07/M05/W01）证据门禁放宽与对比侧范围的纯函数测试（不触真实检索服务）。

- 阈值锚点放宽：金额类阈值在制度原文写作「金额达到 50 万元…审批」，数值与
  「阈值」二字不相邻甚至通篇无「阈值」措辞——锚点放宽到阈值表述词、取值并入货币。
- 编号解读类问法：编号是解读对象而非定位锚点，缺号不再一票否决整份证据；
  定位类问法（查某个编号的属性）仍保持严格拦截。
- 对比侧范围：「归档目录里…相比」的口头知识板块只绑定历史一侧（现行侧剥离），
  界面手动状态筛选仍完全尊重。
"""

import unittest

from src.evidence_support import assess_evidence
from src.retrieval_intent import comparison_statuses


class ThresholdAnchorRelaxationTests(unittest.TestCase):
    Q = "合同金额达到多少需要分管副总经理审批？供应商采购金额达到多少需要三家比价？两个阈值分别是多少？"

    def test_money_wording_satisfies_threshold_ask(self):
        # 制度原文措辞：数值跟在「金额」后而非「阈值」后——修复前整份证据被门禁否决
        row = ("合同审批权限：单笔合同金额达到 50 万元的，须提交分管副总经理审批。"
               "供应商采购金额达到 30 万元的，须进行三家比价。")
        support = assess_evidence(self.Q, [row])
        self.assertEqual(support["status"], "supported", support)
        self.assertNotIn("阈值", support.get("missing", ()))

    def test_threshold_without_any_value_wording_still_blocks(self):
        # 无任何阈值表述词/数值时保持拦截（门禁诚实性不放宽）
        support = assess_evidence(self.Q, ["合同签署须由双方法人签字，一式两份。"])
        self.assertEqual(support["status"], "unsupported")
        self.assertIn("阈值", support["missing"])

    def test_measured_threshold_keeps_strict_units(self):
        # 实测阈值（性能/水质类）保持严格锚点与严格单位：只认实测+单位数值
        row = "本期正式验证合计 18 次。2025-11-01 完成验收。计划阈值为 20 秒，实测阈值未记录。"
        support = assess_evidence("实测阈值和验证次数分别是多少？", [row])
        self.assertEqual(support["status"], "partial")
        self.assertEqual(support["missing"], ["实测阈值"])


class IdentifierMeaningAskTests(unittest.TestCase):
    Q_DECODE = "合同编号 CG-02-2025-108 分别代表什么类别、哪一年、第几号合同？"
    RULE = ("10.1 合同类别代码：CG-01 物资采购、CG-02 服务采购。"
            "合同编号由类别代码加年份加流水号构成，"
            "例如 CG-01-2025-036 表示 2025 年第 36 号物资采购合同。")

    def test_decode_ask_does_not_block_on_absent_number(self):
        # 问题中的编号本身不可能出现在语料（虚构合同号），编码规则章节即为证据：
        # 修复前门禁以「编号缺失」一票否决 → 拒答；修复后不判定、证据放行
        self.assertEqual(assess_evidence(self.Q_DECODE, [self.RULE]), {})

    def test_decode_ask_with_number_present_still_records_check(self):
        support = assess_evidence(self.Q_DECODE, ["编号 CG-02-2025-108 为服务采购合同。"])
        self.assertEqual(support["status"], "supported")
        self.assertIn("CG-02-2025-108", support["checked"])

    def test_locator_ask_keeps_strict_id_blocking(self):
        # 定位类问法（查某编号的属性）：编号不在证据中 → 仍拦截
        support = assess_evidence("CG-02-2025-108 合同的预算金额是多少？", [self.RULE])
        self.assertEqual(support["status"], "unsupported")


class ComparisonSideScopeTests(unittest.TestCase):
    def test_folder_derived_domain_only_binds_historical_side(self):
        # 修复表达式与 pipeline 一致：剥离文本推断的 domain 后两侧均有自然状态
        filters = {"domain": "archive"}
        scopes = comparison_statuses({k: v for k, v in filters.items() if k != "domain"})
        self.assertEqual(scopes["current"], ("active",))
        self.assertEqual(scopes["historical"], ("archive", "expired"))

    def test_manual_domain_keeps_old_single_side_behavior(self):
        # 界面手动选择 archive 知识板块（无显式状态）：现行侧为空（旧行为，尊重手选范围）
        scopes = comparison_statuses({"domain": "archive"})
        self.assertEqual(scopes["current"], ())
        self.assertEqual(scopes["historical"], ("archive",))

    def test_manual_status_selection_respected(self):
        scopes = comparison_statuses({"status": "active"})
        self.assertEqual(scopes["current"], ("active",))
        self.assertEqual(scopes["historical"], ())


if __name__ == "__main__":
    unittest.main()

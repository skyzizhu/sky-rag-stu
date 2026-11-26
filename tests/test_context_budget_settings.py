"""V3.9.2：检索设置上下文预算（context_max_tokens/context_max_per_doc）测试。

- 管道接线：Web 普通问答 honoring 两个覆盖参数（装配层可见 per-doc 限额与 token 上限）
- 枚举通道不跟随：枚举预算保持 40/12000
- SETTINGS_KEYS/normalize 校验：范围外值移除回退 .env 默认
"""

import unittest


class SettingsKeysAndNormalizeTests(unittest.TestCase):
    def test_keys_registered(self):
        from src.app_settings import SETTINGS_KEYS

        self.assertIn("context_max_tokens", SETTINGS_KEYS)
        self.assertIn("context_max_per_doc", SETTINGS_KEYS)

    def test_normalize_keeps_valid_and_pops_invalid(self):
        from src.app_settings import _normalize_choices

        settings = {"context_max_tokens": 4000, "context_max_per_doc": 3}
        self.assertEqual(_normalize_choices(dict(settings)), settings)
        popped = _normalize_choices({"context_max_tokens": 999, "context_max_per_doc": 11})
        self.assertNotIn("context_max_tokens", popped)
        self.assertNotIn("context_max_per_doc", popped)
        kept_edge = _normalize_choices({"context_max_tokens": 8000, "context_max_per_doc": 1})
        self.assertEqual(kept_edge["context_max_tokens"], 8000)
        self.assertEqual(kept_edge["context_max_per_doc"], 1)


class PipelineOverrideTests(unittest.TestCase):
    def setUp(self):
        from tests._helpers import IngestHarness

        self.harness = IngestHarness()
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        notes = [
            self.harness.write_note(
                "## 一、采购立项\n采购立项需要部门负责人审批。\n\n## 二、比价要求\n"
                "采购金额超过阈值须三家比价。\n\n## 三、合同签署\n合同由法务部会签。\n\n"
                "## 四、验收入库\n验收合格后入库。", "采购制度.md"),
            self.harness.write_note(
                "## 一、报销范围\n差旅与办公费用可报销。\n\n## 二、报销流程\n"
                "报销须经财务部审核。\n\n## 三、付款时限\n审核通过后十个工作日内付款。\n\n"
                "## 四、凭证要求\n凭证须为原件。", "报销制度.md"),
        ]
        self.harness.ingest(notes)
        self.harness.use_fakes()  # 工厂函数全部指向临时配置/内存向量库/假向量化，禁真连服务

    def _run(self, **kwargs):
        from src.pipeline import answer_question

        return answer_question("采购审批和报销流程的要求是什么？", top_k=10,
                               use_llm=False, use_query_understanding=False,
                               **kwargs)

    def _trace_text(self, result):
        parts = []
        for node in result.trace:
            parts.append(str(node.get("summary", "")))
            for key, value in node.get("items") or []:
                parts.append(f"{key}: {value}")
        return "\n".join(parts)

    def test_per_doc_limit_plumbs_into_assembly(self):
        result = self._run(context_max_per_doc=1)
        text = self._trace_text(result)
        self.assertIn("同一文档最多采用 1 条", text)
        self.assertLessEqual(len(result.retrieved), 2)

    def test_token_limit_plumbs_into_assembly(self):
        result = self._run(context_max_tokens=1000)
        self.assertIn("token 上限约 1000", self._trace_text(result))

    def test_enum_channel_keeps_its_own_budget(self):
        from src.pipeline import answer_question

        result = answer_question("列出采购制度与报销制度分别有哪些章节？", top_k=10,
                                 use_llm=False, use_query_understanding=False,
                                 context_max_tokens=1000, context_max_per_doc=1)
        meta = getattr(result, "evidence_meta", None) or {}
        self.assertEqual(meta.get("ctx_max_items"), 40)  # 枚举名额不随用户设置缩小
        trace_text = self._trace_text(result)
        self.assertNotIn("同一文档最多采用 1 条", trace_text)


if __name__ == "__main__":
    unittest.main()

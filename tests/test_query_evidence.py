"""Evidence 统一查询入口（V3.9）+ MCP 文档工具的验证。

- query_evidence()：answer_stream 检索半程抽出的独立函数——Evidence/Answer 两个出口共用；
  返回 QAResult（sources/trace/elapsed/usage）+ evidence_meta（coverage 粗判定）；
- MCP get_document / list_documents：按 document_id 取全文/片段、按权限枚举目录。
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from src.pipeline import query_evidence
from src.metadata import build_document_metadata
from tests._helpers import IngestHarness


class QueryEvidenceTests(IngestHarness):
    """Evidence 统一入口：检索半程搬出 answer_stream 后行为不变。"""

    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()
        from src.pipeline import clear_qa_cache
        clear_qa_cache()
        self.ingest([self.write_note("Xtools 支持字幕、降噪、PDF 解析等功能。" * 5, "xtools.md")])

    def test_returns_sources_trace_and_elapsed(self):
        result, meta = query_evidence("Xtools 怎么去除噪音？", top_k=5,
                                      use_query_understanding=False, use_rerank=False)
        self.assertGreaterEqual(len(result.sources), 1)
        self.assertGreaterEqual(len(result.trace), 1)          # 检索半程节点（融合/召回等）
        self.assertIn("retrieval", result.elapsed)
        self.assertIn("coverage", meta)
        self.assertFalse(result.enumeration)

    def test_empty_filter_scope_is_not_found(self):
        result, meta = query_evidence("随便问点什么", top_k=5,
                                      filters={"domain": "no_such_domain"},
                                      use_query_understanding=False, use_rerank=False)
        self.assertEqual(len(result.retrieved), 0)
        self.assertEqual(meta["coverage"], "not_found")

    def test_answers_remove_invalid_citations_and_report_them(self):
        from src.answer_evaluator import compute_signals
        from src.pipeline import answer_question

        def generate(_messages, usage_out):
            usage_out["finish_reason"] = "stop"
            yield "支持降噪 [1]，另一说法 [99]。示例代码 `arr[42]`。"

        with mock.patch("src.pipeline.get_llm_client", return_value=SimpleNamespace(chat_stream=generate)):
            result = answer_question("Xtools 怎么降噪？", use_query_understanding=False, use_rerank=False)
        self.assertIn("[1]", result.answer)
        self.assertNotIn("[99]", result.answer)
        self.assertIn("`arr[42]`", result.answer)
        self.assertEqual(compute_signals(result)["cite"], {"cited": 1, "total": 1, "invalid": [99]})

    def test_web_answer_cache_isolated_by_answer_budgets(self):
        from src.pipeline import answer_question

        def generate(_messages, usage_out):
            usage_out["finish_reason"] = "stop"
            yield "支持降噪 [1]。"

        def answer(cfg):
            return answer_question("Xtools 怎么降噪？", use_query_understanding=False, use_rerank=False, config=cfg)

        with mock.patch("src.pipeline.get_llm_client", return_value=SimpleNamespace(chat_stream=generate)):
            self.assertFalse(answer(self.cfg).cache_hit)
            self.assertTrue(answer(self.cfg).cache_hit)
            for key in ("context_max_tokens", "context_max_per_doc", "rerank_recall_k", "llm_max_tokens"):
                with self.subTest(key=key):
                    updated = replace(self.cfg, **{key: getattr(self.cfg, key) + 1})
                    self.assertFalse(answer(updated).cache_hit)
                    self.assertTrue(answer(updated).cache_hit)
            limited = answer(replace(self.cfg, context_max_tokens=16))
            self.assertFalse(limited.cache_hit)
            self.assertEqual(limited.sources, [])

    def test_revoke_during_generation_withholds_new_deltas_and_clears_output(self):
        from src import mcp_server
        from src.manage import update_metadata
        from src.pipeline import answer_stream

        path = "learning/general/xtools.md"

        def generate(_messages, usage_out):
            yield "可见时已发出的片段。"
            update_metadata(path, {"allowed_groups": [], "allowed_users": []})
            usage_out["finish_reason"] = "stop"
            yield "撤权后生成的片段 [1]。"

        with mock.patch("src.pipeline.get_llm_client", return_value=SimpleNamespace(chat_stream=generate)):
            result, deltas = answer_stream("Xtools 怎么降噪？", use_query_understanding=False,
                                           use_rerank=False, user_groups=[], username="alice")
            self.assertEqual(list(deltas), ["可见时已发出的片段。"])
            self.assertEqual(result.evidence_meta["generation_status"], "invalidated")
            self.assertEqual((result.sources, result.retrieved, result.context, result.trace), ([], [], "", []))
            self.assertNotIn("片段", result.answer)
            update_metadata(path, {"allowed_groups": ["all"], "allowed_users": []})
            with mock.patch.object(mcp_server, "_identity_from_ctx", return_value=([], "alice", {})), \
                 mock.patch("src.config.get_config", return_value=replace(self.cfg, query_understanding=False,
                                                                        rerank_enabled=False)):
                response = json.loads(mcp_server._tool_knowledge_qa_impl("Xtools 怎么降噪？"))
            self.assertTrue(response["refused"])
            self.assertEqual(response["sources"], [])
            self.assertEqual(response["coverage_details"]["generation_status"], "invalidated")

    def test_mcp_answer_serializes_citations_without_raw_grants(self):
        from src import mcp_server
        from src.pipeline import QAResult

        result = QAResult("query", "Entry [1]", [], "", [], filters={"__viewer__": {"username": "alice"}})
        result.enumerated_items = [{
            "name": "Entry", "citations": [1], "evidence": [{
                "source_id": "p", "quote": "Entry appears in the source",
                "metadata": {"document_id": "d", "page": "13", "allowed_users": ["alice", "bob"]},
            }],
        }]
        with mock.patch.object(mcp_server, "_answer_in_scope", return_value=result):
            response = json.loads(mcp_server._tool_knowledge_qa_impl("query"))
        self.assertEqual(response["items"][0]["evidence"][0]["metadata"], {"document_id": "d", "page": "13"})
        self.assertEqual(response["filters_applied"], {})

    def test_mcp_uses_generated_evidence_state_with_sources_and_hides_status_line(self):
        from src import mcp_server
        from src.pipeline import answer_stream

        def generate(_messages, usage_out):
            usage_out["finish_reason"] = "stop"
            yield "[[answer_"
            yield "status:insufficient_evidence]]\n"
            yield "现有 Xtools 文档不足以确认实际降噪效果。"

        with mock.patch("src.pipeline.get_llm_client", return_value=SimpleNamespace(chat_stream=generate)):
            result, deltas = answer_stream("Xtools 的降噪效果如何？", use_query_understanding=False,
                                           use_rerank=False)
            self.assertEqual("".join(deltas), "现有 Xtools 文档不足以确认实际降噪效果。")
            self.assertTrue(result.sources)
            self.assertEqual(result.evidence_meta["answer_status"], "insufficient_evidence")
            with mock.patch.object(mcp_server, "_answer_in_scope", return_value=result):
                response = json.loads(mcp_server._tool_knowledge_qa_impl("Xtools 的降噪效果如何？"))
            self.assertIn("evidence_notice", response)  # 方案A：证据随行信任边界声明
        self.assertTrue(response["refused"])
        self.assertEqual(response["answer_status"], "insufficient_evidence")
        self.assertTrue(response["sources"])

    def test_answer_keeps_verified_deadline_boundary(self):
        from src.evidence_support import deadline_constraint, preserve_deadline_wording
        from src.pipeline import answer_question

        question = "华信合同约定的设备交付截止日期是什么？实际何时完成交付？"
        reference = "The vendor shall deliver all equipment before 2024-09-30."
        self.ingest([self.write_note(reference, "华信合同.md")])

        def generate(_messages, usage_out):
            usage_out["finish_reason"] = "stop"
            yield "[[answer_status:partial]]\n设备应不迟于 2024-09-30 交付 [1]。实际交付日期无法确认。"

        with mock.patch("src.pipeline.get_llm_client", return_value=SimpleNamespace(chat_stream=generate)):
            result = answer_question(question, filters={"source": "华信合同.md"},
                                     use_query_understanding=False, use_rerank=False)
        self.assertIn("2024-09-30之前", result.answer)
        self.assertIn("实际交付日期无法确认", result.answer)
        self.assertTrue(result.evidence_meta["deadline_wording_corrected"])
        self.assertEqual(result.evidence_meta["answer_status"], "partial")

        inclusive = deadline_constraint(question, [{"rank": 1, "text": reference.replace("before", "by")}])
        self.assertIn("2024-09-30当日及之前", preserve_deadline_wording("应在 2024-09-30前交付。", inclusive))
        self.assertEqual(preserve_deadline_wording("付款不得晚于2024-09-30。", result.evidence_meta["deadline_constraint"]),
                         "付款不得晚于2024-09-30。")
        self.assertFalse(deadline_constraint(question, [
            {"rank": 1, "text": reference}, {"rank": 2, "text": reference.replace("before", "by")}]))
        self.assertFalse(deadline_constraint(question, [
            {"rank": 1, "text": "The vendor delivered all equipment before 2024-09-30."}]))
        english = deadline_constraint(question, [{"rank": 1, "text": reference.replace("2024-09-30", "September 30, 2024")}])
        self.assertEqual(preserve_deadline_wording("Deliver by September 30, 2024.", english),
                         "Deliver before September 30, 2024.")

    def test_factual_gate_rejects_unsupported_candidates_on_web_and_mcp(self):
        from src import mcp_server
        from src.pipeline import answer_question

        self.ingest([self.write_note("验收工单 EVT-500 的实测阈值为 17 秒。采购预算尚未填报。", "evidence.md")])
        for query, reason in (
            ("工单 EVT-501 的验收阈值是多少？", "EVT-501"),
            ("采购预算是多少？", "预算金额"),
            ("是否记录了退款办法？", "退款"),
        ):
            with self.subTest(query=query), \
                 mock.patch("src.pipeline.get_llm_client", side_effect=AssertionError("unexpected generation")):
                result = answer_question(query, use_llm=True, use_query_understanding=False, use_rerank=False)
                response = json.loads(mcp_server._tool_knowledge_search_impl(query))
                self.assertIn("evidence_notice", response)  # 方案A：证据随行信任边界声明
                self.assertEqual(result.sources, [])
                self.assertIn(reason, result.evidence_meta["evidence_support"]["missing"])
                self.assertEqual(response["matches"], [])
                self.assertTrue(response["refused"])
                self.assertTrue(response["reason"])

    def test_factual_gate_keeps_partial_and_supported_fields(self):
        from src.evidence_support import assess_evidence

        row = "本期正式验证合计 18 次。2025-11-01 完成验收。计划阈值为 20 秒，实测阈值未记录。"
        support = assess_evidence("实测阈值和验证次数分别是多少？", [row])
        self.assertEqual(support["status"], "partial")
        self.assertEqual(support["missing"], ["实测阈值"])
        self.assertEqual(assess_evidence("验收日期是哪天？", [row])["status"], "supported")
        self.assertEqual(assess_evidence("预算金额是多少？", ["预算金额 | 120,000 元"])["status"], "supported")
        self.assertEqual(assess_evidence("如何设置降噪？", ["可开启噪声抑制。"]), {})

    def test_date_evidence_recognizes_chinese_acceptance_and_english_delivery(self):
        from src.evidence_support import assess_evidence

        cases = (
            ("沧海项目是什么时候通过验收的？",
             "项目于 2025-07-15 通过客户验收，交付物共 8 项。", "验收日期"),
            ("合同约定的设备交付时间是什么时候？",
             "The Vendor shall deliver all equipment before 2024-09-30.", "交付日期"),
            ("When is delivery due?",
             "The Vendor shall deliver all equipment by\nSeptember 30, 2024.", "交付日期"),
        )
        for query, passage, field in cases:
            with self.subTest(query=query):
                support = assess_evidence(query, [passage])
                self.assertEqual(support["status"], "supported")
                self.assertEqual(support["date_fields"], [field])

    def test_date_evidence_does_not_reuse_signing_date_or_relax_missing_ids(self):
        from src.evidence_support import assess_evidence

        for passage in ("签署日为 2024-01-10，设备尚未交付。",
                        "合同签署日期为 2024-01-10 交付时间另行通知。",
                        "The contract was signed on 2024-01-10 and delivery will be arranged.",
                        "# 交付说明\n\n签署日期为 2024-01-10。"):
            with self.subTest(passage=passage):
                support = assess_evidence("设备什么时候交付？", [passage])
                self.assertEqual(support["status"], "partial")
                self.assertEqual(support["missing"], ["交付日期"])
        self.assertEqual(assess_evidence("EVT-501 的交付日期是什么时候？",
                                        ["工单 EVT-500 于 2024-01-10 交付。"])["status"], "unsupported")

    def test_unconfirmed_date_keeps_only_visible_candidate_sources(self):
        from src import mcp_server
        from src.prompt import build_messages

        public = self.write_note("合同于 2024-01-10 签署，设备交付日期待定。", "signed.md")
        private = self.write_note("设备将于 2025-01-01 交付。", "delivery.md")
        self.ingest([public])
        self.ingest([private], default_groups=["finance"])
        question = "设备什么时候交付？"
        result, meta = query_evidence(question, use_llm=False, use_rerank=False, user_groups=["hr"])
        self.assertEqual(meta["coverage"], "partial")
        self.assertIn("日期规则未确认", result.context)
        self.assertIn("signed.md", {s["source"] for s in result.sources})
        self.assertNotIn("delivery.md", {s["source"] for s in result.sources})
        messages = build_messages(result.context, question, evidence_meta=meta)
        self.assertIn("合同截止日不能写成实际完成日", messages[0]["content"])
        with mock.patch.object(mcp_server, "_identity_from_ctx", return_value=(["hr"], "eve", {})):
            search = json.loads(mcp_server._tool_knowledge_search_impl(question))
        self.assertIn("signed.md", {s["source"] for s in search["matches"]})
        self.assertNotIn("delivery.md", {s["source"] for s in search["matches"]})
        self.assertIn("日期规则未确认", search["reason"])

    def test_evidence_only_uses_local_rerank_without_llm(self):
        from src.reranker import RerankOutcome

        def rerank(question, items, top_k, cfg):
            return RerankOutcome(items=items, backend="local_onnx", ok=True)

        with mock.patch("src.pipeline.rerank_documents_local", side_effect=rerank) as local, \
             mock.patch("src.pipeline.get_llm_client", side_effect=AssertionError("unexpected LLM")):
            result, _ = query_evidence("Xtools 怎么降噪？", use_llm=False,
                                       config=replace(self.cfg, rerank_backend="local_onnx"))
        local.assert_called_once()
        self.assertTrue(result.sources)

    def test_coverage_partial_when_quota_cuts_cards(self):
        """枚举模式名额用满（丢弃原因含「上限」）→ coverage=partial。"""
        # 50 节、每节正文 ~500 字（相邻两节 >600 → 各自成卡）：50 张卡 > 名额 40 → 必有截断
        sections = "\n".join(f"### 功能 {i:02d}\n这是功能 {i:02d} 的独立说明，编号 {i}，"
                             f"包含核心能力、适用场景与使用限制。" * 15 for i in range(1, 51))
        big = self.knowledge / "learning" / "general" / "features.md"
        big.write_text(f"# 产品功能\n\n{sections}\n", encoding="utf-8")
        self.ingest([big])
        result, meta = query_evidence("列出该产品的全部功能", top_k=5,
                                      use_query_understanding=False, use_rerank=False)
        self.assertTrue(result.enumeration)
        self.assertEqual(meta["coverage"], "partial")


class MetadataFieldsTests(unittest.TestCase):
    """V3.9 元数据补字段：effective_date（FM 声明）与 owner（入库人，入库时写入）。"""

    def test_effective_date_from_front_matter(self):
        meta = build_document_metadata(
            relative_path="reference/general/a.md", source="a.md", file_type="md",
            title_hint=None, created_at=None, updated_at=None,
            front_matter={"effective_date": "2026-01-01"})
        self.assertEqual(meta["effective_date"], "2026-01-01")

    def test_effective_date_default_empty(self):
        """缺省为空，不造假数据（生效日期是制度性信息，宁缺勿错）。"""
        meta = build_document_metadata(
            relative_path="reference/general/a.md", source="a.md", file_type="md",
            title_hint=None, created_at=None, updated_at=None, front_matter={})
        self.assertEqual(meta["effective_date"], "")

    def test_ingest_owner_lands_in_payload(self):
        """入库人写入每张卡的 payload（审计与溯源依据）。"""

        class V(IngestHarness):
            def runTest(self):
                pass

        v = V("runTest")
        v.setUp()
        v.use_fakes()
        p = v.knowledge / "learning" / "general" / "owned.md"
        p.write_text("# 有主文档\n\n正文内容。\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            v.ingest([p], owner="alice")
        from src.vector_store import get_vector_store
        cards = get_vector_store().get_all_cards(filters={"source": "owned.md"})
        self.assertGreaterEqual(len(cards), 1)
        self.assertTrue(all(c["payload"].get("owner") == "alice" for c in cards))


class McpDocumentToolsTests(IngestHarness):
    """get_document / list_documents：按权限取全文/分页片段/枚举目录。"""

    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()
        # 每节正文 ~300 字：相邻两节合计 >600 → 短小节合并不生效，6 节各自成卡（可分页）
        md = ("# Xtools 功能\n\n" +
              "\n\n".join(f"### 功能 {i:02d}\n功能 {i:02d} 的详细说明，编号 {i}，涵盖核心能力、适用场景与使用限制。" * 12
                          for i in range(1, 7)))
        p = self.knowledge / "reference" / "general" / "doc.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(md, encoding="utf-8")
        # 默认仅 hr 部门可见：无令牌（个人模式）可见；finance 部门被拒
        self.ingest([p], default_groups=["hr"])
        from src.vector_store import get_vector_store
        self.document_id = next(
            d["document_id"] for d in get_vector_store().list_documents()
            if d["source"] == "doc.md")

    def _call(self, fn, *args, **kwargs):
        from src import mcp_server
        with contextlib.redirect_stdout(io.StringIO()):
            return json.loads(getattr(mcp_server, f"_tool_{fn}_impl")(*args, **kwargs))

    def test_knowledge_search_exposes_coverage_and_document_id(self):
        """查询模式响应：coverage 粗判定 + matches 带 document_id（get_document 入口）。"""
        payload = self._call("knowledge_search", "功能 01 的说明", top_k=3)
        self.assertIn(payload["coverage"], ("not_found", "partial", "unknown"))
        self.assertTrue(all("document_id" in m and "version" in m
                            for m in payload["matches"]))

    def test_knowledge_search_expands_evidence_only_for_the_request(self):
        from src.config import get_config

        question = "运维交接记录"
        paths = [self.write_note(f"{question} 样本 {i}。" + "设备交接需要核对标签和接收签字。" * 29,
                                 f"brief-{i:02}.md") for i in range(12)]
        self.ingest(paths, default_groups=["hr"])
        self.ingest([self.write_note(f"{question} 保密内容。", "finance-only.md")], default_groups=["finance"])
        with mock.patch("src.mcp_server._identity_from_ctx", return_value=(["all", "hr"], "alice", {})):
            default = self._call("knowledge_search", question, domain="learning")
            expanded = self._call("knowledge_search", question, domain="learning", top_k=100, max_evidence_tokens=10000)
            limited = self._call("knowledge_search", question, domain="learning", top_k=12, max_evidence_tokens=1000)
            self.assertEqual(self._call("knowledge_search", question, domain="learning")["matches"], default["matches"])

        self.assertEqual(default["evidence_budget"]["top_k"], 5)
        self.assertEqual(default["evidence_budget"]["max_evidence_tokens"], 4000)
        budget = expanded["evidence_budget"]
        self.assertEqual((budget["top_k"], budget["max_evidence_tokens"], budget["returned_matches"]), (12, 8000, 12))
        self.assertEqual({match["source"] for match in expanded["matches"]}, {path.name for path in paths})
        self.assertTrue(all(match["document_id"] and match["version"] and match["chunk_ids"]
                            for match in expanded["matches"]))
        limited_budget = limited["evidence_budget"]
        self.assertLess(limited_budget["returned_matches"], budget["returned_matches"])
        self.assertLessEqual(limited_budget["estimated_text_tokens"], 1000)
        self.assertIn("max_evidence_tokens", limited_budget["truncated_by"])
        self.assertTrue(limited_budget["truncated"])
        self.assertEqual(limited["coverage"], "partial")
        self.assertIs(get_config(), self.cfg)
        _, web_meta = query_evidence(question, use_llm=False, config=replace(self.cfg, context_max_tokens=2500),
                                     use_rerank=False,
                                     user_groups=["all", "hr"], username="alice")
        self.assertEqual(web_meta["context_budget"]["max_tokens"], 2500)

    def test_knowledge_search_auto_expands_only_the_limit_blocking_a_fact(self):
        from src.retriever import RetrievedItem

        # V3.9.1 预扩容后，带「多少」等标记的问题在入口直接放到渠道上限（见下一个用例）。
        # 这里用不含标记的问法检验自适应循环：只放宽卡住事实的那一项限制。
        # 前 6 条与 DPC-550 无关（编号锚点不在窗口内），第 7 条才出现该设备。
        hits = [RetrievedItem(i, 1 / i, f"DPC-{560 + i} 设备说明第 {i} 页。", {
            "document_id": f"d{i}", "chunk_id": f"d{i}_0000", "source": f"d{i}.md", "version": "1.0",
        }, "关键词") for i in range(1, 8)]

        def retrieve(query, *, top_k, **kwargs):
            return hits[:top_k]

        question = "设备 DPC-550 的预算"
        with mock.patch("src.pipeline.get_retriever", return_value=SimpleNamespace(retrieve=retrieve)):
            missing = self._call("knowledge_search", question)
            self.assertEqual(missing["evidence_budget"]["expanded_by"], [])
            self.assertEqual(missing["evidence_budget"]["top_k"], 5)
            self.assertEqual(missing["coverage_details"]["evidence_support"]["missing"], ["DPC-550"])
            hits[6] = replace(hits[6], text="DPC-550 的预算为 200 万元。")
            simple = self._call("knowledge_search", "DPC-560 设备说明")
            self.assertEqual(simple["evidence_budget"]["top_k"], 5)
            self.assertEqual(simple["evidence_budget"]["expanded_by"], [])
            result = self._call("knowledge_search", question)

        self.assertEqual(result["evidence_budget"]["top_k"], 12)
        self.assertEqual(result["evidence_budget"]["max_evidence_tokens"], 4000)
        self.assertEqual(result["evidence_budget"]["expanded_by"], ["top_k"])
        self.assertTrue(any("200 万元" in match["text"] for match in result["matches"]))

        hits[:] = [replace(hits[0], text="DPC-550 的预算为 200 万元。" + "背景。" * 1900)]
        with mock.patch("src.pipeline.get_retriever", return_value=SimpleNamespace(retrieve=retrieve)):
            result = self._call("knowledge_search", question)
        self.assertEqual(result["evidence_budget"]["top_k"], 5)
        self.assertEqual(result["evidence_budget"]["max_evidence_tokens"], 8000)
        self.assertEqual(result["evidence_budget"]["expanded_by"], ["max_evidence_tokens"])

    def test_multipoint_question_is_pre_expanded_even_with_a_small_explicit_top_k(self):
        from src.retriever import RetrievedItem

        question = "设备 DPC-550 的预算是多少？"  # 含「多少」标记
        hits = [RetrievedItem(i, 1 / i, f"DPC-550 设备说明第 {i} 页。", {
            "document_id": f"d{i}", "chunk_id": f"d{i}_0000", "source": f"d{i}.md", "version": "1.0",
        }, "关键词") for i in range(1, 8)]
        hits[6] = replace(hits[6], text="DPC-550 的预算为 200 万元。")
        retrieve = mock.Mock(side_effect=lambda query, *, top_k, **kwargs: hits[:top_k])
        with mock.patch("src.pipeline.get_retriever", return_value=SimpleNamespace(retrieve=retrieve)):
            auto = self._call("knowledge_search", question)
            fixed = self._call("knowledge_search", question, top_k=5)
        # V3.9.1：多点/取值类问题在入口即放到渠道上限，显式给小的 top_k 同样放宽
        for payload in (auto, fixed):
            budget = payload["evidence_budget"]
            self.assertEqual((budget["top_k"], budget["max_evidence_tokens"]), (12, 8000))
            self.assertEqual(budget["expanded_by"], [])
            self.assertTrue(any("200 万元" in match["text"] for match in payload["matches"]))

    def test_mcp_resolves_two_limits_within_one_retrieval(self):
        from src.retriever import RetrievedItem

        markers = [f"DPC-550 说明 {i}。" + "设备背景。" * 153 for i in range(1, 7)]
        markers.append("DPC-550 的预算为 200 万元。" + "补充资料。" * 153)
        hits = [RetrievedItem(i, 1 / i, text, {"document_id": f"d{i}", "source": f"d{i}.md"}, "关键词")
                for i, text in enumerate(markers, 1)]
        retrieve = mock.Mock(side_effect=lambda query, *, top_k, **kwargs: hits[:top_k])
        question = "设备 DPC-550 的预算是多少？"
        with mock.patch("src.pipeline.get_retriever", return_value=SimpleNamespace(retrieve=retrieve)):
            result = self._call("knowledge_search", question)
            retrieve.assert_called_once()
            fixed = self._call("knowledge_search", question, max_evidence_tokens=4000)
        budget = result["evidence_budget"]
        self.assertEqual((budget["top_k"], budget["max_evidence_tokens"]), (12, 8000))
        # V3.9.1 预扩容：带「多少」标记的问题在入口即放到渠道上限，两项限制一次解决，
        # 自适应循环无需再放宽；仍然只检索一次
        self.assertEqual(budget["expanded_by"], [])
        self.assertTrue(any("200 万元" in match["text"] for match in result["matches"]))
        # 显式给小的 token 预算同样被放宽：证据完整性优先于条数节约
        self.assertEqual(fixed["evidence_budget"]["max_evidence_tokens"], 8000)
        self.assertEqual(fixed["coverage_details"]["evidence_support"]["missing"], [])

    def test_get_document_full_stitches_single(self):
        payload = self._call("get_document", [self.document_id], mode="full")
        self.assertEqual(payload["total_cards"], 6)
        for i in range(1, 7):
            self.assertIn(f"功能 {i:02d}", payload["full_text"], i)
        self.assertEqual(payload["full_text"].count("来源文档："), 1)  # 仅首卡保留来源前缀
        # 章节清单供 Agent 定位（R9 起 = 叶子名 ∪ 完整路径并集，路径用于消歧）
        section_names = {s for s in payload["sections"] if ">" not in s}
        self.assertEqual(section_names, {f"功能 {i:02d}" for i in range(1, 7)})
        self.assertTrue(all("功能" in s for s in payload["sections"]))

    def test_get_document_fragments_paginates(self):
        payload = self._call("get_document", [self.document_id], page=2, page_size=2,
                             mode="fragments")
        self.assertEqual(payload["page"], 2)
        self.assertEqual(payload["total_cards"], 6)
        self.assertEqual(payload["total_pages"], 3)
        self.assertEqual(len(payload["fragments"]), 2)

    def test_get_document_section_filter(self):
        """章节过滤：只返回章节名包含关键词的卡片。"""
        payload = self._call("get_document", [self.document_id], mode="full",
                             section="功能 03")
        self.assertIn("功能 03", payload["full_text"])
        self.assertNotIn("功能 04", payload["full_text"])
        # 章节过滤命中后：清单含该节叶子名（完整路径形式可并存，均含"功能 03"）
        self.assertIn("功能 03", " ".join(payload["sections"]))

    def test_get_document_multi_and_not_found(self):
        """多文档一次取全：可见的返回全文；不可见的归入 not_found（不泄露存在性）。"""
        other = self.knowledge / "reference" / "general" / "doc2.md"
        other.write_text("# 另一篇\n\n另一篇的正文。\n", encoding="utf-8")
        self.ingest([other])
        from src.vector_store import get_vector_store
        other_id = next(d["document_id"] for d in get_vector_store().list_documents()
                        if d["source"] == "doc2.md")
        payload = self._call("get_document", [self.document_id, other_id, "doc_不存在"],
                             mode="full")
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["not_found"], ["doc_不存在"])
        self.assertFalse(payload.get("refused", False))
        texts = [d["full_text"] for d in payload["documents"]]
        self.assertTrue(any("功能 01 的详细说明" in t for t in texts))
        self.assertTrue(any("另一篇的正文" in t for t in texts))

    def test_get_document_refuses_when_none_visible(self):
        """全部请求都不可见时才 refused；finance 令牌看不见 hr 限权文档。"""
        from src import mcp_server
        with mock.patch.object(mcp_server, "_identity_from_ctx",
                               return_value=(["finance"], "eve", {"username": "eve"})), \
             contextlib.redirect_stdout(io.StringIO()):
            denied = self._call("get_document", [self.document_id])
        self.assertTrue(denied.get("refused"))

    def test_document_access_is_audited(self):
        """合规（方案 §9.2）：文档读取与目录枚举必须落审计（谁在何时看了什么）。"""
        from src import auth_db, mcp_server
        with mock.patch.object(auth_db, "log_doc_access") as mlog, \
             contextlib.redirect_stdout(io.StringIO()):
            self._call("get_document", self.document_id, mode="full")
            self._call("list_documents")
        actions = [c.kwargs.get("action") for c in mlog.call_args_list]
        self.assertIn("read_document", actions)
        self.assertIn("list_documents", actions)

    def test_list_documents_filters_by_permission_and_domain(self):
        payload = self._call("list_documents")
        self.assertEqual(payload["total"], 1)
        payload = self._call("list_documents", domain="reference")
        self.assertEqual(payload["total"], 1)
        payload = self._call("list_documents", domain="learning")
        self.assertEqual(payload["total"], 0)
        from src import mcp_server
        with mock.patch.object(mcp_server, "_identity_from_ctx",
                               return_value=(["finance"], "eve", {"username": "eve"})), \
             contextlib.redirect_stdout(io.StringIO()):
            denied = self._call("list_documents")
        self.assertEqual(denied["total"], 0)

    def test_read_historical_match_by_version_and_status(self):
        p = self.knowledge / "reference/general/doc.md"
        p.write_text("# 新版\n\n只在新版本出现的产品说明。", encoding="utf-8")
        self.ingest([p])
        search = self._call("knowledge_search", "功能说明", status="expired")
        match = search["matches"][0]
        self.assertEqual(match["status"], "expired")
        old = self._call("get_document", [match["document_id"]], version=match["version"],
                         status=match["status"], mode="full")
        self.assertEqual(old["version"], match["version"])
        self.assertIn("功能 01", old["full_text"])
        self.assertNotIn("只在新版本", old["full_text"])
        all_versions = self._call("list_documents", status="all", page=2, page_size=1)
        self.assertEqual(all_versions["total"], 2)
        self.assertEqual(len(all_versions["documents"]), 1)
        self.assertFalse(all_versions["has_more"])


if __name__ == "__main__":
    unittest.main()


class IdentityFromCtxTests(unittest.TestCase):
    """逐次验身份（V3.9）：ctx 携带当次请求 → 令牌派生权限；无请求回退 contextvar。"""

    def _fake_ctx(self, auth_header: str | None):
        headers = {"authorization": auth_header} if auth_header else {}
        request = mock.Mock(headers=headers)
        rc = mock.Mock(request=request)
        return mock.Mock(request_context=rc)

    def test_valid_token_derives_identity(self):
        from src import auth_db, mcp_server
        ident = {"username": "alice", "department": "hr"}
        with mock.patch.object(auth_db, "verify_token", return_value=ident) as vt, \
             mock.patch("src.permissions.visible_groups", return_value=["all", "hr"]):
            groups, username, user = mcp_server._identity_from_ctx(
                self._fake_ctx("Bearer mcp_pat_valid"))
        vt.assert_called_once_with("mcp_pat_valid")
        self.assertEqual(username, "alice")
        self.assertIn("hr", groups)

    def test_http_request_missing_token_is_rejected(self):
        from src import mcp_server
        ctx = self._fake_ctx(None)
        with mock.patch.object(mcp_server, "_request_groups", mock.Mock(get=lambda: None)), \
             mock.patch.object(mcp_server, "_request_user",
                               mock.Mock(get=lambda: {"username": "bob", "department": "it"})):
            with self.assertRaises(PermissionError):
                mcp_server._identity_from_ctx(ctx)
            groups, username, user = mcp_server._identity_from_ctx(None)
        self.assertEqual(username, "bob")  # 仅无 HTTP 请求的调用保留个人模式兜底。

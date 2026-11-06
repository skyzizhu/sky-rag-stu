"""领域功能的检索准确性审计（每一条都对应一个真实问题，修复后作为回归测试保留）。

① 只改元数据不改卡片数的写操作（档案编辑）后，BM25 / 问答缓存不刷新
   ——最严重的后果：收窄可见部门后，其他部门仍能经 BM25 通道检索到该文档
② 其他进程（前台 ↔ MCP）的写操作，本进程的 BM25 / 问答缓存感知不到
③ 为已有文件夹创建领域后，文件夹里已入库的卡片还留在 reference，按新领域检索不到
④ 文件头声明的领域不计入数据量 → 还有卡片引用的领域可以被删除
⑤ MCP 传显示名称或未知领域 → 检索静默为空，Agent 会误报「知识库里没有」
⑥ 网页入库选择的领域不生效（一律落到 reference）
⑦ 检索领域选 archive：默认只查 active，结果永远为空
⑧ 过滤放宽重试对登录员工失效（权限域让 filters 永远非空），大模型猜错就误报「没有」
⑨ 相邻卡片拼接不分版本：新旧版本同时召回时拼成一段，引用与内容都错
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from src import domains, index_epoch
from src import keyword_search
from src import pipeline as pipeline_mod
from src.context import build_context
from src.manage import ManageError, ingest_web_page, update_metadata
from src.query_understanding import QueryUnderstanding
from src.retriever import RetrievedItem, effective_filters
from tests._helpers import IngestHarness


class _RetrievalCase(IngestHarness):
    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()

    def sources(self, query: str, **kwargs) -> set[str]:
        return {source for source, _domain in self.retrieve_hits(query, **kwargs)}

    def ingest_with_corpus(self, body: str, name: str, folder: str = "learning/general") -> None:
        """入库目标文档 + 同目录几篇无关文档。

        语料必须有区分度：一个词在一半及以上的文档里出现时 IDF ≤ 0，BM25 不返回任何结果，
        关键词通道（及其缓存）根本没参与——这样的用例在有 bug 的代码上也会通过。
        """
        others = {"z1.md": "Python 装饰器的原理与用法。", "z2.md": "Rust 所有权规则说明。",
                  "z3.md": "Docker 镜像分层机制。", "z4.md": "Kafka 分区再均衡过程。"}
        self.ingest([self.write_note(body, name, folder),
                     *(self.write_note(text, fname, folder) for fname, text in others.items())])


class MetadataEditCacheTests(_RetrievalCase):
    def test_narrowing_visibility_takes_effect_in_keyword_channel(self):
        """① 权限问题：收窄可见部门后，develop 员工不能再检索到 hr 专属文档。"""
        self.ingest_with_corpus("薪酬调整方案：明年整体上调百分之八。", "salary.md")
        self.assertIn("salary.md", self.sources("薪酬调整方案", user_groups=["develop"]))

        update_metadata("learning/general/salary.md", {"allowed_groups": ["hr"]})

        self.assertNotIn("salary.md", self.sources("薪酬调整方案", user_groups=["develop"]))
        self.assertIn("salary.md", self.sources("薪酬调整方案", user_groups=["hr"]))

    def test_category_edit_reaches_keyword_channel(self):
        """① 同一类问题：改分类后，BM25 返回的卡片元数据不能还是旧分类。"""
        self.ingest_with_corpus("报销流程：先填申请单。", "a.md")

        def categories() -> set[str]:
            hits = keyword_search.bm25_search("报销流程", top_k=1, store=self.store,
                                              filters={"status": "active"}, config=self.cfg)
            return {h["payload"]["category"] for h in hits}

        self.assertEqual(categories(), {"general"})  # 缓存已建好
        update_metadata("learning/general/a.md", {"category": "newcat"})
        self.assertEqual(categories(), {"newcat"})

    def test_metadata_edit_clears_answer_cache(self):
        self.ingest([self.write_note("内容", "a.md")])
        pipeline_mod._qa_cache["旧答案"] = object()
        update_metadata("learning/general/a.md", {"category": "newcat"})
        self.assertNotIn("旧答案", pipeline_mod._qa_cache)


class CrossProcessCacheTests(_RetrievalCase):
    def test_keyword_cache_notices_payload_change_by_another_process(self):
        """② 另一个进程只改了 payload：本进程没有任何清缓存调用，只能靠纪元文件感知。"""
        self.ingest_with_corpus("报销流程：先填申请单。", "a.md", "finance")
        self.assertIn("a.md", self.sources("报销流程", domain="reference"))  # 未登记 → reference

        domains.create_domain("finance", "财务")
        self.store.reassign_domain([d["document_id"] for d in self.store.list_documents()], "finance")

        self.assertEqual(self.sources("报销流程", domain="reference"), set())
        self.assertIn("a.md", self.sources("报销流程", domain="finance"))

    def test_answer_cache_dropped_when_another_process_changes_index(self):
        self.ingest([self.write_note("报销流程", "a.md")])
        self.sources("报销流程")
        pipeline_mod._qa_cache["旧答案"] = object()

        index_epoch.bump()  # 另一个进程入库了新版本
        self.sources("报销流程")

        self.assertNotIn("旧答案", pipeline_mod._qa_cache)


class CreateDomainReconcileTests(_RetrievalCase):
    def ingest_finance_folder(self) -> None:
        self.ingest([self.write_note("报销流程：先填申请单。", "a.md", "finance")])
        self.assertEqual(self.retrieve_hits("报销流程"), [("a.md", "reference")])

    def test_new_domain_for_existing_folder_fixes_cards(self):
        """③ 用「新增领域」和用「登记为领域」结果必须一致。"""
        self.ingest_finance_folder()
        domains.create_domain("finance", "财务", store=self.store)
        self.assertEqual(self.sources("报销流程", domain="finance"), {"a.md"})
        self.assertEqual(self.sources("报销流程", domain="reference"), set())

    def test_front_matter_declaration_is_respected(self):
        """文件头明确声明别的领域的不能被改；文件头声明了新领域的要改过来。"""
        stays = self.knowledge / "finance" / "stays.md"
        stays.parent.mkdir(parents=True)
        stays.write_text("---\ndomain: learning\n---\n# 甲\n\n正文甲\n", encoding="utf-8")
        declared = self.knowledge / "learning" / "general" / "declared.md"
        declared.write_text("---\ndomain: finance\n---\n# 乙\n\n正文乙\n", encoding="utf-8")
        self.ingest([stays, declared])
        self.assertEqual({d["path"]: d["domain"] for d in self.store.list_documents()},
                         {"finance/stays.md": "learning", "learning/general/declared.md": "learning"})

        domains.create_domain("finance", "财务", store=self.store)

        self.assertEqual({d["path"]: d["domain"] for d in self.store.list_documents()},
                         {"finance/stays.md": "learning", "learning/general/declared.md": "finance"})

    def test_failure_rolls_back_so_it_can_be_retried(self):
        self.ingest_finance_folder()
        with mock.patch.object(self.store, "reassign_domain", side_effect=RuntimeError("Qdrant 断开")):
            with self.assertRaises(domains.DomainError):
                domains.create_domain("finance", "财务", store=self.store)
        self.assertNotIn("finance", domains.all_codes())
        domains.create_domain("finance", "财务", store=self.store)  # 恢复后可以重试
        self.assertEqual(self.sources("报销流程", domain="finance"), {"a.md"})


class FrontMatterDomainUsageTests(_RetrievalCase):
    def test_cards_declaring_a_domain_block_its_deletion(self):
        """④ learning/ 下的文件在文件头声明 domain: finance，卡片属于 finance。"""
        domains.create_domain("finance", "财务")
        path = self.knowledge / "learning" / "general" / "policy.md"
        path.write_text("---\ndomain: finance\n---\n# 报销制度\n\n报销流程说明。\n", encoding="utf-8")
        self.ingest([path])
        self.assertEqual({d["domain"] for d in self.store.list_documents()}, {"finance"})

        usage = domains.usage_by_domain(self.knowledge, self.ledger(), self.store.list_documents())

        self.assertTrue(domains.has_data(usage.get("finance")))
        with self.assertRaises(domains.DomainError):
            domains.delete_domain("finance", usage.get("finance"), self.knowledge)


class McpDomainParameterTests(_RetrievalCase):
    def setUp(self) -> None:
        super().setUp()
        domains.create_domain("finance", "财务")
        self.ingest([self.write_note("报销流程：先填申请单。", "a.md", "finance")])

    def test_names_and_codes_resolve_to_the_code(self):
        """⑤ 改名后 Agent 传新名称也能用；过滤条件永远是编号。"""
        from src.mcp_server import _filters

        for raw, code in (("finance", "finance"), ("FINANCE", "finance"), ("财务", "finance"),
                          ("学习", "learning"), ("Learning", "learning")):
            self.assertEqual(_filters(raw, None)["domain"], code, raw)
        domains.update_domain("finance", name="财务资料")
        self.assertEqual(_filters("财务资料", None)["domain"], "finance")

    def test_all_means_no_domain_filter(self):
        from src.mcp_server import _filters

        self.assertIsNone(_filters("all", None))
        self.assertIsNone(_filters(None, None))

    def test_ambiguous_name_is_not_guessed(self):
        domains.create_domain("finance2", "财务")
        self.assertIsNone(domains.resolve_code("财务"))
        self.assertEqual(domains.resolve_code("finance2"), "finance2")

    def test_unknown_domain_returns_explicit_error_not_empty_result(self):
        from src import mcp_server

        out = json.loads(mcp_server.knowledge_search("报销流程", domain="不存在的领域"))
        self.assertNotIn("matches", out)
        self.assertIn("不存在的领域", out["error"])
        self.assertIn("finance", out["error"])  # 带上可用编号，Agent 能自行纠正
        out = json.loads(mcp_server.knowledge_qa("报销流程", domain="不存在的领域"))
        self.assertIn("finance", out["error"])

    def test_search_by_display_name_finds_the_document(self):
        from src import mcp_server

        with mock.patch.object(mcp_server, "_log_qa"):  # 不写真实 RAG 日志
            out = json.loads(mcp_server.knowledge_search("报销流程", domain="财务"))
        self.assertEqual([m["source"] for m in out["matches"]], ["a.md"])


class WebIngestDomainTests(_RetrievalCase):
    def ingest_page(self, **kwargs) -> dict:
        page = {"text": "报销流程：先填申请单，再找主管审批。" * 3, "title": "报销制度"}
        with mock.patch("src.web_fetcher.fetch_and_parse", return_value=page), \
             contextlib.redirect_stdout(io.StringIO()):
            return ingest_web_page("https://example.com/policy", **kwargs)

    def test_selected_domain_and_category_take_effect(self):
        """⑥ 选了 finance：卡片属于 finance、分类生效，按 finance 能检索到。"""
        domains.create_domain("finance", "财务")
        self.ingest_page(domain="finance", category="policies")
        [doc] = self.store.list_documents()
        self.assertEqual((doc["domain"], doc["category"]), ("finance", "policies"))
        self.assertEqual(self.sources("报销流程", domain="finance"), {doc["source"]})
        self.assertEqual(self.sources("报销流程", domain="reference"), set())

    def test_default_snapshot_is_byte_compatible_with_old_ones(self):
        result = self.ingest_page()
        text = Path(result["file_path"]).read_text(encoding="utf-8")
        self.assertNotIn("\ndomain:", text)    # 不写默认值：存量网页重抓不会无故升版本
        self.assertNotIn("\ncategory:", text)
        self.assertTrue(self.ingest_page()["skipped"])

    def test_switching_domain_versions_the_same_document(self):
        """换领域重抓：同一文档的新版本，而不是两份同时生效。"""
        domains.create_domain("finance", "财务")
        self.ingest_page()
        self.ingest_page(domain="finance")
        docs = self.store.list_documents()
        self.assertEqual(len({d["document_id"] for d in docs}), 1)
        self.assertEqual({(d["domain"], d["status"]) for d in docs},
                         {("reference", "expired"), ("finance", "active")})
        self.assertEqual(self.sources("报销流程", domain="reference"), set())

    def test_hostile_category_does_not_break_front_matter(self):
        self.ingest_page(category="a: b #c")
        [doc] = self.store.list_documents()
        self.assertEqual(doc["category"], "a: b #c")

    def test_unknown_disabled_and_archive_domains_rejected(self):
        domains.create_domain("finance", "财务")
        domains.update_domain("finance", enabled=False)
        for bad in ("ghost", "finance", "archive"):
            with self.assertRaises(ManageError, msg=bad):
                self.ingest_page(domain=bad)


class ArchiveDomainFilterTests(_RetrievalCase):
    """⑦ 选了归档领域却默认只查 active：归档卡片的状态都是 archive，结果永远为空。"""

    def setUp(self) -> None:
        super().setUp()
        self.ingest([self.write_note("旧制度：已废止的报销规定。", "old.md", "archive/learning/general")])

    def test_archive_domain_returns_archived_documents(self):
        self.assertEqual(self.sources("旧制度", domain="archive"), {"old.md"})

    def test_default_search_still_excludes_archived(self):
        self.assertEqual(self.sources("旧制度"), set())

    def test_explicit_status_still_wins(self):
        self.assertEqual(effective_filters({"domain": "archive", "status": "active"}),
                         {"status": "active", "domain": "archive"})
        self.assertEqual(effective_filters({"domain": "archive", "status": "all"}), {"domain": "archive"})
        self.assertEqual(effective_filters({"domain": "learning"}),
                         {"status": "active", "domain": "learning"})


class FilterRelaxationTests(_RetrievalCase):
    """⑧ 大模型猜错过滤条件时的放宽重试：对登录员工（必带权限域）和手动选了领域时也要生效。"""

    def setUp(self) -> None:
        super().setUp()
        self.ingest([self.write_note("报销流程：先填申请单。", "open.md"),
                     self.write_note("Rust 所有权规则说明。", "rust.md")])
        hr_only = self.knowledge / "learning" / "general" / "hr.md"
        hr_only.write_text("---\nallowed_groups: [hr]\n---\n# 薪酬\n\n报销流程之薪酬专项。\n", encoding="utf-8")
        self.ingest([hr_only])

    def stream(self, **kwargs) -> set[str]:
        qu = QueryUnderstanding(original="报销流程", vector_query="报销流程", ok=True,
                                filters={"source": "大模型猜错的文件名.md"})
        with mock.patch("src.pipeline.understand_query", return_value=qu), \
             mock.patch("src.pipeline.get_llm_client"), contextlib.redirect_stdout(io.StringIO()):
            result, _deltas = pipeline_mod.answer_stream("报销流程", top_k=5, use_rerank=False,
                                                         config=self.cfg, **kwargs)
        return {item.metadata["source"] for item in result.retrieved}

    def test_personal_mode_relaxes(self):
        self.assertIn("open.md", self.stream())

    def test_logged_in_employee_relaxes(self):
        self.assertIn("open.md", self.stream(user_groups=["develop"]))

    def test_relaxation_keeps_permission_scope(self):
        hits = self.stream(user_groups=["develop"])
        self.assertNotIn("hr.md", hits)
        self.assertIn("hr.md", self.stream(user_groups=["hr"]))

    def test_relaxation_keeps_manual_domain_filter(self):
        self.assertIn("open.md", self.stream(filters={"domain": "learning"}))
        self.assertEqual(self.stream(filters={"domain": "work"}), set())


class CrossVersionContextTests(unittest.TestCase):
    """⑨ 同时召回同一文档的新旧版本时（检索范围选「全部」），不能把它们当相邻卡片拼成一段。"""

    @staticmethod
    def item(version: str, seq: int, text: str) -> RetrievedItem:
        return RetrievedItem(rank=seq, score=0.9, text=text, metadata={
            "document_id": "doc_1", "source": "a.md", "version": version,
            "chunk_id": f"doc_1_v{version}_{seq:04d}"})

    def test_adjacent_merge_does_not_cross_versions(self):
        context, used, _dropped, notes = build_context(
            [self.item("1.0", 3, "旧版：报销上限五百元。"), self.item("1.1", 4, "新版：报销上限八百元。")])
        self.assertEqual(notes, [])
        self.assertEqual(len(used), 2)

    def test_adjacent_merge_within_one_version_still_works(self):
        _context, used, _dropped, notes = build_context(
            [self.item("1.1", 3, "第三段。"), self.item("1.1", 4, "第四段。")])
        self.assertEqual(len(notes), 1)
        self.assertEqual(len(used), 1)

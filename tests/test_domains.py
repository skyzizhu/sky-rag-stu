"""领域管理：注册表规则 + 「改名不影响检索」回归 + 未登记目录登记 + 恢复拦截。

重点（用户明确要求）：改领域显示名称，不得影响检索——卡片元数据、向量、过滤条件、
检索结果、入库台账都必须原封不动。这里用真实检索流水线（向量 + BM25 混合 + 领域过滤）
在改名前后各跑一遍对比，并对向量库做全量快照比对。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from src import auth_db, domains
from src.domains import DomainError
from tests._helpers import IngestHarness


def note(body: str) -> str:
    return f"# 笔记\n\n{body}\n"


class _RegistryCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.kdir = self.root / "knowledge"
        self.kdir.mkdir()
        patcher = mock.patch.object(domains, "REGISTRY_PATH", self.root / "domains.json")
        patcher.start()
        self.addCleanup(patcher.stop)


class RegistryRuleTests(_RegistryCase):
    def test_defaults_to_builtins_without_writing_a_file(self):
        self.assertEqual(domains.all_codes(), list(domains.BUILTIN_CODES))
        self.assertFalse(domains.REGISTRY_PATH.exists())  # 存量部署零迁移

    def test_code_validation(self):
        for bad in ("Finance", "1finance", "a", "x" * 33, "fin ance", "财务", "all"):
            with self.assertRaises(DomainError, msg=bad):
                domains.create_domain(bad, "名称")
        with self.assertRaises(DomainError):
            domains.create_domain("learning", "重复")
        with self.assertRaises(DomainError):
            domains.create_domain("finance", "")  # 名称必填
        with self.assertRaises(DomainError):
            domains.create_domain("finance", "名" * 21)
        created = domains.create_domain("finance", "财务", "财务制度与报销")
        self.assertEqual((created["code"], created["name"]), ("finance", "财务"))
        self.assertIn("finance", domains.enabled_codes())

    def test_rename_disable_and_reserved_protection(self):
        domains.create_domain("finance", "财务")
        domains.update_domain("finance", name="财务资料")
        self.assertEqual(domains.display_name("finance"), "财务资料")
        with self.assertRaises(DomainError):
            domains.update_domain("finance", name="  ")  # 自定义领域名称不能清空
        domains.update_domain("learning", name="学习资料")
        domains.update_domain("learning", name="")  # 内置领域清空 = 恢复默认多语言标签
        self.assertEqual(domains.display_name("learning"), "")

        domains.update_domain("finance", enabled=False)
        self.assertNotIn("finance", domains.enabled_codes())
        self.assertIn("finance", domains.all_codes())  # 停用后数据层仍认它
        for reserved in ("archive", "reference"):
            with self.assertRaises(DomainError):
                domains.update_domain(reserved, enabled=False)

    def test_delete_only_empty_non_reserved(self):
        domains.create_domain("finance", "财务")
        with self.assertRaises(DomainError):
            domains.delete_domain("finance", {"files": 1}, self.kdir)
        with self.assertRaises(DomainError):
            domains.delete_domain("finance", {"archived": 2}, self.kdir)  # 归档出去的也算
        with self.assertRaises(DomainError):
            domains.delete_domain("archive", None, self.kdir)
        folder = self.kdir / "finance" / "sub"
        folder.mkdir(parents=True)
        (folder / ".DS_Store").write_text("x")
        domains.delete_domain("finance", {"files": 0, "cards": 0}, self.kdir)
        self.assertNotIn("finance", domains.all_codes())
        self.assertFalse((self.kdir / "finance").exists())  # 空目录树连占位文件一并清掉

    def test_delete_never_removes_real_files(self):
        domains.create_domain("finance", "财务")
        keep = self.kdir / "finance" / ".private-notes"
        keep.parent.mkdir(parents=True)
        keep.write_text("用户自己的隐藏文件")
        domains.delete_domain("finance", None, self.kdir)
        self.assertTrue(keep.exists())

    def test_usage_counts_every_source(self):
        for rel in ("finance/a.md", "finance/sub/b.md", "archive/finance/c.md", "root.md",
                    "Finance/upper.md"):
            p = self.kdir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        usage = domains.usage_by_domain(
            self.kdir, {"finance/a.md": {}, "archive/finance/c.md": {}},
            [{"path": "finance/a.md", "domain": "finance", "chunks": 3},
             {"path": "finance/a.md", "domain": "finance", "chunks": 2},
             {"path": "archive/finance/c.md", "domain": "archive", "chunks": 1}])
        stat = usage["finance"]
        self.assertGreaterEqual(stat["files"], 2)  # 大小写不敏感的文件系统上 Finance 即 finance
        self.assertEqual(stat["archived"], 1)
        self.assertEqual(stat["ledger"], 2)
        self.assertEqual(stat["documents"], 3)
        self.assertEqual(stat["cards"], 6)
        self.assertTrue(domains.has_data(stat))
        self.assertFalse(domains.has_data(usage.get("work")))

    def test_saved_filter_choice_falls_back_when_domain_removed(self):
        domains.create_domain("finance", "财务")
        self.assertEqual(domains.sanitize_choice("finance"), "finance")
        domains.delete_domain("finance", None)
        self.assertEqual(domains.sanitize_choice("finance"), "all")
        self.assertEqual(domains.sanitize_choice("all"), "all")

    def test_corrupted_registry_keeps_reserved_domains(self):
        domains.REGISTRY_PATH.write_text('{"domains": [{"code": "work"}]}', encoding="utf-8")
        self.assertEqual(set(domains.all_codes()), {"work", "archive", "reference"})
        domains.REGISTRY_PATH.write_text("not json", encoding="utf-8")
        self.assertEqual(domains.all_codes(), list(domains.BUILTIN_CODES))

    def test_unregistered_dirs(self):
        for rel in ("finance/a.md", "Bad Name/b.md", "learning/c.md"):
            p = self.kdir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        (self.kdir / "empty").mkdir()
        found = {u["folder"]: u for u in domains.unregistered_dirs(self.kdir)}
        self.assertEqual(set(found), {"finance", "Bad Name"})
        self.assertTrue(found["finance"]["valid_code"])
        self.assertFalse(found["Bad Name"]["valid_code"])

    def test_new_front_permission_is_whitelisted(self):
        self.assertIn("domains", auth_db.FRONT_PERMISSIONS)
        self.assertEqual(auth_db._valid_permissions(["domains", "superpower"]), ["domains"])


class RenameDoesNotAffectRetrievalTests(IngestHarness):
    """用户的核心要求：改显示名称，检索的元数据 / 过滤 / 结果 / 向量全部不变。"""

    def setUp(self) -> None:
        super().setUp()
        self.use_fakes()
        domains.create_domain("finance", "财务")
        # finance 下要有足够多的文档：语料太小时 BM25 词项 IDF ≤ 0、不返回结果，关键词通道就没参与
        self.ingest([self.write_note("报销流程：先填申请单，再找主管审批。", "reimburse.md", "finance"),
                     self.write_note("差旅标准：一线城市住宿上限 500 元。", "travel.md", "finance"),
                     self.write_note("发票规范：抬头必须与公司名称一致。", "invoice.md", "finance"),
                     self.write_note("预算编制：每季度滚动调整一次。", "budget.md", "finance"),
                     self.write_note("Python 装饰器的原理与用法。", "python.md", "learning")])

    def retrieve(self, domain: str | None) -> list[tuple[str, str]]:
        return self.retrieve_hits("报销 差旅 住宿 审批", domain=domain, top_k=5)

    def retrieve_detail(self, domain: str) -> list[tuple]:
        """逐条的排名、分数、命中通道：改名前后必须完全一致。"""
        import contextlib
        import io

        from src.pipeline import answer_question

        with contextlib.redirect_stdout(io.StringIO()):
            result = answer_question("报销 差旅 住宿 审批", top_k=5, use_llm=False,
                                     filters={"domain": domain}, use_query_understanding=False,
                                     use_hybrid=True, use_rerank=False, config=self.cfg)
        return [(s["rank"], s["source"], s["domain"], s["channels"], round(float(s["score"]), 6))
                for s in result.sources]

    def snapshot(self) -> list:
        """向量库全量快照：每张卡片的 ID、全部元数据、向量。"""
        points, _ = self.store.client.scroll(self.cfg.qdrant_collection, limit=10_000,
                                             with_payload=True, with_vectors=True)
        return sorted((str(p.id), json.dumps(p.payload, sort_keys=True, ensure_ascii=False),
                       tuple(p.vector)) for p in points)

    def test_cards_carry_code_never_name(self):
        payloads = [json.loads(s[1]) for s in self.snapshot()]
        finance = [p for p in payloads if p["path"].startswith("finance/")]
        self.assertTrue(finance)
        self.assertTrue(all(p["domain"] == "finance" for p in finance))
        self.assertFalse(any("财务" in json.dumps(p, ensure_ascii=False) for p in payloads))

    def test_rename_changes_nothing_in_retrieval(self):
        before_hits = self.retrieve("finance")
        before_detail = self.retrieve_detail("finance")
        before_snapshot = self.snapshot()
        before_ledger = self.ledger()
        self.assertEqual(before_hits, [("budget.md", "finance"), ("invoice.md", "finance"),
                                       ("reimburse.md", "finance"), ("travel.md", "finance")])
        self.assertTrue(any("关键词" in row[3] for row in before_detail), "关键词通道必须参与")

        domains.update_domain("finance", name="财务与报销资料", description="改个名字试试")

        self.assertEqual(self.retrieve("finance"), before_hits)        # 过滤结果不变
        self.assertEqual(self.retrieve_detail("finance"), before_detail)  # 排名、分数、命中通道不变
        self.assertEqual(self.snapshot(), before_snapshot)             # 卡片 ID / 元数据 / 向量一字不差
        self.assertEqual(self.ledger(), before_ledger)                 # 台账不变
        summary = self.ingest()                                        # 全库扫描：改名不触发重新入库
        self.assertEqual(summary.new_files + summary.updated_files, [])
        self.assertEqual(self.snapshot(), before_snapshot)

    def test_disable_keeps_existing_knowledge_searchable(self):
        before = self.retrieve("finance")
        domains.update_domain("finance", enabled=False)
        self.assertEqual(self.retrieve("finance"), before)
        # 停用后已有文件改内容重新入库：领域仍是 finance（不会被兜底成 reference）
        path = self.write_note("报销流程（修订版）：线上提交即可。", "reimburse.md", "finance")
        self.ingest([path])
        payloads = [json.loads(s[1]) for s in self.snapshot()]
        self.assertTrue(all(p["domain"] == "finance" for p in payloads
                            if p["path"] == "finance/reimburse.md"))

    def test_unfiltered_and_other_domains_unaffected(self):
        before_all, before_learning = self.retrieve(None), self.retrieve("learning")
        domains.update_domain("finance", name="新名字")
        self.assertEqual(self.retrieve(None), before_all)
        self.assertEqual(self.retrieve("learning"), before_learning)


class PromptsUseCodesOnlyTests(_RegistryCase):
    def test_query_understanding_prompt_lists_codes_not_names(self):
        from src.query_understanding import understand_query

        domains.create_domain("finance", "财务专属名称")

        class _FailingLLM:
            def __getattr__(self, _name):
                def _fail(*_a, **_k):
                    raise RuntimeError("测试：不真正调用大模型")
                return _fail

        qu = understand_query("报销流程是什么", llm=_FailingLLM(), categories=["general"])
        self.assertIn("finance", qu.user_prompt)
        self.assertNotIn("财务专属名称", qu.user_prompt)

    def test_mcp_tool_description_lists_codes_not_names(self):
        from src.mcp_server import _with_domain_codes

        domains.create_domain("finance", "财务专属名称")

        def fake_tool():
            """domain: 可选（{DOMAIN_CODES}）"""

        doc = _with_domain_codes(fake_tool).__doc__
        self.assertIn("finance", doc)
        self.assertNotIn("财务专属名称", doc)


class RegisterDirectoryTests(IngestHarness):
    def test_registering_fixes_domain_in_place_without_reembedding(self):
        # 未登记时入库：兜底成 reference，按 finance 过滤检索不到
        self.ingest([self.write_note("报销流程", "a.md", "finance")])
        self.ingest([self.write_note("旧制度（已归档）", "old.md", "archive/finance")])
        docs = self.store.list_documents()
        self.assertEqual({d["path"]: d["domain"] for d in docs},
                         {"finance/a.md": "reference", "archive/finance/old.md": "archive"})
        before_points = self.store.count()

        domains.register_directory("finance", "财务", self.store)

        after = {d["path"]: d["domain"] for d in self.store.list_documents()}
        self.assertEqual(after["finance/a.md"], "finance")
        self.assertEqual(after["archive/finance/old.md"], "archive")  # 归档文档不被改写
        self.assertEqual(self.store.count(), before_points)            # 没有新增或重建卡片
        self.assertIn("finance", domains.all_codes())


class RestoreGuardTests(IngestHarness):
    def test_restore_into_missing_domain_is_blocked(self):
        from src.manage import ManageError, restore

        self.write_note("旧制度", "old.md", "archive/ghost")
        with mock.patch("src.manage.get_config", return_value=self.cfg):
            with self.assertRaisesRegex(ManageError, "已不存在"):
                restore("archive/ghost/old.md")
        self.assertTrue((self.knowledge / "archive/ghost/old.md").exists())  # 文件原地未动


if __name__ == "__main__":
    unittest.main()

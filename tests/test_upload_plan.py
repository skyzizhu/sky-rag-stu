"""上传 / 目录导入的重复文档防护：冲突预检、执行、与入库流水线的端到端集成。"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from src.upload_plan import (COPY, KIND_BATCH_DUPLICATE, KIND_BATCH_SAME_NAME, KIND_DUPLICATE,
                             KIND_NEW, KIND_SAME_NAME, KIND_SAME_PATH, NEW, NEW_VERSION, SKIP,
                             VERSION_OF, content_hash, execute_uploads, plan_uploads,
                             write_directory_import)
from tests._helpers import IngestHarness


def note(body: str) -> bytes:
    return f"# 笔记\n\n{body}\n".encode("utf-8")


class _KnowledgeDirCase(unittest.TestCase):
    """临时知识目录 + 手工构造台账（本身无测试用例，供下面两组复用）。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.kdir = Path(tmp.name)

    def _existing(self, rel: str, data: bytes) -> dict:
        path = self.kdir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {"hash": content_hash(data)}


class PlanTests(_KnowledgeDirCase):
    """规则本体：纯函数，台账用手工构造的 dict。"""

    def test_new_file_needs_no_confirmation(self):
        [d] = plan_uploads([("a.md", note("甲"))], "learning/general", {}, self.kdir)
        self.assertEqual((d.kind, d.action), (KIND_NEW, NEW))
        self.assertFalse(d.needs_confirmation)

    def test_same_content_elsewhere_skips_by_default_but_can_force(self):
        ledger = {"work/pm/old.md": self._existing("work/pm/old.md", note("甲"))}
        [d] = plan_uploads([("a.md", note("甲"))], "learning/general", ledger, self.kdir)
        self.assertEqual((d.kind, d.action), (KIND_DUPLICATE, SKIP))
        self.assertEqual(d.options, [SKIP, NEW])
        self.assertEqual(d.existing, ["work/pm/old.md"])

    def test_same_content_same_path_can_only_skip(self):
        ledger = {"learning/general/a.md": self._existing("learning/general/a.md", note("甲"))}
        [d] = plan_uploads([("a.md", note("甲"))], "learning/general", ledger, self.kdir)
        self.assertEqual((d.kind, d.options), (KIND_DUPLICATE, [SKIP]))

    def test_same_path_changed_content_defaults_to_new_version(self):
        ledger = {"learning/general/a.md": self._existing("learning/general/a.md", note("旧"))}
        [d] = plan_uploads([("a.md", note("新"))], "learning/general", ledger, self.kdir)
        self.assertEqual((d.kind, d.action), (KIND_SAME_PATH, NEW_VERSION))
        self.assertEqual(d.options, [NEW_VERSION, COPY])

    def test_same_name_elsewhere_single_candidate_defaults_to_its_new_version(self):
        ledger = {"work/pm/a.md": self._existing("work/pm/a.md", note("旧"))}
        [d] = plan_uploads([("a.md", note("新"))], "learning/general", ledger, self.kdir)
        self.assertEqual(d.kind, KIND_SAME_NAME)
        self.assertEqual(d.action, VERSION_OF + "work/pm/a.md")
        self.assertIn(NEW, d.options)

    def test_same_name_multiple_candidates_does_not_guess(self):
        ledger = {"work/pm/a.md": self._existing("work/pm/a.md", note("一")),
                  "life/a.md": self._existing("life/a.md", note("二"))}
        [d] = plan_uploads([("a.md", note("新"))], "learning/general", ledger, self.kdir)
        self.assertEqual((d.kind, d.action), (KIND_SAME_NAME, NEW))
        self.assertEqual(len(d.options), 3)

    def test_batch_duplicates_and_same_name_conflicts(self):
        files = [("a.md", note("甲")), ("b.md", note("甲")), ("a.md", note("乙"))]
        first, dup, clash = plan_uploads(files, "learning/general", {}, self.kdir)
        self.assertEqual(first.kind, KIND_NEW)
        self.assertEqual((dup.kind, dup.action), (KIND_BATCH_DUPLICATE, SKIP))
        self.assertEqual((clash.kind, clash.action), (KIND_BATCH_SAME_NAME, COPY))


class ExecuteTests(_KnowledgeDirCase):
    def test_actions_write_to_the_right_places(self):
        ledger = {
            "learning/general/same.md": self._existing("learning/general/same.md", note("旧")),
            "work/pm/far.md": self._existing("work/pm/far.md", note("远处旧版")),
            "learning/general/dup.md": self._existing("learning/general/dup.md", note("重复")),
        }
        files = [("same.md", note("新")), ("far.md", note("远处新版")), ("dup.md", note("重复")),
                 ("fresh.md", note("全新"))]
        plan = plan_uploads(files, "learning/general", ledger, self.kdir)
        to_ingest, skipped = execute_uploads(plan, files, self.kdir)
        rels = sorted(p.relative_to(self.kdir).as_posix() for p in to_ingest)
        self.assertEqual(rels, ["learning/general/fresh.md", "learning/general/same.md",
                                "work/pm/far.md"])
        self.assertEqual([d.name for d in skipped], ["dup.md"])
        self.assertEqual((self.kdir / "work/pm/far.md").read_bytes(), note("远处新版"))
        self.assertFalse((self.kdir / "learning/general/far.md").exists())

    def test_copy_choice_keeps_original(self):
        ledger = {"learning/general/a.md": self._existing("learning/general/a.md", note("旧"))}
        files = [("a.md", note("新"))]
        plan = plan_uploads(files, "learning/general", ledger, self.kdir)
        [path], _ = execute_uploads(plan, files, self.kdir, choices={0: COPY})
        self.assertEqual(path.name, "a-2.md")
        self.assertEqual((self.kdir / "learning/general/a.md").read_bytes(), note("旧"))

    def test_forged_choice_falls_back_to_default(self):
        files = [("a.md", note("甲"))]
        plan = plan_uploads(files, "learning/general", {}, self.kdir)
        [path], _ = execute_uploads(plan, files, self.kdir,
                                    choices={0: VERSION_OF + "../../etc/passwd"})
        self.assertEqual(path.relative_to(self.kdir).as_posix(), "learning/general/a.md")


class DirectoryWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.domain = Path(tmp.name) / "learning"
        self.domain.mkdir()

    def test_merge_rewrites_only_changed_and_keeps_removed(self):
        root = self.domain / "proj"
        root.mkdir()
        (root / "same.md").write_bytes(note("不变"))
        (root / "changed.md").write_bytes(note("旧"))
        (root / "removed_from_source.md").write_bytes(note("源目录里已删除"))
        old = time.time() - 3600
        os.utime(root / "same.md", (old, old))

        saved, roots = write_directory_import(
            {"proj": [(("same.md",), note("不变")), (("changed.md",), note("新")),
                      (("sub", "new.md"), note("新增"))]},
            self.domain, merge=True)
        self.assertEqual(roots, ["proj"])
        self.assertEqual(len(saved), 3)
        self.assertAlmostEqual((root / "same.md").stat().st_mtime, old, delta=1)  # 未重写
        self.assertEqual((root / "changed.md").read_bytes(), note("新"))
        self.assertTrue((root / "sub" / "new.md").exists())
        self.assertTrue((root / "removed_from_source.md").exists())  # 默认保留
        self.assertFalse((self.domain / "proj-2").exists())

    def test_copy_mode_creates_numbered_directory(self):
        (self.domain / "proj").mkdir()
        _saved, roots = write_directory_import({"proj": [(("a.md",), note("甲"))]},
                                               self.domain, merge=False)
        self.assertEqual(roots, ["proj-2"])


class UploadIngestIntegrationTests(IngestHarness):
    """端到端：上传 → 预检 → 落盘 → 入库，验证不再产生重复文档。"""

    def upload(self, files, target_dir="learning/general", choices=None):
        plan = plan_uploads(files, target_dir, self.ledger(), self.knowledge)
        to_ingest, skipped = execute_uploads(plan, files, self.knowledge, choices)
        summary = self.ingest(to_ingest, skip_unchanged=True) if to_ingest else None
        return plan, summary, skipped

    def test_changed_file_becomes_new_version_not_duplicate(self):
        self.upload([("a.md", note("第一版"))])
        plan, summary, _ = self.upload([("a.md", note("第二版"))])
        self.assertEqual(plan[0].action, NEW_VERSION)
        self.assertEqual(summary.updated_files, ["learning/general/a.md"])
        self.assertEqual(self.versions(), {"1.0": "expired", "1.1": "active"})
        self.assertFalse((self.knowledge / "learning/general/a-2.md").exists())

    def test_identical_reupload_is_skipped_without_ingest(self):
        self.upload([("a.md", note("内容"))])
        before = self.store.count()
        plan, summary, skipped = self.upload([("a.md", note("内容"))])
        self.assertIsNone(summary)
        self.assertEqual([d.name for d in skipped], ["a.md"])
        self.assertEqual(self.store.count(), before)

    def test_same_content_under_another_name_is_skipped(self):
        self.upload([("a.md", note("同一份内容"))])
        _plan, summary, skipped = self.upload([("renamed.md", note("同一份内容"))],
                                              target_dir="work/pm")
        self.assertIsNone(summary)
        self.assertEqual(len([d for d in self.store.list_documents()]), 1)

    def test_same_name_in_other_folder_versions_that_document(self):
        self.upload([("a.md", note("旧版"))], target_dir="work/pm")
        plan, summary, _ = self.upload([("a.md", note("新版"))])  # 表单选的是 learning/general
        self.assertEqual(plan[0].action, VERSION_OF + "work/pm/a.md")
        self.assertEqual(summary.updated_files, ["work/pm/a.md"])
        self.assertEqual(self.versions("work/pm/a.md"), {"1.0": "expired", "1.1": "active"})
        self.assertFalse((self.knowledge / "learning/general/a.md").exists())

    def test_directory_reimport_merges_incrementally(self):
        domain = self.knowledge / "learning"
        first = {"proj": [(("a.md",), note("A1")), (("b.md",), note("B")), (("d.md",), note("D"))]}
        saved, _ = write_directory_import(first, domain, merge=True)
        self.ingest(saved, skip_unchanged=True)

        second = {"proj": [(("a.md",), note("A2")), (("b.md",), note("B")), (("c.md",), note("C"))]}
        saved, roots = write_directory_import(second, domain, merge=True)
        summary = self.ingest(saved, skip_unchanged=True)

        self.assertEqual(roots, ["proj"])
        self.assertEqual(summary.updated_files, ["learning/proj/a.md"])
        self.assertEqual(summary.skipped_files, ["learning/proj/b.md"])
        self.assertEqual(summary.new_files, ["learning/proj/c.md"])
        active = {d["path"] for d in self.store.list_documents() if d["status"] == "active"}
        self.assertEqual(active, {"learning/proj/a.md", "learning/proj/b.md",
                                  "learning/proj/c.md", "learning/proj/d.md"})  # d 保留
        self.assertFalse((domain / "proj-2").exists())


if __name__ == "__main__":
    unittest.main()

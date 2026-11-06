"""部门权限规则：纯函数 + 内存 Qdrant 端到端检索隔离。"""

from __future__ import annotations

import unittest

from qdrant_client import QdrantClient, models

from src.permissions import scope_filters, visible_groups, write_group_options
from src.vector_store import VectorStore


class VisibleGroupsTests(unittest.TestCase):
    def test_department_member_sees_own_department(self):
        self.assertEqual(visible_groups("hr"), ["hr"])

    def test_no_department_falls_back_to_all_only(self):
        self.assertEqual(visible_groups(None), ["all"])
        self.assertEqual(visible_groups(""), ["all"])


class ScopeFiltersTests(unittest.TestCase):
    def test_none_means_unrestricted_and_keeps_filters_untouched(self):
        self.assertIsNone(scope_filters(None, None))
        original = {"domain": "work"}
        self.assertIs(scope_filters(original, None), original)

    def test_injects_groups_plus_all_and_keeps_other_filters(self):
        out = scope_filters({"domain": "work"}, ["hr"])
        self.assertEqual(out, {"domain": "work", "allowed_groups": ["all", "hr"]})

    def test_caller_cannot_forge_wider_scope(self):
        # 调用方伪造 allowed_groups=["develop"]：注入后必须被覆盖为本人可见域
        out = scope_filters({"allowed_groups": ["develop"]}, ["hr"])
        self.assertEqual(out["allowed_groups"], ["all", "hr"])

    def test_does_not_mutate_input(self):
        original = {"domain": "work"}
        scope_filters(original, ["hr"])
        self.assertEqual(original, {"domain": "work"})


class WriteGroupOptionsTests(unittest.TestCase):
    def test_employee_can_only_pick_all_or_own_department(self):
        self.assertEqual(write_group_options("hr"), ["all", "hr"])

    def test_no_department_can_only_pick_all(self):
        self.assertEqual(write_group_options(None), ["all"])

    def test_editing_keeps_existing_wider_scope_without_adding_others(self):
        # 管理员此前设成 develop 可见：hr 员工编辑时能保留，但选项里不会冒出别的部门
        self.assertEqual(write_group_options("hr", current=["develop"]),
                         ["all", "hr", "develop"])

    def test_deduplicates(self):
        self.assertEqual(write_group_options("hr", current=["hr", "all", ""]), ["all", "hr"])


class RetrievalIsolationTests(unittest.TestCase):
    """端到端：真实 qdrant-client 过滤语义下，develop 员工永远拿不到 hr 专属卡片。"""

    @classmethod
    def setUpClass(cls):
        cls.client = QdrantClient(":memory:")
        cls.client.create_collection(
            "kb", vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
        cls.client.upsert("kb", points=[
            models.PointStruct(id=1, vector=[1.0, 0.0], payload={"name": "hr-only", "allowed_groups": ["hr"]}),
            models.PointStruct(id=2, vector=[1.0, 0.1], payload={"name": "dev-only", "allowed_groups": ["develop"]}),
            models.PointStruct(id=3, vector=[1.0, 0.2], payload={"name": "everyone", "allowed_groups": ["all"]}),
            models.PointStruct(id=4, vector=[1.0, 0.3], payload={"name": "hr+dev", "allowed_groups": ["hr", "develop"]}),
        ], wait=True)

    def _visible_names(self, filters: dict | None) -> set[str]:
        result = self.client.query_points(
            "kb", query=[1.0, 0.0], query_filter=VectorStore.build_filter(filters), limit=10)
        return {p.payload["name"] for p in result.points}

    def test_develop_member_sees_develop_and_all_never_hr(self):
        names = self._visible_names(scope_filters({}, visible_groups("develop")))
        self.assertEqual(names, {"dev-only", "everyone", "hr+dev"})
        self.assertNotIn("hr-only", names)

    def test_forged_filter_still_cannot_reach_hr(self):
        names = self._visible_names(scope_filters({"allowed_groups": ["hr"]},
                                                  visible_groups("develop")))
        self.assertNotIn("hr-only", names)

    def test_no_department_sees_only_all(self):
        names = self._visible_names(scope_filters({}, visible_groups(None)))
        self.assertEqual(names, {"everyone"})

    def test_unrestricted_personal_mode_sees_everything(self):
        self.assertEqual(len(self._visible_names(scope_filters(None, None))), 4)


if __name__ == "__main__":
    unittest.main()

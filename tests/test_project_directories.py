"""Project path allocation and metadata with temporary SQLite and disk."""
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import auth_db, projects
from src.project_directories import INFO_NAME, is_generated_info, other_project_paths, preview_directory, upload_directory
from src.upload_plan import NEW, VERSION_OF, plan_uploads, write_directory_import
from tests._helpers import TempAuthDB


class ProjectDirectoryTests(TempAuthDB):
    def setUp(self):
        super().setUp()
        self.knowledge = auth_db.AUTH_DIR / "knowledge"

    def project(self, name, note=""):
        return projects.save_project(projects.ProjectFields(name, None, None, [], note))

    def test_lazy_creation_collision_and_rename(self):
        first, second = self.project("低空"), self.project("地空")
        path = preview_directory(self.knowledge, "projects", first["id"])
        self.assertEqual(path.name, "dikong")
        with upload_directory(self.knowledge, "projects", first["id"]):
            pass
        self.assertFalse(self.knowledge.exists())
        with upload_directory(self.knowledge, "projects", first["id"]) as directory:
            directory.mkdir(parents=True)
            (directory / "user.md").write_text("user text", encoding="utf-8")
        self.assertTrue(is_generated_info(path / INFO_NAME))
        self.assertEqual(preview_directory(self.knowledge, "projects", second["id"]).name, "dikong-1")
        self.assertEqual(preview_directory(self.knowledge, "learning", second["id"]).name, "dikong")
        with mock.patch("src.config.get_config", return_value=SimpleNamespace(knowledge_dir=self.knowledge)):
            projects.save_project(projects.ProjectFields("新名称", None, None, [], "更新说明"),
                                  project_id=first["id"], revision=first["revision"])
        self.assertEqual(preview_directory(self.knowledge, "projects", first["id"]), path)
        self.assertIn("更新说明", (path / INFO_NAME).read_text(encoding="utf-8"))

    def test_directory_import_preserves_legacy_path_and_separates_unassigned_uploads(self):
        project_id = self.project("验收")["id"]
        legacy = Path("learning", "folder", "old.md")
        old = self.knowledge / legacy
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old")
        with upload_directory(self.knowledge, "learning", project_id) as directory:
            saved, _ = write_directory_import(
                {"folder": [(("old.md",), b"updated"), (("nested", "new.md"), b"new")]},
                directory, merge=True, knowledge_dir=self.knowledge, legacy_paths={legacy})
        self.assertEqual(saved, [old, directory / "folder" / "nested" / "new.md"])
        self.assertEqual(old.read_bytes(), b"updated")
        with upload_directory(self.knowledge, "learning", None) as public:
            write_directory_import({directory.name: [(("unassigned.md",), b"unassigned")]},
                                   public, merge=True, knowledge_dir=self.knowledge)
        self.assertEqual(public.name, "public")
        self.assertTrue((public / directory.name / "unassigned.md").is_file())
        info = public / INFO_NAME
        self.assertTrue(is_generated_info(info))
        self.assertIn("本目录存放入库时未选择项目的资料", info.read_text(encoding="utf-8"))

    def test_public_is_reserved_for_unassigned_files(self):
        project = self.project("Public")
        public = preview_directory(self.knowledge, "learning", None)
        self.assertEqual(public.name, "public")
        self.assertEqual(preview_directory(self.knowledge, "learning", project["id"]).name, "public-1")
        with upload_directory(self.knowledge, "learning", None, expected=public):
            pass
        self.assertFalse(public.exists())
        with self.assertRaisesRegex(ValueError, "通过知识库管理归档"):
            preview_directory(self.knowledge, "archive", None)

    def test_old_project_named_public_keeps_its_directory(self):
        project = self.project("Public")
        with closing(auth_db._connect()) as conn, conn:
            conn.execute("INSERT INTO project_directories VALUES (?, ?, ?)",
                         ("learning", project["id"], "public"))
        self.assertEqual(preview_directory(self.knowledge, "learning", project["id"]).name, "public")
        self.assertEqual(preview_directory(self.knowledge, "learning", None).name, "public-unassigned")

    def test_project_info_marker_preserves_ordinary_markdown(self):
        public = self.knowledge / "learning" / "public"
        public.mkdir(parents=True)
        info = public / INFO_NAME
        info.write_text("# A user's Markdown file", encoding="utf-8")
        with upload_directory(self.knowledge, "learning", None):
            pass
        self.assertFalse(is_generated_info(info))
        self.assertEqual(info.read_text(encoding="utf-8"), "# A user's Markdown file")

    def test_same_name_version_only_targets_the_selected_project(self):
        files = [("report.md", b"new version")]
        own = "learning/legacy/report.md"
        other = "learning/other/report.md"
        ledger = {own: {"project_id": 1}, other: {"project_id": 2}}
        decision, = plan_uploads(files, "learning/newdir", ledger, self.knowledge,
                                 blocked_paths=other_project_paths(ledger, 1))
        self.assertEqual(decision.options, [VERSION_OF + own, NEW])

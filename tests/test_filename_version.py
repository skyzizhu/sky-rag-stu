"""文件名版本识别及入库版本回归：仅使用临时文件和内存向量库。"""
import unittest

from src import index_state
from src.metadata import build_document_metadata, bump_version, version_from_filename
from tests._helpers import IngestHarness


class FilenameVersionTests(unittest.TestCase):
    def test_explicit_version_markers(self):
        for name, expected in {
            '产品说明_v1.2.pdf': '1.2',
            '需求文档V2.0.1.docx': '2.0.1',
            '流程（版本1.3）.md': '1.3',
            '流程_版本号：2.4.xlsx': '2.4',
            'guide_version-3.2.pptx': '3.2',
            '说明_V2.pdf': '2.0',
            '说明_v1.10.pdf': '1.10',
            '说明_v1.2_20261118.pdf': '1.2',
            '说明_v1.2_副本-2.pdf': '1.2',
            '说明_v1.2_版本1.2.md': '1.2',
        }.items():
            with self.subTest(name=name):
                self.assertEqual(version_from_filename(name), expected)

    def test_numbers_dates_and_ambiguous_versions_are_not_guessed(self):
        for name in ['说明_20261118.pdf', '说明_2026.11.18.pdf', '说明_1.2.pdf',
                     '说明_第2章.docx', 'rev1.2.md', '说明_v1.2beta.md',
                     '说明_v1.2-rc1.md', '说明_v1.2.3.4.5.md', '说明_v20261118.md',
                     '说明_v1.2_v2.0.md', '说明_v1..2.md']:
            with self.subTest(name=name):
                self.assertIsNone(version_from_filename(name))

    def test_metadata_filename_precedes_front_matter_for_all_file_types(self):
        for extension in ['md', 'pdf', 'docx', 'xlsx', 'pptx']:
            name = '说明_v2.1.' + extension
            result = build_document_metadata('work/general/' + name, name, extension,
                                             None, None, None, {'version': '9.0'})
            self.assertEqual(result['version'], '2.1')

    def test_metadata_without_filename_marker_keeps_declared_or_default_version(self):
        for fm, expected in [({}, '1.0'), ({'version': '3.0'}, '3.0')]:
            result = build_document_metadata('work/general/说明.md', '说明.md', 'md',
                                             None, None, None, fm)
            self.assertEqual(result['version'], expected)

    def test_auto_bump_preserves_old_rule_and_handles_multi_part_versions(self):
        for old, expected in [('1.0', '1.1'), ('1.9', '2.0'), ('2.0.9', '2.0.10'),
                              ('2.0.0.9', '2.0.0.10'), ('bad', '1.1')]:
            self.assertEqual(bump_version(old), expected)


class FilenameVersionIngestTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()

    def ledger_version(self, path):
        return index_state.load_state()[path.relative_to(self.knowledge).as_posix()]['version']

    def test_filename_version_is_persisted_to_cards_and_ledger(self):
        path = self.write_note('内容', '方案_v2.3.md')
        self.ingest([path])
        self.assertEqual(self.ledger_version(path), '2.3')
        self.assertEqual(self.versions(), {'2.3': 'active'})

    def test_filename_declaration_stays_authoritative_after_content_update(self):
        path = self.write_note('原内容', '方案_v2.3.md')
        self.ingest([path])
        self.write_note('新内容', path.name)
        self.ingest([path])
        self.assertEqual(self.ledger_version(path), '2.3')
        self.assertEqual(self.versions(), {'2.3': 'active'})

    def test_unchanged_forced_reingest_keeps_current_version_and_history(self):
        path = self.write_note('第一版')
        self.ingest([path])
        self.write_note('第二版')
        self.ingest([path])
        before = self.store.count()
        for _ in range(2):
            self.ingest([path])
            self.assertEqual(self.ledger_version(path), '1.1')
            self.assertEqual(self.store.count(), before)
            self.assertEqual(self.versions(), {'1.0': 'expired', '1.1': 'active'})

    def test_no_marker_auto_bumps_only_after_content_changes(self):
        path = self.write_note('第一版')
        for body, expected in [('第一版', '1.0'), ('第二版', '1.1'), ('第三版', '1.2')]:
            self.write_note(body)
            self.ingest([path])
            self.assertEqual(self.ledger_version(path), expected)
            self.ingest(skip_unchanged=True)
            self.assertEqual(self.ledger_version(path), expected)

    def test_manual_archive_override_remains_highest_priority(self):
        path = self.write_note('内容', '方案_v2.3.md')
        self.ingest([path])
        state = index_state.load_state()
        state[path.relative_to(self.knowledge).as_posix()]['overrides'] = {'version': '7.0'}
        index_state.save_state(state)
        self.ingest([path])
        self.assertEqual(self.ledger_version(path), '7.0')

    def test_front_matter_remains_fallback_without_filename_marker(self):
        path = self.write_note('内容')
        path.write_text('---\nversion: "3.0"\n---\n# 文档\n原文', encoding='utf-8')
        self.ingest([path])
        self.assertEqual(self.ledger_version(path), '3.0')
        path.write_text('---\nversion: "3.0"\n---\n# 文档\n修改', encoding='utf-8')
        self.ingest([path])
        self.assertEqual(self.ledger_version(path), '3.0')

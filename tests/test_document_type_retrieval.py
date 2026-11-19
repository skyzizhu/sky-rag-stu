"""文档类型过滤在召回、缓存和过滤放宽中保持有效；仅用隔离数据和假模型。"""
import contextlib
import io
import unittest
from unittest import mock

from src import index_epoch, keyword_search, pipeline
from src.manage import update_metadata
from src.query_understanding import QueryUnderstanding
from tests._helpers import IngestHarness


class DocumentTypeRetrievalTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        documents = [
            ('first.md', '一期方案', 'all', '报销流程先填写申请。'),
            ('second.md', '二期方案', 'all', '报销流程先核实项目编号。'),
            ('private.md', '一期方案', 'finance', '报销流程受限审批要求。'),
        ]
        paths = []
        for name, doc_type, group, body in documents:
            path = self.knowledge / 'learning' / 'general' / name
            path.write_text(f'---\ndoc_type: {doc_type}\nallowed_groups: [{group}]\n---\n# 笔记\n\n{body}',
                            encoding='utf-8')
            paths.append(path)
        self.ingest(paths)

    def add_keyword_background(self):
        # BM25 在每个筛选范围内单独建语料，需给两种类型都放入区分性背景卡。
        for doc_type, prefix in [('一期方案', 'phase1'), ('二期方案', 'phase2')]:
            self.ingest([
                self.write_note('Python 装饰器与函数包装。', f'{prefix}_python.md'),
                self.write_note('Rust 所有权与借用。', f'{prefix}_rust.md'),
                self.write_note('Kafka 分区与消息消费。', f'{prefix}_kafka.md'),
                self.write_note('Docker 镜像与容器。', f'{prefix}_docker.md'),
            ], default_doc_type=doc_type)

    def search(self, doc_type, groups=None, hybrid=True):
        with contextlib.redirect_stdout(io.StringIO()):
            return pipeline.answer_question(
                '报销流程', use_llm=False, use_query_understanding=False,
                use_rerank=False, use_hybrid=hybrid, top_k=10,
                filters={'doc_type': doc_type}, user_groups=groups, config=self.cfg)

    def keyword_sources(self, doc_type):
        with contextlib.redirect_stdout(io.StringIO()):
            hits = keyword_search.bm25_search(
                '报销流程', top_k=10, filters={'status': 'active', 'doc_type': doc_type},
                store=self.store, config=self.cfg)
        return {hit['payload']['source'] for hit in hits}

    def test_type_filter_composes_with_permissions_in_both_recall_modes(self):
        for hybrid in (False, True):
            with self.subTest(hybrid=hybrid):
                result = self.search('一期方案', ['hr'], hybrid)
                self.assertEqual({item.metadata['source'] for item in result.retrieved}, {'first.md'})
                self.assertEqual({source['doc_type'] for source in result.sources}, {'一期方案'})
                self.assertEqual(result.filters['doc_type'], '一期方案')
                self.assertIn('all', result.filters['allowed_groups'])
                self.assertNotIn('private.md', str(result.sources))
                self.assertEqual({item.metadata['source'] for item in
                                  self.search('二期方案', ['hr'], hybrid).retrieved}, {'second.md'})

    def test_keyword_cache_is_separate_by_type_and_invalidated_after_edit(self):
        self.add_keyword_background()
        self.assertEqual(self.keyword_sources('一期方案'), {'first.md', 'private.md'})
        self.assertEqual(self.keyword_sources('二期方案'), {'second.md'})
        update_metadata('learning/general/first.md', {'doc_type': '二期方案'})
        self.assertEqual(self.keyword_sources('一期方案'), {'private.md'})
        self.assertEqual(self.keyword_sources('二期方案'), {'first.md', 'second.md'})

    def test_cross_process_epoch_change_refreshes_type_filtered_keyword_cache(self):
        self.add_keyword_background()
        self.assertEqual(self.keyword_sources('一期方案'), {'first.md', 'private.md'})
        selector = self.store.build_filter({'source': 'first.md'})
        from qdrant_client import models
        self.store.client.set_payload(
            collection_name=self.cfg.qdrant_collection, payload={'doc_type': '二期方案'},
            points=models.FilterSelector(filter=selector), wait=True)
        index_epoch.bump()  # 模拟另一个进程编辑；本进程不直接清 BM25 缓存。
        self.assertEqual(self.keyword_sources('一期方案'), {'private.md'})
        self.assertEqual(self.keyword_sources('二期方案'), {'first.md', 'second.md'})

    def test_inferred_filter_relaxation_keeps_manual_type_and_permissions(self):
        qu = QueryUnderstanding(original='报销流程', vector_query='报销流程', ok=True,
                                filters={'source': '不存在的猜测文件.md'})
        with mock.patch('src.pipeline.understand_query', return_value=qu), \
             mock.patch('src.pipeline.get_llm_client'), contextlib.redirect_stdout(io.StringIO()):
            result, deltas = pipeline.answer_stream(
                '报销流程', top_k=10, use_rerank=False, config=self.cfg,
                filters={'doc_type': '一期方案', 'domain': 'learning'}, user_groups=['hr'])
        # 只检查检索半程，不消费生成器或调用云端模型。
        deltas.close()
        self.assertEqual({item.metadata['source'] for item in result.retrieved}, {'first.md'})
        self.assertEqual(result.filters['doc_type'], '一期方案')
        self.assertEqual(result.filters['domain'], 'learning')
        self.assertIn('hr', result.filters['allowed_groups'])


if __name__ == '__main__':
    unittest.main()

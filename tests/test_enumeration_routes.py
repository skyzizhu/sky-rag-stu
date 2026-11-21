"""列举第一阶段：目录统计、文档展开、跨文档精排与覆盖边界。"""
from dataclasses import replace
from unittest import mock

from src.enumeration import enumeration_route, document_representatives
from src.pipeline import query_evidence, answer_question
from src.prompt import build_messages
from src.reranker import RerankOutcome
from src import index_state
from tests._helpers import IngestHarness


class EnumerationRouteTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()

    def evidence(self, question, **kwargs):
        return query_evidence(question, use_query_understanding=False, use_hybrid=False,
                              use_llm=kwargs.pop('use_llm', False),
                              config=kwargs.pop('config', self.cfg), **kwargs)

    def test_route_conservative_metadata_queries(self):
        self.assertEqual(enumeration_route('知识库里有哪些文件', True), 'catalog')
        self.assertEqual(enumeration_route('今年上传的文件有多少份', True), 'catalog')
        self.assertEqual(enumeration_route('知识库今年上传的文件有多少份', True), 'catalog')
        self.assertEqual(enumeration_route('知识库有多少份文件', True), 'catalog')
        self.assertEqual(enumeration_route('知识库里有哪些关于RAG的文档', True), 'cross_document')
        self.assertEqual(enumeration_route('知识库里有哪些RAG文档', True), 'cross_document')
        self.assertEqual(enumeration_route('这份简历有几段工作经历', True), 'document')
        self.assertEqual(enumeration_route('哪些项目用过RAG', True), 'cross_document')
        self.assertEqual(enumeration_route('什么是RAG', False), 'ordinary')

    def test_catalog_counts_unique_documents_with_permission_and_no_models(self):
        self.ingest([self.write_note('公开内容' * 200, 'public.md')])
        self.ingest([self.write_note('HR内容', 'hr.md')], default_groups=['hr'])
        self.ingest([self.write_note('财务内容', 'finance.md')], default_groups=['finance'])
        with mock.patch('src.pipeline.get_retriever', side_effect=AssertionError('不得向量检索')), \
             mock.patch('src.pipeline.get_embedding_client', side_effect=AssertionError('不得向量化')), \
             mock.patch('src.pipeline.get_llm_client', side_effect=AssertionError('不得调用LLM')):
            result = answer_question('知识库里有哪些文件', user_groups=['hr'])
        self.assertEqual(result.evidence_meta['matched_documents'], 2)
        self.assertEqual(result.evidence_meta['coverage'], 'complete')
        self.assertIn('public.md', result.answer)
        self.assertNotIn('finance.md', result.answer)

    def test_catalog_budget_does_not_report_complete(self):
        for i in range(3):
            self.ingest([self.write_note(f'内容{i}', f'{i}.md')])
        result, meta = self.evidence('知识库里有哪些文件',
                                     config=replace(self.cfg, enum_catalog_docs=1))
        self.assertEqual(meta['matched_documents'], 3)
        self.assertEqual(len(result.sources), 1)
        self.assertEqual(meta['coverage'], 'partial')
        _, meta = self.evidence('知识库里有哪些文件',
                                config=replace(self.cfg, enum_catalog_points=1))
        self.assertTrue(meta['scan_truncated'])
        self.assertEqual(meta['coverage'], 'partial')

    def test_document_expansion_restores_unretrieved_sections(self):
        path = self.write_note('第一部分正文。' * 180 + '\n\n### 第二部分\n' + '独立条目SECOND。' * 180)
        self.ingest([path])
        from src.retriever import get_retriever
        hits = get_retriever().retrieve('正文', top_k=1)
        with mock.patch.object(get_retriever(), 'retrieve', return_value=hits), \
             mock.patch('src.pipeline.rerank_items', side_effect=AssertionError('指定文档不逐卡精排')):
            result, meta = self.evidence('这份文档有哪些条目', filters={'source': path.name})
        self.assertEqual(meta['route'], 'document')
        self.assertIn('独立条目SECOND', result.context)
        self.assertEqual(meta['processed_documents'], 1)

    def test_explicit_source_not_relaxed_to_other_documents(self):
        self.ingest([self.write_note('其他文件的付款条件')])
        from src.query_understanding import QueryUnderstanding
        qu = QueryUnderstanding(original='q', enumeration=True, enumeration_scope='document',
                                filters={'source': 'missing.pdf'}, ok=True)
        with mock.patch('src.pipeline.understand_query', return_value=qu), \
             mock.patch('src.pipeline.get_llm_client'):
            result, meta = query_evidence('这份合同有哪些付款条件', use_llm=True, config=self.cfg)
        self.assertEqual(meta['coverage'], 'not_found')
        self.assertFalse(result.retrieved)

    def test_cross_document_reranks_documents_without_normal_topk_cut(self):
        for i in range(3):
            self.ingest([self.write_note(f'项目{i}使用RAG，条目ITEM{i}。' * 30, f'project{i}.md')])
        seen = []
        def rank(question, items, top_k, **kwargs):
            seen.extend(items)
            return RerankOutcome(items=list(reversed(items)), ok=True)
        with mock.patch('src.pipeline.rerank_items', side_effect=rank), \
             mock.patch('src.pipeline.get_llm_client'):
            result, meta = self.evidence('哪些项目用过RAG', top_k=1, use_llm=True)
        self.assertEqual(meta['route'], 'cross_document')
        self.assertEqual(len(seen), 3)
        self.assertEqual(len({it.metadata['document_id'] for it in seen}), 3)
        self.assertEqual(meta['processed_documents'], 3)
        for i in range(3):
            self.assertIn(f'ITEM{i}', result.context)
        self.assertTrue(any(n['name'] == '文档级精排' for n in result.trace))

    def test_cross_document_budget_and_prompt_disclose_partial(self):
        for i in range(3):
            self.ingest([self.write_note(f'RAG项目{i}', f'{i}.md')])
        result, meta = self.evidence('哪些项目用过RAG', config=replace(self.cfg, enum_expand_docs=1))
        self.assertEqual(meta['coverage'], 'partial')
        self.assertEqual(meta['processed_documents'], 1)
        prompt = build_messages(result.context, result.question, enumerate_mode=True, evidence_meta=meta)
        self.assertIn('必须提示可能遗漏', prompt[0]['content'])
        self.assertIn('不得宣称', prompt[0]['content'])

    def test_local_evidence_rerank_never_calls_cloud(self):
        self.ingest([self.write_note('RAG项目')])
        def local(question, items, top_k, cfg):
            return RerankOutcome(items=items, ok=True, backend='local_onnx')
        with mock.patch('src.pipeline.rerank_documents_local', side_effect=local) as rr, \
             mock.patch('src.pipeline.get_llm_client', side_effect=AssertionError('不得调用云端')):
            _, meta = self.evidence('哪些项目用过RAG', config=replace(self.cfg, rerank_backend='local_onnx'))
        rr.assert_called_once()
        self.assertEqual(meta['route'], 'cross_document')

    def test_expansion_never_crosses_permission_or_status(self):
        path = self.write_note('公开RAG项目')
        self.ingest([path])
        self.ingest([self.write_note('机密SECRET项目', 'secret.md')], default_groups=['finance'])
        result, _ = self.evidence('哪些项目用过RAG', user_groups=['hr'])
        self.assertNotIn('SECRET', result.context)

    def test_retrieval_does_not_change_ledger(self):
        self.ingest([self.write_note('RAG项目')])
        before = index_state.STATE_PATH.read_bytes()
        self.evidence('哪些项目用过RAG')
        self.evidence('知识库有多少份文件')
        self.assertEqual(index_state.STATE_PATH.read_bytes(), before)

    def test_catalog_uses_ingestion_date_and_flags_missing_dates(self):
        self.ingest([self.write_note('资料', 'a.md')])
        self.ingest([self.write_note('资料二', 'b.md')])
        for doc in self.store.list_documents():
            self.store.client.set_payload(collection_name=self.cfg.qdrant_collection,
                                          payload={'indexed_at': '2026-02-03 10:00:00' if doc['source'] == 'a.md' else ''},
                                          points=self.store.build_filter({'document_id': doc['document_id']}))
        _, meta = self.evidence('2026年上传的文件有多少份')
        self.assertEqual(meta['route'], 'catalog')
        self.assertEqual(meta['date_field'], 'indexed_at')
        self.assertEqual(meta['matched_documents'], 1)
        self.assertEqual(meta['unknown_date_documents'], 1)
        self.assertEqual(meta['coverage'], 'partial')

    def test_document_card_limit_is_disclosed(self):
        path = self.write_note('A段内容。' * 300 + '\n\n### B节\n' + 'B段内容。' * 300)
        self.ingest([path])
        _, meta = self.evidence('这份文档有哪些条目', filters={'source': path.name},
                                config=replace(self.cfg, enum_doc_cards=1))
        self.assertTrue(meta['expansion_truncated'])
        self.assertEqual(meta['coverage'], 'partial')

    def test_rank_failure_retains_original_documents(self):
        for i in range(2):
            self.ingest([self.write_note(f'RAG项目ITEM{i}', f'{i}.md')])
        def fail(question, items, **kwargs):
            return RerankOutcome(items=items, ok=False, error='test failure')
        with mock.patch('src.pipeline.rerank_items', side_effect=fail), \
             mock.patch('src.pipeline.get_llm_client'):
            result, meta = self.evidence('哪些项目用过RAG', use_llm=True)
        self.assertEqual(meta['processed_documents'], 2)
        self.assertIn('ITEM0', result.context)
        self.assertIn('ITEM1', result.context)

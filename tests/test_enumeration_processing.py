"""第二阶段：分批处理、证据校验、去重、补查及预算；全部为隔离数据库和假模型。"""
from dataclasses import replace
import json
import re
from unittest import mock

from src.enumeration_processing import (process_enumeration, EXTRACTION_PROMPT, PLANNER_PROMPT,
                                        VERIFICATION_PROMPT, validated_records, merge_record,
                                        BudgetClient, BudgetExceeded, ProcessingOutcome,
                                        evidence_fragments, render_records)
from src.permissions import scope_filters
from src.retriever import get_retriever
from src.pipeline import answer_question
from tests._helpers import IngestHarness


class FakeExtractor:
    def __init__(self, plans=None):
        self.plans = list(plans or [])
        self.inputs = []

    def chat(self, messages, usage_out=None, **options):
        self.inputs.append((messages, options))
        data = json.loads(messages[-1]['content'])
        if usage_out is not None:
            usage_out.update(input=10, output=10, input_cached=0)
        if messages[0]['content'] == PLANNER_PROMPT:
            return json.dumps({'queries': self.plans.pop(0) if self.plans else []}), 0.0
        if messages[0]['content'] == VERIFICATION_PROMPT:
            count = len(data.get('candidates', []))
            return json.dumps({'verdicts': [{'index': i + 1, 'relevant': True}
                                            for i in range(count)]}), 0.0
        items = []
        for source in data['materials']:
            for match in re.finditer(r'(项目[甲乙丙丁]) (PRJ-\d+) 使用RAG[^。]*。', source['text']):
                items.append({'name': match[1], 'identity': match[2], 'evidence': [
                    {'source_id': source['source_id'], 'quote': match[0]}]})
        return json.dumps({'items': items}, ensure_ascii=False), 0.0


class EnumerationProcessingTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.cfg = replace(self.cfg, enum_max_rounds=2, enum_page_cards=1, enum_batch_tokens=800,
                           enum_max_calls=20, enum_max_tokens=40000)

    def note(self, body, name='a.md', groups=None):
        path = self.write_note(body, name)
        self.ingest([path], default_groups=groups)
        return next(d for d in self.store.list_documents() if d['source'] == name)

    def run_processing(self, docs=None, llm=None, route='document', cfg=None, retrieve=None, filters=None,
                       question='哪些项目使用RAG', intent=""):
        docs = self.store.list_documents() if docs is None else docs
        filters = filters or {'status': 'active', 'allowed_groups': ['all']}
        return process_enumeration(question, docs,
                                   [(d['document_id'], d['version']) for d in docs], filters,
                                   self.store, retrieve or (lambda query, deadline: []), None,
                                   llm or FakeExtractor(), cfg or self.cfg, route, intent=intent)

    def test_reads_multiple_pages_and_extracts_tail(self):
        body = '项目甲 PRJ-101 使用RAG完成检索。' + '背景正文。' * 300
        body += '\n\n### 后续\n' + '背景正文。' * 300 + '项目乙 PRJ-102 使用RAG完成问答。'
        self.note(body)
        outcome = self.run_processing()
        self.assertEqual({r['name'] for r in outcome.records}, {'项目甲', '项目乙'})
        self.assertGreater(outcome.stats['cards_read'], 1)
        self.assertGreater(outcome.stats['valid_batches'], 1)
        self.assertEqual(outcome.stats['stop_reason'], 'document_read')

    def test_merges_shared_verified_identifier_and_preserves_sources(self):
        self.note('项目甲 PRJ-101 使用RAG完成检索。', 'a.md')
        self.note('项目乙 PRJ-101 使用RAG完成问答。', 'b.md')
        outcome = self.run_processing()
        self.assertEqual(len(outcome.records), 1)
        self.assertEqual(len(outcome.sources), 2)
        self.assertEqual(outcome.records[0]['citations'], [1, 2])

    def test_same_name_different_ids_remain_separate(self):
        self.note('项目甲 PRJ-101 使用RAG完成检索。', 'a.md')
        self.note('项目甲 PRJ-102 使用RAG完成问答。', 'b.md')
        outcome = self.run_processing()
        self.assertEqual(len(outcome.records), 2)
        self.assertEqual(outcome.stats['possible_duplicates'], 2)
        self.assertIn('不等于去重后的实体总数', render_records(outcome))

    def test_fabricated_sources_quotes_names_and_ids_are_rejected(self):
        fragments = [{'source_id': 'a', 'text': '项目甲 PRJ-101 使用RAG。', 'metadata': {}}]
        base = {'name': '项目甲', 'identity': 'FAKE-999',
                'evidence': [{'source_id': 'a', 'quote': '项目甲 PRJ-101 使用RAG。'}]}
        valid, rejected = validated_records({'items': [base,
            {**base, 'name': '项目乙'},
            {**base, 'evidence': [{'source_id': 'fake', 'quote': '项目甲'}]},
            {**base, 'evidence': [{'source_id': 'a', 'quote': '项目甲没有使用RAG'}]}]}, fragments)
        self.assertEqual(rejected, 3)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]['identity'], '')

    def test_followup_finds_a_previously_unseen_project(self):
        first = self.note('项目甲 PRJ-101 使用RAG完成检索。')
        second = self.note('项目乙 PRJ-102 使用RAG完成问答。', 'b.md')
        hits = get_retriever().retrieve('RAG', top_k=20,
                                        filters={'document_id': second['document_id'], 'status': 'active'})
        llm = FakeExtractor(plans=[['检索增强生成的其他案例'], []])
        retrieve = mock.Mock(return_value=hits)
        outcome = self.run_processing([first], llm, route='cross_document', retrieve=retrieve)
        self.assertEqual(len(outcome.records), 2)
        self.assertEqual(outcome.stats['rounds'], 1)
        self.assertEqual(outcome.stats['stop_reason'], 'no_more_queries')
        self.assertIn('哪些项目使用RAG', retrieve.call_args.args[0])

    def test_repeated_queries_and_no_new_information_stop(self):
        self.note('项目甲 PRJ-101 使用RAG。')
        llm = FakeExtractor(plans=[['线索一'], ['线索二'], ['线索三']])
        outcome = self.run_processing(llm=llm, route='cross_document',
                                      cfg=replace(self.cfg, enum_max_rounds=5, enum_no_new_rounds=2))
        self.assertEqual(outcome.stats['rounds'], 2)
        self.assertEqual(outcome.stats['stop_reason'], 'no_new_information')
        self.assertNotIn('已查全', render_records(outcome).split('不能据此')[0])

    def test_call_budget_retains_valid_records(self):
        self.note('项目甲 PRJ-101 使用RAG。')
        outcome = self.run_processing(route='cross_document', cfg=replace(self.cfg, enum_max_calls=1))
        self.assertEqual(outcome.stats['calls'], 1)
        self.assertEqual(outcome.stats['stop_reason'], 'call_budget')
        self.assertEqual(len(outcome.records), 1)
        self.assertIn('结果不完整', render_records(outcome, coverage='partial').splitlines()[0])

    def test_short_documents_share_one_extraction_batch_and_leave_room_for_followup(self):
        for index, name in enumerate("甲乙丙丁", 1):
            self.note(f'项目{name} PRJ-10{index} 使用RAG。', f'{index}.md')
        llm = FakeExtractor()
        outcome = self.run_processing(llm=llm, route='cross_document',
                                      cfg=replace(self.cfg, enum_max_calls=2))
        self.assertEqual(outcome.stats['valid_batches'], 1)
        self.assertEqual(outcome.stats['processed_documents'], 4)
        self.assertEqual(outcome.stats['stop_reason'], 'no_more_queries')
        self.assertEqual(len(outcome.records), 4)
        self.assertEqual(len(json.loads(llm.inputs[0][0][-1]['content'])['materials']), 4)

    def test_truncated_batch_is_split_within_same_budget(self):
        self.note('项目甲 PRJ-101 使用RAG。', 'a.md')
        self.note('项目乙 PRJ-102 使用RAG。', 'b.md')
        llm = FakeExtractor()
        chat = llm.chat

        def truncate_joint_batch(messages, usage_out=None, **kwargs):
            materials = json.loads(messages[-1]['content'])['materials']
            if len(materials) > 1:
                usage_out.update(input=10, output=10, finish_reason='length')
                return '{"items":[', 0.0
            return chat(messages, usage_out=usage_out, **kwargs)

        llm.chat = truncate_joint_batch
        cfg = replace(self.cfg, enum_verify_enabled=False, enum_max_calls=3)
        outcome = self.run_processing(llm=llm, cfg=cfg)
        self.assertEqual(outcome.stats['split_batches'], 1)
        self.assertEqual(outcome.stats['calls'], 3)
        self.assertEqual(outcome.stats['valid_batches'], 2)
        self.assertEqual(outcome.stats['errors'], 0)
        self.assertEqual({r['name'] for r in outcome.records}, {'项目甲', '项目乙'})
        limited = self.run_processing(llm=llm, cfg=replace(cfg, enum_max_calls=2))
        self.assertEqual(limited.stats['calls'], 2)
        self.assertEqual(limited.stats['stop_reason'], 'call_budget')
        self.assertEqual(len(limited.records), 1)

    def test_verified_product_code_merges_different_quotes_without_merging_other_codes(self):
        fragments = [
            {'source_id': 'a', 'text': 'X-1000 于 2022 年发布。', 'metadata': {}},
            {'source_id': 'b', 'text': 'x‑1000 是一款工业检测仪。', 'metadata': {}},
            {'source_id': 'c', 'text': 'X-2000 新品发布会举行。', 'metadata': {}},
        ]
        raw = {'items': [
            {'name': name, 'evidence': [{'source_id': fragment['source_id'], 'quote': fragment['text']}]}
            for name, fragment in zip(('X-1000', 'x‑1000', 'X-2000'), fragments)
        ]}
        parsed, rejected = validated_records(raw, fragments, model_codes=True)
        self.assertEqual(rejected, 0)
        merged = []
        for record in parsed:
            merge_record(merged, record)
        self.assertEqual([r['identity'] for r in merged], ['x-1000', 'x-2000'])
        self.assertEqual([e['source_id'] for e in merged[0]['evidence']], ['a', 'b'])
        plain, _ = validated_records(raw, fragments)
        self.assertFalse(plain[0]['identity'])

    def test_card_and_document_budgets(self):
        self.note('项目甲 PRJ-101 使用RAG。' + '正文。' * 600)
        self.note('项目乙 PRJ-102 使用RAG。', 'b.md')
        outcome = self.run_processing(cfg=replace(self.cfg, enum_max_cards=1))
        self.assertEqual(outcome.stats['cards_read'], 1)
        self.assertEqual(outcome.stats['stop_reason'], 'card_budget')
        outcome = self.run_processing(cfg=replace(self.cfg, enum_max_documents=1))
        self.assertEqual(outcome.stats['processed_documents'], 1)
        self.assertEqual(outcome.stats['stop_reason'], 'document_budget')

    def test_item_budget_is_not_silent(self):
        self.note('项目甲 PRJ-101 使用RAG。项目乙 PRJ-102 使用RAG。')
        outcome = self.run_processing(cfg=replace(self.cfg, enum_max_items=1))
        self.assertEqual(len(outcome.records), 1)
        self.assertEqual(outcome.stats['stop_reason'], 'item_budget')

    def test_time_and_token_budgets_prevent_model_call(self):
        llm = FakeExtractor()
        outcome = ProcessingOutcome()
        budget = BudgetClient(llm, self.cfg, outcome)
        budget.deadline = 0
        with self.assertRaisesRegex(BudgetExceeded, 'time_budget'):
            budget.chat([])
        budget = BudgetClient(llm, replace(self.cfg, enum_max_tokens=256), outcome)
        with self.assertRaisesRegex(BudgetExceeded, 'token_budget'):
            budget.chat([{'role': 'user', 'content': '中文' * 1000}])
        self.assertFalse(llm.inputs)

    def test_page_reads_keep_permissions_and_status(self):
        public = self.note('项目甲 PRJ-101 使用RAG。')
        hidden = self.note('项目乙 PRJ-102 使用RAG。SECRET', 'b.md', groups=['finance'])
        llm = FakeExtractor()
        filters = scope_filters({'status': 'active'}, ['hr'])
        with mock.patch.object(self.store, 'card_page', wraps=self.store.card_page) as page:
            outcome = self.run_processing([public, hidden], llm, filters=filters)
        self.assertEqual(len(outcome.records), 1)
        self.assertEqual(outcome.stats['processed_documents'], 1)
        self.assertNotIn('SECRET', str(llm.inputs))
        for call in page.call_args_list:
            self.assertEqual(call.args[0]['allowed_groups'], ['all', 'hr'])
            self.assertEqual(call.args[0]['status'], 'active')

    def test_invalid_json_keeps_partial_records(self):
        self.note('项目甲 PRJ-101 使用RAG。')
        llm = FakeExtractor()
        original = llm.chat
        def chat(messages, **kwargs):
            if messages[0]['content'] == PLANNER_PROMPT:
                return 'broken', 0.0
            return original(messages, **kwargs)
        llm.chat = chat
        outcome = self.run_processing(llm=llm, route='cross_document')
        self.assertEqual(len(outcome.records), 1)
        self.assertEqual(outcome.stats['stop_reason'], 'processing_error')

    def test_pipeline_returns_all_records_with_usage_and_time(self):
        path = self.note('项目甲 PRJ-101 使用RAG。项目乙 PRJ-102 使用RAG。')
        llm = FakeExtractor()
        with mock.patch('src.pipeline.get_llm_client', return_value=llm):
            result = answer_question('这份文档有哪些项目', use_query_understanding=False,
                                     use_rerank=False, config=self.cfg,
                                     filters={'source': path['source']}, user_groups=['all'])
        self.assertEqual(len(result.enumerated_items), 2)
        self.assertIn('项目甲', result.answer)
        self.assertIn('项目乙', result.answer)
        self.assertEqual(result.evidence_meta['stage2']['items'], 2)
        self.assertIn('enumeration_1', result.usage['stages'])
        self.assertAlmostEqual(result.elapsed['total'], sum(result.elapsed.get(k, 0) for k in
                               ('query_understanding', 'retrieval_only', 'rerank', 'assembly', 'enumeration', 'llm')))
        self.assertNotEqual(result.evidence_meta['coverage'], 'complete')

    def test_pipeline_discards_records_when_document_grant_changes_during_extraction(self):
        from src.manage import update_metadata

        path = self.note('项目甲 PRJ-101 使用RAG。')
        llm = FakeExtractor()
        original = llm.chat

        def chat(messages, **kwargs):
            output = original(messages, **kwargs)
            if messages[0]['content'] == EXTRACTION_PROMPT:
                update_metadata(path['path'], {'allowed_groups': [], 'allowed_users': []})
            return output

        with mock.patch.object(llm, 'chat', side_effect=chat), \
             mock.patch('src.pipeline.get_llm_client', return_value=llm):
            result = answer_question('这份文档有哪些项目', use_query_understanding=False, use_rerank=False,
                                     config=self.cfg, filters={'source': path['source']},
                                     user_groups=[], username='alice')
        self.assertEqual(result.evidence_meta['scope_issue'], 'index_changed')
        self.assertEqual((result.enumerated_items, result.sources, result.context), ([], [], ''))
        self.assertNotIn('项目甲', result.answer)

    def test_evidence_only_and_disabled_do_not_extract(self):
        self.note('项目甲 PRJ-101 使用RAG。')
        with mock.patch('src.pipeline.get_llm_client', side_effect=AssertionError('仅证据不得调用云端')):
            result = answer_question('这份文档有哪些项目', use_llm=False, config=self.cfg)
        self.assertEqual(result.evidence_meta['stage2']['reason'], 'evidence_only')
        llm = mock.Mock()
        llm.chat_stream.return_value = iter(['阶段一回答'])
        with mock.patch('src.pipeline.get_llm_client', return_value=llm):
            result = answer_question('这份文档有哪些项目', use_query_understanding=False, use_rerank=False,
                                     config=replace(self.cfg, enum_stage2_enabled=False))
        llm.chat.assert_not_called()
        self.assertEqual(result.answer, '阶段一回答')

    def test_long_fragment_is_split_without_losing_tail(self):
        text = '前部内容。' * 800 + '尾部项目甲 PRJ-101 使用RAG。'
        row = {'id': 'card', 'payload': {'text': text, 'document_id': 'doc'}}
        fragments = list(evidence_fragments(row, 800))
        self.assertGreater(len(fragments), 1)
        self.assertTrue(any('尾部项目甲 PRJ-101 使用RAG。' in f['text'] for f in fragments))
        self.assertEqual(len({f['source_id'] for f in fragments}), len(fragments))

    def test_planner_deduplicates_equivalent_queries(self):
        self.note('项目甲 PRJ-101 使用RAG。')
        retrieve = mock.Mock(return_value=[])
        outcome = self.run_processing(llm=FakeExtractor(plans=[['RAG', 'rag', ' RAG ']]),
                                      route='cross_document', retrieve=retrieve)
        self.assertEqual(retrieve.call_count, 1)
        self.assertEqual(outcome.stats['queries'], 1)

    def test_english_renderer_preserves_scope_warning(self):
        self.note('项目甲 PRJ-101 使用RAG。')
        outcome = self.run_processing()
        answer = render_records(outcome, 'en')
        self.assertIn('not a definitive total', answer)
        self.assertIn('do not establish exhaustive coverage', answer)
        self.assertIn('[1]', answer)

    def test_actual_llm_wrapper_disables_retry_and_honors_limits(self):
        from types import SimpleNamespace
        from src.llm import LLMClient
        fake = mock.Mock()
        fake.with_options.return_value = fake
        fake.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{}'), finish_reason='stop')], usage=None)
        with mock.patch('src.llm.OpenAI', return_value=fake):
            client = LLMClient(replace(self.cfg, llm_api_key='test', llm_base_url='https://example.invalid/v1'))
        client.chat([{'role': 'user', 'content': 'test'}], timeout=2.0, max_tokens=128)
        fake.with_options.assert_called_once_with(timeout=2.0, max_retries=0)
        self.assertEqual(fake.chat.completions.create.call_args.kwargs['max_tokens'], 128)
        # 温度：默认取配置值；按次覆盖时透传指定值（Query 理解用 0 保证路由可复现）
        self.assertEqual(fake.chat.completions.create.call_args.kwargs['temperature'],
                         self.cfg.llm_temperature)
        client.chat([{'role': 'user', 'content': 'test'}], temperature=0.0)
        self.assertEqual(fake.chat.completions.create.call_args.kwargs['temperature'], 0.0)

    def test_followup_doc_selection_cap_is_disclosed(self):
        first = self.note('项目甲 PRJ-101 使用RAG。')
        self.note('项目乙 PRJ-102 使用RAG。', 'b.md')
        self.note('项目丙 PRJ-103 使用RAG。', 'c.md')
        hits = get_retriever().retrieve('RAG', top_k=20)
        outcome = self.run_processing([first], FakeExtractor(plans=[['线索'], []]),
                                      route='cross_document', cfg=replace(self.cfg, enum_expand_docs=1),
                                      retrieve=lambda query, deadline: hits)
        self.assertTrue(outcome.stats['selection_truncated'])

    def test_shared_id_alias_is_preserved(self):
        self.note('项目甲 PRJ-101 使用RAG。', 'a.md')
        self.note('项目乙 PRJ-101 使用RAG。', 'b.md')
        outcome = self.run_processing()
        self.assertEqual(outcome.records[0]['aliases'], ['项目乙'])
        self.assertIn('项目乙', render_records(outcome))

    def test_session_roundtrip_keeps_records_and_citations(self):
        import tempfile
        from pathlib import Path
        from src import sessions
        self.note('项目甲 PRJ-101 使用RAG。')
        with mock.patch('src.pipeline.get_llm_client', return_value=FakeExtractor()):
            result = answer_question('这份文档有哪些项目', use_query_understanding=False,
                                     use_rerank=False, config=self.cfg)
        with tempfile.TemporaryDirectory() as root, mock.patch.object(sessions, 'SESSIONS_DIR', Path(root)):
            sid = sessions.save_session('20261118-stage2', [
                {'role': 'assistant', 'content': result.answer, 'result': result}])
            restored = sessions.load_session(sid)['messages'][0]['result']
        self.assertEqual(restored['enumerated_items'], result.enumerated_items)
        self.assertEqual(restored['sources'][0]['source_id'], result.sources[0]['source_id'])
        self.assertIn('enumeration', restored['elapsed'])

    def test_pipeline_falls_back_after_total_extraction_failure(self):
        self.note('项目甲 PRJ-101 使用RAG。')
        llm = mock.Mock()
        llm.chat.return_value = ('not JSON', 0.0)
        llm.chat_stream.return_value = iter(['首轮资料回答 [1]'])
        with mock.patch('src.pipeline.get_llm_client', return_value=llm):
            result = answer_question('这份文档有哪些项目', use_query_understanding=False,
                                     use_rerank=False, config=self.cfg)
        self.assertTrue(result.evidence_meta['stage2']['fallback'])
        self.assertEqual(result.evidence_meta['coverage'], 'partial')
        self.assertEqual(result.answer, '首轮资料回答 [1]')
        self.assertTrue(result.sources)

    def test_output_truncation_is_not_accepted_as_a_complete_batch(self):
        self.note('项目甲 PRJ-101 使用RAG。')
        llm = FakeExtractor()
        original = llm.chat
        def truncated(messages, usage_out=None, **kwargs):
            raw, elapsed = original(messages, usage_out=usage_out, **kwargs)
            usage_out['finish_reason'] = 'length'
            return raw, elapsed
        llm.chat = truncated
        outcome = self.run_processing(llm=llm)
        self.assertFalse(outcome.records)
        self.assertEqual(outcome.stats['stop_reason'], 'processing_error')
        self.assertEqual(outcome.stats['valid_batches'], 0)

    # ---------- 提取后语义复审（相关性把关） ----------

    def _verify_llm(self, llm, verdicts=None, error=False):
        """包一层语义复审应答：verdicts 为判定表，error=True 时复审直接抛错。"""
        original = llm.chat
        def chat(messages, usage_out=None, **kwargs):
            if messages[0]['content'] == VERIFICATION_PROMPT:
                if error:
                    raise RuntimeError('verifier down')
                if usage_out is not None:
                    usage_out.update(input=5, output=5, input_cached=0)
                return json.dumps({'verdicts': verdicts or []}), 0.0
            return original(messages, usage_out=usage_out, **kwargs)
        llm.chat = chat
        return llm

    def test_irrelevant_keyword_hits_are_removed_by_relevance_review(self):
        self.note('项目甲 PRJ-101 使用RAG 做无人机实时建模产品展示。')
        llm = self._verify_llm(FakeExtractor(), verdicts=[{'index': 1, 'relevant': False}])
        outcome = self.run_processing(llm=llm)
        self.assertEqual(outcome.records, [])
        self.assertEqual(outcome.stats['verify_removed'], 1)
        self.assertFalse(outcome.stats['verify_error'])
        self.assertEqual(outcome.sources, [])
        self.assertIn('0 条', render_records(outcome))

    def test_relevant_records_survive_relevance_review(self):
        self.note('项目甲 PRJ-101 使用RAG。')
        llm = self._verify_llm(FakeExtractor(), verdicts=[{'index': 1, 'relevant': True}])
        outcome = self.run_processing(llm=llm)
        self.assertEqual(len(outcome.records), 1)
        self.assertEqual(outcome.stats['verify_removed'], 0)
        self.assertEqual(len(outcome.sources), 1)
        self.assertEqual(outcome.records[0]['citations'], [1])

    def test_mixed_review_filters_orphan_sources_and_keeps_citations(self):
        self.note('项目甲 PRJ-101 使用RAG。')
        self.note('项目乙 PRJ-102 使用RAG。', 'b.md')
        llm = self._verify_llm(FakeExtractor(), verdicts=[{'index': 1, 'relevant': True},
                                                          {'index': 2, 'relevant': False}])
        outcome = self.run_processing(llm=llm)
        self.assertEqual([r['name'] for r in outcome.records], ['项目甲'])
        self.assertEqual(outcome.stats['verify_removed'], 1)
        self.assertEqual(len(outcome.sources), 1)
        self.assertEqual(outcome.records[0]['citations'], [1])

    def test_relevance_review_failure_fails_open(self):
        self.note('项目甲 PRJ-101 使用RAG。')
        llm = self._verify_llm(FakeExtractor(), error=True)
        outcome = self.run_processing(llm=llm)
        self.assertEqual(len(outcome.records), 1)
        self.assertEqual(outcome.stats['verify_removed'], 0)
        self.assertTrue(outcome.stats['verify_error'])
        self.assertEqual(len(outcome.sources), 1)
        # fail-open 不能是静默的：输出里必须可见标注复审未完成
        self.assertIn('语义复审未完成', render_records(outcome))

    def test_relevance_review_can_be_disabled(self):
        self.note('项目甲 PRJ-101 使用RAG。')
        llm = FakeExtractor()
        outcome = self.run_processing(llm=llm, cfg=replace(self.cfg, enum_verify_enabled=False))
        self.assertEqual(len(outcome.records), 1)
        self.assertFalse(any(messages[0]['content'] == VERIFICATION_PROMPT
                             for messages, _ in llm.inputs))

    # ---------- 意图锚点（target_intent）贯通提取与复审 ----------

    def test_intent_anchor_reaches_extraction_and_review(self):
        self.note('项目甲 PRJ-101 使用RAG。')
        llm = FakeExtractor()
        self.run_processing(llm=llm, question='知识库里有哪些建模方法？',
                            intent='用户想知道建模的方法/技术/流程本身')
        extraction_payloads = [json.loads(messages[-1]['content'])
                               for messages, _ in llm.inputs
                               if messages[0]['content'] == EXTRACTION_PROMPT]
        self.assertTrue(extraction_payloads)
        for payload in extraction_payloads:
            self.assertEqual(payload['target_intent'], '用户想知道建模的方法/技术/流程本身')
        review_payloads = [json.loads(messages[-1]['content'])
                           for messages, _ in llm.inputs
                           if messages[0]['content'] == VERIFICATION_PROMPT]
        self.assertTrue(review_payloads)
        for payload in review_payloads:
            self.assertEqual(payload['target_intent'], '用户想知道建模的方法/技术/流程本身')

    def test_intent_defaults_to_empty_when_absent(self):
        self.note('项目甲 PRJ-101 使用RAG。')
        llm = FakeExtractor()
        outcome = self.run_processing(llm=llm)
        payloads = [json.loads(messages[-1]['content'])
                    for messages, _ in llm.inputs
                    if messages[0]['content'] == EXTRACTION_PROMPT]
        self.assertTrue(all(payload['target_intent'] == '' for payload in payloads))
        self.assertEqual(len(outcome.records), 1)

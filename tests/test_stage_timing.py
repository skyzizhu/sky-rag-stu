"""阶段计时兼容性与无重叠计时测试；仅使用临时库与内存向量库。"""
import io
import contextlib
from types import SimpleNamespace
from unittest import TestCase, mock
from dataclasses import replace

from src.answer_evaluator import stage_timing, compute_signals
from tests._helpers import IngestHarness


class TimingCompatibilityTests(TestCase):
    def test_new_five_stage_record(self):
        elapsed = dict(query_understanding=2, retrieval=14, retrieval_only=5, rerank=8,
                       assembly=1, llm=3, total=19)
        timing = stage_timing(elapsed)
        self.assertEqual(timing["mode"], "measured")
        self.assertEqual(sum(timing[k] for k in ("query_understanding", "retrieval_only", "rerank", "assembly", "llm")), timing["total"])

    def test_target_history_can_be_inferred_without_mutation(self):
        elapsed = dict(query_understanding=2.281425, retrieval=67.504691, llm=2.305398, total=72.091514)
        trace = [{"name": "Rerank（精排）", "elapsed": 37.293068},
                 {"name": "合并 / 去重", "elapsed": 0.012344}]
        timing = stage_timing(elapsed, trace)
        self.assertEqual(timing["mode"], "inferred")
        self.assertAlmostEqual(timing["retrieval_only"], 30.199279)
        self.assertNotIn("rerank", elapsed)

    def test_incomplete_duplicate_or_invalid_trace_stays_legacy(self):
        elapsed = dict(retrieval=8, llm=2, total=10)
        for trace in ([], [{"name": "Rerank（精排）", "elapsed": 1}],
                      [{"name": "Rerank（精排）", "elapsed": float("nan")}, {"name": "合并 / 去重", "elapsed": 1}],
                      [{"name": "Rerank（精排）", "elapsed": 9}, {"name": "合并 / 去重", "elapsed": 1}]):
            self.assertNotIn("retrieval_only", stage_timing(elapsed, trace))

    def test_timing_roundtrip_session(self):
        import tempfile
        from pathlib import Path
        from src import sessions

        elapsed = dict(query_understanding=2, retrieval=14, retrieval_only=5, rerank=8,
                       assembly=1, llm=3, total=19)
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(sessions, "SESSIONS_DIR", Path(temp)):
            sid = sessions.save_session("20261118_151419-test", [{"role": "assistant", "content": "answer",
                                         "result": {"elapsed": elapsed}}])
            restored = sessions.load_session(sid)
        self.assertEqual(restored["messages"][0]["result"]["elapsed"], elapsed)

    def test_local_score_lines_are_visible_to_signals(self):
        result = SimpleNamespace(trace=[{"name": "Rerank（精排）", "items": [
            ("本地精排的结果", "C2  doc.md  9.2 分\nC1  other.md  2.1 分")]}], sources=[], retrieved=[], answer="")
        signal = compute_signals(result)["rerank"]
        self.assertTrue(signal["ok"])
        self.assertEqual(signal["n"], 2)


class PipelineStageTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, rerank_backend="llm")
        self.use_fakes()
        self.ingest([self.write_note("系统的第二个能力是网信体系承载能力。" * 12)])

    def test_five_stage_total_and_skipped_rerank(self):
        from src.pipeline import answer_question
        fake = mock.Mock()
        fake.chat_stream.return_value = iter(["第二个能力是网信体系承载能力 [1]。"])
        with mock.patch("src.pipeline.get_llm_client", return_value=fake), contextlib.redirect_stdout(io.StringIO()):
            result = answer_question("第二个能力是什么？", use_query_understanding=False,
                                     use_rerank=False, use_hybrid=True, config=self.cfg)
        elapsed = result.elapsed
        self.assertEqual(elapsed["rerank"], 0)
        self.assertAlmostEqual(elapsed["retrieval"], sum(elapsed[k] for k in ("retrieval_only", "rerank", "assembly")))
        self.assertAlmostEqual(elapsed["total"], sum(elapsed[k] for k in ("query_understanding", "retrieval_only", "rerank", "assembly", "llm")))

    def test_nonzero_qu_rerank_failure_and_cache_original_timings(self):
        from src import pipeline
        from src.query_understanding import QueryUnderstanding
        from src.reranker import RerankOutcome
        fake = mock.Mock()
        fake.chat_stream.side_effect = lambda *a, **kw: iter(["第二个能力是网信体系承载能力 [1]。"])
        qu = QueryUnderstanding(original="q", vector_query="网信体系", elapsed=2.3, ok=True)

        def fail_rr(question, items, **kwargs):
            return RerankOutcome(items=items, error="failed", backend="llm")

        with mock.patch.object(pipeline, "get_llm_client", return_value=fake), \
                mock.patch.object(pipeline, "understand_query", return_value=qu), \
                mock.patch.object(pipeline, "rerank_items", side_effect=fail_rr), \
                contextlib.redirect_stdout(io.StringIO()):
            a = pipeline.answer_question("第二个能力？", use_rerank=True, config=self.cfg)
            b = pipeline.answer_question("第二个能力？", use_rerank=True, config=self.cfg)
        self.assertEqual(a.elapsed["query_understanding"], 2.3)
        self.assertEqual(a.elapsed, b.elapsed)
        self.assertEqual(fake.chat_stream.call_count, 1)
        self.assertTrue(any("缓存命中" in n["name"] for n in b.trace))
        self.assertAlmostEqual(a.elapsed["total"], sum(a.elapsed[k] for k in
                              ("query_understanding", "retrieval_only", "rerank", "assembly", "llm")))

    def test_bm25_cache_check_is_not_repeated_for_display(self):
        from src.keyword_search import _get_cached_index
        from src import keyword_search
        from src.retriever import get_retriever
        with mock.patch.object(keyword_search, "_get_cached_index", wraps=_get_cached_index) as check, contextlib.redirect_stdout(io.StringIO()):
            get_retriever().retrieve("网信体系", top_k=5, use_hybrid=True)
        self.assertEqual(check.call_count, 1)

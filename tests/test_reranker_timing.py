"""精排的候选关联、真实后端及分项计时回归；不使用真实模型或 API。"""
from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase, mock

from src.config import AppConfig
from src.retriever import RetrievedItem
from src import reranker


class RerankerTimingTests(TestCase):
    def setUp(self):
        self.cfg = AppConfig(rerank_backend="local_onnx")
        self.items = [RetrievedItem(rank=i + 1, score=1, text="正文" + str(i),
                                    metadata={"source": f"doc{i}.md"}) for i in range(3)]

    def test_local_scores_follow_original_candidates_after_sort(self):
        scores = iter([0.2, 0.9, 0.4])

        def score(*args):
            args[-1].update(token_count=12, tokenization=0.01, inference=0.1)
            return next(scores)

        tokenizer = SimpleNamespace(model=SimpleNamespace())
        with mock.patch.object(reranker, "_get_local_reranker", return_value=(tokenizer, None, [])), \
                mock.patch.object(reranker, "_score_pair", side_effect=score):
            outcome = reranker.rerank_local("q", self.items, 3, self.cfg)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual([x[0] for x in outcome.scores], ["C2", "C3", "C1"])
        self.assertEqual([x[1] for x in outcome.scores], ["doc1.md", "doc2.md", "doc0.md"])
        self.assertEqual([x[2] for x in outcome.scores], [9, 4, 2])
        self.assertEqual(outcome.backend, "local_onnx")
        self.assertEqual(outcome.pair_tokens, [12] * 3)
        self.assertAlmostEqual(outcome.timings["inference"], 0.3)

    def test_llm_scores_follow_original_candidates_after_sort(self):
        llm = mock.Mock()
        llm.chat.return_value = ('[{"id":"C1","score":2},{"id":"C2","score":9},{"id":"C3","score":4}]', 0.1)
        outcome = reranker.rerank_llm("q", self.items, 3, llm, self.cfg)
        self.assertEqual([x[0] for x in outcome.scores], ["C2", "C3", "C1"])
        self.assertEqual([x[2] for x in outcome.scores], [9, 4, 2])

    def test_llm_rerank_reads_late_evidence_and_rejects_invalid_ids(self):
        item = replace(self.items[0], text="背景说明" * 96 + "验收编号 FIN-971。")
        llm = mock.Mock()
        llm.chat.return_value = ('[{"id":"C99","score":10}]', 0.1)
        outcome = reranker.rerank_llm("验收编号", [item], 1, llm, self.cfg)
        self.assertIn("FIN-971", outcome.user_prompt)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.items, [item])

    def test_fallback_keeps_actual_backend_reason_and_attempt_time(self):
        failed = reranker.RerankOutcome(backend="local_onnx", error="model missing", elapsed=2,
                                       timings={"load": 2}, cold_load=True)
        success = reranker.RerankOutcome(backend="llm", ok=True, elapsed=3)
        with mock.patch.object(reranker, "rerank_local", return_value=failed), \
                mock.patch.object(reranker, "rerank_llm", return_value=success):
            result = reranker.rerank("q", self.items, 3, mock.Mock(), self.cfg)
        self.assertEqual(result.backend, "llm")
        self.assertEqual(result.fallback_reason, "model missing")
        self.assertEqual(result.elapsed, 5)
        self.assertTrue(result.cold_load)

    def test_missing_model_failure_has_load_time(self):
        with mock.patch.object(reranker, "_get_local_reranker", side_effect=FileNotFoundError("missing")):
            result = reranker.rerank_local("q", self.items, 3, self.cfg)
        self.assertFalse(result.ok)
        self.assertIn("missing", result.error)
        self.assertIn("load", result.timings)
        self.assertGreaterEqual(result.elapsed, result.timings["load"])

    def test_config_changes_engine_cache_key(self):
        a = reranker._local_cache_key(self.cfg)
        b = reranker._local_cache_key(replace(self.cfg, rerank_threads=2))
        self.assertNotEqual(a, b)

    def test_empty_candidates_do_not_load_model(self):
        with mock.patch.object(reranker, "_get_local_reranker") as load:
            result = reranker.rerank_local("q", [], 3, self.cfg)
        load.assert_not_called()
        self.assertTrue(result.ok)

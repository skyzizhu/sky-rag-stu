"""Compare real ONNX inference strategies with identical candidate lists."""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
from statistics import median
from threading import Barrier
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

from src import reranker
from src.retriever import RetrievedItem, get_retriever

from . import run
from .rerank_candidate_audit import COMPOUND, ENUMERATION, TAIL, CandidateAudit, Case

STRATEGIES = ("serial", "full_batch", "length_buckets")


class CaptureFeed:
    """Capture exactly the tensors prepared by the production pair tokenizer."""

    def run(self, _, feed):
        self.feed = feed
        return [np.zeros((1, 1))]


def batch_groups(lengths, strategy):
    if strategy == "full_batch":
        return [list(range(len(lengths)))]
    groups = []
    for index in sorted(range(len(lengths)), key=lengths.__getitem__):
        if not groups or len(groups[-1]) == 4 or lengths[index] > lengths[groups[-1][0]] * 1.2:
            groups.append([])
        groups[-1].append(index)
    return groups


def batch_rank(question, items, top_k, config, strategy, profile):
    """Prototype only. Use the production scoring lock, tokenizer policy and tie order."""
    result = reranker.RerankOutcome(backend="local_onnx")
    result.timings = dict(load=0.0, queue=0.0, tokenization=0.0, inference=0.0)
    started = perf_counter()
    if not items:
        result.ok = True
        return result
    try:
        tokenizer, session, names = reranker._get_local_reranker(config)
        result.timings["load"] = perf_counter() - started
        pad = tokenizer.token_to_id("<pad>")
        if pad is None:
            raise ValueError("模型分词器缺少 pad token")

        wait = perf_counter()
        with reranker._LOCAL_SCORE_LOCK:
            result.timings["queue"] = perf_counter() - wait
            prep = perf_counter()
            feeds, lengths = [], []
            for item in items:
                record = CaptureFeed()
                reranker._score_pair(tokenizer, record, names, question, item.text)
                feeds.append(record.feed)
                lengths.append(record.feed["input_ids"].shape[1])
            groups = batch_groups(lengths, strategy)
            result.timings["tokenization"] = perf_counter() - prep
            result.pair_tokens = lengths
            scores = [0.0] * len(items)
            shapes = []
            for indices in groups:
                prep = perf_counter()
                width = max(lengths[i] for i in indices)
                tensors = {key: np.full((len(indices), width), pad if key == "input_ids" else 0,
                                        dtype=np.int64) for key in names}
                for row, i in enumerate(indices):
                    for key, tensor in tensors.items():
                        tensor[row, :lengths[i]] = feeds[i][key][0]
                result.timings["tokenization"] += perf_counter() - prep
                tick = perf_counter()
                logits = session.run(None, tensors)[0].reshape(-1)
                result.timings["inference"] += perf_counter() - tick
                if len(logits) != len(indices) or not np.isfinite(logits).all():
                    raise ValueError("模型未为每个句对返回一个有限分数")
                shapes.append([len(indices), width])
                for i, value in zip(indices, logits):
                    scores[i] = reranker._sigmoid(float(value)) * 10
            profile.extend(shapes)
        order = sorted(range(len(items)), key=lambda i: (-scores[i], i))
        result.items = [items[i] for i in order]
        result.scores = [(f"C{i + 1}", items[i].metadata.get("source", "?"), round(scores[i], 4))
                         for i in order]
        result.ok = True
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.items = items
    result.elapsed = perf_counter() - started
    return result


def parity(reference, trial):
    old = {key: score for key, _, score in reference["scores"]}
    new = {key: score for key, _, score in trial["scores"]}
    if not reference["ok"] or not trial["ok"] or old.keys() != new.keys():
        return {"comparable": False}
    before = [key for key, _, _ in reference["scores"]]
    after = [key for key, _, _ in trial["scores"]]
    return {"comparable": True, "top1_same": before[:1] == after[:1],
            "top4_same": set(before[:4]) == set(after[:4]), "order_same": before == after,
            "max_score_delta": round(max((abs(old[k] - new[k]) for k in old), default=0), 4)}


def composition_probe(config):
    """Test whether changing peer text changes one fixed row's score at the same tensor shape."""
    tokenizer, session, names = reranker._get_local_reranker(config)
    question = "Who signs before service restoration?"
    texts = [
        "Record the sign-off before restoring production.",
        "The shift lead must sign before service restoration.",
        "The engineer signs after the restart.",
        "The shift lead delegated routine maintenance.",
        "The kitchen inventory lists four red containers.",
        "The compensation contract expires next month.",
        "The database records hourly snapshot metadata.",
    ]
    copies = []
    for text in texts:
        capture = CaptureFeed()
        reranker._score_pair(tokenizer, capture, names, question, text)
        copies.append(capture.feed)
    pad = tokenizer.token_to_id("<pad>")
    size = max(row["input_ids"].shape[1] for row in copies)
    groups = {"singleton": (0,), "identical_peers": (0, 0, 0, 0),
              "nearby_peers": (0, 1, 2, 3), "unrelated_peers": (0, 4, 5, 6)}
    values = {}
    for label, indices in groups.items():
        tensors = {key: np.full((len(indices), size), pad if key == "input_ids" else 0,
                                dtype=np.int64) for key in names}
        for row, index in enumerate(indices):
            for key in names:
                vec = copies[index][key][0]
                tensors[key][row, :len(vec)] = vec
        with reranker._LOCAL_SCORE_LOCK:
            score = session.run(None, tensors)[0].reshape(-1)[0]
        values[label] = {"shape": [len(indices), size], "score": round(reranker._sigmoid(float(score)) * 10, 4)}
    return values


class BatchAudit(CandidateAudit):
    strategy = "serial"

    def instrument(self):
        native = reranker.rerank_local

        def route(question, items, top_k, config=None):
            if self.strategy == "serial":
                return native(question, items, top_k, config)
            profile = self.local.extra.setdefault("batch_shapes", [])
            result = batch_rank(question, items, top_k, config or self.cfg, self.strategy, profile)
            if not result.ok:
                self.local.extra["batch_error"] = result.error
            return result

        patch = mock.patch("src.reranker.rerank_local", new=route)
        patch.start()
        self.addCleanup(patch.stop)
        self.local_rank = route
        super().instrument()

    def measure(self, case, mode):
        row = super().measure(case, mode)
        row["strategy"] = self.strategy
        row["batch_shapes"] = row["stage_ms"].pop("batch_shapes", [])
        row["batch_error"] = row["stage_ms"].pop("batch_error", "")
        return row

    def direct_cases(self):
        fixtures = {
            "同长度近义句": [
                "设备恢复前需取得值班长签字。", "设备恢复前需取得调度员签字。",
                "设备恢复后需要值班长签字。", "设备停机前需要巡检员签字。",
                "设备恢复前应由值班长确认并签字。", "设备恢复时填写检查记录。",
            ],
            "长短混合": [
                "快速说明。", "恢复时间为四小时，由当值人员记录。",
                "变更手续不涉及设备恢复。" * 12, "设备恢复前需要值班长签字。" + "处置细则与签批流程。" * 35,
                "涉及夜间值班人员与巡检流程。" * 55, "操作完成后按事故级别上报。" * 64,
            ],
            "超长裁窗": [
                "一般资料的会签记录。", "设备恢复前需要值班长签字。" + "日志格式说明与运行参数。" * 260,
            ],
            "英文短句": [
                "The shift lead must sign before service restoration.", "The engineer signs after the restart.",
                "The shift lead delegated routine maintenance.", "Record the sign-off before restoring production.",
            ],
        }
        rows = []
        for case, texts in fixtures.items():
            question = ("Who signs before service restoration?" if case == "英文短句"
                        else "设备恢复前应由谁签字？")
            items = [RetrievedItem(rank=i, score=0.0, text=body, metadata={"source": f"样例{i}.md"},
                                   channels="测试样例") for i, body in enumerate(texts, 1)]
            results = {}
            for strategy in STRATEGIES:
                self.strategy = strategy
                self.local.extra = {}
                outcome = self.local_rank(question, items, 4, self.cfg)
                results[strategy] = {
                    "ok": outcome.ok, "error": outcome.error, "scores": outcome.scores,
                    "ms": {key: run.ms(value) for key, value in outcome.timings.items()},
                    "pair_lengths": outcome.pair_tokens, "batch_shapes": self.local.extra.get("batch_shapes", []),
                }
            rows.append({"case": case, "results": results,
                         "parity": {s: parity(results["serial"], results[s]) for s in STRATEGIES[1:]}})
        return rows

    def mixed_grid(self, strategies, partial):
        case = Case("巡检恢复时限", "fact", run.QUESTION, "巡检手册", (run.CURRENT,))
        rows = []
        for strategy in strategies:
            self.strategy = strategy
            warm = self.measure(case, "web")
            if not warm["passed"] or warm["batch_error"]:
                raise RuntimeError(f"{strategy} 预热失败：{warm['batch_error'] or warm['evidence']}")
        for cycle in range(3):
            workers_cycle = (1, 2, 4)
            for index, workers in enumerate(workers_cycle[cycle:] + workers_cycle[:cycle]):
                offset = (cycle + index) % len(strategies)
                for strategy in strategies[offset:] + strategies[:offset]:
                    self.strategy = strategy
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        for start in range(0, 4, workers):
                            barrier = Barrier(workers)

                            def task(i):
                                barrier.wait()
                                return self.measure(case, "web" if (i + cycle) % 2 == 0 else "mcp_search")

                            tick = perf_counter()
                            pending = [pool.submit(task, start + i) for i in range(workers)]
                            group = [job.result() for job in pending]
                            elapsed = run.ms(perf_counter() - tick)
                            for i, row in enumerate(group):
                                rows.append({**row, "workers": workers, "cycle": cycle,
                                             "batch_ms": elapsed if i == 0 else 0})
            self.save({**partial, "latency_requests": rows})
            print(f"batch latency round {cycle + 1}/3 completed", flush=True)
        return rows

    def check_quality(self, strategies):
        cases = [
            Case("巡检恢复时限", "fact", run.QUESTION, "巡检手册", (run.CURRENT,)),
            Case("近似文档", "semantic", "网络掉线时，晚班领门禁的纸条谁负责收好？",
                 "通行保管", ("AUDGOLD_CUSTODY",)),
            Case("双事实复合问法", "compound", COMPOUND, "切换运维", ("AUDGOLD_POWER", "AUDGOLD_BACKUP")),
            Case("跨文档交付枚举", "enumeration", ENUMERATION, "交付记录",
                 ("PRJ-101", "PRJ-102", "PRJ-103")),
        ]
        rows = []
        for i, case in enumerate(cases):
            order = strategies if i % 2 == 0 else strategies[::-1]
            for mode in case.modes:
                for strategy in order:
                    self.strategy = strategy
                    rows.append(self.measure(case, mode))
        groups = defaultdict(dict)
        for row in rows:
            groups[row["case"], row["mode"]][row["strategy"]] = row
        checks = []
        for (case, mode), group in groups.items():
            old = group["serial"]
            for strategy in strategies[1:]:
                new = group[strategy]
                old_calls, new_calls = old["onnx_calls"], new["onnx_calls"]
                comparable = (len(old_calls) == len(new_calls)
                              and all(a["input"] == b["input"] for a, b in zip(old_calls, new_calls)))
                checks.append({"case": case, "mode": mode, "strategy": strategy,
                               "candidate_inputs_same": comparable,
                               "pair_checks": [parity(a, b) for a, b in zip(old_calls, new_calls)] if comparable else [],
                               "evidence_same": old["evidence"] == new["evidence"],
                               "citations_same": old["citations"] == new["citations"],
                               "entries_same": old["enumerated"] == new["enumerated"]})
        return rows, checks

    def check_rank_five(self, strategies):
        case = Case("正确证据在粗排第 5 位", "tail_control", TAIL, "封签规程", ("AUDGOLD_SEAL",))
        retrieve = get_retriever().retrieve

        def ordered(query, *, top_k, **kwargs):
            hits = retrieve(query, top_k=20, **kwargs)
            gold = [card for card in hits if "AUDGOLD_SEAL" in card.text]
            distractors = [card for card in hits if "AUDGOLD_SEAL" not in card.text]
            self.assertEqual(len(gold), 1)
            self.assertGreaterEqual(len(distractors), 5)
            return (distractors[:4] + gold + distractors[4:])[:top_k]

        rows = []
        with mock.patch.object(get_retriever(), "retrieve", side_effect=ordered):
            for mode in ("web", "mcp_search"):
                for strategy in strategies:
                    self.strategy = strategy
                    rows.append(self.measure(case, mode))
        return rows

    @staticmethod
    def save(data):
        output = Path(__file__).with_name("rerank_batch_results.json")
        output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_workflows(self):
        self.cfg = replace(self.cfg, enum_stage2_enabled=True, enum_rerank_docs=6, enum_expand_docs=6)
        model = self.cfg.rerank_model_dir / "model_quantized.onnx"
        self.assertTrue(model.is_file(), "需要已安装本地 ONNX 精排模型")
        _, engine, _ = reranker._get_local_reranker(self.cfg)
        model_io = {"inputs": {i.name: i.shape for i in engine.get_inputs()},
                    "outputs": {i.name: i.shape for i in engine.get_outputs()},
                    "providers": engine.get_providers(), "bytes": model.stat().st_size}
        with (
            mock.patch("src.config.get_config", return_value=self.cfg),
            mock.patch("src.pipeline.get_config", return_value=self.cfg),
        ):
            direct = self.direct_cases()
            limits = {"model": model_io, "threads": self.cfg.rerank_threads,
                      "top_k": self.cfg.top_k, "recall_candidates": self.cfg.rerank_recall_k,
                      "direct": direct}
            self.save(limits)
            supported = tuple(
                s for s in STRATEGIES if all(row["results"][s]["ok"] for row in direct))
            self.assertIn("serial", supported)
            print(f"ONNX direct checks completed: {supported}", flush=True)
            if len(supported) == 1:
                return
            limits["composition"] = composition_probe(self.cfg)
            latency = self.mixed_grid(supported, limits)
            quality, quality_parity = self.check_quality(supported)
            tail = self.check_rank_five(supported)
        grouped = defaultdict(list)
        for row in latency:
            grouped[row["strategy"], row["workers"]].append(row)
        metrics = []
        for (strategy, workers), rows in sorted(grouped.items()):
            counts = sorted({c["count"] for r in rows for c in r["onnx_calls"]})
            summary = run.summarize(rows, sum(row["batch_ms"] for row in rows))
            by_interface = {
                mode: {key: value for key, value in run.summarize(
                    [r for r in rows if r["mode"] == mode],
                    sum(r["wall_ms"] for r in rows if r["mode"] == mode)).items()
                       if key != "requests_per_second"}
                for mode in ("web", "mcp_search")
            }
            shapes = [shape for row in rows for shape in row["batch_shapes"]]
            padded = [sum(n * size for n, size in row["batch_shapes"])
                      / sum(t for call in row["onnx_calls"] for t in call["tokens"])
                      for row in rows if row["batch_shapes"]]
            metrics.append({"strategy": strategy, "workers": workers, **summary,
                            "actual_candidates": counts, "batch_shapes": shapes,
                            "padding_multiplier_median": median(padded) if padded else 1.0,
                            "by_interface": by_interface})
        self.save({**limits, "latency": metrics, "latency_requests": latency,
                   "quality_requests": quality, "quality_parity": quality_parity, "rank_five": tail})
        for row in metrics:
            self.assertEqual(row["actual_candidates"], [6])


if __name__ == "__main__":
    run.ARGS = SimpleNamespace(model_dir=Path("models/bge-reranker-v2-m3-onnx").resolve())
    unittest.main(defaultTest="BatchAudit")

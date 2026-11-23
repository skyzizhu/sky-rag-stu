"""Isolated paired measurement of ordinary rerank candidate widths."""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
from statistics import median
from threading import Barrier
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest import mock

from src import mcp_server, pipeline, reranker
from src.enumeration_processing import EXTRACTION_PROMPT, PLANNER_PROMPT, VERIFICATION_PROMPT
from src.retriever import get_retriever
from tests.test_enumeration_processing import FakeExtractor

from . import run

COMPOUND = "配电切换 ATS-101 的复核时限是什么，备份恢复 BAK-202 的执行时限是什么？"
ENUMERATION = "列出使用 RAG 完成交付的项目"
TAIL = "封签清点 SEAL-988 结束后应该把凭条交给哪位负责人？"
MARKER = re.compile(r"\bAUDGOLD_[A-Z0-9_]+\b")


@dataclass(frozen=True)
class Case:
    name: str
    kind: str
    query: str
    doc_type: str | None
    expected: tuple[str, ...]
    modes: tuple[str, ...] = ("web", "mcp_search", "mcp_qa")
    user: str | None = None


class EvidenceEcho(run.FixtureLLM):
    """Read evidence and retain its actual ranks, without making up answer content."""

    def chat(self, messages, usage_out=None, **kwargs):
        system, question = messages[0]["content"], messages[-1]["content"]
        if system in (EXTRACTION_PROMPT, PLANNER_PROMPT, VERIFICATION_PROMPT):
            return FakeExtractor().chat(messages, usage_out=usage_out, **kwargs)
        if usage_out is not None:
            usage_out.update(input=40, output=10, finish_reason="stop")
        if COMPOUND in question:
            data = {"sub_queries": ["配电切换 ATS-101 的复核时限", "备份恢复 BAK-202 的执行时限"]}
        elif ENUMERATION in question:
            data = {"enumeration": True, "enumeration_scope": "cross_document"}
        else:
            data = {}
        return json.dumps(data, ensure_ascii=False), 0.0

    def chat_stream(self, messages, usage_out=None, **kwargs):
        prompt = messages[-1]["content"]
        spans = list(re.finditer(r"\[(\d+)\]\s*来源:", prompt))
        facts = {}
        for index, citation in enumerate(spans):
            end = spans[index + 1].start() if index + 1 < len(spans) else len(prompt)
            for match in MARKER.findall(prompt[citation.end():end]):
                facts.setdefault(match, citation[1])
        if facts:
            yield "[[answer_status:answered]]\n"
            yield " ".join(f"{marker} [{rank}]。" for marker, rank in facts.items())
        else:
            yield "[[answer_status:insufficient_evidence]]\n未找到明确证据。"
        if usage_out is not None:
            usage_out.update(input=80, output=20, finish_reason="stop")


class CandidateAudit(run.WorkflowAudit):
    def instrument(self):
        self.llm = EvidenceEcho()
        super().instrument()
        for target in ("src.reranker.rerank_local", "src.pipeline.rerank_documents_local"):
            module = reranker if target.startswith("src.reranker") else pipeline
            existing = getattr(module, target.rsplit(".", 1)[1])

            def tracked(question, items, *args, delegate=existing, **kwargs):
                outcome = delegate(question, items, *args, **kwargs)
                groups = self.local.extra.setdefault("onnx_calls", [])
                groups.append({
                    "ok": outcome.ok, "count": len(items), "tokens": outcome.pair_tokens,
                    "input": [{"source": item.metadata.get("source"), "markers": MARKER.findall(item.text)}
                              for item in items],
                    "scores": outcome.scores,
                })
                return outcome

            patch = mock.patch(target, new=tracked)
            patch.start()
            self.addCleanup(patch.stop)

    def seed(self):
        super().seed()
        seeds = (
            ("配电复核.md", "配电切换 ATS-101 必须在 35 分钟内复核。AUDGOLD_POWER。", "切换运维"),
            ("恢复执行.md", "备份恢复 BAK-202 应在 75 分钟内执行完毕。AUDGOLD_BACKUP。", "切换运维"),
            ("离线凭据.md", "断网时，夜间值守领用离线通行凭据的纸本回执应由值班主管保管。AUDGOLD_CUSTODY。", "通行保管"),
            ("封签交付.md", "SEAL-988 封签清点结束后，清点凭条必须交给审计主管。AUDGOLD_SEAL。", "封签规程"),
        )
        for name, body, category in seeds:
            self.ingest([self.write_note(body, name)], default_doc_type=category)
        for i in range(8):
            for category, body in (
                ("切换运维", f"配电切换 ATS-{303+i} 与备份恢复 BAK-{505+i} 的记录由值守人员建档，无时间规定。"),
                ("通行保管", f"夜间值守和离线通行凭据登记样表 {i}。留存过往交接记录，责任人处为空。"),
                ("封签规程", f"封签清点 SEAL-988 凭条样式 {i}。用于过程归档，表单不写负责人姓名。"),
            ):
                self.ingest([self.write_note(body, f"{category}对照{i}.md")], default_doc_type=category)
        for name, number in (("甲", 101), ("乙", 102), ("丙", 103), ("丁", 104)):
            self.ingest([self.write_note(f"项目{name} PRJ-{number} 使用RAG完成交付。", f"项目{name}交付.md")],
                        default_doc_type="交付记录", default_groups=[] if name == "丁" else ["all"],
                        default_allowed_users=[self.develop] if name == "丁" else [])
        self.ingest([self.write_note("项目计划只比较传统搜索，最终没有采用向量知识库。", "其他交付.md")],
                    default_doc_type="交付记录")

    def measure(self, case, mode):
        self.local.extra = {}
        self.local.result = None
        user = case.user or self.finance
        args = {"doc_type": case.doc_type} if case.doc_type else {}
        history = [{"role": "user", "content": "请按知识库文件中的证据核对。"}]
        first_delta = None
        started = perf_counter()
        if mode == "web":
            result, deltas = pipeline.answer_stream(
                case.query, top_k=4, history=history, filters=args,
                user_groups=self.groups[user], username=user, config=self.cfg)
            for delta in deltas:
                if first_delta is None and delta.strip():
                    first_delta = run.ms(perf_counter() - started)
            sources = result.sources
            answer = result.answer or ""
            items = result.enumerated_items
        else:
            tool = (mcp_server._tool_knowledge_search_impl if mode == "mcp_search"
                    else mcp_server._tool_knowledge_qa_impl)
            payload = json.loads(tool(case.query, top_k=4, history=history, ctx=self.ctx(user), **args))
            result = self.local.result
            if result is None or payload.get("error"):
                raise RuntimeError(payload.get("error") or "MCP 未返回结果")
            sources = payload.get("matches", payload.get("sources", []))
            answer = payload.get("answer") or ""
            items = payload.get("items", [])
        wall = run.ms(perf_counter() - started)
        recorded = {row["rank"]: row.get("text", "") for row in result.sources}
        serialized = {row["rank"]: row.get("text", row.get("excerpt", "")) for row in sources}
        citations = pipeline.answer_citations(answer)
        evidence = {
            key: sorted(rank for rank, text in recorded.items() if key in text and key in serialized.get(rank, ""))
            for key in case.expected
        }
        has_answer = mode != "mcp_search"
        if case.kind == "enumeration":
            entries = {row["identity"] for row in items if row.get("identity")}
            grounded = not has_answer or (
                entries == set(case.expected) and all(citations & set(evidence[key]) for key in case.expected))
        else:
            entries = set()
            grounded = not has_answer or all(
                any(f"{key} [{rank}]" in answer for rank in evidence[key]) for key in case.expected)
        denied = any("PRJ-104" in text for text in recorded.values())
        passed = (all(evidence.values()) and grounded and not denied and not (citations - serialized.keys())
                  and not result.cache_hit)
        phases = self.timings(result)
        onnx_calls = phases.pop("onnx_calls", [])
        return {"case": case.name, "kind": case.kind, "mode": mode, "passed": passed,
                "wall_ms": wall, "first_delta_ms": first_delta, "stage_ms": phases,
                "onnx_calls": onnx_calls, "evidence": evidence, "citations": sorted(citations),
                "source_names": [row["source"] for row in sources], "enumerated": sorted(entries),
                "denied_evidence": denied, "cache_hit": result.cache_hit,
                "route": result.evidence_meta.get("route"),
                "coverage": result.evidence_meta.get("coverage"),
                "stage2_calls": (result.evidence_meta.get("stage2") or {}).get("calls")}

    def set_width(self, original, width):
        self.cfg = replace(original, rerank_recall_k=width)
        return mock.patch("src.config.get_config", return_value=self.cfg), mock.patch(
            "src.pipeline.get_config", return_value=self.cfg)

    def latency_grid(self, original):
        case = Case("巡检恢复时限", "fact", run.QUESTION, "巡检手册", (run.CURRENT,))
        rows = []
        self.measure(case, "web")
        for trial in range(3):
            concurrency = (1, 2, 4)
            for index, workers in enumerate(concurrency[trial:] + concurrency[:trial]):
                widths = (6, 4) if (trial + index) % 2 == 0 else (4, 6)
                for width in widths:
                    config_patch, pipeline_patch = self.set_width(original, width)
                    with config_patch, pipeline_patch, ThreadPoolExecutor(max_workers=workers) as pool:
                        for offset in range(0, 4, workers):
                            barrier = Barrier(workers)

                            def task(i):
                                barrier.wait()
                                mode = "web" if (i + trial) % 2 == 0 else "mcp_search"
                                return self.measure(case, mode)

                            start = perf_counter()
                            futures = [pool.submit(task, offset + i) for i in range(workers)]
                            batch = [future.result() for future in futures]
                            batch_ms = run.ms(perf_counter() - start)
                            for i, row in enumerate(batch):
                                rows.append({"trial": trial, "workers": workers, "width": width,
                                             "batch_ms": batch_ms if i == 0 else 0, **row})
            print(f"latency round {trial + 1}/3 completed", flush=True)
            self.save({"latency_requests": rows})
        return rows

    def quality_cases(self, original):
        cases = [
            Case("巡检恢复时限", "fact", run.QUESTION, "巡检手册", (run.CURRENT,)),
            Case("财务签批周期", "fact", "出差内审 FINA-119 的签批周期是多少？",
                 "内部差旅", ("AUDGOLD_FINANCE",)),
            Case("幻灯片操作窗口", "fact", "故障切换 SLIDE-308 的操作窗口是什么？",
                 "故障切换", ("AUDGOLD_SLIDE",)),
            Case("受限员工审批", "fact", "受限审批 SAFE-716 的审批人是谁？",
                 "受限审批", ("AUDGOLD_RESTRICTED",), user=self.develop),
            Case("语义改写与近似文档", "semantic", "网络掉线时，晚班领门禁的纸条谁负责收好？",
                 "通行保管", ("AUDGOLD_CUSTODY",)),
            Case("双事实复合问题", "compound", COMPOUND, "切换运维",
                 ("AUDGOLD_POWER", "AUDGOLD_BACKUP")),
            Case("跨文档交付枚举", "enumeration", ENUMERATION, "交付记录",
                 ("PRJ-101", "PRJ-102", "PRJ-103")),
        ]
        rows = []
        for index, case in enumerate(cases):
            for mode in case.modes:
                for width in ((6, 4) if index % 2 == 0 else (4, 6)):
                    config_patch, pipeline_patch = self.set_width(original, width)
                    with config_patch, pipeline_patch:
                        rows.append({"width": width, **self.measure(case, mode)})
        return rows

    def tail_control(self, original):
        case = Case("金标在粗排第 5 位", "tail_control", TAIL, "封签规程", ("AUDGOLD_SEAL",),
                    ("web", "mcp_search"))
        retrieve = get_retriever().retrieve

        def ordered(query, *, top_k, **kwargs):
            pool = retrieve(query, top_k=20, **kwargs)
            gold = [card for card in pool if "AUDGOLD_SEAL" in card.text]
            distractors = [card for card in pool if "AUDGOLD_SEAL" not in card.text]
            if len(gold) != 1 or len(distractors) < 5:
                raise AssertionError("对照缺少所需的真实索引卡片")
            return (distractors[:4] + gold + distractors[4:])[:top_k]

        rows = []
        with mock.patch.object(get_retriever(), "retrieve", side_effect=ordered):
            for mode in case.modes:
                for width in (6, 4):
                    config_patch, pipeline_patch = self.set_width(original, width)
                    with config_patch, pipeline_patch:
                        rows.append({"width": width, **self.measure(case, mode)})
        return rows

    @staticmethod
    def save(data):
        output = Path(__file__).with_name("rerank_candidate_results.json")
        output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_workflows(self):
        original = replace(self.cfg, enum_stage2_enabled=True, enum_rerank_docs=6, enum_expand_docs=6)
        self.cfg = original
        if not (original.rerank_model_dir / "model_quantized.onnx").is_file():
            self.fail("需要已安装本地 ONNX 精排模型")
        latency = self.latency_grid(original)
        quality = self.quality_cases(original)
        tail = self.tail_control(original)
        groups = defaultdict(list)
        for row in latency:
            groups[row["width"], row["workers"]].append(row)
        summaries = []
        for (width, workers), rows in sorted(groups.items()):
            summary = run.summarize(rows, sum(row["batch_ms"] for row in rows))
            onnx_rows = [call for row in rows for call in row["onnx_calls"]]
            summary["actual_candidates"] = sorted({call["count"] for call in onnx_rows})
            summary["onnx_failures"] = sum(not call["ok"] for call in onnx_rows)
            summary["by_interface"] = {}
            for mode in ("web", "mcp_search"):
                group = [row for row in rows if row["mode"] == mode]
                interface = run.summarize(group, sum(row["wall_ms"] for row in group))
                interface.pop("requests_per_second")
                summary["by_interface"][mode] = interface
            summaries.append({"width": width, "workers": workers, **summary})
        self.save({"latency": summaries, "latency_requests": latency, "quality_requests": quality,
                   "tail_control": tail, "documents": len(self.store.list_documents()),
                   "model_threads": original.rerank_threads, "top_k": 4,
                   "median_span_tokens": median(t for row in latency for c in row["onnx_calls"] for t in c["tokens"])})
        for row in summaries:
            self.assertEqual(row["actual_candidates"], [row["width"]])
            self.assertEqual(row["onnx_failures"], 0)


if __name__ == "__main__":
    run.ARGS = SimpleNamespace(model_dir=Path("models/bge-reranker-v2-m3-onnx").resolve())
    unittest.main()

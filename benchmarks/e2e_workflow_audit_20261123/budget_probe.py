"""Paired, isolated control for the ONNX candidate budget."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from statistics import median
from types import SimpleNamespace
import unittest
from unittest import mock

from . import run


class BudgetProbe(run.WorkflowAudit):
    def test_workflows(self):
        self.request("web", history=run.BASE_HISTORY)  # warm the model before measuring
        corrected = [
            {"role": "user", "content": "先讨论星云项目。"},
            {"role": "assistant", "content": "好的，先看星云项目。"},
            {"role": "user", "content": "更正为天枢项目。"},
        ]
        cases = [
            ("public", "web", dict(history=run.BASE_HISTORY)),
            ("public", "mcp_search", dict(history=run.BASE_HISTORY)),
            ("page", "web", dict(question="故障切换 SLIDE-308 的操作窗口是什么？",
                                 doc_type="故障切换", expected="AUDGOLD_SLIDE", history=run.BASE_HISTORY)),
            ("internal", "mcp_qa", dict(question="内部差旅 FINA-119 的签批周期是多少？",
                                       doc_type="内部差旅", expected="AUDGOLD_FINANCE", history=run.BASE_HISTORY)),
            ("restricted", "mcp_search", dict(question="受限审批 SAFE-716 的审批人是谁？",
                                              doc_type="受限审批", expected="AUDGOLD_RESTRICTED",
                                              user=self.develop)),
            ("correction", "web", dict(question="这个项目的预算是多少？", doc_type="预算",
                                     history=corrected, expected="AUDGOLD_TIANSHU",
                                     session_id="budget-probe", state={})),
            ("correction", "mcp_search", dict(question="这个项目的预算是多少？", doc_type="预算",
                                             history=corrected, expected="AUDGOLD_TIANSHU")),
        ]
        settings = (("six", True, 6), ("four", True, 4), ("off", False, 4))
        original = self.cfg
        rows = []
        for cycle in range(3):
            for index, (case, mode, kwargs) in enumerate(cases):
                offset = (cycle + index) % len(settings)
                for name, enabled, recall in settings[offset:] + settings[:offset]:
                    self.cfg = replace(original, rerank_enabled=enabled, rerank_recall_k=recall)
                    with (mock.patch("src.config.get_config", return_value=self.cfg),
                          mock.patch("src.pipeline.get_config", return_value=self.cfg)):
                        result = self.request(mode, use_rerank=enabled, **kwargs)
                    rows.append({"case": case, "mode": mode, "cycle": cycle,
                                 "setting": name, **result})
        sets = []
        for name, _, _ in settings:
            group = [row for row in rows if row["setting"] == name]
            public = [row for row in group if row["case"] == "public"]
            sets.append({"setting": name, "tests": len(group),
                         "correct": sum(row["passed"] for row in group),
                         "public_p50_ms": median(row["wall_ms"] for row in public),
                         "failed": sorted({f"{r['case']}/{r['mode']}" for r in group if not r["passed"]})})
        path = Path(__file__).with_name("budget_controls.json")
        path.write_text(json.dumps({"summary": sets, "requests": rows}, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        print(json.dumps(sets, ensure_ascii=False))


if __name__ == "__main__":
    run.ARGS = SimpleNamespace(model_dir=Path("models/bge-reranker-v2-m3-onnx").resolve())
    unittest.main()

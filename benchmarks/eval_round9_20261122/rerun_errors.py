#!/usr/bin/env python3
"""重跑 regression 中因 402 余额不足报错的题目，合并回 regression_results.json。

只重跑 error 非空的记录（C14~C20）；判分失败但已有完整答案的（如 C08）保持原样，
如实反映当时的真实结果。跑前备份原文件为 regression_results.json.bak。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
sys.path.insert(0, str(HERE))

import run_round9 as r9  # noqa: E402

RESULTS_FILE = HERE / "results" / "regression_results.json"


def main() -> None:
    data = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    errored_ids = [r["id"] for r in data["records"] if r.get("error")]
    if not errored_ids:
        print("没有需要重跑的 error 记录")
        return
    qs = [q for q in r9.EXAM if q["id"] in errored_ids]
    print(f"重跑 {len(qs)} 题: {errored_ids}")

    rerun = r9.run_inproc(qs, "rerun")
    by_id = {r["id"]: r for r in rerun}
    still_errored = [rid for rid, r in by_id.items() if r.get("error")]
    if still_errored:
        print(f"仍有 error，放弃合并: {still_errored}")
        return

    shutil.copy2(RESULTS_FILE, RESULTS_FILE.with_suffix(".json.bak"))
    data["records"] = [by_id.get(r["id"], r) for r in data["records"]]
    data["summary"] = r9.summarize(data["records"])
    data["rerun_note"] = {
        "rerun_ids": errored_ids,
        "reason": "首轮 402 LLMError（账户余额不足），余额恢复后重跑",
        "backup": "regression_results.json.bak",
    }
    RESULTS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    r9.log(f"rerun 汇总：{data['summary']}")


if __name__ == "__main__":
    main()

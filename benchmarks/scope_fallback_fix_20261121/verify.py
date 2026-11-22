# -*- coding: utf-8 -*-
"""scope 回退修复验证：5 个回退场景 + 2 个不回归场景（端到端，走真实流水线）。

预期：
  F1 磐石（未登记+语料有）  → 回退+顶部提示+答出 暂停/资金冻结
  F2 星辰（未登记+语料有）  → 回退+提示+答出 郑浩
  F3 银河（未登记+语料无）  → 回退+提示+诚实拒答（不编造）
  F4 修饰语（预算超过200万的项目）→ 回退+提示+答出 低空/昆仑/磐石+负责人
  F5 多项目状态（星云和天枢）→ 回退+提示+答出 进行中+风险
  N1 已登记项目（星云项目预算）→ 不回退，project_id 过滤生效，无提示行
  N2 negated（不要查星云项目的验收资料）→ 仍拦截
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.pipeline import answer_question  # noqa: E402
from src import auth_db  # noqa: E402
from src.permissions import visible_groups  # noqa: E402

CASES = [
    ("F1", "项目台账里磐石项目现在是什么状态？原因是什么？", "zhaocai",
     ["fallback", "notice", "暂停", "资金冻结"]),
    ("F2", "台账里星辰项目的负责人是谁？", "zhaocai",
     ["fallback", "notice", "郑浩"]),
    ("F3", "银河项目现在的预算是多少万元？", "wangfang",
     ["fallback", "notice", "refuse"]),
    ("F4", "台账里预算超过 200 万的项目有哪些？负责人分别是谁？", "zhaocai",
     ["fallback", "notice", "昆仑", "磐石"]),
    ("F5", "台账里星云和天枢两个项目的状态和风险分别是什么？", "zhaocai",
     ["fallback", "notice", "进行中", "需求变更"]),
    ("N1", "星云项目目前的立项预算是多少万元？", "wangfang",
     ["no_fallback", "135"]),
    ("N2", "不要查星云项目的验收资料，查别的", "wangfang",
     ["intercepted"]),
]


def groups_of(username):
    u = auth_db.get_user(username)
    return visible_groups(auth_db.department_permission_key(
        u.get("department"), u.get("department_id")))


def main():
    results = []
    for tag, question, username, expects in CASES:
        result = answer_question(question, user_groups=groups_of(username), username=username)
        answer = result.answer or ""
        meta = result.evidence_meta or {}
        fb = meta.get("scope_fallback")
        norm = answer.replace(" ", "")
        row = {"tag": tag, "question": question, "fallback": fb,
               "scope_issue": meta.get("scope_issue"),
               "has_notice_line": answer.startswith("> ℹ️"),
               "sources": [s.get("source") for s in result.sources][:4],
               "filters_project": (result.filters or {}).get("project_id"),
               "answer_head": answer[:150].replace("\n", " ")}
        checks = {}
        checks["fallback"] = bool(fb) if "fallback" in expects else (not fb if "no_fallback" in expects else True)
        if "fallback" in expects:
            checks["notice"] = row["has_notice_line"]
        if "no_fallback" in expects:
            checks["no_notice"] = not row["has_notice_line"]
        if "refuse" in expects:
            checks["refuse"] = any(m in answer for m in
                                   ("没有找到", "没有相关", "未找到", "知识库中没有", "无法回答", "查不到"))
        for fact in ("暂停", "资金冻结", "郑浩", "昆仑", "磐石", "进行中", "需求变更", "135"):
            if fact in expects:
                checks[fact] = fact.replace(" ", "") in norm
        if "intercepted" in expects:
            checks["intercepted"] = bool(meta.get("scope_issue"))
        row["checks"] = checks
        row["passed"] = all(checks.values())
        mark = "✅" if row["passed"] else "❌"
        print(f"{mark} [{tag}] fallback={bool(fb)} issue={meta.get('scope_issue')} "
              f"notice行={row['has_notice_line']} checks={checks}")
        print(f"   {row['answer_head']}")
        results.append(row)
    n_pass = sum(1 for r in results if r["passed"])
    print(f"\n通过 {n_pass}/{len(results)}")
    Path(__file__).with_name("results").mkdir(exist_ok=True)
    Path(__file__).with_name("results") / "scope_fallback_verify.json"
    import json
    (Path(__file__).resolve().parent / "results" / "scope_fallback_verify.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

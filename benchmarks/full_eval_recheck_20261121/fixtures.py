"""Synthetic ingest plan and the original answer scoring rules."""

ALL = ["all"]

PLAN = [
    ("work/星海员工手册_2024版.md",       ALL, [], "人力资源部", "lili"),
    ("work/星海财务报销制度_v2.1.md",     ["财务部", "研发部"], [], "财务部", "zhaocai"),
    ("work/星海薪酬福利制度.md",          [], ["wangfang", "lili"], "人力资源部", "lili"),
    ("work/星海信息安全管理制度.md",      ["研发部"], [], "研发部", "wangfang"),
    ("learning/星海RAG技术培训笔记.md",   ALL, [], "研发部", "wangfang"),
    ("learning/星海产品培训_基础篇.docx", ALL, [], "研发部", "wangfang"),
    ("projects/星云项目/星云项目章程.md", ["研发部"], [], "研发部", "wangfang"),
    ("projects/天枢项目/天枢项目立项报告.docx", ["研发部", "市场部"], [], "研发部", "wangfang"),
    ("projects/北斗项目/北斗项目总结报告.md", ["研发部"], [], "研发部", "wangfang"),
    ("reference/星海合同/华信数据采购合同_2024.pdf", ALL, [], "财务部", "zhaocai"),
    ("reference/星海表格/2024年度部门预算表.xlsx", ["财务部"], [], "财务部", "zhaocai"),
    ("reference/星海发布会/星海X-2000发布会纪要.pptx", ALL, [], "市场部", "liuye"),
    ("reference/星海简介/公司简介.html",  ALL, [], "人力资源部", "lili"),
    ("reference/星海评审/产品评审会议纪要.rtf", ALL, [], "研发部", "wangfang"),
    ("reference/星海运维/机房巡检通知.jpg", ALL, [], "研发部", "wangfang"),
    ("archive/星海旧版员工手册_2022.md",  ALL, [], "人力资源部", "lili"),
    ("work/边界测试_空文件.txt",          ALL, [], "研发部", "wangfang"),
]

def norm(text: str | None) -> str:
    """答案归一化：去空白，便于『135 万』『135万』两种写法命中。"""
    return (text or "").replace(" ", "").replace("\u3000", "")

def grade(q: dict, result) -> dict:
    exp = q.get("expect", {})
    answer = norm(result.answer)
    src_names = " || ".join(
        f"{s.get('path') or ''}|{s.get('source') or ''}" for s in result.sources)

    # —— 检索命中 ——
    sources_any = exp.get("sources_any", [])
    hits = [pat for pat in sources_any if pat in src_names]
    best_rank = None
    for pat in sources_any:
        for s in result.sources:
            joined = f"{s.get('path') or ''}|{s.get('source') or ''}"
            if pat in joined:
                best_rank = s.get("rank") if best_rank is None else min(best_rank, s.get("rank"))
    retrieval_hit = bool(hits) if sources_any else None

    # —— 来源数量下限（列举题） ——
    min_count_ok = None
    if "sources_min_count" in exp:
        min_count_ok = len(hits) >= exp["sources_min_count"]
        retrieval_hit = (retrieval_hit and min_count_ok) if retrieval_hit is not None else min_count_ok

    # —— 空来源约束（越权拒答题） ——
    empty_ok = None
    for pat in exp.get("sources_must_empty", []):
        if pat in src_names:
            empty_ok = False
        elif empty_ok is None:
            empty_ok = True

    # —— 召回越权约束 ——
    not_src_ok = None
    for pat in exp.get("must_not_sources", []):
        if pat in src_names:
            not_src_ok = False
        elif not_src_ok is None:
            not_src_ok = True

    # —— 答案必须包含 ——
    must_ok, must_miss = True, []
    for group in exp.get("answer_must", []):
        if not any(norm(alias) in answer for alias in group):
            must_ok = False
            must_miss.append(group[0])

    # —— 答案任一组命中 ——
    any_ok, any_miss = True, []
    for group in exp.get("answer_any_group", []):
        if not any(norm(alias) in answer for alias in group):
            any_ok = False
            any_miss.append(group[0])

    # —— 答案禁止出现 ——
    not_ok, not_violations = True, []
    for bad in exp.get("must_not", []):
        if norm(bad) in answer:
            not_ok = False
            not_violations.append(bad)

    # —— 拒答判定 ——
    refuse = exp.get("refuse")
    refuse_ok = None
    if refuse:
        markers = ("没有找到", "没有相关", "未找到", "没有权限", "无权", "没有可用于回答",
                   "知识库中", "查不到", "无法回答", "不掌握", "没有匹配")
        refuse_ok = any(m in answer for m in markers)

    checks = {"retrieval_hit": retrieval_hit, "sources_min_count": min_count_ok,
              "sources_must_empty": empty_ok, "must_not_sources": not_src_ok,
              "answer_must": must_ok, "answer_any": any_ok,
              "must_not": not_ok, "refuse": refuse_ok}
    checks = {k: v for k, v in checks.items() if v is not None}
    passed = all(checks.values())
    return {"checks": checks, "passed": passed,
            "best_rank": best_rank, "must_miss": must_miss,
            "any_miss": any_miss, "not_violations": not_violations}

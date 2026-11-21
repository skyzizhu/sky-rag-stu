"""Synthetic document facts and independently answerable retrieval cases."""

PROJECTS = [
    ("air", "低空监管平台", "城市治理", "城运部", "工务部", "低空", "UAV-A700", "17 秒", "GNSS 时钟漂移", "蓝湾调度中心", "AL-209", "18 次"),
    ("cold", "曙光冷链", "供应保障", "物流部", "城运部", "冷链", "COLD-K881", "4.8 摄氏度", "探头接头松动", "北港冷藏区", "CL-311", "12 次"),
    ("grid", "青禾配电", "城市治理", "工务部", "物流部", "馈线", "GRID-P602", "11 秒", "断路器位置信号抖动", "青禾东站", "PD-473", "21 次"),
    ("clinic", "星港分诊", "公共服务", "医疗部", "城运部", "分诊", "TRIAGE-F937", "23 秒", "扫码设备未配对", "星港夜诊区", "CLN-502", "16 次"),
    ("water", "澄溪水务", "环境监测", "环保部", "工务部", "水质", "WATER-W315", "0.42 NTU", "采样泵气泡干扰", "澄溪二号泵站", "WT-714", "24 次"),
    ("port", "海棠闸口", "供应保障", "物流部", "医疗部", "闸口", "PORT-L409", "31 秒", "车牌识别逆光", "海棠东闸口", "GT-818", "15 次"),
]

FILES = {
    "report": ("验收报告", "docx"),
    "plan": ("验收方案", "md"),
    "incident": ("故障复盘", "txt"),
    "ledger": ("运营台账", "xlsx"),
    "handover": ("交接纪要", "pptx"),
    "private": ("权限演练记录", "html"),
}

GENERAL = [
    ("g-contacts", "公共值班手册.md", "公共值班服务台编号为 HELP-780。办公网值班时间为 08:30 至 19:00。"),
    ("g-format", "文档命名规范.txt", "正式附件文件名使用 yyyyMMdd，加部门简称与版本号。归档文件编号使用 ARCH-42。"),
    ("g-supplier", "通用供应商入场指引.html", "临时供应商窗口在西门，资料预审期限为两个工作日。供应商临时凭证 SUP-092。"),
    ("g-forms", "通用差旅审批表.xlsx", "国内差旅预申请应至少提前 3 个工作日，临时变更由值班主管确认。"),
    ("g-disaster", "通用备份恢复演练.pptx", "通用备份演练恢复点目标为 45 分钟。演练范围只含通用测试环境。"),
    ("g-procurement", "通用采购流程.docx", "采购需求先登记需求单，再向询价组移交。通用审批编号 PROC-067。"),
]


def project_documents(project):
    key, name, direction, department, other, term, ticket, threshold, cause, site, device, runs = project
    return {
        "report": [f"{name}最终验收报告。{term}场景实测阈值为 {threshold}。",
                   f"验收工单编号为 {ticket}。记录的是现场实测值，计划值以验收方案为准。"],
        "plan": [f"{name}验收方案。现场应先做单点验证，再联调，最后组织签字。",
                 f"验收观察点设在 {site}，验收准备期须核对设备编号。"],
        "incident": [f"{name}复盘记录，影响{term}数据回传。根因是 {cause}。",
                     "应对动作是现场复核信号并补充监控，恢复后需要重新观察。"],
        "ledger": [f"{name}本期运营台账。设备编号为 {device}。",
                   f"本期正式验证合计 {runs}，数据来源为现场台账。" ],
        "handover": [f"{name}夜班交接纪要。交接地址为 {site}，交接时先核对现场记录。",
                     f"交接值班由{other}负责，验收报告与台账分别登记。"],
        "private": [f"{name}授权演练记录，仅供授权员工查看。内部演练口令 {key.upper()}-RESTRICT-271。",
                    "演练只在隔离环境验证访问授权。"],
    }


def questions():
    cases = []

    def add(level, kind, question, expected=None, facts=(), *, actor="auditor", filters=None,
            forbidden=(), expected_count=None):
        cases.append({
            "id": f"Q{len(cases)+1:03}", "level": level, "kind": kind, "question": question,
            "expected": expected or [], "facts": list(facts), "actor": actor,
            "filters": filters or {}, "forbidden": list(forbidden),
            "expected_count": expected_count,
        })

    for project in PROJECTS:
        key, name, direction, department, other, term, ticket, threshold, cause, site, device, runs = project
        doc = lambda kind: f"{key}-{kind}"
        add("简单", "精确编号", f"工单 {ticket} 对应的验收记录是什么？", [doc("report")], [ticket])
        add("简单", "单文档事实", f"{name}的故障根因是什么？", [doc("incident")], [cause])
        add("简单", "表格编号", f"查设备编号 {device} 的台账。", [doc("ledger")], [device])
        add("中等", "项目与类型", f"查「{name}」项目的验收资料", [doc("report"), doc("plan")],
            expected_count=2)
        add("中等", "同义改写", f"{name}上次出问题，调查后发现是哪个环节出了岔子？",
            [doc("incident")], [cause])
        add("中等", "权限命中", f"这个项目的权限演练口令是什么？", [doc("private")],
            [f"{key.upper()}-RESTRICT-271"], filters={"project": name, "doc_type": "权限演练记录"})
        add("复杂", "跨文档组合", f"「{name}」项目的验收实测阈值和本期验证次数分别是多少？",
            [doc("report"), doc("ledger")], [threshold, runs])
        add("复杂", "跨文档组合", f"「{name}」项目交接由谁值班，同时说明故障复盘记录里的根因。",
            [doc("handover"), doc("incident")], [other, cause])
        add("复杂", "权限拒绝", f"这个项目的权限演练口令是什么？", actor="guest",
            filters={"project": name, "doc_type": "权限演练记录"}, forbidden=[doc("private")])
        add("中等", "近主题无答案", f"「{name}」项目的年度采购预算和结算金额是多少？")

    for project in PROJECTS:
        key, name, direction, department, other, term, ticket, threshold, cause, site, device, runs = project
        add("复杂", "已知对象无答案", f"「{name}」项目是否记录了供应商违约赔付和退款办法？")

    add("简单", "通用资料", "值班服务台编号 HELP-780 在哪个手册里？", ["g-contacts"], ["HELP-780"])
    add("简单", "通用资料", "档案命名规范里的归档编号是什么？", ["g-format"], ["ARCH-42"])
    add("简单", "库外无答案", "银河舰队 ZQ-9876 的量子跃迁手册在哪里？")
    add("简单", "库外无答案", "员工食堂周五供应哪些甜点？")
    add("中等", "文档版本", "低空监管平台生效版验收阈值是多少？", ["air-report"], ["17 秒"],
        filters={"project": "低空监管平台", "doc_type": "验收报告", "status": "active"})
    add("中等", "历史版本", "低空监管平台历史版实测阈值是多少？", ["air-report@1.0"], ["29 秒"],
        filters={"project": "低空监管平台", "doc_type": "验收报告", "status": "expired"})
    add("中等", "部门归属", "请列出归属城运部的资料",
        ["air-report", "air-plan", "air-incident", "cold-handover", "clinic-handover"],
        filters={"department": "城运部"})
    add("中等", "空范围", "不存在的文档类型里有什么文件？", filters={"doc_type": "航天保险批单"})
    add("复杂", "业务方向与类型", "查城市治理方向的验收资料",
        ["air-report", "air-plan", "grid-report", "grid-plan"], expected_count=4)
    add("复杂", "业务方向与类型", "列出供应保障方向的验收资料",
        ["cold-report", "cold-plan", "port-report", "port-plan"], expected_count=4)
    add("复杂", "冲突范围", "查「低空监管平台」项目的验收报告", filters={"project": "曙光冷链"})
    add("复杂", "多项目暂不支持", "请查「低空监管平台」项目和「曙光冷链」项目的资料")
    for key, name, *_ in PROJECTS:
        selected = {"project": name, "doc_type": "权限演练记录"}
        add("中等", "授权对照", "内部演练的口令是什么？", [f"{key}-private"],
            [f"{key.upper()}-RESTRICT-271"], filters=selected)
        add("复杂", "拒绝对照", "内部演练的口令是什么？", actor="guest", filters=selected,
            forbidden=[f"{key}-private"])
    return cases

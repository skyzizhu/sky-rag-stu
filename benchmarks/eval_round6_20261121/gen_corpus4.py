# -*- coding: utf-8 -*-
"""R6 语料：10 个新长文本文件（凤凰项目/2026 计划/数据治理等），md 用 H1 章节
（配合 R3-b 一章一卡），docx 用 Heading 1，事实登记在 questions45_r6.json。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
K = ROOT / "knowledge"


def w(path: Path, data, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "wb":
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    print(f"  ✓ {path.relative_to(ROOT)}")


def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _build_pdf(pages: list[list[str]]) -> bytes:
    objs: list[bytes] = []

    def add(data: str) -> int:
        objs.append(data.encode("latin-1"))
        return len(objs)

    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids, content_ids = [], []
    for lines in pages:
        stream = ["BT /F1 11 Tf 72 760 Td 14 TL"]
        for line in lines:
            stream.append(f"({_pdf_escape(line)}) Tj T*")
        stream.append("ET")
        content_ids.append(add("<< /Length " + str(len(" ".join(stream))) + " >>\nstream\n"
                               + " ".join(stream) + "\nendstream"))
    pages_id = len(objs) + len(pages) + 1
    for cid in content_ids:
        page_ids.append(add(
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {cid} 0 R >>"))
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>")
    cat = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs)+1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs)+1} /Root {cat} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF").encode()
    return bytes(out)


# ---------------------------------------------------------------- A 数据治理（H1 长文）
w(K / "work" / "星海数据治理管理制度_v2.0.md", """---
title: 星海数据治理管理制度
doc_type: 管理制度
department: 研发部
---

# 星海数据治理管理制度（V2.0）

## 一、总则

本制度适用于公司全部业务数据的分级、脱敏、质量、主数据与生命周期管理。
数据治理的第一原则：数据有主、分级有据、流转留痕。各部门产生的新数据集
必须在接入时登记数据 Owner、数据分级与更新频率，未登记的数据集不得上线。

## 二、数据分级

公司数据分为四级：公开、内部、秘密、机密。

- 公开级：可对外发布的产品资料、新闻稿；
- 内部级：日常经营数据，限员工访问；
- 秘密级：财务明细、客户名单、未发布产品规划；
- 机密级：核心算法、定价成本、并购信息。

机密级数据外发必须经 CTO 审批；秘密级外发由数据 Owner 与部门负责人双签。
对外合作中需要提供秘密级及以上数据的，一律使用脱敏副本并签订保密协议。

## 三、数据脱敏

静态脱敏规则：手机号保留前 3 位与后 4 位、中间 4 位以 * 替代；
身份证号保留前 6 位与后 4 位、出生日期整体以 * 替代；银行卡号仅保留后 4 位。
测试与开发环境一律使用脱敏副本，禁止在生产数据上直接调试。
动态脱敏在查询出口按角色生效：普通员工看不到完整手机号与证件号。

## 四、数据质量

核心表字段完整率不低于 99%，代码值域合规率不低于 99.5%。
数据质量平台每月执行一次全库扫描，问题单派发到数据 Owner，
严重问题（如主键重复）须在 3 个工作日内修复。

## 五、主数据管理

客户、供应商、物料三类主数据由 MDM 平台统一管理，其他系统只读引用。
主数据新增与变更走 MDM 工单流程，两个工作日内完成审核。
任何系统不得私建客户或供应商主数据副本。

## 六、数据生命周期

业务操作数据在线保留 2 年，期满转入归档库再保留 5 年，到期由数据 Owner
评估销毁或延期。日志类数据在线保留 180 天。涉及诉讼的数据按法务要求冻结。

## 七、审批与流转

跨部门数据导出须数据 Owner 与安全组双审批，审批时限为 1 个工作日。
数据提供方负责留痕：谁、在何时、导出了什么、给了谁。
违反本制度造成数据泄露的，按《星海信息安全管理制度》处理。
""")


# ---------------------------------------------------------------- B 2026 经营计划（docx H1）
def gen_b():
    import docx
    d = docx.Document()
    d.add_heading("星海科技 2026 年度经营计划", level=0)
    d.add_paragraph("版本：V1.0　发布：2026-01-10　范围：全公司")

    d.add_heading("一、总体目标", level=1)
    d.add_paragraph("2026 年营业收入目标 3.2 亿元（2025 年目标 2.5 亿元），"
                    "净利率不低于 12%。全年计划新增员工 200 人，其中研发序列占比 60%。"
                    "核心人才年流失率控制在 12% 以内。")

    d.add_heading("二、三大战略", level=1)
    d.add_paragraph("战略一：X-4000 系列量产，抢占高端便携检测市场；"
                    "战略二：东南亚市场突破，海外营收占比达到 15%；"
                    "战略三：星云数据平台商业化，对外输出数据治理与检测云服务。")

    d.add_heading("三、部门预算", level=1)
    t = d.add_table(rows=7, cols=2)
    t.style = "Table Grid"
    rows = [("部门", "2026 预算（万元）"), ("研发部", "800"), ("市场部", "300"),
            ("人力资源部", "150"), ("财务部", "100"), ("行政部", "90"), ("合计", "1440")]
    for i, (a, b) in enumerate(rows):
        t.cell(i, 0).text = a
        t.cell(i, 1).text = b

    d.add_heading("四、重点项目", level=1)
    d.add_paragraph("2026 年三大重点投入项目：凤凰项目（预算 260 万元，X-4000 软件平台）、"
                    "星云二期（预算 180 万元，数据平台商业化改造）、"
                    "昆仑二期（预算 150 万元，设备管理平台增强）。")

    d.add_heading("五、主要风险", level=1)
    d.add_paragraph("供应链核心器件交期波动、汇率波动影响海外毛利、"
                    "高端算法人才竞争加剧。对策分别见采购与人力专项方案。")
    path = K / "work" / "星海2026年度经营计划.docx"
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(path))
    print("  ✓ work/星海2026年度经营计划.docx")


# ---------------------------------------------------------------- C 凤凰可研（docx H1）
def gen_c():
    import docx
    d = docx.Document()
    d.add_heading("凤凰项目可行性研究报告", level=0)
    d.add_paragraph("版本：V1.0　编制：研发部　日期：2025-12-20")

    d.add_heading("一、项目背景", level=1)
    d.add_paragraph("X-4000 便携式检测仪硬件由供应链团队负责，其软件子系统（设备接入、"
                    "告警联动、数据看板）立项为凤凰项目。项目与星云数据平台共享基础设施。")

    d.add_heading("二、项目基本信息", level=1)
    d.add_paragraph("项目负责人：沈青。项目预算：260 万元。项目周期：2026-01 至 2026-09。"
                    "参与部门：研发部、市场部。项目团队规模 12 人。")

    d.add_heading("三、关键技术指标", level=1)
    d.add_paragraph("支持 2000 台设备并发接入；核心接口响应时间不超过 300 毫秒（95 分位）；"
                    "系统可用性不低于 99.9%。")

    d.add_heading("四、财务测算", level=1)
    d.add_paragraph("项目总投入 260 万元，预计投产后 3 年累计创收 1200 万元，"
                    "投资回收期 2.2 年，内部收益率约 31%。")

    d.add_heading("五、风险与对策", level=1)
    d.add_paragraph("主要风险为核心算法人才招聘不及预期；对策为提前启动校招定向培养、"
                    "并与两家高校实验室建立联合培养机制。")
    path = K / "projects" / "凤凰项目" / "凤凰项目可行性研究报告.docx"
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(path))
    print("  ✓ projects/凤凰项目/凤凰项目可行性研究报告.docx")


# ---------------------------------------------------------------- D 项目管理手册（docx H1 公开）
def gen_d():
    import docx
    d = docx.Document()
    d.add_heading("星海项目管理手册", level=0)
    d.add_paragraph("版本：V1.0　适用：全公司项目")

    d.add_heading("一、立项管理", level=1)
    d.add_paragraph("预算 100 万元及以上的项目立项须经经营管理会议批准；"
                    "100 万元以下由分管副总经理批准。立项材料包含商业论证与资源计划。")

    d.add_heading("二、计划管理", level=1)
    d.add_paragraph("项目计划须做 WBS 分解，工作包颗粒度不超过 2 周。"
                    "里程碑基线一经确认，变更须提交变更控制委员会（CCB）批准。")

    d.add_heading("三、执行与报告", level=1)
    d.add_paragraph("项目周报每周五 17:00 前提交至 PMO；周报包含进度、风险与下周计划。"
                    "连续两周进度偏差超过 10% 的项目由 PMO 约谈项目经理。")

    d.add_heading("四、监控与预警", level=1)
    d.add_paragraph("成本绩效采用挣值管理（EVM）：CPI 低于 0.9 触发黄色预警，"
                    "低于 0.8 触发红色预警并强制制定纠偏计划。")

    d.add_heading("五、收尾管理", level=1)
    d.add_paragraph("项目验收通过后 30 天内完成经验总结、文档归档与团队释放。"
                    "未完成收尾的项目不得启动二期立项。")
    path = K / "learning" / "星海项目管理手册_v1.0.docx"
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(path))
    print("  ✓ learning/星海项目管理手册_v1.0.docx")


# ---------------------------------------------------------------- E 英文长 PDF（6 页）
def gen_e():
    pages = [
        ["PHOENIX PROJECT TECHNICAL PROPOSAL",
         "",
         "Document No: PHX-PROP-2026-007",
         "Date: 2026-01-15  Prepared by: R&D Department",
         "",
         "1. Scope",
         "Software subsystem for the X-4000 portable detector family:",
         "device access, alarm linkage and data dashboard modules.",
         "",
         "2. Architecture",
         "Microservices on Kubernetes, event bus based on Kafka,",
         "time-series storage with cold/hot separation.",
         "",
         "3. Service Level",
         "Uptime SLA: 99.9% per month.",
         "Core API latency: below 300 ms at the 95th percentile.",
         "Disaster recovery: RPO 5 minutes, RTO 30 minutes."],
        ["4. Delivery Plan",
         "",
         "Phase 1 (core access service): 2026-04-30",
         "Phase 2 (alarm linkage): 2026-06-30",
         "Phase 3 (dashboard + GA): 2026-09-15",
         "",
         "5. Team and Effort",
         "Core team of 8 engineers, total effort 120 person-months.",
         "",
         "6. Compliance",
         "MLPS Level 3; annual penetration test required;",
         "audit logs retained for 7 years."],
        ["7. Security Design",
         "",
         "7.1 All APIs authenticated by short-lived tokens;",
         "    service-to-service calls use mTLS.",
         "7.2 Sensitive fields encrypted with SM4 at rest.",
         "7.3 Data leaving the platform must be approved by",
         "    the data owner and the security team.",
         "",
         "8. Testing",
         "Unit test coverage >= 80%; regression suite runs nightly;",
         "load test at 1.5x peak before each release."],
        ["9. Budget Breakdown",
         "",
         "Total fixed price: RMB 2,600,000",
         "(120 person-months x RMB 20,000 plus RMB 200,000 buffer).",
         "",
         "10. Acceptance Criteria",
         "60-day trial run with zero P1 incidents;",
         "all Phase 3 deliverables accepted by 2026-09-15.",
         "",
         "11. Intellectual Property",
         "Custom code developed for Xinghai belongs to Xinghai;",
         "third-party components listed with licenses."],
        ["12. Assumptions",
         "",
         "12.1 Hardware samples available by 2026-02-01.",
         "12.2 Security review completed before Phase 2 starts.",
         "",
         "13. Change Control",
         "Scope changes follow the customer CCB process;",
         "effort impact above 10 person-days needs re-approval.",
         "",
         "Contact: phoenix-pm@xinghai-tech.example",
         "(end of proposal)"],
    ]
    w(K / "reference" / "星海合同" / "Phoenix技术方案建议书_2026.pdf",
      _build_pdf(pages), "wb")


# ---------------------------------------------------------------- F 2026 春季发布会（pptx 8 页）
def gen_f():
    from pptx import Presentation
    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[0])
    s1.shapes.title.text = "星海科技 2026 春季新品发布会纪要"
    s1.placeholders[1].text = "时间：2026-03-18　地点：杭州国际博览中心"

    s2 = prs.slides.add_slide(prs.slide_layouts[1])
    s2.shapes.title.text = "发布产品"
    tf = s2.placeholders[1].text_frame
    tf.text = "X-4000 便携式智能检测仪正式发布"
    p = tf.add_paragraph(); p.text = "标准版售价 21999 元"

    s3 = prs.slides.add_slide(prs.slide_layouts[1])
    s3.shapes.title.text = "旗舰版参数"
    tf = s3.placeholders[1].text_frame
    tf.text = "旗舰版售价 25999 元"
    p = tf.add_paragraph(); p.text = "电池续航 48 小时"
    p = tf.add_paragraph(); p.text = "整机重量 0.78 kg"
    p = tf.add_paragraph(); p.text = "支持 5G、卫星与 Mesh 三模通信"

    s4 = prs.slides.add_slide(prs.slide_layouts[1])
    s4.shapes.title.text = "渠道与开售"
    tf = s4.placeholders[1].text_frame
    tf.text = "官网商城 2026-03-25 开售"
    p = tf.add_paragraph(); p.text = "首批用户赠送三年质保"

    s5 = prs.slides.add_slide(prs.slide_layouts[1])
    s5.shapes.title.text = "合作媒体"
    tf = s5.placeholders[1].text_frame
    tf.text = "到场媒体 8 家：科技日报、电子发烧友、数码之家、充电头网、爱范儿、机锋网、极客公园、36氪"
    p = tf.add_paragraph(); p.text = "回放 2026-03-20 上线官网"

    s6 = prs.slides.add_slide(prs.slide_layouts[1])
    s6.shapes.title.text = "生态合作"
    tf = s6.placeholders[1].text_frame
    tf.text = "与两家省级检测机构签署数据合作协议"
    p = tf.add_paragraph(); p.text = "凤凰项目团队负责 X-4000 软件平台交付"

    s7 = prs.slides.add_slide(prs.slide_layouts[1])
    s7.shapes.title.text = "下一代规划"
    tf = s7.placeholders[1].text_frame
    tf.text = "X-5000 概念机预计 2027 年上半年亮相"
    p = tf.add_paragraph(); p.text = "聚焦无人化巡检场景"
    path = K / "reference" / "星海发布会" / "星海2026春季发布会纪要.pptx"
    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(path))
    print("  ✓ reference/星海发布会/星海2026春季发布会纪要.pptx")


# ---------------------------------------------------------------- G 2026 台账（xlsx 双 sheet）
def gen_g():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "项目台账"
    ws.append(["项目名称", "负责人", "预算（万元）", "状态", "周期"])
    for row in [("星云项目", "陈明", 135, "进行中", "2024-01~2024-09"),
                ("天枢项目", "周红", 200, "已结项", "2024-02~2025-01"),
                ("北斗项目", "陈明", 80, "已结项", "2023-07~2024-06"),
                ("昆仑项目", "林岚", 300, "进行中", "2025-03~2026-02"),
                ("沧海项目", "徐凯", 45, "已验收", "2025-01~2025-06"),
                ("星辰项目", "郑浩", 150, "已启动", "2025-09~2026-08"),
                ("磐石项目", "吴刚", 220, "已暂停", "资金冻结"),
                ("低空预警系统", "李强", 500, "进行中", "2024-05~2026-04"),
                ("凤凰项目", "沈青", 260, "进行中", "2026-01~2026-09"),
                ("星云二期", "陈明", 180, "筹备中", "2026-03 启动"),
                ("昆仑二期", "林岚", 150, "筹备中", "2026-04 启动"),
                ("灯塔项目", "孙倩", 90, "已暂停", "需求重大变更")]:
        ws.append(list(row))
    ws2 = wb.create_sheet("里程碑")
    ws2.append(["项目名称", "里程碑", "计划日期"])
    for row in [("凤凰项目", "M1 核心接入", "2026-04-30"),
                ("凤凰项目", "M2 告警联动", "2026-06-30"),
                ("凤凰项目", "M3 看板与GA", "2026-09-15"),
                ("星云二期", "M1 方案评审", "2026-06-30"),
                ("昆仑二期", "M1 需求冻结", "2026-05-31")]:
        ws2.append(list(row))
    path = K / "reference" / "星海表格" / "星海2026项目总台账.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    print("  ✓ reference/星海表格/星海2026项目总台账.xlsx")


# ---------------------------------------------------------------- H 供应商办法（H1 md 公开）
w(K / "work" / "星海供应商管理办法_2026修订.md", """---
title: 星海供应商管理办法（2026修订）
doc_type: 管理办法
department: 财务部
---

# 星海供应商管理办法（2026 修订）

## 一、准入

新供应商准入须三证齐全（营业执照、税务登记、组织机构代码），年采购额预计
50 万元及以上的供应商须实地考察。准入评审由采购、质量、财务三方会签。

## 二、分级

供应商按年度评审分为 A、B、C、D 四级：A 级优先合作并缩短账期，B 级正常合作，
C 级限制新增合作，D 级淘汰并移出名录。连续两次评审为 C 级的直接降为 D 级。

## 三、比价与定价

单次采购 30 万元及以上的须三家比价；单一来源采购须总经理批准并说明理由。
框架协议每年重谈一次价格，大宗原材料按季度随行就市调整。

## 四、付款

货款统一月结 60 天；质保金比例为合同金额的 5%，质保期满无质量问题后退还。
供应商发票逾期 30 天未送达的，顺延至下一付款周期。

## 五、考核

季度考核维度：交付及时率、质量合格率、响应速度、价格竞争力。
年度考核结果与分级联动，考核细则见采购部操作指引。
""")


# ---------------------------------------------------------------- I 归档旧差旅（2023）
w(K / "archive" / "星海旧版差旅制度_2023.md", """---
title: 星海差旅制度（2023旧版）
doc_type: 管理制度
---

# 星海差旅制度（2023 旧版，已废止）

> 本版本已废止，现行标准以 2025 版员工手册与差旅管理办法为准。

## 住宿标准（旧）

一线城市每晚 300 元，二线城市每晚 250 元，其他城市每晚 180 元。

## 餐费补贴（旧）

每人每天 80 元。

## 审批（旧）

出差审批须提前 3 个工作日提交纸质单据。
""")


# ---------------------------------------------------------------- J X-4000 需求（docx，凤凰项目）
def gen_j():
    import docx
    d = docx.Document()
    d.add_heading("X-4000 需求规格说明书", level=0)
    d.add_paragraph("版本：V1.0　编制：研发部　日期：2026-01-05　隶属：凤凰项目")

    d.add_heading("一、用户角色", level=1)
    d.add_paragraph("系统设四类角色：巡检员、班长、运维工程师、租户管理员。")

    d.add_heading("二、功能模块清单", level=1)
    t = d.add_table(rows=11, cols=3)
    t.style = "Table Grid"
    mods = [("REQ-FH-001", "设备接入", "X-4000 设备注册与心跳"),
            ("REQ-FH-002", "实时监测", "状态与采集数据实时展示"),
            ("REQ-FH-003", "告警管理", "阈值告警与升级通知"),
            ("REQ-FH-004", "告警联动", "告警自动派单与闭环"),
            ("REQ-FH-005", "工单调度", "工单分派与超时升级"),
            ("REQ-FH-006", "数据看板", "运营指标可视化"),
            ("REQ-FH-007", "报表导出", "日报周报月报导出"),
            ("REQ-FH-008", "租户管理", "多租户隔离与配额"),
            ("REQ-FH-009", "权限管理", "基于角色的访问控制"),
            ("REQ-FH-010", "审计日志", "操作留痕与检索")]
    for i, (no, name, desc) in enumerate(mods):
        t.cell(i, 0).text = no
        t.cell(i, 1).text = name
        t.cell(i, 2).text = desc

    d.add_heading("三、关键业务规则", level=1)
    d.add_paragraph("REQ-FH-015（告警联动规则，隶属告警联动模块）：同一设备同一告警类型"
                    "在 30 分钟内触发 2 次的，自动生成工单并派发给责任班组。")
    d.add_paragraph("工单 4 小时未响应自动升级至班长，8 小时未响应升级至租户管理员。")

    d.add_heading("四、非功能性要求", level=1)
    d.add_paragraph("支持 2000 台设备并发接入；核心接口响应时间不超过 300 毫秒（95 分位）；"
                    "系统可用性不低于 99.9%。与《凤凰项目可行性研究报告》保持一致。")

    d.add_heading("五、验收标准", level=1)
    d.add_paragraph("试运行 60 天零 P1 级故障方可申请验收；Phase 3 交付物 2026-09-15 前完成验收。")
    path = K / "projects" / "凤凰项目" / "X-4000需求规格_v1.0.docx"
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(path))
    print("  ✓ projects/凤凰项目/X-4000需求规格_v1.0.docx")


def main():
    print("生成 R6 语料 →", K)
    gen_b()
    gen_c()
    gen_d()
    gen_e()
    gen_f()
    gen_g()
    gen_j()
    print("完成：10 个新文件（2 长 md + 4 长 docx + 1 英文 5 页 PDF + 1 pptx + 1 xlsx + 1 归档 md）。")


if __name__ == "__main__":
    main()

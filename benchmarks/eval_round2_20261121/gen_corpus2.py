# -*- coding: utf-8 -*-
"""第二轮评测语料：12 个新文件（昆仑/沧海项目、2025 预算、维保合同、供应商名录等）。

事实均登记在 questions60.json 中；预算/负责人行采用「项目预算：N 万元」「项目负责人：X」
格式以匹配 project_comparison 通道的 project_facts 抽取正则。
"""
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


# ---- 极简英文 PDF 生成器（与第一轮相同实现，避免依赖被改造的 fixture 模块） ----
def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _build_simple_pdf(pages: list[list[str]]) -> bytes:
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


def gen_markdown():
    # 昆仑项目立项报告（内部：研发部+市场部，项目关联在入库时设置）
    w(K / "projects" / "昆仑项目" / "昆仑项目立项报告.md", """---
title: 昆仑项目立项报告
doc_type: 立项报告
department: 研发部
---

# 昆仑项目立项报告

## 项目基本信息

项目负责人：林岚。项目预算：300 万元。
项目周期 2025-03 至 2026-02，项目组常驻成都分支机构。
参与部门：研发部、市场部。

## 关键技术指标（KPI）

- 支持 1000 台检测设备并发接入；
- 数据上报延迟不超过 500ms；
- 系统可用性 99.95%。

## 里程碑

- M1 方案评审：2025-05；
- M2 开发完成：2025-10；
- M3 试运行：2026-01。

## 数据安全

平台按等级保护三级标准建设，上线前须完成第三方数据安全测评。
""")

    # 星海差旅管理办法（2025 修订）——与员工手册 2024 形成受控矛盾组
    w(K / "work" / "星海差旅管理办法_2025修订.md", """---
title: 星海差旅管理办法（2025修订）
doc_type: 管理办法
department: 财务部
---

# 星海差旅管理办法（2025 修订）

本办法自 2025-01-01 起施行，取代《员工手册》中的差旅报销标准条款。

## 住宿标准

- 一线城市（北京、上海、广州、深圳）：每晚 450 元；
- 二线城市（省会城市及计划单列市）：每晚 350 元；
- 其他城市：每晚 250 元。

## 补贴

餐费补贴每人每天 120 元。市内交通凭票据实报销，不再设上限。

## 审批

出差审批全部走 OA 系统，未经审批的差旅费用不予报销。
""")

    # 星海高管薪酬方案（受限名单：lili）
    w(K / "work" / "星海高管薪酬方案.md", """---
title: 星海科技高管薪酬方案
doc_type: 薪酬制度
department: 人力资源部
---

# 星海科技高管薪酬方案（保密）

适用对象：总经理、副总经理、财务总监。

## 年终奖

高管年终奖为 6~8 个月月薪，与公司年度利润目标达成率挂钩。

## 其他待遇

- 补充商业医疗保险：年度报销额度 20000 元；
- 期权激励另行签署协议，不在本方案范围内。
""")

    # 星海付款审批细则（内部：财务部）
    w(K / "work" / "星海付款审批细则.md", """---
title: 星海付款审批细则
doc_type: 管理制度
department: 财务部
---

# 星海付款审批细则

## 复核与会签

- 对外付款单据须双人复核（经办人 + 复核人）；
- 单笔 10 万元及以上的付款须总会计师会签。

## 付款周期

付款周期为 T+3：审批通过后第 3 个工作日付款。

## 备案

供应商首次付款前须完成合同与发票信息备案。
""")

    # 星海IT资产管理办法（公开，txt）
    w(K / "work" / "星海IT资产管理办法.txt", """星海IT资产管理办法

笔记本电脑使用年限为 4 年，到期由 IT 部统一评估更换。
显示器按双屏标准配置。员工离职时须归还全部 IT 资产，
由 IT 部与人力资源部共同核对后方可办理离职手续。
""")


def gen_archive():
    # 旧版考勤制度（archive 目录 → status=archive）——与 2024 手册作息矛盾组
    w(K / "archive" / "星海旧版考勤制度_2021.md", """---
title: 星海科技考勤制度（2021旧版）
doc_type: 管理制度
---

# 星海科技考勤制度（2021 旧版，已废止）

> 本版本已废止，仅留档备查。

## 作息时间

工作时间 8:30-17:30，午休 1 小时。
迟到一次罚款 20 元。

## 备注

2024 版员工手册已将工作时间调整为 9:00-18:00，并取消了迟到罚款。
""")


def gen_docx():
    import docx

    # 沧海项目验收报告（内部：研发部）
    d = docx.Document()
    d.add_heading("沧海项目验收报告", level=0)
    d.add_heading("一、项目基本信息", level=1)
    d.add_paragraph("项目负责人：徐凯。项目预算：45 万元。"
                    "项目周期 2025-01 至 2025-06，参与部门：研发部。")
    d.add_heading("二、验收结论", level=1)
    d.add_paragraph("项目于 2025-07-15 通过客户验收，交付物共 8 项，全部符合合同要求。")
    d.add_heading("三、遗留问题", level=1)
    d.add_paragraph("项目文档归档不齐全，需在 2025-09-30 前补齐并移交档案室。")
    path = K / "projects" / "沧海项目" / "沧海项目验收报告.docx"
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(path))
    print("  ✓ projects/沧海项目/沧海项目验收报告.docx")


def gen_xlsx():
    import openpyxl

    # 2025 年度部门预算表（内部：财务部）——与 2024 表构成跨年对比组
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "部门预算"
    ws.append(["部门", "2025年度预算（万元）", "负责人", "备注"])
    ws.append(["研发部", 600, "陈明", "含昆仑/沧海项目投入"])
    ws.append(["市场部", 220, "周红", "含海外展会费用"])
    ws.append(["人力资源部", 110, "孙倩", "含校招扩编费用"])
    ws.append(["财务部", 90, "钱进", "新增预算科目"])
    ws.append(["行政部", 75, "郑好", "含园区租金上涨"])
    ws.append(["合计", 1095, "", ""])
    ws2 = wb.create_sheet("编制说明")
    ws2["A1"] = "本预算表经 2025-01-20 经营管理会议审议通过，只限财务部内部使用。"
    ws2["A2"] = "2025 年新增财务部预算科目，行政部租金上调。"
    path = K / "reference" / "星海表格" / "2025年度部门预算表.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    print("  ✓ reference/星海表格/2025年度部门预算表.xlsx")

    # 供应商名录（内部：财务部，双 Sheet）
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "在册供应商"
    ws.append(["序号", "供应商名称", "类别", "所在地", "评级"])
    ws.append([1, "华信数据科技", "服务器与数据服务", "杭州", "A级"])
    ws.append([2, "蓝鲸冷链物流", "冷链运输", "上海", "B级"])
    ws.append([3, "红杉办公用品", "办公用品", "杭州", "B级"])
    ws2 = wb.create_sheet("黑名单")
    ws2.append(["供应商名称", "除名原因", "除名时间"])
    ws2.append(["中天咨询", "2024年数据违规", "2024-12"])
    path = K / "reference" / "星海表格" / "星海供应商名录.xlsx"
    wb.save(str(path))
    print("  ✓ reference/星海表格/星海供应商名录.xlsx")


def gen_pdf():
    pages = [
        ["SERVER MAINTENANCE SERVICE CONTRACT",
         "",
         "Contract No: OCEAN-SVC-2025-041",
         "Signing Date: 2025-06-18",
         "Buyer: Xinghai Technology Co., Ltd.",
         "Vendor: Deep Blue Services Ltd.",
         "",
         "1. Subject",
         "Maintenance service for the HX-Rack-500 server cluster",
         "(purchased under contract HX-2024-088).",
         "",
         "2. Service Fee",
         "The service fee is RMB 120,000 per year.",
         "",
         "3. Term",
         "The contract term is from 2025-07-01 to 2028-06-30",
         "(three years)."],
        ["4. Service Level",
         "SLA: 4-hour on-site response for critical failures.",
         "Monthly routine inspection is included.",
         "",
         "5. Payment",
         "The fee is paid annually in advance.",
         "",
         "6. Dispute Resolution",
         "Any dispute shall be submitted to arbitration in Shanghai.",
         "",
         "Signatures:",
         "Buyer: Xinghai Technology   Vendor: Deep Blue Services",
         "Date: 2025-06-18            Date: 2025-06-18"],
    ]
    w(K / "reference" / "星海合同" / "蓝鲸维保服务合同_2025.pdf",
      _build_simple_pdf(pages), "wb")


def gen_html():
    body = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>昆仑项目在成都正式启动</title></head>
<body>
<header><nav><a href="/">首页</a> | <a href="/news">新闻</a> | <a href="/ir">投资者关系</a></nav></header>
<article>
<h1>昆仑项目在成都正式启动</h1>
<p>2025-05-20，星海科技昆仑项目启动仪式在成都分支机构举行，
现场发布了 X-3000 便携式检测仪概念机。</p>
<p>昆仑项目由研发部与市场部联合推进，计划 2026 年初完成试运行，
投产后将服务西南地区 300 家工业客户。</p>
</article>
<footer>© 2025 星海科技 · 新闻中心 | 往期新闻：<a href="/news/2024">2024</a></footer>
</body></html>"""
    w(K / "reference" / "星海简介" / "昆仑项目启动新闻.html", body)


def gen_pptx():
    from pptx import Presentation

    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[0])
    s1.shapes.title.text = "星海科技 2025 年度规划汇报"
    s1.placeholders[1].text = "汇报人：周红　时间：2025-01-25"

    s2 = prs.slides.add_slide(prs.slide_layouts[1])
    s2.shapes.title.text = "经营目标"
    tf = s2.placeholders[1].text_frame
    tf.text = "全年目标营业收入 2.5 亿元"
    p = tf.add_paragraph(); p.text = "计划新增员工 150 人"

    s3 = prs.slides.add_slide(prs.slide_layouts[1])
    s3.shapes.title.text = "产品规划"
    tf = s3.placeholders[1].text_frame
    tf.text = "X-3000 正式版预计 2025 年下半年发布"
    p = tf.add_paragraph(); p.text = "X-2000 系列继续降价促销"

    s4 = prs.slides.add_slide(prs.slide_layouts[1])
    s4.shapes.title.text = "市场策略"
    tf = s4.placeholders[1].text_frame
    tf.text = "重点拓展东南亚海外市场"
    p = tf.add_paragraph(); p.text = "参加两次国际展会"
    path = K / "reference" / "星海发布会" / "星海2025年度规划汇报.pptx"
    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(path))
    print("  ✓ reference/星海发布会/星海2025年度规划汇报.pptx")


def main():
    print("生成第二轮语料 →", K)
    gen_markdown()
    gen_archive()
    gen_docx()
    gen_xlsx()
    gen_pdf()
    gen_html()
    gen_pptx()
    print("完成。共 12 个新文件（9 md/txt 类 + 1 docx + 2 xlsx + 1 pdf + 1 html + 1 pptx = 12）。")


if __name__ == "__main__":
    main()

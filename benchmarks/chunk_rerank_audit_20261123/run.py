"""Isolated chunk, ranking and evidence-budget audit. Run with python -m benchmarks.chunk_rerank_audit_20261123.run."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import replace
from pathlib import Path
from unittest import mock

from src import pipeline
from src.chunker import chunk_document, est_tokens
from src.cleaner import clean_document
from src.context import build_context, estimate_tokens
from src.enumeration import document_representatives, expand_documents
from src.parser import ParsedDocument
from src.reranker import rerank_llm, rerank_local
from src.retriever import RetrievedItem, _rrf_fuse
from tests._helpers import IngestHarness

AFTER = "--before" not in sys.argv


class Audit(IngestHarness):
    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, chunk_size=640, chunk_overlap=80, top_k=5,
                           context_max_per_doc=3, context_max_tokens=4000,
                           expand_doc_max_cards=12, rerank_recall_k=10)
        self.use_fakes()
        self.results = {}

    def record(self, name, passed, **evidence):
        self.results[name] = {"passed": bool(passed), **evidence}

    def chunks(self, text, source="样本.md", file_type="md"):
        parsed = ParsedDocument(text=text, metadata={
            "source": source, "document_id": "fixture", "file_type": file_type,
        })
        return chunk_document(clean_document(parsed), self.cfg)

    @staticmethod
    def item(index, text, doc="manual", version="1.0", section="流程", page=None):
        return RetrievedItem(rank=index + 1, score=1.0 / (index + 1), text=text, metadata={
            "document_id": doc, "source": doc + ".md", "version": version,
            "chunk_id": f"{doc}_v{version}_{index:04d}", "section": section,
            "section_path": section, "page": page, "status": "active",
        })

    def text_boundaries(self):
        chunks = self.chunks("# 验收\n\n" + "验" * 750 + "\n\n" + "收" * 750)
        lengths = [est_tokens(c.text) for c in chunks]
        self.record("C01_overlap_budget", max(lengths) <= 660,
                    budget=640, tolerance=20, card_tokens=lengths)

        text = "验收编号 ZX-DROP 必须保留。" + "长" * 1000 + "。收尾编号 ZX-TAIL。"
        chunks = self.chunks(text)
        joined = "\n".join(c.text for c in chunks)
        self.record("C02_pending_sentence_lost", "ZX-DROP" in joined and "ZX-TAIL" in joined,
                    previous_sentence_found="ZX-DROP" in joined, tail_found="ZX-TAIL" in joined)

        english = "The acceptance code is ALC-917. " + "This sentence documents a stage gate. " * 135
        cards = self.chunks(english, "acceptance.txt", "txt")
        endings = [c.text[-45:] for c in cards[:-1]]
        self.record("C03_english_sentence_cut", all(s.endswith(".") for s in endings),
                    endings=endings, card_count=len(cards))

        chunks = self.chunks("# 一章\n## 子节\n正文甲。\n# 二章\n## 子节\n正文乙。")
        self.record("C04_chapter_cross_merge", len(chunks) == 2,
                    sections=[c.metadata["section_path"] for c in chunks],
                    contents=[c.text for c in chunks])

        chunks = self.chunks("# 一章\n本章正文。\n## 短节甲\n事实甲。\n## 短节乙\n事实乙。")
        self.record("C05_section_merge_control",
                    len(chunks) == 1 and chunks[0].metadata.get("sections") == ["一章", "短节甲", "短节乙"],
                    coverage=chunks[0].metadata.get("sections"))

        chunks = self.chunks("# 安装\n\n~~~bash\n# comment is not a chapter\necho ready\n~~~\n\n## 正式启动\n执行程序。")
        names = [s for c in chunks for s in c.metadata.get("sections", [c.metadata["section"]])]
        self.record("C06_tilde_fence_headers", "comment is not a chapter" not in names, sections=names)

        text = ("<<<__RAG_PAGE__=1>>>\n# 验收步骤\n提交使用报告。\n"
                "<<<__RAG_PAGE__=2>>>\n由质量主管签字后生效。")
        chunks = self.chunks(text, "handover.pdf", "pdf")
        self.record("C07_continued_page_heading", chunks[1].metadata["section"] == "验收步骤",
                    locations=[(c.metadata["page"], c.metadata["section"]) for c in chunks])

        text = ("<<<__RAG_PAGE__=1>>>\nAll evidence requires manager approval.\nItem One\nPage 1\n"
                "<<<__RAG_PAGE__=2>>>\nAll evidence requires manager approval.\nItem Two\nPage 2\n"
                "<<<__RAG_PAGE__=3>>>\nItem Three\nAll evidence requires manager approval.\nPage 3")
        chunks = self.chunks(text, "repeat.pdf", "pdf")
        found_pages = [c.metadata["page"] for c in chunks if "requires manager approval" in c.text]
        self.record("C08_pdf_margin_heuristic_removes_body", "3" in found_pages, found_pages=found_pages)

        text = "验收口径\n========\n应提交完整说明。\n\n复核细则\n--------\n有两个人复核。"
        chunks = self.chunks(text)
        sections = [name for c in chunks for name in c.metadata.get("sections", [c.metadata["section"]])]
        self.record("C09_setext_heading", "验收口径" in sections and "复核细则" in sections,
                    sections=sections)

    def format_pipeline(self):
        from docx import Document
        from openpyxl import Workbook
        from pptx import Presentation
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        folder = self.knowledge / "learning"
        pdf = folder / "two-pages.pdf"
        writer = PdfWriter()
        for lines in (("# Handover", "Submit the acceptance record."), ("Approver is Alice.",)):
            page = writer.add_blank_page(width=600, height=800)
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({
                    NameObject("/F1"): DictionaryObject({
                        NameObject("/Type"): NameObject("/Font"),
                        NameObject("/Subtype"): NameObject("/Type1"),
                        NameObject("/BaseFont"): NameObject("/Helvetica"),
                    })
                })
            })
            stream = DecodedStreamObject()
            stream.set_data(("BT /F1 12 Tf 75 740 Td 20 TL "
                             + " T* ".join(f"({line}) Tj" for line in lines) + " ET").encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
        writer.write(str(pdf))
        pptx = folder / "slides.pptx"
        presentation = Presentation()
        for title, text in (("准备", "先录入材料。"), ("签收", "PPT-GOLD 校验通过。")):
            slide = presentation.slides.add_slide(presentation.slide_layouts[1])
            slide.shapes.title.text = title
            slide.placeholders[1].text = text
        presentation.save(str(pptx))
        docx = folder / "notes.docx"
        document = Document()
        document.add_heading("一章", 1)
        document.add_heading("实施条款", 2)
        document.add_paragraph("验收负责人为陈主任。")
        document.add_page_break()
        document.add_paragraph("DOCX-GOLD 记录在显式第二页。")
        document.save(str(docx))
        xlsx = folder / "register.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "风险台账"
        sheet.append(("编号", "风险描述", "负责人"))
        for index in range(9):
            sheet.append((f"R-{index + 1:02}", "联调问题待确认。" * 48, "责任人"))
        workbook.save(str(xlsx))
        workbook.close()
        html = folder / "fees.html"
        html.write_text("<main><h1>报销标准</h1><h2>交通费</h2><p>标准 500 元。</p>"
                        "<h2>住宿费</h2><p>标准 700 元。</p></main>", encoding="utf-8")
        txt = folder / "two-paragraphs.txt"
        txt.write_text("首次提交：\n\n表单和明细。\n\n第二次复核：\n\n部门签名。", encoding="utf-8")
        result = self.ingest([pdf, pptx, docx, xlsx, html, txt])
        self.record("F01_format_ingest", result.ok_files == 6,
                    indexed={row["source"]: row["chunks"] for row in result.file_rows},
                    failures=result.failed_files)
        by_source = {name: [row["payload"] for row in self.store.get_all_cards({"source": name})]
                     for name in (pdf.name, pptx.name, docx.name, xlsx.name, html.name, txt.name)}
        for name, expected, text in (
            (pdf.name, "2", "Approver is Alice."),
            (pptx.name, "2", "PPT-GOLD"),
            (docx.name, "2", "DOCX-GOLD"),
        ):
            hits = [row for row in by_source[name] if text in row["text"]]
            self.record("F02_page_" + name, bool(hits) and all(row["page"] == expected for row in hits),
                        indexed_locations=[(r["page"], r["section_path"]) for r in hits])
        hit = next(row for row in by_source[docx.name] if "DOCX-GOLD" in row["text"])
        self.record("F03_docx_heading_path", hit["section_path"] == "一章 > 实施条款",
                    actual=hit["section_path"])
        xcards = by_source[xlsx.name]
        broken_rows = [row["chunk_id"] for row in xcards
                       if re.search(r"R-\d\d", row["text"]) and "风险描述" not in row["text"]]
        self.record("F04_xlsx_split_keeps_columns", not broken_rows, cards=len(xcards), missing_header=broken_rows)
        self.record("F05_html_subheadings",
                    any({"交通费", "住宿费"} <= set(row.get("sections", ())) for row in by_source[html.name]),
                    sections=[row.get("sections") for row in by_source[html.name]])
        self.record("F06_txt_title_body", any("首次提交：" in row["text"] and "表单和明细。" in row["text"]
                                            for row in by_source[txt.name]))
        self.mcp_checks(by_source, pdf.name, html.name)
        self.retrieval_checks()

    def mcp_checks(self, by_source, pdf_source, html_source):
        from src.mcp_server import _tool_get_document_impl, _tool_knowledge_search_impl

        def get(name, **kwargs):
            return json.loads(_tool_get_document_impl([by_source[name][0]["document_id"]], **kwargs))

        response = get(pdf_source, page=2, page_size=1)
        second = response["documents"][0] if response.get("documents") else response
        fragment = second["fragments"][0]
        self.record("M01_mcp_fragment_correct_order", "Approver is Alice" in fragment["text"],
                    selected_pagination=second["page"], total_cards=second["total_cards"])
        self.record("M02_mcp_fragment_source_page", fragment.get("page") == "2",
                    fragment_fields=sorted(fragment), pagination_page=second["page"])
        matched = get(html_source, section="住宿费")
        self.record("M03_mcp_merged_subsection_lookup", matched["total"] == 1,
                    visible_document=bool(matched.get("section_not_matched")), matched=matched["total"])
        cfg = replace(self.cfg, query_understanding=False, hybrid_search=True, rerank_enabled=False)
        with mock.patch("src.config.get_config", return_value=cfg):
            found = json.loads(_tool_knowledge_search_impl("Approver Alice", top_k=3))
        maps = [match for match in found.get("matches", []) if "Approver is Alice." in match["text"]]
        self.record("M04_mcp_search_source_page", bool(maps) and maps[0].get("page") == "2",
                    matching_evidence=len(maps), match_fields=sorted(maps[0]) if maps else [])

    def retrieval_checks(self):
        from src.keyword_search import bm25_search

        questions = (
            ("Approver Alice", "two-pages.pdf", "2", "Approver is Alice."),
            ("PPT-GOLD 校验", "slides.pptx", "2", "PPT-GOLD"),
            ("DOCX-GOLD", "notes.docx", "2" if AFTER else None, "DOCX-GOLD"),
            ("报销标准 住宿费 700 元", "fees.html", None, "700 元"),
        )
        grades = []
        rr = pipeline.get_retriever()
        for question, source, page, text in questions:
            def rank(rows):
                return next((i for i, (body, meta) in enumerate(rows, 1)
                             if meta.get("source") == source and meta.get("page") == page and text in body), None)

            hybrid = rr.retrieve(question, top_k=5, use_hybrid=True)
            keyword = bm25_search(question, top_k=5)
            grades.append({"question": question,
                           "hybrid_rank": rank((hit.text, hit.metadata) for hit in hybrid),
                           "bm25_rank": rank((hit["payload"]["text"], hit["payload"]) for hit in keyword)})
        hits_at = {channel: {k: sum(row[channel] is not None and row[channel] <= k for row in grades)
                             for k in (1, 3, 5)}
                   for channel in ("hybrid_rank", "bm25_rank")}
        self.record("R01_isolated_recall_controls", all(row["hybrid_rank"] for row in grades),
                    grades=grades, hits_at=hits_at)

    def budgets(self):
        left = [self.item(i, f"同文档事实 {i}。") for i in range(5)]
        _, adopted, dropped, _ = build_context(left, self.cfg)
        self.record("B01_default_per_doc_limit", sum(len(hit.metadata["chunk_ids"]) for hit in adopted) == 3,
                    kept_fragments=len(adopted), dropped=dropped)

        pair = [self.item(0, "申请单须经财务部", page="3"),
                self.item(1, "部应于两日内确认。", page="3")]
        if AFTER:
            pair[1].metadata.update(overlap_chars=1, chunk_joiner="")
        context, adopted, _, _ = build_context(pair, self.cfg)
        self.record("B02_merge_removes_real_overlap", "财务部应于两日内确认" in context and len(adopted) == 1,
                    text=adopted[0].text)
        pair = [self.item(0, "提交审批A", page="3"), self.item(1, "A类适用特别规则。", page="3")]
        _, adopted, _, _ = build_context(pair, self.cfg)
        self.record("B03_merge_preserves_real_initial_letter", "审批A\nA类" in adopted[0].text,
                    observed=adopted[0].text)

        cfg = replace(self.cfg, context_max_tokens=500)
        context, adopted, _, _ = build_context([self.item(0, "大" * 1000)], cfg)
        self.record("B04_first_card_respects_budget", estimate_tokens(context) <= 500,
                    budget=500, observed_tokens=estimate_tokens(context))

        pair = [self.item(0, "完全相同的条款", version="1.0"), self.item(0, "完全相同的条款", version="2.0")]
        _, adopted, _, _ = build_context(pair, self.cfg, show_version=True)
        self.record("B05_version_attribution_after_dedup", len(adopted) == 2,
                    adopted_versions=[hit.metadata["version"] for hit in adopted])
        pair = [self.item(0, "跨页条件开头", page="1"), self.item(1, "跨页条件结尾", page="2")]
        _, adopted, _, _ = build_context(pair, self.cfg)
        self.record("B06_page_provenance_control", [hit.metadata["page"] for hit in adopted] == ["1", "2"],
                    locations=[hit.metadata["page"] for hit in adopted])

        def items_for(count, name):
            path = self.knowledge / "learning" / name
            path.write_text("\n\n".join(f"# 章 {i}\n\n第 {i} 项明细。" for i in range(1, count + 1)),
                            encoding="utf-8")
            self.ingest([path])
            rows = [row["payload"] for row in self.store.get_all_cards({"source": name})]
            return [RetrievedItem(i + 1, 1.0, p["text"], p)
                    for i, p in enumerate(sorted(rows, key=lambda r: r["chunk_id"]))]

        twelve = items_for(12, "short-manual.md")
        thirteen = items_for(13, "larger-manual.md")
        for label, items in (("B07_expand_twelve", twelve), ("B08_expand_thirteen", thirteen)):
            output, added, ids, notes = pipeline.expand_facts_dominant_documents(
                items[:2], {"status": "active"}, 5, self.cfg, True)
            expected = (2 < len(output) <= 4) if AFTER and len(items) == 13 else len(output) == len(items)
            self.record(label, expected, total_doc_cards=len(items), expanded=len(output),
                        added=added, notes=notes)
        output, added, ids, notes = pipeline.expand_facts_dominant_documents(
            twelve[:1], {"status": "active"}, 5, self.cfg, True)
        self.record("B09_multifact_single_anchor", len(output) > 1, expanded=len(output), added=added,
                    note="复合问题，仅命中同文档一张；实测候选池无法触发按文档补片")
        output, added, ids, notes = pipeline.expand_facts_dominant_documents(
            twelve[:4], {"status": "active"}, 2, self.cfg, True)
        context, adopted, _, _ = build_context(output, self.cfg, max_items=2 + added, unlimited_docs=ids)
        used_size = sum(len(hit.metadata["chunk_ids"]) for hit in adopted)
        correct = added == len(output) - 4 if AFTER else used_size <= 2
        self.record("B10_requested_top_two_vs_final", correct, requested=2,
                    added_counter=added, actual_new_cards=len(output) - 4,
                    adopted_cards=used_size, response_fragments=len(adopted))

        counts = []
        for k in (1, 2, 3, 5, 10):
            def recall(query, *, top_k, **kwargs):
                return [replace(hit) for hit in twelve[:top_k]]

            with mock.patch("src.pipeline.get_retriever") as retriever:
                retriever.return_value.retrieve.side_effect = recall
                result, _ = pipeline.query_evidence(
                    "手册主要说了什么？", top_k=k, filters={"status": "active"},
                    config=self.cfg, use_query_understanding=False, use_llm=False,
                    use_hybrid=False, use_rerank=False)
            counts.append({"top_k": k, "final_fragments": len(result.sources),
                           "original_cards": sum(len(row["chunk_ids"]) for row in result.sources)})
        self.record("B12_pipeline_topk_contract", all(row["original_cards"] <= row["top_k"] for row in counts),
                    counts=counts, note="固定按原文顺序返回候选，使用真实扩展与组装")

        representatives = document_representatives(twelve[:1])
        selections = []
        for attempt in range(3):
            if attempt:
                self.ingest([self.knowledge / "learning" / "short-manual.md"])
            selected, truncated = expand_documents(
                self.store, representatives, twelve[:1], {"status": "active"}, 4)
            selections.append([int(hit.metadata["chunk_id"].rsplit("_", 1)[1]) for hit in selected])
        self.record("B11_capped_enum_document_order",
                    all(seq == [0, 1, 2, 3] for seq in selections),
                    selected_sequences=selections, truncated=truncated)

        top, _ = _rrf_fuse([self.item(1, "仅向量"), self.item(0, "双命中")],
                           [self.item(0, "双命中")], top_k=1)
        self.record("R02_rrf_dual_recall_control", top[0].text == "双命中",
                    selected=top[0].text, channels=top[0].channels)

    def rerank(self):
        cfg = replace(self.cfg, rerank_backend="local_onnx", rerank_threads=4)
        topics = (
            ("验收负责人是谁？", "验收负责人", "陈静", "文档起草人", "赵主管"),
            ("Which port serves the staging gateway?", "staging gateway port", "9443", "production admin port", "8443"),
            ("采购单的复核时限是多少？", "采购单复核时限", "两个工作日", "归档时限", "十个工作日"),
            ("Which group may approve emergency releases?", "emergency release approval", "platform duty group",
             "routine release approval", "review board"),
            ("年度培训报销上限是多少？", "年度培训报销上限", "2600 元", "季度餐补上限", "900 元"),
            ("What is the recovery point objective for payment reconciliation?",
             "payment reconciliation recovery point objective", "15 minutes", "reporting archive retention", "90 days"),
        )
        grades = []
        for index, (question, correct_field, correct_value, related, wrong_value) in enumerate(topics):
            prefix = ("这里是通用背景材料，项目档案均由业务团队按流程维护。" if index % 2 == 0
                      else "This section records administrative background and document ownership. ") * 20
            tail = f"\n{correct_field}: {correct_value}."
            close = f"{related}: {wrong_value}."
            competitor = self.item(0, close, doc="competitor")
            gold = self.item(1, prefix + tail, doc="answer")
            before = rerank_local(question, [competitor, gold], 1, cfg)
            if AFTER:
                after = rerank_local(question, [competitor, replace(gold, text=gold.text[:1200])], 1, cfg)
            else:
                with mock.patch("src.reranker._LOCAL_DOC_CHARS", 4096):
                    after = rerank_local(question, [competitor, gold], 1, cfg)
            if not before.ok or not after.ok:
                self.results["R03_onnx_model"] = {"passed": None, "error": before.error or after.error}
                break
            grades.append({"question": question, "gold_start_char": len(prefix),
                           "within_local_window": len(prefix) + len(tail) <= 1200,
                           "gold_card_estimated_tokens": est_tokens(gold.text),
                           "current_top": before.items[0].metadata["document_id"],
                           ("old_window_control_top" if AFTER else "full_card_control_top"):
                               after.items[0].metadata["document_id"],
                           "current_scores": before.scores, "control_scores": after.scores,
                           "current_seconds": round(before.elapsed, 3),
                           "control_seconds": round(after.elapsed, 3),
                           "seconds": round(before.elapsed + after.elapsed, 3)})
        if grades:
            self.record("R03_onnx_model", all(row["current_top"] == "answer" for row in grades),
                        comparisons=grades)

        llm = mock.Mock()
        llm.chat.return_value = ('[{"id":"C1","score":9},{"id":"C2","score":0}]', {})
        gold = self.item(1, "通用说明。" * 70 + "对照事实 LLMTOKEN-987。")
        outcome = rerank_llm("对照事实是什么？", [self.item(0, "其他正文"), gold], 1, llm, self.cfg)
        self.record("R04_llm_preview_includes_tail", "LLMTOKEN-987" in outcome.user_prompt,
                    token_visible="LLMTOKEN-987" in outcome.user_prompt)

        llm.chat.return_value = ('[{"id":"C99","score":10}]', {})
        outcome = rerank_llm("测试无效编号", [self.item(0, "文档正文")], 1, llm, self.cfg)
        self.record("R05_llm_invalid_ids_detected", not outcome.ok, reported_ok=outcome.ok,
                    sorted_scores=outcome.scores)

    def test_audit(self):
        self.text_boundaries()
        self.format_pipeline()
        self.budgets()
        self.rerank()
        output = Path(__file__).with_name("results_after.json" if AFTER else "results.json")
        output.write_text(json.dumps(self.results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({name: evidence["passed"] for name, evidence in self.results.items()},
                         ensure_ascii=False))
        if AFTER:
            self.assertEqual([name for name, evidence in self.results.items() if not evidence["passed"]], [])


if __name__ == "__main__":
    import unittest

    unittest.main(argv=[sys.argv[0], "-v"])

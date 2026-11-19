"""Independent doc_type MCP/filter regressions; temporary data and no event loop/services."""
from __future__ import annotations

import functools
import inspect
import json
from types import SimpleNamespace
from unittest import mock

from qdrant_client import models

from src import mcp_server as ms
from src.permissions import scope_filters
from src.vector_store import INDEXED_FIELDS, VectorStore
from tests._helpers import IngestHarness


class DocumentTypeMcpTests(IngestHarness):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.addCleanup(self.store.client.close)
        self.store.client.create_collection(
            collection_name=self.cfg.qdrant_collection,
            vectors_config=models.VectorParams(size=self.embedding.DIM,
                                               distance=models.Distance.COSINE),
        )
        self.ctx = object()
        identity = mock.patch.object(ms, "_identity_from_ctx",
                                     return_value=(["hr"], "tester", {"department": "hr"}))
        self.identity = identity.start()
        self.addCleanup(identity.stop)
        for target in ("src.auth_db.log_doc_access", "src.mcp_server._log_qa"):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def seed_cards(self):
        """Include historical missing/null values and labels that must not be merged."""
        variants = [
            ("missing", {}), ("null", {"doc_type": None}), ("empty", {"doc_type": ""}),
            ("first", {"doc_type": "一期方案"}), ("second", {"doc_type": "二期方案"}),
            ("upper", {"doc_type": "Plan"}), ("lower", {"doc_type": "plan"}),
            ("hidden", {"doc_type": "一期方案", "allowed_groups": ["finance"]}),
            ("hidden_empty", {"doc_type": "", "allowed_groups": ["finance"]}),
            ("expired", {"doc_type": "一期方案", "status": "expired"}),
            ("expired_empty", {"doc_type": "", "status": "expired"}),
            ("work", {"doc_type": "一期方案", "domain": "work"}),
            ("work_empty", {"doc_type": "", "domain": "work"}),
            # Corrupt historical literal is not an alias for missing metadata.
            ("literal_sentinel", {"doc_type": "__unclassified__"}),
        ]
        points = []
        for i, (name, extra) in enumerate(variants, start=1):
            payload = {"document_id": name, "source": f"{name}.md",
                       "path": str(self.knowledge / "learning/general" / f"{name}.md"),
                       "domain": "learning", "category": "general", "status": "active",
                       "allowed_groups": ["all"], "version": "1.0", "title": name,
                       "chunk_id": f"{name}_0001", "section": "目标", "text": f"正文 {name}",
                       "indexed_at": "2026-11-19 10:00:00", **extra}
            points.append(models.PointStruct(id=i, payload=payload,
                                             vector=self.embedding.embed_query(name)))
        self.store.client.upsert(collection_name=self.cfg.qdrant_collection, points=points)

    def card_ids(self, filters):
        return {c["payload"]["document_id"] for c in self.store.get_all_cards(filters=filters)}

    def test_filters_normalize_exact_labels_without_aliases(self):
        self.assertEqual(ms._filters("learning", "active", doc_type=" 一期方案 "),
                         {"domain": "learning", "status": "active", "doc_type": "一期方案"})
        for value in (None, "", "  ", "all", " ALL "):
            with self.subTest(value=value):
                self.assertIsNone(ms._filters(None, None, doc_type=value))
        for value in ("一期方案", "二期方案", "Plan", "plan", "自定义资料", "类" * 80):
            with self.subTest(value=value):
                self.assertEqual(ms._filters(None, None, doc_type=value), {"doc_type": value})
        self.assertEqual(ms._filters(None, None, doc_type=" __unclassified__ "),
                         {"doc_type": "__unclassified__"})

    def test_invalid_filter_values_are_rejected_before_retrieval(self):
        invalid = (1, [], {}, True, "类" * 81, "需求\n文档", "\tall", "__unclassified__\n",
                   "文档\x00", "文档\u200b")
        implementations = [
            (ms._tool_knowledge_search_impl, ("q",)),
            (ms._tool_knowledge_qa_impl, ("q",)),
            (ms._tool_knowledge_digest_impl, ("q",)),
            (ms._tool_get_document_impl, (["doc"],)),
            (ms._tool_list_documents_impl, ()),
        ]
        with mock.patch("src.pipeline.answer_question") as answer, \
             mock.patch.object(self.store, "get_all_cards") as cards, \
             mock.patch.object(self.store, "list_documents") as docs:
            for value in invalid:
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        ms._filters(None, None, doc_type=value)
                for impl, args in implementations:
                    with self.subTest(value=value, tool=impl.__name__):
                        self.assertIn("error", json.loads(impl(*args, doc_type=value)))
            answer.assert_not_called()
            cards.assert_not_called()
            docs.assert_not_called()

    def test_unclassified_actual_local_filter_is_and_with_scope(self):
        self.seed_cards()
        filters = scope_filters(ms._filters("learning", "active", doc_type="__unclassified__"),
                                ["hr"])
        built = VectorStore.build_filter(filters)
        nested = next(c for c in built.must if isinstance(c, models.Filter))
        self.assertEqual(len(nested.should), 2)
        self.assertIsInstance(nested.should[0], models.IsEmptyCondition)
        self.assertEqual(nested.should[0].is_empty.key, "doc_type")
        self.assertEqual(nested.should[1].match.value, "")
        self.assertEqual(self.card_ids(filters), {"missing", "null", "empty"})
        catalog = self.store.document_catalog(filters)
        self.assertEqual({d["document_id"] for d in catalog["documents"]},
                         {"missing", "null", "empty"})
        self.assertTrue(all(d["doc_type"] == "" for d in catalog["documents"]))

    def test_concrete_actual_local_filter_keeps_exact_labels_and_scope(self):
        self.seed_cards()
        for label, expected in (("一期方案", {"first"}), ("二期方案", {"second"}),
                                ("Plan", {"upper"}), ("plan", {"lower"})):
            with self.subTest(label=label):
                filters = scope_filters(ms._filters("learning", "active", doc_type=label), ["hr"])
                self.assertEqual(self.card_ids(filters), expected)
        # Ordinary string/list/datetime conditions keep the pre-existing behavior.
        ordinary = VectorStore.build_filter({"topic": ["RAG", "Agent"], "source": "a.md",
                                              "status": None,
                                              "updated_at": {"from": "2026-01-01", "to": "2026-11-19"}})
        self.assertEqual(ordinary.must[0].match.any, ["RAG", "Agent"])
        self.assertEqual(ordinary.must[1].match.any, ["a.md"])
        self.assertIsInstance(ordinary.must[2].range, models.DatetimeRange)
        self.assertIsNone(VectorStore.build_filter({"topic": [], "status": None}))

    def test_metadata_projections_include_doc_type_and_legacy_blank(self):
        self.seed_cards()
        with mock.patch.object(self.store.client, "scroll", wraps=self.store.client.scroll) as scroll:
            listed = {d["document_id"]: d for d in self.store.list_documents()}
            self.assertIn("doc_type", scroll.call_args.kwargs["with_payload"])
        with mock.patch.object(self.store.client, "scroll", wraps=self.store.client.scroll) as scroll:
            catalog = {d["document_id"]: d for d in self.store.document_catalog()["documents"]}
            self.assertIn("doc_type", scroll.call_args.kwargs["with_payload"])
        for rows in (listed, catalog):
            for key in ("missing", "null", "empty"):
                self.assertEqual(rows[key]["doc_type"], "")
            self.assertEqual(rows["first"]["doc_type"], "一期方案")
            self.assertEqual(rows["upper"]["doc_type"], "Plan")
            self.assertEqual(rows["lower"]["doc_type"], "plan")
        # Reading a projection must not backfill or rewrite old payloads.
        legacy = next(c["payload"] for c in self.store.get_all_cards()
                      if c["payload"]["document_id"] == "missing")
        self.assertNotIn("doc_type", legacy)

    def test_doc_type_keyword_index_uses_existing_idempotent_ingest_setup(self):
        self.assertIn("doc_type", INDEXED_FIELDS)
        with mock.patch.object(self.store.client, "create_payload_index") as index, \
             mock.patch.object(self.store.client, "create_collection") as create, \
             mock.patch.object(self.store.client, "delete_collection") as delete:
            self.store.ensure_collection(self.embedding.DIM)
            self.store.ensure_collection(self.embedding.DIM)
        type_calls = [c for c in index.call_args_list if c.kwargs["field_name"] == "doc_type"]
        self.assertEqual(len(type_calls), 2)
        for call in type_calls:
            self.assertEqual(call.kwargs["field_schema"], models.PayloadSchemaType.KEYWORD)
            self.assertTrue(call.kwargs["wait"])
        create.assert_not_called()
        delete.assert_not_called()

    def test_search_qa_digest_forward_filter_and_serialize_raw_type(self):
        sources = [{"source": "first.md", "score": 0.12345, "doc_type": "一期方案"},
                   {"source": "old.md", "score": 0}, {"source": "null.md", "doc_type": None}]
        items = [SimpleNamespace(metadata={"document_id": s["source"], **s},
                                 text="原文", score=0.2, channels=["vector"])
                 for s in sources]

        def answer_result(*args, **kwargs):
            return SimpleNamespace(answer="回答", sources=sources, retrieved=items,
                                   filters=kwargs["filters"], evidence_meta={}, enumerated_items=[])

        implementations = [
            (ms._tool_knowledge_search_impl, (" q ", 5, "learning", "active", self.ctx), "matches"),
            (ms._tool_knowledge_qa_impl, (" q ", 5, "learning", "active", [], self.ctx), "sources"),
            (ms._tool_knowledge_digest_impl, (" q ", "learning", "active", 10, self.ctx), "sources"),
        ]
        with mock.patch("src.pipeline.answer_question", side_effect=answer_result) as answer:
            for impl, args, key in implementations:
                with self.subTest(tool=impl.__name__):
                    response = json.loads(impl(*args, doc_type=" 一期方案 "))
                    self.assertEqual(answer.call_args.kwargs["filters"],
                                     {"domain": "learning", "status": "active", "doc_type": "一期方案"})
                    self.assertEqual(answer.call_args.kwargs["user_groups"], ["hr"])
                    self.assertEqual([s["doc_type"] for s in response[key]], ["一期方案", "", ""])
                    self.identity.assert_called_with(self.ctx)
        self.assertEqual(ms._sources_payload([{"doc_type": "Plan"}])[0]["doc_type"], "Plan")

    def test_get_document_type_is_optional_and_active_permissions_remain(self):
        self.seed_cards()
        for mode in ("fragments", "full"):
            with self.subTest(mode=mode):
                result = json.loads(ms._tool_get_document_impl(
                    ["first", "second", "hidden", "expired"], 1, 5, mode, None, self.ctx,
                    doc_type="一期方案"))
                self.assertEqual(result["document_id"], "first")
                self.assertEqual(result["doc_type"], "一期方案")
                self.assertEqual(set(result["not_found"]), {"second", "hidden", "expired"})
                self.identity.assert_called_with(self.ctx)
        result = json.loads(ms._tool_get_document_impl(
            ["missing", "null", "empty", "first", "hidden_empty", "expired_empty"],
            doc_type="__unclassified__"))
        self.assertEqual({d["document_id"] for d in result["documents"]}, {"missing", "null", "empty"})
        self.assertTrue(all(d["doc_type"] == "" for d in result["documents"]))
        for label in (None, "", "all"):
            with self.subTest(label=label):
                result = json.loads(ms._tool_get_document_impl(
                    ["first", "second", "hidden", "expired"], doc_type=label))
                self.assertEqual({d["document_id"] for d in result["documents"]}, {"first", "second"})

    def test_list_documents_filters_locally_and_serializes_type(self):
        self.seed_cards()
        for label, expected in ((" 一期方案 ", {"first"}), ("二期方案", {"second"}),
                                ("Plan", {"upper"}), ("plan", {"lower"}),
                                ("__unclassified__", {"missing", "null", "empty"})):
            with self.subTest(label=label):
                response = json.loads(ms._tool_list_documents_impl(
                    "learning", "active", self.ctx, doc_type=label))
                self.assertEqual({d["document_id"] for d in response["documents"]}, expected)
                self.assertTrue(all("doc_type" in d for d in response["documents"]))
        # Explicit local checks must support old store/fake rows too, not only projected rows.
        rows = [{"document_id": key, "domain": "learning", "status": "active", **extra}
                for key, extra in (("missing", {}), ("null", {"doc_type": None}),
                                   ("empty", {"doc_type": ""}), ("concrete", {"doc_type": "需求文档"}))]
        with mock.patch.object(self.store, "list_documents", return_value=rows):
            response = json.loads(ms._tool_list_documents_impl(doc_type="__unclassified__"))
            self.assertEqual({d["document_id"] for d in response["documents"]},
                             {"missing", "null", "empty"})
            for label in (None, "", "all"):
                self.assertEqual(json.loads(ms._tool_list_documents_impl(doc_type=label))["total"], 4)

    def test_public_impl_signatures_and_keyword_forwarding_preserve_ctx_positions(self):
        cases = [
            ("knowledge_search", ("q", 3, "learning", "active", self.ctx)),
            ("knowledge_qa", ("q", 3, "learning", "active", [], self.ctx)),
            ("knowledge_digest", ("q", "learning", "active", 4, self.ctx)),
            ("get_document", (["doc"], 2, 7, "full", "章节", self.ctx)),
            ("list_documents", ("learning", "active", self.ctx)),
        ]
        for name, args in cases:
            public = getattr(ms, name)
            impl = getattr(ms, f"_tool_{name}_impl")
            for fn in (public, impl):
                params = list(inspect.signature(fn).parameters.values())
                self.assertEqual(params[-2].name, "ctx")
                self.assertEqual(params[-2].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                self.assertEqual(params[-1].name, "doc_type")
                self.assertEqual(params[-1].kind, inspect.Parameter.KEYWORD_ONLY)
                self.assertIsNone(params[-1].default)
                self.assertIn("doc_type:", fn.__doc__)

            async def run_inline(call):
                self.assertIsInstance(call, functools.partial)
                return call()

            with self.subTest(tool=name), \
                 mock.patch.object(ms, f"_tool_{name}_impl", return_value="ok") as called, \
                 mock.patch.object(ms.anyio.to_thread, "run_sync", new=run_inline):
                # Drive the mocked wrapper directly; no Windows asyncio socketpair required.
                for kwargs in ({}, {"doc_type": "一期方案"}):
                    coroutine = public(*args, **kwargs)
                    try:
                        with self.assertRaises(StopIteration) as ended:
                            coroutine.send(None)
                        self.assertEqual(ended.exception.value, "ok")
                    finally:
                        coroutine.close()
                    called.assert_called_with(*args, doc_type=kwargs.get("doc_type"))

    def test_sdk_public_schema_has_optional_doc_type_and_hides_ctx(self):
        from mcp.server.fastmcp.tools.base import Tool

        for name in ("knowledge_search", "knowledge_qa", "knowledge_digest", "get_document", "list_documents"):
            with self.subTest(tool=name):
                tool = Tool.from_function(getattr(ms, name))
                properties = tool.parameters["properties"]
                self.assertIn("doc_type", properties)
                self.assertNotIn("doc_type", tool.parameters.get("required", []))
                self.assertNotIn("ctx", properties)
                self.assertIsNone(properties["doc_type"]["default"])
                validated = tool.fn_metadata.arg_model.model_validate(
                    {"question": "q", "topic": "q", "document_ids": ["doc"], "doc_type": "自定义类型"})
                self.assertEqual(validated.doc_type, "自定义类型")
                self.assertEqual(tool.context_kwarg, "ctx")
                self.assertIn("__unclassified__", tool.description)

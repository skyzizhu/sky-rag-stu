"""测试公共夹具：把认证库、入库台账、向量库全部隔离到临时目录 / 内存。

运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -t . -v

铁律：测试绝不触碰真实的 storage/auth/auth.db、storage/index_state.json
与 Qdrant 服务——全部 patch 到临时目录与内存版 Qdrant（QdrantClient(":memory:")）。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import auth_db


def isolate_app_settings(language: str = "system"):
    """AppTest 隔离：把 src.app_settings 的读写路径指到临时 settings.json。

    app.py 经 Streamlit AppTest 在测试进程内真实执行，会读取（且界面交互会写入）
    项目根目录的真实 settings.json——不隔离时测试结果取决于开发者本机的
    界面语言等设置（曾导致 6 个界面用例在英文界面下失败）。

    返回 stop 函数：调用后解除 patch 并清理临时目录。
    用法（setUp 里）：stop = isolate_app_settings(); self.addCleanup(stop)
    """
    from src import app_settings

    tmp_dir = Path(tempfile.mkdtemp(prefix="sky-test-settings-"))
    tmp = tmp_dir / "settings.json"
    tmp.write_text(json.dumps({"language": language, "theme": "system"},
                              ensure_ascii=False), encoding="utf-8")
    p1 = mock.patch.object(app_settings, "SETTINGS_PATH", tmp)
    p2 = mock.patch.object(app_settings, "_LEGACY_PATH", tmp_dir / "legacy.json")
    p1.start()
    p2.start()

    def stop() -> None:
        p2.stop()
        p1.stop()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return stop


class TempAuthDB(unittest.TestCase):
    """每个测试一个全新的临时认证库（含独立签名密钥与 proof 消费记录）。"""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(auth_db, "AUTH_DIR", tmp),
            mock.patch.object(auth_db, "DB_PATH", tmp / "auth.db"),
            mock.patch.object(auth_db, "SECRET_PATH", tmp / "admin_secret.key"),
            mock.patch.object(auth_db, "_SECRET_CACHE", None),
            mock.patch.object(auth_db, "_used_proofs", {}),
        ]
        for p in self._patches:
            p.start()
        auth_db.init_db()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()
        super().tearDown()

    # ---- 便捷构造 ----
    def make_department(self, name: str = "hr") -> dict:
        return auth_db.create_department(name)

    def make_employee(self, username: str = "alice", password: str = "secret1",
                      department: str | None = "hr", **kwargs) -> dict:
        dep_id = None
        if department:
            existing = {d["name"]: d["id"] for d in auth_db.list_departments()}
            dep_id = existing.get(department) or self.make_department(department)["id"]
        return auth_db.create_user(username, password, department_id=dep_id, **kwargs)


def memory_store(config):
    """内存版 Qdrant 的 VectorStore：真实 qdrant-client API（含参数签名），零外部依赖。

    这正是今天 set_payload(points_selector=...) TypeError 能被拦住的关键：
    内存模式与服务端共用同一套客户端方法签名。
    """
    from qdrant_client import QdrantClient

    from src.vector_store import VectorStore

    store = VectorStore.__new__(VectorStore)
    store.cfg = config
    store.client = QdrantClient(":memory:")
    return store


class FakeEmbedding:
    """确定性的假向量化：同一文本永远同一向量，不依赖 Ollama。"""

    DIM = 8

    def model_available(self) -> bool:
        return True

    def dimension(self) -> int:
        return self.DIM

    def embed_texts(self, texts: list[str], batch_size: int = 8) -> list[list[float]]:
        import hashlib

        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vec = [b / 255.0 + 0.01 for b in digest[: self.DIM]]
            vectors.append(vec)
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """检索流水线对单条查询向量化走这个方法（与真实 EmbeddingClient 接口一致）。"""
        return self.embed_texts([text])[0]


class IngestHarness(unittest.TestCase):
    """入库全流程夹具：临时知识目录 + 临时台账 + 内存 Qdrant + 假向量化（关闭自动打标签）。"""

    def setUp(self) -> None:
        super().setUp()
        from dataclasses import replace

        from src import index_state as istate
        from src.config import get_config

        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.knowledge = root / "knowledge"
        (self.knowledge / "learning" / "general").mkdir(parents=True)
        self.cfg = replace(get_config(), knowledge_dir=self.knowledge, auto_tag=False,
                           qdrant_collection="test_kb", debug=False)
        self.store = memory_store(self.cfg)
        self.embedding = FakeEmbedding()
        patcher = mock.patch.object(istate, "STATE_PATH", root / "index_state.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        # 领域注册表同样隔离到临时文件：测试结果不能依赖真实 storage/domains.json
        from src import domains, index_epoch, keyword_search, pipeline
        from src.retriever import get_retriever

        for target, value in ((domains, root / "domains.json"),):
            reg_patcher = mock.patch.object(target, "REGISTRY_PATH", value)
            reg_patcher.start()
            self.addCleanup(reg_patcher.stop)
        epoch_patcher = mock.patch.object(index_epoch, "EPOCH_PATH", root / "index_epoch")
        epoch_patcher.start()
        self.addCleanup(epoch_patcher.stop)
        # 进程级单例与缓存必须每个用例重置：检索器是 lru_cache 单例，不清的话它会一直绑着
        # 上一个用例的内存向量库，检索测试查的就不是本用例的数据（曾让回归测试形同虚设）
        for reset in (get_retriever.cache_clear, keyword_search.invalidate_cache,
                      pipeline.clear_qa_cache):
            reset()
            self.addCleanup(reset)
        self.addCleanup(self._tmp.cleanup)

    def use_fakes(self) -> None:
        """检索 / 管理层的工厂函数全部指向本用例的临时配置、内存向量库与假向量化。"""
        for target in ("src.config.get_config", "src.pipeline.get_config", "src.retriever.get_config",
                       "src.keyword_search.get_config", "src.manage.get_config"):
            self._patch(target, self.cfg)
        for target in ("src.vector_store.get_vector_store", "src.pipeline.get_vector_store",
                       "src.retriever.get_vector_store", "src.keyword_search.get_vector_store",
                       "src.manage.get_vector_store"):
            self._patch(target, self.store)
        for target in ("src.embedding.get_embedding_client", "src.pipeline.get_embedding_client",
                       "src.retriever.get_embedding_client"):
            self._patch(target, self.embedding)

    def _patch(self, target: str, value) -> None:
        patcher = mock.patch(target, return_value=value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def retrieve_hits(self, query: str, domain: str | None = None,
                      user_groups: list[str] | None = None, top_k: int = 10) -> list[tuple[str, str]]:
        """走真实检索流水线（向量 + BM25 混合，不调大模型），返回命中的 (文件名, 领域编号)。"""
        import contextlib
        import io

        from src.pipeline import answer_question
        from src.retriever import get_retriever

        self.assertIs(get_retriever().store, self.store, "检索器必须查本用例的向量库")
        with contextlib.redirect_stdout(io.StringIO()):  # 检索流水线会打印耗时日志
            result = answer_question(query, top_k=top_k, use_llm=False,
                                     filters={"domain": domain} if domain else None,
                                     use_query_understanding=False, use_hybrid=True,
                                     use_rerank=False, config=self.cfg, user_groups=user_groups)
        return sorted({(s["source"], s["domain"]) for s in result.sources})

    def ingest(self, paths=None, **kwargs):
        import contextlib
        import io

        from src.pipeline import ingest_files

        with contextlib.redirect_stdout(io.StringIO()):  # 流水线进度打印不污染测试输出
            return ingest_files(paths=paths, config=self.cfg, embedding_client=self.embedding,
                                store=self.store, **kwargs)

    def write_note(self, body: str, name: str = "note.md", folder: str = "learning/general") -> Path:
        path = self.knowledge / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# 笔记\n\n{body}\n", encoding="utf-8")
        return path

    def versions(self, path_suffix: str | None = None) -> dict[str, str]:
        """{版本号: 状态}；path_suffix 指定只看某个文档。"""
        return {d["version"]: d["status"] for d in self.store.list_documents()
                if path_suffix is None or d["path"].endswith(path_suffix)}

    def ledger(self) -> dict:
        from src import index_state as istate

        return istate.load_state()

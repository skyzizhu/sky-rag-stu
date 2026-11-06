"""测试公共夹具：把认证库、入库台账、向量库全部隔离到临时目录 / 内存。

运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -t . -v

铁律：测试绝不触碰真实的 storage/auth/auth.db、storage/index_state.json
与 Qdrant 服务——全部 patch 到临时目录与内存版 Qdrant（QdrantClient(":memory:")）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import auth_db


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
        from src import domains

        reg_patcher = mock.patch.object(domains, "REGISTRY_PATH", root / "domains.json")
        reg_patcher.start()
        self.addCleanup(reg_patcher.stop)
        self.addCleanup(self._tmp.cleanup)

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

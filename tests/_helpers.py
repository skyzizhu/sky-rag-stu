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

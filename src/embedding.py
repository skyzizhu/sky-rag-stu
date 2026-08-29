"""节点 4：Embedding（向量化）。

产品视角：这一步相当于「给每张知识卡片算一组特征指纹」——
内容相近的卡片，指纹也相近。之后用户提问时，
把问题也算成指纹，比对指纹的相近程度，就能找到最相关的卡片。

要点：
- 文档和用户问题必须用同一个模型算指纹（否则没法比对）；
- 模型就是 Ollama 里那个 qwen3-embedding:4b，全程在你电脑本地运行。
"""

from __future__ import annotations

import sys
import time
from functools import lru_cache

import requests

from src.config import AppConfig, get_config


class EmbeddingError(Exception):
    """向量化失败时抛出，信息直接给人看。"""


class EmbeddingClient:
    """本地向量化客户端：和 Ollama 服务通信。"""

    def __init__(self, config: AppConfig | None = None):
        self.cfg = config or get_config()
        self._dimension: int | None = None
        self._session = requests.Session()

    # ---------- 底层请求 ----------
    def _request(self, method: str, path: str, json_body: dict | None = None, timeout: int = 300) -> dict:
        url = f"{self.cfg.ollama_url.rstrip('/')}{path}"
        try:
            resp = self._session.request(method, url, json=json_body, timeout=timeout)
        except requests.ConnectionError as exc:
            raise EmbeddingError(
                f"连不上本地 Ollama（{self.cfg.ollama_url}）。"
                "请确认 Ollama 已启动（终端运行 ollama list 能看到模型即正常）。"
            ) from exc
        except requests.Timeout as exc:
            raise EmbeddingError("Ollama 响应超时，模型可能太忙，稍后重试。") from exc
        if resp.status_code != 200:
            raise EmbeddingError(f"Ollama 返回错误 {resp.status_code}：{resp.text[:300]}")
        return resp.json()

    # ---------- 能力检查 ----------
    def model_available(self) -> bool:
        """检查配置的向量化模型是否已安装。"""
        data = self._request("GET", "/api/tags", timeout=10)
        names = [m.get("name", "") for m in data.get("models", [])]
        return self.cfg.embedding_model in names

    def dimension(self) -> int:
        """向量的维度（这个模型算出来的指纹有多少个数字）。探测一次后缓存。"""
        if self._dimension is None:
            vector = self.embed_query("维度探测")
            self._dimension = len(vector)
        return self._dimension

    # ---------- 核心能力 ----------
    def embed_texts(self, texts: list[str], batch_size: int = 8) -> list[list[float]]:
        """把一批知识卡片正文批量转成向量。顺序与输入一致。"""
        empty = [i for i, t in enumerate(texts) if not t or not t.strip()]
        if empty:
            raise EmbeddingError(f"第 {empty} 条文本是空的，空文本不能做向量化。")

        vectors: list[list[float]] = []
        total = len(texts)
        start = time.time()
        for begin in range(0, total, batch_size):
            batch = texts[begin : begin + batch_size]
            data = self._request(
                "POST",
                "/api/embed",
                {"model": self.cfg.embedding_model, "input": batch},
            )
            embeddings = data.get("embeddings")
            if not embeddings or len(embeddings) != len(batch):
                raise EmbeddingError("Ollama 返回的向量数量和输入不一致。")
            vectors.extend(embeddings)
            if self.cfg.debug and total > batch_size:
                print(f"    向量化进度：{min(begin + batch_size, total)}/{total}")
        if self.cfg.debug:
            print(f"    向量化完成：{total} 条，耗时 {time.time() - start:.1f} 秒")
        return vectors

    def embed_query(self, query: str) -> list[float]:
        """把用户的一个问题转成向量（和文档用同一个模型）。"""
        if not query or not query.strip():
            raise EmbeddingError("问题不能为空。")
        data = self._request("POST", "/api/embed", {"model": self.cfg.embedding_model, "input": [query]})
        embeddings = data.get("embeddings")
        if not embeddings:
            raise EmbeddingError("Ollama 没有返回向量结果。")
        return embeddings[0]


@lru_cache(maxsize=1)
def get_embedding_client() -> EmbeddingClient:
    """全项目共用一个客户端。"""
    return EmbeddingClient()


# ---------------- 直接运行本文件：向量化演示，看懂「指纹」 ----------------
if __name__ == "__main__":
    client = get_embedding_client()
    print(f"模型：{client.cfg.embedding_model}")
    print(f"模型已安装：{'✅' if client.model_available() else '❌（先 ollama pull ' + client.cfg.embedding_model + '）'}")

    samples = ["RAG 是检索增强生成的缩写", "RAG 指的是检索增强生成技术", "今天晚饭吃红烧牛肉面"]
    vectors = client.embed_texts(samples)
    print(f"\n向量维度：{len(vectors[0])}（每条文本都被算成 {len(vectors[0])} 个数字）")

    def cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        return dot / (sum(x * x for x in a) ** 0.5 * sum(y * y for y in b) ** 0.5)

    print(f"「RAG 是检索增强生成的缩写」vs「RAG 指的是检索增强生成技术」 相似度：{cosine(vectors[0], vectors[1]):.4f}  ← 应该很高")
    print(f"「RAG 是检索增强生成的缩写」vs「今天晚饭吃红烧牛肉面」     相似度：{cosine(vectors[0], vectors[2]):.4f}  ← 应该很低")
    sys.exit(0)

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Union


class EmbeddingError(RuntimeError):
    """Raised when the configured embedding service cannot be used."""


class VLLMEmbedder:
    dimensions = 1024
    # 真向量模型具备语义能力，可参与混合检索
    is_semantic = True

    def __init__(self) -> None:
        self.base_url = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1").rstrip("/")
        self.api_key = os.getenv("VLLM_API_KEY", "")
        self.model = os.getenv("VLLM_EMBEDDING_MODEL", "BAAI/bge-large-zh-v1.5")
        self.dimensions = int(os.getenv("EMBEDDING_DIMENSIONS", str(self.dimensions)))

    def _request(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.model, "input": texts}).encode()
        request = urllib.request.Request(
            f"{self.base_url}/embeddings", data=payload,
            headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=float(os.getenv("VLLM_TIMEOUT", "20"))) as response:
                data: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            vectors = [item["embedding"] for item in sorted(data["data"], key=lambda item: item.get("index", 0))]
            if any(len(vector) != self.dimensions for vector in vectors):
                raise EmbeddingError(f"embedding dimension must be {self.dimensions}")
            return [[float(value) for value in vector] for vector in vectors]
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise EmbeddingError("vLLM embedding request failed") from exc

    def embed_query(self, text: str, prefix: str = "") -> list[float]:
        # BGE 系列需要检索指令前缀，text-embedding / 多模态向量加了反而掉点
        if not prefix and "bge" in self.model.lower():
            prefix = "为这个句子生成表示以用于检索相关文章："
        return self._request([prefix + text])[0]

    def embed_texts(self, texts: list[str], batch_size: int = 16) -> list[list[float]]:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        return [vector for start in range(0, len(texts), batch_size)
                for vector in self._request(texts[start:start + batch_size])]

    def embed(self, text: str) -> list[float]:
        return self.embed_query(text, prefix="")


class HashEmbedder:
    """兼容性兜底，需显式设置 KNOWLEDGE_EMBEDDER=hash 才启用。

    只做哈希分桶，没有语义能力，给出的"相似度"接近随机。
    因此 retriever 会跳过向量通道、退化为纯 BM25：
    把随机排序等权融进 RRF 会把正确答案挤下去（实测「灯一直闪」会召回电视投屏）。
    """
    dimensions = 1024
    is_semantic = False

    def embed(self, text: str) -> list[float]:
        import hashlib, math, re
        vector = [0.0] * self.dimensions
        for token in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.lower()):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            vector[int.from_bytes(digest[:4], "big") % self.dimensions] += 1 if digest[4] & 1 else -1
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector

    def embed_query(self, text: str, prefix: str = "") -> list[float]:
        return self.embed(prefix + text)

    def embed_texts(self, texts: list[str], batch_size: int = 16) -> list[list[float]]:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        return [self.embed(text) for text in texts]


class DashScopeEmbedder:
    """阿里云百炼「通义多模态向量」原生接口。

    这类模型不在 OpenAI 兼容模式里（/compatible-mode/v1 会返回 404），
    必须走 /api/v1/services/embeddings/multimodal-embedding。
    请求体是 {"model": ..., "input": {"contents": [{"text": ...}]}}，
    响应是 {"output": {"embeddings": [{"index": 0, "embedding": [...]}]}}。
    """

    is_semantic = True

    def __init__(self) -> None:
        self.api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv("DASHSCOPE_API_KEY", "")
        self.base_url = os.getenv(
            "EMBEDDING_BASE_URL",
            "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
            "multimodal-embedding/multimodal-embedding")
        self.model = os.getenv("EMBEDDING_MODEL", "tongyi-embedding-vision-flash-2026-03-06")
        self.dimensions = int(os.getenv("EMBEDDING_DIMENSIONS", "768"))
        self.timeout = float(os.getenv("EMBEDDING_TIMEOUT", "40"))

    def _request(self, texts: list[str]) -> list[list[float]]:
        if not self.api_key:
            raise EmbeddingError("EMBEDDING_API_KEY 未配置")
        payload = {"model": self.model,
                   "input": {"contents": [{"text": text} for text in texts]}}
        request = urllib.request.Request(
            self.base_url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            items = data["output"]["embeddings"]
            items = sorted(items, key=lambda item: item.get("index", 0))
            vectors = [[float(value) for value in item["embedding"]] for item in items]
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError,
                IndexError, TypeError) as exc:
            raise EmbeddingError(f"百炼向量接口调用失败: {exc}") from exc
        if not vectors or len(vectors) != len(texts):
            raise EmbeddingError(f"向量数量不一致：请求 {len(texts)}，返回 {len(vectors)}")
        if any(len(vector) != self.dimensions for vector in vectors):
            raise EmbeddingError(
                f"向量维度不是 {self.dimensions}，请检查 EMBEDDING_MODEL 与 EMBEDDING_DIMENSIONS")
        return vectors

    def embed_query(self, text: str, prefix: str = "") -> list[float]:
        return self._request([prefix + text])[0]

    def embed(self, text: str) -> list[float]:
        return self.embed_query(text, prefix="")

    def embed_texts(self, texts: list[str], batch_size: int = 10) -> list[list[float]]:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        return [vector for start in range(0, len(texts), batch_size)
                for vector in self._request(texts[start:start + batch_size])]


def create_embedder() -> Union[VLLMEmbedder, HashEmbedder, DashScopeEmbedder]:
    backend = os.getenv("KNOWLEDGE_EMBEDDER", "vllm").lower()
    if backend == "hash":
        return HashEmbedder()
    if backend == "dashscope":
        return DashScopeEmbedder()
    return VLLMEmbedder()

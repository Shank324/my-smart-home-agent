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

    def __init__(self) -> None:
        self.base_url = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1").rstrip("/")
        self.api_key = os.getenv("VLLM_API_KEY", "")
        self.model = os.getenv("VLLM_EMBEDDING_MODEL", "BAAI/bge-large-zh-v1.5")

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

    def embed_query(self, text: str, prefix: str = "为这个句子生成表示以用于检索相关文章：") -> list[float]:
        return self._request([prefix + text])[0]

    def embed_texts(self, texts: list[str], batch_size: int = 16) -> list[list[float]]:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        return [vector for start in range(0, len(texts), batch_size)
                for vector in self._request(texts[start:start + batch_size])]

    def embed(self, text: str) -> list[float]:
        return self.embed_query(text, prefix="")


class HashEmbedder:
    """Compatibility fallback, explicitly selected with KNOWLEDGE_EMBEDDER=hash."""
    dimensions = 1024

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


def create_embedder() -> Union[VLLMEmbedder, HashEmbedder]:
    if os.getenv("KNOWLEDGE_EMBEDDER", "vllm").lower() == "hash":
        return HashEmbedder()
    return VLLMEmbedder()

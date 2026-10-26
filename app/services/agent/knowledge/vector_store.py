from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from .schemas import RetrievalResult, TextChunk


class VectorStoreError(RuntimeError):
    """Raised when the vector index cannot be used."""


class QdrantVectorStore:
    collection_name = "device_knowledge"
    dimensions = 1024

    def __init__(self, fallback_path: Optional[Path] = None) -> None:
        self.fallback_path = fallback_path or Path(os.getenv("KNOWLEDGE_STORE_PATH", ".data/knowledge.json"))
        self._items: list[tuple[TextChunk, list[float]]] = []
        self._client: Any = None
        try:
            if os.getenv("KNOWLEDGE_VECTOR_BACKEND", "json").lower() != "qdrant":
                self._models = None
                return
            from qdrant_client import QdrantClient
            from qdrant_client.http import models
            self._models = models
            self._client = QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"),
                                        api_key=os.getenv("QDRANT_API_KEY") or None)
            self.ensure_collection()
        except ImportError:
            self._models = None

    @property
    def backend(self) -> str:
        return "qdrant" if self._client else "json"

    def ensure_collection(self) -> None:
        if not self._client:
            return
        try:
            if not self._client.collection_exists(self.collection_name):
                self._client.create_collection(
                    self.collection_name,
                    vectors_config=self._models.VectorParams(size=1024, distance=self._models.Distance.COSINE),
                )
        except Exception as exc:
            raise VectorStoreError("cannot initialize Qdrant collection device_knowledge") from exc

    def replace(self, items: list[tuple[TextChunk, list[float]]]) -> None:
        self._items = items
        if self._client:
            points = [self._models.PointStruct(id=i, vector=vector, payload=chunk.model_dump())
                      for i, (chunk, vector) in enumerate(items)]
            try:
                self._client.delete_collection(self.collection_name)
                self.ensure_collection()
                self._client.upsert(self.collection_name, points=points)
            except Exception as exc:
                raise VectorStoreError("cannot write Qdrant collection") from exc
        else:
            self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
            self.fallback_path.write_text(json.dumps(
                [{"chunk": c.model_dump(), "vector": v} for c, v in items], ensure_ascii=False), encoding="utf-8")

    def search(self, vector: list[float], top_k: int, filter_conditions: Optional[dict[str, Any]] = None) -> list[RetrievalResult]:
        if self._client:
            query_filter = None
            if filter_conditions:
                query_filter = self._models.Filter(must=[
                    self._models.FieldCondition(key=k, match=self._models.MatchValue(value=v))
                    for k, v in filter_conditions.items()])
            try:
                points = self._client.search(self.collection_name, vector, limit=top_k, query_filter=query_filter)
                return [self._result(TextChunk.model_validate(point.payload), float(point.score))
                        for point in points]
            except Exception as exc:
                raise VectorStoreError("Qdrant search failed") from exc
        if not self._items and self.fallback_path.exists():
            raw = json.loads(self.fallback_path.read_text(encoding="utf-8"))
            self._items = [(TextChunk.model_validate(item["chunk"]), item["vector"]) for item in raw]
        results = []
        for chunk, candidate in self._items:
            if filter_conditions and any(chunk.metadata.get(k) != v for k, v in filter_conditions.items()):
                continue
            score = sum(a * b for a, b in zip(vector, candidate))
            results.append(self._result(chunk, score))
        return sorted(results, key=lambda item: item.score, reverse=True)[:top_k]

    @staticmethod
    def _result(chunk: TextChunk, score: float) -> RetrievalResult:
        return RetrievalResult(content=chunk.content, score=score, source_doc=chunk.source,
                               section=" > ".join(chunk.section_path), vector_score=score)

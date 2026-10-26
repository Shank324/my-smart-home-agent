from __future__ import annotations

import os
import re
import math
from pathlib import Path
from typing import Optional

from .embedder import create_embedder
from .loader import load_directory
from .schemas import DocumentMeta, RetrievalResult
from .splitter import split_document
from .vector_store import QdrantVectorStore


class KnowledgeRetriever:
    def __init__(self, data_dir: Optional[Path] = None, store_path: Optional[Path] = None) -> None:
        self.data_dir = data_dir or Path(os.getenv("KNOWLEDGE_DATA_DIR", "seed_data"))
        self.store = QdrantVectorStore(store_path)
        self.embedder = create_embedder()
        self.chunks = []
        self.documents_meta: list[DocumentMeta] = []
        self._tokens: list[list[str]] = []

    def rebuild(self) -> int:
        items = []
        self.documents_meta = []
        self.chunks = []
        for document, content in load_directory(self.data_dir):
            self.documents_meta.append(document)
            self.chunks.extend(split_document(document, content))
        vectors = self.embedder.embed_texts([chunk.content for chunk in self.chunks], batch_size=16)
        for chunk, vector in zip(self.chunks, vectors):
            items.append((chunk, vector))
        self.store.replace(items)
        self._tokens = [self._tokenize(chunk.content) for chunk in self.chunks]
        return len(self.chunks)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        try:
            import jieba
            return list(jieba.cut(text.lower()))
        except ImportError:
            return re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.lower())

    def _bm25(self, query: str, top_k: int) -> dict[str, float]:
        query_tokens = set(self._tokenize(query))
        if not query_tokens or not self._tokens:
            return {}
        average_length = sum(len(tokens) for tokens in self._tokens) / len(self._tokens)
        scores = {}
        document_count = len(self._tokens)
        for chunk, tokens in zip(self.chunks, self._tokens):
            length = len(tokens) or 1
            score = 0.0
            for token in query_tokens:
                frequency = tokens.count(token)
                document_frequency = sum(token in candidate for candidate in self._tokens)
                if not frequency or not document_frequency:
                    continue
                idf = max(0.0, (document_count - document_frequency + 0.5) /
                          (document_frequency + 0.5))
                idf = math.log1p(idf)
                score += idf * frequency * 2.2 / (
                    frequency + 1.2 * (0.25 + 0.75 * length / max(average_length, 1)))
            scores[chunk.chunk_id] = score
        return dict(sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_k])

    def search(self, query: str, top_k: int = 5, device_model: Optional[str] = None) -> list[RetrievalResult]:
        if not self.chunks:
            self.rebuild()
        vector_results = self.store.search(self.embedder.embed_query(query), max(top_k * 3, top_k),
                                           {"device_model": device_model} if device_model else None)
        keyword = self._bm25(query, max(top_k * 3, top_k))
        combined: dict[str, RetrievalResult] = {}
        for rank, result in enumerate(vector_results, 1):
            combined[result.content] = result.model_copy(
                update={"score": 1 / (60 + rank)})
        for rank, chunk_id in enumerate(keyword, 1):
            chunk = next(chunk for chunk in self.chunks if chunk.chunk_id == chunk_id)
            key = chunk.content
            item = combined.get(key)
            if item:
                item.score += 1 / (60 + rank)
            else:
                combined[key] = RetrievalResult(content=chunk.content, source_doc=chunk.source,
                    section=" > ".join(chunk.section_path), score=1 / (60 + rank),
                    keyword_score=keyword[chunk_id])
        return sorted(combined.values(), key=lambda item: item.score, reverse=True)[:top_k]

    def documents(self) -> list[DocumentMeta]:
        if not self.documents_meta and self.data_dir.exists():
            self.documents_meta = [document for document, _ in load_directory(self.data_dir)]
        return self.documents_meta

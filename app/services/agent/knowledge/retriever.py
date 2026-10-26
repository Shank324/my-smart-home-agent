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

    # 中文虚词：在「字 + 二元组」切分下会高频命中无关块，必须过滤
    _STOPWORDS = set(
        "的 了 是 在 和 有 就 都 也 很 会 与 或 但 而 及 等 着 过 把 被 让 给 对 从 于 为 以 之 其 "
        "不 没 吗 呢 啊 吧 个 我 你 他 她 它 这 那 什 么 怎 样 如 何 可 要 能 多 少 一 上 下 中 里 外 "
        "什么时候 什么 怎么 怎样 如何 可以 需要 应该 一般 通常".split()
    )

    @classmethod
    def _tokenize(cls, text: str) -> list[str]:
        """中文按「单字 + 二元组」切，英文/数字按词切。

        不用 jieba：分词边界依赖上下文，查询里的「闪」和文档里的「闪烁」会被切成
        不同的词，导致本该命中的块匹配不上。字 + 二元组对查询和文档是同一套规则，
        短查询的召回率明显更稳。
        """
        lowered = text.lower()
        tokens = re.findall(r"[a-z0-9_]+", lowered)
        for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
            tokens.extend(run)
            tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
        return [token for token in tokens if token not in cls._STOPWORDS]

    def _bm25(self, query: str, top_k: int) -> dict[str, float]:
        query_tokens = set(self._tokenize(query))
        if not query_tokens or not self._tokens:
            return {}
        average_length = sum(len(tokens) for tokens in self._tokens) / len(self._tokens)
        # 先算文档频率，避免在块循环里重复扫描全部块
        document_frequency: dict[str, int] = {}
        for tokens in self._tokens:
            for token in query_tokens:
                if token in tokens:
                    document_frequency[token] = document_frequency.get(token, 0) + 1
        scores: dict[str, float] = {}
        document_count = len(self._tokens)
        for chunk, tokens in zip(self.chunks, self._tokens):
            length = len(tokens) or 1
            score = 0.0
            for token in query_tokens:
                frequency = tokens.count(token)
                if not frequency:
                    continue
                df = document_frequency.get(token, 0)
                if not df:
                    continue
                idf = math.log1p(max(0.0, (document_count - df + 0.5) / (df + 0.5)))
                score += idf * frequency * 2.2 / (
                    frequency + 1.2 * (0.25 + 0.75 * length / max(average_length, 1)))
            # 完全没命中的块不进入结果：否则无匹配时会返回任意块，看起来像"乱答"
            if score > 0:
                scores[chunk.chunk_id] = score
        return dict(sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_k])

    def search(self, query: str, top_k: int = 5, device_model: Optional[str] = None) -> list[RetrievalResult]:
        if not self.chunks:
            self.rebuild()
        # 没有语义能力的 embedding（如 HashEmbedder）不参与融合：
        # 它的排序接近随机，等权融进 RRF 会把 BM25 的正确结果挤下去
        semantic = getattr(self.embedder, "is_semantic", True)
        vector_results = (self.store.search(self.embedder.embed_query(query), max(top_k * 3, top_k),
                                            {"device_model": device_model} if device_model else None)
                          if semantic else [])
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
                item.keyword_score = keyword[chunk_id]
            else:
                combined[key] = RetrievalResult(content=chunk.content, source_doc=chunk.source,
                    section=" > ".join(chunk.section_path), score=1 / (60 + rank),
                    keyword_score=keyword[chunk_id])
        return sorted(combined.values(), key=lambda item: item.score, reverse=True)[:top_k]

    def documents(self) -> list[DocumentMeta]:
        if not self.documents_meta and self.data_dir.exists():
            self.documents_meta = [document for document, _ in load_directory(self.data_dir)]
        return self.documents_meta

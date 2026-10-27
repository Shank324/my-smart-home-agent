from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class DocumentMeta(BaseModel):
    doc_id: str
    filename: str
    device_model: Optional[str] = None
    category: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def id(self) -> str: return self.doc_id

    @property
    def title(self) -> str: return self.filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]

    @property
    def source(self) -> str: return self.filename


class TextChunk(BaseModel):
    chunk_id: str
    doc_id: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def id(self) -> str: return self.chunk_id

    @property
    def document_id(self) -> str: return self.doc_id

    @property
    def text(self) -> str: return self.content

    @property
    def source(self) -> str: return str(self.metadata.get("filename", self.doc_id))

    @property
    def section_path(self) -> list[str]:
        return list(self.metadata.get("section_path", []))


class RetrievalResult(BaseModel):
    content: str
    score: float
    source_doc: str
    section: str = ""
    vector_score: Optional[float] = None
    keyword_score: Optional[float] = None
    title_score: Optional[float] = None

    @property
    def chunk(self) -> TextChunk:
        sections = [self.section] if self.section else []
        return TextChunk(chunk_id="", doc_id=self.source_doc, content=self.content,
                         metadata={"filename": self.source_doc, "section_path": sections})


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    device_model: Optional[str] = Field(default=None, max_length=200)


class KnowledgeSearchResponse(BaseModel):
    results: list[RetrievalResult]


class KnowledgeStatus(BaseModel):
    ready: bool
    documents: int
    chunks: int
    vector_backend: str


Document = DocumentMeta
Chunk = TextChunk
SearchResult = RetrievalResult

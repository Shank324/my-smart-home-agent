from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.agent.knowledge.embedder import EmbeddingError
from app.services.agent.knowledge.loader import KnowledgeLoadError
from app.services.agent.knowledge.retriever import KnowledgeRetriever
from app.services.agent.knowledge.schemas import (
    KnowledgeSearchRequest, KnowledgeSearchResponse, KnowledgeStatus,
)
from app.services.agent.knowledge.vector_store import VectorStoreError

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
retriever = KnowledgeRetriever()


@router.post("/search", response_model=KnowledgeSearchResponse)
def search_knowledge(request: KnowledgeSearchRequest) -> KnowledgeSearchResponse:
    try:
        return KnowledgeSearchResponse(results=retriever.search(
            request.query, request.top_k, request.device_model))
    except (KnowledgeLoadError, EmbeddingError, VectorStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/init")
def init_knowledge() -> dict[str, int]:
    try:
        return {"chunks": retriever.rebuild()}
    except (KnowledgeLoadError, EmbeddingError, VectorStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/status", response_model=KnowledgeStatus)
def knowledge_status() -> KnowledgeStatus:
    # 允许「懒加载」：磁盘上有可用向量就直接读回，不重新 embedding
    try:
        retriever.ensure_ready()
    except (EmbeddingError, VectorStoreError, KnowledgeLoadError, OSError):
        pass
    return KnowledgeStatus(
        ready=bool(retriever.chunks), documents=len(retriever.documents()),
        chunks=len(retriever.chunks), vector_backend=retriever.store.backend)

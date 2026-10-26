from __future__ import annotations

import re

from .schemas import DocumentMeta, TextChunk


def split_document(
    document: DocumentMeta, content: str, chunk_size: int = 512, overlap: int = 64
) -> list[TextChunk]:
    """Split on ##/### semantic sections, then apply bounded character windows."""
    if chunk_size < 1 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be non-negative and smaller than chunk_size")
    lines = content.splitlines()
    sections: list[tuple[list[str], list[str]]] = []
    path: list[str] = []
    body: list[str] = []
    for line in lines:
        heading = re.match(r"^(#{2,3})\s+(.+?)\s*$", line)
        if heading:
            if body:
                sections.append((path[:], body))
                body = []
            level = len(heading.group(1)) - 2
            path = path[:level] + [heading.group(2)]
        body.append(line)
    if body:
        sections.append((path[:], body))
    if not sections:
        sections = [([], lines)]
    chunks: list[TextChunk] = []
    for section_path, section_lines in sections:
        text = "\n".join(section_lines).strip()
        start = 0
        while start < len(text) or (not text and not chunks):
            end = min(start + chunk_size, len(text))
            part = text[start:end].strip()
            # 只有标题、没有正文的块对检索没有价值，反而会挤占召回名额
            if part and any(line.strip() and not line.lstrip().startswith("#")
                            for line in part.splitlines()):
                chunks.append(TextChunk(
                    chunk_id=f"{document.doc_id}-{len(chunks)}", doc_id=document.doc_id,
                    content=part, metadata={"filename": document.filename,
                    "device_model": document.device_model, "category": document.category,
                    "section_path": section_path, **document.metadata}))
            if end >= len(text):
                break
            start = end - overlap
    return chunks

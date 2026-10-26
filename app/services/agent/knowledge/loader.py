from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

from .schemas import DocumentMeta


class KnowledgeLoadError(RuntimeError):
    """Raised when a knowledge source cannot be loaded."""


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        raise KnowledgeLoadError("invalid markdown front matter")
    metadata: dict[str, str] = {}
    for line in parts[0].removeprefix("---").strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip("\"'")
    return metadata, parts[1].lstrip("\r\n")


def load_markdown(path: Path, root: Optional[Path] = None) -> tuple[DocumentMeta, str]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise KnowledgeLoadError(f"cannot read knowledge source: {path}") from exc
    metadata, content = _frontmatter(content)
    title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    title = metadata.pop("title", title_match.group(1).strip() if title_match else path.stem)
    source = str(path.relative_to(root)) if root else str(path)
    document_id = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    device_model = metadata.pop("device_model", None) or metadata.pop("model", None)
    if not device_model and path.stem.lower() == "ac_2000":
        device_model = "AC-2000"
    category = metadata.pop("category", None)
    if category is None and root is not None and path.parent != root:
        category = path.parent.name
    metadata["title"] = title
    return DocumentMeta(doc_id=document_id, filename=source, device_model=device_model,
                        category=category, metadata=metadata), content.strip()


def load_directory(directory: Path) -> list[tuple[DocumentMeta, str]]:
    if not directory.exists() or not directory.is_dir():
        raise KnowledgeLoadError(f"knowledge directory does not exist: {directory}")
    return [load_markdown(path, directory) for path in sorted(directory.rglob("*.md"))]

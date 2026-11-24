"""Bounded, version-bound Markdown reads. No index, inference, or curation writes."""

import json
import re
from hashlib import sha256
from pathlib import PurePosixPath

from . import core


class SourceChanged(ValueError):
    pass


def source_path(value: str, *, prefix: bool = False) -> str:
    if prefix and value == "":
        return value
    path = PurePosixPath(value)
    if (not value or value in {".", ".."} or len(value) > 1024 or path.is_absolute() or "\\" in value
            or path.as_posix() != value or any(ord(c) < 32 for c in value)
            or any(p in {".", "..", *core.RAG_EXCLUDED_DIRS} for p in path.parts)
            or (not prefix and path.suffix != ".md")):
        raise ValueError("use a visible Vault-relative Markdown path or directory prefix")
    return value


def page_offset(cursor: str | None, scope: str, total: int) -> int:
    if cursor is None:
        return 0
    match = re.fullmatch(r"([0-9a-f]{64}):(0|[1-9][0-9]{0,12})", cursor)
    if not match:
        raise ValueError("invalid source cursor")
    if match[1] != scope:
        raise SourceChanged("source_changed: list or read again from the beginning")
    offset = int(match[2])
    if not 0 < offset < total:
        raise ValueError("source cursor is out of range")
    return offset


def list_sources(settings: core.Settings, agent: dict, *, prefix: str = "", cursor: str | None = None,
                 limit: int = 20) -> dict:
    core.require_scope(agent, "vault-rag")
    prefix = source_path(prefix, prefix=True)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be in 1..50")
    # Fail instead of claiming a complete inventory when a file could not be read.
    rows = []
    for path, file in core.iter_markdown_files(settings.vault_dir):
        if prefix and path != prefix and not path.startswith(prefix + "/"):
            continue
        text = file.read_text(encoding="utf-8", errors="replace")
        metadata = core.document_metadata(path, text)
        rows.append({"path": path, "document_hash": sha256(text.encode()).hexdigest(),
                     "title": core.markdown_title(file, core.markdown_body(text))[:160],
                     "memory_type": metadata["memory_type"],
                     "observed_at": str(metadata.get("observed_at") or "")[:80]})
    scope = sha256(json.dumps([prefix, [(r["path"], r["document_hash"]) for r in rows]],
                             ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    start = page_offset(cursor, scope, len(rows))
    end, size = start, 0
    for row in rows[start:start + limit]:
        row_size = len(json.dumps(row, ensure_ascii=False).encode())
        if size + row_size > 48000:
            if end == start:
                raise ValueError("source metadata exceeds the page budget")
            break
        size += row_size
        end += 1
    return {"sources": rows[start:end], "prefix": prefix, "snapshot": scope,
            "total_sources": len(rows), "span": [start, end],
            "next_cursor": f"{scope}:{end}" if end < len(rows) else None,
            "coverage": "File inventory only, not content review. Paths and observed_at are not event-time filters."}


def read_source(settings: core.Settings, agent: dict, *, path: str, document_hash: str,
                cursor: str | None = None, max_characters: int = 6000) -> dict:
    core.require_scope(agent, "vault-rag")
    path = source_path(path)
    if not re.fullmatch(r"[0-9a-f]{64}", document_hash):
        raise ValueError("document_hash must be a SHA-256 from search or source listing")
    if type(max_characters) is not int or not 256 <= max_characters <= 8000:
        raise ValueError("max_characters must be in 256..8000")
    # Reuse the search boundary: excluded directories and links outside the Vault never become readable.
    file = dict(core.iter_markdown_files(settings.vault_dir)).get(path)
    if file is None:
        raise FileNotFoundError("source_not_found: list or search again")
    text = file.read_text(encoding="utf-8", errors="replace")
    actual = sha256(text.encode()).hexdigest()
    if actual != document_hash:
        raise SourceChanged("source_changed: list or search again, then restart this read")
    scope = sha256(f"{path}\0{actual}".encode()).hexdigest()
    start = page_offset(cursor, scope, len(text))
    end = min(len(text), start + max_characters)
    return {"path": path, "document_hash": actual, "text": text[start:end],
            "span": [start, end], "total_characters": len(text),
            "next_cursor": f"{scope}:{end}" if end < len(text) else None,
            "offset_unit": "Unicode code points in decoded Markdown; start inclusive, end exclusive",
            "coverage": "This page only; reaching the end does not prove semantic completeness or truth."}

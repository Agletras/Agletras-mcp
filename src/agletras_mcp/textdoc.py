"""Markdown and plain-text document extraction (stdlib only).

Markdown content is preserved essentially as-is — headings, tables, and code
fences are meaningful structure that must survive collection verbatim. The
extractor only finds a title (first H1) and counts structural landmarks for
metadata; it never interprets or executes anything inside the document.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .config import Settings, load_settings
from .errors import ToolError
from .source_record import normalize_structured_text

LOGGER = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_WORD_RE = re.compile(r"\w+")


def describe_markdown(text: str) -> dict[str, Any]:
    """Count headings / code fences / words for *text* (read-only analysis)."""
    headings: list[str] = []
    fences = 0
    in_fence = False
    for line in text.split("\n"):
        stripped = line.strip()
        if not in_fence and _FENCE_RE.match(line):
            in_fence = True
            fences += 1
            continue
        if in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = False
            continue
        match = _HEADING_RE.match(stripped)
        if match:
            headings.append(match.group(1))
    return {
        "headings": len(headings),
        "heading_titles": headings[:20],
        "code_fences": fences,
        "words": len(_WORD_RE.findall(text)),
    }


def first_heading(text: str) -> str:
    """First markdown heading line, or '' — used as a conservative title."""
    for line in text.split("\n"):
        match = _HEADING_RE.match(line.strip())
        if match:
            return match.group(1)
    return ""


def extract_text_document(
    path: Path, source_type: str, *, settings: Settings | None = None, max_chars: int | None = None
) -> dict[str, Any]:
    """Extract text from ``.md``/``.txt``-style files without touching format.

    Decodes UTF-8 (BOM-tolerant) with a latin-1 fallback so undecodable bytes
    never crash the pipeline; the encoding is reported in metadata.
    """
    settings = settings or load_settings()
    raw = path.read_bytes()
    if not raw.strip():
        raise ToolError("empty_file", f"File is empty: {path.name}")
    try:
        decoded = raw.decode("utf-8-sig")
        encoding = "utf-8"
    except UnicodeDecodeError:
        decoded = raw.decode("latin-1", errors="replace")
        encoding = "latin-1"
    text = normalize_structured_text(decoded)
    truncated = False
    limit = max_chars if max_chars and max_chars > 0 else settings.doc_max_chars
    if len(text) > limit:
        text = text[:limit]
        truncated = True

    metadata = {"encoding": encoding}
    if source_type == "markdown":
        metadata.update(describe_markdown(text))
    title = first_heading(text) if source_type == "markdown" else ""
    return {
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        "title": title,
        "metadata": metadata,
        "warnings": ["Text truncated to the character limit."] if truncated else [],
    }

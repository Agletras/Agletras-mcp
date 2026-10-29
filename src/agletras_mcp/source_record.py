"""Unified source record for the Phase 2 collection pipeline.

Every collected source — web page, search snippet, PDF, or local document —
becomes a :class:`SourceRecord` produced by an adapter in
:mod:`agletras_mcp.adapters` and persisted by the pipeline in
:mod:`agletras_mcp.collect` through :mod:`agletras_mcp.storage`.

Key properties:
- the original source identity (URL / file path) travels with the extracted
  text at every stage;
- publication time is only ever filled from real document/page metadata — a
  missing publication date stays ``None``, it is never inferred from the
  retrieval time;
- ``retrieved_at`` is recorded separately from any publication time;
- extraction status is one of :data:`STATUSES`, with a concise, safe error
  code + message on failure (never file contents or secrets).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

STATUSES = ("pending", "success", "partial", "failed", "unsupported", "skipped")
"""Extraction statuses. ``failed`` = extraction attempted and errored;
``unsupported`` = no safe adapter for the format; ``skipped`` = pre-flight
skip (temporary/hidden file, allowlist miss); ``partial`` = extracted but
with warnings/limits."""

SOURCE_TYPES = ("web", "search_result", "pdf", "markdown", "text", "csv", "docx", "xlsx", "html", "note", "other")

# Storage `kind`: the coarse category existing tools already filter on.
KIND_BY_SOURCE_TYPE: dict[str, str] = {
    "web": "url",
    "html": "url",
    "search_result": "search_result",
    "pdf": "pdf",
    "note": "note",
}
_LEGACY_SOURCE_TYPES = {
    "docx": "doc", "xlsx": "doc", "csv": "doc", "markdown": "doc", "text": "doc", "other": "doc",
}
SOURCE_TYPE_TO_KIND = {**KIND_BY_SOURCE_TYPE, **_LEGACY_SOURCE_TYPES}

_MAX_TEXT = 5_000_000  # absolute upper bound; settings cap below this in practice


def utc_now_iso() -> str:
    """Retrieval timestamp (ISO 8601, UTC, second precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SourceRecord:
    """The Phase 2 unified record (see module docstring)."""

    source_type: str
    title: str = ""
    original_url: str | None = None
    final_url: str | None = None
    file_path: str | None = None
    publication_date: str | None = None
    retrieved_at: str = field(default_factory=utc_now_iso)
    content_type: str | None = None
    file_size_bytes: int | None = None
    checksum: str | None = None
    extracted_text: str = ""
    extraction_status: str = "pending"
    error_code: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    normalized_sha256: str | None = None

    def validate(self) -> None:
        if self.source_type not in SOURCE_TYPES:
            self.source_type = "other"
        if self.extraction_status not in STATUSES:
            self.extraction_status = "pending"

    def kind(self) -> str:
        """Coarse storage kind used by the existing tools/reports."""
        return SOURCE_TYPE_TO_KIND.get(self.source_type, "doc")

    def checksum_text(self) -> str | None:
        """SHA-256 of the *extracted* text, or None when there is none."""
        if not self.extracted_text:
            return None
        return hashlib.sha256(self.extracted_text.encode("utf-8")).hexdigest()

    def to_dict(self, *, include_text: bool = True, text_key: str = "extracted_text") -> dict[str, Any]:
        """Serialize for tool responses / storage."""
        data = asdict(self)
        data["kind_hint"] = self.kind()
        if not include_text:
            data.pop(text_key, None)
        return data

    def set_failure(self, code: str, message: str, *, status: str = "failed") -> None:
        self.extraction_status = status
        self.error_code = code
        self.error_message = message

    def add_warning(self, message: str) -> None:
        if message and message not in self.warnings:
            self.warnings.append(message)


def finalize_text(record: SourceRecord, *, max_chars: int | None) -> None:
    """Trim text to the char cap, trim pathological sizes, and set the checksums."""
    text = record.extracted_text[:_MAX_TEXT]
    if max_chars and max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars]
        record.add_warning(f"Text truncated to {max_chars} characters.")
        record.extraction_status = "partial" if record.extraction_status in ("success", "partial") else record.extraction_status
    record.extracted_text = text
    record.checksum = record.checksum_text()
    record.normalized_sha256 = shape_hash(text)


# ---------------------------------------------------------------------------
# Text normalization: structure-preserving (headings, tables, code blocks)
# ---------------------------------------------------------------------------

_BLANK_RUN = re.compile(r"\n{3,}")


def normalize_structured_text(text: str) -> str:
    """Normalize encoding artifacts and whitespace without flattening structure.

    Newlines are meaningful (Markdown headings, tables, code blocks, CSV rows),
    so this only: strips a BOM, normalizes newlines to ``\\n``, trims trailing
    whitespace per line and Unicode zero-width artifacts, and collapses runs of
    >1 blank line to a single blank line.
    """
    if not text:
        return ""
    cleaned = text.replace("﻿", "").replace("\ufeff", "")
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in cleaned.split("\n")]
    cleaned = "\n".join(lines)
    cleaned = "".join(ch for ch in cleaned if ch == "\n" or unicodedata.category(ch)[0] != "C" or ch.isspace())
    cleaned = _BLANK_RUN.sub("\n\n", cleaned)
    return cleaned.strip()


def collapse_inline(text: str) -> str:
    """Collapse runs of spaces/tabs inside a single line (list/snippet text)."""
    return re.sub(r"[ \t]+", " ", text).strip()


_SHAPE_CLEAN = re.compile(r"[^0-9a-z]+")


def shape_hash(text: str) -> str | None:
    """Cheap, deterministic near-duplicate key (no models required).

    The shape is the word sequence of *text*, lowercased, digits kept, all
    punctuation/whitespace/casing differences erased. Two texts with identical
    shapes are treated as near duplicates by the pipeline. Returns ``None`` for
    empty text.
    """
    if not text:
        return None
    words = _SHAPE_CLEAN.sub(" ", text.lower()).split()
    if not words:
        return None
    return hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest()

# ---------------------------------------------------------------------------
# Publication dates: only from explicit metadata, never invented
# ---------------------------------------------------------------------------

_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S", "%d %b %Y", "%Y-%m-%d", "%Y/%m/%d", "%B %d, %Y",
)


def parse_publication_date(raw: str | None) -> str | None:
    """Best-effort ISO-8601 normalization of an explicit metadata date.

    Returns ``None`` when *raw* is empty or unparseable — never a guessed
    value. The raw string stays available in ``metadata`` for context.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw or len(raw) > 64:
        return None
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat(timespec="seconds")
    return None


def replace_fields(record: SourceRecord, **changes: Any) -> SourceRecord:
    """Dataclass copy helper (keeps factory-style calls concise)."""
    return replace(record, **changes)

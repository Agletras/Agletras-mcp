"""Passage extraction for the evidence library (Phase 3).

Splits extracted source text into *passages*: small, traceable slices that
citations can point at. Every passage keeps the exact character offsets of
its text inside the stored content, its SHA-256, and the page/heading context
that the extraction adapter made available:

- PDF text keeps ``--- page N/M ---`` markers (see :mod:`agletras_mcp.pdf`);
  markers are consumed (never part of a passage) and set the page number.
- Markdown headings (``#`` … ``######``) start a new passage and are recorded
  as the section heading.
- Paragraphs are packed up to ``max_chars``; oversized paragraphs are split at
  sentence boundaries; a trailing mini-passage is merged when it fits.

The splitter is deterministic and model-free: identical text always yields
identical passages. Never modifies characters — passage text is always an
exact ``text[start:end]`` slice of the input.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

DEFAULT_MAX_CHARS = 1200
DEFAULT_MIN_CHARS = 120

#: Hard cap: beyond this, splitting stops (the caller reports truncation).
MAX_PASSAGES = 5000

PAGE_MARKER_RE = re.compile(r"^\s*-{2,}\s*page\s+(\d+)(?:\s*/\s*\d+)?\s*-{2,}\s*$", re.IGNORECASE)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?;:])\s+(?=\S)")


@dataclass(frozen=True)
class PassageData:
    """One immutable evidence passage."""

    index: int
    text: str
    start: int
    end: int
    page: int | None
    heading: str | None
    locator: str
    passage_hash: str


def passage_hash(text: str) -> str:
    """SHA-256 hex digest of a passage's exact text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_locator(page: int | None, heading: str | None) -> str:
    """Human-readable locator, e.g. ``"page 3 · section: Results"``."""
    parts: list[str] = []
    if page is not None:
        parts.append(f"page {page}")
    if heading:
        parts.append(f"section: {heading}")
    return " · ".join(parts)


@dataclass
class _Unit:
    """A paragraph-level block with its page/heading context."""

    start: int
    end: int
    page: int | None
    heading: str | None


def _line_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    position = 0
    for line in text.split("\n"):
        spans.append((position, position + len(line), line))
        position += len(line) + 1
    return spans


def _units(text: str) -> list[_Unit]:
    """Paragraph units with heading/page context (page markers consumed)."""
    units: list[_Unit] = []
    page: int | None = None
    heading: str | None = None
    unit_start: int | None = None
    unit_end: int | None = None
    unit_page: int | None = None
    unit_heading: str | None = None

    def flush() -> None:
        nonlocal unit_start, unit_end, unit_page, unit_heading
        if unit_start is not None and unit_end is not None:
            units.append(_Unit(unit_start, unit_end, unit_page, unit_heading))
        unit_start = unit_end = None
        unit_page = unit_heading = None

    for start, end, line in _line_spans(text):
        if not line.strip():
            flush()
            continue
        page_match = PAGE_MARKER_RE.match(line)
        if page_match:
            flush()
            page = int(page_match.group(1))
            continue
        heading_match = HEADING_RE.match(line)
        if heading_match:
            flush()
            heading = heading_match.group(2).strip()
        if unit_start is None:
            unit_start = start
            unit_page = page
            unit_heading = heading
        unit_end = end
    flush()
    return units


def _emit(text: str, start: int, end: int, page: int | None, heading: str | None) -> tuple[int, int] | None:
    """Trim a slice to non-whitespace bounds; return adjusted ``(start, end)``."""
    raw = text[start:end]
    if not raw.strip():
        return None
    leading = len(raw) - len(raw.lstrip())
    trailing = len(raw) - len(raw.rstrip())
    return start + leading, end - trailing


def _split_long_unit(unit: _Unit, text: str, *, max_chars: int) -> list[tuple[int, int]]:
    chunks: list[tuple[int, int]] = []
    cursor = unit.start
    while unit.end - cursor > max_chars:
        window_end = cursor + max_chars
        cut = None
        for match in _SENTENCE_BREAK.finditer(text, cursor, window_end):
            cut = match.end()
        if cut is None or cut - cursor < max_chars // 4:
            cut = window_end
        chunks.append((cursor, cut))
        cursor = cut
    if cursor < unit.end:
        chunks.append((cursor, unit.end))
    return chunks


def split_passages(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    min_chars: int = DEFAULT_MIN_CHARS,
) -> list[PassageData]:
    """Split *text* into passages. Deterministic; exact ``text[start:end]`` slices."""
    if not text or not text.strip():
        return []
    if max_chars <= 0:
        max_chars = DEFAULT_MAX_CHARS
    if min_chars <= 0 or min_chars >= max_chars:
        min_chars = max(1, min(DEFAULT_MIN_CHARS, max_chars // 2))

    ranges: list[tuple[int, int, int | None, str | None]] = []
    for unit in _units(text):
        if unit.end - unit.start <= max_chars:
            ranges.append((unit.start, unit.end, unit.page, unit.heading))
            continue
        for start, end in _split_long_unit(unit, text, max_chars=max_chars):
            adjusted = _emit(text, start, end, unit.page, unit.heading)
            if adjusted is not None:
                ranges.append((adjusted[0], adjusted[1], unit.page, unit.heading))

    # Pack consecutive ranges that share page/heading and fit the cap.
    packed: list[tuple[int, int, int | None, str | None]] = []
    for start, end, page, heading in ranges:
        if packed:
            prev_start, prev_end, prev_page, prev_heading = packed[-1]
            if (prev_page, prev_heading) == (page, heading) and end - prev_start <= max_chars:
                packed[-1] = (prev_start, end, prev_page, prev_heading)
                continue
        packed.append((start, end, page, heading))

    # Merge a trailing mini-passage back into its predecessor when it fits.
    while len(packed) >= 2:
        start, end, page, heading = packed[-1]
        if end - start >= min_chars:
            break
        prev_start, prev_end, prev_page, prev_heading = packed[-2]
        if (prev_page, prev_heading) != (page, heading) or end - prev_start > max_chars:
            break
        packed[-2] = (prev_start, end, prev_page, prev_heading)
        packed.pop()

    passages: list[PassageData] = []
    for start, end, page, heading in packed[:MAX_PASSAGES]:
        adjusted = _emit(text, start, end, page, heading)
        if adjusted is None:
            continue
        start, end = adjusted
        passage_text = text[start:end]
        index = len(passages)
        passages.append(
            PassageData(
                index=index,
                text=passage_text,
                start=start,
                end=end,
                page=page,
                heading=heading,
                locator=make_locator(page, heading),
                passage_hash=passage_hash(passage_text),
            )
        )
    return passages

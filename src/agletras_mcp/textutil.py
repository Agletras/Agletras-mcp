"""Small text helpers shared across modules."""

from __future__ import annotations

import re

_WS_RUN = re.compile(r"[^\S\n]+")
_NL_RUN = re.compile(r"\n[ \t]*\n(?:\s*\n)+")


def collapse_whitespace(text: str) -> str:
    """Collapse runs of spaces/tabs and reduce blank-line runs to one break."""
    text = _WS_RUN.sub(" ", text)
    text = _NL_RUN.sub("\n\n", text)
    return text.strip()


def excerpt(text: str, limit: int = 400) -> str:
    """Return a short, whitespace-collapsed excerpt of *text*."""
    cleaned = collapse_whitespace(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "\u2026"

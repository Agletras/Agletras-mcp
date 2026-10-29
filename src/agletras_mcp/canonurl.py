"""Stable source identities for the evidence library (Phase 3).

Two functions do all the canonicalization work:

- :func:`canonicalize_url` produces the *identity* of a web source: scheme and
  host lowercased, default ports removed, fragments dropped, well-known
  tracking parameters dropped, query parameters sorted, empty path
  normalized to ``/``. A canonical URL is only used for de-duplication; the
  original (and final) URLs always travel separately with the source record.
- :func:`normalize_local_path` produces the identity of a local file: the
  fully resolved absolute path (symlinks and ``..`` collapsed), so two paths
  that point at the same file share one identity.

Both return ``None``/empty string for input that cannot be a valid identity;
callers decide how to report that (never guessing a value).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}

#: Query parameters that never change the resource identity (analytics/
#: campaign/tracking). Everything else is kept and ordered.
_TRACKING_PARAMS = frozenset(
    {
        "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid",
        "mc_cid", "mc_eid", "igshid", "_ga", "_gl", "ref_src", "mkt_tok",
    }
)
_TRACKING_PREFIXES = ("utm_",)


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith(_TRACKING_PREFIXES)


def canonicalize_url(url: str) -> str | None:
    """Canonical identity for an ``http(s)`` URL, or ``None`` if not usable.

    Careful by construction: only transformations that cannot change which
    resource is addressed are applied. The path is kept byte-for-byte (no
    trailing-slash games, no percent-decoding), and credentials are rejected
    outright rather than being silently dropped.
    """
    raw = (url or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    if "@" in parts.netloc:  # embedded credentials: refuse, never rewrite
        return None
    host = parts.hostname
    if not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = host if port is None or port == DEFAULT_PORTS[scheme] else f"{host}:{port}"
    path = parts.path or "/"
    try:
        pairs = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_tracking(key)
        ]
    except ValueError:
        return None
    pairs.sort(key=lambda item: (item[0], item[1]))
    query = urlencode(pairs)
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_local_path(path: str | Path) -> str:
    """Canonical identity for a local file path (resolved, absolute)."""
    raw = str(path or "").strip()
    if not raw:
        return ""
    candidate = Path(raw).expanduser()
    try:
        return str(candidate.resolve(strict=False))
    except OSError:  # pragma: no cover - exotic filesystems only
        return str(candidate.absolute())


def identity_kind(values: dict[str, object]) -> str:
    """``"url"``/``"path"``/``"note"`` for the identity a record would get."""
    if values.get("canonical_url"):
        return "url"
    if values.get("path"):
        return "path"
    return "note"

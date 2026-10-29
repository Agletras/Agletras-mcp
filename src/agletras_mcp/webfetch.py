"""Minimal HTTP fetching and HTML-to-text extraction (standard library only).

No third-party HTTP dependency: this keeps the zero-cost footprint small and
avoids duplicating capabilities already present in the environment. Only
``http``/``https`` URLs are allowed; responses are size-capped and time-limited.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .config import Settings, load_settings
from .errors import ToolError
from .netpolicy import SafeRedirectHandler, assert_safe_url
from .textutil import collapse_whitespace

LOGGER = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"


class HttpToolError(ToolError):
    """ToolError with the HTTP status attached for failure classification.

    The base ``code`` / message stay identical to Phase 1 behavior (404 still
    surfaces as ``network_error`` with "404" in the message), but the new
    attribute lets the Phase 2 collection pipeline classify the failure
    precisely (retriable vs permanent) without changing Phase 1 contracts.
    """

    def __init__(self, code: str, message: str, *, http_status: int | None = None) -> None:
        super().__init__(code, message)
        self.http_status = http_status

_SKIP_CONTENT_TAGS = {"script", "style", "noscript", "template", "svg", "canvas", "head"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main", "aside", "nav",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "table", "tr", "td",
    "th", "blockquote", "pre", "address", "figure", "figcaption", "form",
    "fieldset", "dl", "dt", "dd", "hr",
}
_BREAK_TAGS = {"br"}

_CHARSET_RE = re.compile(r"charset=([A-Za-z0-9._-]+)")


class _HtmlTextExtractor(HTMLParser):
    """Very small readable-text extractor built on ``html.parser``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in _BLOCK_TAGS or tag in _BREAK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        self._parts.append(data)

    def text(self) -> str:
        return collapse_whitespace("".join(self._parts))


def html_to_text(html_text: str) -> tuple[str, str]:
    """Return ``(title, readable_text)`` for an HTML document."""
    parser = _HtmlTextExtractor()
    parser.feed(html_text)
    parser.close()
    return collapse_whitespace(parser.title), parser.text()


def fetch_bytes(
    url: str, settings: Settings, *, max_bytes: int | None = None, data: bytes | None = None
) -> tuple[str, str, bytes]:
    """Fetch *url* and return ``(final_url, content_type, body)``.

    Passing *data* performs a POST (form submission, used by the search
    provider). The URL (and every redirect destination) is validated against
    the SSRF policy in :mod:`agletras_mcp.netpolicy` before any connection.
    """
    final_url, content_type, body, _headers, _status = fetch_bytes_with_headers(
        url, settings, max_bytes=max_bytes, data=data
    )
    return final_url, content_type, body


def fetch_bytes_with_headers(
    url: str,
    settings: Settings,
    *,
    max_bytes: int | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[str, str, bytes, dict[str, str], int]:
    """Fetch *url* returning ``(final_url, content_type, body, headers, status)``.

    Phase 8 addition for monitoring: callers may pass request headers (e.g.
    ``If-None-Match`` / ``If-Modified-Since``) and read the response headers
    (``ETag`` / ``Last-Modified``). A ``304 Not Modified`` response is returned
    normally with an empty body and status ``304`` — it is *not* an error.
    Every other non-2xx status keeps the Phase 1 behavior (``rate_limited`` for
    429, ``network_error`` with the HTTP status attached otherwise).
    """
    assert_safe_url(url, settings)
    request_headers = {
        "User-Agent": settings.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.5",
    }
    for name, value in (headers or {}).items():
        if value:
            request_headers[str(name)] = str(value)
    request = urllib.request.Request(url, data=data, headers=request_headers)
    limit = max_bytes or settings.fetch_max_bytes
    opener = urllib.request.build_opener(SafeRedirectHandler(settings))
    try:
        with opener.open(request, timeout=settings.fetch_timeout) as response:
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "") or ""
            response_headers = {name: value for name, value in response.headers.items()}
            status = int(getattr(response, "status", 200) or 200)
            body = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            response_headers = {name: value for name, value in exc.headers.items()} if exc.headers else {}
            exc.close()
            return url, response_headers.get("Content-Type", "") or "", b"", response_headers, 304
        exc.close()  # release the error response body (avoids ResourceWarning)
        if exc.code == 429:
            raise HttpToolError(
                "rate_limited", f"HTTP 429 (rate limited) while fetching {url}", http_status=429
            ) from exc
        raise HttpToolError(
            "network_error", f"HTTP {exc.code} while fetching {url}", http_status=exc.code
        ) from exc
    except urllib.error.URLError as exc:
        raise ToolError("network_error", f"Could not reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ToolError(
            "network_error", f"Timed out after {settings.fetch_timeout:g}s fetching {url}"
        ) from exc
    except ValueError as exc:
        raise ToolError("invalid_argument", f"Invalid URL {url!r}: {exc}") from exc
    except OSError as exc:
        raise ToolError("network_error", f"Network failure for {url}: {exc}") from exc
    if len(body) > limit:
        raise ToolError(
            "too_large",
            f"Response from {url} exceeds the {limit} byte limit; "
            "use extract_pdf for large documents or raise AGLETRAS_FETCH_MAX_BYTES.",
        )
    return final_url, content_type, body, response_headers, status


def decode_body(body: bytes, content_type: str) -> str:
    """Decode response bytes honoring a charset parameter (UTF-8 fallback)."""
    match = _CHARSET_RE.search(content_type or "")
    encoding = match.group(1) if match else "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


# Back-compat aliases for internal callers.
_decode = decode_body


def looks_like_pdf(content_type: str, body: bytes) -> bool:
    """True when the content type or magic bytes identify a PDF."""
    if "pdf" in (content_type or "").lower():
        return True
    return body[:1024].lstrip().startswith(PDF_MAGIC)


def looks_like_html(content_type: str, body: bytes) -> bool:
    """True for html/xhtml by type or document-shaped magic bytes."""
    lowered = (content_type or "").lower()
    if "html" in lowered or "xhtml" in lowered:
        return True
    head = body[:1024].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


_is_pdf = looks_like_pdf
_is_html = looks_like_html


def fetch_url_text(url: str, settings: Settings | None = None, *, max_chars: int | None = None) -> dict[str, Any]:
    """Fetch *url* and return a readable-text payload."""
    settings = settings or load_settings()
    final_url, content_type, body = fetch_bytes(url, settings)
    if _is_pdf(content_type, body):
        raise ToolError(
            "unsupported_content",
            f"{final_url} serves a PDF; call extract_pdf with this URL instead.",
        )
    limit = max_chars if max_chars and max_chars > 0 else settings.fetch_max_chars
    text = decode_body(body, content_type)
    title = ""
    lowered = (content_type or "").lower()
    if looks_like_html(content_type, body):
        title, text = html_to_text(text)
    elif not (
        lowered.startswith("text/")
        or lowered.startswith("application/json")
        or "xml" in lowered
        or not lowered  # unknown content type: treat as text
    ):
        raise ToolError(
            "unsupported_content",
            f"Unsupported content type {content_type!r} at {final_url}.",
        )
    text = text.strip()
    truncated = len(text) > limit
    if truncated:
        text = text[:limit]
    return {
        "url": url,
        "final_url": final_url,
        "content_type": content_type,
        "title": title,
        "text": text,
        "chars": len(text),
        "truncated": truncated,
    }


def download_to_cache(url: str, settings: Settings | None = None) -> tuple[Path, str]:
    """Download a PDF into the cache directory. Returns ``(path, final_url)``."""
    settings = settings or load_settings()
    final_url, content_type, body = fetch_bytes(url, settings, max_bytes=settings.pdf_max_bytes)
    if not body.lstrip().startswith(PDF_MAGIC):
        raise ToolError(
            "unsupported_content",
            f"{final_url} did not return a PDF (content type {content_type!r}).",
        )
    return store_pdf_in_cache(body, settings), final_url


def store_pdf_in_cache(body: bytes, settings: Settings | None = None) -> Path:
    """Write PDF *body* into the cache directory (hash-named, reused)."""
    settings = settings or load_settings()
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    path = settings.cache_dir / f"{sha256(body).hexdigest()[:16]}.pdf"
    if not path.exists():
        path.write_bytes(body)
        LOGGER.info("cached pdf %s (%d bytes)", path.name, len(body))
    return path

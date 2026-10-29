"""Open-web search with free, key-less provider adapters.

Providers (selected with ``AGLETRAS_SEARCH_PROVIDER``, default ``duckduckgo``):

- ``duckduckgo``: DuckDuckGo's HTML endpoint (``https://html.duckduckgo.com/html/``)
  queried through its standard POST search form. No API key and no terms
  requiring registration; it is however an *unofficial* endpoint, so responses
  are parsed defensively and calls are paced politely (default 2 s between
  searches). Challenge/anomaly pages are never solved or followed — they map
  to a typed ``rate_limited`` error.
- ``wikipedia``: the official MediaWiki Action API of English Wikipedia —
  documented, free, stable JSON. Encyclopedic coverage only.

All network access goes through :func:`webfetch.fetch_bytes`, so the SSRF
policy, timeouts, byte caps, and the identifying User-Agent apply to search
as well. Results are normalized to ``{title, url, snippet, source}``; nothing
is ever synthesized — an empty or failing provider yields an empty list or a
typed ``ToolError``, never placeholder data.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any

from . import webfetch
from .config import Settings, load_settings
from .errors import ToolError
from .textutil import collapse_whitespace

LOGGER = logging.getLogger(__name__)

_SEARCH_TIMEOUT_MARKERS = (
    "anomaly",
    "unusual traffic",
    "verify you are human",
    "was blocked",
)


@dataclass(frozen=True)
class SearchResult:
    """One normalized search result."""

    title: str
    url: str
    snippet: str
    source: str

    def as_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet, "source": self.source}


class SearchProvider:
    """Adapter interface: query in, normalized results out."""

    name = "abstract"
    default_endpoint = ""

    def __init__(self, endpoint: str = "") -> None:
        self.endpoint = endpoint or self.default_endpoint
        self._query = ""
        self._limit = 0

    def search(self, query: str, limit: int, settings: Settings) -> list[SearchResult]:
        raise NotImplementedError

    def build_url(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def payload(self) -> bytes | None:
        """Form body for the search request (None = GET)."""
        return None

    def fetch_body(self, settings: Settings) -> str:
        """Fetch this provider's endpoint and decode the response."""
        _final_url, _content_type, body = webfetch.fetch_bytes(
            self.build_url(), settings, data=self.payload()
        )
        return body.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# DuckDuckGo (HTML endpoint)
# --------------------------------------------------------------------------


class _DuckDuckGoParser(HTMLParser):
    """Extract (title, url, snippet) triples from the DDG HTML result page.

    The page wraps every hit in containers; result links carry class
    ``result__a`` (href is a ``//duckduckgo.com/l/?uddg=<encoded>`` redirect)
    and the matching snippet is an anchor with class ``result__snippet``.
    Advertisement containers carry ``result--ad`` and are skipped.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._div_classes: list[frozenset[str]] = []  # open <div> class stacks
        self._capture: str | None = None  # "title" | "snippet"
        self._buffer: list[str] = []
        self._href = ""
        self._in_ad = False
        self._last_kept = False  # snippet anchors attach only to kept results
        self._seen_urls: set[str] = set()

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> frozenset[str]:
        value = next((v or "" for k, v in attrs if k == "class"), "")
        return frozenset(value.split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if tag == "div":
            self._div_classes.append(classes)
            if "result--ad" in classes:
                self._in_ad = True
        elif tag == "a":
            if "result__a" in classes:
                self._in_ad = self._in_ad or any("result--ad" in c for c in self._div_classes)
                self._capture = "title"
                self._buffer = []
                self._href = next((v or "" for k, v in attrs if k == "href"), "")
            elif "result__snippet" in classes:
                self._capture = "snippet"
                self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture:
            text = collapse_whitespace("".join(self._buffer))
            if self._capture == "title":
                url = unwrap_ddg_href(self._href)
                keep = bool(url) and not self._in_ad and url not in self._seen_urls and bool(text)
                if keep:
                    self._seen_urls.add(url)
                    self.results.append(SearchResult(title=text, url=url, snippet="", source="duckduckgo"))
                self._last_kept = keep
            elif self._last_kept and self.results:
                self.results[-1] = replace_snippet(self.results[-1], text)
            self._capture = None
            self._buffer = []
            self._href = ""
        elif tag == "div":
            if self._div_classes:
                classes = self._div_classes.pop()
                if "result--ad" in classes:
                    self._in_ad = any("result--ad" in c for c in self._div_classes)

    def close(self) -> None:
        super().close()
        # Truncated documents simply leave an unclosed capture; handle_endtag
        # never fires for it, so the partial anchor is dropped (safe).


def replace_snippet(result: SearchResult, snippet: str) -> SearchResult:
    """Return *result* with *snippet* attached (first non-empty wins)."""
    if result.snippet or not snippet:
        return result
    return SearchResult(title=result.title, url=result.url, snippet=snippet, source=result.source)


def unwrap_ddg_href(href: str) -> str:
    """Resolve DuckDuckGo's ``//duckduckgo.com/l/?uddg=<url>`` redirect links.

    Returns the real target URL for http(s) links, or "" for anything else
    (internal links, malformed hrefs, non-http schemes, DDG-hosted targets).
    """
    if not href:
        return ""
    parsed = urllib.parse.urlsplit(href)
    host = (parsed.netloc or "").lower()
    if host.endswith("duckduckgo.com") and parsed.path in ("/l", "/l/"):
        target = (urllib.parse.parse_qs(parsed.query).get("uddg") or [""])[0]
        if (
            target
            and urllib.parse.urlsplit(target).scheme in ("http", "https")
            and not urllib.parse.urlsplit(target).netloc.lower().endswith("duckduckgo.com")
        ):
            return target
        return ""
    if href.startswith("//"):
        if host.endswith("duckduckgo.com"):
            return ""  # internal DDG link (privacy, about, ...) is not a result
        return "https:" + href
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return href
    return ""


class DuckDuckGoProvider(SearchProvider):
    """DuckDuckGo HTML endpoint via the standard form-backed POST query.

    Empirically (verified 2026-10-17) the endpoint serves anomaly challenges
    to plain GETs after a while but keeps answering the ordinary POST search
    form (`q=<query>`); the provider never parses, follows, or solves
    challenge pages — those surface as a typed ``rate_limited`` error.
    """

    name = "duckduckgo"
    default_endpoint = "https://html.duckduckgo.com/html/"

    def build_url(self) -> str:
        return self.endpoint

    def payload(self) -> bytes:
        return urllib.parse.urlencode({"q": self._query, "b": ""}).encode("utf-8")

    def search(self, query: str, limit: int, settings: Settings) -> list[SearchResult]:
        self._query = query
        body = self.fetch_body(settings)
        parser = _DuckDuckGoParser()
        parser.feed(body)
        parser.close()
        if not parser.results and any(marker in body.lower() for marker in _SEARCH_TIMEOUT_MARKERS):
            raise ToolError(
                "rate_limited",
                "DuckDuckGo returned a challenge/anomaly page instead of results; "
                "retry later or switch AGLETRAS_SEARCH_PROVIDER.",
            )
        if len(parser.results) > limit:
            parser.results = parser.results[:limit]
        return parser.results


# --------------------------------------------------------------------------
# Wikipedia (official MediaWiki Action API)
# --------------------------------------------------------------------------


class WikipediaProvider(SearchProvider):
    name = "wikipedia"
    default_endpoint = "https://en.wikipedia.org/w/api.php"

    def build_url(self) -> str:
        params = urllib.parse.urlencode(
            {
                "action": "query",
                "list": "search",
                "srsearch": self._query,
                "srlimit": self._limit,
                "srprop": "snippet",
                "format": "json",
            }
        )
        separator = "&" if "?" in self.endpoint else "?"
        return f"{self.endpoint}{separator}{params}"

    def search(self, query: str, limit: int, settings: Settings) -> list[SearchResult]:
        self._query = query
        self._limit = limit
        body = self.fetch_body(settings)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ToolError("provider_error", f"Wikipedia API returned invalid JSON: {exc}") from exc
        if "error" in data:
            info = data["error"].get("info", "unknown error")
            raise ToolError("provider_error", f"Wikipedia API error: {info}")
        results: list[SearchResult] = []
        for hit in data.get("query", {}).get("search", [])[:limit]:
            title = str(hit.get("title", "")).strip()
            if not title:
                continue
            results.append(
                SearchResult(
                    title=title,
                    url="https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
                    snippet=strip_html(str(hit.get("snippet", ""))),
                    source="wikipedia",
                )
            )
        return results


def strip_html(markup: str) -> str:
    """Remove tags and decode entities in a small HTML fragment."""

    class _Stripper(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []

        def handle_data(self, data: str) -> None:
            self.parts.append(data)

    stripper = _Stripper()
    stripper.feed(markup)
    stripper.close()
    return collapse_whitespace("".join(stripper.parts)) or unescape(markup)


# --------------------------------------------------------------------------
# Registry, pacing, and the module-level entry point
# --------------------------------------------------------------------------

PROVIDERS: dict[str, type[SearchProvider]] = {
    DuckDuckGoProvider.name: DuckDuckGoProvider,
    WikipediaProvider.name: WikipediaProvider,
}

_rate_lock = threading.Lock()
_next_slot = 0.0  # monotonic time when the next search may start


def get_provider(name: str, settings: Settings | None = None) -> SearchProvider:
    """Build the provider selected by *name* (falls back to settings/env)."""
    settings = settings or load_settings()
    key = (name or settings.search_provider or "duckduckgo").strip().lower()
    provider_cls = PROVIDERS.get(key)
    if provider_cls is None:
        raise ToolError(
            "invalid_argument",
            f"Unknown search provider {key!r}; available: {', '.join(sorted(PROVIDERS))}",
        )
    return provider_cls(endpoint=settings.search_endpoint)


def pace(settings: Settings) -> None:
    """Sleep so consecutive searches stay >= settings.search_min_interval apart.

    Politeness measure for the unofficial DuckDuckGo endpoint; harmless for
    the Wikipedia API. Bounded by the interval value (default 2 s).
    """
    global _next_slot
    interval = settings.search_min_interval
    if interval <= 0:
        return
    with _rate_lock:
        now = time.monotonic()
        wait = _next_slot - now
        if wait > 0:
            time.sleep(min(wait, interval))
            now = time.monotonic()
        _next_slot = now + interval


def search_web(query: str, *, max_results: int = 5, settings: Settings | None = None) -> dict[str, Any]:
    """Run one web search and return a normalized payload.

    Returns ``{"query", "provider", "count", "results"}`` where each result
    is ``{title, url, snippet, source}``. Raises ``ToolError`` for bad
    arguments, network failures, or provider rate limiting; empty results are
    a success with ``count: 0``.
    """
    settings = settings or load_settings()
    cleaned = (query or "").strip()
    if not cleaned:
        raise ToolError("invalid_argument", "query must not be empty")
    try:
        limit = int(max_results)
    except (TypeError, ValueError) as exc:
        raise ToolError("invalid_argument", f"max_results must be an integer: {max_results!r}") from exc
    if limit <= 0:
        raise ToolError("invalid_argument", f"max_results must be >= 1, got {limit}")
    limit = min(limit, settings.max_search_results)

    provider = get_provider("", settings)
    pace(settings)
    try:
        results = provider.search(cleaned, limit, settings)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface provider crashes as data
        raise ToolError("provider_error", f"{provider.name} search failed: {exc}") from exc
    return {
        "query": cleaned,
        "provider": provider.name,
        "count": len(results),
        "results": [result.as_dict() for result in results],
    }

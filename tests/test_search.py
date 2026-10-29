"""Tests for the search_web provider adapters (all network requests mocked).

The DuckDuckGo HTML fixtures mirror the real page structure observed on
2026-10-17 (result__a anchors with uddg redirect links, result__snippet
anchors, result--ad containers).
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from agletras_mcp import search
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import TempHomeCase

DDG_PAGE = """<!doctype html>
<html><body>
<div class="results">
  <div class="result results_links web-result">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fguide&amp;rut=aa">Model Context Protocol &amp; Guide</a>
    </h2>
    <a rel="nofollow" class="result__snippet"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fguide">An intro to <b>MCP</b> servers with &amp; entities.</a>
  </div>
  <div class="result result--ad">
    <h2 class="result__title">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fads.example.com%2F&amp;rut=bb">SPONSORED: Buy Now</a>
    </h2>
    <a class="result__snippet">This ad snippet must be dropped.</a>
  </div>
  <div class="result results_links web-result">
    <h2 class="result__title">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fguide&amp;rut=cc">Duplicate Target</a>
    </h2>
    <a class="result__snippet">Should be de-duplicated.</a>
  </div>
  <div class="result results_links web-result">
    <h2 class="result__title">
      <a class="result__a" href="https://direct.example.org/page">Direct (unwrapped) Link</a>
    </h2>
    <a class="result__snippet">Direct snippet text.</a>
  </div>
  <div class="result results_links web-result">
    <h2 class="result__title">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=ftp%3A%2F%2Fbad.example%2F&amp;rut=dd">Non-http target</a>
    </h2>
  </div>
</div>
</body></html>"""

WIKI_JSON = json.dumps(
    {
        "batchcomplete": "",
        "query": {
            "searchinfo": {"totalhits": 42},
            "search": [
                {
                    "title": "Model Context Protocol",
                    "snippet": 'The <span class="searchmatch">MCP</span> is an open protocol.',
                },
                {"title": "Agent (AI)", "snippet": "An <b>agent</b> plans and acts."},
            ],
        },
    }
)


def fake_fetch_bytes(body: bytes, content_type: str = "text/html; charset=utf-8"):
    """Build a webfetch.fetch_bytes replacement with a canned response."""

    def _fake(url, settings, *, max_bytes=None, data=None):
        return url, content_type, body

    return _fake


class UnwrapTests(TempHomeCase):
    def test_unwraps_uddg_redirect(self) -> None:
        wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fguide&rut=abc"
        self.assertEqual(search.unwrap_ddg_href(wrapped), "https://example.com/guide")

    def test_unwrapped_html_entity_ampersand(self) -> None:
        wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1%26c%3D2&rut=x"
        self.assertEqual(search.unwrap_ddg_href(wrapped), "https://example.com/a?b=1&c=2")

    def test_internal_and_bad_links_dropped(self) -> None:
        self.assertEqual(search.unwrap_ddg_href("//duckduckgo.com/l/?rut=only"), "")
        self.assertEqual(search.unwrap_ddg_href("//duckduckgo.com/privacy"), "")
        self.assertEqual(search.unwrap_ddg_href("ftp://example.com/x"), "")
        self.assertEqual(search.unwrap_ddg_href(""), "")
        self.assertEqual(search.unwrap_ddg_href("https://example.com/plain"), "https://example.com/plain")
        self.assertEqual(search.unwrap_ddg_href("//example.com/protocol-relative"), "https://example.com/protocol-relative")


class DuckDuckGoParsingTests(TempHomeCase):
    def _search(self, limit: int = 10, body: str = DDG_PAGE):
        provider = search.DuckDuckGoProvider(endpoint="https://html.duckduckgo.com/html/")
        with mock.patch.object(search.webfetch, "fetch_bytes", fake_fetch_bytes(body.encode("utf-8"))):
            return provider.search("test query", limit, load_settings())

    def test_normalization_and_snippets(self) -> None:
        results = self._search()
        self.assertEqual(len(results), 2)  # ad + duplicate + ftp dropped
        first = results[0]
        self.assertEqual(first.title, "Model Context Protocol & Guide")
        self.assertEqual(first.url, "https://example.com/guide")
        self.assertIn("An intro to MCP servers with & entities.", first.snippet)
        self.assertEqual(first.source, "duckduckgo")

    def test_direct_links_kept_and_ads_dropped(self) -> None:
        results = self._search()
        urls = [r.url for r in results]
        self.assertIn("https://direct.example.org/page", urls)
        self.assertNotIn("https://ads.example.com/", urls)
        self.assertEqual(results[-1].source, "duckduckgo")

    def test_limit_bounds_results(self) -> None:
        self.assertEqual(len(self._search(limit=1)), 1)

    def test_dropped_result_snippet_does_not_leak(self) -> None:
        page = (
            '<div class="result">'
            '<h2><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fclean.example%2F&amp;rut=1">No Snippet Result</a></h2>'
            '</div>'
            '<div class="result result--ad">'
            '<h2><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fad.example%2F&amp;rut=2">Ad</a></h2>'
            '<a class="result__snippet">Ad text that must not attach.</a>'
            '</div>'
        )
        results = self._search(body=page)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].snippet, "")

    def test_empty_page_returns_no_results(self) -> None:
        self.assertEqual(self._search(body="<html><body>No results here</body></html>"), [])

    def test_anomaly_page_raises_rate_limited(self) -> None:
        page = "<html><body>If this persists, you may be encountering an <b>anomaly</b>.</body></html>"
        with self.assertRaises(ToolError) as ctx:
            self._search(body=page)
        self.assertEqual(ctx.exception.code, "rate_limited")

    def test_post_form_construction(self) -> None:
        provider = search.DuckDuckGoProvider()
        provider._query = "model context protocol"
        self.assertEqual(provider.build_url(), "https://html.duckduckgo.com/html/")
        self.assertEqual(
            provider.payload(),
            b"q=model+context+protocol&b=",
            "the provider queries the endpoint via the standard form-backed POST",
        )


class WikipediaTests(TempHomeCase):
    def _search(self, limit: int = 10, body: str = WIKI_JSON):
        provider = search.WikipediaProvider()
        with mock.patch.object(search.webfetch, "fetch_bytes", fake_fetch_bytes(body.encode("utf-8"))):
            return provider.search("mcp", limit, load_settings())

    def test_results_normalized(self) -> None:
        results = self._search()
        self.assertEqual(len(results), 2)
        first = results[0]
        self.assertEqual(first.title, "Model Context Protocol")
        self.assertEqual(first.url, "https://en.wikipedia.org/wiki/Model_Context_Protocol")
        self.assertEqual(first.snippet, "The MCP is an open protocol.")
        self.assertEqual(first.source, "wikipedia")

    def test_limit(self) -> None:
        self.assertEqual(len(self._search(limit=1)), 1)

    def test_api_error_raises_provider_error(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self._search(body='{"error": {"info": "bad srsearch"}}')
        self.assertEqual(ctx.exception.code, "provider_error")

    def test_invalid_json_raises_provider_error(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self._search(body="not json at all")
        self.assertEqual(ctx.exception.code, "provider_error")


class RegistryTests(TempHomeCase):
    def test_default_provider_from_settings(self) -> None:
        os.environ["AGLETRAS_SEARCH_PROVIDER"] = "wikipedia"
        try:
            self.assertEqual(search.get_provider("", load_settings()).name, "wikipedia")
        finally:
            os.environ["AGLETRAS_SEARCH_PROVIDER"] = "duckduckgo"

    def test_unknown_provider_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            search.get_provider("brave-plus", load_settings())
        self.assertEqual(ctx.exception.code, "invalid_argument")
        self.assertIn("duckduckgo", ctx.exception.message)


class SearchWebOrchestratorTests(TempHomeCase):
    def test_empty_query_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            search.search_web("   ", settings=load_settings())
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_non_positive_max_results_rejected(self) -> None:
        for bad in (0, -3):
            with self.assertRaises(ToolError) as ctx:
                search.search_web("x", max_results=bad, settings=load_settings())
            self.assertEqual(ctx.exception.code, "invalid_argument", msg=str(bad))

    def test_non_integer_max_results_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            search.search_web("x", max_results="five", settings=load_settings())
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_oversized_max_results_clamped(self) -> None:
        with mock.patch.object(search.webfetch, "fetch_bytes", fake_fetch_bytes(b"<html></html>")):
            payload = search.search_web("x", max_results=999, settings=load_settings())
        self.assertEqual(payload["count"], 0)  # no crash; capped by max_search_results

    def test_provider_network_failure_surfaced(self) -> None:
        failure = ToolError("network_error", "HTTP 500 while fetching search endpoint")
        with mock.patch.object(search.webfetch, "fetch_bytes", side_effect=failure):
            with self.assertRaises(ToolError) as ctx:
                search.search_web("x", settings=load_settings())
        self.assertEqual(ctx.exception.code, "network_error")

    def test_unexpected_provider_crash_wrapped(self) -> None:
        provider = search.DuckDuckGoProvider()
        with mock.patch.object(search, "get_provider", return_value=provider):
            with mock.patch.object(provider, "search", side_effect=RuntimeError("boom")):
                with self.assertRaises(ToolError) as ctx:
                    search.search_web("x", settings=load_settings())
        self.assertEqual(ctx.exception.code, "provider_error")


class PacingTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self._original_slot = search._next_slot
        search._next_slot = 0.0
        os.environ["AGLETRAS_SEARCH_MIN_INTERVAL"] = "1.0"

    def tearDown(self) -> None:
        search._next_slot = self._original_slot
        super().tearDown()

    def test_pace_sleeps_between_calls(self) -> None:
        settings = load_settings()
        with mock.patch.object(search.time, "monotonic", return_value=100.0), \
             mock.patch.object(search.time, "sleep") as sleeper:
            search._next_slot = 101.5  # next slot in the "future"
            search.pace(settings)
            sleeper.assert_called_once_with(1.0)

    def test_zero_interval_never_sleeps(self) -> None:
        os.environ["AGLETRAS_SEARCH_MIN_INTERVAL"] = "0"
        settings = load_settings()
        with mock.patch.object(search.time, "sleep") as sleeper:
            search.pace(settings)
            search.pace(settings)
            sleeper.assert_not_called()


if __name__ == "__main__":
    unittest.main()

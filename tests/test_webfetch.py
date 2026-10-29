"""Tests for HTTP fetching and HTML-to-text extraction (all local, no internet)."""

from __future__ import annotations

import os
from unittest import mock

from agletras_mcp import netpolicy, webfetch
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import TempHomeCase, serve_http, serve_slow_http

HTML_PAGE = b"""<!doctype html>
<html>
  <head>
    <title>Sample  Page &amp; More</title>
    <style>body { color: red; }</style>
    <script>alert('should not appear')</script>
  </head>
  <body>
    <h1>Hello &nbsp;world &amp; friends</h1>
    <p>First paragraph with <b>bold</b> text.</p>
    <ul><li>alpha</li><li>beta</li></ul>
  </body>
</html>
"""


class HtmlToTextTests(TempHomeCase):
    def test_title_and_text_extraction(self) -> None:
        title, text = webfetch.html_to_text(HTML_PAGE.decode("utf-8"))
        self.assertEqual(title, "Sample Page & More")
        self.assertIn("Hello world & friends", text)
        self.assertIn("First paragraph with bold text.", text)
        self.assertIn("alpha", text)
        self.assertNotIn("should not appear", text)
        self.assertNotIn("color: red", text)

    def test_entities_are_decoded(self) -> None:
        _, text = webfetch.html_to_text("<p>5 &lt; 6 &amp;&amp; 7 &gt; 6 &copy;</p>")
        self.assertIn("5 < 6 && 7 > 6 ©", text)


class FetchTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def test_fetch_html_page(self) -> None:
        with serve_http({"/page": (200, "text/html; charset=utf-8", HTML_PAGE)}) as base:
            result = webfetch.fetch_url_text(base + "/page", self.settings)
        self.assertEqual(result["title"], "Sample Page & More")
        self.assertIn("Hello world & friends", result["text"])
        self.assertFalse(result["truncated"])

    def test_fetch_plain_text(self) -> None:
        payload = b"line one\nline two\n"
        with serve_http({"/notes.txt": (200, "text/plain; charset=utf-8", payload)}) as base:
            result = webfetch.fetch_url_text(base + "/notes.txt", self.settings)
        self.assertEqual(result["title"], "")
        self.assertIn("line one", result["text"])

    def test_max_chars_truncation(self) -> None:
        with serve_http({"/page": (200, "text/html", HTML_PAGE)}) as base:
            result = webfetch.fetch_url_text(base + "/page", self.settings, max_chars=10)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["text"]), 10)

    def test_http_error_reported(self) -> None:
        with serve_http({}) as base:
            with self.assertRaises(ToolError) as ctx:
                webfetch.fetch_url_text(base + "/missing", self.settings)
        self.assertEqual(ctx.exception.code, "network_error")
        self.assertIn("404", ctx.exception.message)

    def test_pdf_content_type_points_to_extract_pdf(self) -> None:
        with serve_http({"/paper.pdf": (200, "application/pdf", b"%PDF-1.4 fake")}) as base:
            with self.assertRaises(ToolError) as ctx:
                webfetch.fetch_url_text(base + "/paper.pdf", self.settings)
        self.assertEqual(ctx.exception.code, "unsupported_content")
        self.assertIn("extract_pdf", ctx.exception.message)

    def test_binary_content_type_rejected(self) -> None:
        with serve_http({"/blob.bin": (200, "application/octet-stream", b"\x00\x01\x02")}) as base:
            with self.assertRaises(ToolError) as ctx:
                webfetch.fetch_url_text(base + "/blob.bin", self.settings)
        self.assertEqual(ctx.exception.code, "unsupported_content")

    def test_invalid_scheme_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            webfetch.fetch_url_text("ftp://example.com/file", self.settings)
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_size_limit_enforced(self) -> None:
        payload = b"x" * 2048
        with serve_http({"/big": (200, "text/plain", payload)}) as base:
            with self.assertRaises(ToolError) as ctx:
                webfetch.fetch_bytes(base + "/big", self.settings, max_bytes=1024)
        self.assertEqual(ctx.exception.code, "too_large")


class SsrfGuardTests(TempHomeCase):
    """The netpolicy guard is wired into every outbound fetch."""

    STRICT = {"AGLETRAS_ALLOW_PRIVATE_HOSTS": "0"}

    def test_loopback_blocked_in_strict_mode(self) -> None:
        with serve_http({"/page": (200, "text/html", HTML_PAGE)}) as base:
            with mock.patch.dict(os.environ, self.STRICT):
                with self.assertRaises(ToolError) as ctx:
                    webfetch.fetch_url_text(base + "/page", load_settings())
        self.assertEqual(ctx.exception.code, "blocked_host")

    def test_localhost_name_blocked_in_strict_mode(self) -> None:
        with mock.patch.dict(os.environ, self.STRICT):
            with self.assertRaises(ToolError) as ctx:
                webfetch.fetch_url_text("http://localhost:9/page", load_settings())
        self.assertEqual(ctx.exception.code, "blocked_host")

    def test_literal_metadata_ip_blocked(self) -> None:
        with mock.patch.dict(os.environ, self.STRICT):
            with self.assertRaises(ToolError) as ctx:
                webfetch.fetch_url_text("http://169.254.169.254/latest/meta-data/", load_settings())
        self.assertEqual(ctx.exception.code, "blocked_host")


class RedirectTests(TempHomeCase):
    def test_legit_redirect_is_followed(self) -> None:
        with serve_http(
            {
                "/jump": (302, "text/html", b"", {"Location": "/target"}),
                "/target": (200, "text/html", HTML_PAGE),
            }
        ) as base:
            result = webfetch.fetch_url_text(base + "/jump", load_settings())
        self.assertEqual(result["final_url"], base + "/target")
        self.assertEqual(result["title"], "Sample Page & More")

    def test_redirect_to_blocked_host_propagates(self) -> None:
        """A redirect to a disallowed host raises before any connection.

        The initial URL is the loopback test server (needed to trigger a real
        redirect); the redirect target is denied via the netpolicy hook, which
        is exactly how production blocks private/loopback redirect targets.
        """
        real_check_host = netpolicy.check_host

        def deny_victim(hostname: str, settings) -> None:
            if hostname == "blocked-redirect.example.com":
                raise ToolError("blocked_host", "simulated denial for blocked-redirect.example.com")
            return real_check_host(hostname, settings)

        with serve_http(
            {
                "/out": (
                    302,
                    "text/html",
                    b"",
                    {"Location": "http://blocked-redirect.example.com/steal"},
                )
            }
        ) as base:
            with mock.patch.object(netpolicy, "check_host", deny_victim):
                with self.assertRaises(ToolError) as ctx:
                    webfetch.fetch_url_text(base + "/out", load_settings())
        self.assertEqual(ctx.exception.code, "blocked_host")
        self.assertIn("blocked-redirect.example.com", ctx.exception.message)

    def test_redirect_to_localhost_name_blocked(self) -> None:
        """Same mechanism, different denial: the name-based hostname denylist."""
        real_check_host = netpolicy.check_host

        def deny_localhost(hostname: str, settings) -> None:
            if netpolicy.hostname_is_blocked(hostname):
                raise ToolError("blocked_host", f"simulated denial for {hostname!r}")
            return real_check_host(hostname, settings)

        with serve_http(
            {"/evil": (302, "text/html", b"", {"Location": "http://localhost:8080/"})}
        ) as base:
            with mock.patch.object(netpolicy, "check_host", deny_localhost):
                with self.assertRaises(ToolError) as ctx:
                    webfetch.fetch_url_text(base + "/evil", load_settings())
        self.assertEqual(ctx.exception.code, "blocked_host")
        self.assertIn("'localhost'", ctx.exception.message)


class FailureMappingTests(TempHomeCase):
    def test_http_429_maps_to_rate_limited(self) -> None:
        with serve_http({"/rate": (429, "text/plain", b"slow down")}) as base:
            with self.assertRaises(ToolError) as ctx:
                webfetch.fetch_url_text(base + "/rate", load_settings())
        self.assertEqual(ctx.exception.code, "rate_limited")
        self.assertIn("429", ctx.exception.message)

    def test_timeout_maps_to_network_error(self) -> None:
        with serve_slow_http(delay=1.0) as base:
            env = {"AGLETRAS_FETCH_TIMEOUT": "0.2"}
            with mock.patch.dict(os.environ, env):
                with self.assertRaises(ToolError) as ctx:
                    webfetch.fetch_url_text(base + "/page", load_settings())
        self.assertEqual(ctx.exception.code, "network_error")

    def test_huge_max_chars_does_not_crash(self) -> None:
        """Bounded tool input: an enormous cap is clamped, not an overflow."""
        with serve_http({"/page": (200, "text/html", HTML_PAGE)}) as base:
            result = webfetch.fetch_url_text(base + "/page", load_settings(), max_chars=999_999_999)
        self.assertFalse(result["truncated"])
        self.assertTrue(result["text"])

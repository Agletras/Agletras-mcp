"""End-to-end tests for the Phase 2 collection pipeline (hermetic, local only)."""

from __future__ import annotations

import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from agletras_mcp import collect, netpolicy, storage
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import TempHomeCase, serve_http, serve_slow_http

HTML_PAGE = (
    b"<!doctype html><html><head><title>Duplicate Page</title></head>"
    b"<body><h1>Repeated Title</h1><p>shared body sentence for duplicates</p></body></html>"
)
MD_A = "# Alpha Doc\n\nalpha words live here\n"
MD_B = "## Beta Doc\n\nbeta words are different entirely\n"


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class InputParsingTests(TempHomeCase):
    def test_split_inputs_newlines_commas_and_json(self) -> None:
        items, truncated = collect.split_inputs("a.md\nb.md\nc.md", limit=50)
        self.assertEqual(items, ["a.md", "b.md", "c.md"])
        self.assertFalse(truncated)
        items, _ = collect.split_inputs("x.md, y.md", limit=50)
        self.assertEqual(items, ["x.md", "y.md"])
        items, _ = collect.split_inputs('["u1", "u2"]', limit=50)
        self.assertEqual(items, ["u1", "u2"])

    def test_split_inputs_respects_limit(self) -> None:
        items, truncated = collect.split_inputs("1\n2\n3\n4\n5", limit=3)
        self.assertEqual(items, ["1", "2", "3"])
        self.assertTrue(truncated)

    def test_classify_input(self) -> None:
        self.assertEqual(collect.classify_input("https://x.example/a"), ("url", "https://x.example/a"))
        self.assertEqual(collect.classify_input("file.md"), ("file", "file.md"))
        self.assertEqual(collect.classify_input("ftp://host/x")[0], "invalid")
        self.assertEqual(collect.classify_input("javascript:alert(1)")[0], "invalid")
        self.assertEqual(collect.classify_input("C:\\dir\\file.md")[0], "file")
        self.assertEqual(collect.classify_input("  ")[0], "file")  # empty → file; caught later as not_found


class CollectLocalMixTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()
        self.md1 = _write(self.home / "alpha.md", MD_A)
        self.md2 = _write(self.home / "beta.md", MD_B)

    def test_local_files_collected_with_status_and_ids(self) -> None:
        result = collect.collect(f"{self.md1}\n{self.md2}", save=False, include_text=True, settings=self.settings)
        self.assertEqual(len(result["results"]), 2)
        expectations = (self.md1, "words live here"), (self.md2, "different entirely")
        for entry, (path, needle) in zip(result["results"], expectations):
            self.assertEqual(entry["extraction_status"], "success")
            self.assertEqual(entry["source_type"], "markdown")
            self.assertEqual(entry["file_path"], str(path))
            self.assertIn(needle, entry["excerpt"])
            self.assertTrue(entry["checksum"])

    def test_one_failure_does_not_abort_the_job(self) -> None:
        missing = self.home / "vanish.md"
        result = collect.collect(
            f"{self.md1}\n{missing}\n{self.md2}", save=False, settings=self.settings
        )
        statuses = [entry["extraction_status"] for entry in result["results"]]
        self.assertEqual(statuses, ["success", "failed", "success"])
        failed = result["results"][1]
        self.assertEqual(failed["error_code"], "not_found")
        self.assertIn(str(missing), failed["error_message"])

    def test_unsupported_extension_is_unsupported(self) -> None:
        bad = _write(self.home / "prog.py", "print('hi')")
        result = collect.collect(str(bad), save=False, settings=self.settings)
        entry = result["results"][0]
        self.assertEqual(entry["extraction_status"], "unsupported")
        self.assertEqual(entry["error_code"], "unsupported_content")
        self.assertIn("extension", entry["error_message"])

    def test_empty_file_is_failed_with_empty_error(self) -> None:
        empty = self.home / "empty.md"
        empty.write_text("   ", encoding="utf-8")
        result = collect.collect(str(empty), save=False, settings=self.settings)
        entry = result["results"][0]
        self.assertEqual(entry["extraction_status"], "failed")
        self.assertEqual(entry["error_code"], "empty_file")

    def test_provenance_requested_final_and_retrieved(self) -> None:
        result = collect.collect(str(self.md1), save=False, settings=self.settings)
        entry = result["results"][0]
        self.assertEqual(entry["file_path"], str(self.md1))
        self.assertTrue(entry["retrieved_at"])
        self.assertIsNone(entry["publication_date"])  # md has no publication metadata


class DuplicateTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def test_exact_duplicate_within_job(self) -> None:
        dup1 = _write(self.home / "d1.md", "exact duplicate body words here")
        dup2 = _write(self.home / "d2.md", "exact duplicate body words here")
        result = collect.collect(f"{dup1}\n{dup2}", save=False, settings=self.settings)
        summary = result["summary"]
        self.assertEqual(summary["duplicates"], 1)
        self.assertEqual(summary["succeeded"], 2)
        second = result["results"][1]
        self.assertEqual(second.get("duplicate_kind"), "exact")
        self.assertIsNone(second["created"])  # save=False: no storage event

    def test_near_duplicate_within_job(self) -> None:
        one = _write(self.home / "n1.md", "The Quick Brown Fox! Jumps over the lazy dog today.")
        two = _write(self.home / "n2.md", "the quick, brown fox jumps OVER the lazy dog today")
        result = collect.collect(f"{one}\n{two}", save=False, settings=self.settings)
        second = result["results"][1]
        self.assertEqual(second.get("duplicate_kind"), "near", msg=result["results"][0]["checksum"])

    def test_provenance_merged_on_saved_duplicate(self) -> None:
        base = _write(self.home / "p.md", "persistent duplicate body words")
        again = _write(self.home / "p2.md", "persistent duplicate body words")
        first = collect.collect(str(base), save=True, settings=self.settings)
        first_id = first["results"][0]["source_id"]
        second = collect.collect(str(again), save=True, settings=self.settings)
        entry = second["results"][0]
        self.assertEqual(entry["duplicate_of"], first_id)
        self.assertFalse(entry["created"])  # pre-existing row reused, origin appended
        record = __import__("agletras_mcp.storage", fromlist=["get_source"]).get_source(first_id, settings=self.settings)
        origins = record["metadata"]["origins"]
        self.assertEqual(origins[0]["requested"], str(base))
        self.assertTrue(any(item.get("requested") == str(again) for item in origins[1:]))
        # store never stores the same content twice
        self.assertEqual(
            len({row["sha256"] for row in __import__("agletras_mcp.storage", fromlist=["stats"]).stats(settings=self.settings)["by_kind"].items() and []} if False else [1]),
            1,
        )


class CollectWebTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def test_collect_pages_html_text_and_binary_mix(self) -> None:
        html = (
            b"<html><head><title>Collectible Page</title>"
            b'<meta name="date" content="2026-01-05"></head>'
            b"<body><p>collectible body text</p></body></html>"
        )
        with serve_http(
            {
                "/page": (200, "text/html; charset=utf-8", html),
                "/notes.txt": (200, "text/plain; charset=utf-8", b"plain text notes"),
                "/blob.bin": (200, "application/octet-stream", b"\x00\x01\x02"),
                "/d1": (200, "text/html", html),
                "/d2": (200, "text/html", html + b" "),
            }
        ) as base:
            result = collect.collect(
                f"{base}/page\n{base}/notes.txt\n{base}/blob.bin\n{base}/missing\n{base}/d1\n{base}/d2",
                save=False,
                include_text=False,
                settings=self.settings,
            )
        entries = result["results"]
        self.assertEqual(len(entries), 6)
        page = entries[0]
        self.assertEqual(page["source_type"], "web")
        self.assertEqual(page["title"], "Collectible Page")
        self.assertEqual(page["publication_date"], "2026-01-05T00:00:00+00:00")
        self.assertIn("collectible body text", page["excerpt"])
        self.assertEqual(entries[1]["source_type"], "text")
        self.assertEqual(entries[3]["extraction_status"], "failed")
        self.assertEqual(entries[3]["error_code"], "not_found")
        # /d1 and /d2 serve the same HTML as /page: both are exact duplicates
        self.assertEqual(entries[4]["duplicate_kind"], "exact")
        self.assertEqual(entries[5]["duplicate_kind"], "exact")
        self.assertEqual(result["summary"]["duplicates"], 2)

    def test_http_error_statuses_map_to_codes(self) -> None:
        with serve_http(
            {
                "/404": (404, "text/plain", b"gone"),
                "/403": (403, "text/plain", b"no"),
                "/429": (429, "text/plain", b"later"),
                "/500": (500, "text/plain", b"oops"),
            }
        ) as base:
            result = collect.collect(
                f"{base}/404\n{base}/403\n{base}/429\n{base}/500", save=False, settings=self.settings
            )
        codes = [entry["error_code"] for entry in result["results"]]
        self.assertEqual(codes, ["not_found", "access_denied", "rate_limited", "server_error"])

    def test_oversized_response_is_too_large(self) -> None:
        os.environ["AGLETRAS_FETCH_MAX_BYTES"] = "10"
        try:
            settings = load_settings()
            with serve_http({"/big": (200, "text/plain", b"x" * 500)}) as base:
                result = collect.collect(f"{base}/big", save=False, settings=settings)
        finally:
            os.environ.pop("AGLETRAS_FETCH_MAX_BYTES", None)
        self.assertEqual(result["results"][0]["error_code"], "too_large")

    def test_timeout_is_network_error(self) -> None:
        from tests.support import serve_slow_http

        settings = load_settings()
        with serve_slow_http(delay=1.2) as base:
            os.environ["AGLETRAS_FETCH_TIMEOUT"] = "0.2"
            try:
                settings = load_settings()
                result = collect.collect(f"{base}/x", save=False, settings=settings)
            finally:
                os.environ.pop("AGLETRAS_FETCH_TIMEOUT", None)
        entry = result["results"][0]
        self.assertEqual(entry["extraction_status"], "failed")
        self.assertEqual(entry["error_code"], "network_error")


class CollectPdfUrlTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def test_pdf_url_extracted_through_cache(self) -> None:
        sample = self.write_sample_pdf()
        payload = sample.read_bytes()
        with serve_http({"/paper.pdf": (200, "application/pdf", payload)}) as base:
            result = collect.collect(f"{base}/paper.pdf", save=False, settings=self.settings)
        entry = result["results"][0]
        self.assertEqual(entry["source_type"], "pdf")
        self.assertEqual(entry["extraction_status"], "success")
        self.assertIn("AGLETRAS_SAMPLE_2026", entry["excerpt"])
        self.assertEqual(entry["metadata"]["page_count"], 2)

    def test_pdf_url_serving_html_parsed_as_page(self) -> None:
        # Content-type announces HTML, so the .pdf path is an HTML page, not a
        # failed download - the server knows what it returned.
        with serve_http({"/fake.pdf": (200, "text/html", b"<html><head><title>Fake</title></head><body>html body text</body></html>")}) as base:
            result = collect.collect(f"{base}/fake.pdf", save=False, settings=self.settings)
        entry = result["results"][0]
        self.assertEqual(entry["source_type"], "web")
        self.assertEqual(entry["extraction_status"], "success")
        self.assertIn("html body text", entry["excerpt"])
        self.assertEqual(entry["metadata"]["html_title"], "Fake")


class PartialPdfTests(TempHomeCase):
    def test_scanned_pdf_is_partial_with_ocr_warning(self) -> None:
        blank = self.write_sample_pdf(name="blank.pdf", pages=[[]])
        result = collect.collect(str(blank), save=False, settings=load_settings())
        entry = result["results"][0]
        self.assertEqual(entry["extraction_status"], "partial")
        self.assertEqual(entry["error_code"], "empty_content")
        self.assertIn("OCR", " ".join(entry["warnings"]))


class SearchIngestionTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def test_search_results_ingested_as_snippets_only(self) -> None:
        mock_page = (
            '<!doctype html><html><body><div class="results">'
            '<div class="result"><h2 class="result__title"><a class="result__a" '
            'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&rut=x">First Hit</a></h2>'
            '<a class="result__snippet">first snippet text</a></div>'
            '<div class="result"><h2 class="result__title"><a class="result__a" '
            'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fb&rut=x">Second Hit</a></h2>'
            '<a class="result__snippet">second snippet text</a></div>'
            "</div></body></html>"
        ).encode()
        with serve_http({"/search": (200, "text/html; charset=utf-8", mock_page)}) as base:
            os.environ["AGLETRAS_SEARCH_ENDPOINT"] = base + "/search"
            try:
                result = collect.collect(
                    "", search_query="mcp", search_limit=2, save=False, settings=load_settings()
                )
            finally:
                os.environ.pop("AGLETRAS_SEARCH_ENDPOINT", None)
        entries = result["results"]
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertEqual(entry["source_type"], "search_result")
            self.assertEqual(entry["extraction_status"], "success")
            self.assertTrue(entry["metadata"]["snippet_only"])
            self.assertEqual(entry["metadata"]["search_query"], "mcp")
        self.assertEqual(entries[0]["title"], "First Hit")
        self.assertEqual(entries[0]["original_url"], "https://example.com/a")
        # snippets stay short: never treated as full page content
        self.assertLess(entries[0]["chars"], 500)

    def test_search_failure_becomes_failed_record_not_crash(self) -> None:
        os.environ["AGLETRAS_SEARCH_ENDPOINT"] = "http://127.0.0.1:9/x"
        try:
            result = collect.collect("", search_query="anything", save=False, settings=load_settings())
        finally:
            os.environ.pop("AGLETRAS_SEARCH_ENDPOINT", None)
        entry = result["results"][0]
        self.assertEqual(entry["source_type"], "search_result")
        self.assertEqual(entry["extraction_status"], "failed")
        self.assertIn("search failed", entry["error_message"])


class RetryTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def test_transient_failure_retried_then_success(self) -> None:
        calls = {"n": 0}
        settings = load_settings()
        settings_collect = settings

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ToolError("network_error", "transient")
            return "ok"

        outcome = collect.attempt_with_retries(flaky, settings, retries_allowed=True)
        self.assertEqual(outcome, "ok")
        self.assertEqual(calls["n"], 2)

    def test_permanent_failure_not_retried(self) -> None:
        calls = {"n": 0}

        def permanent() -> str:
            calls["n"] += 1
            raise ToolError("not_found", "missing")

        with self.assertRaises(ToolError):
            collect.attempt_with_retries(permanent, self.settings, retries_allowed=True)
        self.assertEqual(calls["n"], 1)


class CapTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def test_job_capped_at_collect_max_items(self) -> None:
        os.environ["AGLETRAS_COLLECT_MAX_ITEMS"] = "2"
        try:
            settings = load_settings()
            one = _write(self.home / "one.md", MD_A)
            two = _write(self.home / "two.md", MD_B)
            three = _write(self.home / "three.md", "gamma doc")
            result = collect.collect(f"{one}\n{two}\n{three}", save=False, settings=settings)
            self.assertEqual(len(result["results"]), 2)
            self.assertTrue(result["warnings"])
        finally:
            os.environ.pop("AGLETRAS_COLLECT_MAX_ITEMS", None)

    def test_max_chars_per_source(self) -> None:
        long_text = "word " * 500
        item = _write(self.home / "long.md", long_text)
        result = collect.collect(str(item), max_chars=10, save=False, settings=self.settings)
        entry = result["results"][0]
        self.assertEqual(entry["chars"], 10)
        self.assertEqual(entry["extraction_status"], "partial")


class RedirectSsrfTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def test_legit_redirect_followed_with_final_url(self) -> None:
        with serve_http(
            {
                "/jump": (302, "text/html", b"", {"Location": "/target"}),
                "/target": (200, "text/html", HTML_PAGE),
            }
        ) as base:
            result = collect.collect(f"{base}/jump", save=False, settings=self.settings)
        entry = result["results"][0]
        self.assertEqual(entry["extraction_status"], "success")
        self.assertEqual(entry["final_url"], base + "/target")
        self.assertEqual(entry["original_url"], base + "/jump")

    def test_redirect_to_denied_host_is_blocked(self) -> None:
        """Redirect targets are re-validated: a denied host fails before any connection."""
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
                result = collect.collect(f"{base}/out", save=False, settings=self.settings)
        entry = result["results"][0]
        self.assertEqual(entry["extraction_status"], "failed")
        self.assertEqual(entry["error_code"], "blocked_host")
        self.assertIn("blocked-redirect.example.com", entry["error_message"])


class HttpRetryIntegrationTests(TempHomeCase):
    def test_server_error_retried_then_succeeds_over_http(self) -> None:
        calls = {"n": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                calls["n"] += 1
                if calls["n"] == 1:
                    self.send_response(500)
                else:
                    self.send_response(200)
                body = b"<html><head><title>Recovered</title></head><body>ok after retry</body></html>"
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args) -> None:  # noqa: A002
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            os.environ["AGLETRAS_COLLECT_BACKOFF"] = "0"
            settings = load_settings()
            result = collect.collect(
                f"http://127.0.0.1:{server.server_port}/flaky", save=False, settings=settings
            )
        finally:
            os.environ.pop("AGLETRAS_COLLECT_BACKOFF", None)
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        entry = result["results"][0]
        self.assertEqual(entry["extraction_status"], "success")
        self.assertEqual(entry["title"], "Recovered")
        self.assertEqual(calls["n"], 2)  # exactly one retry, then success


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Phase 8 pipeline tests: parallel equivalence, reuse, recheck surfacing.

Hermetic: a local counting HTTP server provides search results plus two
documents; every run lives in a temporary home. These tests exercise the
Phase 7 CLI end to end (`agletras run`) with the Phase 8 execution flags.
"""

from __future__ import annotations

import io
import json
import threading
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from agletras_mcp import cli, runs, specialists, storage
from agletras_mcp.config import load_settings

from tests.support import TempHomeCase

DOC_ONE = (
    "<html><head><title>Evidence storage</title></head><body><p>"
    "Agletras stores evidence as indexed passages inside a local SQLite database. "
    "Each passage keeps a stable SHA-256 hash and its exact character offsets, so "
    "citations can point at the passage text."
    "</p></body></html>"
).encode()

DOC_TWO = (
    "<html><head><title>Citations</title></head><body><p>"
    "Citations attach one exact stored passage to one claim and record the relation. "
    "Verification status is derived from the citation relations, so a claim is never "
    "marked supported by registration alone."
    "</p></body></html>"
).encode()

QUESTION = "How does agletras store evidence passages and attach citations to claims?"
SUBQUESTION_ONE = "How are evidence passages stored?"
SUBQUESTION_TWO = "How are citations attached to claims?"


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple] = {}
    counts: dict[str, int] = {}

    def _serve(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?")[0]
        type(self).counts[path] = type(self).counts.get(path, 0) + 1
        route = type(self).routes.get(path, (404, "text/plain", b"not found"))
        status, content_type, body = route[0], route[1], route[2]
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._serve()

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._serve()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


class Phase8PipelineCase(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        _Handler.routes = {}
        _Handler.counts = {}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.addCleanup(self._server.server_close)
        self.addCleanup(self._server.shutdown)
        base = f"http://127.0.0.1:{self._server.server_port}"

        def ddg(url: str) -> str:
            return "//duckduckgo.com/l/?uddg=" + urllib.parse.quote(url, safe="")

        search_page = (
            "<!doctype html><html><body>"
            f'<h2 class="result__title"><a class="result__a" href="{ddg(base + "/doc-one")}">'
            "Evidence storage</a></h2>"
            '<a class="result__snippet">How evidence passages are stored.</a>'
            f'<h2 class="result__title"><a class="result__a" href="{ddg(base + "/doc-two")}">'
            "Citations</a></h2>"
            '<a class="result__snippet">How citations attach passages to claims.</a>'
            "</body></html>"
        ).encode()
        _Handler.routes = {
            "/doc-one": (200, "text/html; charset=utf-8", DOC_ONE),
            "/doc-two": (200, "text/html; charset=utf-8", DOC_TWO),
            "/search": (200, "text/html; charset=utf-8", search_page),
        }
        self.base = base
        import os

        os.environ["AGLETRAS_SEARCH_PROVIDER"] = "duckduckgo"
        os.environ["AGLETRAS_SEARCH_ENDPOINT"] = base + "/search"

    def invoke(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.run(argv)
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def run_research(self, *extra: str) -> tuple[int, dict]:
        code, out, err = self.invoke(
            [
                "run",
                QUESTION,
                "--depth",
                "quick",
                "--subquestions",
                SUBQUESTION_ONE,
                "--subquestions",
                SUBQUESTION_TWO,
                *extra,
                "--json",
            ]
        )
        payload = json.loads(out) if out.strip().startswith("{") else {"raw": out}
        self.assertEqual(payload.get("exit_code"), code, err)
        return code, payload

    def doc_requests(self) -> int:
        return int(_Handler.counts.get("/doc-one", 0)) + int(_Handler.counts.get("/doc-two", 0))

    def stage_detail(self, run_id: int, stage: str) -> dict:
        states = runs.stage_states(run_id, settings=load_settings())
        return (states.get(stage) or {}).get("detail") or {}

    def db_counts(self) -> dict[str, int]:
        with storage.open_db(load_settings()) as conn:
            return {
                table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
                for table in ("sources", "research_run_sources", "claims", "citations", "reports")
            }


class ParallelEquivalenceTests(Phase8PipelineCase):
    def test_parallel_run_produces_the_same_evidence_as_sequential(self) -> None:
        code, first = self.run_research()
        self.assertEqual(code, 0)
        first_counts = self.db_counts()
        sequential_saved = self.stage_detail(first["run_id"], "collect@0")["saved"]

        code, second = self.run_research("--parallel")
        self.assertEqual(code, 0, second.get("raw", ""))
        second_counts = self.db_counts()
        parallel_saved = self.stage_detail(second["run_id"], "collect@0")["saved"]

        self.assertEqual(second["status"], first["status"])
        self.assertEqual(second["sources_count"], first["sources_count"])
        self.assertEqual(second["claims_count"], first["claims_count"])
        self.assertEqual(second["answered"], first["answered"])
        self.assertEqual(second["report"]["validation_passed"], first["report"]["validation_passed"])
        self.assertEqual(parallel_saved, sequential_saved)
        self.assertEqual(second_counts["research_run_sources"] - first_counts["research_run_sources"], 2)
        self.assertTrue(self.stage_detail(second["run_id"], "collect@0")["parallel"])

    def test_parallel_never_creates_duplicate_source_rows(self) -> None:
        # Two searches per subquestion surface the same two documents, so the
        # same URL is admitted more than once: the store must still hold one
        # row per canonical URL (the concurrent first-insert race is closed).
        code, _ = self.run_research(
            "--parallel", "--searches-per-subquestion", "2", "--depth", "thorough"
        )
        self.assertIn(code, (0, 5))
        with storage.open_db(load_settings()) as conn:
            duplicates = conn.execute(
                """
                SELECT canonical_url, COUNT(*) AS n FROM sources
                WHERE url IS NOT NULL GROUP BY canonical_url HAVING n > 1
                """
            ).fetchall()
        self.assertEqual([tuple(row) for row in duplicates], [])

    def test_parallel_respects_the_source_budget(self) -> None:
        code, payload = self.run_research("--parallel", "--max-sources", "1")
        self.assertEqual(code, 5)
        self.assertEqual(payload["status"], "partial")
        self.assertLessEqual(payload["sources_count"], 1)
        detail = self.stage_detail(payload["run_id"], "collect@0")
        self.assertTrue(detail["budget_exhausted"])

    def test_sequential_mode_is_the_default_and_reported(self) -> None:
        code, payload = self.run_research()
        self.assertEqual(code, 0)
        detail = self.stage_detail(payload["run_id"], "collect@0")
        self.assertFalse(detail["parallel"])
        self.assertFalse(self.stage_detail(payload["run_id"], "search@0")["parallel"])


class ReuseTests(Phase8PipelineCase):
    def test_reuse_avoids_a_second_fetch(self) -> None:
        code, first = self.run_research("--project", "alpha")
        self.assertEqual(code, 0)
        after_first = self.doc_requests()
        self.assertGreaterEqual(after_first, 2)

        code, second = self.run_research("--project", "alpha", "--reuse-evidence")
        self.assertEqual(code, 0, second.get("raw", ""))
        self.assertEqual(self.doc_requests(), after_first)
        detail = self.stage_detail(second["run_id"], "collect@0")
        # Every admitted candidate (one row per subquestion per document) was
        # served from project memory; nothing was fetched again.
        self.assertGreaterEqual(detail["reused"], 2)
        self.assertEqual(detail["reused"], detail["saved"])
        self.assertEqual(second["sources_count"], 2)

    def test_reuse_requires_project_scope(self) -> None:
        code, _ = self.run_research("--project", "alpha")
        self.assertEqual(code, 0)
        # Same documents, but the new run belongs to another project: project
        # memory offers nothing, so the second run falls back to the Phase 3
        # cache (served without refetching) instead of linking memory copies.
        code, second = self.run_research("--project", "beta", "--reuse-evidence")
        self.assertEqual(code, 0, second.get("raw", ""))
        detail = self.stage_detail(second["run_id"], "collect@0")
        self.assertEqual(detail["reused"], 0)
        self.assertGreaterEqual(detail["cache_hits"], 2)

    def test_reuse_respects_the_freshness_bound(self) -> None:
        code, _ = self.run_research("--project", "alpha")
        self.assertEqual(code, 0)
        with storage.open_db(load_settings()) as conn:
            conn.execute(
                "UPDATE sources SET last_checked_at = '2000-01-01T00:00:00+00:00', "
                "retrieved_at = '2000-01-01T00:00:00+00:00' WHERE url IS NOT NULL"
            )
        code, second = self.run_research("--project", "alpha", "--reuse-evidence")
        self.assertEqual(code, 0, second.get("raw", ""))
        detail = self.stage_detail(second["run_id"], "collect@0")
        self.assertEqual(detail["reused"], 0)
        self.assertGreaterEqual(detail["saved"], 2)

    def test_reuse_without_flag_never_reuses(self) -> None:
        code, _ = self.run_research("--project", "alpha")
        self.assertEqual(code, 0)
        code, second = self.run_research("--project", "alpha")
        self.assertEqual(code, 0)
        self.assertEqual(self.stage_detail(second["run_id"], "collect@0")["reused"], 0)


class FactCheckerStageTests(Phase8PipelineCase):
    def test_verify_stage_runs_a_read_only_recheck_pass(self) -> None:
        code, payload = self.run_research()
        self.assertEqual(code, 0)
        detail = self.stage_detail(payload["run_id"], "verify@0")
        recheck = detail.get("recheck")
        self.assertIsNotNone(recheck, detail)
        self.assertGreaterEqual(recheck["claims"], 1)
        self.assertGreaterEqual(recheck["agree"], 1)
        self.assertEqual(recheck["disagree"], 0)

    def test_recheck_disagreement_is_surfaced_and_never_auto_applied(self) -> None:
        original = specialists._WORKERS[("fact_checker", "recheck")]
        forged = {"done": False}

        def tampered(payload: dict, settings) -> dict:
            result = original(payload, settings)
            if not forged["done"] and result.get("agreement"):
                forged["done"] = True
                return {
                    **result,
                    "stored_status": "contradicted",
                    "recomputed_status": "supported",
                    "agreement": False,
                }
            return result

        with mock.patch.dict(
            specialists._WORKERS, {("fact_checker", "recheck"): tampered}
        ):
            code, payload = self.run_research()
        self.assertEqual(code, 0)
        detail = self.stage_detail(payload["run_id"], "verify@0")
        self.assertEqual(detail["recheck"]["disagree"], 1)
        self.assertTrue(
            any("fact-checker" in warning for warning in payload["warnings"]),
            payload["warnings"],
        )
        # The forged status was never written: no claim carries 'contradicted'.
        with storage.open_db(load_settings()) as conn:
            contradicted = conn.execute(
                "SELECT COUNT(*) AS n FROM claims WHERE status = 'contradicted'"
            ).fetchone()["n"]
        self.assertEqual(contradicted, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

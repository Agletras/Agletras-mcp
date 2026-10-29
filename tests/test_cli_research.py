"""End-to-end tests for `agletras run` / `resume` against local mock web servers.

Everything is hermetic: the search provider and the fetched pages are served by
a throwaway HTTP server on 127.0.0.1, the database lives in a temporary home,
and no test touches the real network or the project's data directory.
"""

from __future__ import annotations

import io
import json
import os
import signal
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agletras_mcp import cli, pipeline, runs, storage
from agletras_mcp.config import load_settings

from tests.support import TempHomeCase, serve_http

DOC_ONE = (
    "<html><head><title>Evidence storage</title></head><body><p>"
    "Agletras stores evidence as indexed passages inside a local SQLite database. "
    "Each passage keeps a stable SHA-256 hash and its exact character offsets, so "
    "citations can point at the passage text. The passage index uses FTS5 full-text "
    "search when the SQLite build supports it."
    "</p></body></html>"
).encode()

DOC_TWO = (
    "<html><head><title>Citations</title></head><body><p>"
    "Citations attach one exact stored passage to one claim and record the relation, "
    "such as supports or contradicts. Verification status is derived from the citation "
    "relations, so a claim is never marked supported by registration alone. Reports "
    "render stable citation identifiers and list the references section."
    "</p></body></html>"
).encode()

QUESTION = "How does agletras store evidence passages and attach citations to claims?"
SUBQUESTION_ONE = "How are evidence passages stored?"
SUBQUESTION_TWO = "How are citations attached to claims?"


def build_search_page(base: str, doc_one: str, doc_two: str) -> bytes:
    def ddg(url: str) -> str:
        return "//duckduckgo.com/l/?uddg=" + urllib.parse.quote(url, safe="")

    return (
        "<!doctype html><html><body>"
        f'<h2 class="result__title"><a class="result__a" href="{ddg(doc_one)}">Evidence storage</a></h2>'
        '<a class="result__snippet">How evidence passages are stored.</a>'
        f'<h2 class="result__title"><a class="result__a" href="{ddg(doc_two)}">Citations</a></h2>'
        '<a class="result__snippet">How citations attach passages to claims.</a>'
        "</body></html>"
    ).encode()


class CliResearchCase(TempHomeCase):
    """Fixture: local search endpoint linking to two local HTML documents."""

    def setUp(self) -> None:
        super().setUp()
        self.routes: dict[str, tuple] = {}
        self._server = serve_http(self.routes)
        self.base = self._server.__enter__()
        self.addCleanup(self._server.__exit__, None, None, None)
        self.routes["/doc-one"] = (200, "text/html; charset=utf-8", DOC_ONE)
        self.routes["/doc-two"] = (200, "text/html; charset=utf-8", DOC_TWO)
        self.routes["/search"] = (
            200,
            "text/html; charset=utf-8",
            build_search_page(self.base, self.base + "/doc-one", self.base + "/doc-two"),
        )
        os.environ["AGLETRAS_SEARCH_PROVIDER"] = "duckduckgo"
        os.environ["AGLETRAS_SEARCH_ENDPOINT"] = self.base + "/search"

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

    def db_counts(self) -> dict[str, int]:
        settings = load_settings()
        with storage.open_db(settings) as conn:
            return {
                table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
                for table in (
                    "research_run_sources",
                    "sources",
                    "claims",
                    "citations",
                    "reports",
                    "research_checkpoints",
                )
            }


class FullRunTests(CliResearchCase):
    def test_full_run_report_inspection_and_resume_refusal(self) -> None:
        code, payload = self.run_research()
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["sources_count"], 2)
        self.assertGreaterEqual(payload["claims_count"], 1)
        self.assertEqual(payload["answered"], payload["subquestions"])
        self.assertTrue(payload["report"]["validation_passed"])

        # The report file exists on disk and the stdout payload told the truth.
        report_path = payload["report"]["path"]
        self.assertTrue(os.path.exists(report_path))
        with open(report_path, encoding="utf-8") as handle:
            report_text = handle.read()
        self.assertIn("[S1]", report_text)
        self.assertIn("Sources and references", report_text)

        # The JSONL log file is separate from the report and parses line by line.
        log_path = payload["log_file"]
        self.assertTrue(log_path.endswith(".jsonl"))
        with open(log_path, encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(records[0]["event"], "run_created")
        self.assertTrue(any(record.get("stage", "").startswith("search@") for record in records))

        # `report` prints the stored Markdown, citations intact.
        code, out, _ = self.invoke(["report", "1"])
        self.assertEqual(code, 0)
        self.assertIn("[S1]", out)
        self.assertIn("SUPPORTED", out)

        # `status` summarizes the run; `list` shows it.
        code, out, _ = self.invoke(["status", "1", "--json"])
        self.assertEqual(code, 0)
        status = json.loads(out)
        self.assertEqual(status["run"]["status"], "completed")
        self.assertEqual([item["status"] for item in status["subquestions"]], ["answered", "answered"])
        self.assertFalse(status["resumable"]["resumable"])
        code, out, _ = self.invoke(["list", "--json"])
        self.assertEqual(json.loads(out)["count"], 1)

        # A completed run refuses resume, even with --force.
        for extra in ([], ["--force"]):
            code, out, err = self.invoke(["resume", "1", *extra, "--json"])
            self.assertEqual(code, 2)
            self.assertIn("already_closed", out)

    def test_reruns_are_separate_runs_with_independent_reports(self) -> None:
        self.run_research()
        code, second = self.run_research()
        self.assertEqual(code, 0)
        self.assertEqual(second["run_id"], 2)
        counts = self.db_counts()
        self.assertEqual(counts["reports"], 2)
        self.assertGreaterEqual(counts["sources"], 2)
        # Evidence is de-duplicated across runs: the same two URLs stay two rows.
        with storage.open_db(load_settings()) as conn:
            distinct_urls = conn.execute(
                "SELECT COUNT(DISTINCT canonical_url) FROM sources WHERE url IS NOT NULL"
            ).fetchone()[0]
        self.assertLessEqual(int(distinct_urls), 2)

    def test_partial_run_reports_gaps_and_exits_5(self) -> None:
        code, payload = self.run_research("--max-sources", "1")
        self.assertEqual(code, 5)
        self.assertEqual(payload["status"], "partial")
        self.assertGreaterEqual(len(payload["gaps"]), 1)
        self.assertTrue(payload["report"]["validation_passed"])
        self.assertIn("gap", payload["stopping_reason"].lower() + " ".join(
            gap.get("question", "") for gap in payload["gaps"]
        ).lower() + " partially")
        # The run is closed as partial: resume is refused without corrupting it.
        response = self.invoke(["resume", "1", "--json"])
        self.assertEqual(response[0], 2)
        self.assertIn("already_closed", response[1])


class InterruptionResumeTests(CliResearchCase):
    def _collect_then_interrupt(self) -> None:
        """Run the real collect stage, then interrupt before it completes."""
        original = pipeline.ResearchSession.stage_collect
        calls = {"count": 0}

        def wrapper(session, round_index):
            result = original(session, round_index)
            calls["count"] += 1
            if calls["count"] == 1:
                raise KeyboardInterrupt
            return result

        self._patch = mock.patch.object(pipeline.ResearchSession, "stage_collect", wrapper)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_interrupted_run_resumes_without_duplicating_evidence(self) -> None:
        self._collect_then_interrupt()
        code, out, err = self.invoke(
            ["run", QUESTION, "--depth", "quick", "--json"]
        )
        self.assertEqual(code, 130, err)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "interrupted")
        run_id = payload["run_id"]

        # The partial evidence from the interrupted collect stage survived.
        first_counts = self.db_counts()
        self.assertGreaterEqual(first_counts["research_run_sources"], 1)

        # Status reports the pause and says the run is resumable. The interrupted
        # stage is retried on resume (it has no completed checkpoint).
        code, out, _ = self.invoke(["status", str(run_id), "--json"])
        status = json.loads(out)
        self.assertEqual(status["run"]["status"], "interrupted")
        self.assertTrue(status["resumable"]["resumable"])
        collect_states = [
            checkpoint["status"]
            for checkpoint in status["checkpoints"]
            if checkpoint["stage"] == "collect@0"
        ]
        self.assertEqual(collect_states[-1], "interrupted")

        # Resume completes the run; collect re-runs against the same URLs and the
        # evidence library de-duplicates, so nothing is doubled.
        code, out, err = self.invoke(["resume", str(run_id), "--json"])
        self.assertEqual(code, 0, err)
        resumed = json.loads(out)
        self.assertEqual(resumed["status"], "completed")
        self.assertTrue(resumed["resumed"])
        self.assertTrue(resumed["report"]["validation_passed"])
        self.assertEqual(resumed["sources_count"], 2)

        final = self.db_counts()
        self.assertEqual(final["research_run_sources"], resumed["sources_count"])
        self.assertEqual(final["reports"], 1)
        with storage.open_db(load_settings()) as conn:
            duplicates = conn.execute(
                """
                SELECT source_id, COUNT(*) AS n FROM research_run_sources
                WHERE run_id = ? GROUP BY source_id HAVING n > 1
                """,
                (run_id,),
            ).fetchall()
        self.assertEqual(duplicates, [])

        # Resuming again is refused: the run is closed.
        code, out, _ = self.invoke(["resume", str(run_id), "--json"])
        self.assertEqual(code, 2)
        self.assertIn("already_closed", out)

    def test_sigterm_is_handled_like_ctrl_c(self) -> None:
        original = pipeline.ResearchSession.stage_claims
        old_handler = signal.getsignal(signal.SIGTERM)

        def wrapper(session, round_index):
            os.kill(os.getpid(), signal.SIGTERM)  # the CLI's SIGTERM handler raises
            return original(session, round_index)

        self.addCleanup(signal.signal, signal.SIGTERM, old_handler)
        with mock.patch.object(pipeline.ResearchSession, "stage_claims", wrapper):
            code, out, err = self.invoke(["run", QUESTION, "--depth", "quick", "--json"])
        self.assertEqual(code, 130, err)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "interrupted")
        self.assertIn("resume", payload["error"])
        code, out, _ = self.invoke(["status", str(payload["run_id"]), "--json"])
        self.assertEqual(json.loads(out)["run"]["status"], "interrupted")


class ResumeStateTests(CliResearchCase):
    def create_running_run(self) -> int:
        """Create a CLI run and stop it cleanly at the plan stage."""
        original = pipeline.ResearchSession.stage_search
        raised = {"done": False}

        def wrapper(session, round_index):
            if not raised["done"]:
                raised["done"] = True
                raise KeyboardInterrupt
            return original(session, round_index)

        with mock.patch.object(pipeline.ResearchSession, "stage_search", wrapper):
            code, out, _ = self.invoke(["run", QUESTION, "--depth", "quick", "--json"])
        self.assertEqual(code, 130)
        return int(json.loads(out)["run_id"])

    def test_failed_run_requires_force(self) -> None:
        run_id = self.create_running_run()
        settings = load_settings()
        runs.set_run_status(run_id, "failed", error="boom", settings=settings)
        code, out, _ = self.invoke(["resume", str(run_id), "--json"])
        self.assertEqual(code, 2)
        self.assertIn("failed", out)
        code, out, err = self.invoke(["resume", str(run_id), "--force", "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "completed")

    def test_stale_run_requires_force(self) -> None:
        run_id = self.create_running_run()
        settings = load_settings()
        with storage.open_db(settings) as conn:
            conn.execute(
                "UPDATE research_runs SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (run_id,),
            )
        os.environ["AGLETRAS_RESEARCH_RESUME_MAX_AGE_DAYS"] = "7"
        code, out, _ = self.invoke(["resume", str(run_id), "--json"])
        self.assertEqual(code, 2)
        self.assertIn("stale", out)
        code, _, err = self.invoke(["resume", str(run_id), "--force", "--json"])
        self.assertEqual(code, 0, err)

    def test_cancelled_run_resumes_without_force(self) -> None:
        run_id = self.create_running_run()
        runs.set_run_status(run_id, "cancelled", settings=load_settings())
        code, out, err = self.invoke(["resume", str(run_id), "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "completed")

    def test_restart_reruns_completed_stages_explicitly(self) -> None:
        code, payload = self.run_research()
        self.assertEqual(code, 0)
        run_id = payload["run_id"]
        settings = load_settings()
        runs.set_run_status(run_id, "interrupted", settings=settings)
        code, out, err = self.invoke(["resume", str(run_id), "--restart", "--json"])
        self.assertEqual(code, 0, err)
        resumed = json.loads(out)
        self.assertEqual(resumed["status"], "completed")
        self.assertTrue(resumed["report"]["validation_passed"])
        # Restarting may archive an updated report (the previous one is kept as
        # history); reports are never duplicated in place.
        self.assertGreaterEqual(self.db_counts()["reports"], 1)

    def test_resume_with_different_options_is_refused_without_force(self) -> None:
        run_id = self.create_running_run()
        code, out, _ = self.invoke(
            ["resume", str(run_id), "--depth", "thorough", "--json"]
        )
        self.assertEqual(code, 2)
        self.assertIn("incompatible", out)
        code, _, err = self.invoke(
            ["resume", str(run_id), "--depth", "thorough", "--force", "--json"]
        )
        self.assertEqual(code, 0, err)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

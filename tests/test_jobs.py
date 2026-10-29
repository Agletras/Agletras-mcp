"""Tests for Phase 8 named repeatable jobs (``jobs.py`` and ``agletras jobs``)."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agletras_mcp import cli, evidence, jobs
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import TempHomeCase


class JobsModuleTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def test_add_get_list_round_trip(self) -> None:
        created = jobs.add_job(
            "weekly-mcp",
            "What changed in MCP this week?",
            profile="quick",
            project="demo",
            title="Weekly MCP",
            notes="first pass",
            settings=self.settings,
        )
        self.assertEqual(created["name"], "weekly-mcp")
        self.assertEqual(created["run_count"], 0)
        self.assertIsNone(created["last_run_id"])
        self.assertEqual(created["runs"], [])

        fetched = jobs.get_job("weekly-mcp", settings=self.settings)
        self.assertEqual(fetched, created)
        by_id = jobs.get_job(str(created["job_id"]), settings=self.settings)
        self.assertEqual(by_id["job_id"], created["job_id"])
        # Case-insensitive lookup.
        self.assertEqual(
            jobs.get_job("WEEKLY-MCP", settings=self.settings)["job_id"], created["job_id"]
        )

        listing = jobs.list_jobs(settings=self.settings)
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["jobs"][0]["question"], "What changed in MCP this week?")

    def test_duplicate_names_conflict_case_insensitively(self) -> None:
        jobs.add_job("Weekly", "q one", settings=self.settings)
        with self.assertRaises(ToolError) as caught:
            jobs.add_job("WEEKLY", "q two", settings=self.settings)
        self.assertEqual(caught.exception.code, "conflict")
        self.assertIn("already exists", caught.exception.message)

    def test_blank_and_oversized_values_are_rejected(self) -> None:
        with self.assertRaises(ToolError) as caught:
            jobs.add_job("   ", "q", settings=self.settings)
        self.assertEqual(caught.exception.code, "invalid_argument")
        with self.assertRaises(ToolError):
            jobs.add_job("n", "   ", settings=self.settings)
        with self.assertRaises(ToolError) as caught:
            jobs.add_job("x" * 200, "q", settings=self.settings)
        self.assertIn("limit", caught.exception.message)

    def test_unknown_lookups(self) -> None:
        self.assertIsNone(jobs.get_job("ghost", settings=self.settings))
        with self.assertRaises(ToolError) as caught:
            jobs.remove_job("ghost", settings=self.settings)
        self.assertEqual(caught.exception.code, "not_found")

    def test_record_run_updates_counters_and_history(self) -> None:
        job = jobs.add_job("repeat", "Repeat me", settings=self.settings)
        first = int(evidence.start_run("Repeat me", settings=self.settings)["run_id"])
        second = int(evidence.start_run("Repeat me", settings=self.settings)["run_id"])
        updated = jobs.record_run(job["job_id"], first, settings=self.settings)
        self.assertEqual(updated["run_count"], 1)
        self.assertEqual(updated["last_run_id"], first)
        updated = jobs.record_run(job["job_id"], second, settings=self.settings)
        self.assertEqual(updated["run_count"], 2)
        self.assertEqual(updated["last_run_id"], second)
        # Newest first in the history.
        self.assertEqual([item["run_id"] for item in updated["runs"]], [second, first])
        self.assertEqual(updated["runs"][0]["query"], "Repeat me")

    def test_record_run_rejects_unknown_job_and_run(self) -> None:
        job = jobs.add_job("fk", "q", settings=self.settings)
        with self.assertRaises(ToolError) as caught:
            jobs.record_run(job["job_id"], 999999, settings=self.settings)
        self.assertEqual(caught.exception.code, "conflict")
        run_id = int(evidence.start_run("q", settings=self.settings)["run_id"])
        with self.assertRaises(ToolError) as caught:
            jobs.record_run(999999, run_id, settings=self.settings)
        self.assertEqual(caught.exception.code, "not_found")

    def test_removing_a_job_keeps_its_research_runs(self) -> None:
        job = jobs.add_job("gone", "q", settings=self.settings)
        run_id = int(evidence.start_run("q", settings=self.settings)["run_id"])
        jobs.record_run(job["job_id"], run_id, settings=self.settings)
        payload = jobs.remove_job("gone", settings=self.settings)
        self.assertTrue(payload["removed"])
        self.assertEqual(payload["job"]["run_count"], 1)
        self.assertIsNone(jobs.get_job("gone", settings=self.settings))
        self.assertIsNotNone(evidence.get_run(run_id, settings=self.settings))

    def test_values_are_whitespace_normalized(self) -> None:
        created = jobs.add_job(
            "  spaced   name ",
            "  question   with\nnewline ",
            project="  proj  ",
            settings=self.settings,
        )
        self.assertEqual(created["name"], "spaced name")
        self.assertEqual(created["question"], "question with newline")
        self.assertEqual(created["project"], "proj")

    def test_schema_version_7_is_recorded(self) -> None:
        from agletras_mcp import storage

        with storage.open_db(self.settings) as conn:
            versions = [
                int(row[0])
                for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
            ]
        self.assertEqual(versions, list(range(1, storage.SCHEMA_VERSION + 1)))
        self.assertEqual(storage.SCHEMA_VERSION, 7)


class JobsCliTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def invoke(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.run(argv)
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def write_config(self, text: str) -> None:
        (self.home / "agletras.toml").write_text(text, encoding="utf-8")

    def test_jobs_commands_end_to_end(self) -> None:
        self.write_config('[profiles.quick]\ndepth = "quick"\n\n[branding]\nname = "Acme"\n')
        code, out, err = self.invoke(
            ["jobs", "add", "weekly", "What changed?", "--profile", "quick", "--project", "demo"]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("job 1 (weekly) added", out)

        code, out, _ = self.invoke(["jobs"])
        self.assertEqual(code, 0)
        self.assertIn("weekly: What changed?", out)
        self.assertIn("profile=quick", out)

        code, out, _ = self.invoke(["jobs", "show", "weekly", "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["profile"], "quick")
        self.assertEqual(payload["project"], "demo")

        code, out, _ = self.invoke(["jobs", "remove", "weekly"])
        self.assertEqual(code, 0)
        self.assertIn("removed", out)
        code, out, _ = self.invoke(["jobs", "--json"])
        self.assertEqual(json.loads(out)["count"], 0)

    def test_jobs_add_rejects_unknown_profile(self) -> None:
        code, _, err = self.invoke(["jobs", "add", "x", "q", "--profile", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("unknown profile", err)

    def test_jobs_duplicate_and_unknown_names(self) -> None:
        self.assertEqual(self.invoke(["jobs", "add", "dup", "q"])[0], 0)
        code, _, err = self.invoke(["jobs", "add", "DUP", "q2"])
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        code, _, err = self.invoke(["jobs", "show", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("no job named", err)
        code, _, err = self.invoke(["jobs", "run", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("no job named", err)
        code, _, err = self.invoke(["jobs", "remove", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("not_found", err)

    def test_jobs_run_uses_profile_branding_and_records_history(self) -> None:
        self.write_config(
            """
[branding]
name = "Acme"

[profiles.quick]
depth = "quick"
max_sources = 4
"""
        )
        self.invoke(["jobs", "add", "weekly", "What changed?", "--profile", "quick"])
        captured: dict = {}

        def fake_create_run(options, settings, reporter, **kwargs):
            captured["options"] = options
            run_id = int(evidence.start_run(options.question, settings=settings)["run_id"])
            captured["run_id"] = run_id
            return run_id

        def fake_execute_research(run_id, options, settings=None, reporter=None, **kwargs):
            return {"run_id": run_id, "status": "completed", "exit_code": 0}

        with mock.patch.object(cli, "create_run", fake_create_run), mock.patch.object(
            cli, "execute_research", fake_execute_research
        ):
            code, out, err = self.invoke(["jobs", "run", "weekly"])
        self.assertEqual(code, 0, err)
        options = captured["options"]
        self.assertEqual(options.depth, "quick")
        self.assertEqual(options.max_sources, 4)
        self.assertEqual(options.title, "Acme: What changed?")
        job = jobs.get_job("weekly", settings=self.settings)
        self.assertEqual(job["last_run_id"], captured["run_id"])
        self.assertEqual(job["run_count"], 1)
        self.assertEqual(job["runs"][0]["run_id"], captured["run_id"])
        self.assertEqual(job["runs"][0]["status"], "running")

    def test_jobs_run_fails_loudly_when_the_profile_disappeared(self) -> None:
        self.write_config('[profiles.quick]\ndepth = "quick"\n')
        self.invoke(["jobs", "add", "weekly", "Q?", "--profile", "quick"])
        # The profile is removed from the config file afterwards.
        self.write_config("[research]\nmax_sources = 3\n")
        code, _, err = self.invoke(["jobs", "run", "weekly"])
        self.assertEqual(code, 2)
        self.assertIn("references profile 'quick'", err)
        self.assertIn("not defined", err)
        # No run was attempted.
        self.assertIsNone(jobs.get_job("weekly", settings=self.settings)["last_run_id"])

    def test_jobs_help_and_usage(self) -> None:
        code, out, _ = self.invoke(["jobs", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)
        for action in ("list", "add", "show", "run", "remove"):
            code, out, _ = self.invoke(["jobs", action, "--help"])
            self.assertEqual(code, 0, action)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Tests for the Phase 7 run sessions: checkpoints, statuses, resume state."""

from __future__ import annotations

import unittest

from agletras_mcp import evidence, runs, storage
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import TempHomeCase


class RunsCase(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def start_run(self, query: str = "How do widgets work?", *, cli: bool = True) -> int:
        config = {"source": "cli", "options": {"depth": "quick"}} if cli else {"source": "agent"}
        run = evidence.start_run(query=query, config=config, settings=self.settings)
        return int(run["run_id"])


class CheckpointTests(RunsCase):
    def test_checkpoint_lifecycle_and_latest_state(self) -> None:
        run_id = self.start_run()
        started = runs.start_checkpoint(run_id, "plan", settings=self.settings)
        self.assertEqual(started["status"], "started")
        self.assertEqual(started["attempt"], 1)
        finished = runs.finish_checkpoint(
            run_id,
            "plan",
            status="complete",
            attempt=started["attempt"],
            detail={"subquestion_count": 2},
            settings=self.settings,
        )
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["attempt"], 1)
        self.assertIsNotNone(finished["finished_at"])

        # A retried stage gets a higher attempt number and becomes the latest state.
        retry = runs.start_checkpoint(run_id, "plan", settings=self.settings)
        self.assertEqual(retry["attempt"], 2)
        state = runs.stage_states(run_id, settings=self.settings)
        self.assertEqual(state["plan"]["status"], "started")
        self.assertEqual(state["plan"]["attempt"], 2)
        self.assertEqual(runs.stage_status(run_id, "plan", settings=self.settings), "started")
        self.assertIsNone(runs.stage_status(run_id, "search@0", settings=self.settings))

        history = runs.checkpoints(run_id, settings=self.settings)
        self.assertEqual([row["status"] for row in history], ["started", "complete", "started"])
        self.assertEqual([row["attempt"] for row in history], [1, 1, 2])

    def test_invalid_status_and_stage_are_rejected(self) -> None:
        run_id = self.start_run()
        with self.assertRaises(ToolError):
            runs.record_checkpoint(run_id, "plan", "broken", settings=self.settings)
        with self.assertRaises(ToolError):
            runs.record_checkpoint(run_id, "  ", "complete", settings=self.settings)
        with self.assertRaises(ToolError):
            runs.record_checkpoint(999, "plan", "complete", settings=self.settings)

    def test_checkpoint_writes_are_atomic_per_row(self) -> None:
        run_id = self.start_run()
        runs.record_checkpoint(
            run_id, "collect@0", "complete", detail={"saved": 3}, settings=self.settings
        )
        fetched = runs.checkpoints(run_id, settings=self.settings)[0]
        self.assertEqual(fetched["detail"], {"saved": 3})
        self.assertEqual(fetched["run_id"], run_id)


class StatusTests(RunsCase):
    def test_terminal_status_sets_completed_at_and_reopen_clears_it(self) -> None:
        run_id = self.start_run()
        payload = runs.set_run_status(run_id, "completed", stage="done", settings=self.settings)
        self.assertEqual(payload["status"], "completed")
        self.assertIsNotNone(payload["completed_at"])
        reopened = runs.set_run_status(run_id, "running", stage="resumed", settings=self.settings)
        self.assertIsNone(reopened["completed_at"])
        self.assertEqual(reopened["stage"], "resumed")

    def test_all_documented_statuses_are_accepted_and_others_rejected(self) -> None:
        run_id = self.start_run()
        for status in runs.RUN_STATUSES:
            runs.set_run_status(run_id, status, settings=self.settings)
        with self.assertRaises(ToolError) as caught:
            runs.set_run_status(run_id, "finished", settings=self.settings)
        self.assertIn("status", caught.exception.message)

    def test_error_and_stage_are_persisted(self) -> None:
        run_id = self.start_run()
        runs.set_run_status(
            run_id, "failed", error="boom", stage="failed", settings=self.settings
        )
        state = runs.get_run_state(run_id, settings=self.settings)
        self.assertEqual(state["error"], "boom")
        self.assertEqual(state["stage"], "failed")
        self.assertIsNotNone(state["updated_at"])

    def test_get_run_state_unknown_returns_none(self) -> None:
        self.assertIsNone(runs.get_run_state(12345, settings=self.settings))


class ResumeAssessmentTests(RunsCase):
    def test_running_cli_run_is_resumable(self) -> None:
        run_id = self.start_run()
        runs.start_checkpoint(run_id, "search@0", settings=self.settings)
        assessment = runs.resume_assessment(run_id, settings=self.settings)
        self.assertTrue(assessment["resumable"])
        self.assertEqual(assessment["reason"], "ok")
        self.assertEqual(assessment["paused_stage"], "search@0")

    def test_unknown_run_is_not_resumable(self) -> None:
        assessment = runs.resume_assessment(999, settings=self.settings)
        self.assertFalse(assessment["resumable"])
        self.assertEqual(assessment["reason"], "not_found")

    def test_mcp_agent_run_without_checkpoints_is_not_resumable(self) -> None:
        run_id = self.start_run(cli=False)
        assessment = runs.resume_assessment(run_id, settings=self.settings)
        self.assertFalse(assessment["resumable"])
        self.assertEqual(assessment["reason"], "not_cli_managed")

    def test_completed_and_partial_runs_refuse_even_with_force(self) -> None:
        force_run = self.start_run()
        runs.set_run_status(force_run, "completed", settings=self.settings)
        for force in (False, True):
            assessment = runs.resume_assessment(force_run, settings=self.settings, force=force)
            self.assertFalse(assessment["resumable"])
            self.assertEqual(assessment["reason"], "already_closed")
        partial_run = self.start_run()
        runs.set_run_status(partial_run, "partial", settings=self.settings)
        self.assertFalse(
            runs.resume_assessment(partial_run, settings=self.settings, force=True)["resumable"]
        )

    def test_failed_run_requires_force(self) -> None:
        run_id = self.start_run()
        runs.set_run_status(run_id, "failed", error="boom", settings=self.settings)
        self.assertEqual(
            runs.resume_assessment(run_id, settings=self.settings)["reason"], "failed"
        )
        self.assertTrue(
            runs.resume_assessment(run_id, settings=self.settings, force=True)["resumable"]
        )

    def test_cancelled_and_interrupted_runs_resume_without_force(self) -> None:
        for status in ("cancelled", "interrupted"):
            run_id = self.start_run()
            runs.set_run_status(run_id, status, settings=self.settings)
            assessment = runs.resume_assessment(run_id, settings=self.settings)
            self.assertTrue(assessment["resumable"], status)

    def test_stale_run_requires_force(self) -> None:
        run_id = self.start_run()
        with storage.open_db(self.settings) as conn:
            conn.execute(
                "UPDATE research_runs SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (run_id,),
            )
        assessment = runs.resume_assessment(
            run_id, settings=self.settings, max_age_days=7
        )
        self.assertFalse(assessment["resumable"])
        self.assertEqual(assessment["reason"], "stale")
        forced = runs.resume_assessment(run_id, settings=self.settings, max_age_days=7, force=True)
        self.assertTrue(forced["resumable"])

    def test_zero_max_age_disables_the_staleness_check(self) -> None:
        run_id = self.start_run()
        with storage.open_db(self.settings) as conn:
            conn.execute(
                "UPDATE research_runs SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (run_id,),
            )
        self.assertTrue(
            runs.resume_assessment(run_id, settings=self.settings, max_age_days=0)["resumable"]
        )


class ListRunsTests(RunsCase):
    def test_list_returns_newest_first_with_counts(self) -> None:
        first = self.start_run("first question")
        second = self.start_run("second question")
        payload = runs.list_runs(settings=self.settings)
        self.assertEqual([row["run_id"] for row in payload["runs"]], [second, first])
        self.assertEqual(payload["runs"][0]["query"], "second question")

    def test_status_filter_and_invalid_status(self) -> None:
        run_id = self.start_run("filtered")
        self.start_run("kept running")
        runs.set_run_status(run_id, "completed", settings=self.settings)
        payload = runs.list_runs(status="completed", settings=self.settings)
        self.assertEqual([row["run_id"] for row in payload["runs"]], [run_id])
        with self.assertRaises(ToolError):
            runs.list_runs(status="bogus", settings=self.settings)

    def test_limit_is_bounded(self) -> None:
        for index in range(3):
            self.start_run(f"question {index}")
        self.assertEqual(runs.list_runs(limit=2, settings=self.settings)["count"], 2)


class RunConfigTests(RunsCase):
    def test_build_run_config_redacts_secret_looking_keys(self) -> None:
        config = runs.build_run_config(
            {"depth": "quick", "api_key": "super-secret", "nested": {"token": "abc"}},
            self.settings,
            tool_version="9.9.9",
        )
        self.assertEqual(config["source"], "cli")
        self.assertEqual(config["tool_version"], "9.9.9")
        self.assertEqual(config["options"]["api_key"], "***redacted***")
        self.assertEqual(config["options"]["nested"]["token"], "***redacted***")
        self.assertEqual(config["settings"]["verification_api_key_env"], "***redacted***")

    def test_config_differences_reports_option_and_setting_changes(self) -> None:
        stored = runs.build_run_config(
            {"depth": "quick", "max_sources": 4, "subquestions": ["a"]},
            self.settings,
            tool_version="1",
        )
        same = runs.config_differences(
            stored, {"depth": "quick", "max_sources": 4, "subquestions": ["a"]}, self.settings
        )
        self.assertEqual(same, [])
        changed = runs.config_differences(
            stored,
            {"depth": "thorough", "max_sources": 4, "subquestions": ["a"]},
            self.settings,
        )
        self.assertTrue(any("depth" in item for item in changed))
        stored_settings = stored["settings"]
        stored_settings["search_provider"] = "wikipedia"
        changed_settings = runs.config_differences(
            stored, {"depth": "quick", "max_sources": 4, "subquestions": ["a"]}, self.settings
        )
        self.assertTrue(any("search_provider" in item for item in changed_settings))

    def test_run_report_paths_lists_stored_reports(self) -> None:
        run_id = self.start_run()
        self.assertEqual(runs.run_report_paths(run_id, settings=self.settings), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

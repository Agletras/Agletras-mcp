"""Tests for the `agletras` CLI: help, exit codes, config, inspection commands."""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agletras_mcp import __version__, cli, evidence

from tests.support import TempHomeCase


class CliCase(TempHomeCase):
    def invoke(self, argv: list[str]) -> tuple[int, str, str]:
        """Run the CLI in-process, capturing both streams and SystemExit."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.run(argv)
            except SystemExit as exc:  # argparse help/usage/version paths
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def assert_json(self, text: str) -> dict:
        return json.loads(text)


class HelpAndVersionTests(CliCase):
    def test_root_help_exits_zero_and_lists_commands(self) -> None:
        code, out, _ = self.invoke(["--help"])
        self.assertEqual(code, 0)
        for command in ("run", "resume", "status", "list", "report", "config", "doctor", "version"):
            self.assertIn(command, out)
        self.assertIn("exit codes", out)

    def test_every_subcommand_has_help(self) -> None:
        for command in (
            "run",
            "resume",
            "status",
            "list",
            "report",
            "profiles",
            "export",
            "jobs",
            "monitor",
            "config",
            "doctor",
            "version",
            "serve",
        ):
            code, out, _ = self.invoke([command, "--help"])
            self.assertEqual(code, 0, command)
            self.assertIn("usage:", out, command)
        for action in ("show", "validate"):
            code, out, _ = self.invoke(["config", action, "--help"])
            self.assertEqual(code, 0, action)
        for action in ("list", "add", "remove", "check", "changes", "report"):
            code, out, _ = self.invoke(["monitor", action, "--help"])
            self.assertEqual(code, 0, action)
            self.assertIn("usage:", out, action)

    def test_version_command_and_root_flag(self) -> None:
        code, out, _ = self.invoke(["version"])
        self.assertEqual(code, 0)
        self.assertIn(f"agletras-mcp {__version__}", out)
        code, out, _ = self.invoke(["version", "--json"])
        payload = self.assert_json(out)
        self.assertEqual(payload["version"], __version__)
        self.assertIn("schema_version", payload)
        code, out, _ = self.invoke(["--version"])
        self.assertEqual(code, 0)
        self.assertIn(__version__, out)

    def test_no_command_and_unknown_command_are_usage_errors(self) -> None:
        code, _, err = self.invoke([])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)
        code, _, err = self.invoke(["frobnicate"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)


class ConfigCommandTests(CliCase):
    def test_config_show_masks_secret_named_settings(self) -> None:
        code, out, _ = self.invoke(["config", "show", "--json"])
        self.assertEqual(code, 0)
        payload = self.assert_json(out)
        self.assertEqual(payload["settings"]["verification_api_key_env"], "***redacted***")
        self.assertEqual(payload["settings"]["vision_api_key_env"], "***redacted***")
        self.assertIsNone(payload["config_file"])

    def test_config_show_reads_and_reports_a_broken_file(self) -> None:
        path = self.home / "bad.toml"
        path.write_text("[fetch]\ntypo_key = 1\n", encoding="utf-8")
        os.environ["AGLETRAS_CONFIG"] = str(path)
        code, _, err = self.invoke(["config", "show"])
        self.assertEqual(code, 2)
        self.assertIn("typo_key", err)

    def test_config_show_uses_a_valid_file(self) -> None:
        path = self.home / "ok.toml"
        path.write_text('[fetch]\ntimeout = 7.5\n\n[research]\ndepth = "quick"\n', encoding="utf-8")
        code, out, _ = self.invoke(["--config", str(path), "config", "show", "--json"])
        self.assertEqual(code, 0)
        payload = self.assert_json(out)
        self.assertEqual(payload["settings"]["fetch_timeout"], 7.5)
        self.assertEqual(payload["settings"]["research_depth"], "quick")
        self.assertEqual(payload["config_file"], str(path))

    def test_config_validate_detects_out_of_range_values(self) -> None:
        os.environ["AGLETRAS_COLLECT_CONCURRENCY"] = "99"
        code, out, err = self.invoke(["config", "validate", "--json"])
        self.assertEqual(code, 2)
        payload = self.assert_json(out)
        self.assertFalse(payload["ok"])
        self.assertTrue(any("concurrency" in item for item in payload["errors"]))
        os.environ.pop("AGLETRAS_COLLECT_CONCURRENCY")

    def test_config_validate_is_quiet_when_valid(self) -> None:
        code, out, _ = self.invoke(["config", "validate"])
        self.assertEqual(code, 0)
        self.assertIn("configuration valid", out)

    def test_unknown_config_action_is_a_usage_error(self) -> None:
        code, _, err = self.invoke(["config", "explain"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)


class InspectionCommandTests(CliCase):
    def test_list_with_no_runs(self) -> None:
        code, out, _ = self.invoke(["list"])
        self.assertEqual(code, 0)
        self.assertIn("no research runs", out)
        code, out, _ = self.invoke(["list", "--json"])
        payload = self.assert_json(out)
        self.assertEqual(payload["count"], 0)

    def test_config_broken_file_blocks_commands_with_exit_2(self) -> None:
        path = self.home / "broken.toml"
        path.write_text("[nope]\nvalue = 1\n", encoding="utf-8")
        os.environ["AGLETRAS_CONFIG"] = str(path)
        code, _, err = self.invoke(["list", "--config", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("configuration error", err)

    def test_global_flags_work_before_and_after_the_subcommand(self) -> None:
        before = self.invoke(["--json", "list"])
        after = self.invoke(["list", "--json"])
        self.assertEqual(before[0], 0)
        self.assertEqual(after[0], 0)
        self.assertEqual(self.assert_json(before[1]), self.assert_json(after[1]))

    def test_status_report_and_resume_reject_unknown_runs(self) -> None:
        for argv in (["status", "42"], ["report", "42"], ["resume", "42"]):
            code, _, err = self.invoke(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("no research run with id 42", err)

    def test_run_validates_arguments_before_any_network_work(self) -> None:
        code, _, err = self.invoke(["run", ""])
        self.assertEqual(code, 2)
        self.assertIn("must not be empty", err)
        code, _, err = self.invoke(["run", "question", "--depth", "extreme"])
        self.assertEqual(code, 2)
        code, _, err = self.invoke(["run", "question", "--images", "--no-images"])
        self.assertEqual(code, 2)
        self.assertIn("mutually exclusive", err)

    def test_invalid_research_value_is_reported_before_any_network_work(self) -> None:
        # Point the provider at a dead local port: if validation regresses and a
        # fetch is attempted, the test fails fast instead of hitting the internet.
        os.environ["AGLETRAS_SEARCH_ENDPOINT"] = "http://127.0.0.1:9/search"
        os.environ["AGLETRAS_FETCH_TIMEOUT"] = "1"
        code, _, err = self.invoke(
            ["run", "question", "--verification", "telepathy", "--max-sources", "1"]
        )
        self.assertEqual(code, 2)
        self.assertIn("verification method", err.lower())


class DoctorCommandTests(CliCase):
    def test_doctor_skip_mcp_reports_all_required_checks(self) -> None:
        code, out, err = self.invoke(["doctor", "--skip-mcp", "--json"])
        self.assertEqual(code, 0)
        payload = self.assert_json(out)
        names = {check["name"] for check in payload["checks"]}
        self.assertLessEqual(
            {"runtime", "dependencies", "config", "paths", "database", "search_adapter"}, names
        )
        self.assertNotIn("mcp_server", names)
        self.assertEqual(payload["required_failures"], [])
        self.assertEqual(payload["status"], "ok")

    def test_doctor_json_progress_lines_parse_as_json(self) -> None:
        code, _, err = self.invoke(["doctor", "--skip-mcp", "--log-format", "json", "-v"])
        self.assertEqual(code, 0)
        for line in err.splitlines():
            json.loads(line)

    def test_doctor_human_output_has_result_line(self) -> None:
        code, out, _ = self.invoke(["doctor", "--skip-mcp"])
        self.assertEqual(code, 0)
        self.assertIn("agletras doctor", out)
        self.assertIn("result: OK", out)

    def test_doctor_with_broken_config_exits_2(self) -> None:
        path = self.home / "broken.toml"
        path.write_text("[fetch]\ntimeout = -1\n", encoding="utf-8")
        code, out, _ = self.invoke(["doctor", "--skip-mcp", "--json", "--config", str(path)])
        self.assertEqual(code, 2)
        payload = self.assert_json(out)
        self.assertEqual(payload["status"], "invalid_config")


class ProfileCommandTests(CliCase):
    """Phase 8 `agletras profiles` and profile-aware `run` option resolution."""

    def write_config(self, text: str) -> None:
        (self.home / "agletras.toml").write_text(text, encoding="utf-8")

    def test_profiles_with_no_config_file(self) -> None:
        code, out, _ = self.invoke(["profiles"])
        self.assertEqual(code, 0)
        self.assertIn("profiles: 0", out)
        self.assertIn("[profiles.<name>]", out)

    def test_profiles_lists_defined_profiles_and_branding(self) -> None:
        self.write_config(
            """
[branding]
name = "Acme"

[profiles.quick]
depth = "quick"
max_sources = 5
"""
        )
        code, out, _ = self.invoke(["profiles"])
        self.assertEqual(code, 0)
        self.assertIn("quick: depth=quick, max_sources=5", out)
        self.assertIn("branding: Acme", out)

    def test_profiles_json_payload_shape(self) -> None:
        self.write_config('[profiles.quick]\nparallel = true\n\n[branding]\nname = "Acme"\n')
        code, out, _ = self.invoke(["profiles", "--json"])
        self.assertEqual(code, 0)
        payload = self.assert_json(out)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["profiles"]["quick"], {"parallel": True})
        self.assertEqual(payload["branding"], {"name": "Acme"})

    def test_profiles_shows_one_definition(self) -> None:
        self.write_config('[profiles.quick]\ndepth = "quick"\nmax_sources = 5\n')
        code, out, _ = self.invoke(["profiles", "quick"])
        self.assertEqual(code, 0)
        self.assertIn("profile quick:", out)
        self.assertIn("max_sources = 5", out)
        self.assertIn("depth = quick", out)

    def test_unknown_profile_is_a_usage_error(self) -> None:
        self.write_config("[profiles.quick]\ndepth = \"quick\"\n")
        code, _, err = self.invoke(["profiles", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("unknown profile", err)
        self.assertIn("quick", err)

    def test_broken_profile_definition_exits_2(self) -> None:
        self.write_config("[profiles.quick]\nmagic = true\n")
        code, _, err = self.invoke(["profiles"])
        self.assertEqual(code, 2)
        self.assertIn("configuration error", err)


class ProfileRunOptionTests(CliCase):
    """`agletras run --profile` precedence: flags > profile > [research] settings."""

    def write_config(self, text: str) -> None:
        (self.home / "agletras.toml").write_text(text, encoding="utf-8")

    def run_capture(self, argv: list[str]) -> tuple[int, dict, str, str]:
        captured: dict = {}

        def fake_create_run(options, settings, reporter, **kwargs):
            captured["options"] = options
            return 7

        def fake_execute_research(run_id, options, settings=None, reporter=None, **kwargs):
            captured["executed"] = options
            return {"run_id": run_id, "status": "completed", "exit_code": 0}

        with mock.patch.object(cli, "create_run", fake_create_run), mock.patch.object(
            cli, "execute_research", fake_execute_research
        ):
            code, out, err = self.invoke(argv)
        return code, captured, out, err

    def test_profile_values_reach_the_run(self) -> None:
        self.write_config(
            """
[profiles.quick]
depth = "quick"
max_sources = 5
parallel = true
reuse_evidence = true
project = "demo"
title = "Profiled"
"""
        )
        code, captured, _, err = self.run_capture(["run", "--profile", "quick", "What is X?"])
        self.assertEqual(code, 0, err)
        options = captured["options"]
        self.assertEqual(options.depth, "quick")
        self.assertEqual(options.max_sources, 5)
        self.assertTrue(options.parallel)
        self.assertTrue(options.reuse_evidence)
        self.assertEqual(options.project, "demo")
        self.assertEqual(options.title, "Profiled")
        self.assertEqual(options.question, "What is X?")

    def test_explicit_flags_beat_profile_values(self) -> None:
        self.write_config(
            """
[profiles.quick]
max_sources = 5
parallel = true
reuse_evidence = true
title = "Profiled"
"""
        )
        code, captured, _, err = self.run_capture(
            [
                "run",
                "--profile",
                "quick",
                "--max-sources",
                "3",
                "--sequential",
                "--no-reuse",
                "--title",
                "Custom",
                "What is X?",
            ]
        )
        self.assertEqual(code, 0, err)
        options = captured["options"]
        self.assertEqual(options.max_sources, 3)
        self.assertFalse(options.parallel)
        self.assertFalse(options.reuse_evidence)
        self.assertEqual(options.title, "Custom")

    def test_profile_beats_research_settings_and_settings_apply_without_one(self) -> None:
        self.write_config(
            """
[research]
max_sources = 12

[profiles.quick]
max_sources = 9
"""
        )
        code, captured, _, err = self.run_capture(["run", "Q?"])
        self.assertEqual(code, 0, err)
        self.assertEqual(captured["options"].max_sources, 12)
        code, captured, _, err = self.run_capture(["run", "--profile", "quick", "Q?"])
        self.assertEqual(code, 0, err)
        self.assertEqual(captured["options"].max_sources, 9)

    def test_branding_name_becomes_the_default_report_title(self) -> None:
        self.write_config('[branding]\nname = "Acme"\n[profiles.quick]\ndepth = "quick"\n')
        code, captured, _, err = self.run_capture(["run", "--profile", "quick", "What is X?"])
        self.assertEqual(code, 0, err)
        self.assertEqual(captured["options"].title, "Acme: What is X?")
        code, captured, _, err = self.run_capture(
            ["run", "--profile", "quick", "--title", "Custom", "What is X?"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(captured["options"].title, "Custom")

    def test_unknown_profile_fails_before_any_run_is_created(self) -> None:
        self.write_config('[profiles.quick]\ndepth = "quick"\n')
        code, captured, _, err = self.run_capture(["run", "--profile", "nope", "Q?"])
        self.assertEqual(code, 2)
        self.assertIn("unknown profile", err)
        self.assertNotIn("options", captured)

    def test_mutually_exclusive_mode_flags_are_usage_errors(self) -> None:
        code, captured, _, err = self.run_capture(["run", "--parallel", "--sequential", "Q?"])
        self.assertEqual(code, 2)
        self.assertIn("mutually exclusive", err)
        self.assertNotIn("options", captured)
        code, captured, _, err = self.run_capture(
            ["run", "--reuse-evidence", "--no-reuse", "Q?"]
        )
        self.assertEqual(code, 2)
        self.assertIn("mutually exclusive", err)
        self.assertNotIn("options", captured)

    def test_sequential_and_no_reuse_are_the_defaults_without_profile(self) -> None:
        code, captured, _, err = self.run_capture(["run", "Q?"])
        self.assertEqual(code, 0, err)
        options = captured["options"]
        self.assertFalse(options.parallel)
        self.assertFalse(options.reuse_evidence)
        self.assertEqual(options.project, "")


class ExportCommandTests(CliCase):
    """Phase 8 `agletras export`: Mermaid maps plus CSV tables."""

    def make_run(self) -> int:
        settings = cli.load_settings()
        run_id = int(evidence.start_run("Export CLI test question", settings=settings)["run_id"])
        payload = evidence.save_evidence(
            run_id=run_id,
            title="CLI source",
            text="Export test passages mention heliotrope collectors.",
            url="https://exporttest.example/page",
            settings=settings,
        )
        passage = evidence.search_evidence(
            "heliotrope", run_id=run_id, settings=settings
        )["passages"][0]
        claim_id = int(
            evidence.register_claim(
                run_id, "Heliotrope collectors appear in export tests.", settings=settings
            )["claim_id"]
        )
        evidence.attach_citation(
            claim_id,
            int(passage["passage_id"]),
            quote="heliotrope collectors",
            relation="supports",
            settings=settings,
        )
        self.assertIsNotNone(payload["source_id"])
        return run_id

    def test_export_unknown_run_exits_2(self) -> None:
        code, _, err = self.invoke(["export", "987654"])
        self.assertEqual(code, 2)
        self.assertIn("not_found", err)

    def test_export_writes_artifacts_and_reports_paths(self) -> None:
        run_id = self.make_run()
        out_dir = self.home / "visuals-out"
        code, out, _ = self.invoke(["export", str(run_id), "--out", str(out_dir)])
        self.assertEqual(code, 0)
        self.assertIn(f"run {run_id}: exported 5 file(s)", out)
        for name in ("evidence-map.mmd", "timeline.mmd", "sources.csv", "claims.csv", "citations.csv"):
            self.assertTrue((out_dir / name).is_file(), name)
        self.assertIn("Heliotrope collectors appear in export tests.", (out_dir / "claims.csv").read_text())

    def test_export_json_payload_and_format_subset(self) -> None:
        run_id = self.make_run()
        out_dir = self.home / "visuals-json"
        code, out, _ = self.invoke(
            ["export", str(run_id), "--out", str(out_dir), "--format", "mermaid", "--json"]
        )
        self.assertEqual(code, 0)
        payload = self.assert_json(out)
        self.assertEqual(payload["run_id"], run_id)
        self.assertEqual(payload["formats"], ["mermaid"])
        self.assertEqual(
            {item["name"] for item in payload["files"]}, {"evidence-map.mmd", "timeline.mmd"}
        )
        self.assertEqual(payload["counts"]["sources"], 1)
        self.assertEqual(payload["counts"]["claims"], 1)
        self.assertEqual(payload["counts"]["citations"], 1)
        self.assertFalse((out_dir / "sources.csv").exists())


class MonitorCommandTests(CliCase):
    """Phase 8 `agletras monitor`: targets, manual checks, and local reports."""

    def test_monitor_add_list_remove_round_trip(self) -> None:
        note = self.home / "note.txt"
        note.write_text("hello monitoring", encoding="utf-8")
        code, out, err = self.invoke(
            ["monitor", "add", str(note), "--name", "Note", "--project", "acme"]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("added", out)
        self.assertIn("nothing is fetched until", out)

        code, out, _ = self.invoke(["monitor"])  # default action: list
        self.assertEqual(code, 0)
        self.assertIn("1: Note", out)
        self.assertIn("never checked", out)
        self.assertIn("project=acme", out)

        code, out, _ = self.invoke(["monitor", "list", "--project", "acme", "--json"])
        payload = self.assert_json(out)
        self.assertEqual(payload["count"], 1)
        target = payload["targets"][0]
        self.assertEqual(target["kind"], "path")
        self.assertEqual(target["project"], "acme")
        self.assertIsNone(target["last_snapshot"])
        self.assertEqual(self.assert_json(self.invoke(["monitor", "list", "--project", "nope", "--json"])[1])["count"], 0)

        code, out, _ = self.invoke(["monitor", "remove", "1"])
        self.assertEqual(code, 0)
        self.assertIn("removed", out)
        self.assertIn("monitor targets: 0", self.invoke(["monitor", "list"])[1])
        code, _, err = self.invoke(["monitor", "remove", "1"])
        self.assertEqual(code, 2)
        self.assertIn("not_found", err)

    def test_check_change_detection_changes_and_report(self) -> None:
        note = self.home / "page.txt"
        note.write_text(
            "Quarterly revenue grew 12 percent.\nLast updated: 2026-10-01\n", encoding="utf-8"
        )
        self.assertEqual(self.invoke(["monitor", "add", str(note)])[0], 0)

        code, out, _ = self.invoke(["monitor", "check"])
        self.assertEqual(code, 0, out)
        self.assertIn("baseline stored", out)

        # A timestamp-only rewrite must not alert.
        note.write_text(
            "Quarterly revenue grew 12 percent.\nLast updated: 2026-11-15\n", encoding="utf-8"
        )
        code, out, _ = self.invoke(["monitor", "check", "1", "--sequential"])
        self.assertEqual(code, 0)
        self.assertIn("unchanged", out)
        self.assertEqual(self.assert_json(self.invoke(["monitor", "changes", "--json"])[1])["count"], 0)

        # A substantive edit is a meaningful change with both sides on record.
        note.write_text(
            "Quarterly revenue grew 12 percent.\nTwo new offices opened in Lisbon.\n"
            "Last updated: 2026-11-15\n",
            encoding="utf-8",
        )
        code, out, _ = self.invoke(["monitor", "check"])
        self.assertEqual(code, 0)
        self.assertIn("meaningful change", out)

        code, out, _ = self.invoke(["monitor", "changes", "--target", "1"])
        self.assertEqual(code, 0)
        self.assertIn("changes: 1", out)
        self.assertIn("Two new offices opened in Lisbon.", out)
        changes = self.assert_json(
            self.invoke(["monitor", "changes", "--since-days", "1", "--json"])[1]
        )
        self.assertEqual(changes["count"], 1)
        self.assertEqual(changes["changes"][0]["change"]["status"], "changed")
        self.assertEqual(changes["changes"][0]["target"]["target_id"], 1)

        # The local report is written inside the reports directory and links evidence.
        code, out, _ = self.invoke(["monitor", "report"])
        self.assertEqual(code, 0, out)
        self.assertIn("monitoring report written:", out)
        payload = self.assert_json(self.invoke(["monitor", "report", "--json"])[1])
        report_path = Path(payload["path"])
        self.assertTrue(report_path.is_file())
        self.assertTrue(report_path.name.startswith("monitor-report-"))
        text = report_path.read_text(encoding="utf-8")
        self.assertIn("## Detected changes", text)
        self.assertIn("Two new offices opened in Lisbon.", text)
        self.assertIn("## Failed checks (not interpreted as deletion)", text)
        reports_root = (Path(self.home) / "reports").resolve()
        self.assertTrue(report_path.resolve().is_relative_to(reports_root))
        # A second render never overwrites the first file.
        second = Path(self.assert_json(self.invoke(["monitor", "report", "--json"])[1])["path"])
        self.assertNotEqual(second, report_path)

    def test_failed_check_is_never_a_change_or_deletion(self) -> None:
        note = self.home / "flaky.txt"
        note.write_text("stable content to monitor", encoding="utf-8")
        self.assertEqual(self.invoke(["monitor", "add", str(note), "--name", "Flaky"])[0], 0)
        self.assertEqual(self.invoke(["monitor", "check"])[0], 0)

        note.unlink()  # the target disappears, e.g. an unreachable page
        code, out, _ = self.invoke(["monitor", "check", "1"])
        self.assertEqual(code, 0)
        self.assertIn("failed", out)
        self.assertIn("not a change or deletion", out)
        self.assertEqual(self.assert_json(self.invoke(["monitor", "changes", "--json"])[1])["count"], 0)

        code, out, _ = self.invoke(["monitor", "list"])
        self.assertEqual(code, 0)
        self.assertIn("failed", out)
        payload = self.assert_json(self.invoke(["monitor", "report", "--json"])[1])
        text = Path(payload["path"]).read_text(encoding="utf-8")
        self.assertIn("## Failed checks (not interpreted as deletion)", text)
        self.assertIn("no deletion is inferred", text)

    def test_report_without_targets_creates_nothing(self) -> None:
        code, out, _ = self.invoke(["monitor", "report"])
        self.assertEqual(code, 0)
        self.assertIn("no monitoring report written", out)
        self.assertEqual(list((Path(self.home) / "reports").glob("monitor-report-*.md")), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

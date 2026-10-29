"""Tests for `agletras doctor` / ``agletras_mcp.doctor``.

Doctor checks are hermetic: temporary homes, dead-port TCP probes, and one
subprocess MCP startup probe. Secrets are checked by construction — the report
JSON is searched for the literal credential values that must never appear.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from agletras_mcp import doctor, storage
from agletras_mcp.config import load_settings

from tests.support import TempHomeCase


class DoctorCase(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def run_doctor(self, **overrides) -> dict:
        return doctor.run_doctor(
            self.settings,
            include_mcp=overrides.get("include_mcp", False),
            mcp_timeout=overrides.get("mcp_timeout", 60.0),
        )


class DoctorRequiredTests(DoctorCase):
    def test_all_required_checks_pass_in_a_clean_home(self) -> None:
        report = self.run_doctor()
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["required_failures"], [])
        names = {check["name"] for check in report["checks"]}
        self.assertLessEqual(
            {"runtime", "dependencies", "config", "paths", "database", "search_adapter"}, names
        )
        self.assertNotIn("mcp_server", names)  # include_mcp=False
        for check in report["checks"]:
            if check["name"] == "images":
                self.assertIn(check["status"], ("ok", "skipped", "warn"))

    def test_render_human_lists_every_check_and_the_result(self) -> None:
        report = self.run_doctor()
        text = doctor.render_human(report)
        self.assertIn("agletras doctor", text)
        for check in report["checks"]:
            self.assertIn(check["name"], text)
        self.assertIn("result: OK", text)

    def test_invalid_configuration_fails_with_exit_code_2(self) -> None:
        settings = load_settings(overrides={"collect_concurrency": 99})
        report = doctor.run_doctor(settings, include_mcp=False)
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(report["status"], "invalid_config")
        self.assertIn("config", report["required_failures"])

    def test_config_load_error_is_reported_without_crashing(self) -> None:
        report = doctor.run_doctor(None, config_error="bad TOML: nope", include_mcp=False)
        self.assertEqual(report["exit_code"], 2)
        config_check = next(check for check in report["checks"] if check["name"] == "config")
        self.assertEqual(config_check["status"], "fail")
        self.assertIn("remediation", config_check)

    def test_data_dir_that_is_not_a_directory_is_a_required_failure(self) -> None:
        # A file where a directory belongs fails the path check without
        # permission games (also safe when the suite runs as root).
        not_a_dir = self.home / "data-dir-is-a-file"
        not_a_dir.write_text("not a directory", encoding="utf-8")
        os.environ["AGLETRAS_DATA_DIR"] = str(not_a_dir)
        settings = load_settings()
        # The paths check itself fails with a remediation...
        paths_check = doctor._check_paths(settings)
        self.assertEqual(paths_check.status, "fail")
        self.assertIn("not a directory", paths_check.detail)
        # ...and doctor surfaces it as a required failure (via the config
        # validation that flags the same path problem first).
        report = doctor.run_doctor(settings, include_mcp=False)
        self.assertIn(report["exit_code"], (2, 3))
        self.assertTrue(report["required_failures"])

    def test_database_with_a_newer_schema_is_refused(self) -> None:
        with storage.open_db(self.settings) as conn:
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (999, 'future-migration', '2030-01-01T00:00:00+00:00')"
            )
        report = self.run_doctor()
        self.assertEqual(report["exit_code"], 3)
        database = next(check for check in report["checks"] if check["name"] == "database")
        self.assertEqual(database["status"], "fail")
        self.assertIn("newer", database["detail"])

    def test_quick_check_failure_is_a_required_failure(self) -> None:
        with mock.patch.object(doctor, "MINIMUM_PACKAGES", {"definitely-not-installed": "1.0"}):
            report = doctor.run_doctor(self.settings, include_mcp=False)
        self.assertEqual(report["exit_code"], 3)
        dependencies = next(check for check in report["checks"] if check["name"] == "dependencies")
        self.assertEqual(dependencies["status"], "fail")
        self.assertIn("pip install", dependencies["remediation"])


class DoctorOptionalTests(DoctorCase):
    def test_unreachable_configured_endpoint_is_optional_and_does_not_flip_exit_code(self) -> None:
        # Port 9 (discard) on loopback is closed; the probe must fail cleanly.
        os.environ["AGLETRAS_VERIFICATION_MODEL_ENDPOINT"] = "http://127.0.0.1:9/v1"
        report = doctor.run_doctor(load_settings(), include_mcp=False)
        endpoints = next(
            check for check in report["checks"] if check["name"] == "model_endpoints"
        )
        self.assertEqual(endpoints["status"], "fail")
        self.assertFalse(endpoints["required"])
        self.assertEqual(report["exit_code"], 0)
        self.assertIn("model_endpoints", report["optional_failures"])

    def test_vision_is_skipped_or_warned_when_unconfigured(self) -> None:
        images = next(check for check in self.run_doctor()["checks"] if check["name"] == "images")
        self.assertIn(images["status"], ("skipped", "warn"))
        self.assertFalse(images["required"])

    def test_opencode_check_works_on_the_real_project_config(self) -> None:
        report = self.run_doctor()
        opencode = next(
            check for check in report["checks"] if check["name"] == "opencode"
        )
        # The project ships opencode.json registering the stdio server.
        self.assertEqual(opencode["status"], "ok")
        self.assertIn("opencode", str(opencode["data"]))

    def test_opencode_example_config_validates(self) -> None:
        import pathlib

        root = pathlib.Path(self.home) / "project"
        root.mkdir()
        example = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "servers": {
                    "agletras": {
                        "type": "local",
                        "command": [os.sys.executable, "-m", "agletras_mcp"],
                        "codemode": False,
                    }
                }
            },
        }
        (root / "opencode.json").write_text(json.dumps(example), encoding="utf-8")
        with mock.patch.object(doctor, "PROJECT_ROOT", root):
            report = self.run_doctor()
        opencode = next(
            check for check in report["checks"] if check["name"] == "opencode"
        )
        self.assertEqual(opencode["status"], "ok")
        self.assertIn("registers the agletras stdio server", opencode["detail"])


class DoctorSecurityTests(DoctorCase):
    def test_safe_endpoint_strips_userinfo(self) -> None:
        self.assertEqual(
            doctor._safe_endpoint("http://user:s3cret@example.com:8080/v1/models"),
            "http://example.com:8080",
        )
        self.assertEqual(doctor._safe_endpoint("https://127.0.0.1:9"), "https://127.0.0.1:9")
        # Paths and queries never survive into the display form.
        self.assertNotIn("v1/models", doctor._safe_endpoint("https://example.com/v1/models?x=1"))

    def test_no_credentials_appear_anywhere_in_the_report(self) -> None:
        os.environ["AGLETRAS_VERIFICATION_MODEL_ENDPOINT"] = "http://user:topsecret@127.0.0.1:9/v1"
        report = doctor.run_doctor(load_settings(), include_mcp=False)
        dumped = json.dumps(report)
        self.assertNotIn("topsecret", dumped)
        self.assertIn("***", dumped)  # the endpoint itself is redacted


class DoctorMcpTests(DoctorCase):
    def test_mcp_startup_and_tool_discovery_over_stdio(self) -> None:
        report = doctor.run_doctor(self.settings, include_mcp=True, mcp_timeout=90.0)
        mcp = next(check for check in report["checks"] if check["name"] == "mcp_server")
        self.assertEqual(mcp["status"], "ok")
        self.assertGreaterEqual(mcp["data"]["tool_count"], 34)
        self.assertEqual(report["exit_code"], 0)

    def test_mcp_check_handles_a_broken_server_gracefully(self) -> None:
        # A file where the database directory belongs makes the child's
        # health_check report a degraded store.
        not_a_dir = self.home / "data-dir-is-a-file"
        not_a_dir.write_text("not a directory", encoding="utf-8")
        os.environ["AGLETRAS_DATA_DIR"] = str(not_a_dir)
        settings = load_settings()
        report = doctor.run_doctor(settings, include_mcp=True, mcp_timeout=90.0)
        mcp = next(check for check in report["checks"] if check["name"] == "mcp_server")
        self.assertEqual(mcp["status"], "fail")
        # The config check flags the same broken path first, so doctor exits 2
        # (invalid config) or 3 (degraded) — either way the failure is visible.
        self.assertIn(report["exit_code"], (2, 3))
        self.assertIn("mcp_server", report["required_failures"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

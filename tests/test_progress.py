"""Tests for the Phase 7 structured progress reporter (human + JSONL)."""

from __future__ import annotations

import io
import json
import unittest

from agletras_mcp.progress import ProgressReporter, sanitize_text

from tests.support import TempHomeCase


class SanitizeTests(unittest.TestCase):
    def test_credentials_in_urls_are_masked(self) -> None:
        clean = sanitize_text("fetch failed for https://alice:s3cr3t@example.com/private")
        self.assertIn("alice:***@example.com", clean)
        self.assertNotIn("s3cr3t", clean)

    def test_newlines_collapse_and_long_messages_are_clipped(self) -> None:
        self.assertEqual(sanitize_text("line one\r\nline two"), "line one line two")
        clipped = sanitize_text("x" * 900)
        self.assertEqual(len(clipped), 500)
        self.assertTrue(clipped.endswith("..."))


class ReporterTests(TempHomeCase):
    def make(self, **kwargs) -> tuple[ProgressReporter, io.StringIO]:
        stream = io.StringIO()
        reporter = ProgressReporter(stream=stream, **kwargs)
        return reporter, stream

    def test_human_lines_carry_run_stage_status_and_duration(self) -> None:
        reporter, stream = self.make(run_id=7)
        reporter.info("run_created", "created", stage="created")
        reporter.stage_end("plan", "complete", duration_ms=12)
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("run=7", lines[0])
        self.assertIn("stage=created", lines[0])
        self.assertIn("run_created", lines[0])
        self.assertIn("(complete)", lines[1])
        self.assertIn("[12 ms]", lines[1])

    def test_json_format_emits_one_object_per_line(self) -> None:
        reporter, stream = self.make(run_id=3, log_format="json")
        reporter.info("search", "searched", stage="search@0", data={"results": 4})
        reporter.warn("careful")
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(records[0]["event"], "search")
        self.assertEqual(records[0]["run_id"], 3)
        self.assertEqual(records[0]["data"], {"results": 4})
        self.assertEqual(records[1]["level"], "warning")

    def test_quiet_hides_info_but_not_errors(self) -> None:
        reporter, stream = self.make(verbosity="quiet")
        reporter.info("noise", "hidden")
        reporter.error("boom", code="internal")
        output = stream.getvalue()
        self.assertNotIn("noise", output)
        self.assertIn("error", output)
        self.assertIn("boom", output)

    def test_verbose_adds_debug_lines(self) -> None:
        reporter, stream = self.make(verbosity="verbose")
        reporter.debug("details", steps=2)
        self.assertIn("debug", stream.getvalue())

    def test_invalid_verbosity_and_format_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ProgressReporter(verbosity="loud", stream=io.StringIO())
        with self.assertRaises(ValueError):
            ProgressReporter(log_format="yaml", stream=io.StringIO())

    def test_log_file_flushes_buffered_records_when_attached(self) -> None:
        reporter, _stream = self.make(run_id=1)
        reporter.info("run_created", "before attach", stage="created")
        log_path = self.home / "logs" / "run-1.jsonl"
        reporter.attach_log_file(log_path)
        reporter.stage_start("plan")
        reporter.close()
        self.assertEqual(reporter.log_file_path, log_path)
        records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["event"] for record in records], ["run_created", "stage_start"])
        self.assertEqual(records[0]["message"], "before attach")

    def test_log_file_never_contains_raw_credentials(self) -> None:
        log_path = self.home / "logs" / "secret.jsonl"
        reporter, stream = self.make(run_id=2, log_file_path=log_path, log_format="json")
        reporter.error("auth failed for https://user:hunter2@example.com/x", code="fetch")
        reporter.close()
        for content in (log_path.read_text(encoding="utf-8"), stream.getvalue()):
            self.assertNotIn("hunter2", content)
            self.assertIn("user:***@example.com", content)

    def test_data_values_are_shaped_and_bounded(self) -> None:
        reporter, stream = self.make(log_format="json")
        reporter.info(
            "collect",
            "batch",
            data={"urls": [f"https://example.com/{index}" for index in range(30)], "big": "y" * 700},
        )
        record = json.loads(stream.getvalue().strip())
        self.assertEqual(len(record["data"]["urls"]), 20)
        self.assertEqual(len(record["data"]["big"]), 500)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

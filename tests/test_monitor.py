"""Tests for Phase 8 monitoring: targets, snapshots, meaningful-change detection.

All servers are local fixtures. The tests pin the three monitoring rules:
failures are never interpreted as changes or deletions, boilerplate-only
rewrites produce no alert, and every detected change stays linked to the
stored evidence for both sides of the comparison.
"""

from __future__ import annotations

import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

from agletras_mcp import monitor, storage
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import TempHomeCase


class HttpFixture:
    """Mutable body + conditional-request behavior for one test path."""

    def __init__(self, body: str = "", *, content_type: str = "text/html; charset=utf-8") -> None:
        self.body = body.encode("utf-8")
        self.content_type = content_type
        self.etag = ""
        self.last_modified = ""
        self.fail_status = 0
        self.requests = 0
        self.not_modified = 0
        self.conditional_headers: list[tuple[str, str]] = []
        self.served_paths: set[str] | None = None  # None serves every path

    def set_body(self, text: str) -> None:
        self.body = text.encode("utf-8")


@contextmanager
def serve_fixture(fixture: HttpFixture) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            fixture.requests += 1
            path = self.path.split("?")[0]
            if fixture.served_paths is not None and path not in fixture.served_paths:
                payload = b"not found"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if fixture.fail_status:
                payload = b"fixture failure"
                self.send_response(fixture.fail_status)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if_none_match = self.headers.get("If-None-Match") or ""
            if_modified_since = self.headers.get("If-Modified-Since") or ""
            if if_none_match or if_modified_since:
                fixture.conditional_headers.append((if_none_match, if_modified_since))
            if fixture.etag and if_none_match and if_none_match == fixture.etag:
                fixture.not_modified += 1
                self.send_response(304)
                self.send_header("ETag", fixture.etag)
                self.end_headers()
                return
            if (
                fixture.last_modified
                and if_modified_since
                and if_modified_since == fixture.last_modified
            ):
                fixture.not_modified += 1
                self.send_response(304)
                self.send_header("Last-Modified", fixture.last_modified)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", fixture.content_type)
            if fixture.etag:
                self.send_header("ETag", fixture.etag)
            if fixture.last_modified:
                self.send_header("Last-Modified", fixture.last_modified)
            self.send_header("Content-Length", str(len(fixture.body)))
            self.end_headers()
            self.wfile.write(fixture.body)

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _page(
    *,
    revenue: str = "Revenue grew by 12 percent in the third quarter.",
    extra: tuple[str, ...] = ("The company opened two new offices in Lisbon.",),
    nav: tuple[str, ...] = ("Home", "Search"),
    year: str = "2026",
    updated: str = "2026-10-01",
    cookies: bool = False,
) -> str:
    paragraphs = "".join(f"<p>{text}</p>" for text in extra)
    nav_items = "".join(f"<li>{item}</li>" for item in nav)
    banner = (
        '<div>We use cookies to improve your experience.</div>'
        '<div>Accept all cookies</div>'
        if cookies
        else ""
    )
    return (
        "<html><head><title>Example quarterly report</title></head><body>"
        f"<nav><ul>{nav_items}</ul></nav>"
        f"{banner}"
        f"<h1>Quarterly report</h1><p>{revenue}</p>{paragraphs}"
        f"<footer>© {year} Example Corp. All rights reserved. Last updated: {updated}</footer>"
        "</body></html>"
    )


class MonitorTestCase(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def _add(self, target: str, **kwargs) -> dict:
        return monitor.add_target(target, settings=self.settings, **kwargs)

    def _check(self, target_id: int, **kwargs) -> dict:
        return monitor.check_target(target_id, settings=self.settings, **kwargs)


class TargetManagementTests(MonitorTestCase):
    def test_add_is_idempotent_by_canonical_url(self) -> None:
        first = self._add("http://example.com/page#section")
        self.assertTrue(first["created"])
        second = self._add("http://example.com/page?utm_source=newsletter")
        self.assertFalse(second["created"])
        self.assertEqual(first["target"]["target_id"], second["target"]["target_id"])
        listing = monitor.list_targets(settings=self.settings)
        self.assertEqual(listing["count"], 1)

    def test_named_target_keeps_name_and_project(self) -> None:
        added = self._add("http://example.com/page", name="Pricing page", project="alpha")
        target = added["target"]
        self.assertEqual(target["name"], "Pricing page")
        self.assertEqual(target["project"], "alpha")
        listing = monitor.list_targets(project="alpha", settings=self.settings)
        self.assertEqual(listing["count"], 1)
        self.assertEqual(monitor.list_targets(project="beta", settings=self.settings)["count"], 0)

    def test_update_merges_config_without_duplicate_row(self) -> None:
        first = self._add("http://example.com/page", name="One", config={"depth": 2})
        second = self._add("http://example.com/page", name="Two", config={"mode": "text"})
        self.assertFalse(second["created"])
        self.assertEqual(second["target"]["name"], "Two")
        self.assertEqual(second["target"]["config"], {"depth": 2, "mode": "text"})
        self.assertEqual(first["target"]["target_id"], second["target"]["target_id"])

    def test_local_path_targets_resolve_and_identity_on_the_resolved_path(self) -> None:
        sample = self.home / "note.txt"
        sample.write_text("hello monitoring", encoding="utf-8")
        added = self._add(str(sample.parent / "." / "note.txt"))
        self.assertEqual(added["target"]["kind"], "path")
        listing = monitor.list_targets(settings=self.settings)
        self.assertEqual(listing["count"], 1)

    def test_path_outside_access_roots_is_rejected_upfront(self) -> None:
        outside = Path(self.home).parent
        with self.assertRaises(ToolError) as ctx:
            self._add(str(outside))
        self.assertEqual(ctx.exception.code, "access_denied")

    def test_invalid_targets_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self._add("   ")
        with self.assertRaises(ToolError):
            self._add("http://example.com/page", kind="ftp")
        with self.assertRaises(ToolError) as ctx:
            self._add("ftp://example.com/file")
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_remove_cascades_to_snapshots(self) -> None:
        fixture = HttpFixture(_page())
        with serve_fixture(fixture) as base:
            target_id = self._add(base + "/page")["target"]["target_id"]
            self._check(target_id)
        removed = monitor.remove_target(target_id, settings=self.settings)
        self.assertTrue(removed["removed"])
        self.assertEqual(removed["snapshots_removed"], 1)
        with storage.open_db(self.settings) as conn:
            orphaned = conn.execute(
                "SELECT COUNT(*) AS n FROM monitor_snapshots WHERE target_id = ?", (target_id,)
            ).fetchone()["n"]
        self.assertEqual(orphaned, 0)
        with self.assertRaises(ToolError) as ctx:
            self._check(target_id)
        self.assertEqual(ctx.exception.code, "not_found")


class ChangeDetectionTests(MonitorTestCase):
    def test_initial_check_stores_baseline_evidence(self) -> None:
        fixture = HttpFixture(_page())
        with serve_fixture(fixture) as base:
            target_id = self._add(base + "/page", name="Report")["target"]["target_id"]
            result = self._check(target_id)
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["status"], "initial")
        self.assertFalse(snapshot["meaningful"])
        self.assertIsNotNone(snapshot["source_id"])
        self.assertTrue(snapshot["content_hash"])
        self.assertIn("Baseline stored", snapshot["summary"])
        self.assertEqual(snapshot["source"]["source_id"], snapshot["source_id"])

    def test_identical_recheck_is_unchanged_and_adds_no_evidence(self) -> None:
        fixture = HttpFixture(_page())
        with serve_fixture(fixture) as base:
            target_id = self._add(base + "/page")["target"]["target_id"]
            first = self._check(target_id)["snapshot"]
            second = self._check(target_id)["snapshot"]
        self.assertEqual(second["status"], "unchanged")
        self.assertFalse(second["meaningful"])
        self.assertEqual(second["source_id"], first["source_id"])
        self.assertIn("No content change", second["summary"])
        self.assertEqual(monitor.changes(settings=self.settings)["count"], 0)

    def test_boilerplate_only_rewrite_is_not_a_change(self) -> None:
        fixture = HttpFixture(_page())
        with serve_fixture(fixture) as base:
            target_id = self._add(base + "/page")["target"]["target_id"]
            self._check(target_id)
            fixture.set_body(_page(nav=("Home", "Search", "Contact"), year="2027",
                                   updated="2027-01-05", cookies=True))
            result = self._check(target_id)
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["status"], "unchanged")
        self.assertFalse(snapshot["meaningful"])
        self.assertEqual(monitor.changes(settings=self.settings)["count"], 0)

    def test_substantive_change_is_detected_with_both_sides_of_the_diff(self) -> None:
        fixture = HttpFixture(_page())
        with serve_fixture(fixture) as base:
            target_id = self._add(base + "/page", name="Report")["target"]["target_id"]
            first = self._check(target_id)["snapshot"]
            fixture.set_body(
                _page(
                    revenue="Revenue grew by 15 percent in the third quarter.",
                    extra=(
                        "The company opened two new offices in Lisbon.",
                        "The company announced a new product line.",
                    ),
                )
            )
            result = self._check(target_id)
            changes = monitor.changes(settings=self.settings)
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["status"], "changed")
        self.assertTrue(snapshot["meaningful"])
        self.assertIn("Revenue grew by 15 percent", " ".join(snapshot["added_terms"]))
        self.assertIn("new product line", " ".join(snapshot["added_terms"]))
        self.assertIn("Revenue grew by 12 percent", " ".join(snapshot["removed_terms"]))
        self.assertEqual(changes["count"], 1)
        item = changes["changes"][0]
        self.assertEqual(item["target"]["target_id"], target_id)
        self.assertEqual(item["change"]["previous_snapshot_id"], first["snapshot_id"])
        self.assertEqual(item["previous"]["snapshot_id"], first["snapshot_id"])
        self.assertIsNotNone(item["change"]["source_id"])
        self.assertEqual(item["change"]["content_hash"], snapshot["content_hash"])

    def test_changed_status_has_diff_against_last_stored_evidence(self) -> None:
        """A boilerplate-only hash change never detaches the evidence chain.

        Heavy duplication differences are the one path that reaches a
        ``changed`` snapshot without storing new evidence; the next real diff
        must still be computed against the last stored text.
        """
        fixture = HttpFixture(_page())
        with serve_fixture(fixture) as base:
            target_id = self._add(base + "/page")["target"]["target_id"]
            first = self._check(target_id)["snapshot"]
            # Duplicate one substantive line: line sets equal, hash differs.
            fixture.set_body(
                _page(
                    extra=(
                        "The company opened two new offices in Lisbon.",
                        "The company opened two new offices in Lisbon.",
                    ),
                )
            )
            duplicate = self._check(target_id)["snapshot"]
            fixture.set_body(
                _page(revenue="Revenue fell by 3 percent in the fourth quarter.")
            )
            changed = self._check(target_id)["snapshot"]
        self.assertEqual(duplicate["status"], "changed")
        self.assertFalse(duplicate["meaningful"])
        self.assertEqual(changed["status"], "changed")
        self.assertTrue(changed["meaningful"])
        self.assertIn("Revenue fell by 3 percent", " ".join(changed["added_terms"]))
        self.assertIn("Revenue grew by 12 percent", " ".join(changed["removed_terms"]))
        self.assertNotIn("new offices", " ".join(changed["removed_terms"]))

    def test_conditional_request_records_not_modified(self) -> None:
        fixture = HttpFixture(_page())
        fixture.etag = '"v1"'
        with serve_fixture(fixture) as base:
            target_id = self._add(base + "/page")["target"]["target_id"]
            self._check(target_id)
            result = self._check(target_id)
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["status"], "not_modified")
        self.assertFalse(snapshot["meaningful"])
        self.assertEqual(fixture.not_modified, 1)
        self.assertEqual(snapshot["etag"], '"v1"')
        self.assertEqual(monitor.changes(settings=self.settings)["count"], 0)

    def test_last_modified_conditional_request_works_without_etag(self) -> None:
        fixture = HttpFixture(_page())
        fixture.last_modified = "Wed, 01 Oct 2026 10:00:00 GMT"
        with serve_fixture(fixture) as base:
            target_id = self._add(base + "/page")["target"]["target_id"]
            self._check(target_id)
            result = self._check(target_id)
        self.assertEqual(result["snapshot"]["status"], "not_modified")
        self.assertEqual(fixture.not_modified, 1)

    def test_force_content_bypasses_conditional_shortcut(self) -> None:
        fixture = HttpFixture(_page())
        fixture.etag = '"v1"'
        with serve_fixture(fixture) as base:
            target_id = self._add(base + "/page")["target"]["target_id"]
            self._check(target_id)
            result = self._check(target_id, force_content=True)
        self.assertEqual(result["snapshot"]["status"], "unchanged")
        self.assertEqual(fixture.not_modified, 0)
        self.assertEqual(fixture.requests, 2)

    def test_failed_fetch_is_recorded_but_never_a_change_or_deletion(self) -> None:
        fixture = HttpFixture(_page())
        with serve_fixture(fixture) as base:
            target_id = self._add(base + "/page")["target"]["target_id"]
            baseline = self._check(target_id)["snapshot"]
            fixture.fail_status = 502
            failed = self._check(target_id)["snapshot"]
            fixture.fail_status = 0
            recovered = self._check(target_id)["snapshot"]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error_code"], "network_error")
        self.assertIsNotNone(failed["source_id"])  # the stored evidence is untouched
        self.assertEqual(failed["source_id"], baseline["source_id"])
        self.assertIn("not evidence that the source changed or was deleted", failed["summary"])
        self.assertEqual(monitor.changes(settings=self.settings)["count"], 0)
        # The source and its text are still retrievable: no deletion inferred.
        source = storage.get_source(baseline["source_id"], settings=self.settings)
        self.assertIn("Revenue grew by 12 percent", source["content"])
        self.assertEqual(recovered["status"], "unchanged")

    def test_local_file_change_detected(self) -> None:
        sample = self.home / "notes.txt"
        sample.write_text("Alpha finding\nBeta finding\n", encoding="utf-8")
        target_id = self._add(str(sample), name="Notes")["target"]["target_id"]
        first = self._check(target_id)["snapshot"]
        self.assertEqual(first["status"], "initial")
        sample.write_text("Alpha finding\nGamma finding\n", encoding="utf-8")
        result = self._check(target_id)
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["status"], "changed")
        self.assertTrue(snapshot["meaningful"])
        self.assertIn("Gamma finding", " ".join(snapshot["added_terms"]))
        self.assertIn("Beta finding", " ".join(snapshot["removed_terms"]))

    def test_deleted_local_file_is_failed_not_deleted(self) -> None:
        sample = self.home / "notes.txt"
        sample.write_text("content before deletion\n", encoding="utf-8")
        target_id = self._add(str(sample))["target"]["target_id"]
        baseline = self._check(target_id)["snapshot"]
        sample.unlink()
        failed = self._check(target_id)["snapshot"]
        self.assertEqual(failed["status"], "failed")
        self.assertNotEqual(failed["error_code"], "")
        self.assertEqual(monitor.changes(settings=self.settings)["count"], 0)
        source = storage.get_source(baseline["source_id"], settings=self.settings)
        self.assertIn("content before deletion", source["content"])

    def test_disabled_target_is_not_checked(self) -> None:
        fixture = HttpFixture(_page())
        with serve_fixture(fixture) as base:
            target_id = self._add(base + "/page")["target"]["target_id"]
            with storage.open_db(self.settings) as conn:
                conn.execute("UPDATE monitor_targets SET enabled = 0 WHERE id = ?", (target_id,))
            result = self._check(target_id)
        self.assertFalse(result["checked"])
        self.assertEqual(result["reason"], "target is disabled")
        self.assertEqual(fixture.requests, 0)


class RunChecksTests(MonitorTestCase):
    def test_batch_parallel_and_sequential_agree_on_identical_workload(self) -> None:
        fixture = HttpFixture(_page())
        with serve_fixture(fixture) as base:
            ids = [self._add(f"{base}/page")["target"]["target_id"]]  # same URL identity
            # Distinct paths per target keep them separate while sharing content.
            monitor.remove_target(ids[0], settings=self.settings)
            ids = [
                self._add(f"{base}/p{index}")["target"]["target_id"] for index in range(4)
            ]
            parallel_result = monitor.run_checks(
                target_ids=ids[:2], parallel_mode=True, settings=self.settings
            )
            sequential_result = monitor.run_checks(
                target_ids=ids[2:], parallel_mode=False, settings=self.settings
            )
        self.assertEqual(parallel_result["counts_by_status"], {"initial": 2})
        self.assertEqual(sequential_result["counts_by_status"], {"initial": 2})
        self.assertEqual(
            [snapshot["target_id"] for snapshot in parallel_result["snapshots"]], ids[:2]
        )
        self.assertEqual(
            [snapshot["target_id"] for snapshot in sequential_result["snapshots"]], ids[2:]
        )

    def test_batch_one_failure_does_not_abort_other_targets(self) -> None:
        fixture = HttpFixture(_page())
        fixture.served_paths = {"/good"}
        with serve_fixture(fixture) as base:
            good = self._add(base + "/good")["target"]["target_id"]
            bad = self._add(base + "/missing")["target"]["target_id"]
            result = monitor.run_checks(
                target_ids=[good, bad], parallel_mode=True, settings=self.settings
            )
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["counts_by_status"], {"initial": 1, "failed": 1})
        self.assertEqual(result["warnings"], [])

    def test_batch_with_no_targets_is_empty_not_an_error(self) -> None:
        result = monitor.run_checks(settings=self.settings)
        self.assertEqual(result["checked"], 0)
        self.assertIn("no matching monitoring targets", result["warnings"][0])


class SnapshotHistoryTests(MonitorTestCase):
    def test_list_snapshots_newest_first_with_evidence_refs(self) -> None:
        fixture = HttpFixture(_page())
        with serve_fixture(fixture) as base:
            target_id = self._add(base + "/page")["target"]["target_id"]
            self._check(target_id)
            fixture.set_body(_page(revenue="Revenue grew by 40 percent in the third quarter."))
            self._check(target_id)
            history = monitor.list_snapshots(target_id, settings=self.settings)
        statuses = [item["status"] for item in history["snapshots"]]
        self.assertEqual(statuses, ["changed", "initial"])
        self.assertTrue(all(item["source_id"] for item in history["snapshots"]))
        self.assertTrue(all(item["source"] for item in history["snapshots"]))
        self.assertEqual(history["count"], 2)

    def test_changes_filters_meaningful_default(self) -> None:
        fixture = HttpFixture(_page())
        with serve_fixture(fixture) as base:
            target_id = self._add(base + "/page", project="p1")["target"]["target_id"]
            self._check(target_id)
            fixture.set_body(_page(revenue="Revenue grew by 22 percent."))
            self._check(target_id)
            meaningful = monitor.changes(project="p1", settings=self.settings)
            all_changes = monitor.changes(
                project="p1", meaningful_only=False, settings=self.settings
            )
        self.assertEqual(meaningful["count"], 1)
        self.assertEqual(all_changes["count"], 1)
        self.assertEqual(meaningful["changes"][0]["change"]["status"], "changed")


class ReportTests(MonitorTestCase):
    def test_report_links_evidence_and_survives_repeated_renders(self) -> None:
        fixture = HttpFixture(_page())
        with serve_fixture(fixture) as base:
            target_id = self._add(base + "/page", name="Quarterly")["target"]["target_id"]
            self._check(target_id)
            fixture.set_body(_page(revenue="Revenue grew by 18 percent."))
            self._check(target_id)
            first = monitor.render_change_report(settings=self.settings)
            second = monitor.render_change_report(settings=self.settings)
        self.assertTrue(first["created"])
        first_path = Path(first["path"])
        second_path = Path(second["path"])
        self.assertTrue(first_path.exists())
        self.assertTrue(second_path.exists())
        self.assertNotEqual(first_path, second_path)
        self.assertTrue(str(first_path).startswith(str(self.settings.reports_dir)))
        markdown = first["markdown"]
        self.assertIn("## Detected changes", markdown)
        self.assertIn("Quarterly", markdown)
        self.assertIn("**Evidence (new):** source", markdown)
        self.assertIn("**Compared against snapshot:**", markdown)
        self.assertIn("## Failed checks (not interpreted as deletion)", markdown)
        self.assertIn("## Method and limitations", markdown)

    def test_report_shows_failures_and_boilerplate_note(self) -> None:
        fixture = HttpFixture(_page())
        with serve_fixture(fixture) as base:
            failed_target = self._add(base + "/down", name="Down")["target"]["target_id"]
            fixture.fail_status = 503
            self._check(failed_target)
            fixture.fail_status = 0
            stable_target = self._add(base + "/stable", name="Stable")["target"]["target_id"]
            self._check(stable_target)
            report = monitor.render_change_report(settings=self.settings)
        markdown = report["markdown"]
        self.assertIn("No meaningful changes were detected", markdown)
        self.assertIn("Down", markdown)
        self.assertIn("network_error", markdown)

    def test_report_without_targets_is_not_created(self) -> None:
        result = monitor.render_change_report(settings=self.settings)
        self.assertFalse(result["created"])
        self.assertIsNone(result["path"])

    def test_report_respects_project_scope(self) -> None:
        fixture = HttpFixture(_page())
        with serve_fixture(fixture) as base:
            self._add(base + "/a", name="Alpha target", project="alpha")
            self._add(base + "/b", name="Beta target", project="beta")
            report = monitor.render_change_report(project="alpha", settings=self.settings)
        self.assertIn("Alpha target", report["markdown"])
        self.assertNotIn("Beta target", report["markdown"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

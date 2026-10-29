"""Phase 8 tests: project memory (cross-run recall, freshness, reuse).

Hermetic: temporary home + SQLite store, no network. Text evidence is saved
with explicit URL provenance so recall has real stored passages to find.
"""

from __future__ import annotations

import unittest

from agletras_mcp import evidence, memory, storage
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import TempHomeCase

ALPHA_URL = "https://alpha.example/storage"
BETA_URL = "https://beta.example/citations"
ALPHA_TEXT = (
    "Alpha memory fixture: the store keeps every passage with a stable hash "
    "and exact character offsets so citations point at the passage text."
)
BETA_TEXT = (
    "Beta memory fixture: citations attach one exact stored passage to one "
    "claim and record the relation."
)


class MemoryCase(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()
        self.run_alpha = int(
            evidence.start_run("alpha question", project="alpha", settings=self.settings)["run_id"]
        )
        self.run_beta = int(
            evidence.start_run("beta question", project="beta", settings=self.settings)["run_id"]
        )
        self.alpha = evidence.save_evidence(
            text=ALPHA_TEXT, url=ALPHA_URL, run_id=self.run_alpha, settings=self.settings
        )
        self.beta = evidence.save_evidence(
            text=BETA_TEXT, url=BETA_URL, run_id=self.run_beta, settings=self.settings
        )


class RecallTests(MemoryCase):
    def test_recall_returns_exact_stored_passages_with_provenance(self) -> None:
        outcome = memory.recall("stable hash offsets", settings=self.settings)
        self.assertGreaterEqual(outcome["count"], 1)
        hit = outcome["passages"][0]
        self.assertIn("stable hash", hit["text"])
        self.assertEqual(hit["source_id"], int(self.alpha["source_id"]))
        self.assertIn(self.run_alpha, hit["run_ids"])
        self.assertIn("alpha", hit["projects"])
        self.assertIsNotNone(hit["content_hash"])
        self.assertIsInstance(hit["age_days"], float)
        self.assertIs(hit["fresh"], True)

    def test_recall_scopes_to_one_project(self) -> None:
        outcome = memory.recall(
            "passage citations", project="alpha", settings=self.settings
        )
        self.assertGreaterEqual(outcome["excluded_by_project"], 1)
        self.assertTrue(outcome["passages"])
        for hit in outcome["passages"]:
            self.assertIn("alpha", hit["projects"])
            self.assertNotIn("beta", hit["projects"])

    def test_recall_without_scope_sees_every_project(self) -> None:
        outcome = memory.recall("stored passage", settings=self.settings)
        source_ids = {int(hit["source_id"]) for hit in outcome["passages"]}
        self.assertIn(int(self.alpha["source_id"]), source_ids)
        self.assertIn(int(self.beta["source_id"]), source_ids)

    def test_recall_excludes_known_stale_hits_and_counts_them(self) -> None:
        with storage.open_db(self.settings) as conn:
            conn.execute(
                "UPDATE sources SET retrieved_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (int(self.alpha["source_id"]),),
            )
        bounded = memory.recall(
            "stable hash offsets", project="alpha", max_age_days=7, settings=self.settings
        )
        self.assertEqual(bounded["count"], 0)
        self.assertGreaterEqual(bounded["excluded_as_stale"], 1)
        unbounded = memory.recall("stable hash offsets", project="alpha", settings=self.settings)
        self.assertGreaterEqual(unbounded["count"], 1)
        self.assertIs(unbounded["passages"][0]["fresh"], True)

    def test_recall_keeps_unknown_age_hits_and_flags_fresh_null(self) -> None:
        with storage.open_db(self.settings) as conn:
            conn.execute(
                "UPDATE sources SET retrieved_at = NULL WHERE id = ?",
                (int(self.alpha["source_id"]),),
            )
            conn.execute(
                "DELETE FROM research_run_sources WHERE source_id = ?",
                (int(self.alpha["source_id"]),),
            )
        outcome = memory.recall(
            "stable hash offsets", project="", max_age_days=1, settings=self.settings
        )
        self.assertEqual(outcome["count"], 1)
        hit = outcome["passages"][0]
        self.assertIsNone(hit["age_days"])
        self.assertIsNone(hit["fresh"])

    def test_recall_for_question_builds_a_terms_query(self) -> None:
        outcome = memory.recall_for_question(
            "What keeps a stable hash and offsets?", project="alpha", settings=self.settings
        )
        self.assertEqual(outcome["question"], "What keeps a stable hash and offsets?")
        self.assertTrue(outcome["query"])
        self.assertGreaterEqual(outcome["count"], 1)

    def test_recall_for_question_rejects_empty_input(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            memory.recall_for_question("   ", settings=self.settings)
        self.assertEqual(ctx.exception.code, "invalid_argument")


class FindReusableTests(MemoryCase):
    def test_fresh_url_is_reusable_with_provenance(self) -> None:
        hit = memory.find_reusable(
            ALPHA_URL, project="alpha", max_age_days=7, settings=self.settings
        )
        self.assertTrue(hit["found"])
        self.assertEqual(hit["source_id"], int(self.alpha["source_id"]))
        self.assertEqual(hit["identity"], "https://alpha.example/storage")
        self.assertIn(self.run_alpha, hit["run_ids"])
        self.assertIsInstance(hit["age_days"], float)

    def test_unknown_url_is_not_reusable(self) -> None:
        hit = memory.find_reusable("https://alpha.example/other", settings=self.settings)
        self.assertFalse(hit["found"])
        self.assertIn("no stored source", hit["reason"])

    def test_project_mismatch_is_not_reusable(self) -> None:
        hit = memory.find_reusable(ALPHA_URL, project="beta", settings=self.settings)
        self.assertFalse(hit["found"])
        self.assertIn("not linked to project", hit["reason"])
        self.assertEqual(hit["source_id"], int(self.alpha["source_id"]))

    def test_stale_copy_is_not_reusable(self) -> None:
        with storage.open_db(self.settings) as conn:
            conn.execute(
                "UPDATE sources SET last_checked_at = '2000-01-01T00:00:00+00:00', "
                "retrieved_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (int(self.alpha["source_id"]),),
            )
        hit = memory.find_reusable(
            ALPHA_URL, project="alpha", max_age_days=7, settings=self.settings
        )
        self.assertFalse(hit["found"])
        self.assertIn("older than the freshness bound", hit["reason"])
        # Without a freshness bound the stored copy is reusable again.
        unbounded = memory.find_reusable(ALPHA_URL, project="alpha", settings=self.settings)
        self.assertTrue(unbounded["found"])

    def test_copy_without_timestamps_is_not_reusable_under_a_bound(self) -> None:
        with storage.open_db(self.settings) as conn:
            conn.execute(
                "UPDATE sources SET last_checked_at = NULL, retrieved_at = NULL WHERE id = ?",
                (int(self.alpha["source_id"]),),
            )
        hit = memory.find_reusable(
            ALPHA_URL, project="alpha", max_age_days=7, settings=self.settings
        )
        self.assertFalse(hit["found"])
        self.assertIn("no usable timestamp", hit["reason"])

    def test_failed_extraction_is_not_reusable(self) -> None:
        with storage.open_db(self.settings) as conn:
            conn.execute(
                "UPDATE sources SET extraction_status = 'failed' WHERE id = ?",
                (int(self.alpha["source_id"]),),
            )
        hit = memory.find_reusable(ALPHA_URL, project="alpha", settings=self.settings)
        self.assertFalse(hit["found"])
        self.assertIn("no usable extracted text", hit["reason"])

    def test_missing_local_path_is_not_reusable_and_does_not_raise(self) -> None:
        hit = memory.find_reusable(str(self.home / "nope.md"), settings=self.settings)
        self.assertFalse(hit["found"])
        self.assertIn("cannot be reused", hit["reason"])

    def test_empty_source_is_refused(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            memory.find_reusable("  ", settings=self.settings)
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_local_and_url_identities_do_not_cross(self) -> None:
        note = self.home / "local.md"
        note.write_text(ALPHA_TEXT, encoding="utf-8")
        evidence.save_evidence(source=str(note), run_id=self.run_alpha, settings=self.settings)
        by_path = memory.find_reusable(str(note), settings=self.settings)
        self.assertTrue(by_path["found"])
        self.assertEqual(by_path["kind"], "path")
        by_url = memory.find_reusable(ALPHA_URL, settings=self.settings)
        self.assertTrue(by_url["found"])
        self.assertEqual(by_url["kind"], "url")
        self.assertNotEqual(by_path["source_id"], by_url["source_id"])


class StatsAndAgeTests(MemoryCase):
    def test_stats_scope_to_project(self) -> None:
        alpha = memory.stats(project="alpha", settings=self.settings)
        self.assertEqual(alpha["runs"], 1)
        self.assertEqual(alpha["sources"], 1)
        self.assertGreaterEqual(alpha["passages"], 1)
        every = memory.stats(settings=self.settings)
        self.assertEqual(every["runs"], 2)
        self.assertGreaterEqual(every["sources"], 2)
        self.assertGreater(every["passages"], alpha["passages"])

    def test_age_days_handles_missing_and_bad_timestamps(self) -> None:
        self.assertIsNone(memory.age_days(None))
        self.assertIsNone(memory.age_days(""))
        self.assertIsNone(memory.age_days("not a timestamp"))
        age = memory.age_days("2000-01-01T00:00:00+00:00")
        self.assertIsNotNone(age)
        self.assertGreater(age, 3650)

    def test_recall_never_rewrites_passages(self) -> None:
        before = evidence.search_evidence(
            "stable hash offsets", limit=5, settings=self.settings
        )["passages"][0]
        memory.recall("stable hash offsets", settings=self.settings)
        after = evidence.search_evidence(
            "stable hash offsets", limit=5, settings=self.settings
        )["passages"][0]
        self.assertEqual(before["passage_hash"], after["passage_hash"])
        self.assertEqual(before["text"], after["text"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Tests for the Phase 3 evidence library: migrations, persistence, identity
deduplication, the content cache, passages, claims, and citations."""

from __future__ import annotations

import json
import os
import sqlite3
import unittest

from agletras_mcp import evidence, storage
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import TempHomeCase, serve_http

PAGE_V1 = (
    b"<html><head><title>Widget Page</title></head><body>"
    b"<h1>Widgets</h1><p>Widgets are small mechanical components used in many devices.</p>"
    b"<p>Gears transmit rotational motion between shafts.</p></body></html>"
)
PAGE_V2 = (
    b"<html><head><title>Widget Page</title></head><body>"
    b"<h1>Widgets</h1><p>Widgets are manufactured in several standard sizes.</p>"
    b"<p>Gears transmit rotational motion between shafts.</p></body></html>"
)

NOTE_TEXT = (
    "# Findings\n"
    "Widgets are small mechanical components used in many devices.\n\n"
    "Gears transmit rotational motion between shafts and axles."
)


class EvidenceCase(TempHomeCase):
    """Shared setup: fresh settings resolved per test."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    # -- helpers -----------------------------------------------------------

    def save_note(self, text: str = NOTE_TEXT, title: str = "Findings note", run_id: int = 0):
        return evidence.save_evidence(
            text=text, title=title, run_id=run_id, settings=self.settings
        )

    def run_claim_citation(self, quote: str = "Widgets are small mechanical components"):
        run = evidence.start_run("How do widgets work?", settings=self.settings)
        saved = self.save_note(run_id=run["run_id"])
        found = evidence.search_evidence("widgets mechanical components", settings=self.settings)
        passage_id = found["passages"][0]["passage_id"]
        claim = evidence.register_claim(
            run["run_id"], "Widgets are small mechanical components.", settings=self.settings
        )
        citation = evidence.attach_citation(
            claim["claim_id"], passage_id, quote=quote, settings=self.settings
        )
        return run, saved, found, claim, citation

    def db_counts(self) -> dict[str, int]:
        with storage.open_db(self.settings) as conn:
            return {
                table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
                for table in (
                    "sources",
                    "source_contents",
                    "source_identities",
                    "passages",
                    "claims",
                    "citations",
                    "reports",
                    "research_runs",
                )
            }


class MigrationTests(EvidenceCase):
    def test_schema_version_recorded(self) -> None:
        stats_result = storage.stats(settings=self.settings)
        self.assertEqual(stats_result["schema_version"], storage.SCHEMA_VERSION)
        with storage.open_db(self.settings) as conn:
            versions = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations")]
        self.assertEqual(versions, list(range(1, storage.SCHEMA_VERSION + 1)))

    def test_evidence_tables_exist(self) -> None:
        with storage.open_db(self.settings) as conn:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertLessEqual(
            {
                "research_runs",
                "source_contents",
                "source_identities",
                "research_run_sources",
                "passages",
                "claims",
                "citations",
                "reports",
                "schema_migrations",
                # Phase 4: research planner
                "research_subquestions",
                "research_run_events",
                # Phase 6: claim verification
                "claim_verifications",
            },
            names,
        )

    def test_sources_gains_evidence_columns(self) -> None:
        with storage.open_db(self.settings) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)").fetchall()}
        self.assertLessEqual(
            {"canonical_url", "content_id", "raw_hash", "last_checked_at"}, columns
        )

    def test_foreign_keys_enforced_on_open(self) -> None:
        with storage.open_db(self.settings) as conn:
            self.assertEqual(int(conn.execute("PRAGMA foreign_keys").fetchone()[0]), 1)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO citations (claim_id, passage_id, source_id, relation, created_at) "
                    "VALUES (999, 999, 999, 'supports', 'now')"
                )

    def test_reopen_is_idempotent(self) -> None:
        before = storage.stats(settings=self.settings)
        for _ in range(3):
            with storage.open_db(self.settings) as conn:
                count = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]
                self.assertEqual(int(count), storage.SCHEMA_VERSION)
        self.assertEqual(storage.stats(settings=self.settings)["schema_version"], before["schema_version"])

    def test_interrupted_migration_retries_cleanly(self) -> None:
        """A database whose migration rows are missing is upgraded again."""
        with storage.open_db(self.settings) as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version = 2")
        with storage.open_db(self.settings) as conn:
            versions = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations")]
        self.assertEqual(versions, list(range(1, storage.SCHEMA_VERSION + 1)))
        # The Phase 4 migration is retried the same way (idempotent DDL).
        with storage.open_db(self.settings) as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version = 3")
        with storage.open_db(self.settings) as conn:
            versions = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations")]
            plan_column = {
                row[1] for row in conn.execute("PRAGMA table_info(research_runs)").fetchall()
            }
        self.assertEqual(versions, list(range(1, storage.SCHEMA_VERSION + 1)))
        self.assertIn("plan", plan_column)
        # The Phase 6 migration is retried the same way (idempotent DDL).
        with storage.open_db(self.settings) as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version = 4")
        with storage.open_db(self.settings) as conn:
            versions = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations")]
            verification_column = {
                row[1] for row in conn.execute("PRAGMA table_info(claims)").fetchall()
            }
        self.assertEqual(versions, list(range(1, storage.SCHEMA_VERSION + 1)))
        self.assertIn("verification_status", verification_column)


class ResearchRunTests(EvidenceCase):
    def test_start_get_finish_roundtrip(self) -> None:
        run = evidence.start_run("What are widgets?", config={"depth": 2}, settings=self.settings)
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["config"], {"depth": 2})
        self.assertIsNone(run["completed_at"])
        fetched = evidence.get_run(run["run_id"], settings=self.settings)
        assert fetched is not None
        self.assertEqual(fetched["query"], "What are widgets?")
        finished = evidence.finish_run(run["run_id"], settings=self.settings)
        assert finished is not None
        self.assertEqual(finished["status"], "completed")
        self.assertIsNotNone(finished["completed_at"])
        # Idempotent: finishing twice keeps the first completion time.
        again = evidence.finish_run(run["run_id"], status="failed", settings=self.settings)
        assert again is not None
        self.assertEqual(again["status"], "completed")

    def test_empty_query_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            evidence.start_run("   ", settings=self.settings)
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_unknown_run_errors(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            evidence.finish_run(4242, settings=self.settings)
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(ToolError):
            evidence.register_claim(4242, "x is y", settings=self.settings)

    def test_history_lists_runs_with_counts(self) -> None:
        first = evidence.start_run("first query", settings=self.settings)
        second = evidence.start_run("second query", settings=self.settings)
        evidence.save_evidence(
            text="Widgets are used in machines.", run_id=second["run_id"], settings=self.settings
        )
        evidence.register_claim(second["run_id"], "Widgets are used.", settings=self.settings)
        history = evidence.get_research_run_history(settings=self.settings)
        self.assertEqual(history["count"], 2)
        self.assertEqual(history["runs"][0]["run_id"], second["run_id"])
        self.assertEqual(history["runs"][0]["sources_count"], 1)
        self.assertEqual(history["runs"][0]["claims_count"], 1)
        self.assertEqual(history["runs"][0]["unverified_count"], 1)
        self.assertEqual(history["runs"][1]["run_id"], first["run_id"])

    def test_history_detail_traces_sources_claims_reports(self) -> None:
        run, saved, _found, claim, _citation = self.run_claim_citation()
        detail = evidence.get_research_run_history(run_id=run["run_id"], settings=self.settings)
        self.assertEqual(detail["run"]["run_id"], run["run_id"])
        self.assertEqual(detail["counts"]["sources"], 1)
        self.assertEqual(detail["counts"]["claims"], 1)
        self.assertEqual(detail["sources"][0]["source_id"], saved["source_id"])
        self.assertGreater(detail["sources"][0]["passages"], 0)
        self.assertEqual(detail["claims"][0]["status"], "supported")
        self.assertEqual(detail["claims_by_status"], {"supported": 1})
        self.assertEqual(detail["unverified_claims"], [])


class SaveEvidenceTests(EvidenceCase):
    def test_text_mode_saves_passages_and_identity(self) -> None:
        result = self.save_note()
        self.assertTrue(result["created_source"])
        self.assertEqual(result["source_type"], "note")
        self.assertEqual(result["identity_kind"], "note")
        self.assertTrue(result["identity"].startswith("note:"))
        self.assertGreaterEqual(result["passages_total"], 1)
        self.assertEqual(result["passages_saved"], result["passages_total"])
        self.assertFalse(result["cache_hit"])
        record = evidence.get_source_by_id(result["source_id"], settings=self.settings)
        assert record is not None
        self.assertEqual(record["evidence"]["passage_count"], result["passages_total"])
        self.assertIn("Widgets are small mechanical components", record["content"])
        self.assertEqual(record["sha256"], result["content_hash"])

    def test_exactly_one_mode_enforced(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            evidence.save_evidence(settings=self.settings)
        self.assertEqual(ctx.exception.code, "invalid_argument")
        with self.assertRaises(ToolError):
            evidence.save_evidence(text="a", source="https://example.com", settings=self.settings)

    def test_url_dedup_and_cache_hit(self) -> None:
        routes = {"/page": (200, "text/html; charset=utf-8", PAGE_V1)}
        with serve_http(routes) as base:
            first = evidence.save_evidence(source=base + "/page", settings=self.settings)
            second = evidence.save_evidence(source=base + "/page", settings=self.settings)
        self.assertTrue(first["created_source"])
        self.assertTrue(first["fetched"])
        self.assertFalse(first["cache_hit"])
        self.assertLessEqual(first["passages_total"], first["passages_saved"])
        self.assertFalse(second["created_source"])
        self.assertFalse(second["fetched"])
        self.assertTrue(second["cache_hit"])
        self.assertFalse(second["content_changed"])
        self.assertEqual(first["source_id"], second["source_id"])
        self.assertEqual(first["content_hash"], second["content_hash"])
        # The cache keeps serving after the origin is gone.
        third = evidence.save_evidence(source=base + "/page", settings=self.settings)
        self.assertTrue(third["cache_hit"])
        self.assertEqual(third["source_id"], first["source_id"])

    def test_tracking_parameters_share_one_identity(self) -> None:
        routes = {"/page": (200, "text/html; charset=utf-8", PAGE_V1)}
        with serve_http(routes) as base:
            plain = evidence.save_evidence(source=base + "/page", settings=self.settings)
            tracked = evidence.save_evidence(
                source=base + "/page?utm_source=newsletter&fbclid=abc", settings=self.settings
            )
        self.assertEqual(plain["source_id"], tracked["source_id"])
        self.assertTrue(tracked["cache_hit"])
        self.assertIn(base + "/page", tracked["identities"])

    def test_distinct_urls_same_content_keep_separate_provenance(self) -> None:
        routes = {
            "/a": (200, "text/html; charset=utf-8", PAGE_V1),
            "/b": (200, "text/html; charset=utf-8", PAGE_V1),
        }
        with serve_http(routes) as base:
            first = evidence.save_evidence(source=base + "/a", settings=self.settings)
            second = evidence.save_evidence(source=base + "/b", settings=self.settings)
        self.assertNotEqual(first["source_id"], second["source_id"])
        self.assertEqual(first["content_id"], second["content_id"])  # one shared blob
        self.assertEqual(first["content_hash"], second["content_hash"])
        record_a = evidence.get_source_by_id(first["source_id"], settings=self.settings)
        record_b = evidence.get_source_by_id(second["source_id"], settings=self.settings)
        assert record_a is not None and record_b is not None
        self.assertEqual(record_a["url"], base + "/a")
        self.assertEqual(record_b["url"], base + "/b")
        counts = self.db_counts()
        self.assertEqual(counts["sources"], 2)
        self.assertEqual(counts["source_contents"], 1)

    def test_local_file_cache_hit_and_modification(self) -> None:
        run = evidence.start_run("file change trace", settings=self.settings)
        target = self.home / "notes.md"
        target.write_text("# Topic\nAlpha text about widgets and gears.\n")
        first = evidence.save_evidence(
            source=str(target), run_id=run["run_id"], settings=self.settings
        )
        self.assertTrue(first["created_source"])
        self.assertEqual(first["identity_kind"], "path")
        second = evidence.save_evidence(source=str(target), settings=self.settings)
        self.assertTrue(second["cache_hit"])
        self.assertFalse(second["fetched"])
        # Attach a citation to the first revision.
        claim = evidence.register_claim(
            run["run_id"], "Alpha text talks about widgets.", settings=self.settings
        )
        found = evidence.search_evidence("widgets gears", settings=self.settings)
        passage_id = found["passages"][0]["passage_id"]
        evidence.attach_citation(claim["claim_id"], passage_id, settings=self.settings)
        # Change the file: the next save refetches, records the change, and
        # must not erase the earlier passage, citation, or run association.
        target.write_text("# Topic\nBeta text about widgets and sprockets, now longer.\n")
        changed = evidence.save_evidence(
            source=str(target), run_id=run["run_id"], settings=self.settings
        )
        self.assertFalse(changed["cache_hit"])
        self.assertTrue(changed["content_changed"])
        self.assertTrue(changed["fetched"])
        self.assertNotEqual(changed["content_hash"], first["content_hash"])
        self.assertEqual(changed["source_id"], first["source_id"])
        counts = self.db_counts()
        self.assertEqual(counts["source_contents"], 2)  # old + new blob
        self.assertGreaterEqual(counts["passages"], 2)  # old + new passages
        citations = evidence.get_citations_for_claim(claim["claim_id"], settings=self.settings)
        self.assertEqual(len(citations["citations"]), 1)
        self.assertEqual(citations["citations"][0]["passage"]["passage_id"], passage_id)
        detail = evidence.get_research_run_history(run_id=run["run_id"], settings=self.settings)
        self.assertEqual(detail["counts"]["sources"], 1)
        self.assertEqual(detail["sources"][0]["source_id"], first["source_id"])
        # Saving once more without changes hits the cache again.
        again = evidence.save_evidence(source=str(target), settings=self.settings)
        self.assertTrue(again["cache_hit"])
        self.assertFalse(again["content_changed"])

    def test_refresh_forces_refetch(self) -> None:
        routes = {"/page": (200, "text/html; charset=utf-8", PAGE_V1)}
        with serve_http(routes) as base:
            first = evidence.save_evidence(source=base + "/page", settings=self.settings)
            routes["/page"] = (200, "text/html; charset=utf-8", PAGE_V2)
            refreshed = evidence.save_evidence(
                source=base + "/page", refresh=True, settings=self.settings
            )
        self.assertEqual(refreshed["source_id"], first["source_id"])
        self.assertTrue(refreshed["content_changed"])
        self.assertTrue(refreshed["fetched"])
        self.assertFalse(refreshed["cache_hit"])

    def test_expired_cache_attempts_refresh_and_flags_stale(self) -> None:
        routes = {"/page": (200, "text/html; charset=utf-8", PAGE_V1)}
        with serve_http(routes) as base:
            first = evidence.save_evidence(source=base + "/page", settings=self.settings)
        os.environ["AGLETRAS_EVIDENCE_CACHE_TTL"] = "0"
        os.environ["AGLETRAS_COLLECT_MAX_RETRIES"] = "0"
        try:
            # The origin is gone now: the refresh fails, the stored copy survives.
            after = evidence.save_evidence(source=base + "/page")
        finally:
            os.environ.pop("AGLETRAS_EVIDENCE_CACHE_TTL", None)
            os.environ.pop("AGLETRAS_COLLECT_MAX_RETRIES", None)
        self.assertEqual(after["source_id"], first["source_id"])
        self.assertTrue(after["stale"])
        self.assertEqual(after["content_hash"], first["content_hash"])
        self.assertIn("refresh_error", after)
        self.assertTrue(any("refresh failed" in warning for warning in after["warnings"]))

    def test_failed_first_fetch_raises_and_stores_nothing(self) -> None:
        routes = {"/page": (500, "text/plain", b"server error")}
        os.environ["AGLETRAS_COLLECT_MAX_RETRIES"] = "0"
        try:
            with serve_http(routes) as base:
                with self.assertRaises(ToolError):
                    evidence.save_evidence(source=base + "/page")
        finally:
            os.environ.pop("AGLETRAS_COLLECT_MAX_RETRIES", None)
        self.assertEqual(storage.stats(settings=self.settings)["total"], 0)

    def test_promote_existing_phase2_source(self) -> None:
        source_id, created = storage.save_source(
            kind="note",
            title="Phase 2 note",
            content="Widgets are small mechanical components used in many devices.",
            settings=self.settings,
        )
        self.assertTrue(created)
        result = evidence.save_evidence(source_id=source_id, settings=self.settings)
        self.assertEqual(result["source_id"], source_id)
        self.assertFalse(result["created_source"])
        self.assertGreaterEqual(result["passages_total"], 1)
        record = evidence.get_source_by_id(source_id, settings=self.settings)
        assert record is not None
        self.assertEqual(record["evidence"]["passage_count"], result["passages_saved"])
        # Promoting the same stored source twice is a cache hit, no duplicates.
        again = evidence.save_evidence(source_id=source_id, settings=self.settings)
        self.assertTrue(again["cache_hit"])
        self.assertEqual(self.db_counts()["sources"], 1)

    def test_promote_source_without_text_is_rejected(self) -> None:
        source_id, _ = storage.save_source(
            kind="note", title="empty", content="placeholder", settings=self.settings
        )
        with storage.open_db(self.settings) as conn:
            conn.execute("UPDATE sources SET content = '' WHERE id = ?", (source_id,))
        with self.assertRaises(ToolError) as ctx:
            evidence.save_evidence(source_id=source_id, settings=self.settings)
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_unknown_source_id_is_not_found(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            evidence.save_evidence(source_id=999, settings=self.settings)
        self.assertEqual(ctx.exception.code, "not_found")

    def test_evidence_stored_in_raw_text_mode_with_url(self) -> None:
        result = evidence.save_evidence(
            text="Widgets are small mechanical components.",
            url="https://example.com/widgets",
            title="Widgets overview",
            settings=self.settings,
        )
        self.assertEqual(result["identity"], "https://example.com/widgets")
        self.assertEqual(result["identity_kind"], "url")
        self.assertEqual(result["url"], "https://example.com/widgets")
        record = evidence.get_source_by_id(result["source_id"], settings=self.settings)
        assert record is not None
        self.assertEqual(record["evidence"]["identities"], [{"identity": "https://example.com/widgets", "kind": "url"}])


class ClaimAndCitationTests(EvidenceCase):
    def test_claim_starts_unverified(self) -> None:
        run = evidence.start_run("claims", settings=self.settings)
        claim = evidence.register_claim(run["run_id"], "Widgets  are   useful.", settings=self.settings)
        self.assertEqual(claim["claim_text"], "Widgets are useful.")
        self.assertEqual(claim["status"], "unverified")
        again = evidence.register_claim(run["run_id"], "Widgets are useful.", settings=self.settings)
        self.assertFalse(again["created"])
        self.assertEqual(again["claim_id"], claim["claim_id"])

    def test_status_derivation(self) -> None:
        self.assertEqual(evidence.derive_claim_status([]), "unverified")
        self.assertEqual(evidence.derive_claim_status(["context"]), "unverified")
        self.assertEqual(evidence.derive_claim_status(["supports"]), "supported")
        self.assertEqual(
            evidence.derive_claim_status(["supports", "partially_supports"]), "partially_supported"
        )
        self.assertEqual(evidence.derive_claim_status(["partially_supports"]), "partially_supported")
        self.assertEqual(evidence.derive_claim_status(["supports", "contradicts"]), "contradicted")
        self.assertEqual(evidence.derive_claim_status(["supports", "needs_review"]), "needs_review")

    def test_attach_citation_requires_an_exact_passage(self) -> None:
        run = evidence.start_run("citations", settings=self.settings)
        claim = evidence.register_claim(run["run_id"], "Widgets exist.", settings=self.settings)
        with self.assertRaises(ToolError) as ctx:
            evidence.attach_citation(claim["claim_id"], 99999, settings=self.settings)
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertEqual(self.db_counts()["citations"], 0)

    def test_fabricated_quote_is_rejected(self) -> None:
        run = evidence.start_run("quotes", settings=self.settings)
        self.save_note()
        found = evidence.search_evidence("widgets mechanical", settings=self.settings)
        passage_id = found["passages"][0]["passage_id"]
        claim = evidence.register_claim(run["run_id"], "Widgets are cheap.", settings=self.settings)
        with self.assertRaises(ToolError) as ctx:
            evidence.attach_citation(
                claim["claim_id"],
                passage_id,
                quote="this sentence never appeared anywhere",
                settings=self.settings,
            )
        self.assertEqual(ctx.exception.code, "invalid_argument")
        self.assertEqual(self.db_counts()["citations"], 0)
        stored = evidence.get_claim(claim["claim_id"], settings=self.settings)
        assert stored is not None
        self.assertEqual(stored["status"], "unverified")

    def test_wrong_source_id_is_rejected(self) -> None:
        run = evidence.start_run("cross-check", settings=self.settings)
        self.save_note()
        found = evidence.search_evidence("widgets mechanical", settings=self.settings)
        passage_id = found["passages"][0]["passage_id"]
        other_id, _ = storage.save_source(
            kind="note", title="other", content="unrelated", settings=self.settings
        )
        claim = evidence.register_claim(run["run_id"], "Widgets exist.", settings=self.settings)
        with self.assertRaises(ToolError) as ctx:
            evidence.attach_citation(
                claim["claim_id"], passage_id, source_id=other_id, settings=self.settings
            )
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_invalid_relation_is_rejected(self) -> None:
        run = evidence.start_run("relations", settings=self.settings)
        claim = evidence.register_claim(run["run_id"], "Widgets exist.", settings=self.settings)
        with self.assertRaises(ToolError):
            evidence.attach_citation(claim["claim_id"], 1, relation="proves", settings=self.settings)

    def test_supported_claim_upgrades_status_with_quote(self) -> None:
        _run, _saved, found, claim, citation = self.run_claim_citation()
        self.assertEqual(citation["derived_status"], "supported")
        self.assertTrue(citation["citation"]["quote_matches_passage"])
        self.assertEqual(citation["claim"]["status"], "supported")
        record = evidence.get_citations_for_claim(claim["claim_id"], settings=self.settings)
        self.assertEqual(record["derived_status"], "supported")
        self.assertEqual(len(record["citations"]), 1)
        citation_payload = record["citations"][0]
        self.assertEqual(citation_payload["source"]["title"], "Findings note")
        self.assertGreater(citation_payload["passage"]["start_offset"], -1)
        self.assertEqual(
            record["citations"][0]["passage"]["passage_id"], found["passages"][0]["passage_id"]
        )

    def test_contradicted_and_context_only_claims(self) -> None:
        run = evidence.start_run("mixed", settings=self.settings)
        self.save_note()
        found = evidence.search_evidence("widgets mechanical components", settings=self.settings)
        passage_id = found["passages"][0]["passage_id"]
        contradicted = evidence.register_claim(
            run["run_id"], "Widgets do not exist.", settings=self.settings
        )
        result = evidence.attach_citation(
            contradicted["claim_id"], passage_id, relation="contradicts", settings=self.settings
        )
        self.assertEqual(result["derived_status"], "contradicted")
        context_only = evidence.register_claim(
            run["run_id"], "Widgets are mentioned somewhere.", settings=self.settings
        )
        result = evidence.attach_citation(
            context_only["claim_id"], passage_id, relation="context", settings=self.settings
        )
        self.assertEqual(result["derived_status"], "unverified")

    def test_quote_matches_ignores_whitespace_runs(self) -> None:
        self.assertTrue(evidence.quote_matches("alpha   beta", "some alpha\nbeta gamma"))
        self.assertFalse(evidence.quote_matches("alpha gamma", "some alpha beta gamma"))

    def test_quote_matches_equates_typographic_quote_glyphs(self) -> None:
        # Straight ASCII quote against curly passage (and the reverse alike).
        self.assertTrue(evidence.quote_matches("L'affluence varie", "L\u2019affluence varie"))
        self.assertTrue(evidence.quote_matches("L\u2019affluence varie", "L'affluence varie"))
        self.assertTrue(evidence.quote_matches('"quoted text"', "\u201cquoted text\u201d"))
        self.assertTrue(evidence.quote_matches("\u201cquoted text\u201d", '"quoted text"'))
        # Primes used as foot/inch marks match a plain apostrophe/quote.
        self.assertTrue(evidence.quote_matches("1,083'", "1,083\u2032"))
        # Everything else stays exact: a paraphrase is still refused.
        self.assertFalse(evidence.quote_matches("L'affluence double", "L\u2019affluence varie"))

    def test_unverified_listing(self) -> None:
        run = evidence.start_run("unverified listing", settings=self.settings)
        evidence.register_claim(run["run_id"], "Claim one.", settings=self.settings)
        evidence.register_claim(run["run_id"], "Claim two.", settings=self.settings)
        listing = evidence.get_unverified_claims(run_id=run["run_id"], settings=self.settings)
        self.assertEqual(listing["count"], 2)
        self.assertEqual({claim["status"] for claim in listing["claims"]}, {"unverified"})


class SearchTests(EvidenceCase):
    def test_extract_terms_drops_stopwords(self) -> None:
        terms = evidence.extract_terms("How do the widgets work with gears?")
        self.assertEqual(terms, ["widgets", "work", "gears"])

    def test_search_finds_passages_with_locators(self) -> None:
        self.save_note()
        result = evidence.search_evidence("gears rotational motion", settings=self.settings)
        self.assertIn(result["engine"], ("fts5", "like"))
        self.assertGreaterEqual(result["count"], 1)
        top = result["passages"][0]
        self.assertIn("Gears transmit rotational motion", top["text"])
        self.assertEqual(top["heading"], "Findings")
        self.assertEqual(top["locator"], "section: Findings")
        self.assertGreaterEqual(top["start_offset"], 0)
        self.assertLess(top["start_offset"], top["end_offset"])

    def test_search_prefers_higher_term_coverage(self) -> None:
        evidence.save_evidence(
            text="Alpha passage mentions gears once.",
            title="one", settings=self.settings,
        )
        evidence.save_evidence(
            text="Beta passage mentions gears, shafts, and axles together.",
            title="two", settings=self.settings,
        )
        result = evidence.search_evidence("gears shafts axles", settings=self.settings)
        self.assertIn("shafts", result["passages"][0]["text"])

    def test_like_fallback_when_fts_disabled(self) -> None:
        self.save_note()
        os.environ["AGLETRAS_EVIDENCE_FTS"] = "off"
        try:
            result = evidence.search_evidence("widgets mechanical")
        finally:
            os.environ.pop("AGLETRAS_EVIDENCE_FTS", None)
        self.assertEqual(result["engine"], "like")
        self.assertGreaterEqual(result["count"], 1)
        self.assertIn("Widgets are small mechanical components", result["passages"][0]["text"])

    def test_like_pattern_escapes_wildcards(self) -> None:
        # The LIKE fallback escapes %, _ and \ so search terms are literal.
        self.assertEqual(evidence._like_pattern("a%b_c\\d"), "%a\\%b\\_c\\\\d%")

    def test_like_fallback_finds_literal_percent_text(self) -> None:
        evidence.save_evidence(
            text="Discount reached 50% during the sale.", title="percent", settings=self.settings
        )
        evidence.save_evidence(text="Nothing special here.", title="plain", settings=self.settings)
        os.environ["AGLETRAS_EVIDENCE_FTS"] = "off"
        try:
            result = evidence.search_evidence("50")
        finally:
            os.environ.pop("AGLETRAS_EVIDENCE_FTS", None)
        self.assertEqual(result["engine"], "like")
        self.assertEqual(result["count"], 1)
        self.assertIn("50%", result["passages"][0]["text"])

    def test_run_filter_limits_results(self) -> None:
        first_run = evidence.start_run("first", settings=self.settings)
        second_run = evidence.start_run("second", settings=self.settings)
        evidence.save_evidence(
            text="Alpha widgets belong to the first run.", run_id=first_run["run_id"], settings=self.settings
        )
        evidence.save_evidence(
            text="Beta widgets belong to the second run.", run_id=second_run["run_id"], settings=self.settings
        )
        result = evidence.search_evidence(
            "widgets", run_id=first_run["run_id"], settings=self.settings
        )
        self.assertEqual(result["count"], 1)
        self.assertIn("first run", result["passages"][0]["text"])

    def test_empty_query_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            evidence.search_evidence("the and of", settings=self.settings)
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_retrieve_passages_for_claim(self) -> None:
        run = evidence.start_run("retrieval", settings=self.settings)
        self.save_note(run_id=run["run_id"])
        claim = evidence.register_claim(
            run["run_id"], "Gears transmit rotational motion.", settings=self.settings
        )
        result = evidence.retrieve_passages_for_claim(claim_id=claim["claim_id"], settings=self.settings)
        self.assertEqual(result["claim_id"], claim["claim_id"])
        self.assertGreaterEqual(result["count"], 1)
        self.assertIn("Gears", result["passages"][0]["text"])
        with self.assertRaises(ToolError) as ctx:
            evidence.retrieve_passages_for_claim(claim_id=999, settings=self.settings)
        self.assertEqual(ctx.exception.code, "not_found")


class IntegrityTests(EvidenceCase):
    def test_rollback_on_failed_write(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            with storage.open_db(self.settings) as conn:
                conn.execute(
                    """
                    INSERT INTO claims (run_id, claim_text, status, created_at, updated_at)
                    VALUES (999, 'orphan claim', 'unverified', 'now', 'now')
                    """
                )
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(self.db_counts()["claims"], 0)

    def test_source_with_citations_cannot_be_deleted(self) -> None:
        _run, saved, _found, _claim, _citation = self.run_claim_citation()
        with self.assertRaises(ToolError) as ctx:
            with storage.open_db(self.settings) as conn:
                conn.execute("DELETE FROM sources WHERE id = ?", (saved["source_id"],))
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(self.db_counts()["sources"], 1)

    def test_claim_text_limit(self) -> None:
        run = evidence.start_run("limit", settings=self.settings)
        with self.assertRaises(ToolError) as ctx:
            evidence.register_claim(run["run_id"], "x" * 3000, settings=self.settings)
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_metadata_json_roundtrip_on_citation(self) -> None:
        run = evidence.start_run("metadata", settings=self.settings)
        self.save_note()
        found = evidence.search_evidence("widgets mechanical", settings=self.settings)
        passage_id = found["passages"][0]["passage_id"]
        claim = evidence.register_claim(run["run_id"], "Widgets exist.", settings=self.settings)
        citation = evidence.attach_citation(
            claim["claim_id"],
            passage_id,
            metadata={"checked_by": "tester", "revision": 3},
            settings=self.settings,
        )
        self.assertEqual(
            citation["citation"]["metadata"], {"checked_by": "tester", "revision": 3}
        )

    def test_source_contents_blob_matches_record(self) -> None:
        self.save_note()
        with storage.open_db(self.settings) as conn:
            row = conn.execute("SELECT content_hash, extracted_text FROM source_contents").fetchone()
            self.assertEqual(storage.content_hash(row["extracted_text"]), row["content_hash"])
        record = evidence.get_source_by_id(1, settings=self.settings)
        assert record is not None
        self.assertEqual(record["evidence"]["content"]["content_hash"], record["sha256"])


class ReportWorkflowTests(EvidenceCase):
    def test_generate_and_validate_report_with_unverified_claim(self) -> None:
        run = evidence.start_run("Widget report", settings=self.settings)
        saved = self.save_note(run_id=run["run_id"])
        found = evidence.search_evidence("widgets mechanical components", settings=self.settings)
        passage_id = found["passages"][0]["passage_id"]
        supported = evidence.register_claim(
            run["run_id"], "Widgets are small mechanical components.", settings=self.settings
        )
        evidence.attach_citation(
            supported["claim_id"], passage_id, quote="small mechanical components", settings=self.settings
        )
        unverified = evidence.register_claim(
            run["run_id"], "Widgets were invented in 1842.", settings=self.settings
        )
        report = evidence.generate_report(run["run_id"], settings=self.settings)
        self.assertEqual(report["citation_validation_status"], "validated")
        self.assertTrue(report["validation"]["passed"])
        self.assertEqual(report["validation"]["validation_kind"], "structural")
        self.assertEqual(report["validation"]["semantic_support"], "not_evaluated")
        self.assertIn("UNVERIFIED", report["markdown"])
        self.assertIn("[S1]", report["markdown"])
        self.assertEqual(set(report["citation_map"]), {"S1"})
        self.assertEqual(
            report["validation"]["unverified_claims"], [unverified["claim_id"]]
        )
        # The run is completed once its report is generated.
        finished = evidence.get_run(run["run_id"], settings=self.settings)
        assert finished is not None
        self.assertEqual(finished["status"], "completed")
        # Re-validating an unchanged report passes again.
        again = evidence.validate_report_citations(report["report_id"], settings=self.settings)
        self.assertTrue(again["passed"])
        self.assertEqual(again["status"], "validated")

    def test_tampered_passage_fails_validation(self) -> None:
        run, saved, found, claim, _citation = self.run_claim_citation()
        report = evidence.generate_report(run["run_id"], settings=self.settings)
        self.assertTrue(report["validation"]["passed"])
        passage_id = found["passages"][0]["passage_id"]
        with storage.open_db(self.settings) as conn:
            conn.execute(
                "UPDATE passages SET text = 'rewritten passage text' WHERE id = ?", (passage_id,)
            )
        revalidated = evidence.validate_report_citations(report["report_id"], settings=self.settings)
        self.assertFalse(revalidated["passed"])
        codes = {error["code"] for error in revalidated["errors"]}
        self.assertIn("passage_hash_mismatch", codes)
        self.assertIn("passage_offset_mismatch", codes)
        stored = evidence.get_report(report["report_id"], settings=self.settings)
        assert stored is not None
        self.assertEqual(stored["citation_validation_status"], "failed")

    def test_status_tampering_is_caught(self) -> None:
        run, saved, found, claim, _citation = self.run_claim_citation()
        report = evidence.generate_report(run["run_id"], settings=self.settings)
        with storage.open_db(self.settings) as conn:
            conn.execute(
                "UPDATE claims SET status = 'contradicted' WHERE id = ?", (claim["claim_id"],)
            )
        revalidated = evidence.validate_report_citations(report["report_id"], settings=self.settings)
        self.assertFalse(revalidated["passed"])
        codes = {error["code"] for error in revalidated["errors"]}
        self.assertIn("claim_status_inconsistent", codes)

    def test_report_of_unknown_run_errors(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            evidence.generate_report(999, settings=self.settings)
        self.assertEqual(ctx.exception.code, "not_found")

    def test_report_without_claims_still_validates(self) -> None:
        run = evidence.start_run("empty run", settings=self.settings)
        report = evidence.generate_report(run["run_id"], settings=self.settings)
        self.assertTrue(report["validation"]["passed"])
        self.assertIn("No claims are registered", report["markdown"])

    def test_citation_linked_to_claim_of_another_run_is_caught(self) -> None:
        first_run = evidence.start_run("first run", settings=self.settings)
        second_run = evidence.start_run("second run", settings=self.settings)
        self.save_note(run_id=first_run["run_id"])
        found = evidence.search_evidence("widgets mechanical", settings=self.settings)
        passage_id = found["passages"][0]["passage_id"]
        other_claim = evidence.register_claim(
            first_run["run_id"], "Widget from another run.", settings=self.settings
        )
        citation = evidence.attach_citation(
            other_claim["claim_id"], passage_id, settings=self.settings
        )
        report = evidence.generate_report(second_run["run_id"], settings=self.settings)
        # Smuggle the other run's citation into this report's map: the
        # structural validator must reject it (the report belongs to another
        # run), proving [Sn] never resolves to unrelated evidence.
        with storage.open_db(self.settings) as conn:
            conn.execute(
                "UPDATE reports SET citation_map = ? WHERE id = ?",
                (json.dumps({"S1": citation["citation"]["citation_id"]}), report["report_id"]),
            )
        revalidated = evidence.validate_report_citations(report["report_id"], settings=self.settings)
        self.assertFalse(revalidated["passed"])
        codes = {error["code"] for error in revalidated["errors"]}
        self.assertIn("citation_wrong_run", codes)


if __name__ == "__main__":
    unittest.main()

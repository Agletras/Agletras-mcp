"""Phase 6 tests: claim extraction, evidence mapping, verification statuses,
model validation/fallback, source quality, and conflict analysis.

All tests are hermetic: temporary ``AGLETRAS_HOME``, local HTTP fixtures only,
no external network.
"""

from __future__ import annotations

import json
import os
import unittest

from agletras_mcp import evidence, planner, storage, verification
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import TempHomeCase, serve_http

NOTE_TEXT = (
    "Widgets are small mechanical components used in many devices. "
    "Standard widgets are manufactured in several sizes. "
    "The average widget weighs 250 grams according to the manufacturer survey."
)

VERBATIM_CLAIM = "Widgets are small mechanical components used in many devices."
OVERCLAIM = "All widgets weigh 250 grams."
NUMBER_CONFLICT = "The average widget weighs 300 grams according to the manufacturer survey."
UNSUPPORTED_CLAIM = "Widgets are assembled in Europe by hand."
UNVERIFIABLE_CLAIM = "Quantum entanglement teleports energy between galaxies."


def make_candidate(
    claim_text: str,
    passage_text: str,
    passage_id: int,
    *,
    source_id: int = 1,
    group: str = "origin:example.org",
) -> dict:
    """Candidate payload shaped like verification._gather_candidates output."""
    return {
        "kind": "test",
        "passage_id": passage_id,
        "source_id": source_id,
        "source_title": "test source",
        "source_url": None,
        "source_path": None,
        "source_type": None,
        "publication_date": None,
        "retrieved_at": None,
        "content_hash": None,
        "locator": "",
        "text": passage_text,
        "independence_group": group,
        "independence_note": "",
        "from_citation": False,
        "citation_id": None,
        "relation": None,
        "quote": "",
        "checks": verification.check_claim_against_passage(claim_text, passage_text),
    }


class Phase6Case(TempHomeCase):
    """Shared setup with a run, one text source, and settings."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def new_run(self, query: str = "How are widgets made?") -> dict:
        return evidence.start_run(query, settings=self.settings)

    def save_text(
        self,
        run_id: int,
        text: str = NOTE_TEXT,
        *,
        title: str = "Widget findings",
        url: str = "https://example.org/widgets",
    ) -> dict:
        return evidence.save_evidence(
            text=text, title=title, url=url, run_id=run_id, settings=self.settings
        )


# ---------------------------------------------------------------------------
# Storage / migration
# ---------------------------------------------------------------------------


class Phase6MigrationTests(Phase6Case):
    def test_migration_adds_columns_and_table(self) -> None:
        with storage.open_db(self.settings) as conn:
            claim_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(claims)").fetchall()
            }
            report_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(reports)").fetchall()
            }
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertLessEqual(
            {"claim_type", "note", "verification_status", "verified_at"}, claim_columns
        )
        self.assertLessEqual(
            {"template_version", "finding_map", "image_map", "generation_config"},
            report_columns,
        )
        self.assertIn("claim_verifications", tables)

    def test_claim_types_and_notes_roundtrip(self) -> None:
        run = self.new_run()
        claim = evidence.register_claim(
            run["run_id"],
            "The trend is probably slowing.",
            claim_type="inference",
            note="Derived from the two annual measurements.",
            settings=self.settings,
        )
        self.assertEqual(claim["claim_type"], "inference")
        self.assertEqual(claim["note"], "Derived from the two annual measurements.")
        self.assertEqual(claim["status"], "unverified")
        self.assertEqual(claim["verification_status"], "")
        with self.assertRaises(ToolError):
            evidence.register_claim(
                run["run_id"], "Bad type.", claim_type="opinion", settings=self.settings
            )

    def test_verification_audit_rows_and_history(self) -> None:
        run = self.new_run()
        self.save_text(run["run_id"])
        evidence.register_claim(run["run_id"], VERBATIM_CLAIM, settings=self.settings)
        result = verification.verify_claims(run["run_id"], settings=self.settings)
        self.assertEqual(result["counts_by_status"].get("supported"), 1)
        with storage.open_db(self.settings) as conn:
            rows = conn.execute("SELECT * FROM claim_verifications").fetchall()
            claim_row = conn.execute("SELECT * FROM claims").fetchone()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "supported")
        self.assertEqual(rows[0]["method"], "source_text_comparison")
        self.assertEqual(claim_row["verification_status"], "supported")
        self.assertIsNotNone(claim_row["verified_at"])
        # Re-running verification appends an audit row and updates the cache.
        verification.verify_claims(run["run_id"], settings=self.settings)
        with storage.open_db(self.settings) as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM claim_verifications").fetchone()["n"]
        self.assertEqual(int(count), 2)
        history = evidence.get_research_run_history(run_id=run["run_id"], settings=self.settings)
        self.assertEqual(history["claims_by_verification_status"].get("supported"), 1)
        self.assertEqual(history["verifications_count"], 2)


# ---------------------------------------------------------------------------
# Extraction (pure + run-scoped)
# ---------------------------------------------------------------------------


class ExtractionTests(Phase6Case):
    def test_split_sentences_and_question_filter(self) -> None:
        text = "Widgets are useful. Are they? Yes, widgets are small."
        self.assertEqual(
            verification.split_sentences(text),
            ["Widgets are useful.", "Are they?", "Yes, widgets are small."],
        )
        candidates = verification.extract_candidates(text, max_claims=10)
        texts = [item["text"] for item in candidates]
        self.assertNotIn("Are they?", texts)

    def test_candidate_markers_and_stability(self) -> None:
        text = (
            "The survey found that 42 percent of users prefer widgets in 2024. "
            "Widgets are likely used by most teams."
        )
        first = verification.extract_candidates(text, max_claims=5)
        second = verification.extract_candidates(text, max_claims=5)
        self.assertEqual(first, second)  # deterministic ids and ordering
        self.assertEqual(first[0]["candidate_id"], "C1")
        self.assertIn("number", first[0]["markers"])
        self.assertIn("date", first[0]["markers"])

    def test_extract_claims_maps_evidence_and_respects_register(self) -> None:
        run = self.new_run()
        self.save_text(run["run_id"])
        result = verification.extract_claims(
            run["run_id"], register=False, max_claims=10, settings=self.settings
        )
        self.assertGreaterEqual(result["candidate_count"], 1)
        self.assertEqual(result["registered"], [])
        with storage.open_db(self.settings) as conn:
            claims = conn.execute("SELECT COUNT(*) AS n FROM claims").fetchone()["n"]
        self.assertEqual(int(claims), 0)
        top = result["candidates"][0]
        self.assertIn("evidence_matches", top)
        self.assertTrue(top["evidence_matches"])  # mapped to stored passages
        self.assertLessEqual(top["evidence_matches"][0]["coverage"], 1.0)
        self.assertEqual(result["extraction_method"], "deterministic_sentence_marker_scoring")

        registered = verification.extract_claims(
            run["run_id"], register=True, max_claims=3, claim_type="finding", settings=self.settings
        )
        self.assertEqual(len(registered["registered"]), len(registered["candidates"]))
        with storage.open_db(self.settings) as conn:
            rows = conn.execute("SELECT * FROM claims").fetchall()
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["status"], "unverified")
            self.assertEqual(row["claim_type"], "finding")
            self.assertEqual(row["verification_status"], "")

    def test_extract_rejects_unlinked_source_and_bad_type(self) -> None:
        run = self.new_run()
        other = evidence.save_evidence(
            text=NOTE_TEXT, title="unlinked", settings=self.settings
        )
        with self.assertRaises(ToolError) as ctx:
            verification.extract_claims(
                run["run_id"], source_ids=[other["source_id"]], settings=self.settings
            )
        self.assertEqual(ctx.exception.code, "invalid_argument")
        with self.assertRaises(ToolError) as ctx:
            verification.extract_claims(
                run["run_id"], text=NOTE_TEXT, claim_type="rumour", settings=self.settings
            )
        self.assertEqual(ctx.exception.code, "invalid_argument")
        with self.assertRaises(ToolError) as ctx:
            verification.extract_claims(
                run["run_id"], source_ids=[999], settings=self.settings
            )
        self.assertEqual(ctx.exception.code, "not_found")


# ---------------------------------------------------------------------------
# Deterministic comparison and status deciding (pure)
# ---------------------------------------------------------------------------


class CheckAndDecideTests(unittest.TestCase):
    def test_verbatim_check_has_full_coverage(self) -> None:
        checks = verification.check_claim_against_passage(VERBATIM_CLAIM, NOTE_TEXT)
        self.assertEqual(checks["coverage"], 1.0)
        self.assertTrue(checks["verbatim_claim"])
        self.assertEqual(checks["flags"], [])
        self.assertEqual(checks["conflict_signals"], [])

    def test_number_conflict_detected_with_context(self) -> None:
        checks = verification.check_claim_against_passage(NUMBER_CONFLICT, NOTE_TEXT)
        self.assertIn("numbers_disagree", checks["conflict_signals"])
        self.assertIn("claim_numbers_missing_from_passage", checks["flags"])
        self.assertIn("300", checks["numbers_missing"])

    def test_unrelated_number_is_not_a_conflict(self) -> None:
        passage = "The factory opened in 1998 and now employs 84 people."
        checks = verification.check_claim_against_passage(
            "The bridge is 1450 meters long.", passage
        )
        self.assertNotIn("numbers_disagree", checks["conflict_signals"])

    def test_overgeneralization_and_polarity_flags(self) -> None:
        universal = verification.check_claim_against_passage(OVERCLAIM, NOTE_TEXT)
        self.assertTrue(universal["possible_overgeneralization"])
        self.assertIn("possible_overgeneralization", universal["flags"])
        negated = verification.check_claim_against_passage(
            "Widgets are not small mechanical components.", NOTE_TEXT
        )
        self.assertTrue(negated["polarity_mismatch"])

    def test_decide_unverifiable_without_candidates(self) -> None:
        result = verification.decide_verification("Anything at all.", [], [])
        self.assertEqual(result["status"], "unverifiable")

    def test_decide_unverifiable_when_irrelevant(self) -> None:
        candidate = make_candidate(UNVERIFIABLE_CLAIM, NOTE_TEXT, 1)
        result = verification.decide_verification(UNVERIFIABLE_CLAIM, [], [candidate])
        self.assertEqual(result["status"], "unverifiable")

    def test_decide_supported_verbatim(self) -> None:
        candidate = make_candidate(VERBATIM_CLAIM, VERBATIM_CLAIM, 1)
        result = verification.decide_verification(VERBATIM_CLAIM, [], [candidate])
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["confidence"], "high")

    def test_decide_partially_supported_overgeneralization(self) -> None:
        candidate = make_candidate(OVERCLAIM, NOTE_TEXT, 1)
        result = verification.decide_verification(OVERCLAIM, [], [candidate])
        self.assertEqual(result["status"], "partially_supported")
        self.assertTrue(result["limitations"])

    def test_decide_contradicted_number_conflict(self) -> None:
        candidate = make_candidate(NUMBER_CONFLICT, NOTE_TEXT, 1)
        result = verification.decide_verification(NUMBER_CONFLICT, [], [candidate])
        self.assertEqual(result["status"], "contradicted")

    def test_decide_unsupported_weak_coverage_without_flags(self) -> None:
        candidate = make_candidate(UNSUPPORTED_CLAIM, NOTE_TEXT, 1)
        result = verification.decide_verification(UNSUPPORTED_CLAIM, [], [candidate])
        self.assertEqual(result["status"], "unsupported")

    def test_decide_citation_contradiction_wins(self) -> None:
        citations = [
            {"relation": "contradicts", "source_id": 2, "independence_group": "origin:b.example"}
        ]
        candidate = make_candidate(VERBATIM_CLAIM, VERBATIM_CLAIM, 1)
        result = verification.decide_verification(VERBATIM_CLAIM, citations, [candidate])
        self.assertEqual(result["status"], "contradicted")

    def test_decide_conflicting_evidence_with_independent_support(self) -> None:
        citations = [
            {"relation": "contradicts", "source_id": 2, "independence_group": "origin:b.example"},
            {"relation": "supports", "source_id": 1, "independence_group": "origin:a.example"},
        ]
        result = verification.decide_verification(VERBATIM_CLAIM, citations, [])
        self.assertEqual(result["status"], "conflicting_evidence")
        self.assertTrue(result["limitations"])

    def test_decide_conflicting_evidence_between_passages(self) -> None:
        supporting = make_candidate(VERBATIM_CLAIM, VERBATIM_CLAIM, 1, group="origin:a.example")
        conflicting = make_candidate(
            NUMBER_CONFLICT,
            "The average widget weighs 300 grams according to the manufacturer survey.",
            2,
            source_id=2,
            group="origin:b.example",
        )
        result = verification.decide_verification(VERBATIM_CLAIM, [], [supporting, conflicting])
        # The claim itself is verbatim-supportable here; conflict only fires when
        # the *best* candidate disagrees. With an unrelated second passage the
        # support stands.
        self.assertEqual(result["status"], "supported")

        weakened = "The average widget weighs 300 grams in Europe."
        weak_support = make_candidate(weakened, "The average widget weighs 300 grams in Europe.", 3,
                                      group="origin:a.example")
        disagree = make_candidate(weakened, NOTE_TEXT, 4, source_id=2, group="origin:b.example")
        outcome = verification.decide_verification(weakened, [], [weak_support, disagree])
        self.assertIn(outcome["status"], ("conflicting_evidence", "partially_supported"))


# ---------------------------------------------------------------------------
# Model parsing and capability (no network)
# ---------------------------------------------------------------------------


class ModelParsingTests(Phase6Case):
    def test_capability_default_off(self) -> None:
        capability = verification.verification_capability(self.settings)
        self.assertFalse(capability["model_available"])
        self.assertIsNone(capability["model"])
        self.assertIn("AGLETRAS_VERIFICATION_MODEL_ENDPOINT", capability["reason"])

    def test_capability_on_when_configured(self) -> None:
        os.environ["AGLETRAS_VERIFICATION_MODEL_ENDPOINT"] = "http://127.0.0.1:9/verify"
        os.environ["AGLETRAS_VERIFICATION_MODEL"] = "test-model"
        capability = verification.verification_capability(load_settings())
        self.assertTrue(capability["model_available"])
        self.assertEqual(capability["model"], "test-model")

    def test_parse_valid_response(self) -> None:
        parsed = verification.parse_model_verification_response(
            json.dumps(
                {
                    "status": "partially_supported",
                    "rationale": "Only the size claim is stated.",
                    "evidence": [3],
                    "confidence": "low",
                }
            ),
            {3, 4},
        )
        self.assertEqual(parsed["status"], "partially_supported")
        self.assertEqual(parsed["evidence_passage_ids"], [3])
        self.assertEqual(parsed["confidence"], "low")

    def test_parse_rejects_unstructured_or_uncited(self) -> None:
        cases = [
            "not json at all",
            {"status": "maybe", "rationale": "x", "evidence": [1]},
            {"status": "supported", "rationale": "", "evidence": [1]},
            {"status": "supported", "rationale": "x", "evidence": []},
            {"status": "supported", "rationale": "x", "evidence": [999]},
        ]
        for payload in cases:
            with self.assertRaises(ToolError) as ctx:
                verification.parse_model_verification_response(payload, {1, 2})
            self.assertEqual(ctx.exception.code, "provider_error")


# ---------------------------------------------------------------------------
# Run-scoped verification (database + local fixtures)
# ---------------------------------------------------------------------------


class VerifyClaimsTests(Phase6Case):
    def test_supported_attaches_quote_verified_citation(self) -> None:
        run = self.new_run()
        self.save_text(run["run_id"])
        claim = evidence.register_claim(run["run_id"], VERBATIM_CLAIM, settings=self.settings)
        result = verification.verify_claims(run["run_id"], settings=self.settings)
        entry = result["claims"][0]
        self.assertEqual(entry["verification"]["status"], "supported")
        self.assertEqual(entry["citation_relation"], "supports")
        details = evidence.get_citations_for_claim(claim["claim_id"], settings=self.settings)
        self.assertEqual(len(details["citations"]), 1)
        citation = details["citations"][0]
        self.assertEqual(citation["relation"], "supports")
        self.assertIn(VERBATIM_CLAIM, citation["quote"])
        metadata = citation.get("metadata") or {}
        self.assertEqual(metadata.get("verified_by"), "agletras_phase6")

    def test_contradicted_never_auto_attached_and_rerun_is_stable(self) -> None:
        run = self.new_run()
        self.save_text(run["run_id"])
        claim = evidence.register_claim(run["run_id"], NUMBER_CONFLICT, settings=self.settings)
        result = verification.verify_claims(run["run_id"], settings=self.settings)
        self.assertEqual(result["counts_by_status"].get("contradicted"), 1)
        details = evidence.get_citations_for_claim(claim["claim_id"], settings=self.settings)
        self.assertEqual(details["citations"], [])
        # A second verification run must not attach anything either.
        verification.verify_claims(run["run_id"], settings=self.settings)
        details = evidence.get_citations_for_claim(claim["claim_id"], settings=self.settings)
        self.assertEqual(details["citations"], [])

    def test_claim_ids_subset_and_unknown_ids(self) -> None:
        run = self.new_run()
        self.save_text(run["run_id"])
        first = evidence.register_claim(run["run_id"], VERBATIM_CLAIM, settings=self.settings)
        evidence.register_claim(run["run_id"], UNVERIFIABLE_CLAIM, settings=self.settings)
        result = verification.verify_claims(
            run["run_id"], claim_ids=[first["claim_id"]], settings=self.settings
        )
        self.assertEqual(result["verified_count"], 1)
        self.assertEqual(result["claims"][0]["claim_id"], first["claim_id"])
        with self.assertRaises(ToolError) as ctx:
            verification.verify_claims(run["run_id"], claim_ids=[999], settings=self.settings)
        self.assertEqual(ctx.exception.code, "not_found")

    def test_invalid_method_rejected(self) -> None:
        run = self.new_run()
        with self.assertRaises(ToolError) as ctx:
            verification.verify_claims(run["run_id"], method="psychic", settings=self.settings)
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_deterministic_only_records_transparent_method(self) -> None:
        run = self.new_run()
        self.save_text(run["run_id"])
        evidence.register_claim(run["run_id"], VERBATIM_CLAIM, settings=self.settings)
        result = verification.verify_claims(
            run["run_id"], method="deterministic", settings=self.settings
        )
        entry = result["claims"][0]
        self.assertEqual(entry["verification"]["method"], "source_text_comparison")
        self.assertFalse(result["model_available"])
        self.assertTrue(result["deterministic_only"])
        self.assertIn("not proof", result["note"])

    def test_model_failure_falls_back_with_recorded_reason(self) -> None:
        routes = {"/verify": (500, "application/json", b"{}")}
        with serve_http(routes) as base:
            os.environ["AGLETRAS_VERIFICATION_MODEL_ENDPOINT"] = base + "/verify"
            settings = load_settings()
            run = self.new_run()
            self.save_text(run["run_id"])
            evidence.register_claim(run["run_id"], VERBATIM_CLAIM, settings=settings)
            result = verification.verify_claims(run["run_id"], settings=settings)
        entry = result["claims"][0]
        self.assertEqual(entry["verification"]["method"], "model_fallback_deterministic")
        model_block = entry["verification"]["checks"]["model"]
        self.assertTrue(model_block["attempted"])
        self.assertFalse(model_block["available"])
        self.assertTrue(any("model-based verification unavailable" in item
                            for item in entry["verification"]["limitations"]))
        # The deterministic assessment still stands and is what was reported.
        self.assertEqual(entry["verification"]["checks"]["deterministic_assessment"]["status"],
                         entry["verification"]["status"])

    def test_valid_model_response_is_used_with_stored_passage_ids(self) -> None:
        run = self.new_run()
        self.save_text(run["run_id"])
        claim = evidence.register_claim(run["run_id"], OVERCLAIM, settings=self.settings)
        found = evidence.search_evidence(OVERCLAIM, run_id=run["run_id"], settings=self.settings)
        passage_id = found["passages"][0]["passage_id"]
        payload = {
            "status": "supported",
            "rationale": "The passage establishes the intended meaning.",
            "evidence": [passage_id],
            "confidence": "medium",
        }
        routes = {"/verify": (200, "application/json", json.dumps(payload).encode("utf-8"))}
        with serve_http(routes) as base:
            os.environ["AGLETRAS_VERIFICATION_MODEL_ENDPOINT"] = base + "/verify"
            os.environ["AGLETRAS_VERIFICATION_MODEL"] = "stub-model"
            settings = load_settings()
            result = verification.verify_claims(run["run_id"], settings=settings)
        entry = result["claims"][0]
        self.assertEqual(entry["verification"]["method"], "model")
        self.assertEqual(entry["verification"]["status"], "supported")
        self.assertEqual(entry["verification"]["checks"]["model"]["assessment"]["evidence_passage_ids"],
                         [passage_id])
        self.assertEqual(result["methods_used"].get("model"), 1)

    def test_invalid_model_response_is_rejected_and_falls_back(self) -> None:
        run = self.new_run()
        self.save_text(run["run_id"])
        evidence.register_claim(run["run_id"], OVERCLAIM, settings=self.settings)
        payload = {"status": "supported", "rationale": "trust me", "evidence": [99999]}
        routes = {"/verify": (200, "application/json", json.dumps(payload).encode("utf-8"))}
        with serve_http(routes) as base:
            os.environ["AGLETRAS_VERIFICATION_MODEL_ENDPOINT"] = base + "/verify"
            settings = load_settings()
            result = verification.verify_claims(run["run_id"], settings=settings)
        entry = result["claims"][0]
        self.assertEqual(entry["verification"]["method"], "model_fallback_deterministic")
        error = entry["verification"]["checks"]["model"]["error"]
        self.assertEqual(error["code"], "provider_error")


# ---------------------------------------------------------------------------
# Source quality and conflicts
# ---------------------------------------------------------------------------


class SourceQualityTests(Phase6Case):
    def test_duplicate_content_and_cautions(self) -> None:
        run = self.new_run()
        self.save_text(run["run_id"], url="https://a.example/widgets")
        self.save_text(run["run_id"], url="https://a.example/widgets-copy")
        assessment = verification.assess_sources(run["run_id"], settings=self.settings)
        self.assertEqual(assessment["source_count"], 2)
        self.assertEqual(len(assessment["duplicate_content_groups"]), 1)
        self.assertTrue(any("repeats the same content" in item for item in assessment["cautions"]))
        # Same-file copies are never independent corroboration.
        groups = assessment["shared_independence_groups"]
        self.assertTrue(groups)
        self.assertTrue(any(group["count"] == 2 for group in groups))
        self.assertIn("never treated as truth", assessment["note"])

    def test_quality_fields_and_relevance(self) -> None:
        run = self.new_run("widget mechanical components")
        self.save_text(run["run_id"])
        assessment = verification.assess_sources(run["run_id"], settings=self.settings)
        source = assessment["sources"][0]
        self.assertGreater(source["relevance_to_run_query"], 0.5)
        self.assertIn(source["directness"], ("primary_document", "likely_primary",
                                             "likely_secondary", "snippet", "unknown"))
        self.assertIn("potentially_stale", source)


class ConflictAnalysisTests(Phase6Case):
    def test_citation_relation_conflict_is_independent_and_unresolved(self) -> None:
        run = self.new_run()
        first = self.save_text(
            run["run_id"], url="https://a.example/widgets",
            text="The bridge is 1200 meters long.",
        )
        second = self.save_text(
            run["run_id"], url="https://b.example/bridges",
            text="The bridge is 1450 meters long.",
        )
        claim = evidence.register_claim(
            run["run_id"], "The bridge is 1200 meters long.", settings=self.settings
        )
        passage_a = evidence.search_evidence(
            "1200", run_id=run["run_id"], settings=self.settings
        )["passages"][0]
        passage_b = evidence.search_evidence(
            "1450", run_id=run["run_id"], settings=self.settings
        )["passages"][0]
        evidence.attach_citation(
            claim["claim_id"], passage_a["passage_id"], relation="supports",
            quote="The bridge is 1200 meters long.", settings=self.settings,
        )
        evidence.attach_citation(
            claim["claim_id"], passage_b["passage_id"], relation="contradicts",
            quote="The bridge is 1450 meters long.", settings=self.settings,
        )
        result = verification.analyze_conflicts(run["run_id"], settings=self.settings)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["unresolved_count"], 1)
        conflict = result["conflicts"][0]
        self.assertEqual(conflict["kind"], "citation_relations")
        self.assertEqual(conflict["status"], "conflicting_evidence")
        self.assertFalse(conflict["resolved"])
        self.assertEqual(len(conflict["sides"]), 2)
        self.assertIn("unresolved", conflict["resolution"])
        self.assertIn("source_assessment", result)

    def test_subquestion_conflict_is_listed(self) -> None:
        run = self.new_run()
        plan = planner.save_plan(
            run["run_id"],
            objective="Explain widgets",
            subquestions="Are widgets standard?\nWhere are widgets made?",
            settings=self.settings,
        )
        subquestions = plan["subquestions"]
        planner.update_subquestion(
            run["run_id"], subquestions[0]["subquestion_id"], status="conflicting",
            note="sources disagree", settings=self.settings,
        )
        with storage.open_db(self.settings) as conn:
            conflicts = verification.conflicts_in_conn(conn, run["run_id"])
        kinds = {item["kind"] for item in conflicts["conflicts"]}
        self.assertIn("subquestion", kinds)
        self.assertGreaterEqual(conflicts["unresolved_count"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

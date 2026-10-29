"""Tests for the Phase 4 research planner and orchestrator support.

Covers: subquestion parsing, per-run limits (defaults, overrides, ceilings),
plan persistence and the subquestion cap, subquestion statuses and gap
detection, the append-only execution log (including the per-subquestion
candidate cap and the follow-up round cap), unique-source capacity
enforcement inside the evidence pipeline, stopping conditions, run-history
reporting, and reports enriched with the stored plan.

Every test runs against a temporary SQLite database (no production store).
"""

from __future__ import annotations

import unittest

from agletras_mcp import evidence, planner, storage
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError
from agletras_mcp.report import build_evidence_markdown

from tests.support import TempHomeCase, serve_http

PAGE_HTML = (
    b"<html><head><title>Widget Page</title></head><body>"
    b"<p>Widgets are small mechanical components used in many devices.</p>"
    b"</body></html>"
)


class PlannerCase(TempHomeCase):
    """Shared setup: fresh settings and one open research run per test."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()
        self.run = evidence.start_run("How do widgets work?", settings=self.settings)
        self.run_id = self.run["run_id"]

    def plan(self, questions: str = "What are widgets?\nHow do gears work?", **kwargs):
        return planner.save_plan(
            self.run_id,
            objective=kwargs.pop("objective", "Understand widgets"),
            subquestions=questions,
            settings=self.settings,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Parsing and limits (pure)
# ---------------------------------------------------------------------------


class ParsingTests(unittest.TestCase):
    def test_plain_lines(self) -> None:
        parsed = planner.parse_subquestions("First?\n\nSecond?\n")
        self.assertEqual(
            [item["question"] for item in parsed], ["First?", "Second?"]
        )
        self.assertEqual([item["priority"] for item in parsed], [1, 2])

    def test_list_markers_are_stripped(self) -> None:
        parsed = planner.parse_subquestions("- First?\n2) Second?\n* Third?")
        self.assertEqual(
            [item["question"] for item in parsed], ["First?", "Second?", "Third?"]
        )

    def test_json_array_of_strings_and_objects(self) -> None:
        parsed = planner.parse_subquestions(
            '["Alpha?", {"question": "Beta?", "priority": 1}, {"text": "Gamma?"}]'
        )
        self.assertEqual([item["question"] for item in parsed], ["Alpha?", "Beta?", "Gamma?"])
        self.assertEqual(parsed[1]["priority"], 1)

    def test_duplicates_collapse_case_insensitively(self) -> None:
        parsed = planner.parse_subquestions("Widgets?\nwidgets?\n  Widgets?  ")
        self.assertEqual(len(parsed), 1)

    def test_invalid_inputs_are_rejected(self) -> None:
        with self.assertRaises(ToolError):
            planner.parse_subquestions("[1, 2]")
        with self.assertRaises(ToolError):
            planner.parse_subquestions('["x",]')  # malformed JSON
        with self.assertRaises(ToolError):
            planner.parse_subquestions("x" * 501)

    def test_assumptions_parse_and_dedupe(self) -> None:
        assumptions = planner.parse_assumptions("- Only English sources\n- Only English sources\nRecency matters")
        self.assertEqual(assumptions, ["Only English sources", "Recency matters"])
        self.assertEqual(planner.parse_assumptions(["A", "  ", "B"]), ["A", "B"])

    def test_default_limits(self) -> None:
        self.assertEqual(
            planner.normalize_limits(None),
            {
                "max_subquestions": 8,
                "max_followup_rounds": 2,
                "max_candidates_per_subquestion": 4,
                "max_unique_sources": 20,
            },
        )

    def test_limit_overrides_and_ceilings(self) -> None:
        limits = planner.normalize_limits(
            {"max_subquestions": 3, "max_unique_sources": 10_000}
        )
        self.assertEqual(limits["max_subquestions"], 3)
        self.assertEqual(limits["max_unique_sources"], planner.LIMIT_CEILINGS["max_unique_sources"])

    def test_invalid_limits_are_rejected(self) -> None:
        for bad in ({"nope": 3}, {"max_subquestions": 0}, {"max_subquestions": -2},
                    {"max_subquestions": "8"}, {"max_subquestions": True}, {"max_subquestions": 1.5}):
            with self.assertRaises(ToolError):
                planner.normalize_limits(bad)


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


class PlanTests(PlannerCase):
    def test_save_plan_stores_questions_limits_and_log_event(self) -> None:
        result = self.plan(
            scope="widgets only",
            assumptions="Sources are in English",
            limits={"max_unique_sources": 5},
        )
        self.assertEqual(result["total_subquestions"], 2)
        self.assertEqual(result["created_subquestions"], 2)
        self.assertEqual(result["limits"]["max_unique_sources"], 5)
        self.assertEqual(result["plan"]["objective"], "Understand widgets")
        self.assertEqual(result["plan"]["scope"], "widgets only")
        self.assertEqual(result["plan"]["assumptions"], ["Sources are in English"])
        statuses = [item["status"] for item in result["subquestions"]]
        self.assertEqual(statuses, ["pending", "pending"])
        overview = planner.run_overview(self.run_id, settings=self.settings)
        self.assertEqual([event["kind"] for event in overview["events"]], ["plan"])

    def test_subquestion_cap_is_enforced_at_plan_time(self) -> None:
        nine = "\n".join(f"Question {index}?" for index in range(1, 10))
        with self.assertRaises(ToolError) as ctx:
            self.plan(nine)
        self.assertEqual(ctx.exception.code, "invalid_argument")
        self.assertIn("limit", ctx.exception.message)
        # One explicit override makes three the cap.
        result = self.plan("A?\nB?\nC?", limits={"max_subquestions": 3})
        self.assertEqual(result["total_subquestions"], 3)
        with self.assertRaises(ToolError):
            self.plan("A?\nB?\nC?\nD?", limits={"max_subquestions": 3})

    def test_resaving_adds_new_questions_and_keeps_statuses(self) -> None:
        first = self.plan()
        subquestion_id = first["subquestions"][0]["subquestion_id"]
        planner.update_subquestion(
            self.run_id, subquestion_id, "answered", note="evidence found", settings=self.settings
        )
        again = self.plan("What are widgets?\nHow do gears work?\nWhich sizes exist?")
        self.assertEqual(again["created_subquestions"], 1)
        self.assertEqual(again["total_subquestions"], 3)
        kept = next(
            item for item in again["subquestions"] if item["subquestion_id"] == subquestion_id
        )
        self.assertEqual(kept["status"], "answered")
        self.assertEqual(kept["note"], "evidence found")

    def test_plan_validation_errors(self) -> None:
        with self.assertRaises(ToolError):
            self.plan(objective="   ")
        with self.assertRaises(ToolError):
            self.plan("   ")
        with self.assertRaises(ToolError):
            planner.save_plan(9999, objective="x", subquestions="y", settings=self.settings)

    def test_update_subquestion_validates_and_detects_gaps(self) -> None:
        plan = self.plan()
        first, second = plan["subquestions"]
        result = planner.update_subquestion(
            self.run_id,
            first["subquestion_id"],
            "answered",
            note="two sources agree",
            settings=self.settings,
        )
        self.assertEqual(result["subquestion"]["status"], "answered")
        self.assertEqual(result["stopping"]["gaps_remaining"], 1)
        overview = planner.run_overview(self.run_id, settings=self.settings)
        gap_questions = [gap["question"] for gap in overview["gaps"]]
        self.assertEqual(gap_questions, [second["question"]])
        self.assertEqual(overview["gaps"][0]["note"], "")
        # Notes accumulate; statuses are validated; cross-run ids are refused.
        planner.update_subquestion(
            self.run_id, second["subquestion_id"], "conflicting", note="sources disagree",
            settings=self.settings,
        )
        overview = planner.run_overview(self.run_id, settings=self.settings)
        self.assertEqual(overview["gaps"][0]["note"], "sources disagree")
        with self.assertRaises(ToolError):
            planner.update_subquestion(
                self.run_id, second["subquestion_id"], "probably", settings=self.settings
            )
        with self.assertRaises(ToolError):
            planner.update_subquestion(9999, second["subquestion_id"], "answered", settings=self.settings)

    def test_overview_stopping_reflects_run_state(self) -> None:
        plan = self.plan()
        self.assertFalse(
            planner.run_overview(self.run_id, settings=self.settings)["stopping"]["should_stop"]
        )
        for item in plan["subquestions"]:
            planner.update_subquestion(
                self.run_id, item["subquestion_id"], "answered", settings=self.settings
            )
        state = planner.run_overview(self.run_id, settings=self.settings)["stopping"]
        self.assertTrue(state["should_stop"])
        self.assertEqual(state["reasons"], ["all subquestions are answered"])


# ---------------------------------------------------------------------------
# Execution log
# ---------------------------------------------------------------------------


class EventLogTests(PlannerCase):
    def test_events_are_appended_in_order(self) -> None:
        plan = self.plan()
        subquestion_id = plan["subquestions"][0]["subquestion_id"]
        first = planner.log_event(
            self.run_id, "search", "searched widgets", subquestion_id=subquestion_id,
            payload={"query": "widgets"}, settings=self.settings,
        )
        second = planner.log_event(
            self.run_id, "gap", "no source covers gears", settings=self.settings
        )
        self.assertTrue(first["event_id"] < second["event_id"])
        overview = planner.run_overview(self.run_id, settings=self.settings)
        kinds = [event["kind"] for event in overview["events"]]
        self.assertEqual(kinds, ["plan", "search", "gap"])
        self.assertEqual(overview["events"][1]["payload"], {"query": "widgets"})
        self.assertEqual(overview["event_count"], 3)

    def test_event_validation(self) -> None:
        with self.assertRaises(ToolError):
            planner.log_event(self.run_id, "sing", "x", settings=self.settings)
        with self.assertRaises(ToolError):
            planner.log_event(self.run_id, "note", "  ", settings=self.settings)
        with self.assertRaises(ToolError):
            planner.log_event(self.run_id, "note", "x", source_id=999, settings=self.settings)
        with self.assertRaises(ToolError):
            planner.log_event(self.run_id, "note", "x", payload={"big": "y" * 9000}, settings=self.settings)
        with self.assertRaises(ToolError) as ctx:
            planner.log_event(self.run_id, "followup_round", "because", settings=self.settings)
        self.assertIn("start_followup_round", ctx.exception.message)

    def test_source_candidate_cap_is_per_subquestion_per_round(self) -> None:
        plan = self.plan()
        first, second = (item["subquestion_id"] for item in plan["subquestions"])
        for index in range(4):
            result = planner.log_event(
                self.run_id, "source_candidate", f"candidate {index}",
                subquestion_id=first, settings=self.settings,
            )
        self.assertEqual(result["candidates_used"], 4)
        self.assertEqual(result["candidates_remaining"], 0)
        with self.assertRaises(ToolError) as ctx:
            planner.log_event(
                self.run_id, "source_candidate", "candidate 5",
                subquestion_id=first, settings=self.settings,
            )
        self.assertEqual(ctx.exception.code, "limit_reached")
        # Other subquestions have their own budget.
        planner.log_event(
            self.run_id, "source_candidate", "candidate for second",
            subquestion_id=second, settings=self.settings,
        )
        # A follow-up round targeting the subquestion resets its budget.
        planner.start_followup_round(
            self.run_id, "reopen widgets", subquestion_id=first, settings=self.settings
        )
        reset = planner.log_event(
            self.run_id, "source_candidate", "candidate after round",
            subquestion_id=first, settings=self.settings,
        )
        self.assertEqual(reset["candidates_used"], 1)

    def test_source_candidate_requires_a_subquestion(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            planner.log_event(self.run_id, "source_candidate", "url", settings=self.settings)
        self.assertEqual(ctx.exception.code, "invalid_argument")


class FollowupRoundTests(PlannerCase):
    def test_rounds_are_bounded_and_logged(self) -> None:
        plan = self.plan()
        target = plan["subquestions"][1]["subquestion_id"]
        first = planner.start_followup_round(
            self.run_id, "gears still unanswered", subquestion_id=target, settings=self.settings
        )
        self.assertEqual((first["round"], first["rounds_remaining"]), (1, 1))
        second = planner.start_followup_round(self.run_id, "conflict on gear sizes", settings=self.settings)
        self.assertEqual((second["round"], second["rounds_remaining"]), (2, 0))
        with self.assertRaises(ToolError) as ctx:
            planner.start_followup_round(self.run_id, "one more", settings=self.settings)
        self.assertEqual(ctx.exception.code, "limit_reached")
        overview = planner.run_overview(self.run_id, settings=self.settings)
        self.assertEqual(overview["budget"]["followup_rounds_used"], 2)
        self.assertEqual(
            [event["message"] for event in overview["events"] if event["kind"] == "followup_round"],
            ["gears still unanswered", "conflict on gear sizes"],
        )

    def test_round_validation(self) -> None:
        with self.assertRaises(ToolError):
            planner.start_followup_round(self.run_id, "  ", settings=self.settings)
        with self.assertRaises(ToolError):
            planner.start_followup_round(self.run_id, "why", subquestion_id=123, settings=self.settings)


# ---------------------------------------------------------------------------
# Unique-source capacity (enforced inside the evidence pipeline)
# ---------------------------------------------------------------------------


class SourceCapacityTests(PlannerCase):
    def _save(self, text: str, *, run_id: int | None = None, title: str = "note"):
        return evidence.save_evidence(
            text=text, title=title, run_id=run_id if run_id is not None else self.run_id,
            settings=self.settings,
        )

    def test_unique_source_cap_blocks_new_sources_but_allows_resaves(self) -> None:
        self.plan(limits={"max_unique_sources": 2})
        self._save("Alpha widgets exist.", title="A")
        self._save("Beta widgets exist.", title="B")
        with self.assertRaises(ToolError) as ctx:
            self._save("Gamma widgets exist.", title="C")
        self.assertEqual(ctx.exception.code, "limit_reached")
        self.assertIn("2 unique sources", ctx.exception.message)
        # Re-saving an already-linked source adds no unique source.
        again = self._save("Alpha widgets exist.", title="A")
        self.assertEqual(again["source_id"], 1)
        # The rejected save left no partial rows behind.
        stats = storage.stats(settings=self.settings)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["evidence"]["passages"], 2)
        # A different run has its own budget.
        other = evidence.start_run("Another question", settings=self.settings)
        third = self._save("Gamma widgets exist.", title="C", run_id=other["run_id"])
        self.assertTrue(third["source_id"])

    def test_cached_stored_source_still_respects_the_cap(self) -> None:
        with serve_http({"/page": (200, "text/html", PAGE_HTML)}) as base:
            url = base + "/page"
            # Store the page once under a decoy run (source row exists, not linked here).
            decoy = evidence.start_run("Decoy", settings=self.settings)
            evidence.save_evidence(source=url, run_id=decoy["run_id"], settings=self.settings)
            self.plan(limits={"max_unique_sources": 1})
            self._save("Alpha widgets exist.", title="A")
            with self.assertRaises(ToolError) as ctx:
                evidence.save_evidence(source=url, run_id=self.run_id, settings=self.settings)
            self.assertEqual(ctx.exception.code, "limit_reached")
            # Raising the limit lets the cached copy link without refetching.
            self.plan("What are widgets?\nHow do gears work?", limits={"max_unique_sources": 5})
            saved = evidence.save_evidence(source=url, run_id=self.run_id, settings=self.settings)
            self.assertTrue(saved["cache_hit"])
            history = evidence.get_research_run_history(run_id=self.run_id, settings=self.settings)
            self.assertIn(saved["source_id"], [item["source_id"] for item in history["sources"]])

    def test_capacity_counts_overlap_across_subquestions(self) -> None:
        plan = self.plan(limits={"max_unique_sources": 2})
        first, second = (item["subquestion_id"] for item in plan["subquestions"])
        self._save("Source for the first question.", title="one")
        self._save("Source for the second question.", title="two")
        # Both subquestions share one overall cap: the third unique source fails
        # no matter which subquestion it would serve.
        planner.log_event(
            self.run_id, "source_candidate", "third candidate",
            subquestion_id=second, settings=self.settings,
        )
        with self.assertRaises(ToolError) as ctx:
            self._save("Source for either question.", title="three")
        self.assertEqual(ctx.exception.code, "limit_reached")
        self.assertEqual(first, plan["subquestions"][0]["subquestion_id"])


# ---------------------------------------------------------------------------
# Stopping conditions
# ---------------------------------------------------------------------------


def _subquestion(status: str, *, subquestion_id: int = 1) -> dict:
    return {"subquestion_id": subquestion_id, "question": f"Q{subquestion_id}", "status": status}


class StoppingTests(unittest.TestCase):
    LIMITS = dict(planner.DEFAULT_LIMITS)

    def test_no_plan_never_stops(self) -> None:
        state = planner.evaluate_stopping([], self.LIMITS, sources_used=0, rounds_used=0)
        self.assertFalse(state["should_stop"])
        self.assertIn("no research plan", state["reasons"][0])

    def test_all_answered_stops(self) -> None:
        state = planner.evaluate_stopping(
            [_subquestion("answered"), _subquestion("answered", subquestion_id=2)],
            self.LIMITS, sources_used=3, rounds_used=0,
        )
        self.assertTrue(state["should_stop"])
        self.assertEqual(state["reasons"], ["all subquestions are answered"])

    def test_open_gaps_with_budget_left_continue(self) -> None:
        state = planner.evaluate_stopping(
            [_subquestion("answered"), _subquestion("unanswered", subquestion_id=2)],
            self.LIMITS, sources_used=3, rounds_used=1,
        )
        self.assertFalse(state["should_stop"])
        self.assertEqual(state["gaps_remaining"], 1)

    def test_round_limit_stops_with_gaps(self) -> None:
        state = planner.evaluate_stopping(
            [_subquestion("partially_answered")],
            self.LIMITS, sources_used=1, rounds_used=2,
        )
        self.assertTrue(state["should_stop"])
        self.assertIn("follow-up round limit reached", state["reasons"][0])

    def test_source_cap_stops_with_gaps(self) -> None:
        state = planner.evaluate_stopping(
            [_subquestion("conflicting")],
            self.LIMITS, sources_used=20, rounds_used=0,
        )
        self.assertTrue(state["should_stop"])
        self.assertIn("unique-source cap reached", state["reasons"][0])


# ---------------------------------------------------------------------------
# Run history and report integration
# ---------------------------------------------------------------------------


class HistoryAndReportTests(PlannerCase):
    def test_history_detail_includes_plan_budget_and_log(self) -> None:
        plan = self.plan(scope="widgets", limits={"max_unique_sources": 9})
        subquestion_id = plan["subquestions"][0]["subquestion_id"]
        planner.log_event(
            self.run_id, "search", "query logged", subquestion_id=subquestion_id,
            settings=self.settings,
        )
        detail = evidence.get_research_run_history(run_id=self.run_id, settings=self.settings)
        self.assertEqual(detail["plan"]["objective"], "Understand widgets")
        self.assertEqual(detail["plan"]["limits"]["max_unique_sources"], 9)
        self.assertEqual(len(detail["subquestions"]), 2)
        self.assertEqual(detail["budget"]["sources_used"], 0)
        self.assertEqual(detail["budget"]["sources_remaining"], 9)
        self.assertEqual(detail["budget"]["followup_rounds_remaining"], 2)
        self.assertEqual(detail["stopping"]["should_stop"], False)
        self.assertEqual([event["kind"] for event in detail["events"]], ["plan", "search"])
        # The Phase 3 keys remain intact alongside the Phase 4 additions.
        self.assertEqual(detail["counts"]["sources"], 0)
        self.assertEqual(detail["run"]["run_id"], self.run_id)

    def test_report_includes_stored_plan_methodology_and_gaps(self) -> None:
        plan = self.plan(scope="widgets only", assumptions="English sources only")
        first, second = plan["subquestions"]
        saved = evidence.save_evidence(
            text="Widgets are small mechanical components used in many devices.",
            title="Widget note", run_id=self.run_id, settings=self.settings,
        )
        found = evidence.search_evidence(
            "widgets mechanical components", run_id=self.run_id, settings=self.settings
        )
        passage_id = found["passages"][0]["passage_id"]
        claim = evidence.register_claim(
            self.run_id, "Widgets are small mechanical components.", settings=self.settings
        )
        evidence.attach_citation(
            claim["claim_id"], passage_id,
            quote="small mechanical components used in many devices",
            settings=self.settings,
        )
        planner.update_subquestion(
            self.run_id, first["subquestion_id"], "answered", note="backed by the widget note",
            settings=self.settings,
        )
        planner.update_subquestion(
            self.run_id, second["subquestion_id"], "unanswered", note="no source covers gears",
            settings=self.settings,
        )
        planner.log_event(self.run_id, "search", "searched gears", settings=self.settings)
        planner.log_event(self.run_id, "source_failed", "one fetch failed", settings=self.settings)
        report = evidence.generate_report(
            self.run_id, title="Widget Report", settings=self.settings
        )
        markdown = report["markdown"]
        self.assertIn("## Research question and scope", markdown)
        self.assertIn("**Objective:** Understand widgets", markdown)
        self.assertIn("**Scope:** widgets only", markdown)
        self.assertIn("English sources only", markdown)
        self.assertIn("## Methodology", markdown)
        self.assertIn("Sources collected and stored for this run: 1", markdown)
        self.assertIn("Web searches logged in the run history: 1", markdown)
        self.assertIn("Retrieval failures logged: 1", markdown)
        self.assertIn("## Unanswered questions", markdown)
        self.assertIn("no source covers gears", markdown)
        self.assertIn("## Sources reviewed", markdown)
        self.assertIn("Widget note", markdown)
        self.assertIn("[S1]", markdown)
        self.assertEqual(report["citation_validation_status"], "validated")
        self.assertTrue(saved["source_id"])

    def test_report_without_plan_keeps_phase3_shape(self) -> None:
        """A run with no plan renders exactly the Phase 3 sections."""
        saved = evidence.save_evidence(
            text="Widgets are small mechanical components.", run_id=self.run_id,
            settings=self.settings,
        )
        report = evidence.generate_report(self.run_id, settings=self.settings)
        markdown = report["markdown"]
        self.assertNotIn("## Research question and scope", markdown)
        self.assertNotIn("## Unanswered questions", markdown)
        self.assertIn("## Claims", markdown)
        self.assertIn("## Sources reviewed", markdown)  # stored sources are still listed
        self.assertTrue(saved["source_id"])

    def test_direct_builder_renders_phase4_sections(self) -> None:
        run = {"run_id": 7, "query": "How do widgets work?"}
        markdown, _map = build_evidence_markdown(
            run,
            [],
            title="Direct",
            plan={
                "objective": "Understand widgets",
                "scope": "widgets",
                "assumptions": ["no prior knowledge"],
                "limits": dict(planner.DEFAULT_LIMITS),
            },
            gaps=[{"question": "Gears?", "status": "unanswered", "note": "nothing found"}],
            methodology={"sources_collected": 2, "searches_logged": 3},
            run_sources=[
                {
                    "title": "Widget note",
                    "url": "https://example.com/widgets",
                    "publication_date": "2026-01-15T00:00:00+00:00",
                    "retrieved_at": "2026-02-01T10:00:00+00:00",
                    "source_type": "web",
                }
            ],
        )
        self.assertIn("## Research question and scope", markdown)
        self.assertIn("8 subquestions", markdown)
        self.assertIn("2 follow-up rounds", markdown)
        self.assertIn("## Methodology", markdown)
        self.assertIn("## Unanswered questions", markdown)
        self.assertIn("**UNANSWERED**: nothing found", markdown)
        self.assertIn("## Sources reviewed", markdown)
        self.assertIn("published 2026-01-15", markdown)
        self.assertIn("retrieved 2026-02-01", markdown)


if __name__ == "__main__":
    unittest.main()

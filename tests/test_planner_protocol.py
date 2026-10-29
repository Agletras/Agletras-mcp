"""End-to-end Phase 4 acceptance test over the real MCP stdio protocol.

Spawns the server exactly like OpenCode does and walks the orchestrator
workflow end to end with local HTTP fixtures and a temporary SQLite database:
plan -> search -> evidence -> claims/citations -> gap -> bounded follow-up ->
cited report -> stopping reason -> restart persistence.

No fixture data ever touches the production evidence library.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from typing import Any
from urllib.parse import quote

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from agletras_mcp import storage
from tests.support import PROJECT_ROOT, AsyncTempHomeCase, serve_http

PHASE4_TOOLS = {
    "save_research_plan",
    "update_subquestion",
    "log_run_event",
    "start_followup_round",
    "finish_research_run",
}

ARTICLE_ONE = (
    b"<html><head><title>Widgets explained</title></head><body>"
    b"<h1>Widgets</h1>"
    b"<p>Widgets are small mechanical components used in many devices.</p>"
    b"<p>Standard widgets are manufactured in several sizes.</p>"
    b"</body></html>"
)

ARTICLE_TWO = (
    b"<html><head><title>Gears explained</title></head><body>"
    b"<h1>Gears</h1>"
    b"<p>Gears transmit rotational motion between shafts and axles.</p>"
    b"</body></html>"
)


def build_search_page(base: str) -> bytes:
    """A DuckDuckGo-style results page whose results point at local fixtures."""
    first = quote(base + "/article-one", safe="")
    second = quote(base + "/article-two", safe="")
    html = (
        "<!doctype html><html><body><div class=\"results\">"
        "<div class=\"result\">"
        "<h2 class=\"result__title\"><a class=\"result__a\" "
        f"href=\"//duckduckgo.com/l/?uddg={first}&amp;rut=x\">Widgets explained</a></h2>"
        "<a class=\"result__snippet\">Widgets are small mechanical components.</a>"
        "</div>"
        "<div class=\"result\">"
        "<h2 class=\"result__title\"><a class=\"result__a\" "
        f"href=\"//duckduckgo.com/l/?uddg={second}&amp;rut=x\">Gears explained</a></h2>"
        "<a class=\"result__snippet\">Gears transmit rotational motion between shafts.</a>"
        "</div>"
        "</div></body></html>"
    )
    return html.encode("utf-8")


def _text(result: Any) -> str:
    for block in result.content:
        if getattr(block, "type", "") == "text":
            return block.text
    raise AssertionError(f"no text content in result: {result!r}")


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(_text(result))


class ResearchOrchestratorProtocolTests(AsyncTempHomeCase):
    def _server_params(self, **env_overrides: str) -> StdioServerParameters:
        env = {**os.environ, **env_overrides}
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "agletras_mcp"],
            cwd=str(PROJECT_ROOT),
            env=env,
        )

    async def test_tool_catalog_includes_phase4_tools(self) -> None:
        async with Client(self._server_params()) as client:
            catalog = await client.list_tools()
        names = {tool.name for tool in catalog.tools}
        self.assertLessEqual(PHASE4_TOOLS, names)
        schemas = {tool.name: tool.input_schema for tool in catalog.tools}
        self.assertIn("subquestions", schemas["save_research_plan"].get("properties", {}))
        self.assertIn("limits", schemas["save_research_plan"].get("properties", {}))
        self.assertIn("status", schemas["update_subquestion"].get("properties", {}))
        self.assertIn("kind", schemas["log_run_event"].get("properties", {}))
        self.assertIn("reason", schemas["start_followup_round"].get("properties", {}))

    async def test_full_orchestrator_workflow(self) -> None:
        routes: dict[str, tuple] = {
            "/article-one": (200, "text/html; charset=utf-8", ARTICLE_ONE),
            "/article-two": (200, "text/html; charset=utf-8", ARTICLE_TWO),
            "/missing": (404, "text/plain", b"gone"),
        }
        with serve_http(routes) as base:
            # The search endpoint is only reachable after the server is up.
            routes["/search"] = (200, "text/html; charset=utf-8", build_search_page(base))
            env = {
                "AGLETRAS_SEARCH_PROVIDER": "duckduckgo",
                "AGLETRAS_SEARCH_ENDPOINT": base + "/search",
            }
            async with Client(self._server_params(**env)) as client:
                # 1. Start the run and record the plan.
                run = _payload(
                    await client.call_tool(
                        "start_research_run",
                        {"query": "How do widgets and gears work?"},
                    )
                )
                self.assertTrue(run["ok"])
                run_id = run["run_id"]
                planned = _payload(
                    await client.call_tool(
                        "save_research_plan",
                        {
                            "run_id": run_id,
                            "objective": "Explain widgets and gears",
                            "scope": "general mechanics only",
                            "assumptions": "English-language public sources",
                            "subquestions": json.dumps(
                                [
                                    {"question": "What are widgets?", "priority": 1},
                                    {"question": "How do gears work?", "priority": 2},
                                ]
                            ),
                        },
                    )
                )
                self.assertTrue(planned["ok"])
                self.assertEqual(planned["created_subquestions"], 2)
                self.assertEqual(planned["stopping"]["should_stop"], False)
                widgets, gears = planned["subquestions"]
                self.assertEqual([item["status"] for item in planned["subquestions"]], ["pending", "pending"])

                # 2. Search (the mocked endpoint returns two local results).
                search = _payload(
                    await client.call_tool("search_web", {"query": "widgets explained"})
                )
                self.assertTrue(search["ok"])
                self.assertEqual(search["count"], 2)
                widget_url = next(
                    item["url"] for item in search["results"] if item["title"] == "Widgets explained"
                )
                gear_url = next(
                    item["url"] for item in search["results"] if item["title"] == "Gears explained"
                )
                self.assertTrue(widget_url.startswith(base))
                logged = _payload(
                    await client.call_tool(
                        "log_run_event",
                        {
                            "run_id": run_id,
                            "kind": "search",
                            "message": "searched for widgets",
                            "subquestion_id": widgets["subquestion_id"],
                            "payload": json.dumps({"query": "widgets explained"}),
                        },
                    )
                )
                self.assertEqual(logged["kind"], "search")

                # 3. Collect evidence for subquestion 1; one candidate fails.
                candidate = _payload(
                    await client.call_tool(
                        "log_run_event",
                        {
                            "run_id": run_id,
                            "kind": "source_candidate",
                            "message": widget_url,
                            "subquestion_id": widgets["subquestion_id"],
                        },
                    )
                )
                self.assertEqual(candidate["candidates_remaining"], 3)
                saved = _payload(
                    await client.call_tool(
                        "save_evidence", {"source": widget_url, "run_id": run_id}
                    )
                )
                self.assertTrue(saved["ok"])
                self.assertTrue(saved["created_source"])
                self.assertGreaterEqual(saved["passages_saved"], 1)
                widget_source_id = saved["source_id"]
                _payload(
                    await client.call_tool(
                        "log_run_event",
                        {
                            "run_id": run_id,
                            "kind": "source_saved",
                            "message": "saved the widgets article",
                            "subquestion_id": widgets["subquestion_id"],
                            "source_id": widget_source_id,
                        },
                    )
                )
                failed = _payload(
                    await client.call_tool(
                        "save_evidence", {"source": base + "/missing", "run_id": run_id}
                    )
                )
                self.assertFalse(failed["ok"])
                self.assertEqual(failed["error"]["code"], "network_error")
                self.assertIn("404", failed["error"]["message"])
                _payload(
                    await client.call_tool(
                        "log_run_event",
                        {
                            "run_id": run_id,
                            "kind": "source_failed",
                            "message": "candidate 404ed",
                            "subquestion_id": gears["subquestion_id"],
                            "payload": json.dumps({"code": "network_error", "url": base + "/missing"}),
                        },
                    )
                )

                # 4. Store the supported claim with an exact passage quote.
                found = _payload(
                    await client.call_tool(
                        "search_evidence",
                        {"query": "widgets mechanical components", "run_id": run_id},
                    )
                )
                self.assertGreaterEqual(found["count"], 1)
                passage = found["passages"][0]
                quote = "small mechanical components used in many devices"
                self.assertIn(quote, passage["text"])
                claim = _payload(
                    await client.call_tool(
                        "register_claim",
                        {
                            "run_id": run_id,
                            "claim_text": "Widgets are small mechanical components.",
                        },
                    )
                )
                self.assertEqual(claim["status"], "unverified")
                citation = _payload(
                    await client.call_tool(
                        "attach_citation",
                        {
                            "claim_id": claim["claim_id"],
                            "passage_id": passage["passage_id"],
                            "source_id": widget_source_id,
                            "quote": quote,
                        },
                    )
                )
                self.assertEqual(citation["derived_status"], "supported")

                # 5. Evaluate: subquestion 1 answered; subquestion 2 is a gap.
                evaluated = _payload(
                    await client.call_tool(
                        "update_subquestion",
                        {
                            "run_id": run_id,
                            "subquestion_id": widgets["subquestion_id"],
                            "status": "answered",
                            "note": "one stored source with a cited claim",
                        },
                    )
                )
                self.assertEqual(evaluated["stopping"]["gaps_remaining"], 1)
                _payload(
                    await client.call_tool(
                        "update_subquestion",
                        {
                            "run_id": run_id,
                            "subquestion_id": gears["subquestion_id"],
                            "status": "unanswered",
                            "note": "candidate page missing",
                        },
                    )
                )
                history = _payload(
                    await client.call_tool("get_research_history", {"run_id": run_id})
                )
                self.assertEqual([gap["question"] for gap in history["gaps"]], ["How do gears work?"])
                self.assertEqual(history["budget"]["sources_used"], 1)
                self.assertFalse(history["stopping"]["should_stop"])

                # 6. Bounded follow-up round for the gap, then targeted evidence.
                round_start = _payload(
                    await client.call_tool(
                        "start_followup_round",
                        {
                            "run_id": run_id,
                            "reason": "subquestion 2 has no surviving source",
                            "subquestion_id": gears["subquestion_id"],
                        },
                    )
                )
                self.assertEqual((round_start["round"], round_start["rounds_remaining"]), (1, 1))
                saved_two = _payload(
                    await client.call_tool(
                        "save_evidence", {"source": gear_url, "run_id": run_id}
                    )
                )
                self.assertTrue(saved_two["ok"])
                found_two = _payload(
                    await client.call_tool(
                        "search_evidence", {"query": "gears rotational motion", "run_id": run_id}
                    )
                )
                passage_two = found_two["passages"][0]
                quote_two = "Gears transmit rotational motion between shafts and axles."
                self.assertIn(quote_two, passage_two["text"])
                claim_two = _payload(
                    await client.call_tool(
                        "register_claim",
                        {
                            "run_id": run_id,
                            "claim_text": "Gears transmit rotational motion between shafts and axles.",
                        },
                    )
                )
                citation_two = _payload(
                    await client.call_tool(
                        "attach_citation",
                        {
                            "claim_id": claim_two["claim_id"],
                            "passage_id": passage_two["passage_id"],
                            "quote": quote_two,
                        },
                    )
                )
                self.assertEqual(citation_two["derived_status"], "supported")
                _payload(
                    await client.call_tool(
                        "update_subquestion",
                        {
                            "run_id": run_id,
                            "subquestion_id": gears["subquestion_id"],
                            "status": "partially_answered",
                            "note": "one source answers the basic motion question only",
                        },
                    )
                )

                # 7. Generate the cited report: plan, methodology, gap, sources.
                report = _payload(
                    await client.call_tool(
                        "generate_report", {"run_id": run_id, "title": "Widgets and Gears"}
                    )
                )
                self.assertTrue(report["ok"])
                self.assertEqual(report["citation_validation_status"], "validated")
                self.assertTrue(report["validation"]["passed"])
                self.assertEqual(report["claims_rendered"], 2)
                self.assertEqual(set(report["citation_map"]), {"S1", "S2"})
                markdown = report["markdown"]
                self.assertIn("# Widgets and Gears", markdown)
                self.assertIn("## Research question and scope", markdown)
                self.assertIn("**Objective:** Explain widgets and gears", markdown)
                self.assertIn("## Methodology", markdown)
                self.assertIn("## Unanswered questions", markdown)
                self.assertIn("**PARTIALLY_ANSWERED**", markdown)
                self.assertIn("one source answers the basic motion question only", markdown)
                self.assertIn("[S1]", markdown)
                self.assertIn("[S2]", markdown)
                self.assertIn("## Sources reviewed", markdown)
                self.assertTrue(os.path.exists(report["path"]))

                # 8. Record the stopping reason and close the run.
                finished = _payload(
                    await client.call_tool(
                        "finish_research_run",
                        {
                            "run_id": run_id,
                            "status": "completed",
                            "reason": "initial evidence collected; remaining uncertainty documented",
                        },
                    )
                )
                self.assertTrue(finished["ok"])
                self.assertEqual(finished["run"]["status"], "completed")
                self.assertEqual(finished["stopping_event"]["kind"], "stopping")

                # 9. The full execution log and budget are retrievable.
                history = _payload(
                    await client.call_tool("get_research_history", {"run_id": run_id})
                )
                kinds = [event["kind"] for event in history["events"]]
                self.assertLessEqual(
                    {"plan", "search", "source_candidate", "source_saved", "source_failed",
                     "followup_round", "stopping"},
                    set(kinds),
                )
                self.assertEqual(history["budget"]["followup_rounds_used"], 1)
                self.assertEqual(history["budget"]["followup_rounds_remaining"], 1)
                self.assertEqual(history["budget"]["sources_used"], 2)
                self.assertEqual(history["run"]["status"], "completed")
                self.assertEqual(history["reports"][0]["citation_validation_status"], "validated")

            # 10. Everything survives a server restart (same home).
            async with Client(self._server_params(**env)) as client:
                stats = _payload(await client.call_tool("stats", {}))
                self.assertEqual(stats["schema_version"], storage.SCHEMA_VERSION)
                self.assertGreaterEqual(stats["evidence"]["research_subquestions"], 2)
                self.assertGreaterEqual(stats["evidence"]["research_run_events"], 7)
                history = _payload(
                    await client.call_tool("get_research_history", {"run_id": run_id})
                )
                self.assertEqual(history["run"]["status"], "completed")
                self.assertEqual(len(history["subquestions"]), 2)
                revalidated = _payload(
                    await client.call_tool("validate_report", {"report_id": report["report_id"]})
                )
                self.assertTrue(revalidated["passed"])

    async def test_limits_and_stopping_are_enforced_over_the_wire(self) -> None:
        async with Client(self._server_params()) as client:
            run = _payload(await client.call_tool("start_research_run", {"query": "Bounded run"}))
            run_id = run["run_id"]
            plan = _payload(
                await client.call_tool(
                    "save_research_plan",
                    {
                        "run_id": run_id,
                        "objective": "Stay inside tight limits",
                        "subquestions": "Only question?",
                        "limits": json.dumps({"max_unique_sources": 1, "max_followup_rounds": 1}),
                    },
                )
            )
            self.assertEqual(plan["limits"]["max_unique_sources"], 1)

            # Subquestion cap: the ninth question is refused, the plan rolls back.
            nine = "\n".join(f"Question {index}?" for index in range(1, 10))
            too_many = _payload(
                await client.call_tool(
                    "save_research_plan",
                    {"run_id": run_id, "objective": "More", "subquestions": nine},
                )
            )
            self.assertFalse(too_many["ok"])
            self.assertEqual(too_many["error"]["code"], "invalid_argument")

            # Unique-source cap.
            first = _payload(
                await client.call_tool(
                    "save_evidence", {"text": "Only one source allowed.", "run_id": run_id}
                )
            )
            self.assertTrue(first["ok"])
            second = _payload(
                await client.call_tool(
                    "save_evidence", {"text": "A second source is too many.", "run_id": run_id}
                )
            )
            self.assertFalse(second["ok"])
            self.assertEqual(second["error"]["code"], "limit_reached")

            # Follow-up round cap.
            round_one = _payload(
                await client.call_tool(
                    "start_followup_round", {"run_id": run_id, "reason": "verify the answer"}
                )
            )
            self.assertTrue(round_one["ok"])
            round_two = _payload(
                await client.call_tool(
                    "start_followup_round", {"run_id": run_id, "reason": "one more round"}
                )
            )
            self.assertFalse(round_two["ok"])
            self.assertEqual(round_two["error"]["code"], "limit_reached")

            # Candidate cap: the fifth candidate for one subquestion is refused.
            subquestion_id = plan["subquestions"][0]["subquestion_id"]
            for index in range(4):
                ok_candidate = _payload(
                    await client.call_tool(
                        "log_run_event",
                        {
                            "run_id": run_id,
                            "kind": "source_candidate",
                            "message": f"candidate {index}",
                            "subquestion_id": subquestion_id,
                        },
                    )
                )
                self.assertTrue(ok_candidate["ok"])
            extra = _payload(
                await client.call_tool(
                    "log_run_event",
                    {
                        "run_id": run_id,
                        "kind": "source_candidate",
                        "message": "candidate 5",
                        "subquestion_id": subquestion_id,
                    },
                )
            )
            self.assertFalse(extra["ok"])
            self.assertEqual(extra["error"]["code"], "limit_reached")

            # Stopping state: both budgets are exhausted with a pending subquestion.
            history = _payload(await client.call_tool("get_research_history", {"run_id": run_id}))
            self.assertEqual(history["budget"]["sources_remaining"], 0)
            self.assertEqual(history["budget"]["followup_rounds_remaining"], 0)
            self.assertTrue(history["stopping"]["should_stop"])
            self.assertEqual(history["stopping"]["gaps_remaining"], 1)
            self.assertEqual(len(history["stopping"]["reasons"]), 2)

            # A failed run can be closed with its error recorded.
            finished = _payload(
                await client.call_tool(
                    "finish_research_run",
                    {
                        "run_id": run_id,
                        "status": "failed",
                        "reason": "limits reached with the question unresolved",
                        "error": "search provider unavailable",
                    },
                )
            )
            self.assertTrue(finished["ok"])
            self.assertEqual(finished["run"]["status"], "failed")
            self.assertEqual(finished["run"]["error"], "search provider unavailable")
            empty_reason = _payload(
                await client.call_tool(
                    "finish_research_run", {"run_id": run_id, "status": "completed", "reason": " "}
                )
            )
            self.assertFalse(empty_reason["ok"])
            self.assertEqual(empty_reason["error"]["code"], "invalid_argument")


if __name__ == "__main__":
    unittest.main()

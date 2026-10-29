"""End-to-end evidence-library acceptance test over the real MCP stdio protocol.

Spawns the server exactly like OpenCode does and walks the full Phase 3
workflow: run -> evidence -> search -> claims -> citations -> report
validation. The unverified claim must stay visibly UNVERIFIED, and the whole
trace must survive a server restart (same home directory).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from agletras_mcp import storage
from tests.support import PROJECT_ROOT, AsyncTempHomeCase

PHASE3_TOOLS = {
    "start_research_run",
    "save_evidence",
    "search_evidence",
    "register_claim",
    "attach_citation",
    "get_claim_citations",
    "get_research_history",
    "generate_report",
    "validate_report",
}

EVIDENCE_TEXT = (
    "# Widget notes\n"
    "Widgets are small mechanical components used in many devices.\n\n"
    "Gears transmit rotational motion between shafts and axles.\n"
)


def _text(result: Any) -> str:
    for block in result.content:
        if getattr(block, "type", "") == "text":
            return block.text
    raise AssertionError(f"no text content in result: {result!r}")


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(_text(result))


class EvidenceProtocolTests(AsyncTempHomeCase):
    def _server_params(self, **env_overrides: str) -> StdioServerParameters:
        env = {**os.environ, **env_overrides}
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "agletras_mcp"],
            cwd=str(PROJECT_ROOT),
            env=env,
        )

    async def test_full_evidence_workflow(self) -> None:
        evidence_file = self.home / "evidence.md"
        evidence_file.write_text(EVIDENCE_TEXT, encoding="utf-8")
        async with Client(self._server_params()) as client:
            catalog = await client.list_tools()
            names = {tool.name for tool in catalog.tools}
            self.assertLessEqual(PHASE3_TOOLS, names)
            schemas = {tool.name: tool.input_schema for tool in catalog.tools}
            save_props = schemas["save_evidence"].get("properties", {})
            self.assertIn("source", save_props)
            self.assertIn("text", save_props)
            self.assertIn("passage_id", schemas["attach_citation"].get("properties", {}))

            # 1. Start a run.
            run = _payload(
                await client.call_tool(
                    "start_research_run",
                    {"query": "How do widgets work?", "config": '{"depth": 1}'},
                )
            )
            self.assertTrue(run["ok"])
            self.assertEqual(run["status"], "running")
            self.assertEqual(run["config"], {"depth": 1})
            run_id = run["run_id"]

            # 2. Save evidence from a local file into the run.
            saved = _payload(
                await client.call_tool(
                    "save_evidence", {"source": str(evidence_file), "run_id": run_id}
                )
            )
            self.assertTrue(saved["ok"])
            self.assertTrue(saved["created_source"])
            self.assertGreaterEqual(saved["passages_saved"], 1)
            source_id = saved["source_id"]

            # 3. Find exact passages.
            found = _payload(
                await client.call_tool("search_evidence", {"query": "widgets mechanical components"})
            )
            self.assertTrue(found["ok"])
            self.assertGreaterEqual(found["count"], 1)
            top = found["passages"][0]
            self.assertIn("Widgets are small mechanical components", top["text"])
            passage_id = top["passage_id"]

            # 4. Register one supportable claim and one claim with no evidence.
            supported = _payload(
                await client.call_tool(
                    "register_claim",
                    {"run_id": run_id, "claim_text": "Widgets are small mechanical components."},
                )
            )
            unverified = _payload(
                await client.call_tool(
                    "register_claim",
                    {"run_id": run_id, "claim_text": "Widgets were invented in 1842."},
                )
            )
            self.assertEqual(supported["status"], "unverified")  # never auto-verified
            self.assertEqual(unverified["status"], "unverified")

            # 5. A fabricated quote is rejected; an exact quote attaches.
            fabricated = _payload(
                await client.call_tool(
                    "attach_citation",
                    {
                        "claim_id": supported["claim_id"],
                        "passage_id": passage_id,
                        "quote": "this quotation does not exist in the source",
                    },
                )
            )
            self.assertFalse(fabricated["ok"])
            self.assertEqual(fabricated["error"]["code"], "invalid_argument")

            citation = _payload(
                await client.call_tool(
                    "attach_citation",
                    {
                        "claim_id": supported["claim_id"],
                        "passage_id": passage_id,
                        "quote": "small mechanical components used in many devices",
                        "source_id": source_id,
                    },
                )
            )
            self.assertTrue(citation["ok"])
            self.assertEqual(citation["derived_status"], "supported")
            self.assertTrue(citation["citation"]["quote_matches_passage"])

            # 6. Claims and citations are retrievable with full provenance.
            citations = _payload(
                await client.call_tool(
                    "get_claim_citations", {"claim_id": supported["claim_id"]}
                )
            )
            self.assertEqual(citations["status"], "supported")
            self.assertEqual(len(citations["citations"]), 1)
            self.assertEqual(citations["citations"][0]["passage"]["passage_id"], passage_id)
            self.assertEqual(citations["citations"][0]["source"]["source_id"], source_id)
            unverified_citations = _payload(
                await client.call_tool(
                    "get_claim_citations", {"claim_id": unverified["claim_id"]}
                )
            )
            self.assertEqual(unverified_citations["status"], "unverified")
            self.assertEqual(unverified_citations["citations"], [])

            # 7. Generate the report: stable [S1], visible UNVERIFIED.
            report = _payload(
                await client.call_tool(
                    "generate_report", {"run_id": run_id, "title": "Widget Report"}
                )
            )
            self.assertTrue(report["ok"])
            self.assertEqual(report["citation_validation_status"], "validated")
            self.assertEqual(report["citation_map"], {"S1": citation["citation"]["citation_id"]})
            self.assertIn("[S1]", report["markdown"])
            self.assertIn("UNVERIFIED", report["markdown"])
            self.assertIn("Widgets were invented in 1842.", report["markdown"])
            self.assertTrue(report["path"].startswith(str(self.home / "reports")))
            self.assertTrue(os.path.exists(report["path"]))
            self.assertEqual(
                report["validation"]["unverified_claims"], [unverified["claim_id"]]
            )
            self.assertEqual(report["validation"]["semantic_support"], "not_evaluated")

            # 8. Re-validation is stable and explicit about its limits.
            revalidated = _payload(
                await client.call_tool("validate_report", {"report_id": report["report_id"]})
            )
            self.assertTrue(revalidated["ok"])
            self.assertTrue(revalidated["passed"])
            self.assertEqual(revalidated["validation_kind"], "structural")

            # 9. The full run trace is available, including the unverified claim.
            history = _payload(
                await client.call_tool("get_research_history", {"run_id": run_id})
            )
            self.assertEqual(history["run"]["status"], "completed")
            self.assertEqual(history["counts"]["sources"], 1)
            self.assertEqual(history["counts"]["claims"], 2)
            self.assertEqual(history["claims_by_status"], {"supported": 1, "unverified": 1})
            self.assertEqual(
                [claim["claim_id"] for claim in history["unverified_claims"]],
                [unverified["claim_id"]],
            )
            self.assertEqual(history["reports"][0]["citation_validation_status"], "validated")

            # 10. get_source exposes the evidence extras (passages, identities).
            source = _payload(
                await client.call_tool(
                    "get_source", {"source_id": source_id, "include_passages": True}
                )
            )
            self.assertTrue(source["ok"])
            self.assertGreaterEqual(source["source"]["evidence"]["passage_count"], 1)
            self.assertEqual(source["source"]["evidence"]["run_ids"], [run_id])
            self.assertGreaterEqual(len(source["source"]["passages"]), 1)

        # 11. Everything survives a server restart.
        async with Client(self._server_params()) as client:
            history = _payload(await client.call_tool("get_research_history", {}))
            self.assertEqual(history["count"], 1)
            self.assertEqual(history["runs"][0]["claims_count"], 2)
            stats = _payload(await client.call_tool("stats", {}))
            self.assertEqual(stats["schema_version"], storage.SCHEMA_VERSION)
            self.assertFalse(stats["evidence"]["fts5"] is None)
            found = _payload(
                await client.call_tool("search_evidence", {"query": "gears rotational motion"})
            )
            self.assertGreaterEqual(found["count"], 1)

    async def test_evidence_errors_are_structured_over_the_wire(self) -> None:
        async with Client(self._server_params()) as client:
            no_run = _payload(
                await client.call_tool("register_claim", {"run_id": 42, "claim_text": "x"})
            )
            self.assertFalse(no_run["ok"])
            self.assertEqual(no_run["error"]["code"], "not_found")
            bad_mode = _payload(await client.call_tool("save_evidence", {}))
            self.assertFalse(bad_mode["ok"])
            self.assertEqual(bad_mode["error"]["code"], "invalid_argument")
            bad_relation = _payload(
                await client.call_tool(
                    "attach_citation", {"claim_id": 1, "passage_id": 1, "relation": "proves"}
                )
            )
            self.assertFalse(bad_relation["ok"])
            self.assertEqual(bad_relation["error"]["code"], "invalid_argument")
            missing_report = _payload(
                await client.call_tool("validate_report", {"report_id": 7})
            )
            self.assertFalse(missing_report["ok"])
            self.assertEqual(missing_report["error"]["code"], "not_found")

"""End-to-end Phase 6 acceptance test over the real MCP stdio protocol.

Spawns the server exactly like OpenCode does and walks the full Phase 6
workflow: run -> evidence -> claim extraction -> verification -> conflicts ->
synthesis report -> integrity validation -> retrieval. Everything must
survive a server restart (same home directory).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from tests.support import PROJECT_ROOT, AsyncTempHomeCase

PHASE6_TOOLS = {
    "extract_claims",
    "verify_claims",
    "analyze_conflicts",
    "generate_markdown_report",
    "get_report",
}

CLAIM_TEXT = "The bridge is 1200 meters long."


def _text(result: Any) -> str:
    for block in result.content:
        if getattr(block, "type", "") == "text":
            return block.text
    raise AssertionError(f"no text content in result: {result!r}")


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(_text(result))


class Phase6ProtocolTests(AsyncTempHomeCase):
    def _server_params(self, **env_overrides: str) -> StdioServerParameters:
        env = {**os.environ, **env_overrides}
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "agletras_mcp"],
            cwd=str(PROJECT_ROOT),
            env=env,
        )

    async def test_full_phase6_workflow(self) -> None:
        async with Client(self._server_params()) as client:
            catalog = await client.list_tools()
            names = {tool.name for tool in catalog.tools}
            self.assertLessEqual(PHASE6_TOOLS, names)
            schemas = {tool.name: tool.input_schema for tool in catalog.tools}
            self.assertIn("run_id", schemas["extract_claims"].get("properties", {}))
            self.assertIn("register", schemas["extract_claims"].get("properties", {}))
            self.assertIn("method", schemas["verify_claims"].get("properties", {}))
            self.assertIn("run_id", schemas["generate_markdown_report"].get("properties", {}))
            self.assertIn("report_id", schemas["get_report"].get("properties", {}))

            # 1. Start a run and save two conflicting sources.
            run = _payload(
                await client.call_tool(
                    "start_research_run", {"query": "How long is the bridge?"}
                )
            )
            self.assertTrue(run["ok"])
            run_id = run["run_id"]
            saved_a = _payload(
                await client.call_tool(
                    "save_evidence",
                    {
                        "text": "The bridge is 1200 meters long.",
                        "title": "Bridge survey A",
                        "url": "https://a.example/bridge",
                        "run_id": run_id,
                    },
                )
            )
            self.assertTrue(saved_a["created_source"])
            saved_b = _payload(
                await client.call_tool(
                    "save_evidence",
                    {
                        "text": "The bridge is 1450 meters long.",
                        "title": "Bridge survey B",
                        "url": "https://b.example/bridge",
                        "run_id": run_id,
                    },
                )
            )
            self.assertTrue(saved_b["created_source"])

            # 2. Extract candidate statements and register them (UNVERIFIED).
            extracted = _payload(
                await client.call_tool(
                    "extract_claims",
                    {"run_id": run_id, "text": CLAIM_TEXT, "register": True, "max_claims": 3},
                )
            )
            self.assertTrue(extracted["ok"])
            self.assertEqual(extracted["extraction_method"],
                             "deterministic_sentence_marker_scoring")
            self.assertEqual(extracted["candidate_count"], 1)
            self.assertEqual(len(extracted["registered"]), 1)
            self.assertTrue(extracted["candidates"][0]["evidence_matches"])
            claim_id = extracted["registered"][0]["claim_id"]

            # 3. Attach one supporting and one contradicting citation.
            passage_a = _payload(
                await client.call_tool(
                    "search_evidence", {"query": "1200", "run_id": run_id}
                )
            )["passages"][0]
            passage_b = _payload(
                await client.call_tool(
                    "search_evidence", {"query": "1450", "run_id": run_id}
                )
            )["passages"][0]
            citation_a = _payload(
                await client.call_tool(
                    "attach_citation",
                    {
                        "claim_id": claim_id,
                        "passage_id": passage_a["passage_id"],
                        "quote": "The bridge is 1200 meters long.",
                        "relation": "supports",
                    },
                )
            )
            self.assertEqual(citation_a["derived_status"], "supported")
            citation_b = _payload(
                await client.call_tool(
                    "attach_citation",
                    {
                        "claim_id": claim_id,
                        "passage_id": passage_b["passage_id"],
                        "quote": "The bridge is 1450 meters long.",
                        "relation": "contradicts",
                    },
                )
            )
            self.assertEqual(citation_b["derived_status"], "contradicted")

            # 4. Verification: deterministic, explicit status, method recorded.
            verified = _payload(
                await client.call_tool("verify_claims", {"run_id": run_id})
            )
            self.assertTrue(verified["ok"])
            self.assertTrue(verified["deterministic_only"])
            self.assertFalse(verified["model_available"])
            self.assertEqual(verified["verified_count"], 1)
            entry = verified["claims"][0]
            self.assertEqual(entry["verification"]["status"], "conflicting_evidence")
            self.assertEqual(entry["verification"]["method"], "deterministic_checks")
            self.assertIn("CONFLICTING EVIDENCE",
                          entry["verification"]["status_label"])
            self.assertEqual(verified["counts_by_status"],
                             {"conflicting_evidence": 1})

            # 5. Conflict analysis keeps the disagreement unresolved.
            conflicts = _payload(
                await client.call_tool("analyze_conflicts", {"run_id": run_id})
            )
            self.assertTrue(conflicts["ok"])
            self.assertGreaterEqual(conflicts["unresolved_count"], 1)
            kinds = {item["kind"] for item in conflicts["conflicts"]}
            self.assertIn("citation_relations", kinds)
            self.assertIn("verification", kinds)
            self.assertIn("source_assessment", conflicts)

            # 6. Synthesis report: 10 sections, stable ids, integrity validated.
            generated = _payload(
                await client.call_tool(
                    "generate_markdown_report",
                    {"run_id": run_id, "title": "Bridge synthesis"},
                )
            )
            self.assertTrue(generated["ok"])
            self.assertEqual(generated["template_version"], "phase6")
            self.assertEqual(generated["validation_status"], "validated")
            self.assertEqual(generated["validation"]["errors"], [])
            self.assertEqual(generated["validation"]["semantic_support"], "not_evaluated")
            markdown = generated["markdown"]
            self.assertIn("[F1]", markdown)
            self.assertIn("[S1]", markdown)
            self.assertIn("CONFLICTING EVIDENCE", markdown)
            self.assertIn("## Conflicting evidence", markdown)
            self.assertIn("UNRESOLVED", markdown)
            self.assertIn("no liveness check was performed", markdown)
            self.assertTrue(generated["path"].startswith(str(self.home / "reports")))
            self.assertTrue(os.path.exists(generated["path"]))
            report_id = generated["report_id"]

            # 7. Retrieval exposes sections, maps, and on-disk integrity.
            fetched = _payload(
                await client.call_tool(
                    "get_report", {"report_id": report_id, "include_content": True}
                )
            )
            self.assertTrue(fetched["ok"])
            self.assertTrue(fetched["file_exists"])
            self.assertTrue(fetched["file_hash_matches"])
            self.assertEqual(fetched["finding_map"], {"F1": claim_id})
            self.assertIn("Key findings", fetched["sections"])
            self.assertIn("Image attribution", fetched["sections"])
            self.assertIn(CLAIM_TEXT, fetched["content"])

            # 8. Re-validation via the shared validate_report tool.
            revalidated = _payload(
                await client.call_tool("validate_report", {"report_id": report_id})
            )
            self.assertTrue(revalidated["ok"])
            self.assertTrue(revalidated["passed"])
            self.assertTrue(revalidated["phase6_checks"])
            self.assertEqual(revalidated["validation_kind"], "structural+phase6_integrity")

            # 9. Close the run.
            finished = _payload(
                await client.call_tool(
                    "finish_research_run",
                    {"run_id": run_id, "reason": "synthesis report validated"},
                )
            )
            self.assertEqual(finished["run"]["status"], "completed")
            self.assertEqual(finished["stopping_event"]["kind"], "stopping")

        # 10. The report and its validation survive a server restart.
        async with Client(self._server_params()) as client:
            fetched = _payload(
                await client.call_tool("get_report", {"report_id": report_id})
            )
            self.assertEqual(fetched["path"], generated["path"])
            self.assertEqual(fetched["citation_validation_status"], "validated")
            history = _payload(
                await client.call_tool("get_research_history", {"run_id": run_id})
            )
            self.assertEqual(history["run"]["status"], "completed")
            self.assertEqual(history["verifications_count"], 1)

    async def test_phase6_errors_are_structured_over_the_wire(self) -> None:
        async with Client(self._server_params()) as client:
            bad_claim_type = _payload(
                await client.call_tool(
                    "extract_claims",
                    {"run_id": 1, "text": "Widgets are small components of devices.", "claim_type": "rumour"},
                )
            )
            self.assertFalse(bad_claim_type["ok"])
            self.assertEqual(bad_claim_type["error"]["code"], "invalid_argument")

            missing_run = _payload(
                await client.call_tool(
                    "extract_claims", {"run_id": 1, "text": "Widgets are small components of devices."}
                )
            )
            self.assertFalse(missing_run["ok"])
            self.assertEqual(missing_run["error"]["code"], "not_found")

            run = _payload(
                await client.call_tool("start_research_run", {"query": "widgets?"})
            )

            bad_method = _payload(
                await client.call_tool(
                    "verify_claims", {"run_id": run["run_id"], "method": "psychic"}
                )
            )
            self.assertFalse(bad_method["ok"])
            self.assertEqual(bad_method["error"]["code"], "invalid_argument")

            missing_report = _payload(
                await client.call_tool("get_report", {"report_id": 999})
            )
            self.assertFalse(missing_report["ok"])
            self.assertEqual(missing_report["error"]["code"], "not_found")

            missing_generate = _payload(
                await client.call_tool("generate_markdown_report", {"run_id": 999})
            )
            self.assertFalse(missing_generate["ok"])
            self.assertEqual(missing_generate["error"]["code"], "not_found")

            missing_validate = _payload(
                await client.call_tool("validate_report", {"report_id": 999})
            )
            self.assertFalse(missing_validate["ok"])
            self.assertEqual(missing_validate["error"]["code"], "not_found")

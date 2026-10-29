"""Phase 8 MCP protocol tests: memory recall and monitoring over stdio.

These spawn the real server exactly as OpenCode does and exercise the four
Phase 8 tools end to end. All fixtures are local; no external network.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from tests.support import PROJECT_ROOT, AsyncTempHomeCase


def _text(result: Any) -> str:
    for block in result.content:
        if getattr(block, "type", "") == "text":
            return block.text
    raise AssertionError(f"no text content in result: {result!r}")


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(_text(result))


class Phase8ProtocolTests(AsyncTempHomeCase):
    def _server_params(self) -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "agletras_mcp"],
            cwd=str(PROJECT_ROOT),
            env={**os.environ},
        )

    async def test_memory_recall_over_the_wire(self) -> None:
        async with Client(self._server_params()) as client:
            run = _payload(
                await client.call_tool("start_research_run", {"query": "Memory protocol test"})
            )
            run_id = run["run_id"]
            saved = _payload(
                await client.call_tool(
                    "save_evidence",
                    {
                        "text": "Heliotrope collectors track the sun across the southern sky.",
                        "title": "Heliotrope notes",
                        "url": "https://memory.example/one",
                        "run_id": run_id,
                    },
                )
            )
            self.assertTrue(saved["ok"])
            source_id = saved["source_id"]

            recalled = _payload(
                await client.call_tool("search_memory", {"query": "heliotrope collectors"})
            )
            self.assertTrue(recalled["ok"])
            self.assertGreaterEqual(recalled["count"], 1)
            hit = recalled["passages"][0]
            self.assertEqual(hit["source_id"], source_id)
            self.assertIn("Heliotrope collectors", hit["text"])
            self.assertIn(run_id, hit["run_ids"])
            self.assertTrue(hit["fresh"])
            self.assertIsNotNone(hit["age_days"])

            # Project filter excludes evidence from other projects (honest counts).
            filtered = _payload(
                await client.call_tool(
                    "search_memory", {"query": "heliotrope collectors", "project": "other"}
                )
            )
            self.assertEqual(filtered["count"], 0)
            self.assertGreaterEqual(filtered["excluded_by_project"], 1)

            # Bounded freshness excludes only what it can prove stale.
            bounded = _payload(
                await client.call_tool(
                    "search_memory", {"query": "heliotrope collectors", "max_age_days": 365}
                )
            )
            self.assertGreaterEqual(bounded["count"], 1)
            self.assertTrue(bounded["passages"][0]["fresh"])

    async def test_monitoring_change_detection_over_the_wire(self) -> None:
        sample = self.home / "watch.txt"
        sample.write_text("Baseline finding: alpha.\n", encoding="utf-8")
        async with Client(self._server_params()) as client:
            added = _payload(
                await client.call_tool("monitor_add_target", {"target": str(sample), "name": "Watch"})
            )
            self.assertTrue(added["ok"])
            self.assertTrue(added["created"])
            target_id = added["target"]["target_id"]

            first = _payload(
                await client.call_tool("monitor_check", {"target_ids": str(target_id)})
            )
            self.assertEqual(first["counts_by_status"], {"initial": 1})
            self.assertEqual(first["snapshots"][0]["status"], "initial")

            sample.write_text(
                "Baseline finding: alpha.\nSubstantive update about the gamma project.\n",
                encoding="utf-8",
            )
            second = _payload(
                await client.call_tool("monitor_check", {"target_ids": str(target_id)})
            )
            self.assertEqual(second["counts_by_status"], {"changed": 1})

            listed = _payload(
                await client.call_tool("monitor_changes", {"target_id": target_id})
            )
            self.assertTrue(listed["ok"])
            self.assertEqual(listed["count"], 1)
            item = listed["changes"][0]
            self.assertEqual(item["target"]["target_id"], target_id)
            self.assertEqual(item["change"]["status"], "changed")
            self.assertIn("gamma project", " ".join(item["change"]["added_terms"]))
            self.assertIsNotNone(item["previous"])
            self.assertIsNotNone(item["change"]["source_id"])

            # Re-adding the same target updates, never duplicates.
            again = _payload(
                await client.call_tool(
                    "monitor_add_target", {"target": str(sample), "project": "demo"}
                )
            )
            self.assertFalse(again["created"])
            self.assertEqual(again["target"]["target_id"], target_id)
            self.assertEqual(again["target"]["project"], "demo")

    async def test_failed_check_is_not_a_change_or_deletion(self) -> None:
        sample = self.home / "gone.txt"
        sample.write_text("Evidence that must survive a failed check.\n", encoding="utf-8")
        async with Client(self._server_params()) as client:
            added = _payload(await client.call_tool("monitor_add_target", {"target": str(sample)}))
            target_id = added["target"]["target_id"]
            baseline = _payload(
                await client.call_tool("monitor_check", {"target_ids": str(target_id)})
            )
            source_id = baseline["snapshots"][0]["source_id"]
            self.assertIsNotNone(source_id)

            sample.unlink()
            failed = _payload(
                await client.call_tool("monitor_check", {"target_ids": str(target_id)})
            )
            snapshot = failed["snapshots"][0]
            self.assertEqual(snapshot["status"], "failed")
            self.assertNotEqual(snapshot["error_code"], "")
            self.assertEqual(snapshot["source_id"], source_id)  # evidence untouched
            listed = _payload(
                await client.call_tool("monitor_changes", {"target_id": target_id})
            )
            self.assertEqual(listed["count"], 0)

            stored = _payload(await client.call_tool("get_source", {"source_id": source_id}))
            self.assertIn("must survive", stored["source"]["content"])


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()

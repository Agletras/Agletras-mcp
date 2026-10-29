"""End-to-end MCP protocol tests: spawn the real server over stdio.

These tests exercise exactly what OpenCode does when it launches the server
configured in ``opencode.json``: a stdio subprocess speaking the MCP protocol.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from tests.support import PROJECT_ROOT, AsyncTempHomeCase

EXPECTED_TOOLS = {
    "health_check",
    "fetch_url",
    "search_web",
    "extract_pdf",
    "collect_sources",
    "collect_local_folder",
    "extract_document",
    "save_source",
    "get_source",
    "list_sources",
    "search_sources",
    "export_report",
    "stats",
    # Phase 3: evidence library
    "start_research_run",
    "save_evidence",
    "search_evidence",
    "register_claim",
    "attach_citation",
    "get_claim_citations",
    "get_research_history",
    "generate_report",
    "validate_report",
    # Phase 4: research planner / orchestrator
    "save_research_plan",
    "update_subquestion",
    "log_run_event",
    "start_followup_round",
    "finish_research_run",
    # Phase 5: image research and visual analysis
    "search_images",
    "analyze_image",
    # Phase 6: claim extraction, verification, and synthesis reports
    "extract_claims",
    "verify_claims",
    "analyze_conflicts",
    "generate_markdown_report",
    "get_report",
    # Phase 8: project memory, monitoring, and change detection
    "search_memory",
    "monitor_add_target",
    "monitor_check",
    "monitor_changes",
}

# Minimal DuckDuckGo-style page for the mocked search endpoint (local server).
MOCK_SEARCH_PAGE = b"""<!doctype html>
<html><body><div class="results">
  <div class="result">
    <h2 class="result__title"><a class="result__a"
      href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fmcp&amp;rut=x">Model Context Protocol</a></h2>
    <a class="result__snippet">A protocol for connecting tools to agents.</a>
  </div>
</div></body></html>"""


def _text(result: Any) -> str:
    for block in result.content:
        if getattr(block, "type", "") == "text":
            return block.text
    raise AssertionError(f"no text content in result: {result!r}")


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(_text(result))


class ServerProtocolTests(AsyncTempHomeCase):
    def _server_params(self, **env_overrides: str) -> StdioServerParameters:
        env = {**os.environ, **env_overrides}
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "agletras_mcp"],
            cwd=str(PROJECT_ROOT),
            env=env,
        )

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.sample = self.write_sample_pdf()

    async def test_protocol_roundtrip(self) -> None:
        async with Client(self._server_params()) as client:
            # 1. Catalog: all documented tools are exposed.
            catalog = await client.list_tools()
            names = {tool.name for tool in catalog.tools}
            self.assertLessEqual(EXPECTED_TOOLS, names)

            # 2. Input schemas survived the error-handling decorator.
            schemas = {tool.name: tool.input_schema for tool in catalog.tools}
            self.assertIn("content", schemas["save_source"].get("properties", {}))
            self.assertIn("source", schemas["extract_pdf"].get("properties", {}))
            self.assertIn("query", schemas["search_sources"].get("properties", {}))

            # 3. Storage tools.
            stats = _payload(await client.call_tool("stats", {}))
            self.assertTrue(stats["ok"])
            self.assertEqual(stats["total"], 0)

            saved = _payload(
                await client.call_tool(
                    "save_source",
                    {
                        "kind": "note",
                        "title": "MCP protocol test",
                        "content": "agletras protocol marker content",
                    },
                )
            )
            self.assertTrue(saved["ok"])
            source_id = saved["source_id"]
            self.assertTrue(saved["created"])

            fetched = _payload(await client.call_tool("get_source", {"source_id": source_id}))
            self.assertTrue(fetched["ok"])
            self.assertEqual(fetched["source"]["id"], source_id)

            found = _payload(await client.call_tool("search_sources", {"query": "protocol marker"}))
            self.assertTrue(found["ok"])
            self.assertGreaterEqual(found["count"], 1)

            listed = _payload(await client.call_tool("list_sources", {}))
            self.assertTrue(listed["ok"])
            self.assertEqual(listed["count"], 1)

            # 4. PDF extraction through the protocol.
            pdf_result = _payload(
                await client.call_tool("extract_pdf", {"source": str(self.sample), "save": True})
            )
            self.assertTrue(pdf_result["ok"])
            self.assertEqual(pdf_result["page_count"], 2)
            self.assertIn("AGLETRAS_SAMPLE_2026", pdf_result["text"])
            self.assertTrue(pdf_result["saved"])

            # 5. Report export.
            report = _payload(
                await client.call_tool("export_report", {"title": "Protocol Test Report"})
            )
            self.assertTrue(report["ok"])
            self.assertEqual(report["sources_included"], 2)
            self.assertTrue(report["path"].startswith(str(self.home / "reports")))
            self.assertIn("# Protocol Test Report", report["markdown"])

            # 6. Errors stay structured, not crashes.
            missing = _payload(
                await client.call_tool("extract_pdf", {"source": str(self.home / "missing.pdf")})
            )
            self.assertFalse(missing["ok"])
            self.assertEqual(missing["error"]["code"], "not_found")

            bad_kind = _payload(
                await client.call_tool(
                    "save_source", {"kind": "video", "title": "x", "content": "y"}
                )
            )
            self.assertFalse(bad_kind["ok"])
            self.assertEqual(bad_kind["error"]["code"], "invalid_argument")

    async def test_second_spawn_reuses_store(self) -> None:
        """A fresh server process sees data saved by an earlier one (same home)."""
        async with Client(self._server_params()) as client:
            await client.call_tool(
                "save_source",
                {"kind": "note", "title": "persisted", "content": "persistent bytes"},
            )
        async with Client(self._server_params()) as client:
            stats = _payload(await client.call_tool("stats", {}))
            self.assertEqual(stats["total"], 1)

    async def test_health_check_reports_local_status(self) -> None:
        async with Client(self._server_params()) as client:
            catalog = await client.list_tools()
            self.assertIn("health_check", {tool.name for tool in catalog.tools})
            payload = _payload(await client.call_tool("health_check", {}))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["server"], "agletras")
        self.assertTrue(payload["version"])
        self.assertEqual(payload["transport"], "stdio")
        self.assertEqual(
            sorted(payload["tools"]),
            sorted(EXPECTED_TOOLS),
            "health_check and tool_discovery must agree on the tool list",
        )
        self.assertTrue(payload["storage"]["ok"])
        self.assertIn("not tested", payload["network"])  # no connectivity claims

    async def test_health_check_degrades_without_storage(self) -> None:
        """An unusable database yields status=degraded, not a crash."""
        blocker = self.home / "blocked-file"  # a FILE, so mkdir of a child dir fails
        blocker.write_text("not a directory")
        env = {"AGLETRAS_DB_PATH": str(blocker / "inner" / "research.db")}
        async with Client(self._server_params(**env)) as client:
            payload = _payload(await client.call_tool("health_check", {}))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "degraded")
        self.assertFalse(payload["storage"]["ok"])
        self.assertIn("error", payload["storage"])

    async def test_search_web_through_protocol(self) -> None:
        """search_web end-to-end with the provider pointed at a local endpoint."""
        from tests.support import serve_http

        with serve_http({"/search": (200, "text/html; charset=utf-8", MOCK_SEARCH_PAGE)}) as base:
            env = {
                "AGLETRAS_SEARCH_PROVIDER": "duckduckgo",
                "AGLETRAS_SEARCH_ENDPOINT": base + "/search",
            }
            async with Client(self._server_params(**env)) as client:
                payload = _payload(await client.call_tool("search_web", {"query": "mcp servers", "max_results": 3}))
                empty = _payload(await client.call_tool("search_web", {"query": ""}))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["provider"], "duckduckgo")
        self.assertEqual(payload["count"], 1)
        result = payload["results"][0]
        self.assertEqual(
            set(result), {"title", "url", "snippet", "source"}
        )
        self.assertEqual(result["title"], "Model Context Protocol")
        self.assertEqual(result["url"], "https://example.com/mcp")
        self.assertIn("protocol for connecting tools", result["snippet"])
        self.assertEqual(result["source"], "duckduckgo")
        self.assertFalse(empty["ok"])
        self.assertEqual(empty["error"]["code"], "invalid_argument")

    async def test_search_images_protocol_validation(self) -> None:
        """search_images is registered and rejects an empty query."""
        async with Client(self._server_params()) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            self.assertIn("search_images", names)
            empty = _payload(await client.call_tool("search_images", {"query": ""}))
        self.assertFalse(empty["ok"])
        self.assertEqual(empty["error"]["code"], "invalid_argument")

    async def test_analyze_image_protocol_validation(self) -> None:
        """analyze_image is registered and rejects a missing image input."""
        async with Client(self._server_params()) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            self.assertIn("analyze_image", names)
            empty = _payload(await client.call_tool("analyze_image", {}))
        self.assertFalse(empty["ok"])
        self.assertEqual(empty["error"]["code"], "invalid_argument")

    async def test_fetch_url_validation_through_protocol(self) -> None:
        """Malformed inputs produce structured errors over the wire."""
        async with Client(self._server_params()) as client:
            negative = _payload(
                await client.call_tool("fetch_url", {"url": "https://example.com", "max_chars": -5})
            )
            ftp = _payload(await client.call_tool("fetch_url", {"url": "ftp://example.com/x"}))
        self.assertFalse(negative["ok"])
        self.assertEqual(negative["error"]["code"], "invalid_argument")
        self.assertFalse(ftp["ok"])
        self.assertEqual(ftp["error"]["code"], "invalid_argument")

    async def test_fetch_url_ssrf_blocked_through_protocol(self) -> None:
        """A strict-mode subprocess refuses loopback targets: SSRF guard works
        through the real protocol (no bypass), while permissive subprocesses
        can still fetch local servers (developer/test mode)."""
        from tests.support import serve_http

        with serve_http({"/page": (200, "text/html; charset=utf-8", MOCK_SEARCH_PAGE)}) as base:
            async with Client(self._server_params(AGLETRAS_ALLOW_PRIVATE_HOSTS="0")) as client:
                blocked = _payload(await client.call_tool("fetch_url", {"url": base + "/page"}))
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"]["code"], "blocked_host")

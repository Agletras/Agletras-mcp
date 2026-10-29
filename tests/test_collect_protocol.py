"""Stdio protocol tests for the Phase 2 collection tools.

Spawns the real server as a subprocess (exactly how OpenCode launches it) and
exercises collect_sources / collect_local_folder / extract_document end to end,
plus the Phase 1 storage keys that must stay intact.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from tests.support import (
    PROJECT_ROOT,
    AsyncTempHomeCase,
    build_docx,
    build_xlsx,
    serve_http,
)

NEW_TOOLS = {"collect_sources", "collect_local_folder", "extract_document"}

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


class CollectProtocolTests(AsyncTempHomeCase):
    def _server_params(self, **env_overrides: str) -> StdioServerParameters:
        env = {**os.environ, **env_overrides}
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "agletras_mcp"],
            cwd=str(PROJECT_ROOT),
            env=env,
        )

    def _write_fixtures(self) -> dict[str, Path]:
        paths = {
            "md": self.home / "alpha.md",
            "docx": self.home / "report.docx",
            "xlsx": self.home / "sheet.xlsx",
            "csv": self.home / "table.csv",
        }
        paths["md"].write_text("# Alpha Doc\n\nalpha protocol words\n", encoding="utf-8")
        paths["docx"].write_bytes(
            build_docx(title="Protocol Report", paragraphs=[(None, "docx protocol body text")])
        )
        paths["xlsx"].write_bytes(
            build_xlsx(
                [("Plan", [["task", "status"], ["extract", "done"]])],
                title="Protocol Sheet",
            )
        )
        paths["csv"].write_text("name,role\nada,pioneer\n", encoding="utf-8")
        return paths

    async def test_tool_catalog_includes_phase2_tools(self) -> None:
        async with Client(self._server_params()) as client:
            catalog = await client.list_tools()
        names = {tool.name for tool in catalog.tools}
        self.assertLessEqual(NEW_TOOLS, names)

    async def test_collect_sources_supported_documents(self) -> None:
        paths = self._write_fixtures()
        arg = "\n".join(str(path) for path in paths.values())
        async with Client(self._server_params()) as client:
            payload = _payload(
                await client.call_tool("collect_sources", {"sources": arg, "save": True})
            )
        self.assertTrue(payload["ok"])
        summary = payload["summary"]
        self.assertEqual(summary["requested"], 4)
        self.assertEqual(summary["succeeded"], 4)
        self.assertEqual(summary["saved"], 4)
        self.assertEqual(summary["duplicates"], 0)
        by_type = {entry["source_type"]: entry for entry in payload["results"]}
        self.assertEqual(set(by_type), {"markdown", "docx", "xlsx", "csv"})
        for entry in payload["results"]:
            self.assertEqual(entry["extraction_status"], "success")
            self.assertTrue(entry["source_id"])
            self.assertTrue(entry["checksum"])
            self.assertTrue(entry["retrieved_at"])
        self.assertIn("alpha protocol words", by_type["markdown"]["excerpt"])
        self.assertIn("docx protocol body text", by_type["docx"]["excerpt"])
        self.assertEqual(by_type["xlsx"]["title"], "Protocol Sheet")
        self.assertIn("pioneer", by_type["csv"]["excerpt"])

    async def test_collect_sources_dry_run_does_not_persist(self) -> None:
        solo = self.home / "solo.md"
        solo.write_text("# Solo\n\nsolo dry run words\n", encoding="utf-8")
        async with Client(self._server_params()) as client:
            payload = _payload(
                await client.call_tool(
                    "collect_sources", {"sources": str(solo), "save": False}
                )
            )
            listed = _payload(await client.call_tool("list_sources", {}))
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["results"][0]["source_id"])
        self.assertIsNone(payload["results"][0]["created"])
        self.assertEqual(listed["count"], 0)

    async def test_collect_sources_failure_isolation(self) -> None:
        good = self.home / "good.md"
        good.write_text("# Good\n\ngood words present\n", encoding="utf-8")
        missing = self.home / "gone.md"
        async with Client(self._server_params()) as client:
            payload = _payload(
                await client.call_tool(
                    "collect_sources",
                    {"sources": f"{good}\n{missing}", "save": True},
                )
            )
        by_label = {entry["label"]: entry for entry in payload["results"]}
        self.assertEqual(by_label[str(good)]["extraction_status"], "success")
        self.assertEqual(by_label[str(missing)]["extraction_status"], "failed")
        self.assertEqual(by_label[str(missing)]["error_code"], "not_found")
        self.assertEqual(by_label[str(missing)]["label"], str(missing))

    async def test_collect_sources_search_snippets_only(self) -> None:
        with serve_http(
            {"/search": (200, "text/html; charset=utf-8", MOCK_SEARCH_PAGE)}
        ) as base:
            async with Client(
                self._server_params(AGLETRAS_SEARCH_ENDPOINT=base + "/search")
            ) as client:
                payload = _payload(
                    await client.call_tool(
                        "collect_sources", {"search": "mcp", "search_limit": 1}
                    )
                )
        entry = payload["results"][0]
        self.assertEqual(entry["source_type"], "search_result")
        self.assertEqual(entry["extraction_status"], "success")
        self.assertTrue(entry["metadata"]["snippet_only"])
        self.assertEqual(entry["title"], "Model Context Protocol")
        self.assertEqual(entry["original_url"], "https://example.com/mcp")
        self.assertLess(entry["chars"], 500)

    async def test_extract_document_save_roundtrip_keeps_phase1_keys(self) -> None:
        paths = self._write_fixtures()
        async with Client(self._server_params()) as client:
            extracted = _payload(
                await client.call_tool(
                    "extract_document",
                    {"source": str(paths["docx"]), "save": True, "notes": "protocol note"},
                )
            )
            self.assertTrue(extracted["ok"])
            self.assertEqual(extracted["extraction_status"], "success")
            self.assertTrue(extracted["saved"])
            source_id = extracted["source_id"]
            phase1 = _payload(await client.call_tool("get_source", {"source_id": source_id}))
        record = phase1["source"]
        # Phase 1 keys stay present and meaningful next to the unified fields.
        for key in ("kind", "title", "content", "notes", "checksum"):
            self.assertIn(key, record)
        self.assertEqual(record["kind"], "doc")
        self.assertIn("docx protocol body text", record["content"])
        self.assertEqual(record["notes"], "protocol note")
        self.assertEqual(record["source_type"], "docx")

    async def test_extract_document_url_and_errors(self) -> None:
        html = (
            b"<html><head><title>Doc Page</title></head>"
            b"<body><p>page body protocol text</p></body></html>"
        )
        with serve_http({"/doc": (200, "text/html; charset=utf-8", html)}) as base:
            async with Client(self._server_params()) as client:
                fetched = _payload(
                    await client.call_tool("extract_document", {"source": base + "/doc"})
                )
                bad_scheme = _payload(
                    await client.call_tool("extract_document", {"source": "ftp://x/y"})
                )
        self.assertTrue(fetched["ok"])
        self.assertEqual(fetched["source_type"], "web")
        self.assertEqual(fetched["title"], "Doc Page")
        self.assertIn("page body protocol text", fetched["excerpt"])
        self.assertFalse(bad_scheme["ok"])
        self.assertEqual(bad_scheme["error"]["code"], "invalid_argument")

    async def test_list_sources_source_type_filter(self) -> None:
        paths = self._write_fixtures()
        arg = "\n".join(str(path) for path in paths.values())
        async with Client(self._server_params()) as client:
            await client.call_tool("collect_sources", {"sources": arg, "save": True})
            everything = _payload(await client.call_tool("list_sources", {}))
            csvs = _payload(await client.call_tool("list_sources", {"source_type": "csv"}))
        self.assertEqual(everything["count"], 4)
        self.assertEqual(csvs["count"], 1)
        self.assertEqual(csvs["sources"][0]["source_type"], "csv")

    async def test_collect_local_folder_reports_skips(self) -> None:
        docs = self.home / "docs"
        (docs / "sub").mkdir(parents=True)
        (docs / "a.md").write_text("# A\n\nalpha file words\n", encoding="utf-8")
        (docs / "keep.tmp").write_text("temp", encoding="utf-8")
        (docs / "prog.py").write_text("print('no')", encoding="utf-8")
        (docs / "sub" / "deep.md").write_text("# Deep\n\ndeep file words\n", encoding="utf-8")
        async with Client(self._server_params()) as client:
            payload = _payload(
                await client.call_tool(
                    "collect_local_folder", {"folder": str(docs), "save": False}
                )
            )
        self.assertTrue(payload["ok"])
        summary = payload["summary"]
        self.assertEqual(summary["files_picked"], 2)
        self.assertEqual(summary["files_skipped"], 2)
        self.assertFalse(summary["scan_truncated"])
        statuses = [entry["extraction_status"] for entry in payload["results"]]
        self.assertEqual(statuses, ["success", "success"])
        self.assertIsNone(payload["results"][0]["source_id"])
        reasons = {entry["reason"] for entry in payload["skipped"]}
        self.assertEqual(
            reasons, {"temporary_file", "unsupported_extension"}
        )

    async def test_collect_local_folder_outside_roots_denied(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            async with Client(self._server_params()) as client:
                payload = _payload(
                    await client.call_tool(
                        "collect_local_folder",
                        {"folder": outside, "extensions": "md,txt,csv,docx,xlsx"},
                    )
                )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "access_denied")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Tests for the SQLite source store."""

from __future__ import annotations

from agletras_mcp import storage
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import TempHomeCase


class StorageTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def test_save_and_get_roundtrip(self) -> None:
        source_id, created = storage.save_source(
            kind="url",
            title="Example Domain",
            url="https://example.com",
            content="Example Domain text body",
            notes="phase check",
            settings=self.settings,
        )
        self.assertTrue(created)
        record = storage.get_source(source_id, settings=self.settings)
        assert record is not None
        self.assertEqual(record["title"], "Example Domain")
        self.assertEqual(record["kind"], "url")
        self.assertEqual(record["notes"], "phase check")
        self.assertIn("Example Domain text", record["content"])
        self.assertEqual(len(record["sha256"]), 64)
        self.assertTrue(record["created_at"].startswith("20"))

    def test_duplicate_content_is_deduplicated(self) -> None:
        first, created_first = storage.save_source(
            kind="note", title="One", content="same bytes", settings=self.settings
        )
        second, created_second = storage.save_source(
            kind="note", title="Two", content="same bytes", settings=self.settings
        )
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first, second)
        self.assertEqual(storage.stats(settings=self.settings)["total"], 1)

    def test_list_sources_filters_and_orders_newest_first(self) -> None:
        storage.save_source(kind="note", title="first", content="aaa", settings=self.settings)
        storage.save_source(kind="pdf", title="second", content="bbb", settings=self.settings)
        storage.save_source(kind="pdf", title="third", content="ccc", settings=self.settings)
        newest = storage.list_sources(settings=self.settings)
        self.assertEqual([record["title"] for record in newest], ["third", "second", "first"])
        pdfs = storage.list_sources(kind="pdf", settings=self.settings)
        self.assertEqual([record["title"] for record in pdfs], ["third", "second"])
        page = storage.list_sources(limit=1, offset=1, settings=self.settings)
        self.assertEqual([record["title"] for record in page], ["second"])

    def test_search_matches_title_and_content(self) -> None:
        storage.save_source(
            kind="note",
            title="Kernel notes",
            content="the quick brown fox",
            settings=self.settings,
        )
        storage.save_source(
            kind="note",
            title="Other",
            content="unrelated material",
            settings=self.settings,
        )
        by_title = storage.search_sources("kernel", settings=self.settings)
        self.assertEqual(len(by_title), 1)
        by_content = storage.search_sources("brown fox", settings=self.settings)
        self.assertEqual(len(by_content), 1)
        self.assertEqual(storage.search_sources("missing", settings=self.settings), [])

    def test_search_escapes_like_wildcards(self) -> None:
        storage.save_source(kind="note", title="Percent", content="100% done", settings=self.settings)
        storage.save_source(kind="note", title="Plain", content="nothing special", settings=self.settings)
        results = storage.search_sources("%", settings=self.settings)
        self.assertEqual([record["title"] for record in results], ["Percent"])

    def test_get_missing_source_returns_none(self) -> None:
        self.assertIsNone(storage.get_source(999, settings=self.settings))

    def test_invalid_kind_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            storage.save_source(kind="video", title="x", content="y", settings=self.settings)
        self.assertEqual(ctx.exception.code, "invalid_argument")
        with self.assertRaises(ToolError):
            storage.list_sources(kind="video", settings=self.settings)

    def test_empty_content_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            storage.save_source(kind="note", title="x", content="   ", settings=self.settings)
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_stats_by_kind(self) -> None:
        storage.save_source(kind="url", title="a", content="1", settings=self.settings)
        storage.save_source(kind="url", title="b", content="2", settings=self.settings)
        storage.save_source(kind="pdf", title="c", content="3", settings=self.settings)
        result = storage.stats(settings=self.settings)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["by_kind"], {"pdf": 1, "url": 2})
        self.assertGreater(result["db_size_bytes"], 0)
        self.assertTrue(result["db_path"].endswith("research.db"))

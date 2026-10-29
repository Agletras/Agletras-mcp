"""Tests for the unified source record and text/date helpers (Phase 2)."""

from __future__ import annotations

import unittest

from agletras_mcp.source_record import (
    SourceRecord,
    collapse_inline,
    finalize_text,
    normalize_structured_text,
    parse_publication_date,
    shape_hash,
)


class NormalizeStructuredTextTests(unittest.TestCase):
    def test_normalizes_newlines_and_collapses_blank_runs(self) -> None:
        text = "\ufeffHeading\r\n\r\n\r\ncode   line \r\n\r\n\r\n\r\nmore"
        result = normalize_structured_text(text)
        self.assertEqual(result, "Heading\n\ncode   line\n\nmore")

    def test_preserves_heading_table_and_code_structure(self) -> None:
        markdown = "# Title\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n```python\nprint('hi')\n```\n"
        result = normalize_structured_text(markdown)
        self.assertIn("# Title", result)
        self.assertIn("| a | b |", result)
        self.assertIn("print('hi')", result)
        self.assertNotIn("\r", result)

    def test_collapse_inline(self) -> None:
        self.assertEqual(collapse_inline("  a \t b  "), "a b")


class ShapeHashTests(unittest.TestCase):
    def test_identical_word_sequences_have_identical_shape_only(self) -> None:
        base = "The quick brown fox jumps over the lazy dog."
        variant = "the QUICK brown-fox jumps over the lazy dog"
        self.assertIsNotNone(shape_hash(base))
        self.assertEqual(shape_hash(base), shape_hash(variant))
        self.assertNotEqual(shape_hash(base), shape_hash("a totally different text"))

    def test_empty_text_has_no_shape(self) -> None:
        self.assertIsNone(shape_hash(""))
        self.assertIsNone(shape_hash("   ??  ##"))


class PublicationDateTests(unittest.TestCase):
    def test_parses_common_metadata_formats_to_iso(self) -> None:
        cases = {
            "2026-02-03T10:00:00Z": "2026-02-03T10:00:00+00:00",
            "2026-02-03T05:30:00+05:30": "2026-02-03T05:30:00+05:30",
            "2026-02-03": "2026-02-03T00:00:00+00:00",
            "February 3, 2026": "2026-02-03T00:00:00+00:00",
        }
        for raw, expected in cases.items():
            self.assertEqual(parse_publication_date(raw), expected, msg=raw)

    def test_unknown_dates_stay_unknown(self) -> None:
        for raw in ("", "   ", "not a date at all", "x" * 80, None):
            self.assertIsNone(parse_publication_date(raw))


class SourceRecordTests(unittest.TestCase):
    def test_status_and_error_fields(self) -> None:
        record = SourceRecord(source_type="web", title="t")
        record.set_failure("not_found", "missing", status="failed")
        self.assertEqual(record.extraction_status, "failed")
        self.assertEqual(record.error_code, "not_found")
        self.assertTrue(record.warnings == [])

    def test_kind_mapping(self) -> None:
        self.assertEqual(SourceRecord(source_type="web").kind(), "url")
        self.assertEqual(SourceRecord(source_type="search_result").kind(), "search_result")
        self.assertEqual(SourceRecord(source_type="pdf").kind(), "pdf")
        self.assertEqual(SourceRecord(source_type="docx").kind(), "doc")
        self.assertEqual(SourceRecord(source_type="csv").kind(), "doc")

    def test_finalize_text_sets_checksums_and_cap(self) -> None:
        record = SourceRecord(source_type="text", extracted_text="hello world content")
        record.extraction_status = "success"
        finalize_text(record, max_chars=5)
        self.assertEqual(len(record.extracted_text), 5)
        self.assertEqual(record.extraction_status, "partial")
        self.assertTrue(record.warnings)
        self.assertEqual(len(record.checksum or ""), 64)
        self.assertEqual(len(record.normalized_sha256 or ""), 64)

    def test_finalize_text_leaves_failed_status_alone(self) -> None:
        record = SourceRecord(source_type="text", extracted_text="content")
        record.extraction_status = "success"
        finalize_text(record, max_chars=0)  # no cap
        self.assertEqual(record.extraction_status, "success")

    def test_retrieval_time_is_separate_from_publication(self) -> None:
        record = SourceRecord(source_type="web")
        self.assertTrue(record.retrieved_at.startswith("20"))
        self.assertIsNone(record.publication_date)

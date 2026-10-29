"""Tests for source-identity canonicalization (Phase 3)."""

from __future__ import annotations

import unittest
from pathlib import Path

from agletras_mcp.canonurl import canonicalize_url, identity_kind, normalize_local_path


class CanonicalizeUrlTests(unittest.TestCase):
    def test_stable_identity_normalizations(self) -> None:
        cases = {
            "HTTPS://Example.COM/Path?b=2&a=1": "https://example.com/Path?a=1&b=2",
            "https://example.com": "https://example.com/",
            "https://example.com:443/x": "https://example.com/x",
            "http://example.com:80/x": "http://example.com/x",
            "https://example.com/x#section": "https://example.com/x",
            "https://example.com/x?utm_source=news&utm_medium=email": "https://example.com/x",
            "https://example.com/x?a=1&fbclid=abc&b=2": "https://example.com/x?a=1&b=2",
        }
        for raw, expected in cases.items():
            with self.subTest(url=raw):
                self.assertEqual(canonicalize_url(raw), expected)

    def test_distinct_resources_stay_distinct(self) -> None:
        first = canonicalize_url("https://example.com/a")
        second = canonicalize_url("https://example.com/a/")
        third = canonicalize_url("https://example.com/b")
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, third)
        # Query parameter order is not meaningful; the pair is sorted.
        self.assertEqual(
            canonicalize_url("https://example.com/?q=1&p=2"),
            canonicalize_url("https://example.com/?p=2&q=1"),
        )

    def test_unusable_urls_return_none(self) -> None:
        for raw in (
            "",
            "   ",
            "ftp://example.com/file",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "https://",
            "https://user:secret@example.com/private",
            "http://example.com:notaport/x",
        ):
            with self.subTest(url=raw):
                self.assertIsNone(canonicalize_url(raw))

    def test_blank_query_values_are_preserved(self) -> None:
        self.assertEqual(
            canonicalize_url("https://example.com/x?flag=&other=1"),
            "https://example.com/x?flag=&other=1",
        )


class NormalizeLocalPathTests(unittest.TestCase):
    def test_relative_and_absolute_agree(self) -> None:
        absolute = normalize_local_path("/tmp/../tmp/example.txt")
        self.assertEqual(absolute, "/tmp/example.txt")
        self.assertTrue(Path(absolute).is_absolute())

    def test_symlink_resolution_points_at_one_identity(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.txt"
            target.write_text("content")
            link = Path(tmp) / "link.txt"
            link.symlink_to(target)
            self.assertEqual(normalize_local_path(link), normalize_local_path(target))

    def test_empty_path_is_empty_string(self) -> None:
        self.assertEqual(normalize_local_path(""), "")
        self.assertEqual(normalize_local_path("   "), "")


class IdentityKindTests(unittest.TestCase):
    def test_identity_kind_precedence(self) -> None:
        self.assertEqual(identity_kind({"canonical_url": "https://example.com/"}), "url")
        self.assertEqual(identity_kind({"path": "/tmp/x.txt"}), "path")
        self.assertEqual(identity_kind({}), "note")


if __name__ == "__main__":
    unittest.main()

"""Tests for markdown/plain-text extraction (Phase 2)."""

from __future__ import annotations

import os
from pathlib import Path

from agletras_mcp import textdoc
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import TempHomeCase

MARKDOWN = """# Main Heading

Some **markdown** text with a table:

| col a | col b |
| --- | --- |
| 1 | 2 |

```python
print("code survives")
```


## Sub Heading
More content.
"""


class MarkdownTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def test_structure_preserved_and_title_found(self) -> None:
        path = self.home / "doc.md"
        path.write_text(MARKDOWN, encoding="utf-8")
        result = textdoc.extract_text_document(path, "markdown", settings=self.settings)
        self.assertEqual(result["title"], "Main Heading")
        text = result["text"]
        self.assertIn("# Main Heading", text)
        self.assertIn("| col a | col b |", text)
        self.assertIn('print("code survives")', text)
        self.assertIn("## Sub Heading", text)
        # blank-line runs collapse but single blank lines (structure) remain
        self.assertNotIn("\n\n\n\n", text)
        self.assertEqual(result["metadata"]["headings"], 2)
        self.assertEqual(result["metadata"]["code_fences"], 1)
        self.assertEqual(result["metadata"]["encoding"], "utf-8")

    def test_no_heading_title_falls_back_to_empty(self) -> None:
        path = self.home / "doc.md"
        path.write_text("just words, no heading", encoding="utf-8")
        result = textdoc.extract_text_document(path, "markdown", settings=self.settings)
        self.assertEqual(result["title"], "")
        self.assertEqual(result["metadata"]["headings"], 0)

    def test_chars_cap_truncates_to_partial(self) -> None:
        os.environ["AGLETRAS_DOC_MAX_CHARS"] = "20"
        try:
            path = self.home / "doc.md"
            path.write_text(MARKDOWN, encoding="utf-8")
            result = textdoc.extract_text_document(path, "markdown", settings=load_settings())
            self.assertTrue(result["truncated"])
            self.assertEqual(len(result["text"]), 20)
            self.assertIn("truncated", " ".join(result["warnings"]))
        finally:
            os.environ.pop("AGLETRAS_DOC_MAX_CHARS", None)


class TextTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def test_plain_text_extracted_with_encoding(self) -> None:
        path = self.home / "notes.txt"
        path.write_bytes("plain notes\ncafé latte\n".encode("utf-8"))
        result = textdoc.extract_text_document(path, "text", settings=self.settings)
        self.assertIn("plain notes", result["text"])
        self.assertIn("café latte", result["text"])
        self.assertEqual(result["metadata"]["encoding"], "utf-8")
        # .txt documents carry no invented title
        self.assertEqual(result["title"], "")

    def test_latin1_fallback_keeps_bytes_meaning(self) -> None:
        path = self.home / "legacy.rst"
        path.write_bytes(b"caf\xe9 latte")  # latin-1 é, invalid UTF-8
        result = textdoc.extract_text_document(path, "text", settings=self.settings)
        self.assertIn("café latte", result["text"])
        self.assertEqual(result["metadata"]["encoding"], "latin-1")

    def test_empty_text_file_reports_empty_file(self) -> None:
        path = self.home / "empty.txt"
        path.write_bytes(b"   \n  ")
        with self.assertRaises(ToolError) as ctx:
            textdoc.extract_text_document(path, "text", settings=self.settings)
        self.assertEqual(ctx.exception.code, "empty_file")


if __name__ == "__main__":  # pragma: no cover
    __import__("unittest").main()

"""Tests for DOCX/XLSX/CSV extraction (fixtures built locally, fully hermetic)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from agletras_mcp import officedoc
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import Cell, TempHomeCase, build_docx, build_xlsx


class DocxTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def write_docx(self, name: str = "t.docx", **kwargs) -> Path:
        path = self.home / name
        path.write_bytes(build_docx(**kwargs))
        return path

    def test_paragraphs_headings_and_tables(self) -> None:
        path = self.write_docx(
            paragraphs=[("Heading1", "Sample Document"), (None, "Body text."), (None, "")],
            tables=[[["Name", "Value"], ["alpha", "12"]]],
        )
        result = officedoc.extract_docx(path, settings=self.settings)
        text = result["text"]
        self.assertIn("# Sample Document", text)
        self.assertIn("Body text.", text)
        self.assertIn("| Name | Value |", text)
        self.assertIn("| alpha | 12 |", text)
        self.assertEqual(result["metadata"]["tables"], 1)
        self.assertEqual(result["metadata"]["table_rows"], 2)
        self.assertEqual(result["metadata"]["paragraphs"], 3)
        self.assertFalse(result["truncated"])

    def test_core_properties_title_used(self) -> None:
        path = self.write_docx(
            paragraphs=[(None, "text")], title="Core Title", creator="A. Author"
        )
        result = officedoc.extract_docx(path, settings=self.settings)
        self.assertEqual(result["title"], "Core Title")
        core = result["metadata"]["core_properties"]
        self.assertEqual(core.get("title"), "Core Title")
        self.assertEqual(core.get("creator"), "A. Author")

    def test_nested_table_like_structure_does_not_crash(self) -> None:
        path = self.write_docx(paragraphs=[("Heading2", "Two level")], tables=[])
        result = officedoc.extract_docx(path, settings=self.settings)
        self.assertIn("## Two level", result["text"])

    def test_bad_zip_is_malformed(self) -> None:
        path = self.home / "broken.docx"
        path.write_bytes(b"%PK not a real zip\x01\x02")
        with self.assertRaises(ToolError) as ctx:
            officedoc.extract_docx(path, settings=self.settings)
        self.assertEqual(ctx.exception.code, "malformed_file")

    def test_zip_without_document_is_unsupported(self) -> None:
        path = self.home / "emptyzip.docx"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("some/other.xml", "<x/>")
        path.write_bytes(buffer.getvalue())
        with self.assertRaises(ToolError) as ctx:
            officedoc.extract_docx(path, settings=self.settings)
        self.assertEqual(ctx.exception.code, "unsupported_content")

    def test_empty_docx_reports_empty_file(self) -> None:
        path = self.write_docx(paragraphs=[])
        with self.assertRaises(ToolError) as ctx:
            officedoc.extract_docx(path, settings=self.settings)
        self.assertEqual(ctx.exception.code, "empty_file")


class XlsxTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def write_xlsx(self, sheets, name: str = "book.xlsx", **kwargs) -> Path:
        path = self.home / name
        path.write_bytes(build_xlsx(sheets, **kwargs))
        return path

    def test_sheets_cells_shared_strings_and_types(self) -> None:
        path = self.write_xlsx(
            [
                (
                    "Data",
                    [
                        ["name", "value", "ratio", None, Cell("inline", "inline entry"), True],
                        ["alpha", 1, 2.5, None, Cell("inline", "again"), False],
                    ],
                ),
                ("Notes", [["single"]]),
            ]
        )
        result = officedoc.extract_xlsx(path, settings=self.settings)
        text = result["text"]
        self.assertIn("## Sheet: Data", text)
        self.assertIn("## Sheet: Notes", text)
        self.assertIn("inline entry", text)          # inlineStr supported
        self.assertIn("TRUE", text)                  # boolean cells
        self.assertIn("2.5", text)                   # numeric cells
        # gap cells (None) are preserved as empty positions: 6-wide row renders 6 columns
        first_row = text.splitlines()[1]
        self.assertEqual(first_row.count("|"), 7, msg=first_row)
        self.assertEqual(result["metadata"]["sheets"][0]["rows"], 2)
        self.assertEqual(result["metadata"]["sheets"][1]["name"], "Notes")

    def test_document_title_from_core_properties(self) -> None:
        path = self.write_xlsx([("Sheet1", [["x", "y"]])], title="Workbook Title")
        result = officedoc.extract_xlsx(path, settings=self.settings)
        self.assertEqual(result["title"], "Workbook Title")

    def test_cell_cap_warns_and_keeps_partial(self) -> None:
        import os
        os.environ["AGLETRAS_OFFICE_MAX_CELLS"] = "3"
        try:
            path = self.write_xlsx([("Wide", [[1, 2, 3], [4, 5, 6]])])
            result = officedoc.extract_xlsx(path, settings=load_settings())
            self.assertTrue(result["warnings"])
            self.assertIn("cap reached", result["warnings"][0])
            self.assertTrue(result["text"])
        finally:
            os.environ.pop("AGLETRAS_OFFICE_MAX_CELLS", None)

    def test_not_an_xlsx_zip(self) -> None:
        path = self.home / "broken.xlsx"
        path.write_bytes(b"PK\x03\x04 garbage no real zip structure")
        with self.assertRaises(ToolError) as ctx:
            officedoc.extract_xlsx(path, settings=self.settings)
        self.assertEqual(ctx.exception.code, "malformed_file")

    def test_zip_without_workbook_is_malformed(self) -> None:
        path = self.home / "noworkbook.xlsx"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("docProps/app.xml", "<x/>")
        path.write_bytes(buffer.getvalue())
        with self.assertRaises(ToolError) as ctx:
            officedoc.extract_xlsx(path, settings=self.settings)
        self.assertEqual(ctx.exception.code, "malformed_file")


class CsvTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def extract_csv(self, text: str, **kwargs) -> dict:
        return officedoc.render_csv_text(text, settings=self.settings, **kwargs)

    def test_default_comma_headers_and_rows(self) -> None:
        result = self.extract_csv("name,value,alpha\none,1,x\ntwo,2,y\n")
        self.assertEqual(result["metadata"]["delimiter"], ",")
        self.assertEqual(result["metadata"]["header_columns"], 3)
        self.assertEqual(result["metadata"]["data_rows"], 2)
        self.assertIn("| name | value | alpha |", result["text"])
        self.assertIn("| one | 1 | x |", result["text"])

    def test_sniffed_delimiters(self) -> None:
        for payload, expected in [
            ("a;b\n1;2\n", ";"),
            ("a\tb\n1\t2\n", "\t"),
            ("a|b\n1|2\n", "|"),
        ]:
            self.assertEqual(self.extract_csv(payload)["metadata"]["delimiter"], expected, msg=payload)

    def test_quoted_fields_kept_together(self) -> None:
        result = self.extract_csv('name,note\n"Row, with comma",ok\n')
        self.assertIn("Row, with comma", result["text"])

    def test_ragged_rows_warn_but_are_kept(self) -> None:
        result = self.extract_csv("a,b\n1\n2,3\n")
        self.assertTrue(any("different column count" in warning for warning in result["warnings"]))
        self.assertIn("| 1 |", result["text"])

    def test_empty_csv_is_empty_file(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.extract_csv("   \n  ")
        self.assertEqual(ctx.exception.code, "empty_file")

    def test_only_header_is_not_empty(self) -> None:
        result = self.extract_csv("a,b\n")
        self.assertEqual(result["metadata"]["data_rows"], 0)
        self.assertIn("| a | b |", result["text"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

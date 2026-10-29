"""Office document text extraction using the standard library only.

DOCX and XLSX files are ZIP containers full of plain XML, so ``zipfile`` +
``xml.etree.ElementTree`` cover the required subset — paragraphs and tables
(DOCX), worksheet names and cell values (XLSX), headers and rows (CSV) —
without adding ``python-docx``/``openpyxl`` (see README: the Phase 0
dependency plan keeps the zero-dependency backbone where practical).

Security notes:
- documents are never executed: XML is parsed text-only; VBA/macro parts
  (``vbaProject.bin``), upstream feeds, and external links are ignored;
- formulas are never evaluated — only the last cached value ``<v>`` is read;
- parsing is bounded (cell caps) so a hostile file cannot produce unbounded
  output.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .config import Settings, load_settings
from .errors import ToolError
from .source_record import collapse_inline, normalize_structured_text

LOGGER = logging.getLogger(__name__)

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_DC_NS = "{http://purl.org/dc/elements/1.1/}"
_DCT_NS = "{http://purl.org/dc/terms/}"
_CP_NS = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"

_CELL_TEXT_CAP = 2000
_HEADING_RE = re.compile(r"^Heading(\d+)$", re.IGNORECASE)
_COL_RE = re.compile(r"^([A-Z]+)(\d+)$")


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def _docx_paragraph_text(paragraph: ET.Element) -> tuple[str, str]:
    """Return ``(text, style)`` for one ``w:p`` element."""
    style = ""
    style_node = paragraph.find(f"{_W_NS}pPr/{_W_NS}pStyle")
    if style_node is not None:
        style = style_node.get(f"{_W_NS}val", "") or ""
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{_W_NS}t" and node.text:
            parts.append(node.text)
        elif node.tag in (f"{_W_NS}tab", f"{_W_NS}br", f"{_W_NS}cr"):
            parts.append(" ")
    return collapse_inline("".join(parts)), style


def _docx_cell_text(cell: ET.Element, settings: Settings) -> str:
    """Inline text of one table cell (its paragraphs joined with ' ')."""
    texts: list[str] = []
    for paragraph in cell.findall(f"{_W_NS}p"):
        text, _style = _docx_paragraph_text(paragraph)
        if text:
            texts.append(text)
    if not texts:  # fallback: stray w:t without paragraph structure
        raw = collapse_inline("".join(node.text or "" for node in cell.iter() if node.tag == f"{_W_NS}t"))
        if raw:
            texts.append(raw)
    joined = " ".join(texts)
    return joined[: _CELL_TEXT_CAP] + ("…" if len(joined) > _CELL_TEXT_CAP else "")


def _docx_table_rows(table: ET.Element, settings: Settings) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.findall(f"{_W_NS}tr"):
        cells = [_docx_cell_text(cell, settings) for cell in row.findall(f"{_W_NS}tc")]
        if cells:
            rows.append(cells)
    return rows


def _docx_heading_prefix(style: str) -> str:
    match = _HEADING_RE.match(style)
    if match:
        return "#" * min(int(match.group(1)), 6) + " "
    if style.lower().startswith("heading"):
        return "#### "
    return ""


def _read_core_properties(archive: zipfile.ZipFile) -> dict[str, str]:
    """dc:title / creator / modified-style core metadata (no date invention)."""
    core: dict[str, str] = {}
    try:
        data = archive.read("docProps/core.xml")
    except KeyError:
        return core
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        LOGGER.debug("core.xml unparseable: %s", exc)
        return core
    for key, tag in (("title", f"{_DC_NS}title"), ("creator", f"{_DC_NS}creator")):
        node = root.find(tag)
        if node is not None and node.text and node.text.strip():
            core[key] = node.text.strip()
    created = root.find(f"{_DCT_NS}created")
    if created is None:
        created = root.find(f"{_CP_NS}created")
    if created is not None and created.text and created.text.strip():
        core["created"] = created.text.strip()
    return core


def _docx_render_paragraphs(body: ET.Element, settings: Settings, parts: list[str], tables: list[list[list[str]]]) -> int:
    """Emit paragraphs/tables in document order; nested sdt blocks are recursed."""
    paragraphs = 0
    for child in body:
        tag = child.tag
        if tag == f"{_W_NS}p":
            text, style = _docx_paragraph_text(child)
            prefix = _docx_heading_prefix(style)
            if prefix and text:
                parts.append(f"{prefix}{text}")
            elif text:
                parts.append(f"- {text}" if child.find(f"{_W_NS}pPr/{_W_NS}numPr") is not None else text)
            paragraphs += 1
        elif tag == f"{_W_NS}tbl":
            rows = _docx_table_rows(child, settings)
            if rows:
                columns = max(len(row) for row in rows)
                parts.append(f"[table {len(rows)} rows x {columns} cols]")
                for row in rows:
                    parts.append("| " + " | ".join(row) + " |")
                tables.append(rows)
        elif tag == f"{_W_NS}sdt":
            content = child.find(f"{_W_NS}sdtContent")
            if content is not None:
                paragraphs += _docx_render_paragraphs(content, settings, parts, tables)
    return paragraphs


def extract_docx(path: Path, *, settings: Settings | None = None, max_chars: int | None = None) -> dict[str, Any]:
    """Extract paragraphs and tables from a DOCX file (never executes anything)."""
    settings = settings or load_settings()
    try:
        with zipfile.ZipFile(path) as archive:
            try:
                document_data = archive.read("word/document.xml")
            except KeyError as exc:
                raise ToolError(
                    "unsupported_content", f"No word/document.xml inside {path.name}; not a DOCX file."
                ) from exc
            core = _read_core_properties(archive)
            try:
                document = ET.fromstring(document_data)
            except ET.ParseError as exc:
                raise ToolError("malformed_file", f"Malformed DOCX XML in {path.name}: {exc}") from exc
            # Paragraphs/tables may live under <w:body> or (rarely) directly in
            # the document element; either root yields a sensible count.
            body = document.find(f"{_W_NS}body")
            root = body if body is not None else document
            parts: list[str] = []
            tables: list[list[list[str]]] = []
            paragraphs = _docx_render_paragraphs(root, settings, parts, tables)
            if paragraphs == 0 and not any(part.strip() for part in parts):
                raise ToolError("empty_file", f"DOCX has no extractable paragraphs or tables: {path.name}")
    except zipfile.BadZipFile as exc:
        raise ToolError("malformed_file", f"Corrupted or invalid DOCX archive: {path.name}") from exc
    except NotImplementedError as exc:
        raise ToolError("unsupported_content", f"Unsupported or encrypted DOCX: {path.name}") from exc

    text = normalize_structured_text("\n".join(parts))
    truncated = False
    limit = max_chars if max_chars and max_chars > 0 else settings.doc_max_chars
    if len(text) > limit:
        text = text[:limit]
        truncated = True

    headings = [line for line in text.split("\n") if line and line.startswith("#")]
    return {
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        "title": core.get("title", "") or "",
        "metadata": {
            "paragraphs": paragraphs,
            "tables": len(tables),
            "table_rows": sum(len(rows) for rows in tables),
            "headings": len(headings),
            "core_properties": core,
        },
        "warnings": ["Text truncated to the character limit."] if truncated else [],
    }


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        data = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    strings: list[str] = []
    for si in root.findall(f"{_MAIN_NS}si"):
        parts = [t.text or "" for t in si.iter() if t.tag == f"{_MAIN_NS}t"]
        strings.append("".join(parts))
    return strings


def _xlsx_sheet_paths(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Return ``(name, archive_path)`` pairs in workbook order."""
    try:
        workbook_data = archive.read("xl/workbook.xml")
        rels_data = archive.read("xl/_rels/workbook.xml.rels")
    except (KeyError, ET.ParseError) as exc:
        raise ToolError("malformed_file", f"Malformed XLSX package: {exc}") from exc
    try:
        workbook = ET.fromstring(workbook_data)
        rels = ET.fromstring(rels_data)
    except ET.ParseError as exc:
        raise ToolError("malformed_file", f"Malformed XLSX XML: {exc}") from exc
    targets: dict[str, str] = {}
    for rel in rels.findall(f"{_PKG_REL_NS}Relationship"):
        identifier = rel.get("Id")
        target = rel.get("Target") or ""
        if identifier and target and "worksheet" in (rel.get("Type") or ""):
            targets[identifier] = target.lstrip("/")
    sheets: list[tuple[str, str]] = []
    for sheet in workbook.iter(f"{_MAIN_NS}sheet"):
        name = sheet.get(f"{_MAIN_NS}name") or sheet.get("name") or ""
        rid = sheet.get(f"{'{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'}id", "")
        target = targets.get(rid, "")
        if not name or not target:
            continue
        if target.startswith("xl/"):
            path = target
        elif target.startswith("/"):
            path = target[1:]
        else:
            path = "xl/" + target
        sheets.append((name, path))
    return sheets


def _xlsx_column_index(ref: str, fallback: int) -> int:
    match = _COL_RE.match((ref or "").upper())
    if match is None:
        return fallback
    index = 0
    for ch in match.group(1):
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


def _xlsx_cell_value(cell: ET.Element, shared: list[str]) -> str:
    kind = (cell.get("t") or "").strip().lower()
    inline = cell.find(f"{_MAIN_NS}is")
    if kind == "inlinestr" and inline is not None:
        return collapse_inline("".join(t.text or "" for t in inline.iter() if t.tag == f"{_MAIN_NS}t"))
    value_node = cell.find(f"{_MAIN_NS}v")
    if value_node is None or value_node.text is None:
        return ""
    if kind == "s":
        try:
            return shared[int(value_node.text.strip())]
        except (IndexError, ValueError):
            return ""
    if kind == "b":
        return "TRUE" if value_node.text.strip() == "1" else "FALSE"
    return value_node.text.strip()[: _CELL_TEXT_CAP]


def extract_xlsx(path: Path, *, settings: Settings | None = None, max_chars: int | None = None) -> dict[str, Any]:
    """Extract worksheet names and cell values from an XLSX file.

    Rows render as ``| v1 | v2 |`` pipe tables (first row as the de facto
    header; noted in metadata). Structure is preserved per sheet; huge sheets
    are capped by AGLETRAS_OFFICE_MAX_CELLS with a warning, never silently cut.
    """
    settings = settings or load_settings()
    try:
        with zipfile.ZipFile(path) as archive:
            sheets = _xlsx_sheet_paths(archive)
            shared = _xlsx_shared_strings(archive)
            core = _read_core_properties(archive)
            if not sheets:
                raise ToolError("empty_file", f"XLSX contains no worksheets: {path.name}")
            parts: list[str] = []
            sheet_meta: list[dict[str, Any]] = []
            warnings: list[str] = []
            cells_left = max(int(settings.office_max_cells), 1)
            for name, target in sheets:
                try:
                    sheet_data = archive.read(target)
                except KeyError:
                    LOGGER.debug("worksheet %r missing in %s", target, path.name)
                    continue
                try:
                    sheet = ET.fromstring(sheet_data)
                except ET.ParseError as exc:
                    raise ToolError("malformed_file", f"Malformed worksheet XML in {path.name}: {exc}") from exc
                meta = _xlsx_render_sheet(sheet, shared, name, parts, cells_left)
                sheet_meta.append(meta)
                cells_left = int(meta.get("cells_left", cells_left))
                if meta.get("warning"):
                    warnings.append(meta["warning"])
    except zipfile.BadZipFile as exc:
        raise ToolError("malformed_file", f"Corrupted or invalid spreadsheet archive: {path.name}") from exc
    except NotImplementedError as exc:
        raise ToolError("unsupported_content", f"Unsupported or encrypted spreadsheet: {path.name}") from exc

    text = normalize_structured_text("\n".join(parts))
    truncated = False
    limit = max_chars if max_chars and max_chars > 0 else settings.doc_max_chars
    if len(text) > limit:
        text = text[:limit]
        truncated = True
        warnings.append("Text truncated to the character limit.")
    return {
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        "title": core.get("title", "") or "",
        "metadata": {
            "sheets": [
                {"name": meta["name"], "rows": meta["rows"], "columns": meta["columns"],
                 "rendered_rows": meta["rendered_rows"]}
                for meta in sheet_meta
            ],
            "core_properties": core,
        },
        "warnings": warnings,
    }


def _xlsx_render_sheet(
    sheet: ET.Element, shared: list[str], name: str, parts: list[str], cells_left: int
) -> dict[str, Any]:
    """Render one worksheet into *parts* (sharing the job-wide cell budget).

    Returns sheet metadata; ``warning`` is set when the cap cut rows off.
    """
    parts.append(f"## Sheet: {collapse_inline(name) or 'unnamed'}")
    data = sheet.find(f"{_MAIN_NS}sheetData")
    rendered_rows = 0
    total_rows = 0
    column_max = 0
    warning = ""
    rows_total = data.findall(f"{_MAIN_NS}row") if data is not None else []
    if data is not None:
        for row in rows_total:
            if cells_left <= 0:
                not_rendered = len(rows_total) - rendered_rows
                warning = (
                    f"Spreadsheet cell cap reached (AGLETRAS_OFFICE_MAX_CELLS); "
                    f"{not_rendered} row(s) of sheet {name!r} not rendered."
                )
                break
            values: dict[int, str] = {}
            position = 0
            for cell in row.findall(f"{_MAIN_NS}c"):
                index = _xlsx_column_index(cell.get("r") or "", position)
                values[index] = _xlsx_cell_value(cell, shared)
                position = index + 1
                cells_left -= 1
            if not values:
                continue
            width = max(values) + 1
            column_max = max(column_max, width)
            total_rows += 1
            if cells_left >= 0:
                padded = [values.get(i, "") or " " for i in range(width)]
                parts.append("| " + " | ".join(padded) + " |")
                rendered_rows += 1
    parts.append("")
    return {
        "name": name,
        "rows": total_rows,
        "columns": column_max,
        "rendered_rows": rendered_rows,
        "warning": warning,
        "cells_left": max(cells_left, 0),
    }

# ---------------------------------------------------------------------------
# CSV / TSV
# ---------------------------------------------------------------------------

_DELIMITERS = ",;\t|"


def render_csv_text(text: str, *, settings: Settings | None = None, max_chars: int | None = None) -> dict[str, Any]:
    """Parse CSV text and render a structure-preserving pipe table.

    Headers come directly from the first row; every data row stays aligned to
    a header column. Ragged rows are kept and reported (partial, not dropped).
    """
    settings = settings or load_settings()
    if not text.strip():
        raise ToolError("empty_file", "CSV is empty")
    delimiter = _DELIMITERS[0]
    try:
        delimiter = csv.Sniffer().sniff(text[:4096], delimiters=_DELIMITERS).delimiter
    except csv.Error:
        pass  # single-column file: default comma is harmless
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [[(field or "").strip() for field in row] for row in reader]
    rows = [row for row in rows if any(field for field in row)]
    if not rows:
        raise ToolError("empty_file", "CSV contains no data rows")
    headers, body = rows[0], rows[1:]
    width = max(len(row) for row in rows)
    headers = headers + [""] * (width - len(headers))

    parts = ["| " + " | ".join(headers) + " |", "| " + " | ".join([" --- "] * width) + " |"]
    ragged = 0
    budget = max(int(settings.office_max_cells), 1)
    for row in body:
        if len(row) != width:
            ragged += 1
        padded = row + [""] * (width - len(row))
        parts.append("| " + " | ".join(cell or " " for cell in padded) + " |")
        budget -= width
        if budget <= 0:
            break
    text_out = normalize_structured_text("\n".join(parts))
    warnings = []
    truncated = False
    limit = max_chars if max_chars and max_chars > 0 else settings.doc_max_chars
    if len(text_out) > limit:
        text_out = text_out[:limit]
        truncated = True
        warnings.append("Text truncated to the character limit.")
    if ragged:
        warnings.append(f"{ragged} row(s) had a different column count; values kept and padded.")
    if budget <= 0:
        warnings.append("CSV cell cap reached; additional rows not rendered (AGLETRAS_OFFICE_MAX_CELLS).")
    return {
        "text": text_out,
        "chars": len(text_out),
        "truncated": truncated,
        "title": "",
        "metadata": {"delimiter": delimiter, "header_columns": len(headers), "data_rows": len(body)},
        "warnings": warnings,
    }

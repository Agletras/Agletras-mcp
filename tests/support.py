"""Shared helpers for the agletras-mcp test suite.

All tests are hermetic: they use a temporary ``AGLETRAS_HOME`` (no project
``data/`` writes) and local HTTP servers (no external network).
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

ENV_KEYS = (
    "AGLETRAS_HOME",
    "AGLETRAS_DATA_DIR",
    "AGLETRAS_REPORTS_DIR",
    "AGLETRAS_CACHE_DIR",
    "AGLETRAS_DB_PATH",
    "AGLETRAS_FETCH_MAX_BYTES",
    "AGLETRAS_FETCH_MAX_CHARS",
    "AGLETRAS_PDF_MAX_BYTES",
    "AGLETRAS_PDF_MAX_PAGES",
    "AGLETRAS_PDF_MAX_CHARS",
    "AGLETRAS_MAX_REDIRECTS",
    "AGLETRAS_MAX_CHARS_CAP",
    "AGLETRAS_ALLOW_PRIVATE_HOSTS",
    "AGLETRAS_ALLOWED_DOMAINS",
    "AGLETRAS_SEARCH_PROVIDER",
    "AGLETRAS_SEARCH_ENDPOINT",
    "AGLETRAS_SEARCH_MIN_INTERVAL",
    "AGLETRAS_MAX_SEARCH_RESULTS",
    "AGLETRAS_USER_AGENT",
    "AGLETRAS_FETCH_TIMEOUT",
    # Phase 2: collection pipeline
    "AGLETRAS_LOCAL_ROOTS",
    "AGLETRAS_COLLECT_CONCURRENCY",
    "AGLETRAS_COLLECT_MAX_ITEMS",
    "AGLETRAS_COLLECT_MAX_RETRIES",
    "AGLETRAS_COLLECT_BACKOFF",
    "AGLETRAS_DOC_MAX_CHARS",
    "AGLETRAS_FOLDER_MAX_FILES",
    "AGLETRAS_FOLDER_MAX_DEPTH",
    "AGLETRAS_FOLDER_MAX_FILE_BYTES",
    "AGLETRAS_OFFICE_MAX_CELLS",
    # Phase 3: evidence library
    "AGLETRAS_EVIDENCE_CACHE_TTL",
    "AGLETRAS_EVIDENCE_PASSAGE_CHARS",
    "AGLETRAS_EVIDENCE_FTS",
    # Phase 6: claim verification and synthesis
    "AGLETRAS_VERIFICATION_MODEL_ENDPOINT",
    "AGLETRAS_VERIFICATION_MODEL",
    "AGLETRAS_VERIFICATION_API_KEY_ENV",
    "AGLETRAS_VERIFICATION_MAX_CLAIMS",
    "AGLETRAS_VERIFICATION_MAX_CANDIDATES",
    "AGLETRAS_REPORT_MAX_FINDINGS",
    # Phase 7: CLI, configuration file, logging, research pipeline
    "AGLETRAS_CONFIG",
    "AGLETRAS_LOGS_DIR",
    "AGLETRAS_LOG_FORMAT",
    "AGLETRAS_LOG_FILE",
    "AGLETRAS_RESEARCH_DEPTH",
    "AGLETRAS_RESEARCH_MAX_SOURCES",
    "AGLETRAS_RESEARCH_MAX_CLAIMS",
    "AGLETRAS_RESEARCH_RESULTS_PER_SEARCH",
    "AGLETRAS_RESEARCH_SEARCHES_PER_SUBQUESTION",
    "AGLETRAS_RESEARCH_FOLLOWUP_ROUNDS",
    "AGLETRAS_RESEARCH_VERIFICATION_METHOD",
    "AGLETRAS_RESEARCH_INCLUDE_IMAGES",
    "AGLETRAS_RESEARCH_IMAGE_RESULTS",
    "AGLETRAS_RESEARCH_RESUME_MAX_AGE_DAYS",
    "AGLETRAS_RESEARCH_REUSE_MAX_AGE_DAYS",
    # hermeticity for the remaining Phase 1-5 knobs
    "AGLETRAS_DEFAULT_LIST_LIMIT",
    "AGLETRAS_MAX_LIST_LIMIT",
    "AGLETRAS_IMAGE_SEARCH_PROVIDER",
    "AGLETRAS_IMAGE_MAX_BYTES",
    "AGLETRAS_IMAGE_MAX_DIMENSION",
    "AGLETRAS_IMAGE_MAX_RESULTS",
    "AGLETRAS_VISION_PROVIDER",
    "AGLETRAS_VISION_MODEL",
    "AGLETRAS_VISION_MODEL_ENDPOINT",
    "AGLETRAS_VISION_API_KEY_ENV",
)

# No blanket values: _apply_permissive_env() clears keys and applies only the
# two overrides tests need (loopback-friendly fetches, no search pacing).


def _apply_permissive_env() -> None:
    """Clear AGLETRAS_* env keys and apply test-friendly overrides.

    Blanket-clearing keeps tests hermetic (a stray developer env var cannot
    change behavior). Two overrides are needed because every test HTTP server
    lives on 127.0.0.1, which the production SSRF guard blocks, and because
    the search provider would otherwise pace calls with real sleeps.
    """
    for key in ENV_KEYS:
        os.environ.pop(key, None)
    os.environ["AGLETRAS_ALLOW_PRIVATE_HOSTS"] = "1"
    os.environ["AGLETRAS_SEARCH_MIN_INTERVAL"] = "0"


def load_pdf_builder():
    """Import ``scripts/make_sample_pdf.py`` as a module (single source of truth)."""
    spec = importlib.util.spec_from_file_location(
        "agletras_make_sample_pdf", SCRIPTS_DIR / "make_sample_pdf.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError("could not load scripts/make_sample_pdf.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PDF_BUILDER = load_pdf_builder()
SAMPLE_PAGES = PDF_BUILDER.SAMPLE_PAGES


class TempHomeMixin:
    """Isolate every agletras path inside a temporary directory."""

    home: Path

    def _setup_home(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="agletras-test-")
        self.home = Path(self._tmp.name)
        self._saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        _apply_permissive_env()
        os.environ["AGLETRAS_HOME"] = str(self.home)
        # Local files under the temporary home are collectible by default.
        os.environ["AGLETRAS_LOCAL_ROOTS"] = str(self.home)

    def allow_roots(self, *extras: str | Path) -> None:
        """Add extra paths to AGLETRAS_LOCAL_ROOTS on top of the temp home."""
        roots = [str(self.home)] + [str(Path(p).resolve()) for p in extras]
        os.environ["AGLETRAS_LOCAL_ROOTS"] = ",".join(roots)

    def _teardown_home(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def write_sample_pdf(
        self,
        name: str = "sample.pdf",
        pages: list[list[str]] | None = None,
    ) -> Path:
        target = self.home / name
        target.write_bytes(PDF_BUILDER.build_pdf(pages if pages is not None else SAMPLE_PAGES))
        return target


class TempHomeCase(TempHomeMixin, unittest.TestCase):
    """Synchronous test case with an isolated home directory."""

    def setUp(self) -> None:
        super().setUp()
        self._setup_home()

    def tearDown(self) -> None:
        self._teardown_home()
        super().tearDown()


class StrictHostTestsMixin:
    """Opt tests back into production SSRF strictness (no loopback bypass).

    Use when testing the private-address guard itself; normal fetch tests run
    permissive so their loopback HTTP servers are reachable.
    """

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        os.environ["AGLETRAS_ALLOW_PRIVATE_HOSTS"] = "0"

    def tearDown(self) -> None:
        os.environ.pop("AGLETRAS_ALLOW_PRIVATE_HOSTS", None)
        super().tearDown()  # type: ignore[misc]



class AsyncTempHomeCase(TempHomeMixin, unittest.IsolatedAsyncioTestCase):
    """Async test case with an isolated home directory."""

    def setUp(self) -> None:
        super().setUp()
        self._setup_home()

    def tearDown(self) -> None:
        self._teardown_home()
        super().tearDown()


@contextmanager
def serve_http(routes: dict[str, tuple]) -> Iterator[str]:
    """Serve fixed payloads on 127.0.0.1. Yields the base URL.

    ``routes`` maps a path to ``(status, content_type, body)`` or
    ``(status, content_type, body, extra_headers)``. Redirect statuses (3xx)
    automatically get their ``Location`` header emitted when present, so a
    route like ``(302, "text/html", b"", {"Location": "/next"})`` works.
    """

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:  # noqa: N802 - http.server API
            route = routes.get(self.path.split("?")[0], (404, "text/plain", b"not found"))
            status, content_type, body = route[0], route[1], route[2]
            extra_headers = route[3] if len(route) > 3 else {}
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for name, value in extra_headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self._serve()

        def do_POST(self) -> None:  # noqa: N802 - http.server API (search form)
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._serve()

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return  # keep test output quiet

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def serve_slow_http(delay: float, body: bytes = b"slow response") -> Iterator[str]:
    """Serve one path that answers only after *delay* seconds (timeout tests)."""

    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            time.sleep(delay)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Phase 2 document fixtures (minimal but spec-shaped OOXML packages)
# ---------------------------------------------------------------------------

DOCX_MAIN = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOCX_STYLES_MAIN = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XLSX_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLSX_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
DC_NS = "http://purl.org/dc/elements/1.1/"
DCT_NS = "http://purl.org/dc/terms/"


def _xml_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def build_docx(
    paragraphs: list[tuple[str | None, str]] | None = None,
    tables: list[list[list[str]]] | None = None,
    *,
    title: str | None = None,
    creator: str | None = None,
) -> bytes:
    """Build a minimal, spec-valid DOCX package as bytes.

    ``paragraphs`` items are ``(style_or_None, text)``; `tables` are row/column
    matrices of cell text.
    """
    paragraphs = paragraphs if paragraphs is not None else [(None, "docx body text")]
    body_parts: list[str] = []
    for style, text in paragraphs:
        props = f'<w:pPr><w:pStyle w:val="{_xml_attr(style)}"/></w:pPr>' if style else ""
        runs = f"<w:r><w:t>{_xml_attr(text)}</w:t></w:r>" if text else ""
        body_parts.append(f"<w:p>{props}{runs}</w:p>")
    for rows in tables or []:
        row_xml = "".join(
            "<w:tr>"
            + "".join(
                f'<w:tc><w:p><w:r><w:t>{_xml_attr(cell)}</w:t></w:r></w:p></w:tc>'
                for cell in row
            )
            + "</w:tr>"
            for row in rows
        )
        body_parts.append(f"<w:tbl>{row_xml}</w:tbl>")
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{DOCX_MAIN}"><w:body>{"".join(body_parts)}</w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    members: dict[str, str | bytes] = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": rels,
        "word/document.xml": document,
    }
    if title or creator:
        core = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            f'xmlns:dc="{DC_NS}" xmlns:dcterms="{DCT_NS}" '
            f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            + (f"<dc:title>{_xml_attr(title)}</dc:title>" if title else "")
            + (f"<dc:creator>{_xml_attr(creator)}</dc:creator>" if creator else "")
            + '<dcterms:created xsi:type="dcterms:W3CDTF">2026-01-15T09:30:00Z</dcterms:created>'
            "</cp:coreProperties>"
        )
        members["docProps/core.xml"] = core
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


# Cell override helper for the XLSX fixture builder


class Cell:
    """Explicit cell for build_xlsx: kind in {s(shared), inline, bool, num, f}."""

    def __init__(self, kind: str, value: str, *, reference: str | None = None) -> None:
        self.kind = kind
        self.value = value
        self.reference = reference


def build_xlsx(
    sheets: list[tuple[str, list[list[object]]]],
    *,
    title: str | None = None,
) -> bytes:
    """Build a minimal, spec-valid XLSX package as bytes.

    Cell values: ``str`` -> shared string, numbers -> numeric, ``bool`` ->
    boolean, ``None`` -> gap (missing cell), :class:`Cell` -> explicit kind:
    kind ``inline`` renders an inlineStr, ``f`` a formula cell with a cached
    value (the value is used and the formula is never evaluated).
    """
    shared_index: dict[str, int] = {}
    shared_order: list[str] = []

    def shared_string(text: str) -> str:
        if text not in shared_index:
            shared_index[text] = len(shared_order)
            shared_order.append(text)
        return str(shared_index[text])

    def cell_xml(value: object, row_number: int, column_number: int) -> str:
        reference = f"{_column_name(column_number)}{row_number}"
        if isinstance(value, Cell):
            if value.kind == "inline":
                return (
                    f'<c r="{reference}" t="inlineStr">'
                    f"<is><t>{_xml_attr(value.value)}</t></is></c>"
                )
            if value.kind == "f":
                return f'<c r="{reference}"><f>{_xml_attr(value.value)}</f><v>{value.reference or ""}</v></c>'
            return f'<c r="{reference}" t="{value.kind}"><v>{value.value}</v></c>'
        if value is None:
            return ""
        if isinstance(value, bool):
            return f'<c r="{reference}" t="b"><v>{1 if value else 0}</v></c>'
        if isinstance(value, (int, float)):
            return f'<c r="{reference}"><v>{value}</v></c>'
        return f'<c r="{reference}" t="s"><v>{shared_string(str(value))}</v></c>'

    def _column_name(number: int) -> str:  # 1-based
        letters = ""
        while number > 0:
            number, remainder = divmod(number - 1, 26)
            letters = chr(ord("A") + remainder) + letters
        return letters

    sheet_elements: list[str] = []
    sheet_members: list[tuple[str, str]] = []
    for index, (name, rows) in enumerate(sheets, start=1):
        row_parts: list[str] = []
        for row_index, row in enumerate(rows, start=1):
            cells_xml = "".join(
                cell_xml(value, row_index, position) for position, value in enumerate(row, start=1)
            )
            row_parts.append('<row r="%d">%s</row>' % (row_index, cells_xml))
        row_xml = "".join(row_parts)
        sheet_xml = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{XLSX_MAIN}"><sheetData>{row_xml}</sheetData></worksheet>'
        )
        target = f"worksheets/sheet{index}.xml"
        sheet_members.append((target, sheet_xml))
        sheet_elements.append(f'<sheet name="{_xml_attr(name)}" sheetId="{index}" r:id="rId{index}"/>')

    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{XLSX_MAIN}" xmlns:r="{XLSX_REL}">'
        f"<sheets>{''.join(sheet_elements)}</sheets>"
        "</workbook>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{PKG_REL}">'
        + "".join(
            f'<Relationship Id="rId{i}" Type="{XLSX_REL}/worksheet" Target="{target}"/>'
            for i, (target, _payload) in enumerate(sheet_members, start=1)
        )
        + "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(
            f'<Override PartName="/xl/{target}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for target, _payload in sheet_members
        )
        + "</Types>"
    )
    members: dict[str, str | bytes] = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": rels,
        **{f"xl/{target}": payload for target, payload in sheet_members},
    }
    if shared_order:
        shared_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<sst xmlns="{XLSX_MAIN}" count="{len(shared_order)}" uniqueCount="{len(shared_order)}">'
            + "".join(f"<si><t>{_xml_attr(text)}</t></si>" for text in shared_order)
            + "</sst>"
        )
        members["xl/sharedStrings.xml"] = shared_xml
    if title:
        members["docProps/core.xml"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="{DC_NS}">'
            f"<dc:title>{_xml_attr(title)}</dc:title></cp:coreProperties>"
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()

"""Source adapters: preparation (retrieval) + extraction into unified records.

Adapters share one shape - prepare raw material, then produce a
:class:`~agletras_mcp.source_record.SourceRecord` - and everything routes
through :func:`record_from_prepared`:

- **web URLs** are fetched once by :func:`prepare_url` (through the Phase 1
  SSRF policy, byte caps, and timeout) and then dispatched: PDFs go to the
  pypdf extractor (Phase 1), HTML to the ``html.parser`` extractor, anything
  textual to plain-text handling.
- **search queries** are answered by the Phase 1 ``search_web`` providers and
  turned into ``search_result`` records that keep title, URL, and snippet.
  Snippets are never treated as full-page content (``snippet_only: true``
  metadata marks that).
- **local files** are dispatched by extension using the Phase 2 parsers
  (pypdf / markdown-text / office), with access/root checks done by the
  pipeline via ``localfs.resolve_allowed`` before anything is read.

Phase 1 modules are composed here, not duplicated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from . import localfs, officedoc, pdf as pdf_mod, textdoc, webfetch
from .config import Settings, load_settings
from .errors import ToolError
from .source_record import (
    SourceRecord,
    collapse_inline,
    normalize_structured_text,
    parse_publication_date,
)

LOGGER = logging.getLogger(__name__)

# <meta> keys that a page may use to publish its own date. Only real metadata
# sets publication_date; absence stays None (never the retrieval time).
_META_DATE_KEYS = frozenset(
    {"article:published_time", "og:published_time", "date", "dc.date", "pubdate", "publication_date"}
)


@dataclass
class Prepared:
    """Raw, already-retrieved material handed to an extractor."""

    route: str  # "url" | "file"
    label: str  # exact user-provided provenance string
    url: str | None = None  # requested URL
    final_url: str | None = None  # after redirects
    path: Path | None = None
    body: bytes | None = None
    content_type: str = ""
    http_status: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# MetaPublication extraction (HTML)
# ---------------------------------------------------------------------------


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.date_candidates: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        values = {key.lower(): (value or "") for key, value in attrs}
        key = values.get("property") or values.get("name") or values.get("itemprop") or ""
        content = values.get("content") or ""
        if key.lower() in _META_DATE_KEYS and content.strip():
            self.date_candidates.append(content.strip())


def publication_date_from_html(html_text: str) -> str | None:
    """First date-shaped <meta> candidate from real page metadata, else None."""
    parser = _MetaParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # noqa: BLE001 - meta scan is best effort
        return None
    for candidate in parser.date_candidates:
        iso = parse_publication_date(candidate)
        if iso:
            return iso
    return None


# ---------------------------------------------------------------------------
# Preparation: retrieval before extraction
# ---------------------------------------------------------------------------


def prepare_url(url: str, settings: Settings) -> Prepared:
    """Fetch *url* once (SSRF-guarded). PDF-specific size cap applies."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    is_pdf_guess = (parsed.path or "").lower().endswith(".pdf")
    max_bytes = settings.pdf_max_bytes if is_pdf_guess else settings.fetch_max_bytes
    final_url, content_type, body = webfetch.fetch_bytes(url, settings, max_bytes=max_bytes)
    return Prepared(
        route="url", label=url, url=url, final_url=final_url,
        body=body, content_type=content_type,
    )


def prepare_file(path: Path, settings: Settings) -> Prepared:
    """Read a permitted local file, bounded by ``AGLETRAS_FOLDER_MAX_FILE_BYTES``."""
    resolved = localfs.resolve_allowed(path, settings)
    if resolved.is_dir():
        raise ToolError("invalid_argument", f"Expected a file, got a directory: {resolved}. Use collect_local_folder.")
    size = resolved.stat().st_size
    if size > settings.folder_max_file_bytes:
        raise ToolError(
            "too_large",
            f"{resolved.name} is {size} bytes; limit is {settings.folder_max_file_bytes} "
            "(AGLETRAS_FOLDER_MAX_FILE_BYTES).",
        )
    extension = resolved.suffix.lower()
    source_type = localfs.EXTENSION_TO_TYPE.get(extension)
    if source_type is None:
        raise ToolError(
            "unsupported_content",
            f"Unsupported extension {extension or '(none)'} for {resolved.name}; supported: "
            + ", ".join(sorted(localfs.SUPPORTED_EXTENSIONS)),
        )
    body = resolved.read_bytes()
    return Prepared(
        route="file", label=str(resolved), path=resolved, body=body,
        content_type=source_type, extra={"extension": extension},
    )


# ---------------------------------------------------------------------------
# Extraction routing
# ---------------------------------------------------------------------------


def record_from_prepared(prepared: Prepared, *, settings: Settings | None = None, max_chars: int | None = None) -> SourceRecord:
    """Route *prepared* to the right extractor and build the SourceRecord."""
    settings = settings or load_settings()
    if prepared.route == "url":
        return _record_from_url(prepared, settings, max_chars)
    return _record_from_file(prepared, settings, max_chars)


def _fallback_title(prepared: Prepared, default: str) -> str:
    if prepared.path is not None:
        return prepared.path.stem
    if prepared.final_url:
        netloc = urlsplit_host(prepared.final_url)
        if netloc:
            return netloc
    return default


def urlsplit_host(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return urlsplit(url).netloc or ""
    except ValueError:
        return ""


def _record_from_url(prepared: Prepared, settings: Settings, max_chars: int | None) -> SourceRecord:
    body = prepared.body or b""
    record = SourceRecord(
        source_type="web",
        title="",
        original_url=prepared.url,
        final_url=prepared.final_url,
        content_type=prepared.content_type,
        file_size_bytes=len(body),
    )
    lowered = (prepared.content_type or "").lower()
    # Content-type first, magic bytes second: a .html URL may serve PDF bytes
    # and a .pdf path may serve an HTML error page - the server announces what
    # it returned, so that has priority over URL-based guessing. Undecoded
    # generic types (octet-stream, missing) fall back to the %PDF- magic check.
    pdf_by_magic = body_ok_as_pdf(body)
    generic_types = ("", "application/octet-stream", "application/binary", "binary/octet-stream")
    if "pdf" in lowered or (lowered in generic_types and pdf_by_magic):
        return _record_from_pdf_cache(prepared, settings, max_chars, record)
    decoded = webfetch.decode_body(body, prepared.content_type)
    if webfetch.looks_like_html(prepared.content_type, body):
        record.source_type = "web"
        title, text = webfetch.html_to_text(decoded)
        record.publication_date = publication_date_from_html(decoded)
        record.metadata = {"host": urlsplit_host(prepared.final_url or prepared.url or ""), "html_title": title}
    elif lowered.startswith("text/") or "json" in lowered or "xml" in lowered or not lowered:
        record.source_type = "text"
        title, text = "", normalize_structured_text(decoded)
        record.metadata = {"host": urlsplit_host(prepared.final_url or prepared.url or "")}
    else:
        record.set_failure(
            "unsupported_content",
            f"Unsupported content type {prepared.content_type!r} at {prepared.final_url}.",
            status="unsupported",
        )
        record.metadata = {"content_type": prepared.content_type}
        return record
    if not text.strip():
        record.set_failure(
            "empty_content", f"Page {prepared.final_url} rendered no readable text.", status="partial"
        )
    record.title = collapse_inline(title) or _fallback_title(prepared, "web page")
    record.extracted_text = text  # the pipeline applies the char cap (finalize_text)
    record.extraction_status = "success" if not record.error_code else record.extraction_status
    return record


def _record_from_pdf_cache(prepared: Prepared, settings: Settings, max_chars: int | None, record: SourceRecord) -> SourceRecord:
    """PDF behind a URL: store bytes in the cache once, reuse Phase 1 pdf extractor."""
    if not body_ok_as_pdf(prepared.body or b""):
        record.set_failure(
            "unsupported_content",
            f"{prepared.final_url} did not return a PDF (content type {prepared.content_type!r}).",
            status="unsupported",
        )
        return record
    record.source_type = "pdf"
    cache_path = webfetch.store_pdf_in_cache(prepared.body or b"", settings)
    record.metadata = {"cached_pdf": True}
    try:
        payload = pdf_mod.extract_text(
            str(cache_path),
            max_chars=max_chars,
            settings=settings,
        )
    except ToolError as exc:
        record.set_failure(exc.code, exc.message)
        return record
    _apply_pdf_payload(record, payload, prepared, cache_path)
    return record


def body_ok_as_pdf(body: bytes) -> bool:
    return body.lstrip().startswith(webfetch.PDF_MAGIC)


def _apply_pdf_payload(
    record: SourceRecord, payload: dict[str, Any], prepared: Prepared, path: Path
) -> None:
    pdf_title = payload["metadata"].get("title", "")
    url_name = ""
    from urllib.parse import urlsplit

    if prepared.url:
        url_name = Path(urlsplit(prepared.url).path).stem
    record.title = pdf_title or record.title or url_name or str(path.stem)
    record.file_path = str(path)
    record.extracted_text = payload["text"]
    record.file_size_bytes = None  # URL source: response size already captured
    if not payload["has_text"]:
        record.extraction_status = "partial"
        record.error_code = "empty_content"
        record.error_message = "PDF has no extractable text; may be scanned. OCR is not available."
    else:
        record.extraction_status = "partial" if payload["warnings"] else "success"
    for warning in payload["warnings"]:
        record.add_warning(warning)
    record.metadata.update(
        {
            "pdf": payload["metadata"],
            "page_count": payload["page_count"],
            "pages_extracted": payload["pages_extracted"],
        }
    )


def _record_from_file(prepared: Prepared, settings: Settings, max_chars: int | None) -> SourceRecord:
    assert prepared.path is not None
    source_type = prepared.content_type  # extension-derived type from prepare_file
    record = SourceRecord(
        source_type=source_type,
        title="",
        original_url=None,
        final_url=None,
        file_path=str(prepared.path),
        file_size_bytes=len(prepared.body or b""),
        content_type=None,
    )
    body = prepared.body or b""
    if not body.strip():
        record.set_failure("empty_file", f"File is empty (0 bytes): {prepared.path.name}", status="failed")
        return record
    try:
        if source_type == "pdf":
            payload = pdf_mod.extract_text(str(prepared.path), max_chars=max_chars, settings=settings)
            record.title = payload["metadata"].get("title", "") or prepared.path.stem
            record.extracted_text = payload["text"]
            record.extraction_status = "partial" if payload["warnings"] else "success"
            if not payload["has_text"]:
                record.extraction_status = "partial"
                record.error_code = "empty_content"
                record.error_message = "PDF has no extractable text; may be scanned. OCR is not available."
            for warning in payload["warnings"]:
                record.add_warning(warning)
            record.metadata = {
                "pdf": payload["metadata"],
                "page_count": payload["page_count"],
                "pages_extracted": payload["pages_extracted"],
            }
        elif source_type in ("markdown", "text"):
            payload = textdoc.extract_text_document(prepared.path, source_type, settings=settings, max_chars=max_chars)
            record.title = payload["title"] or prepared.path.stem
            record.extracted_text = payload["text"]
            record.extraction_status = "partial" if payload["warnings"] else "success"
            record.metadata = dict(payload["metadata"])
            for warning in payload["warnings"]:
                record.add_warning(warning)
        elif source_type == "html_file":
            decoded = textdoc_html_decode(body)
            title, text = webfetch.html_to_text(decoded)
            record.source_type = "web"
            record.title = collapse_inline(title) or prepared.path.stem
            record.publication_date = publication_date_from_html(decoded)
            record.extracted_text = text  # pipeline finalizes/ caps
            record.metadata = {"extension": prepared.extra.get("extension", ".html")}
            if not text.strip():
                record.set_failure("empty_content", "HTML file rendered no readable text.", status="partial")
            else:
                record.extraction_status = "success"
        elif source_type == "csv":
            payload = decode_office_text(prepared, officedoc.render_csv_text, max_chars, settings)
            record.title = prepared.path.stem
            record.extracted_text = payload["text"]
            record.extraction_status = "partial" if payload["warnings"] else "success"
            record.metadata = dict(payload["metadata"])
            for warning in payload["warnings"]:
                record.add_warning(warning)
        elif source_type == "docx":
            payload = officedoc.extract_docx(prepared.path, settings=settings, max_chars=max_chars)
            record.title = payload["title"] or prepared.path.stem
            record.extracted_text = payload["text"]
            record.extraction_status = "partial" if payload["warnings"] else "success"
            record.metadata = dict(payload["metadata"])
            for warning in payload["warnings"]:
                record.add_warning(warning)
        elif source_type == "xlsx":
            payload = officedoc.extract_xlsx(prepared.path, settings=settings, max_chars=max_chars)
            record.title = payload["title"] or prepared.path.stem
            record.extracted_text = payload["text"]
            record.extraction_status = "partial" if payload["warnings"] else "success"
            record.metadata = dict(payload["metadata"])
            for warning in payload["warnings"]:
                record.add_warning(warning)
        else:  # pragma: no cover - prepare_file rejects unknown extensions
            record.set_failure("unsupported_content", f"No extractor for type {source_type!r}", status="unsupported")
            return record
    except ToolError as exc:
        record.set_failure(exc.code, exc.message, status=exc.code in ("unsupported_content",) and "unsupported" or "failed")
        if not record.title:
            record.title = prepared.path.stem
        return record
    except Exception as exc:  # noqa: BLE001 - one bad file must not abort a job
        LOGGER.warning("unexpected extraction failure for %s: %s", prepared.path, exc)
        record.set_failure("internal", f"{exc.__class__.__name__}: {exc}")
        return record
    if record.extracted_text == "":
        record.set_failure(
            "empty_content", f"No extractable text produced from {prepared.path.name}", status="partial"
        )
    return record


def textdoc_html_decode(body: bytes) -> str:
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return body.decode("latin-1", errors="replace")


def decode_office_text(
    prepared: Prepared, extractor: Any, max_chars: int | None, settings: Settings
) -> dict[str, Any]:
    """Decode file bytes then run a text-based extractor (csv)."""
    try:
        text = (prepared.body or b"").decode("utf-8-sig")
    except UnicodeDecodeError:
        text = (prepared.body or b"").decode("latin-1", errors="replace")
    return extractor(text, settings=settings, max_chars=max_chars)


# ---------------------------------------------------------------------------
# Search-result ingestion (snippets only, never fetched pages)
# ---------------------------------------------------------------------------


def records_from_search(
    query: str, *, max_results: int, settings: Settings | None = None
) -> tuple[list[SourceRecord], str]:
    """Run a Phase 1 web search and turn each result into a snippet record."""
    from . import search

    settings = settings or load_settings()
    result = search.search_web(query, max_results=max_results, settings=settings)
    records: list[SourceRecord] = []
    for item in result["results"]:
        snippet = collapse_inline(item.get("snippet", ""))
        records.append(
            SourceRecord(
                source_type="search_result",
                title=item.get("title", ""),
                original_url=item.get("url", ""),
                final_url=item.get("url", ""),
                extracted_text=snippet,
                extraction_status="success",
                content_type=None,
                metadata={
                    "snippet_only": True,
                    "provider": result["provider"],
                    "search_query": result["query"],
                },
            )
        )
    return records, result["provider"]

"""PDF text extraction backed by pypdf (pure Python, no system libraries).

Accepts local paths (absolute, or relative to the project root / cwd) and
``http(s)`` URLs (downloaded through the size-capped cache in webfetch).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from . import webfetch
from .config import Settings, load_settings
from .errors import ToolError

LOGGER = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"


def resolve_source(source: str, settings: Settings) -> tuple[Path, str | None]:
    """Resolve *source* to a local PDF path. Returns ``(path, source_url)``."""
    source = (source or "").strip()
    if not source:
        raise ToolError("invalid_argument", "source must not be empty")
    if source.lower().startswith(("http://", "https://")):
        path, final_url = webfetch.download_to_cache(source, settings)
        return path, final_url

    path = Path(source).expanduser()
    if not path.is_absolute():
        project_candidate = settings.project_root / path
        path = project_candidate if project_candidate.exists() else Path.cwd() / path
    if not path.exists():
        raise ToolError("not_found", f"PDF not found: {source}")
    if not path.is_file():
        raise ToolError("invalid_argument", f"Not a file: {path}")
    size = path.stat().st_size
    if size > settings.pdf_max_bytes:
        raise ToolError(
            "too_large",
            f"{path} is {size} bytes, over the {settings.pdf_max_bytes} byte limit.",
        )
    with path.open("rb") as handle:
        head = handle.read(len(PDF_MAGIC))
    if head != PDF_MAGIC:
        raise ToolError("unsupported_content", f"Not a PDF (missing %PDF- header): {path}")
    return path, None


def _metadata(reader: PdfReader) -> dict[str, Any]:
    info = getattr(reader, "metadata", None)
    if info is None:
        return {}
    fields = ("title", "author", "subject", "creator", "producer")
    metadata: dict[str, Any] = {}
    for field in fields:
        value = getattr(info, field, None)
        if value:
            metadata[field] = str(value).strip()
    creation = getattr(info, "creation_date", None)
    if creation:
        metadata["creation_date"] = str(creation)
    return metadata


def extract_text(
    source: str,
    *,
    max_pages: int | None = None,
    max_chars: int | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Extract text from a PDF, page by page.

    Raises :class:`ToolError` with codes ``not_found``, ``unsupported_content``,
    ``too_large``, or ``pdf_error``.
    """
    settings = settings or load_settings()
    path, source_url = resolve_source(source, settings)
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                decrypted = bool(reader.decrypt(""))
            except Exception:  # noqa: BLE001 - pypdf raises various types here
                decrypted = False
            if not decrypted:
                raise ToolError(
                    "pdf_error",
                    "PDF is encrypted and cannot be opened without a password.",
                )
        page_count = len(reader.pages)
        page_limit = min(page_count, max_pages or settings.pdf_max_pages)
        char_limit = max_chars if max_chars and max_chars > 0 else settings.pdf_max_chars

        parts: list[str] = []
        warnings: list[str] = []
        has_text = False
        failed_pages: list[int] = []
        for index in range(page_limit):
            page_text = ""
            try:
                page_text = (reader.pages[index].extract_text() or "").strip()
            except Exception as exc:  # noqa: BLE001 - keep going on bad pages
                LOGGER.warning("page %d of %s failed to extract: %s", index + 1, path, exc)
                failed_pages.append(index + 1)
            if page_text:
                has_text = True
            parts.append(f"--- page {index + 1}/{page_count} ---\n{page_text or '(no extractable text)'}")

        text = "\n\n".join(parts)
        truncated = False
        if len(text) > char_limit:
            text = text[:char_limit]
            truncated = True

        if page_count > page_limit:
            warnings.append(f"Only the first {page_limit} of {page_count} pages were extracted.")
        if failed_pages:
            warnings.append("Extraction failed on page(s): " + ", ".join(map(str, failed_pages)))
        if not has_text:
            warnings.append(
                "No extractable text found; the PDF may be scanned. "
                "OCR is not available in this environment."
            )
    except ToolError:
        raise
    except PdfReadError as exc:
        raise ToolError("pdf_error", f"Could not read PDF {path}: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise ToolError("pdf_error", f"Could not read PDF {path}: {exc}") from exc

    return {
        "source": source,
        "path": str(path),
        "url": source_url,
        "page_count": page_count,
        "pages_extracted": page_limit,
        "has_text": has_text,
        "chars": len(text),
        "truncated": truncated,
        "text": text,
        "metadata": _metadata(reader),
        "warnings": warnings,
    }

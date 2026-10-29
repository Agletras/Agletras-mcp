"""Reusable collection pipeline (Phase 2).

Pipeline stages::

    input -> source validation -> adapter selection -> retrieval ->
    extraction -> normalization -> deduplication -> source record ->
    research results

- Multiple sources are processed in one request with bounded concurrency
  (``ThreadPoolExecutor``); one failed source never aborts the job.
- Transient failures (connection issues, timeouts, 429/5xx) are retried with
  small exponential backoff; permanent failures (404/403, unsupported formats,
  oversized downloads, invalid arguments) are not.
- Every result carries its provenance: the exact requested URL/path/search
  string, the final URL after redirects, the local file path, and the
  retrieval time. On duplicates, every re-requested origin is appended to the
  surviving record (``metadata.origins``) and to the tool response.
- Deduplication is model-free: exact SHA-256 of the extracted text, plus a
  "shape" hash (word sequence, case/punctuation-insensitive) and a bounded
  word-set similarity scan for near duplicates.
- Publication dates come only from real document/page metadata; missing dates
  stay unknown (None). Retrieval time is recorded separately.
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from . import adapters, localfs, storage
from .config import Settings, load_settings
from .errors import ToolError
from .source_record import SourceRecord, finalize_text
from .textutil import excerpt

LOGGER = logging.getLogger(__name__)

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_NEAR_SIMILARITY_THRESHOLD = 0.90
_NEAR_COMPARE_LIMIT = 60  # pairs are compared only within reasonably small jobs


# ---------------------------------------------------------------------------
# Input parsing and validation
# ---------------------------------------------------------------------------


def split_inputs(raw: str, *, limit: int) -> tuple[list[str], bool]:
    """Split the ``sources`` argument into items.

    Accepts a JSON array string, or newline-/comma-separated entries. Returns
    ``(items, truncated)``. Newline separation is safest for URLs that contain
    commas; a JSON array always works.
    """
    text = (raw or "").strip()
    if not text:
        return [], False
    items: list[str]
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            items = [str(entry).strip() for entry in parsed if str(entry).strip()]
            trunc = limit > 0 and len(items) > limit
            return items[:limit] if trunc else items, trunc
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) == 1:
        lines = [part.strip() for part in lines[0].split(",") if part.strip()]
    truncated = limit > 0 and len(lines) > limit
    return (lines[:limit] if truncated else lines), truncated


def classify_input(raw: str) -> tuple[str, str]:
    """Route one raw input to ``url`` or ``file`` (anything else is invalid)."""
    stripped = (raw or "").strip()
    match = _SCHEME_RE.match(stripped)
    scheme = match.group(0)[:-1].lower() if match else ""
    if match:
        # A single-letter scheme followed by a backslash is a Windows drive
        # path (C:\docs\file.md), not a URL.
        if len(scheme) == 1 and stripped[len(scheme) + 1 : len(scheme) + 2] == "\\":
            return "file", stripped
    if scheme or stripped.lower().startswith("search:"):
        return "url" if scheme in ("http", "https") else "invalid", stripped
    return "file", stripped


def classify_failure(exc: BaseException) -> dict[str, Any]:
    """Map an exception to pipeline semantics: stable code + retriable flag."""
    if not isinstance(exc, ToolError):
        return {"code": "internal", "retriable": False}
    http_status = getattr(exc, "http_status", None)
    if http_status is not None:
        if http_status == 404:
            code = "not_found"
        elif http_status in (401, 403):
            code = "access_denied"
        elif http_status == 429:
            code = "rate_limited"
        elif http_status >= 500:
            code = "server_error"
        else:
            code = "http_error"
        return {
            "code": code,
            "retriable": code in ("rate_limited", "server_error"),
        }
    return {"code": exc.code, "retriable": exc.code in ("rate_limited", "network_error")}


def attempt_with_retries(action, settings: Settings, *, retries_allowed: bool):
    """Run *action*, retrying only transient failures with bounded backoff.

    Never retries permanent failures (invalid arguments, 404/403, unsupported
    formats, oversized payloads, storage errors, ...).
    """
    attempts = (settings.collect_max_retries + 1) if retries_allowed else 1
    for attempt in range(attempts):
        try:
            return action()
        except ToolError as exc:
            if not classify_failure(exc)["retriable"] or attempt >= attempts - 1:
                raise
            wait = min(settings.collect_backoff * (2**attempt), 4.0)
            LOGGER.warning(
                "transient failure (attempt %d/%d); retrying in %.1fs", attempt + 1, attempts, wait
            )
            time.sleep(wait)


# ---------------------------------------------------------------------------
# Job processing
# ---------------------------------------------------------------------------


@dataclass
class JobItem:
    ack: int  # input order (acknowledged index in the response)
    kind: str  # "url" | "file" | "invalid" | "search"
    label: str


@dataclass
class ProcessedRecord:
    item: JobItem
    record: SourceRecord
    requested: str
    source_id: int | None = None
    created: bool | None = None
    duplicate_of: int | None = None
    duplicate_kind: str | None = None


def _failure_record(item: JobItem, exc: BaseException) -> SourceRecord:
    """A record for an item that failed before/around extraction."""
    classified = classify_failure(exc)
    if isinstance(exc, ToolError):
        message = exc.message
        status = "unsupported" if exc.code in ("unsupported_content",) else "failed"
    else:  # unexpected; keep type name, never dump internals/contents
        message = f"{exc.__class__.__name__}: {exc}"
        status = "failed"
    return SourceRecord(
        source_type="web" if item.kind == "url" else "other",
        title=item.label[:200],
        original_url=item.label if item.kind == "url" else None,
        file_path=item.label if item.kind == "file" else None,
        extraction_status=status,
        error_code=classified["code"],
        error_message=message,
    )


def process_item(item: JobItem, settings: Settings, *, max_chars: int | None) -> SourceRecord:
    """Validation -> retrieval -> extraction for one item. Never persists."""
    if item.kind == "invalid":
        record = SourceRecord(source_type="other", title=item.label[:200])
        record.original_url = item.label
        record.set_failure(
            "invalid_argument",
            "Unsupported scheme (only http/https URLs, local file paths, or an explicit "
            "search query are accepted).",
        )
        return record
    try:
        if item.kind == "url":
            prepared = attempt_with_retries(
                lambda: adapters.prepare_url(item.label, settings), settings, retries_allowed=True
            )
            return adapters.record_from_prepared(
                prepared, settings=settings, max_chars=max_chars
            )
        resolved = localfs.resolve_allowed(item.label, settings)
        prepared = adapters.prepare_file(resolved, settings)  # local: no retries
        return adapters.record_from_prepared(prepared, settings=settings, max_chars=max_chars)
    except ToolError as exc:
        return _failure_record(item, exc)
    except Exception as exc:  # noqa: BLE001 - full isolation per item
        LOGGER.exception("unexpected failure collecting %r", item.label)
        return _failure_record(item, exc)


def run_search(search_query: str, *, max_results: int, settings: Settings) -> list[SourceRecord]:
    """Ingest one search query as search_result records (snippets only)."""
    cleaned = (search_query or "").strip()
    try:
        records, _provider = adapters.records_from_search(
            cleaned, max_results=max_results, settings=settings
        )
        return records
    except ToolError as exc:
        failed = SourceRecord(source_type="search_result", title=f"search: {cleaned}")
        failed.set_failure(classify_failure(exc)["code"], f"search failed: {exc.message}")
        return [failed]
    except Exception as exc:  # noqa: BLE001 - never abort the whole job
        failed = SourceRecord(source_type="search_result", title=f"search: {cleaned}")
        failed.set_failure("provider_error", f"search failed: {exc.__class__.__name__}: {exc}")
        return [failed]


# ---------------------------------------------------------------------------
# Near-duplicate similarity (bounded, no models)
# ---------------------------------------------------------------------------


def word_set(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[0-9a-z']+", text.lower()))


def best_similarity(candidate: SourceRecord, prior: list[ProcessedRecord]) -> tuple[float, int | None]:
    """Max word-set Jaccard similarity against prior in-job stored entries."""
    if not candidate.extracted_text or len(prior) > _NEAR_COMPARE_LIMIT:
        return 0.0, None
    candidate_words = word_set(candidate.extracted_text)
    if not candidate_words:
        return 0.0, None
    best, best_id = 0.0, None
    for other in prior:
        if other.source_id is None:
            continue
        other_words = word_set(other.record.extracted_text)
        if not other_words:
            continue
        union = len(candidate_words | other_words)
        if union == 0:
            continue
        similarity = len(candidate_words & other_words) / union
        if similarity > best:
            best, best_id = similarity, other.source_id
    return best, best_id


def default_cap(source_type: str, settings: Settings) -> int:
    """Per-type default output cap (command max_chars overrides, 0 = these)."""
    if source_type in ("web", "html", "text"):
        return settings.fetch_max_chars
    if source_type == "pdf":
        return settings.pdf_max_chars
    return settings.doc_max_chars


# ---------------------------------------------------------------------------
# Collection entry point
# ---------------------------------------------------------------------------


def collect(
    sources_raw: str,
    *,
    search_query: str = "",
    search_limit: int = 0,
    max_chars: int = 0,
    save: bool = True,
    include_text: bool = False,
    concurrency: int = 0,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run the full pipeline. Returns summary + per-item results + warnings."""
    settings = settings or load_settings()
    started = time.monotonic()
    warnings: list[str] = []

    raw_items, truncated = split_inputs(sources_raw, limit=settings.collect_max_items)
    if truncated:
        warnings.append(
            f"Source list truncated to {settings.collect_max_items} items (AGLETRAS_COLLECT_MAX_ITEMS)."
        )

    items: list[JobItem] = []
    for index, raw in enumerate(raw_items):
        kind, label = classify_input(raw)
        items.append(JobItem(ack=index, kind=kind, label=label))

    search_records: list[SourceRecord] = []
    search_item = JobItem(ack=-1, kind="search", label=f"search: {search_query.strip()}")
    if search_query.strip():
        limit = search_limit if search_limit and search_limit > 0 else 5
        search_records = run_search(
            search_query, max_results=min(limit, settings.max_search_results), settings=settings
        )

    if not items and not search_records:
        raise ToolError("invalid_argument", "No sources given; provide sources and/or a search query.")

    # 1) retrieval + extraction (bounded concurrency; failures are isolated)
    item_results: list[tuple[JobItem, SourceRecord]] = []
    if items:
        workers = max(
            1,
            min(
                concurrency if concurrency and concurrency > 0 else settings.collect_concurrency,
                len(items),
                16,
            ),
        )
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(process_item, item, settings, max_chars=max_chars or None): item for item in items}
            for future in futures:
                item = futures[future]
                try:
                    record = future.result()
                except Exception as exc:  # noqa: BLE001 - defensive isolation
                    LOGGER.exception("worker crashed for %r", item.label)
                    record = _failure_record(item, exc)
                item_results.append((item, record))
    else:
        item_results = []

    # 2) normalization + checksums, in deterministic input order
    entries: list[ProcessedRecord] = []
    for item, record in item_results:
        finalize_text(record, max_chars=max_chars or default_cap(record.source_type, settings))
        entries.append(ProcessedRecord(item=item, record=record, requested=item.label))
    for record in search_records:
        finalize_text(record, max_chars=max_chars or default_cap(record.source_type, settings))
        entries.append(ProcessedRecord(item=search_item, record=record, requested=search_item.label))

    # 3) deduplication + persistence (sequential, single-writer)
    first_by_checksum: dict[str, ProcessedRecord] = {}
    first_by_shape: dict[str, ProcessedRecord] = {}
    stored: list[ProcessedRecord] = []  # successful, non-duplicate entries so far
    for entry in entries:
        record = entry.record
        if record.extraction_status not in ("success", "partial") or not record.extracted_text.strip():
            continue
        checksum = record.checksum or ""
        shape = record.normalized_sha256 or ""
        # Within-job exact duplicate: identical text to an earlier entry.
        if checksum and checksum in first_by_checksum:
            _mark_duplicate(entry, first_by_checksum[checksum], "exact", save, settings)
            continue
        # Within-job near duplicate: identical word-sequence shape.
        if shape and shape in first_by_shape:
            _mark_duplicate(entry, first_by_shape[shape], "near", save, settings)
            continue
        # Register this entry as the job's first occurrence of its content.
        if checksum:
            first_by_checksum[checksum] = entry
        if shape:
            first_by_shape[shape] = entry
        stored.append(entry)
        if not save:
            continue
        try:
            source_id, created, match_kind = storage.save_record(
                record, origin=entry.requested, settings=settings
            )
        except ToolError as exc:
            if exc.code == "invalid_argument":  # empty extracted text
                record.add_warning("not saved: extracted text is empty")
                continue
            LOGGER.warning("saving source failed: %s", exc.message)
            record.set_failure("storage_error", exc.message, status=record.extraction_status)
            continue
        entry.source_id = source_id
        entry.created = created
        if not created:  # duplicate against the persistent store
            entry.duplicate_of = source_id
            entry.duplicate_kind = match_kind
            continue
        # Bounded near-duplicate scan among entries stored in this job.
        similarity, similar_id = best_similarity(record, stored[:-1])
        if similarity >= _NEAR_SIMILARITY_THRESHOLD and similar_id is not None:
            storage.append_origin(similar_id, entry.requested, settings=settings)
            _mark_duplicate(entry, similar_id, "near", save, settings)

    # 4) summary + results
    status_counts: dict[str, int] = {}
    for entry in entries:
        status_counts[entry.record.extraction_status] = status_counts.get(entry.record.extraction_status, 0) + 1
    summary = {
        "requested": len(entries),
        "succeeded": status_counts.get("success", 0),
        "partial": status_counts.get("partial", 0),
        "failed": status_counts.get("failed", 0),
        "unsupported": status_counts.get("unsupported", 0),
        "skipped": status_counts.get("skipped", 0),
        "duplicates": sum(1 for entry in entries if entry.duplicate_kind is not None),
        "saved": sum(1 for entry in entries if entry.created),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    return {
        "summary": summary,
        "results": [result_payload(entry, include_text=include_text) for entry in entries],
        "warnings": warnings,
    }


def _mark_duplicate(
    entry: ProcessedRecord,
    first: ProcessedRecord,
    kind: str,
    save: bool,
    settings: Settings,
) -> None:
    """Annotate an entry as a duplicate of *first* (this job's kept occurrence).

    Provenance survives: the requested origin is appended to the surviving
    record's stored metadata when it has been saved.
    """
    entry.duplicate_kind = kind
    if first.source_id is not None:
        entry.duplicate_of = first.source_id
        entry.source_id = first.source_id
        entry.created = False
        if save:
            storage.append_origin(first.source_id, entry.requested, settings=settings)


# ---------------------------------------------------------------------------
# Response payload
# ---------------------------------------------------------------------------


def result_payload(entry: ProcessedRecord, *, include_text: bool) -> dict[str, Any]:
    """One tool-facing result: provenance + statuses + excerpt (or full text)."""
    record = entry.record
    payload: dict[str, Any] = {
        "label": entry.requested,
        "source_type": record.source_type,
        "title": record.title,
        "original_url": record.original_url,
        "final_url": record.final_url,
        "file_path": record.file_path,
        "publication_date": record.publication_date,
        "retrieved_at": record.retrieved_at,
        "content_type": record.content_type,
        "file_size_bytes": record.file_size_bytes,
        "checksum": record.checksum,
        "extraction_status": record.extraction_status,
        "error_code": record.error_code,
        "error_message": record.error_message,
        "metadata": record.metadata,
        "warnings": record.warnings,
        "chars": len(record.extracted_text),
        "excerpt": excerpt(record.extracted_text) if record.extracted_text else "",
        "source_id": entry.source_id,
        "created": entry.created,
    }
    if entry.duplicate_of is not None:
        payload["duplicate_of"] = entry.duplicate_of
        payload["duplicate_kind"] = entry.duplicate_kind
    elif entry.duplicate_kind is not None:
        payload["duplicate"] = "within_job"
        payload["duplicate_kind"] = entry.duplicate_kind
    if include_text:
        payload["text"] = record.extracted_text
    return payload

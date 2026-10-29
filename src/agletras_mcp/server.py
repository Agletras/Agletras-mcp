"""Agletras research MCP server: tool registration and shared error handling.

Conventions:
- Every tool returns a single JSON string (see :mod:`agletras_mcp.results`).
- Tool failures never crash the server: expected failures raise
  :class:`~agletras_mcp.errors.ToolError` and become ``{"ok": false, ...}``
  payloads; unexpected exceptions are logged and reported as ``internal``.
- Nothing ever writes to stdout; the stdio transport owns that stream. All
  logging goes to stderr.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from . import (
    __version__,
    collect,
    evidence,
    images,
    localfs,
    memory,
    monitor,
    pdf,
    planner,
    report,
    search,
    storage,
    synthesis,
    verification,
    webfetch,
)
from .config import load_settings
from .errors import ToolError
from .results import fail, ok
from .source_record import finalize_text

LOGGER = logging.getLogger(__name__)

TOOL_NAMES = (
    "health_check",
    "fetch_url",
    "search_web",
    "extract_pdf",
    "collect_sources",
    "collect_local_folder",
    "extract_document",
    "save_source",
    "get_source",
    "list_sources",
    "search_sources",
    "export_report",
    "stats",
    # Phase 3: evidence library
    "start_research_run",
    "save_evidence",
    "search_evidence",
    "register_claim",
    "attach_citation",
    "get_claim_citations",
    "get_research_history",
    "generate_report",
    "validate_report",
    # Phase 4: research planner / orchestrator
    "save_research_plan",
    "update_subquestion",
    "log_run_event",
    "start_followup_round",
    "finish_research_run",
    # Phase 5: image research and visual analysis
    "search_images",
    "analyze_image",
    # Phase 6: claim extraction, verification, and synthesis reports
    "extract_claims",
    "verify_claims",
    "analyze_conflicts",
    "generate_markdown_report",
    "get_report",
    # Phase 8: project memory, monitoring, and change detection
    "search_memory",
    "monitor_add_target",
    "monitor_check",
    "monitor_changes",
)

SERVER_INSTRUCTIONS = """Agletras exposes local, zero-cost research tools.
Workflow: discover sources with search_web, then collect material with
collect_sources (web URLs, local documents, or an optional search query),
collect_local_folder for an approved folder, or extract_document for one
file. Collected sources persist in the local store with provenance, checksums,
and extraction statuses. Recall them with search_sources/list_sources and
export_report to produce a Markdown report.

Evidence workflow (Phase 3): start_research_run creates a run; save_evidence
stores a source (URL/path), an existing source_id, or raw text into the
evidence library with content-cache reuse, deduplication by canonical
identity, and passage indexing. search_evidence finds exact passages;
register_claim records each significant factual claim; attach_citation links a
claim to one exact passage (a quote must occur in the passage — never
fabricate). generate_report renders stable [Sn] citation identifiers with a
references section and validates it; validate_report re-checks a report.
Claims without adequate evidence stay visibly UNVERIFIED and are never
presented as verified.

Orchestrated research (Phase 4): save_research_plan records the objective,
scope, assumptions, subquestions, and the run limits (defaults: 8
subquestions, 2 follow-up rounds, 4 candidate sources per subquestion per
round, 20 unique sources per run — enforced by the tools). log_run_event
appends tool actions, queries, gaps, and errors to the run's append-only
execution log; start_followup_round logs and bounds each follow-up round;
finish_research_run records the stopping reason and closes the run. Pass
run_id to save_evidence/search_evidence to keep evidence scoped to its run,
and call get_research_history(run_id) to see the plan, subquestions, budget,
gaps, stopping state, and the log. Every tool returns a JSON string; check
the "ok" field before using other fields. health_check reports server status
without any network activity.

Image research (Phase 5): search_images discovers images through a free,
documented source (Wikimedia Commons by default) and returns structured
metadata (URL, source page, creator, license, dimensions, retrieval time)
without inventing missing values. analyze_image returns that discovery
metadata plus structured visual analysis (observations / inferences /
unverified claims / limitations). Visual analysis is only performed when a
vision model endpoint is explicitly configured
(AGLETRAS_VISION_MODEL_ENDPOINT); otherwise analyze_image reports
vision_unavailable and never pretends the image was inspected.

Claim verification and synthesis (Phase 6): extract_claims proposes
candidate statements from stored sources (deterministic, no model) and maps
them to the passages that discuss them; verify_claims compares each claim
with the exact stored passage text and records an explicit status —
SUPPORTED, PARTIALLY SUPPORTED, CONTRADICTED, CONFLICTING EVIDENCE,
UNSUPPORTED, or UNVERIFIABLE — with method, confidence, rationale, and
limitations. Model-based verification is opt-in
(AGLETRAS_VERIFICATION_MODEL_ENDPOINT); without it the transparent
deterministic checks run, and a model result that cannot cite stored
passages is rejected. analyze_conflicts lists every disagreement with the
evidence on both sides (unresolved conflicts are never papered over) and
source-quality heuristics kept separate from verification statuses.
generate_markdown_report renders the ten-section verified report (executive
summary, scope/methodology, findings - facts vs inferences vs unverified
statements kept apart - evidence comparison, conflicting evidence,
limitations/open questions, conclusion, references with stable [Sn] ids,
image attribution) and validates its integrity; get_report returns one
stored report; validate_report re-runs the checks for either template."""

server = MCPServer(
    name="agletras",
    title="Agletras Research Tools",
    version=__version__,
    instructions=SERVER_INSTRUCTIONS,
)


def tool_safe(fn: Callable[..., str]) -> Callable[..., str]:
    """Convert ToolError and unexpected exceptions into JSON error envelopes."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except ToolError as exc:
            LOGGER.warning("tool %s failed [%s]: %s", fn.__name__, exc.code, exc.message)
            return fail(exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - top-level tool guard
            LOGGER.exception("tool %s crashed", fn.__name__)
            return fail("internal", f"{exc.__class__.__name__}: {exc}")

    return wrapper


def _parse_int_list(raw: str, *, what: str) -> list[int]:
    """Parse an optional id list from a JSON array or comma/space-separated text."""
    clean = (raw or "").strip()
    if not clean:
        return []
    if clean.startswith("["):
        try:
            loaded = json.loads(clean)
        except json.JSONDecodeError as exc:
            raise ToolError("invalid_argument", f"{what} must be a JSON array of integers: {exc}") from exc
        if not isinstance(loaded, list):
            raise ToolError("invalid_argument", f"{what} must be a JSON array of integers")
        items: list[Any] = loaded
    else:
        items = [item for item in re.split(r"[,\s]+", clean) if item]
    parsed: list[int] = []
    for item in items:
        try:
            parsed.append(int(item))
        except (TypeError, ValueError) as exc:
            raise ToolError(
                "invalid_argument", f"{what} must contain integers only; got {item!r}"
            ) from exc
    return parsed


@server.tool(description="Return server status, name, version, transport, storage status, and supported tools. Performs no network requests.")
@tool_safe
def health_check() -> str:
    """Liveness probe: local status only, never claims network connectivity."""
    settings = load_settings()
    storage_status = storage.ping(settings)
    return ok(
        status="ok" if storage_status["ok"] else "degraded",
        server=server.name,
        version=__version__,
        transport="stdio",
        storage=storage_status,
        tools=list(TOOL_NAMES),
        network="not tested - health_check performs no external requests",
    )


@server.tool(
    description="Search the open web with a free provider (duckduckgo HTML endpoint or wikipedia API; AGLETRAS_SEARCH_PROVIDER). Returns real results with title, url, snippet, and source. Never fabricates results."
)
@tool_safe
def search_web(query: str, max_results: int = 5) -> str:
    """Search the open web and return normalized results.

    Args:
        query: Search terms (required, non-empty).
        max_results: Maximum results to return (default 5; bounded by
            AGLETRAS_MAX_SEARCH_RESULTS, default cap 10).
    """
    settings = load_settings()
    result = search.search_web(query, max_results=max_results, settings=settings)
    return ok(
        query=result["query"],
        provider=result["provider"],
        count=result["count"],
        results=result["results"],
    )


@server.tool(
    description="Fetch an http(s) web page and return readable text. Set save=true to store it as a citable source."
)
@tool_safe
def fetch_url(url: str, max_chars: int = 0, save: bool = False, title: str = "", notes: str = "") -> str:
    """Fetch a web page and extract readable text (scripts/styles removed).

    Args:
        url: http or https URL to fetch.
        max_chars: Cap on returned characters (0 = server default; bounded
            by AGLETRAS_MAX_CHARS_CAP).
        save: Persist the extracted text as a source for citation.
        title: Optional title for the saved source (defaults to the page title).
        notes: Optional notes attached to the saved source.
    """
    settings = load_settings()
    if max_chars < 0:
        raise ToolError("invalid_argument", f"max_chars must be >= 0, got {max_chars}")
    bounded = min(max_chars, settings.max_chars_cap) if max_chars else 0
    result = webfetch.fetch_url_text(url, settings, max_chars=bounded or None)
    payload: dict[str, Any] = {
        "url": result["url"],
        "final_url": result["final_url"],
        "content_type": result["content_type"],
        "title": result["title"],
        "chars": result["chars"],
        "truncated": result["truncated"],
        "text": result["text"],
    }
    if save:
        display_title = title.strip() or result["title"] or result["final_url"]
        source_id, created = storage.save_source(
            kind="url",
            title=display_title,
            url=result["final_url"],
            content=result["text"],
            notes=notes,
            settings=settings,
        )
        payload.update(source_id=source_id, saved=True, created=created)
    return ok(**payload)


@server.tool(
    description="Extract text from a PDF (local path or http/https URL) using pypdf. Set save=true to store it as a citable source."
)
@tool_safe
def extract_pdf(
    source: str,
    max_pages: int = 0,
    max_chars: int = 0,
    save: bool = False,
    title: str = "",
    notes: str = "",
) -> str:
    """Extract PDF text page by page.

    Args:
        source: Local PDF path (absolute, or relative to the project root) or URL.
        max_pages: Stop after this many pages (0 = server default).
        max_chars: Cap on returned characters (0 = server default).
        save: Persist the extracted text as a source for citation.
        title: Optional title for the saved source (defaults to PDF metadata).
        notes: Optional notes attached to the saved source.
    """
    settings = load_settings()
    result = pdf.extract_text(
        source,
        max_pages=max_pages or None,
        max_chars=max_chars or None,
        settings=settings,
    )
    payload: dict[str, Any] = {
        "source": result["source"],
        "path": result["path"],
        "url": result["url"],
        "page_count": result["page_count"],
        "pages_extracted": result["pages_extracted"],
        "has_text": result["has_text"],
        "chars": result["chars"],
        "truncated": result["truncated"],
        "metadata": result["metadata"],
        "warnings": result["warnings"],
        "text": result["text"],
    }
    if save:
        metadata_title = result["metadata"].get("title", "")
        display_title = title.strip() or metadata_title or f"PDF: {result['source']}"
        source_id, created = storage.save_source(
            kind="pdf",
            title=display_title,
            url=result["url"],
            path=result["path"],
            content=result["text"],
            notes=notes,
            settings=settings,
        )
        payload.update(source_id=source_id, saved=True, created=created)
    return ok(**payload)


@server.tool(
    description=(
        "Collect multiple sources in one request: http(s) URLs, supported local document paths "
        "(pdf/md/txt/csv/docx/xlsx/html), and/or an optional web-search query ingested as snippet "
        "records. Extraction runs per source with bounded concurrency; one failure never aborts "
        "the job. Set include_text=true to return full text per source (default: 400-char excerpt)."
    )
)
@tool_safe
def collect_sources(
    sources: str = "",
    search: str = "",
    search_limit: int = 0,
    max_chars: int = 0,
    save: bool = True,
    include_text: bool = False,
    concurrency: int = 0,
) -> str:
    """Run the collection pipeline over many sources at once.

    Args:
        sources: Newline- or comma-separated list, or a JSON array of strings.
            Each item is an http(s) URL or a permitted local file path.
        search: Optional web-search query; results are ingested as
            ``search_result`` records (title, URL, snippet - never fetched as
            full pages).
        search_limit: Max search results to ingest (0 = default 5).
        max_chars: Per-source text cap; 0 = per-type defaults.
        save: Persist successful extractions into the store (default true).
        include_text: Return the full extracted text in each result.
        concurrency: Max parallel workers (0 = AGLETRAS_COLLECT_CONCURRENCY).
    """
    settings = load_settings()
    if max_chars < 0 or search_limit < 0 or concurrency < 0:
        raise ToolError("invalid_argument", "negative counts are not allowed")
    bounded_chars = min(max_chars, settings.max_chars_cap) if max_chars else 0
    result = collect.collect(
        sources,
        search_query=search,
        search_limit=search_limit,
        max_chars=bounded_chars,
        save=save,
        include_text=include_text,
        concurrency=concurrency,
        settings=settings,
    )
    return ok(**result)


@server.tool(
    description=(
        "Collect supported files from one explicitly permitted local folder (access roots: "
        "AGLETRAS_LOCAL_ROOTS). Recursive by default; skippable temporary/unsupported files are "
        "reported with reasons. Set include_text=true to return full text per file."
    )
)
@tool_safe
def collect_local_folder(
    folder: str,
    recursive: bool = True,
    extensions: str = "",
    max_files: int = 0,
    save: bool = True,
    include_text: bool = False,
) -> str:
    """Walk a folder and collect every supported file through the pipeline.

    Args:
        folder: Directory inside the configured local access roots.
        recursive: Traverse subdirectories (bounded by AGLETRAS_FOLDER_MAX_DEPTH).
        extensions: Optional comma-separated allowlist (e.g. "md,csv").
        max_files: Cap on files collected (0 = AGLETRAS_FOLDER_MAX_FILES).
        save: Persist successful extractions (default true).
        include_text: Return full extracted text per collected file.
    """
    settings = load_settings()
    if max_files < 0:
        raise ToolError("invalid_argument", "max_files must be >= 0")
    allowed = localfs.resolve_allowed(folder, settings)
    if not allowed.is_dir():
        raise ToolError("invalid_argument", f"Not a directory: {allowed}")
    extension_list = tuple(part.strip() for part in extensions.split(",") if part.strip())
    scan = localfs.scan_folder(
        allowed,
        settings,
        recursive=recursive,
        extensions=extension_list,
        max_files=max_files or None,
    )
    result = collect.collect(
        "\n".join(str(path) for path in scan["picked"]),
        save=save,
        include_text=include_text,
        settings=settings,
    )
    summary = dict(result["summary"])
    summary.update(
        {
            "folder": str(allowed),
            "recursive": recursive,
            "files_picked": len(scan["picked"]),
            "files_skipped": len(scan["skipped"]),
            "scan_truncated": scan["truncated"],
        }
    )
    return ok(**{**result, "summary": summary}, skipped=scan["skipped"])


@server.tool(
    description=(
        "Extract text and metadata from one supported document: local path (pdf/md/txt/csv/"
        "docx/xlsx/html) or http(s) URL. Uses the same unified record as collect_sources; add "
        "save=true to store it."
    )
)
@tool_safe
def extract_document(
    source: str,
    max_chars: int = 0,
    save: bool = False,
    title: str = "",
    notes: str = "",
) -> str:
    """Extract one document (or web page/PDF URL) into a unified record.

    Args:
        source: Local path (must be inside the configured local roots) or an
            http(s) URL.
        max_chars: Cap on extracted text (0 = per-type default).
        save: Persist to the store (default false; matching extract_pdf).
        title: Optional display title (defaults to a document-derived title).
        notes: Optional notes attached when saving.
    """
    settings = load_settings()
    if max_chars < 0:
        raise ToolError("invalid_argument", "max_chars must be >= 0")
    bounded = min(max_chars, settings.max_chars_cap) if max_chars else 0
    item = collect.JobItem(ack=0, kind=collect.classify_input(source)[0], label=source)
    record = collect.process_item(item, settings, max_chars=bounded or None)
    cap = bounded or collect.default_cap(record.source_type, settings)
    finalize_text(record, max_chars=cap)
    payload = collect.result_payload(
        collect.ProcessedRecord(item=item, record=record, requested=source), include_text=True
    )
    if record.extraction_status in ("failed", "unsupported"):
        raise ToolError(
            record.error_code or "extraction_failed",
            record.error_message or f"extraction failed for {source}",
        )
    if save:
        source_id, created, _match_kind = storage.save_record(
            record, origin=source, notes=notes, settings=settings
        )
        if title.strip():
            storage.set_source_title(source_id, title.strip(), settings=settings)
            payload["title"] = title.strip()
        payload.update(source_id=source_id, saved=True, created=created)
    return ok(**payload)


@server.tool(description="Save a research source (url, pdf, or note) with its content for citation tracking.")
@tool_safe
def save_source(
    kind: str,
    title: str,
    content: str,
    url: str = "",
    path: str = "",
    notes: str = "",
) -> str:
    """Store a source record in the local research database.

    Args:
        kind: One of "url", "pdf", or "note".
        title: Human-readable title used in reports.
        content: Full text content to store (required, de-duplicated by hash).
        url: Optional source URL.
        path: Optional local file path.
        notes: Optional free-form notes.
    """
    source_id, created = storage.save_source(
        kind=kind,
        title=title,
        content=content,
        url=url or None,
        path=path or None,
        notes=notes,
    )
    return ok(source_id=source_id, created=created, kind=kind, title=title)


@server.tool(description="Fetch one stored source by id, optionally without its full content, and optionally with its indexed evidence passages.")
@tool_safe
def get_source(source_id: int, include_content: bool = True, include_passages: bool = False) -> str:
    """Return one stored source record with its evidence-library extras.

    Args:
        source_id: The source id shown by list_sources/search_sources.
        include_content: Include the full stored text (default true).
        include_passages: Also return the indexed passages (Phase 3; default false).
    """
    record = evidence.get_source_by_id(
        source_id, include_content=include_content, include_passages=include_passages
    )
    if record is None:
        raise ToolError("not_found", f"No source with id {source_id}")
    return ok(source=record)


@server.tool(description="List stored sources (newest first) with optional kind/source_type filters and pagination.")
@tool_safe
def list_sources(limit: int = 0, offset: int = 0, kind: str = "", source_type: str = "") -> str:
    """List stored sources, newest first.

    Args:
        limit: Maximum records to return (0 = server default).
        offset: Number of records to skip.
        kind: Optional coarse filter: "url", "pdf", "note", "search_result", or "doc".
        source_type: Optional precise filter: "web", "search_result", "pdf",
            "markdown", "text", "csv", "docx", or "xlsx".
    """
    records = storage.list_sources(
        limit=limit or None, offset=offset, kind=kind or None, source_type=source_type or None
    )
    return ok(count=len(records), sources=records)


@server.tool(description="Keyword search across all stored sources (title, url, notes, content).")
@tool_safe
def search_sources(query: str, limit: int = 0) -> str:
    """Search stored sources by keyword.

    Args:
        query: Case-insensitive keyword or phrase.
        limit: Maximum records to return (0 = server default).
    """
    records = storage.search_sources(query, limit=limit or None)
    return ok(query=query, count=len(records), sources=records)


@server.tool(description="Export a Markdown research report from saved sources (by ids, query, or newest).")
@tool_safe
def export_report(
    title: str,
    source_ids: str = "",
    query: str = "",
    limit: int = 20,
    include_excerpts: bool = True,
) -> str:
    """Write a Markdown report with a citation list.

    Args:
        title: Report title (also used for the filename).
        source_ids: Comma-separated source ids (e.g. "3,7,11"). Empty = use query/newest.
        query: Optional keyword selection when source_ids is empty.
        limit: Maximum sources when selecting by query or recency.
        include_excerpts: Include content excerpts in each citation.
    """
    ids: list[int] = []
    if source_ids.strip():
        try:
            ids = [int(part.strip()) for part in source_ids.split(",") if part.strip()]
        except ValueError as exc:
            raise ToolError("invalid_argument", f"source_ids must be comma-separated integers: {source_ids!r}") from exc
    result = report.export_report(
        title,
        source_ids=ids or None,
        query=query or None,
        limit=limit,
        include_excerpts=include_excerpts,
    )
    return ok(**result)


@server.tool(description="Show counts, kinds, and location of the local research store.")
@tool_safe
def stats() -> str:
    """Return storage statistics for the research database."""
    return ok(**storage.stats())


# ---------------------------------------------------------------------------
# Phase 3: evidence library tools
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "Start a research run: the unit that evidence, claims, and validated reports attach to. "
        "Returns the run_id used by save_evidence, register_claim, and generate_report."
    )
)
@tool_safe
def start_research_run(query: str, config: str = "") -> str:
    """Create a research run.

    Args:
        query: The research question this run addresses (required).
        config: Optional JSON object with caller-defined settings (e.g.
            "{\"depth\": 2}"); stored verbatim for provenance.
    """
    parsed: dict[str, Any] | None = None
    if config.strip():
        try:
            loaded = json.loads(config)
        except json.JSONDecodeError as exc:
            raise ToolError("invalid_argument", f"config must be a JSON object: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ToolError("invalid_argument", "config must be a JSON object")
        parsed = loaded
    return ok(**evidence.start_run(query, config=parsed))


@server.tool(
    description=(
        "Save one piece of evidence into the library: an http(s) URL or permitted local file "
        "path (retrieved, cached by canonical identity, split into indexed passages), an existing "
        "source_id (promotes a stored source), or raw text with optional url/path provenance. "
        "Provide exactly one mode. Returns source_id, passage ids (via search_evidence), and "
        "cache/change flags."
    )
)
@tool_safe
def save_evidence(
    source: str = "",
    source_id: int = 0,
    text: str = "",
    title: str = "",
    url: str = "",
    path: str = "",
    notes: str = "",
    run_id: int = 0,
    refresh: bool = False,
    max_chars: int = 0,
) -> str:
    """Persist evidence with deduplication, caching, and passage indexing.

    Args:
        source: An http(s) URL or permitted local file path to retrieve and extract.
        source_id: Id of an already-stored source to register as evidence.
        text: Raw text to store directly (paste mode).
        title: Optional display title override.
        url: Optional provenance URL when saving raw text.
        path: Optional provenance path when saving raw text.
        notes: Optional notes appended to the source record.
        run_id: Optional research run to associate this evidence with.
        refresh: Ignore the cache and refetch/re-extract (default false).
        max_chars: Cap on stored text (0 = per-type default).
    """
    if max_chars < 0:
        raise ToolError("invalid_argument", "max_chars must be >= 0")
    settings = load_settings()
    bounded = min(max_chars, settings.max_chars_cap) if max_chars else None
    return ok(
        **evidence.save_evidence(
            source=source,
            source_id=source_id,
            text=text,
            title=title,
            url=url,
            path=path,
            notes=notes,
            run_id=run_id,
            refresh=refresh,
            max_chars=bounded,
            settings=settings,
        )
    )


@server.tool(
    description=(
        "Search stored evidence passages by keyword/phrase (FTS5 with a LIKE fallback). Returns "
        "exact passage text with passage_id, source id, title, URL/path, content hash, and locator. "
        "Use passage_id with attach_citation."
    )
)
@tool_safe
def search_evidence(query: str, limit: int = 10, run_id: int = 0) -> str:
    """Find exact passages matching claim/search terms.

    Args:
        query: Keywords or a phrase (stopwords dropped, case-insensitive).
        limit: Maximum passages to return (default 10).
        run_id: Optional: restrict to sources associated with one research run.
    """
    if limit < 0:
        raise ToolError("invalid_argument", "limit must be >= 0")
    return ok(**evidence.search_evidence(query, limit=limit or None, run_id=run_id))


@server.tool(
    description=(
        "Register a significant factual claim on a research run. Claims start UNVERIFIED and are "
        "never marked supported by registration; attach_citation links exact passages."
    )
)
@tool_safe
def register_claim(run_id: int, claim_text: str) -> str:
    """Record one factual claim of a run.

    Args:
        run_id: The run this claim belongs to.
        claim_text: The claim statement (required; whitespace-normalized).
    """
    return ok(**evidence.register_claim(run_id, claim_text))


@server.tool(
    description=(
        "Attach one exact stored passage to a claim and re-derive the claim's status from the "
        "citation relations. A non-empty quote must actually occur in the passage (fabricated "
        "quotations are rejected). Relation: supports, partially_supports, contradicts, context, "
        "or needs_review."
    )
)
@tool_safe
def attach_citation(
    claim_id: int,
    passage_id: int,
    source_id: int = 0,
    quote: str = "",
    locator: str = "",
    relation: str = "supports",
    metadata: str = "",
) -> str:
    """Link a claim to a passage; recompute the claim status honestly.

    Args:
        claim_id: The claim to cite.
        passage_id: The exact passage id from search_evidence.
        source_id: Optional cross-check: must match the passage's source.
        quote: Optional exact excerpt from the passage (verified against it).
        locator: Optional locator override (defaults to the passage's locator).
        relation: How the passage relates: supports (default), partially_supports,
            contradicts, context, or needs_review.
        metadata: Optional JSON object stored with the citation.
    """
    parsed: dict[str, Any] | None = None
    if metadata.strip():
        try:
            loaded = json.loads(metadata)
        except json.JSONDecodeError as exc:
            raise ToolError("invalid_argument", f"metadata must be a JSON object: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ToolError("invalid_argument", "metadata must be a JSON object")
        parsed = loaded
    return ok(
        **evidence.attach_citation(
            claim_id,
            passage_id,
            source_id=source_id,
            quote=quote,
            locator=locator,
            relation=relation,
            metadata=parsed,
        )
    )


@server.tool(
    description=(
        "Return one claim with every attached citation: relation, verified quote, the exact "
        "passage (text, hash, offsets, locator), and full source provenance."
    )
)
@tool_safe
def get_claim_citations(claim_id: int) -> str:
    """Claim + citations + passages + sources for one claim.

    Args:
        claim_id: The claim id returned by register_claim.
    """
    return ok(**evidence.get_citations_for_claim(claim_id))


@server.tool(
    description=(
        "Research history: omit run_id for all runs with source/claim/report counts; pass run_id "
        "for one run's full trace (sources with staleness, claims by status, unverified claims, "
        "reports)."
    )
)
@tool_safe
def get_research_history(run_id: int = 0, limit: int = 0) -> str:
    """List runs or return one run's full evidence trace.

    Args:
        run_id: Optional run id for the detailed view.
        limit: Maximum runs to list (0 = server default).
    """
    if limit < 0:
        raise ToolError("invalid_argument", "limit must be >= 0")
    return ok(**evidence.get_research_run_history(run_id=run_id or None, limit=limit or None))


@server.tool(
    description=(
        "Render a Markdown report for a run from its claims and citations with stable [Sn] "
        "identifiers, a references section, and visible UNVERIFIED/PARTIALLY SUPPORTED/"
        "CONTRADICTED/NEEDS REVIEW markers; writes the file, records it, and structurally "
        "validates it."
    )
)
@tool_safe
def generate_report(run_id: int, title: str = "") -> str:
    """Generate + store + validate a claims/citations report.

    Args:
        run_id: The research run to report on.
        title: Optional report title (default: derived from the run query).
    """
    return ok(**evidence.generate_report(run_id, title=title))


@server.tool(
    description=(
        "Re-run structural integrity and citation validation on a stored report: checks [Sn] "
        "identifiers resolve, passages/hashes/offsets/quotes are intact, claim statuses match "
        "their citation relations, provenance is present, and no foreign keys dangle. Phase 6 "
        "reports additionally re-check finding ids, verification records, conflict rendering, "
        "image attribution, and the liveness disclaimer. Never judges truth."
    )
)
@tool_safe
def validate_report(report_id: int) -> str:
    """Re-validate one stored report (Phase 3 structural or Phase 6 integrity).

    Args:
        report_id: The report id returned by generate_report or generate_markdown_report.
    """
    return ok(**synthesis.validate_phase6_report(report_id))


# ---------------------------------------------------------------------------
# Phase 4: research planner / orchestrator tools
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "Record the research plan of a run: objective, scope, assumptions, non-overlapping "
        "subquestions (newline-separated text or a JSON array), and optional per-run limits "
        "(JSON: max_subquestions, max_followup_rounds, max_candidates_per_subquestion, "
        "max_unique_sources; defaults 8/2/4/20). Saving again updates the plan and adds new "
        "subquestions; existing ones keep their status. Returns the plan, subquestions, budget, "
        "and stopping state."
    )
)
@tool_safe
def save_research_plan(
    run_id: int,
    objective: str,
    subquestions: str,
    scope: str = "",
    assumptions: str = "",
    limits: str = "",
) -> str:
    """Store the plan (and per-run limits) that guides the research workflow.

    Args:
        run_id: The research run to plan (from start_research_run).
        objective: What the research must answer (required).
        subquestions: One subquestion per line, or a JSON array of strings
            or {"question": ..., "priority": ...} objects (required).
        scope: Optional scope statement (in/out of bounds, time range).
        assumptions: Optional assumptions, one per line; recorded so they are
            visible in the run history and report preparation.
        limits: Optional JSON object overriding the default limits, e.g.
            '{"max_unique_sources": 30}'.
    """
    parsed_limits: dict[str, Any] | None = None
    if limits.strip():
        try:
            loaded = json.loads(limits)
        except json.JSONDecodeError as exc:
            raise ToolError("invalid_argument", f"limits must be a JSON object: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ToolError("invalid_argument", "limits must be a JSON object")
        parsed_limits = loaded
    return ok(
        **planner.save_plan(
            run_id,
            objective=objective,
            subquestions=subquestions,
            scope=scope,
            assumptions=assumptions,
            limits=parsed_limits,
        )
    )


@server.tool(
    description=(
        "Set the evaluation status of one subquestion: pending, answered, partially_answered, "
        "unanswered, or conflicting — with an optional rationale note appended to its audit "
        "trail. Use after weighing the stored evidence so gaps stay visible."
    )
)
@tool_safe
def update_subquestion(
    run_id: int, subquestion_id: int, status: str, note: str = ""
) -> str:
    """Record whether a subquestion is answered, partial, unanswered, or conflicting.

    Args:
        run_id: The run that owns the subquestion.
        subquestion_id: The id from save_research_plan/get_research_history.
        status: One of pending, answered, partially_answered, unanswered, conflicting.
        note: Optional rationale (e.g. which evidence answered it or what is missing).
    """
    return ok(**planner.update_subquestion(run_id, subquestion_id, status, note=note))


@server.tool(
    description=(
        "Append one entry to a run's execution log: tool actions, queries, sources saved or "
        "failed, evidence, claims, citations, gaps, errors, or notes. Kinds: plan, search, "
        "source_candidate (capped per subquestion per round), source_saved, source_failed, "
        "evidence, claim, citation, gap, decision, error, stopping, report, note. Never log "
        "credentials or secrets. Follow-up rounds use start_followup_round."
    )
)
@tool_safe
def log_run_event(
    run_id: int,
    kind: str,
    message: str,
    subquestion_id: int = 0,
    source_id: int = 0,
    payload: str = "",
) -> str:
    """Log one step of the research workflow for the run's audit trail.

    Args:
        run_id: The research run to log against.
        kind: Event kind (see the tool description for the allowed list).
        message: Short human-readable description (required).
        subquestion_id: Optional subquestion this step belongs to; required and
            capped for source_candidate events.
        source_id: Optional stored source id this step refers to.
        payload: Optional JSON object with extra structured details (queries,
            URLs, error codes, counts).
    """
    parsed: dict[str, Any] | None = None
    if payload.strip():
        try:
            loaded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ToolError("invalid_argument", f"payload must be a JSON object: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ToolError("invalid_argument", "payload must be a JSON object")
        parsed = loaded
    return ok(
        **planner.log_event(
            run_id,
            kind,
            message,
            subquestion_id=subquestion_id,
            source_id=source_id,
            payload=parsed,
        )
    )


@server.tool(
    description=(
        "Begin one bounded follow-up research round and log why it was started (max 2 rounds by "
        "default). Fails with limit_reached when the round limit is exhausted — stop and report "
        "the remaining gaps honestly. Targeting a subquestion resets its candidate budget."
    )
)
@tool_safe
def start_followup_round(run_id: int, reason: str, subquestion_id: int = 0) -> str:
    """Log the start of one follow-up round and enforce the round limit.

    Args:
        run_id: The research run continuing with follow-up work.
        reason: Why the round is needed (the gap, conflict, or weakness found).
        subquestion_id: Optional subquestion the round focuses on; resets its
            per-round candidate budget.
    """
    return ok(**planner.start_followup_round(run_id, reason, subquestion_id=subquestion_id))


@server.tool(
    description=(
        "Close a research run: logs a stopping event with the reason (required) and marks the "
        "run completed or failed. Call it as the final workflow step — after generate_report on "
        "success, or with status=failed when a report could not be produced."
    )
)
@tool_safe
def finish_research_run(
    run_id: int, status: str = "completed", reason: str = "", error: str = ""
) -> str:
    """Record why the workflow stopped and close the run.

    Args:
        run_id: The research run to close.
        status: "completed" (default) or "failed".
        reason: Why the workflow stopped (required; recorded in the run log).
        error: Optional failure detail when status is "failed".
    """
    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise ToolError(
            "invalid_argument",
            "reason must not be empty: record why the research workflow stopped.",
        )
    if status not in ("completed", "failed"):
        raise ToolError("invalid_argument", f"status must be completed or failed; got {status!r}")
    stopping_event = planner.log_event(
        run_id,
        "stopping",
        clean_reason,
        payload={"status": status, "error": error.strip() or None},
    )
    run = evidence.finish_run(run_id, status=status, error=error.strip() or None)
    return ok(run=run, stopping_event=stopping_event)


# ---------------------------------------------------------------------------
# Phase 5: image research and visual analysis
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "Search for images matching a natural-language query through a free, "
        "documented source (Wikimedia Commons by default). Returns structured "
        "metadata per result: stable result id, image URL, source page URL, "
        "title/caption, creator, retrieval timestamp (UTC), dimensions, MIME "
        "type, license, attribution, provider, and missing-metadata indicators. "
        "Missing values are never invented."
    )
)
@tool_safe
def search_images(
    query: str,
    provider: str = "",
    max_results: int = 0,
) -> str:
    """Discover images and return normalized, source-linked metadata.

    Args:
        query: Natural-language search terms (required, non-empty).
        provider: Image source name; default is wikimedia_commons.
        max_results: Maximum results to return (0 = server default, capped
            by AGLETRAS_IMAGE_MAX_RESULTS).
    """
    settings = load_settings()
    result = images.search_images(
        query, provider=provider, max_results=max_results, settings=settings
    )
    return ok(**{key: val for key, val in result.items() if key != "ok"})


@server.tool(
    description=(
        "Analyze one or more images: accepts an image URL, an approved local "
        "image path, or multiple image URLs for comparison. Returns discovery "
        "metadata (URL, source page, creator, license, dimensions, MIME type, "
        "retrieval time) plus structured visual analysis split into "
        "observations, inferences, unverified claims, and limitations. Visual "
        "analysis only runs when a vision model endpoint is configured "
        "(AGLETRAS_VISION_MODEL_ENDPOINT); otherwise it reports "
        "vision_unavailable and never pretends the image was inspected."
    )
)
@tool_safe
def analyze_image(
    image_url: str = "",
    image_urls: str = "",
    local_path: str = "",
    source_id: int = 0,
    save: bool = False,
    prompt: str = "",
) -> str:
    """Analyze image(s) and return discovery metadata plus visual analysis.

    Args:
        image_url: http(s) image URL to analyze.
        image_urls: optional JSON array of image URLs for comparison.
        local_path: approved local image path (within AGLETRAS_LOCAL_ROOTS).
        source_id: id of an already-stored image source to analyze.
        save: persist the primary image's discovery metadata as evidence.
        prompt: optional custom prompt for the vision model (only used when
            a vision endpoint is configured).
    """
    settings = load_settings()
    result = images.analyze_image(
        image_url=image_url,
        image_urls=image_urls,
        local_path=local_path,
        source_id=source_id,
        save=save,
        prompt=prompt,
        settings=settings,
    )
    return ok(**{key: val for key, val in result.items() if key != "ok"})


# ---------------------------------------------------------------------------
# Phase 6: claim extraction, verification, and synthesis reports
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "Extract candidate factual statements from a run's stored sources (or explicit text) "
        "with a deterministic sentence splitter and marker scorer — no model is required or "
        "assumed — and map each candidate to the stored passages that discuss it (coverage, "
        "locator, exact quote when verbatim). Extraction never verifies; set register=true to "
        "record the candidates as claims (they always start UNVERIFIED)."
    )
)
@tool_safe
def extract_claims(
    run_id: int,
    text: str = "",
    source_ids: str = "",
    max_claims: int = 0,
    register: bool = False,
    claim_type: str = "claim",
) -> str:
    """Extract + map candidate statements for one research run.

    Args:
        run_id: The research run whose sources should be mined.
        text: Optional explicit text to extract from instead of stored sources.
        source_ids: Optional source ids to restrict extraction to (JSON array or
            comma-separated). Sources must already be linked to the run.
        max_claims: Maximum candidates to return (0 = server default, capped by
            AGLETRAS_VERIFICATION_MAX_CLAIMS).
        register: Persist the candidates as claims (default false). Claims always
            start UNVERIFIED; extraction never asserts support.
        claim_type: Claim classification used when registering: claim (default),
            finding, inference, unverified, or question.
    """
    if max_claims < 0:
        raise ToolError("invalid_argument", "max_claims must be >= 0")
    return ok(
        **verification.extract_claims(
            run_id,
            text=text,
            source_ids=_parse_int_list(source_ids, what="source_ids") or None,
            max_claims=max_claims or 25,
            register=register,
            claim_type=claim_type,
        )
    )


@server.tool(
    description=(
        "Evaluate a run's claims against the exact stored passage text and record an explicit "
        "status per claim: SUPPORTED, PARTIALLY SUPPORTED, CONTRADICTED, CONFLICTING EVIDENCE, "
        "UNSUPPORTED, or UNVERIFIABLE — with method, confidence, rationale, evidence references, "
        "and limitations. Deterministic checks run by default; a verification model is only used "
        "when AGLETRAS_VERIFICATION_MODEL_ENDPOINT is configured (method=auto) and its response "
        "must cite stored passage ids or it is rejected with the deterministic result recorded. "
        "Decisive full/partial matches may attach supports/partially_supports citations; "
        "contradictions and conflicts are never auto-attached."
    )
)
@tool_safe
def verify_claims(
    run_id: int,
    claim_ids: str = "",
    apply_citations: bool = True,
    method: str = "auto",
) -> str:
    """Verify claims against stored evidence (deterministic by default).

    Args:
        run_id: The research run whose claims to verify.
        claim_ids: Optional subset of claim ids (JSON array or comma-separated);
            default is every claim of the run (bounded by AGLETRAS_VERIFICATION_MAX_CLAIMS).
        apply_citations: Attach supports/partially_supports citations for decisive
            matches (default true). Contradictions are never auto-attached.
        method: auto (use the configured verification model when available, else
            deterministic checks) or deterministic (never call a model).
    """
    return ok(
        **verification.verify_claims(
            run_id,
            claim_ids=_parse_int_list(claim_ids, what="claim_ids") or None,
            apply_citations=apply_citations,
            method=method,
        )
    )


@server.tool(
    description=(
        "List every disagreement visible in a run's stored evidence: claims with supporting and "
        "contradicting citations, verification results that found number/date disagreements, and "
        "subquestions marked conflicting — with the evidence on each side (source, date, locator, "
        "quote, content hash, independence group), the differences that may explain it, and "
        "whether it remains unresolved (conflicts are never resolved by preference). Optionally "
        "returns metadata-driven source-quality heuristics, kept separate from verification "
        "statuses."
    )
)
@tool_safe
def analyze_conflicts(run_id: int, include_sources: bool = True) -> str:
    """Conflict + source-quality analysis for one research run.

    Args:
        run_id: The research run to analyze.
        include_sources: Include the source-quality indicators (default true).
    """
    return ok(**verification.analyze_conflicts(run_id, include_sources=include_sources))


@server.tool(
    description=(
        "Render, store, and integrity-validate the ten-section Phase 6 synthesis report for a "
        "run: executive summary; scope and methodology; key findings (facts, inferences, and "
        "unverified statements kept apart with [F#] ids); evidence comparison tables; conflicting "
        "evidence (unresolved conflicts stay visible); limitations and open questions; "
        "conclusion; sources and references with stable [Sn] ids; and image attribution. "
        "Statements keep explicit SUPPORTED / PARTIALLY SUPPORTED / UNSUPPORTED / UNVERIFIABLE / "
        "CONTRADICTED / CONFLICTING EVIDENCE labels, verification method, and limitations. The "
        "file is written inside the reports directory; identical content is deduplicated and "
        "existing files are never overwritten."
    )
)
@tool_safe
def generate_markdown_report(
    run_id: int,
    title: str = "",
    include_source_assessment: bool = True,
) -> str:
    """Generate + store + validate a Phase 6 verified-synthesis report.

    Args:
        run_id: The research run to report on.
        title: Optional report title (default: derived from the run query).
        include_source_assessment: Include source-quality indicators (default true).
    """
    return ok(
        **synthesis.generate_markdown_report(
            run_id,
            title=title,
            include_source_assessment=include_source_assessment,
        )
    )


@server.tool(
    description=(
        "Fetch one stored report (either template) with its metadata, validation status, section "
        "list, finding/citation/image identifier maps, and on-disk integrity flags "
        "(file_exists, file_hash_matches). Set include_content=true to return the stored "
        "Markdown."
    )
)
@tool_safe
def get_report(report_id: int, include_content: bool = False) -> str:
    """Retrieve one stored report record.

    Args:
        report_id: The report id returned by generate_report or generate_markdown_report.
        include_content: Return the stored Markdown content (default false).
    """
    return ok(**synthesis.get_markdown_report(report_id, include_content=include_content))


# ---------------------------------------------------------------------------
# Phase 8: project memory, monitoring, and change detection
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "Search stored evidence passages across every run (project memory) with full "
        "provenance and freshness flags. Use it before new research to reuse already-stored, "
        "already-cited material instead of re-fetching it. Passages are returned verbatim; "
        "nothing is rewritten, and a bounded max_age_days excludes only passages whose age "
        "is known to exceed it (unknown-age passages stay, marked fresh=null)."
    )
)
@tool_safe
def search_memory(query: str, project: str = "", limit: int = 10, max_age_days: int = 0) -> str:
    """Recall stored evidence across runs, ranked like search_evidence.

    Args:
        query: Keywords or a phrase to recall (required).
        project: Only recall evidence linked to runs tagged with this project.
        limit: Maximum passages to return (default 10; bounded by max_list_limit).
        max_age_days: Exclude passages older than this many days (0 = no bound).
    """
    return ok(
        **memory.recall(query, project=project, limit=limit, max_age_days=max_age_days)
    )


@server.tool(
    description=(
        "Register (or update) a monitoring target: an http(s) URL or a permitted local file. "
        "Nothing is fetched now, and targets never check themselves: use monitor_check (or the "
        "CLI) for manual runs. Idempotent by identity, so re-adding the same target updates "
        "its label/project instead of duplicating it."
    )
)
@tool_safe
def monitor_add_target(target: str, name: str = "", project: str = "", kind: str = "") -> str:
    """Add or update one monitoring target.

    Args:
        target: URL or local file path to watch (required).
        name: Human label (default: host for URLs, file name for paths).
        project: Project label that scopes change queries.
        kind: Force "url" or "path" when the target alone is ambiguous.
    """
    return ok(**monitor.add_target(target, name=name, project=project, kind=kind))


@server.tool(
    description=(
        "Check monitoring targets for changes now (manual action): conditional HTTP requests "
        "(ETag/Last-Modified), normalized content hashes, and meaningful-change detection that "
        "ignores ads/navigation/timestamp-only churn. 304 Not Modified and failed fetches are "
        "recorded as such — a failure is never treated as a deletion or a change."
    )
)
@tool_safe
def monitor_check(target_ids: str = "", project: str = "", parallel: bool = True) -> str:
    """Run change checks over targets and return per-target outcomes.

    Args:
        target_ids: Comma/space-separated ids or a JSON array (empty = all enabled
            targets, optionally filtered by project).
        project: Only check enabled targets with this project label.
        parallel: Bounded parallel checks (default true); false runs sequentially.
    """
    ids = _parse_int_list(target_ids, what="target_ids")
    return ok(**monitor.run_checks(target_ids=ids, project=project, parallel_mode=parallel))


@server.tool(
    description=(
        "List detected changes, newest first, with the old and new evidence references, "
        "normalized content hashes, and the added/removed term summary. Unchanged, not-"
        "modified, and failed checks are not changes and never appear here (set "
        "meaningful_only=false to also see non-substantive churn)."
    )
)
@tool_safe
def monitor_changes(
    target_id: int = 0,
    project: str = "",
    meaningful_only: bool = True,
    since_days: int = 0,
    limit: int = 50,
) -> str:
    """List detected changes with both sides of the diff.

    Args:
        target_id: Restrict to one monitoring target (0 = every target).
        project: Restrict to targets with this project label.
        meaningful_only: Only meaningful changes (default true).
        since_days: Only changes detected in the last N days (0 = no bound).
        limit: Maximum changes (default 50; bounded by the list caps).
    """
    return ok(
        **monitor.changes(
            target_id=target_id,
            project=project,
            meaningful_only=meaningful_only,
            since_days=since_days,
            limit=limit,
        )
    )

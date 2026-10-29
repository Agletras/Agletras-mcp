"""Evidence library (Phase 3): runs, sources, content cache, passages, claims,
citations, and citation-validated reports.

Everything lives in the same SQLite database as the Phase 1/2 source store
(``storage.SCHEMA_VERSION`` 2 adds the evidence tables). Design rules:

- **Source identity.** A canonical URL or normalized local path is bound to
  one ``sources`` row through ``source_identities``; the same page/path saved
  twice reuses the row instead of duplicating it. Different identities with
  identical content keep *separate* source rows (separate provenance and
  citation URLs) while sharing one content blob.
- **Content cache.** Extracted text is stored once in ``source_contents``
  keyed by SHA-256; raw byte hashes are kept when available. An existing
  record is served without refetching until it goes stale (cache TTL, or a
  changed size/mtime for local files). Changed content creates a new content
  blob; earlier passages, citations, and run associations are never erased.
- **Passages are immutable.** They are exact, hash-verified slices of one
  content blob with character offsets plus page/heading locators.
- **Citations point at exact passages.** A citation cannot exist without its
  passage/source, and a stored quotation must actually occur in the passage —
  quotes, page numbers, and locators are never fabricated.
- **Claim status is derived, never asserted.** ``supported`` /
  ``partially_supported`` / ``contradicted`` / ``needs_review`` come from the
  caller-declared relations of attached citations; attaching anything never
  silently marks a claim supported.
- **Report validation is structural.** It proves identifiers resolve,
  passages are intact, and provenance is present. It explicitly states that
  citation existence alone does not prove a statement.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import adapters, canonurl, collect, localfs, storage
from . import passages as passages_mod
from . import planner
from .config import Settings, load_settings
from .errors import ToolError
from .source_record import SourceRecord, finalize_text, utc_now_iso

LOGGER = logging.getLogger(__name__)

#: Serializes the identity-resolution + row-persist critical section inside one
#: process. Phase 8 runs collection workers concurrently, so two workers can
#: save the same URL at the same time; without this lock both could read
#: "no existing row" and each insert a duplicate source. SQLite's own locks
#: cannot help with that read-then-insert race. Retrieval/extraction stays
#: outside the lock, so network work still overlaps.
_PERSIST_LOCK = threading.RLock()

RUN_STATUSES = ("running", "completed", "failed")
CLAIM_STATUSES = ("unverified", "supported", "partially_supported", "contradicted", "needs_review")
#: Claim classification (Phase 6, PART 5): every statement is a fact/finding
#: candidate, an inference, an explicitly unverified claim, or an open
#: question. The classification is set by the caller and never inferred.
CLAIM_TYPES = ("claim", "finding", "inference", "unverified", "question")
CITATION_RELATIONS = ("supports", "partially_supports", "contradicts", "context", "needs_review")
REPORT_FORMATS = ("markdown",)
REPORT_VALIDATION_STATUSES = ("unvalidated", "validated", "failed")

_MAX_ORIGINS = 50
_MAX_CLAIM_CHARS = 2000
_MAX_NOTE_CHARS = 4000
_LIKE_CANDIDATE_LIMIT = 2000
_SEMANTIC_NOTE = (
    "Structural validation proves that identifiers resolve, passages are intact, and provenance "
    "is present. It does not prove that any statement is true: support relations are caller-declared "
    "and citation existence alone is not evidence of support."
)

_STOPWORDS = frozenset(
    """a an the and or but if then else of in on at to for with by from as is are was were be
    been being it its this that these those not no do does did can could will would should may
    might must have has had he she they we you i how what when where why who which whose whom
    about into over under than too very just also""".split()
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return utc_now_iso()


def _load_object(raw: str) -> dict[str, Any]:
    """Parse a stored JSON object, never raising on malformed data."""
    try:
        parsed = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _normalize_ws(text: str) -> str:
    return " ".join((text or "").split())


# Typographic quote glyphs that are visually the same character as their ASCII
# counterpart: curly single/double quotes and prime marks. Equating them keeps
# excerpts from non-English sources (l'affluence, «guillemets»-style quoting,
# foot/inch primes) from being rejected over pure punctuation rendering.
_QUOTE_GLYPHS = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2033": '"',
    }
)


def quote_matches(quote: str, passage_text: str) -> bool:
    """True when *quote* occurs in *passage_text*.

    Matching is whitespace-insensitive and equates typographic quote glyphs
    (curly quotes/apostrophes, primes) with their ASCII equivalents, so a
    straight-quoted excerpt still matches a passage that uses ' or ". Any
    other difference still fails — fabricated quotations are rejected exactly
    as before.
    """
    needle = _normalize_ws(quote).translate(_QUOTE_GLYPHS)
    if not needle:
        return False
    return needle in _normalize_ws(passage_text).translate(_QUOTE_GLYPHS)


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def extract_terms(query: str, *, max_terms: int = 12) -> list[str]:
    """Lowercase search terms (stopwords dropped) for claim/passage retrieval."""
    terms: list[str] = []
    for word in re.findall(r"[\w']+", (query or "").lower()):
        cleaned = word.strip("'")
        if len(cleaned) < 2 or cleaned in _STOPWORDS:
            continue
        if cleaned not in terms:
            terms.append(cleaned)
        if len(terms) >= max_terms:
            break
    return terms


def derive_claim_status(relations: list[str]) -> str:
    """Claim verification status derived from its citations' relations.

    Conservative by design: a contradiction wins; a pending review wins over
    support; ``context`` citations never establish support on their own.
    """
    present = {relation for relation in relations if relation in CITATION_RELATIONS}
    if not present:
        return "unverified"
    if "contradicts" in present:
        return "contradicted"
    if "needs_review" in present:
        return "needs_review"
    supports = "supports" in present
    partials = "partially_supports" in present
    if supports and partials:
        return "partially_supported"
    if supports:
        return "supported"
    if partials:
        return "partially_supported"
    return "unverified"  # only "context" citations


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_seconds(timestamp: str | None) -> float | None:
    moment = _parse_iso(timestamp)
    if moment is None:
        return None
    return (datetime.now(timezone.utc) - moment).total_seconds()


def _is_stale_timestamp(timestamp: str | None, ttl: int) -> bool:
    if ttl <= 0:
        return True
    age = _age_seconds(timestamp)
    return age is None or age > ttl


# ---------------------------------------------------------------------------
# Research runs
# ---------------------------------------------------------------------------


def _require_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM research_runs WHERE id = ?", (int(run_id),)).fetchone()
    if row is None:
        raise ToolError(
            "not_found",
            f"No research run with id {run_id}; create one with start_research_run first.",
        )
    return row


def _run_payload(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "run_id": int(row["id"]),
        "query": row["query"],
        "status": row["status"],
        "project": (row["project"] or "") if "project" in keys else "",
        "config": _load_object(row["config"]),
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "error": row["error"],
    }


def start_run(
    query: str,
    *,
    config: dict[str, Any] | None = None,
    project: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Create a research run that evidence, claims, and reports attach to."""
    settings = settings or load_settings()
    query = (query or "").strip()
    if not query:
        raise ToolError("invalid_argument", "query must not be empty")
    payload = _dumps(dict(config or {}))
    with storage.open_db(settings) as conn:
        cursor = conn.execute(
            "INSERT INTO research_runs (query, status, config, project, started_at) "
            "VALUES (?, 'running', ?, ?, ?)",
            (query, payload, (project or "").strip(), _utc_now()),
        )
        run_id = int(cursor.lastrowid)
        row = _require_run(conn, run_id)
        return _run_payload(row)


def get_run(run_id: int, *, settings: Settings | None = None) -> dict[str, Any] | None:
    """Return one research run, or ``None`` when the id is unknown."""
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        row = conn.execute("SELECT * FROM research_runs WHERE id = ?", (int(run_id),)).fetchone()
    return _run_payload(row) if row is not None else None


def finish_run(
    run_id: int,
    *,
    status: str = "completed",
    error: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """Close a research run (completed/failed). Idempotent."""
    settings = settings or load_settings()
    if status not in ("completed", "failed"):
        raise ToolError("invalid_argument", f"status must be completed or failed; got {status!r}")
    with storage.open_db(settings) as conn:
        row = _require_run(conn, run_id)
        if row["status"] == "running":
            conn.execute(
                "UPDATE research_runs SET status = ?, completed_at = COALESCE(completed_at, ?), error = ? WHERE id = ?",
                (status, _utc_now(), error, int(run_id)),
            )
        updated = _require_run(conn, run_id)
        return _run_payload(updated)


def get_research_run_history(
    *, run_id: int | None = None, limit: int | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """List runs with counts, or one run's full trace (sources/claims/reports)."""
    settings = settings or load_settings()
    bounded = min(max(int(limit or settings.default_list_limit), 1), settings.max_list_limit)
    with storage.open_db(settings) as conn:
        if run_id:
            row = _require_run(conn, run_id)
            return {
                "run": _run_payload(row),
                **_run_detail(conn, int(run_id), settings),
                # Phase 4: plan, subquestions, budget, stopping state, run log.
                **planner.describe_run(conn, int(run_id)),
            }
        rows = conn.execute(
            """
            SELECT r.*,
                (SELECT COUNT(*) FROM research_run_sources x WHERE x.run_id = r.id) AS sources_count,
                (SELECT COUNT(*) FROM claims c WHERE c.run_id = r.id) AS claims_count,
                (SELECT COUNT(*) FROM claims c WHERE c.run_id = r.id
                    AND c.status IN ('unverified', 'needs_review')) AS unverified_count,
                (SELECT COUNT(*) FROM reports rp WHERE rp.run_id = r.id) AS reports_count
            FROM research_runs r ORDER BY r.id DESC LIMIT ?
            """,
            (bounded,),
        ).fetchall()
        runs = [
            {
                **_run_payload(row),
                "sources_count": int(row["sources_count"]),
                "claims_count": int(row["claims_count"]),
                "unverified_count": int(row["unverified_count"]),
                "reports_count": int(row["reports_count"]),
            }
            for row in rows
        ]
        return {"count": len(runs), "runs": runs}


def _run_detail(conn: sqlite3.Connection, run_id: int, settings: Settings) -> dict[str, Any]:
    source_rows = conn.execute(
        """
        SELECT s.*, rrs.added_at,
            (SELECT COUNT(*) FROM passages p WHERE p.source_id = s.id) AS passage_count
        FROM research_run_sources rrs JOIN sources s ON s.id = rrs.source_id
        WHERE rrs.run_id = ? ORDER BY rrs.added_at, s.id
        """,
        (run_id,),
    ).fetchall()
    sources = [
        {
            "source_id": int(row["id"]),
            "title": row["title"],
            "url": row["url"],
            "path": row["path"],
            "source_type": row["source_type"],
            "kind": row["kind"],
            "extraction_status": row["extraction_status"],
            "retrieved_at": row["retrieved_at"],
            "publication_date": row["publication_date"],
            "canonical_url": row["canonical_url"],
            "content_hash": row["sha256"],
            "passages": int(row["passage_count"]),
            "stale": source_is_stale(row, settings),
        }
        for row in source_rows
    ]
    claims = _claims_with_citations(conn, run_id)
    claim_summaries = []
    by_status: dict[str, int] = {}
    by_verification_status: dict[str, int] = {}
    unverified: list[dict[str, Any]] = []
    for claim in claims:
        by_status[claim["status"]] = by_status.get(claim["status"], 0) + 1
        if claim.get("verification_status"):
            key = claim["verification_status"]
            by_verification_status[key] = by_verification_status.get(key, 0) + 1
        claim_summaries.append(
            {
                "claim_id": claim["claim_id"],
                "claim_text": claim["claim_text"],
                "claim_type": claim.get("claim_type", "claim"),
                "status": claim["status"],
                "verification_status": claim.get("verification_status") or None,
                "verified_at": claim.get("verified_at"),
                "citation_count": len(claim["citations"]),
                "created_at": claim["created_at"],
                "updated_at": claim["updated_at"],
            }
        )
        if claim["status"] in ("unverified", "needs_review"):
            unverified.append(
                {
                    "claim_id": claim["claim_id"],
                    "claim_text": claim["claim_text"],
                    "status": claim["status"],
                    "verification_status": claim.get("verification_status") or None,
                    "citation_count": len(claim["citations"]),
                }
            )
    reports = [
        {
            "report_id": int(row["id"]),
            "title": row["title"],
            "path": row["path"],
            "format": row["format"],
            "template_version": row["template_version"] if "template_version" in row.keys() else "phase3",
            "citation_validation_status": row["citation_validation_status"],
            "generated_at": row["generated_at"],
        }
        for row in conn.execute(
            "SELECT * FROM reports WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
    ]
    verifications_count = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM claim_verifications WHERE run_id = ?", (int(run_id),)
        ).fetchone()["n"]
    )
    return {
        "sources": sources,
        "claims": claim_summaries,
        "claims_by_status": by_status,
        "claims_by_verification_status": by_verification_status,
        "verifications_count": verifications_count,
        "unverified_claims": unverified,
        "reports": reports,
        "counts": {
            "sources": len(sources),
            "claims": len(claim_summaries),
            "unverified": len(unverified),
            "reports": len(reports),
            "verifications": verifications_count,
        },
    }


# ---------------------------------------------------------------------------
# Identities, content blobs, and staleness
# ---------------------------------------------------------------------------


def _find_identity(conn: sqlite3.Connection, identity: str) -> sqlite3.Row | None:
    """The stored source bound to *identity*, if any."""
    if not identity:
        return None
    return conn.execute(
        """
        SELECT s.* FROM source_identities i JOIN sources s ON s.id = i.source_id
        WHERE i.identity = ? ORDER BY s.id DESC LIMIT 1
        """,
        (identity,),
    ).fetchone()


def _bind_identity(conn: sqlite3.Connection, source_id: int, identity: str, identity_kind: str) -> bool:
    """Bind *identity* to *source_id*. Returns False when another source owns it."""
    if not identity:
        return True
    now = _utc_now()
    owner = conn.execute(
        "SELECT source_id FROM source_identities WHERE identity = ?", (identity,)
    ).fetchone()
    if owner is not None:
        if int(owner["source_id"]) != int(source_id):
            return False
        conn.execute(
            "UPDATE source_identities SET last_seen_at = ? WHERE identity = ?", (now, identity)
        )
        return True
    cursor = conn.execute(
        """
        INSERT INTO source_identities (identity, source_id, identity_kind, created_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(identity) DO NOTHING
        """,
        (identity, int(source_id), identity_kind, now, now),
    )
    if cursor.rowcount == 0:
        # Lost a first-insert race: re-check the winner before claiming success.
        owner = conn.execute(
            "SELECT source_id FROM source_identities WHERE identity = ?", (identity,)
        ).fetchone()
        if owner is not None and int(owner["source_id"]) != int(source_id):
            return False
        conn.execute(
            "UPDATE source_identities SET last_seen_at = ? WHERE identity = ?", (now, identity)
        )
    return True


def _identities_from_row(row: sqlite3.Row | dict[str, Any]) -> list[tuple[str, str]]:
    """Every (identity, kind) a stored source row can be found by."""
    identities: list[tuple[str, str]] = []
    seen: set[str] = set()
    for key in ("canonical_url", "final_url", "url"):
        value = row[key] if key in row.keys() else None
        if value:
            canonical = canonurl.canonicalize_url(str(value))
            if canonical and canonical not in seen:
                identities.append((canonical, "url"))
                seen.add(canonical)
    path = row["path"] if "path" in row.keys() else None
    if path:
        normalized = canonurl.normalize_local_path(str(path))
        if normalized and normalized not in seen:
            identities.append((normalized, "path"))
            seen.add(normalized)
    if not identities:
        digest = row["sha256"] if "sha256" in row.keys() else None
        if not digest and "content" in row.keys():
            digest = storage.content_hash(str(row["content"] or ""))
        if digest:
            identities.append((f"note:{digest}", "note"))
    return identities


def _usable_content(row: sqlite3.Row) -> bool:
    return bool((row["content"] or "").strip()) and row["extraction_status"] in ("success", "partial")


def source_is_stale(row: sqlite3.Row, settings: Settings) -> bool:
    """Cache staleness for one stored source row (TTL + local file changes)."""
    if not _usable_content(row):
        return True
    checked = row["last_checked_at"] or row["retrieved_at"] or row["created_at"]
    if row["path"] and not (row["url"] or row["final_url"]):
        try:
            target = localfs.resolve_allowed(str(row["path"]), settings)
        except ToolError:
            return True
        fingerprint = _load_object(row["metadata"]).get("file_fingerprint")
        if not isinstance(fingerprint, dict):
            return True
        try:
            stat = target.stat()
        except OSError:
            return True
        if (
            fingerprint.get("size") != stat.st_size
            or fingerprint.get("mtime_ns") != stat.st_mtime_ns
        ):
            return True
    return _is_stale_timestamp(checked, settings.evidence_cache_ttl)


def _local_file_modified(row: sqlite3.Row, path: Path) -> bool:
    fingerprint = _load_object(row["metadata"]).get("file_fingerprint")
    if not isinstance(fingerprint, dict):
        return True
    try:
        stat = path.stat()
    except OSError:
        return True
    return fingerprint.get("size") != stat.st_size or fingerprint.get("mtime_ns") != stat.st_mtime_ns


def _upsert_content(conn: sqlite3.Connection, candidate: "_Candidate", digest: str) -> tuple[int, bool]:
    """Reusable content blob keyed by ``digest`` (SHA-256 of extracted text)."""
    row = conn.execute(
        "SELECT id FROM source_contents WHERE content_hash = ?", (digest,)
    ).fetchone()
    if row is not None:
        return int(row["id"]), False
    now = _utc_now()
    cursor = conn.execute(
        """
        INSERT INTO source_contents (
            content_hash, raw_hash, extracted_text, extraction_status, error_code,
            error_message, content_type, file_size_bytes, storage_location,
            metadata, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(content_hash) DO NOTHING
        """,
        (
            digest,
            candidate.raw_hash,
            candidate.text,
            candidate.extraction_status,
            candidate.error_code,
            candidate.error_message,
            candidate.content_type,
            candidate.file_size_bytes,
            candidate.path,
            _dumps(candidate.metadata),
            now,
            now,
        ),
    )
    if cursor.rowcount == 0:
        # A concurrent writer stored the same blob first (two workers can
        # legitimately save identical text under different identities).
        row = conn.execute(
            "SELECT id FROM source_contents WHERE content_hash = ?", (digest,)
        ).fetchone()
        if row is None:  # pragma: no cover - defensive: conflict without a row
            raise ToolError("conflict", f"content blob {digest[:16]}… vanished during insert")
        return int(row["id"]), False
    return int(cursor.lastrowid), True


def save_extracted_content(
    *,
    text: str,
    extraction_status: str = "success",
    error_code: str | None = None,
    error_message: str | None = None,
    content_type: str | None = None,
    file_size_bytes: int | None = None,
    raw_hash: str | None = None,
    storage_location: str | None = None,
    metadata: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Store (or reuse) one content blob keyed by SHA-256 of the text."""
    settings = settings or load_settings()
    if not (text or "").strip():
        raise ToolError("invalid_argument", "content text must not be empty")
    digest = storage.content_hash(text)
    candidate = _Candidate(
        identity=None,
        identity_kind="note",
        extra_identities=[],
        title="",
        kind="note",
        source_type="note",
        url=None,
        path=storage_location,
        text=text,
        extraction_status=extraction_status,
        error_code=error_code,
        error_message=error_message,
        content_type=content_type,
        file_size_bytes=file_size_bytes,
        publication_date=None,
        retrieved_at=_utc_now(),
        raw_hash=raw_hash,
        normalized_sha256=None,
        metadata=dict(metadata or {}),
        origin="",
    )
    with storage.open_db(settings) as conn:
        content_id, created = _upsert_content(conn, candidate, digest)
    return {"content_id": content_id, "content_hash": digest, "created": created}


def save_passages(
    *,
    source_id: int,
    content_id: int,
    content_hash: str,
    text: str,
    max_chars: int | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Split *text* into passages for one source/content pair (idempotent)."""
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        source = conn.execute("SELECT id FROM sources WHERE id = ?", (int(source_id),)).fetchone()
        if source is None:
            raise ToolError("not_found", f"No source with id {source_id}")
        content = conn.execute(
            "SELECT id, content_hash FROM source_contents WHERE id = ?", (int(content_id),)
        ).fetchone()
        if content is None:
            raise ToolError("not_found", f"No content blob with id {content_id}")
        if content["content_hash"] != content_hash:
            raise ToolError("conflict", "content hash does not match the stored content blob")
        return save_passages_in_conn(
            conn, int(source_id), int(content_id), content_hash, text, settings, max_chars=max_chars
        )


def _associate_run(conn: sqlite3.Connection, run_id: int, source_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO research_run_sources (run_id, source_id, added_at) VALUES (?, ?, ?)",
        (int(run_id), int(source_id), _utc_now()),
    )


# ---------------------------------------------------------------------------
# Saving evidence (deduplication + content cache + passage indexing)
# ---------------------------------------------------------------------------


@dataclass
class _Candidate:
    """One piece of evidence about to be persisted under an identity."""

    identity: str | None
    identity_kind: str
    extra_identities: list[tuple[str, str]]
    title: str
    kind: str
    source_type: str
    url: str | None
    path: str | None
    text: str
    extraction_status: str
    error_code: str | None
    error_message: str | None
    content_type: str | None
    file_size_bytes: int | None
    publication_date: str | None
    retrieved_at: str
    raw_hash: str | None
    normalized_sha256: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    origin: str = ""


def _candidate_from_record(
    record: SourceRecord,
    *,
    identity: str | None,
    identity_kind: str,
    extra_identities: list[tuple[str, str]],
    raw_hash: str | None,
    origin: str,
    fingerprint: dict[str, Any] | None = None,
) -> _Candidate:
    metadata = dict(record.metadata or {})
    if fingerprint is not None:
        metadata["file_fingerprint"] = fingerprint
    return _Candidate(
        identity=identity,
        identity_kind=identity_kind,
        extra_identities=extra_identities,
        title=(record.title or "").strip() or "(untitled)",
        kind=record.kind(),
        source_type=record.source_type,
        url=record.final_url or record.original_url,
        path=record.file_path,
        text=record.extracted_text or "",
        extraction_status=record.extraction_status,
        error_code=record.error_code,
        error_message=record.error_message,
        content_type=record.content_type,
        file_size_bytes=record.file_size_bytes,
        publication_date=record.publication_date,
        retrieved_at=record.retrieved_at or _utc_now(),
        raw_hash=raw_hash,
        normalized_sha256=record.normalized_sha256,
        metadata=metadata,
        origin=origin,
    )


def _merged_metadata(
    existing_raw: str | None,
    candidate: _Candidate,
    *,
    content_changed: bool,
) -> dict[str, Any]:
    previous = _load_object(existing_raw or "")
    if content_changed:
        merged = dict(candidate.metadata)
        for key in ("origins", "duplicate_origins", "revisions"):
            if key in previous:
                merged[key] = previous[key]
    else:
        merged = {**previous, **candidate.metadata}
    origins = [entry for entry in merged.get("origins", []) if isinstance(entry, dict)]
    entry = {"requested": candidate.origin, "at": _utc_now()}
    if candidate.origin and entry not in origins and len(origins) < _MAX_ORIGINS:
        origins.append(entry)
    if origins:
        merged["origins"] = origins
    revisions = int(merged.get("revisions") or 0) + (1 if content_changed else 0)
    if revisions:
        merged["revisions"] = revisions
    return merged


def _append_notes(conn: sqlite3.Connection, source_id: int, notes: str) -> None:
    clean = (notes or "").strip()
    if not clean:
        return
    conn.execute(
        """
        UPDATE sources SET notes = CASE
            WHEN TRIM(notes) = '' THEN ?
            ELSE notes || char(10) || ?
        END WHERE id = ?
        """,
        (clean, clean, int(source_id)),
    )


def _persist_candidate(
    conn: sqlite3.Connection,
    candidate: _Candidate,
    existing: sqlite3.Row | None,
    *,
    run_id: int,
    notes: str,
    title_override: str,
    settings: Settings,
    warnings: list[str],
    fetched: bool = True,
) -> dict[str, Any]:
    """Create or update the source row, content blob, passages, and run link."""
    if run_id:
        # Phase 4: enforce the run's unique-source cap before anything is
        # written. Re-saving an already-linked source is always allowed.
        planner.check_source_capacity(
            conn,
            run_id,
            already_linked=existing is not None
            and planner.run_links_source(conn, run_id, int(existing["id"])),
        )
    digest = storage.content_hash(candidate.text)
    created_source = existing is None
    content_changed = False
    content_id: int | None = None
    if existing is not None:
        source_id = int(existing["id"])
        same_content = (existing["sha256"] or "") == digest and (existing["content"] or "") == candidate.text
        content_changed = not same_content
        if same_content and existing["content_id"] is not None:
            content_id = int(existing["content_id"])
        else:
            content_id, _created = _upsert_content(conn, candidate, digest)
        metadata = _merged_metadata(existing["metadata"], candidate, content_changed=content_changed)
        conn.execute(
            """
            UPDATE sources SET
                sha256 = ?, content = ?, content_id = ?, raw_hash = ?,
                retrieved_at = ?, last_checked_at = ?,
                content_type = ?, file_size_bytes = ?,
                publication_date = COALESCE(?, publication_date),
                extraction_status = ?, error_code = ?, error_message = ?,
                normalized_sha256 = COALESCE(?, normalized_sha256),
                final_url = COALESCE(?, final_url),
                url = COALESCE(?, url),
                metadata = ?
            WHERE id = ?
            """,
            (
                digest, candidate.text, content_id, candidate.raw_hash,
                candidate.retrieved_at, _utc_now(),
                candidate.content_type, candidate.file_size_bytes,
                candidate.publication_date,
                candidate.extraction_status, candidate.error_code, candidate.error_message,
                candidate.normalized_sha256,
                candidate.url, candidate.url,
                _dumps(metadata),
                source_id,
            ),
        )
    else:
        content_id, _created = _upsert_content(conn, candidate, digest)
        cursor = conn.execute(
            """
            INSERT INTO sources (
                kind, title, url, path, content, notes, sha256, created_at,
                source_type, original_url, final_url, content_type,
                file_size_bytes, publication_date, retrieved_at,
                extraction_status, error_code, error_message, metadata,
                normalized_sha256, canonical_url, content_id, raw_hash, last_checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.kind, candidate.title, candidate.url, candidate.path,
                candidate.text, (notes or "").strip(), digest, _utc_now(),
                candidate.source_type, candidate.url, candidate.url, candidate.content_type,
                candidate.file_size_bytes, candidate.publication_date, candidate.retrieved_at,
                candidate.extraction_status, candidate.error_code, candidate.error_message,
                _dumps(_merged_metadata(None, candidate, content_changed=False)),
                candidate.normalized_sha256,
                candidate.identity if candidate.identity_kind == "url" else None,
                content_id, candidate.raw_hash, _utc_now(),
            ),
        )
        source_id = int(cursor.lastrowid)

    # Identities: primary + the additional URL spellings that led here.
    bound = True
    for identity, kind in [(candidate.identity, candidate.identity_kind), *candidate.extra_identities]:
        if identity and not _bind_identity(conn, source_id, identity, kind):
            bound = False
            warnings.append(
                f"identity {identity!r} is already bound to a different source row; "
                "the new content was stored under this source."
            )
    if title_override:
        conn.execute(
            "UPDATE sources SET title = ? WHERE id = ?", (title_override.strip(), source_id)
        )
    elif not created_source:
        current = conn.execute("SELECT title FROM sources WHERE id = ?", (source_id,)).fetchone()
        if current is not None and not (current["title"] or "").strip():
            conn.execute("UPDATE sources SET title = ? WHERE id = ?", (candidate.title, source_id))
    _append_notes(conn, source_id, notes)
    if run_id:
        _associate_run(conn, run_id, source_id)

    passage_stats = save_passages_in_conn(conn, source_id, int(content_id), digest, candidate.text, settings)
    if passage_stats["truncated"]:
        warnings.append(
            f"passage indexing stopped at {passages_mod.MAX_PASSAGES} passages; "
            "the stored content itself is complete."
        )

    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    identities = [
        {"identity": record["identity"], "kind": record["identity_kind"]}
        for record in conn.execute(
            "SELECT identity, identity_kind FROM source_identities WHERE source_id = ? ORDER BY identity",
            (source_id,),
        ).fetchall()
    ]
    return _source_payload(
        row,
        content_id=int(content_id),
        content_hash=digest,
        identities=identities,
        passages=passage_stats,
        created_source=created_source,
        content_changed=content_changed,
        # A cache hit means the stored copy was reused without retrieving it.
        cache_hit=not created_source and not content_changed and not fetched,
        fetched=fetched,
        warnings=warnings,
        run_id=run_id or None,
    )


def save_passages_in_conn(
    conn: sqlite3.Connection,
    source_id: int,
    content_id: int,
    content_hash: str,
    text: str,
    settings: Settings,
    *,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Insert passages using an existing connection (no nested transactions)."""
    limit = max_chars or settings.evidence_passage_chars
    split = (
        passages_mod.split_passages(text, max_chars=limit)
        if (text or "").strip()
        else []
    )
    now = _utc_now()
    saved = 0
    for passage in split:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO passages (
                source_id, content_id, content_hash, passage_index, text,
                passage_hash, start_offset, end_offset, page, heading, locator, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(source_id), int(content_id), content_hash, passage.index, passage.text,
                passage.passage_hash, passage.start, passage.end, passage.page,
                passage.heading, passage.locator, now,
            ),
        )
        saved += max(cursor.rowcount, 0)
    return {
        "saved": saved,
        "total": len(split),
        "truncated": len(split) >= passages_mod.MAX_PASSAGES,
    }


def _source_payload(
    row: sqlite3.Row,
    *,
    content_id: int,
    content_hash: str,
    identities: list[dict[str, str]],
    passages: dict[str, Any],
    created_source: bool,
    content_changed: bool,
    cache_hit: bool,
    fetched: bool,
    warnings: list[str],
    run_id: int | None,
    stale: bool = False,
) -> dict[str, Any]:
    return {
        "source_id": int(row["id"]),
        "run_id": run_id,
        "title": row["title"],
        "kind": row["kind"],
        "source_type": row["source_type"],
        "url": row["url"],
        "original_url": row["original_url"],
        "final_url": row["final_url"],
        "path": row["path"],
        "canonical_url": row["canonical_url"],
        "identity": identities[0]["identity"] if identities else None,
        "identity_kind": identities[0]["kind"] if identities else None,
        "identities": [entry["identity"] for entry in identities],
        "content_id": content_id,
        "content_hash": content_hash,
        "raw_hash": row["raw_hash"],
        "retrieved_at": row["retrieved_at"],
        "last_checked_at": row["last_checked_at"],
        "publication_date": row["publication_date"],
        "extraction_status": row["extraction_status"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "chars": len(row["content"] or ""),
        "passages_saved": passages["saved"],
        "passages_total": passages["total"],
        "created_source": created_source,
        "content_changed": content_changed,
        "cache_hit": cache_hit,
        "fetched": fetched,
        "stale": stale,
        "warnings": warnings,
    }


def save_evidence(
    *,
    source: str = "",
    source_id: int = 0,
    text: str = "",
    title: str = "",
    url: str = "",
    path: str = "",
    notes: str = "",
    run_id: int = 0,
    refresh: bool = False,
    max_chars: int | None = None,
    source_type: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Save one piece of evidence into the library (Phase 3 entry point).

    Exactly one mode:

    - ``source``: an http(s) URL or permitted local file path — retrieved and
      extracted with the Phase 1/2 adapters, cached by identity, and split
      into passages;
    - ``source_id``: promote an already-stored Phase 1/2 source row (its text
      is registered, passage-split, and linked to the research run);
    - ``text``: raw text with optional ``url``/``path`` provenance. An
      optional ``source_type`` (e.g. ``web`` for a page snapshot fetched by
      monitoring) records the material kind instead of the generic ``note``.

    Deduplicated by canonical identity; identical content at different
    identities shares one ``source_contents`` row but keeps separate source
    rows and citation URLs. Returns cache/change flags plus passage counts.
    """
    settings = settings or load_settings()
    modes = (
        int(bool((source or "").strip()))
        + int(bool(source_id))
        + int(bool((text or "").strip()))
    )
    if modes != 1:
        raise ToolError(
            "invalid_argument",
            "Provide exactly one of: source (URL or local path), source_id, or text.",
        )
    if run_id:
        with storage.open_db(settings) as conn:
            _require_run(conn, run_id)
    if source_id:
        return _save_from_existing(
            source_id=int(source_id), title=title, notes=notes, run_id=int(run_id),
            settings=settings,
        )
    if (text or "").strip():
        return _save_from_text(
            text=text, title=title, url=url, path=path, notes=notes, run_id=int(run_id),
            max_chars=max_chars, settings=settings, source_type=source_type,
        )
    return _save_from_source(
        source=source.strip(), title=title, notes=notes, run_id=int(run_id),
        refresh=bool(refresh), max_chars=max_chars, settings=settings,
    )


def _save_from_existing(
    *, source_id: int, title: str, notes: str, run_id: int, settings: Settings
) -> dict[str, Any]:
    record = storage.get_source(source_id, include_content=True, settings=settings)
    if record is None:
        raise ToolError("not_found", f"No source with id {source_id}")
    content_text = record.get("content") or ""
    if not content_text.strip():
        raise ToolError(
            "invalid_argument",
            f"Source {source_id} has no stored text; nothing to index as evidence.",
        )
    warnings: list[str] = []
    identities = _identities_from_row(record)
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    candidate = _Candidate(
        identity=identities[0][0] if identities else None,
        identity_kind=identities[0][1] if identities else "note",
        extra_identities=identities[1:],
        title=(record.get("title") or "").strip() or "(untitled)",
        kind=record.get("kind") or "doc",
        source_type=record.get("source_type") or record.get("kind") or "other",
        url=record.get("final_url") or record.get("url"),
        path=record.get("path"),
        text=content_text,
        extraction_status=record.get("extraction_status") or "success",
        error_code=record.get("error_code"),
        error_message=record.get("error_message"),
        content_type=record.get("content_type"),
        file_size_bytes=record.get("file_size_bytes"),
        publication_date=record.get("publication_date"),
        retrieved_at=record.get("retrieved_at") or _utc_now(),
        raw_hash=None,
        normalized_sha256=record.get("normalized_sha256"),
        metadata=dict(metadata),
        origin=f"source:{source_id}",
    )
    with storage.open_db(settings) as conn:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (int(source_id),)).fetchone()
        if row is None:  # pragma: no cover - deleted between the two reads
            raise ToolError("not_found", f"No source with id {source_id}")
        # Preserve the Phase 2 checksum when it is the same text.
        row = _with_normalized_digest(row, content_text)
        return _persist_candidate(
            conn, candidate, row,
            run_id=run_id, notes=notes, title_override=title.strip(),
            settings=settings, warnings=warnings, fetched=False,
        )


def _with_normalized_digest(row: sqlite3.Row, content: str) -> sqlite3.Row:
    """Ensure the row's sha256 matches the content being indexed (legacy rows)."""
    digest = storage.content_hash(content)
    if (row["sha256"] or "") != digest:
        return _RowOverride(row, "sha256", digest)
    return row


class _RowOverride:
    """Read-only view of a sqlite3.Row with one field replaced."""

    def __init__(self, row: sqlite3.Row, key: str, value: Any) -> None:
        self._row = row
        self._key = key
        self._value = value

    def keys(self):
        return self._row.keys()

    def __getitem__(self, key: str) -> Any:
        return self._value if key == self._key else self._row[key]


def _save_from_text(
    *,
    text: str,
    title: str,
    url: str,
    path: str,
    notes: str,
    run_id: int,
    max_chars: int | None,
    settings: Settings,
    source_type: str = "",
) -> dict[str, Any]:
    warnings: list[str] = []
    cap = min(max_chars or settings.doc_max_chars, settings.max_chars_cap)
    record = SourceRecord(
        source_type=(source_type or "").strip() or "note",
        title=(title or "").strip() or "(note)",
        original_url=url.strip() or None,
        final_url=url.strip() or None,
        file_path=path.strip() or None,
        content_type="text/plain",
        extraction_status="success",
        extracted_text=text,
    )
    finalize_text(record, max_chars=cap)
    if not record.extracted_text.strip():
        raise ToolError("invalid_argument", "text must not be empty")
    identity: str | None
    if url.strip():
        identity = canonurl.canonicalize_url(url)
        if identity is None:
            raise ToolError("invalid_argument", f"url is not a usable http(s) URL: {url!r}")
        identity_kind = "url"
    elif path.strip():
        identity = canonurl.normalize_local_path(path)
        identity_kind = "path"
    else:
        identity = f"note:{record.checksum}"
        identity_kind = "note"
    candidate = _candidate_from_record(
        record,
        identity=identity,
        identity_kind=identity_kind,
        extra_identities=[],
        raw_hash=None,
        origin=url.strip() or path.strip() or f"note:{record.checksum}",
    )
    with _PERSIST_LOCK, storage.open_db(settings) as conn:
        existing = _find_identity(conn, identity) if identity else None
        return _persist_candidate(
            conn, candidate, existing,
            run_id=run_id, notes=notes, title_override=title.strip(),
            settings=settings, warnings=warnings,
        )


def _cached_result(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    identity: str | None,
    identity_kind: str,
    origin: str,
    run_id: int,
    settings: Settings,
    fetched: bool,
    was_stale: bool,
    warnings: list[str],
) -> dict[str, Any]:
    """Serve an already-stored record without refetching (cache hit)."""
    if run_id:
        # Phase 4: linking this stored source to the run also respects the
        # unique-source cap (a source already linked stays re-savable).
        planner.check_source_capacity(
            conn,
            run_id,
            already_linked=planner.run_links_source(conn, run_id, int(row["id"])),
        )
    if identity:
        _bind_identity(conn, int(row["id"]), identity, identity_kind)
    conn.execute(
        "UPDATE sources SET last_checked_at = ? WHERE id = ?", (_utc_now(), int(row["id"]))
    )
    metadata = _load_object(row["metadata"])
    origins = [entry for entry in metadata.get("origins", []) if isinstance(entry, dict)]
    entry = {"requested": origin, "at": _utc_now()}
    if origin and entry not in origins and len(origins) < _MAX_ORIGINS:
        origins.append(entry)
        metadata["origins"] = origins
        conn.execute(
            "UPDATE sources SET metadata = ? WHERE id = ?",
            (_dumps(metadata), int(row["id"])),
        )
    if run_id:
        _associate_run(conn, run_id, int(row["id"]))
    content_id = int(row["content_id"]) if row["content_id"] is not None else 0
    passage_count = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM passages WHERE source_id = ?", (int(row["id"]),)
        ).fetchone()["n"]
    )
    identities = [
        {"identity": item["identity"], "kind": item["identity_kind"]}
        for item in conn.execute(
            "SELECT identity, identity_kind FROM source_identities WHERE source_id = ? ORDER BY identity",
            (int(row["id"]),),
        ).fetchall()
    ]
    if was_stale:
        warnings = [*warnings, "served the cached copy; it is due for a refresh"]
    return _source_payload(
        row,
        content_id=content_id,
        content_hash=row["sha256"],
        identities=identities,
        passages={"saved": 0, "total": passage_count, "truncated": False},
        created_source=False,
        content_changed=False,
        cache_hit=True,
        fetched=fetched,
        warnings=warnings,
        run_id=run_id or None,
        stale=source_is_stale(row, settings) or was_stale,
    )


def _save_from_source(
    *,
    source: str,
    title: str,
    notes: str,
    run_id: int,
    refresh: bool,
    max_chars: int | None,
    settings: Settings,
) -> dict[str, Any]:
    kind_label, label = collect.classify_input(source)
    if kind_label == "invalid":
        raise ToolError(
            "invalid_argument",
            f"Unsupported source {source!r}: use an http(s) URL or a permitted local file path.",
        )
    warnings: list[str] = []
    if kind_label == "url":
        requested_identity = canonurl.canonicalize_url(label)
        if requested_identity is None:
            raise ToolError("invalid_argument", f"Not a usable http(s) URL: {label!r}")
        with storage.open_db(settings) as conn:
            existing = _find_identity(conn, requested_identity)
            if (
                existing is not None
                and not refresh
                and _usable_content(existing)
                and not source_is_stale(existing, settings)
            ):
                return _cached_result(
                    conn, existing,
                    identity=requested_identity, identity_kind="url", origin=label,
                    run_id=run_id, settings=settings, fetched=False, was_stale=False,
                    warnings=warnings,
                )
        try:
            prepared = collect.attempt_with_retries(
                lambda: adapters.prepare_url(label, settings), settings, retries_allowed=True
            )
            record = adapters.record_from_prepared(prepared, settings=settings, max_chars=max_chars)
        except ToolError as exc:
            # The retrieval itself failed: never lose earlier stored evidence.
            return _serve_failed_extraction(
                existing=existing,
                final_identity=requested_identity,
                label=label,
                run_id=run_id,
                settings=settings,
                warnings=warnings,
                error_code=exc.code,
                error_message=exc.message,
            )
        raw_hash = (
            hashlib.sha256(prepared.body).hexdigest() if prepared.body is not None else None
        )
        final_identity = canonurl.canonicalize_url(record.final_url or prepared.final_url or label)
        identity = final_identity or requested_identity
        extra = [
            (candidate, "url")
            for candidate in dict.fromkeys([requested_identity, final_identity])
            if candidate and candidate != identity
        ]
        if record.extraction_status not in ("success", "partial") or not record.extracted_text.strip():
            return _serve_failed_extraction(
                existing=existing, final_identity=final_identity, record=record,
                label=label, run_id=run_id, settings=settings, warnings=warnings,
            )
        finalize_text(record, max_chars=max_chars or collect.default_cap(record.source_type, settings))
        candidate = _candidate_from_record(
            record,
            identity=identity,
            identity_kind="url",
            extra_identities=extra,
            raw_hash=raw_hash,
            origin=label,
        )
        with _PERSIST_LOCK, storage.open_db(settings) as conn:
            # Re-check under the lock: a concurrent worker may have stored
            # this identity while this thread was fetching.
            existing = _find_identity(conn, identity) or existing
            return _persist_candidate(
                conn, candidate, existing,
                run_id=run_id, notes=notes, title_override=title.strip(),
                settings=settings, warnings=warnings,
            )

    resolved = localfs.resolve_allowed(label, settings)
    if resolved.is_dir():
        raise ToolError(
            "invalid_argument",
            f"Expected a file, got a directory: {resolved}. Use collect_local_folder instead.",
        )
    identity = canonurl.normalize_local_path(resolved)
    with storage.open_db(settings) as conn:
        existing = _find_identity(conn, identity)
        if (
            existing is not None
            and not refresh
            and _usable_content(existing)
            and not _local_file_modified(existing, resolved)
        ):
            return _cached_result(
                conn, existing,
                identity=identity, identity_kind="path", origin=str(resolved),
                run_id=run_id, settings=settings, fetched=False, was_stale=False,
                warnings=warnings,
            )
    prepared: Any = None
    try:
        prepared = adapters.prepare_file(resolved, settings)
        record = adapters.record_from_prepared(prepared, settings=settings, max_chars=max_chars)
    except ToolError as exc:
        return _serve_failed_extraction(
            existing=existing,
            final_identity=None,
            label=str(resolved),
            run_id=run_id,
            settings=settings,
            warnings=warnings,
            error_code=exc.code,
            error_message=exc.message,
        )
    except OSError as exc:
        return _serve_failed_extraction(
            existing=existing,
            final_identity=None,
            label=str(resolved),
            run_id=run_id,
            settings=settings,
            warnings=warnings,
            error_code="extraction_failed",
            error_message=f"Could not read {resolved}: {exc}",
        )
    raw_hash = hashlib.sha256(prepared.body or b"").hexdigest()
    if record.extraction_status not in ("success", "partial") or not record.extracted_text.strip():
        return _serve_failed_extraction(
            existing=existing, final_identity=None, record=record,
            label=str(resolved), run_id=run_id, settings=settings, warnings=warnings,
        )
    finalize_text(record, max_chars=max_chars or collect.default_cap(record.source_type, settings))
    try:
        stat = resolved.stat()
        fingerprint = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except OSError:  # pragma: no cover - raced deletion after read
        fingerprint = None
    candidate = _candidate_from_record(
        record,
        identity=identity,
        identity_kind="path",
        extra_identities=[],
        raw_hash=raw_hash,
        origin=str(resolved),
        fingerprint=fingerprint,
    )
    with _PERSIST_LOCK, storage.open_db(settings) as conn:
        existing = _find_identity(conn, identity) or existing
        return _persist_candidate(
            conn, candidate, existing,
            run_id=run_id, notes=notes, title_override=title.strip(),
            settings=settings, warnings=warnings,
        )


def _serve_failed_extraction(
    *,
    existing: sqlite3.Row | None,
    final_identity: str | None,
    label: str,
    run_id: int,
    settings: Settings,
    warnings: list[str],
    record: SourceRecord | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    """A refresh produced no text: keep earlier evidence, or raise cleanly."""
    code = error_code or (record.error_code if record is not None else None) or "extraction_failed"
    message = (
        error_message
        or (record.error_message if record is not None else None)
        or f"Extraction failed for {label}."
    )
    identity = final_identity or canonurl.canonicalize_url(label)
    identity_kind = "url" if identity else "path"
    with storage.open_db(settings) as conn:
        cached = _find_identity(conn, identity) if identity else None
        cached = cached or existing
        if cached is not None and _usable_content(cached):
            _mark_last_error(conn, int(cached["id"]), code, message)
            warnings.append(
                "refresh failed; the previously stored evidence is unchanged "
                f"({code})."
            )
            result = _cached_result(
                conn, cached,
                identity=identity,
                identity_kind=identity_kind,
                origin=label, run_id=run_id, settings=settings,
                fetched=True, was_stale=True, warnings=warnings,
            )
            result["cache_hit"] = True
            result["content_changed"] = False
            result["refresh_error"] = {"code": code, "message": message}
            result["stale"] = True
            return result
    raise ToolError(code, message)


def _mark_last_error(conn: sqlite3.Connection, source_id: int, code: str, message: str) -> None:
    row = conn.execute("SELECT metadata FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        return
    metadata = _load_object(row["metadata"])
    metadata["last_refresh_error"] = {"code": code, "message": message, "at": _utc_now()}
    conn.execute(
        "UPDATE sources SET last_checked_at = ?, metadata = ? WHERE id = ?",
        (_utc_now(), _dumps(metadata), source_id),
    )


# ---------------------------------------------------------------------------
# Passage retrieval (FTS5 with a tested LIKE fallback)
# ---------------------------------------------------------------------------


def fts_available(conn: sqlite3.Connection, settings: Settings) -> bool:
    """True when the FTS5 passage index exists and is enabled by config."""
    if (settings.evidence_fts or "auto").lower() == "off":
        return False
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'passages_fts'"
    ).fetchone()
    return row is not None


def _passage_columns() -> str:
    return """
        p.id AS passage_id, p.source_id, p.passage_index, p.text, p.passage_hash,
        p.content_hash, p.start_offset, p.end_offset, p.page, p.heading, p.locator,
        p.created_at,
        s.title, s.url, s.path, s.kind, s.source_type, s.retrieved_at, s.canonical_url
    """


def _passage_payload(row: sqlite3.Row, tokens: list[str], *, rank: float | None = None) -> dict[str, Any]:
    score = _overlap_score(row["text"], row["heading"], tokens)
    payload: dict[str, Any] = {
        "passage_id": int(row["passage_id"]),
        "source_id": int(row["source_id"]),
        "title": row["title"],
        "url": row["url"],
        "path": row["path"],
        "kind": row["kind"],
        "source_type": row["source_type"],
        "retrieved_at": row["retrieved_at"],
        "content_hash": row["content_hash"],
        "passage_hash": row["passage_hash"],
        "locator": row["locator"],
        "page": row["page"],
        "heading": row["heading"],
        "start_offset": row["start_offset"],
        "end_offset": row["end_offset"],
        "text": row["text"],
        "score": round(score, 4),
    }
    if rank is not None:
        payload["rank"] = round(rank, 6)
    return payload


def _overlap_score(text: str, heading: str | None, tokens: list[str]) -> float:
    if not tokens:
        return 0.0
    haystack = (text or "").lower()
    heading_text = (heading or "").lower()
    hits = 0
    for token in tokens:
        if token in heading_text or re.search(
            rf"(?<!\w){re.escape(token)}(?!\w)", haystack, flags=re.UNICODE
        ):
            hits += 1
    return hits / len(tokens)


def _terms_pattern(tokens: list[str]) -> str:
    match = " OR ".join(f'"{token}"' for token in tokens[:12])
    return match


def search_evidence(
    query: str, *, limit: int | None = None, run_id: int = 0, settings: Settings | None = None
) -> dict[str, Any]:
    """Search stored passages (claim terms, FTS5/BM25 with a LIKE fallback).

    Every result keeps its source id, title, URL/path, content hash, locator,
    and exact passage text.
    """
    settings = settings or load_settings()
    tokens = extract_terms(query)
    if not tokens:
        raise ToolError("invalid_argument", "query must contain at least one searchable term")
    bounded = min(max(int(limit or 10), 1), settings.max_list_limit)
    engine = "like"
    candidates: list[tuple[sqlite3.Row, float | None]] = []
    with storage.open_db(settings) as conn:
        if fts_available(conn, settings):
            try:
                sql = f"""
                    SELECT {_passage_columns()}, bm25(passages_fts) AS rank
                    FROM passages_fts
                    JOIN passages p ON p.id = passages_fts.rowid
                    JOIN sources s ON s.id = p.source_id
                    {"JOIN research_run_sources rrs ON rrs.source_id = p.source_id AND rrs.run_id = ?" if run_id else ""}
                    WHERE passages_fts MATCH ?
                    ORDER BY rank LIMIT ?
                """
                params: list[Any] = [run_id] if run_id else []
                params.extend([_terms_pattern(tokens), max(bounded * 5, 25)])
                candidates = [(row, float(row["rank"])) for row in conn.execute(sql, params)]
                engine = "fts5"
            except sqlite3.OperationalError as exc:
                LOGGER.warning("FTS5 query failed (%s); falling back to LIKE search", exc)
                candidates = []
        if not candidates and engine != "fts5":
            candidates = _like_candidates(conn, tokens, run_id=run_id)
        elif not candidates and engine == "fts5":
            # An empty FTS result is a real empty result, not a failure.
            return {"query": query, "engine": engine, "count": 0, "passages": []}
        # Rank: term coverage first, then BM25 rank, then passage id (stable).
        ranked = sorted(
            candidates,
            key=lambda item: (
                -_overlap_score(item[0]["text"], item[0]["heading"], tokens),
                item[1] if item[1] is not None else 0.0,
                int(item[0]["passage_id"]),
            ),
        )[:bounded]
    passages = [_passage_payload(row, tokens, rank=rank) for row, rank in ranked]
    return {"query": query, "engine": engine, "count": len(passages), "passages": passages}


def _like_candidates(
    conn: sqlite3.Connection, tokens: list[str], *, run_id: int
) -> list[tuple[sqlite3.Row, float | None]]:
    clause = " OR ".join(
        "(p.text LIKE ? ESCAPE '\\' OR IFNULL(p.heading, '') LIKE ? ESCAPE '\\')"
        for _ in tokens
    )
    pattern_params: list[Any] = []
    for token in tokens:
        pattern = _like_pattern(token)
        pattern_params.extend([pattern, pattern])
    sql = f"""
        SELECT {_passage_columns()}
        FROM passages p JOIN sources s ON s.id = p.source_id
        {"JOIN research_run_sources rrs ON rrs.source_id = p.source_id AND rrs.run_id = ?" if run_id else ""}
        WHERE ({clause}) LIMIT ?
    """
    params: list[Any] = [run_id] if run_id else []
    params.extend([*pattern_params, _LIKE_CANDIDATE_LIMIT])
    rows = conn.execute(sql, params).fetchall()
    candidates: list[tuple[sqlite3.Row, float | None]] = []
    for row in rows:
        if _overlap_score(row["text"], row["heading"], tokens) > 0:
            candidates.append((row, None))
    return candidates


def retrieve_passages_for_claim(
    claim_id: int = 0, *, text: str = "", limit: int = 5, settings: Settings | None = None
) -> dict[str, Any]:
    """Rank stored passages against one claim (or raw text) — evidence lookup."""
    settings = settings or load_settings()
    query = ""
    resolved_claim_id: int | None = None
    if claim_id:
        with storage.open_db(settings) as conn:
            row = conn.execute("SELECT * FROM claims WHERE id = ?", (int(claim_id),)).fetchone()
        if row is None:
            raise ToolError("not_found", f"No claim with id {claim_id}")
        query = row["claim_text"]
        resolved_claim_id = int(row["id"])
    elif (text or "").strip():
        query = text
    else:
        raise ToolError("invalid_argument", "Provide a claim_id or text to retrieve passages for.")
    result = search_evidence(query, limit=limit, settings=settings)
    return {
        "claim_id": resolved_claim_id,
        "query": query,
        "engine": result["engine"],
        "count": result["count"],
        "passages": result["passages"],
    }


# ---------------------------------------------------------------------------
# Claims and citations
# ---------------------------------------------------------------------------


def register_claim(
    run_id: int,
    claim_text: str,
    *,
    claim_type: str = "claim",
    note: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Register a factual claim on a research run (starts UNVERIFIED).

    Re-registering the identical text on the same run returns the existing
    claim (``created: false``). Support is never implied by registration.
    ``claim_type`` (Phase 6) classifies the statement: ``claim`` (default),
    ``finding``, ``inference``, ``unverified``, or ``question``; ``note``
    carries a short justification (required in the report for inferences —
    it becomes the stated reasoning).
    """
    settings = settings or load_settings()
    clean = _normalize_ws(claim_text)
    if not clean:
        raise ToolError("invalid_argument", "claim_text must not be empty")
    if len(clean) > _MAX_CLAIM_CHARS:
        raise ToolError(
            "invalid_argument",
            f"claim_text is {len(clean)} characters; the limit is {_MAX_CLAIM_CHARS}. "
            "Split it into separate claims.",
        )
    clean_type = (claim_type or "claim").strip().lower()
    if clean_type not in CLAIM_TYPES:
        raise ToolError(
            "invalid_argument",
            f"claim_type must be one of {', '.join(CLAIM_TYPES)}; got {claim_type!r}",
        )
    clean_note = _normalize_ws(note)
    if len(clean_note) > _MAX_NOTE_CHARS:
        raise ToolError(
            "invalid_argument",
            f"note is {len(clean_note)} characters; the limit is {_MAX_NOTE_CHARS}.",
        )
    with storage.open_db(settings) as conn:
        _require_run(conn, run_id)
        existing = conn.execute(
            "SELECT * FROM claims WHERE run_id = ? AND claim_text = ?", (int(run_id), clean)
        ).fetchone()
        if existing is not None:
            return {**_claim_payload(existing), "created": False}
        cursor = conn.execute(
            """
            INSERT INTO claims (run_id, claim_text, status, claim_type, note, created_at, updated_at)
            VALUES (?, ?, 'unverified', ?, ?, ?, ?)
            """,
            (int(run_id), clean, clean_type, clean_note, _utc_now(), _utc_now()),
        )
        row = conn.execute("SELECT * FROM claims WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
        return {**_claim_payload(row), "created": True}


def _claim_payload(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    return {
        "claim_id": int(row["id"]),
        "run_id": int(row["run_id"]),
        "claim_text": row["claim_text"],
        "claim_type": row["claim_type"] if "claim_type" in keys else "claim",
        "note": row["note"] if "note" in keys else "",
        "status": row["status"],
        "verification_status": (
            row["verification_status"] if "verification_status" in keys else ""
        ),
        "verified_at": row["verified_at"] if "verified_at" in keys else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_claim(claim_id: int, *, settings: Settings | None = None) -> dict[str, Any] | None:
    """Return one claim with its citations (or ``None`` when unknown)."""
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        row = conn.execute("SELECT * FROM claims WHERE id = ?", (int(claim_id),)).fetchone()
        if row is None:
            return None
        citations = conn.execute(
            """
            SELECT ci.id AS citation_id, ci.relation, ci.quote, ci.locator,
                   ci.metadata, ci.created_at,
                   p.id AS passage_id, p.passage_index, p.text AS passage_text,
                   p.passage_hash, p.content_hash, p.start_offset, p.end_offset,
                   p.page, p.heading, p.locator AS passage_locator,
                   s.id AS source_id, s.title AS source_title, s.url AS source_url,
                   s.path AS source_path, s.source_type, s.kind,
                   s.retrieved_at AS source_retrieved_at,
                   s.publication_date AS source_publication_date
            FROM citations ci
            JOIN passages p ON p.id = ci.passage_id
            JOIN sources s ON s.id = ci.source_id
            WHERE ci.claim_id = ? ORDER BY ci.id
            """,
            (int(claim_id),),
        ).fetchall()
    return {
        **_claim_payload(row),
        "citations": [_citation_payload(item) for item in citations],
        "derived_status": derive_claim_status([item["relation"] for item in citations]),
    }


def _citation_payload(row: sqlite3.Row) -> dict[str, Any]:
    quote = row["quote"] or ""
    return {
        "citation_id": int(row["citation_id"]),
        "relation": row["relation"],
        "quote": quote,
        "quote_matches_passage": quote_matches(quote, row["passage_text"]) if quote else None,
        "locator": row["locator"],
        "metadata": _load_object(row["metadata"]),
        "created_at": row["created_at"],
        "passage": {
            "passage_id": int(row["passage_id"]),
            "passage_index": row["passage_index"],
            "text": row["passage_text"],
            "passage_hash": row["passage_hash"],
            "content_hash": row["content_hash"],
            "start_offset": row["start_offset"],
            "end_offset": row["end_offset"],
            "page": row["page"],
            "heading": row["heading"],
            "locator": row["passage_locator"],
        },
        "source": {
            "source_id": int(row["source_id"]),
            "title": row["source_title"],
            "url": row["source_url"],
            "path": row["source_path"],
            "source_type": row["source_type"],
            "kind": row["kind"],
            "retrieved_at": row["source_retrieved_at"],
            "publication_date": row["source_publication_date"],
        },
    }


def attach_citation(
    claim_id: int,
    passage_id: int,
    *,
    source_id: int = 0,
    quote: str = "",
    locator: str = "",
    relation: str = "supports",
    metadata: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Attach one exact passage to a claim and re-derive the claim's status.

    The passage must exist; an optional ``source_id`` must match the passage's
    source; a non-empty ``quote`` must actually occur in the passage text
    (whitespace-insensitive; typographic quote glyphs are equated with their
    ASCII equivalents) — fabricated quotations are rejected. The claim status
    is always recomputed from the citation relations.
    """
    settings = settings or load_settings()
    if relation not in CITATION_RELATIONS:
        raise ToolError(
            "invalid_argument",
            f"relation must be one of {', '.join(CITATION_RELATIONS)}; got {relation!r}",
        )
    with storage.open_db(settings) as conn:
        claim = conn.execute("SELECT * FROM claims WHERE id = ?", (int(claim_id),)).fetchone()
        if claim is None:
            raise ToolError("not_found", f"No claim with id {claim_id}")
        passage = conn.execute(
            """
            SELECT p.*, s.title AS source_title FROM passages p
            JOIN sources s ON s.id = p.source_id WHERE p.id = ?
            """,
            (int(passage_id),),
        ).fetchone()
        if passage is None:
            raise ToolError(
                "not_found",
                f"No passage with id {passage_id}; search_evidence returns passage ids.",
            )
        if source_id and int(source_id) != int(passage["source_id"]):
            raise ToolError(
                "invalid_argument",
                f"Passage {passage_id} belongs to source {passage['source_id']}, not {source_id}.",
            )
        clean_quote = (quote or "").strip()
        if clean_quote and not quote_matches(clean_quote, passage["text"]):
            raise ToolError(
                "invalid_argument",
                "The quote does not occur in the passage text; refusing to store a "
                "fabricated quotation. Copy an exact excerpt from the passage.",
            )
        existing = conn.execute(
            "SELECT id FROM citations WHERE claim_id = ? AND passage_id = ? AND relation = ?",
            (int(claim_id), int(passage_id), relation),
        ).fetchone()
        if existing is None:
            cursor = conn.execute(
                """
                INSERT INTO citations (claim_id, passage_id, source_id, relation, quote, locator, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(claim_id), int(passage_id), int(passage["source_id"]), relation,
                    clean_quote, (locator or "").strip() or (passage["locator"] or ""),
                    _dumps(dict(metadata or {})), _utc_now(),
                ),
            )
            citation_id = int(cursor.lastrowid)
            created = True
        else:
            citation_id = int(existing["id"])
            created = False
        relations = [
            item["relation"]
            for item in conn.execute(
                "SELECT relation FROM citations WHERE claim_id = ?", (int(claim_id),)
            ).fetchall()
        ]
        status = derive_claim_status(relations)
        conn.execute(
            "UPDATE claims SET status = ?, updated_at = ? WHERE id = ?",
            (status, _utc_now(), int(claim_id)),
        )
        updated = conn.execute("SELECT * FROM claims WHERE id = ?", (int(claim_id),)).fetchone()
        row = conn.execute(
            """
            SELECT ci.id AS citation_id, ci.relation, ci.quote, ci.locator,
                   ci.metadata, ci.created_at,
                   p.id AS passage_id, p.passage_index, p.text AS passage_text,
                   p.passage_hash, p.content_hash, p.start_offset, p.end_offset,
                   p.page, p.heading, p.locator AS passage_locator,
                   s.id AS source_id, s.title AS source_title, s.url AS source_url,
                   s.path AS source_path, s.source_type, s.kind,
                   s.retrieved_at AS source_retrieved_at,
                   s.publication_date AS source_publication_date
            FROM citations ci
            JOIN passages p ON p.id = ci.passage_id
            JOIN sources s ON s.id = ci.source_id
            WHERE ci.id = ?
            """,
            (citation_id,),
        ).fetchone()
        return {
            "created": created,
            "claim": _claim_payload(updated),
            "derived_status": status,
            "citation": _citation_payload(row),
        }


def get_citations_for_claim(
    claim_id: int, *, settings: Settings | None = None
) -> dict[str, Any]:
    """Claim + every attached citation with its exact passage and source."""
    settings = settings or load_settings()
    record = get_claim(claim_id, settings=settings)
    if record is None:
        raise ToolError("not_found", f"No claim with id {claim_id}")
    return record


def get_unverified_claims(
    *, run_id: int = 0, limit: int | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """Claims that have no established support (unverified or needs-review)."""
    settings = settings or load_settings()
    bounded = min(max(int(limit or settings.default_list_limit), 1), settings.max_list_limit)
    clauses = ["c.status IN ('unverified', 'needs_review')"]
    params: list[Any] = []
    if run_id:
        clauses.append("c.run_id = ?")
        params.append(int(run_id))
    sql = f"""
        SELECT c.*, r.query AS run_query,
            (SELECT COUNT(*) FROM citations ci WHERE ci.claim_id = c.id) AS citation_count
        FROM claims c JOIN research_runs r ON r.id = c.run_id
        WHERE {' AND '.join(clauses)}
        ORDER BY c.id DESC LIMIT ?
    """
    params.append(bounded)
    with storage.open_db(settings) as conn:
        rows = conn.execute(sql, params).fetchall()
    claims = [
        {
            "claim_id": int(row["id"]),
            "run_id": int(row["run_id"]),
            "run_query": row["run_query"],
            "claim_text": row["claim_text"],
            "status": row["status"],
            "citation_count": int(row["citation_count"]),
        }
        for row in rows
    ]
    return {"count": len(claims), "claims": claims}


def _claims_with_citations(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    """Every claim of a run with its citations joined to passages/sources."""
    claim_rows = conn.execute(
        "SELECT * FROM claims WHERE run_id = ? ORDER BY id", (int(run_id),)
    ).fetchall()
    claims: list[dict[str, Any]] = []
    for claim in claim_rows:
        keys = claim.keys()
        citation_rows = conn.execute(
            """
            SELECT ci.id AS citation_id, ci.relation, ci.quote, ci.locator,
                   ci.metadata, ci.created_at,
                   p.id AS passage_id, p.passage_index, p.text AS passage_text,
                   p.passage_hash, p.content_hash, p.start_offset, p.end_offset,
                   p.page, p.heading, p.locator AS passage_locator,
                   s.id AS source_id, s.title AS source_title, s.url AS source_url,
                   s.path AS source_path, s.source_type, s.kind,
                   s.retrieved_at AS source_retrieved_at,
                   s.publication_date AS source_publication_date
            FROM citations ci
            JOIN passages p ON p.id = ci.passage_id
            JOIN sources s ON s.id = ci.source_id
            WHERE ci.claim_id = ? ORDER BY ci.id
            """,
            (int(claim["id"]),),
        ).fetchall()
        claims.append(
            {
                "claim_id": int(claim["id"]),
                "run_id": int(claim["run_id"]),
                "claim_text": claim["claim_text"],
                "claim_type": claim["claim_type"] if "claim_type" in keys else "claim",
                "note": claim["note"] if "note" in keys else "",
                "status": claim["status"],
                "verification_status": (
                    claim["verification_status"] if "verification_status" in keys else ""
                ),
                "verified_at": claim["verified_at"] if "verified_at" in keys else None,
                "created_at": claim["created_at"],
                "updated_at": claim["updated_at"],
                "citations": [
                    {
                        "citation_id": int(row["citation_id"]),
                        "relation": row["relation"],
                        "quote": row["quote"] or "",
                        "locator": row["locator"] or "",
                        "claim_id": int(claim["id"]),
                        "passage_id": int(row["passage_id"]),
                        "passage_text": row["passage_text"],
                        "passage_index": row["passage_index"],
                        "passage_hash": row["passage_hash"],
                        "content_hash": row["content_hash"],
                        "start_offset": row["start_offset"],
                        "end_offset": row["end_offset"],
                        "page": row["page"],
                        "heading": row["heading"],
                        "passage_locator": row["passage_locator"],
                        "source_id": int(row["source_id"]),
                        "source_title": row["source_title"],
                        "source_url": row["source_url"],
                        "source_path": row["source_path"],
                        "source_type": row["source_type"],
                        "source_kind": row["kind"],
                        "source_retrieved_at": row["source_retrieved_at"],
                        "source_publication_date": row["source_publication_date"],
                    }
                    for row in citation_rows
                ],
            }
        )
    return claims


def claims_with_citations(
    run_id: int, *, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Public wrapper: every claim of a run with citations, passages, sources.

    Used by the Phase 6 synthesis layer (report building, conflict analysis)
    without re-implementing the join.
    """
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        _require_run(conn, int(run_id))
        return _claims_with_citations(conn, int(run_id))


# ---------------------------------------------------------------------------
# Reports: metadata, rendering, structural citation validation
# ---------------------------------------------------------------------------


def save_report_metadata(
    run_id: int,
    *,
    title: str,
    path: str,
    content: str,
    format: str = "markdown",
    citation_map: dict[str, int] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Record a generated report (path, hash, citation map) before validation."""
    settings = settings or load_settings()
    if format not in REPORT_FORMATS:
        raise ToolError("invalid_argument", f"format must be one of {REPORT_FORMATS}")
    clean_path = str(Path(path))
    with storage.open_db(settings) as conn:
        _require_run(conn, run_id)
        cursor = conn.execute(
            """
            INSERT INTO reports (
                run_id, title, path, format, content, content_hash,
                citation_map, citation_validation_status, validation, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'unvalidated', '{}', ?)
            """,
            (
                int(run_id), (title or "").strip(), clean_path, format, content,
                storage.content_hash(content), _dumps(dict(citation_map or {})), _utc_now(),
            ),
        )
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
    return _report_payload(row)


def _report_payload(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    return {
        "report_id": int(row["id"]),
        "run_id": int(row["run_id"]),
        "title": row["title"],
        "path": row["path"],
        "format": row["format"],
        "content_hash": row["content_hash"],
        "citation_map": _load_object(row["citation_map"]),
        "finding_map": _load_object(row["finding_map"]) if "finding_map" in keys else {},
        "image_map": _load_object(row["image_map"]) if "image_map" in keys else {},
        "generation_config": (
            _load_object(row["generation_config"]) if "generation_config" in keys else {}
        ),
        "template_version": (
            row["template_version"] if "template_version" in keys else "phase3"
        ),
        "citation_validation_status": row["citation_validation_status"],
        "validation": _load_object(row["validation"]),
        "generated_at": row["generated_at"],
        "validated_at": row["validated_at"],
    }


def get_report(report_id: int, *, settings: Settings | None = None) -> dict[str, Any] | None:
    """Return one stored report record (metadata + validation), or ``None``."""
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (int(report_id),)).fetchone()
    return _report_payload(row) if row is not None else None


def generate_report(
    run_id: int, *, title: str = "", settings: Settings | None = None
) -> dict[str, Any]:
    """Render, store, and structurally validate a claims/citations report.

    The Markdown is recorded in the ``reports`` table with its content hash
    and citation map, written into the reports directory, then validated; the
    run is marked completed. Evidence rows are already committed by their own
    operations, so a failed generation step never loses evidence.
    """
    settings = settings or load_settings()
    from . import report as report_mod

    with storage.open_db(settings) as conn:
        run_row = _require_run(conn, run_id)
        claims = _claims_with_citations(conn, run_id)
        run = _run_payload(run_row)
        # Phase 4: stored plan/budget/gaps/log facts enrich the report without
        # any model-written prose (question, scope, methodology, gaps, sources).
        overview = planner.describe_run(conn, int(run_id))
        event_counts = {
            row["kind"]: int(row["n"])
            for row in conn.execute(
                "SELECT kind, COUNT(*) AS n FROM research_run_events WHERE run_id = ? GROUP BY kind",
                (int(run_id),),
            ).fetchall()
        }
        stopping_row = conn.execute(
            """
            SELECT message FROM research_run_events
            WHERE run_id = ? AND kind = 'stopping' ORDER BY id DESC LIMIT 1
            """,
            (int(run_id),),
        ).fetchone()
        run_sources = [
            {
                "source_id": int(row["id"]),
                "title": row["title"],
                "url": row["url"],
                "path": row["path"],
                "source_type": row["source_type"],
                "publication_date": row["publication_date"],
                "retrieved_at": row["retrieved_at"],
                "extraction_status": row["extraction_status"],
            }
            for row in conn.execute(
                """
                SELECT s.* FROM research_run_sources rrs JOIN sources s ON s.id = rrs.source_id
                WHERE rrs.run_id = ? ORDER BY rrs.added_at, s.id
                """,
                (int(run_id),),
            ).fetchall()
        ]
        display_title = title.strip() or f"Research run {run_id}: {run['query']}"
        markdown, citation_map = report_mod.build_evidence_markdown(
            run,
            claims,
            title=display_title,
            plan=overview["plan"],
            subquestions=overview["subquestions"],
            gaps=overview["gaps"],
            budget=overview["budget"],
            stopping=overview["stopping"],
            stopping_reason=stopping_row["message"] if stopping_row is not None else "",
            methodology={
                "sources_collected": overview["budget"]["sources_used"],
                "searches_logged": event_counts.get("search", 0),
                "followup_rounds_used": overview["budget"]["followup_rounds_used"],
                "max_followup_rounds": overview["budget"]["max_followup_rounds"],
                "retrieval_failures": event_counts.get("source_failed", 0),
                "claims_registered": len(claims),
                "citations_attached": sum(len(claim["citations"]) for claim in claims),
            },
            run_sources=run_sources,
        )
        payload = save_report_metadata_in_conn(
            conn,
            run_id,
            title=display_title,
            path="",
            content=markdown,
            citation_map=citation_map,
        )
        report_id = int(payload["report_id"])
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"evidence-run{run_id}-{report_mod.slugify(display_title)}-{stamp}.md"
    path = settings.reports_dir / filename
    try:
        path.write_text(markdown, encoding="utf-8")
    except OSError as exc:
        raise ToolError("storage_error", f"Could not write report file: {exc}") from exc
    with storage.open_db(settings) as conn:
        conn.execute("UPDATE reports SET path = ? WHERE id = ?", (str(path), report_id))
    validation = validate_report_citations(report_id, settings=settings)
    finish_run(run_id, status="completed", settings=settings)
    stored = get_report(report_id, settings=settings) or payload
    return {
        "report_id": report_id,
        "run_id": int(run_id),
        "title": stored["title"],
        "path": str(path),
        "filename": filename,
        "citation_map": citation_map,
        "markdown": markdown,
        "claims_rendered": len(claims),
        "validation": validation,
        "citation_validation_status": validation["status"],
    }


def save_report_metadata_in_conn(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    title: str,
    path: str,
    content: str,
    citation_map: dict[str, int] | None = None,
    finding_map: dict[str, int] | None = None,
    image_map: dict[str, int] | None = None,
    generation_config: dict[str, Any] | None = None,
    template_version: str = "phase3",
) -> dict[str, Any]:
    """Insert a report row on an existing connection (see save_report_metadata)."""
    cursor = conn.execute(
        """
        INSERT INTO reports (
            run_id, title, path, format, content, content_hash,
            citation_map, citation_validation_status, validation, generated_at,
            template_version, finding_map, image_map, generation_config
        ) VALUES (?, ?, ?, 'markdown', ?, ?, ?, 'unvalidated', '{}', ?, ?, ?, ?, ?)
        """,
        (
            int(run_id), (title or "").strip(), str(Path(path)), content,
            storage.content_hash(content), _dumps(dict(citation_map or {})), _utc_now(),
            str(template_version or "phase3"),
            _dumps(dict(finding_map or {})),
            _dumps(dict(image_map or {})),
            _dumps(dict(generation_config or {})),
        ),
    )
    row = conn.execute("SELECT * FROM reports WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
    return _report_payload(row)


def validate_report_citations(
    report_id: int, *, settings: Settings | None = None
) -> dict[str, Any]:
    """Structural citation validation for one report (updates its status).

    Checks (never modifies evidence): the report file and content hashes; that
    every rendered ``[Sn]`` resolves to a stored citation of this run; that
    every citation's passage exists, belongs to its source, matches its hash
    and offsets, and contains its quote; that every claim is rendered and its
    status is consistent with its citation relations; that references carry
    available provenance (URL/path, retrieval date, locator); and that no
    foreign key dangles. It does **not** judge whether a statement is true.
    """
    settings = settings or load_settings()
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    with storage.open_db(settings) as conn:
        report_row = conn.execute("SELECT * FROM reports WHERE id = ?", (int(report_id),)).fetchone()
        if report_row is None:
            raise ToolError("not_found", f"No report with id {report_id}")
        run_row = conn.execute(
            "SELECT * FROM research_runs WHERE id = ?", (report_row["run_id"],)
        ).fetchone()
        if run_row is None:
            errors.append(
                {
                    "code": "orphan_report",
                    "message": f"Report {report_id} references missing run {report_row['run_id']}.",
                }
            )

        markdown = report_row["content"] or ""
        stored_hash = report_row["content_hash"]
        if markdown and storage.content_hash(markdown) != stored_hash:
            errors.append(
                {
                    "code": "report_content_hash_mismatch",
                    "message": "Stored report copy does not match its recorded content hash.",
                }
            )
        report_path = Path(report_row["path"] or "")
        if report_row["path"] and report_path.exists():
            try:
                file_text = report_path.read_text(encoding="utf-8")
            except OSError as exc:
                errors.append(
                    {
                        "code": "report_file_unreadable",
                        "message": f"Report file could not be read: {exc}",
                    }
                )
            else:
                if storage.content_hash(file_text) != stored_hash:
                    errors.append(
                        {
                            "code": "report_file_hash_mismatch",
                            "message": "On-disk report differs from the stored content hash.",
                        }
                    )
                else:
                    markdown = file_text
        else:
            errors.append(
                {
                    "code": "report_file_missing",
                    "message": f"Report file not found at {report_row['path']!r}.",
                }
            )
            if markdown:
                warnings.append("validated against the database copy; the file is missing on disk")

        citation_map = _load_object(report_row["citation_map"])
        rendered_ids = sorted({int(value) for value in re.findall(r"\[S(\d+)\]", markdown)})
        checks["citation_identifiers_rendered"] = len(rendered_ids)
        for number in rendered_ids:
            if f"S{number}" not in citation_map:
                errors.append(
                    {
                        "code": "unresolved_citation_identifier",
                        "message": f"[S{number}] appears in the report but resolves to no citation.",
                    }
                )

        run_claims = _claims_with_citations(conn, int(report_row["run_id"]))
        claim_by_id = {claim["claim_id"]: claim for claim in run_claims}
        normalized_markdown = _normalize_ws(markdown)
        checks["claims_registered"] = len(run_claims)
        checks["citations_registered"] = len(citation_map)

        # 1) Every mapped citation resolves and its evidence is intact.
        resolved_sources: dict[int, dict[str, Any]] = {}
        for label, citation_id in citation_map.items():
            row = conn.execute(
                """
                SELECT ci.id AS citation_id, ci.claim_id, ci.relation, ci.quote,
                       ci.source_id, ci.passage_id,
                       p.id AS passage_exists, p.text AS passage_text, p.passage_hash,
                       p.content_hash AS passage_content_hash, p.start_offset, p.end_offset,
                       p.source_id AS passage_source_id,
                       s.id AS source_exists, s.title AS source_title, s.url AS source_url,
                       s.path AS source_path, s.retrieved_at AS source_retrieved_at,
                       s.publication_date AS source_publication_date, s.source_type
                FROM citations ci
                LEFT JOIN passages p ON p.id = ci.passage_id
                LEFT JOIN sources s ON s.id = ci.source_id
                WHERE ci.id = ?
                """,
                (int(citation_id),),
            ).fetchone()
            if row is None:
                errors.append(
                    {
                        "code": "unresolved_citation_record",
                        "message": f"[{label}] maps to citation {citation_id}, which does not exist.",
                    }
                )
                continue
            claim = claim_by_id.get(int(row["claim_id"]))
            if claim is None or claim["run_id"] != int(report_row["run_id"]):
                errors.append(
                    {
                        "code": "citation_wrong_run",
                        "message": f"[{label}] (citation {citation_id}) belongs to a claim outside this run.",
                    }
                )
            if row["passage_exists"] is None:
                errors.append(
                    {
                        "code": "dangling_citation_passage",
                        "message": f"[{label}] points to missing passage {row['passage_id']}.",
                    }
                )
                continue
            if row["source_exists"] is None:
                errors.append(
                    {
                        "code": "dangling_citation_source",
                        "message": f"[{label}] points to missing source {row['source_id']}.",
                    }
                )
                continue
            if int(row["passage_source_id"]) != int(row["source_id"]):
                errors.append(
                    {
                        "code": "citation_source_mismatch",
                        "message": f"[{label}] cites source {row['source_id']} but its passage belongs "
                        f"to source {row['passage_source_id']}.",
                    }
                )
            content_row = conn.execute(
                "SELECT extracted_text FROM source_contents WHERE content_hash = ?",
                (row["passage_content_hash"],),
            ).fetchone()
            if content_row is None:
                errors.append(
                    {
                        "code": "passage_content_missing",
                        "message": f"[{label}] passage {row['passage_id']} references a missing content blob.",
                    }
                )
            else:
                passage_text = row["passage_text"] or ""
                if storage.content_hash(passage_text) != row["passage_hash"]:
                    errors.append(
                        {
                            "code": "passage_hash_mismatch",
                            "message": f"[{label}] passage {row['passage_id']} text does not match its hash.",
                        }
                    )
                content_text = content_row["extracted_text"] or ""
                start, end = int(row["start_offset"]), int(row["end_offset"])
                if not (0 <= start <= end <= len(content_text)) or content_text[start:end] != passage_text:
                    errors.append(
                        {
                            "code": "passage_offset_mismatch",
                            "message": f"[{label}] passage {row['passage_id']} does not match its stored "
                            "content offsets.",
                        }
                    )
            quote = row["quote"] or ""
            if quote and not quote_matches(quote, row["passage_text"] or ""):
                errors.append(
                    {
                        "code": "quote_not_in_passage",
                        "message": f"[{label}] stored quote does not occur in passage {row['passage_id']}.",
                    }
                )
            resolved_sources[int(row["citation_id"])] = {
                "label": label,
                "source_id": int(row["source_id"]),
                "source_title": row["source_title"],
                "source_url": row["source_url"],
                "source_path": row["source_path"],
                "source_retrieved_at": row["source_retrieved_at"],
                "source_type": row["source_type"],
            }

        # 2) Claims: rendered, status consistent, support actually attached.
        for claim in run_claims:
            if _normalize_ws(claim["claim_text"]) not in normalized_markdown:
                errors.append(
                    {
                        "code": "claim_not_rendered",
                        "message": f"Claim {claim['claim_id']} does not appear in the report.",
                    }
                )
            relations = [citation["relation"] for citation in claim["citations"]]
            derived = derive_claim_status(relations)
            if claim["status"] != derived:
                errors.append(
                    {
                        "code": "claim_status_inconsistent",
                        "message": f"Claim {claim['claim_id']} is stored as {claim['status']!r} but its "
                        f"citations derive {derived!r}.",
                    }
                )
            if claim["status"] in ("supported", "partially_supported") and not any(
                relation in ("supports", "partially_supports") for relation in relations
            ):
                errors.append(
                    {
                        "code": "supported_claim_without_citation",
                        "message": f"Claim {claim['claim_id']} is marked {claim['status']} but has no "
                        "supporting citation.",
                    }
                )
            if claim["status"] == "contradicted" and "contradicts" not in relations:
                errors.append(
                    {
                        "code": "contradicted_claim_without_contradiction",
                        "message": f"Claim {claim['claim_id']} is marked contradicted but no citation "
                        "contradicts it.",
                    }
                )
            if claim["status"] in ("supported", "partially_supported", "contradicted"):
                labels = [
                    label
                    for label, citation_id in citation_map.items()
                    if citation_id in {citation["citation_id"] for citation in claim["citations"]}
                ]
                if not labels:
                    errors.append(
                        {
                            "code": "claim_citations_not_rendered",
                            "message": f"Claim {claim['claim_id']} has no rendered citation identifier.",
                        }
                    )

        # 3) References carry the available provenance.
        for label, citation_id in citation_map.items():
            info = resolved_sources.get(int(citation_id))
            if info is None:
                continue
            if info["source_url"]:
                if info["source_url"] not in markdown:
                    errors.append(
                        {
                            "code": "reference_provenance_missing",
                            "message": f"[{label}] URL {info['source_url']!r} is missing from the references.",
                        }
                    )
            elif info["source_path"]:
                if info["source_path"] not in markdown:
                    errors.append(
                        {
                            "code": "reference_provenance_missing",
                            "message": f"[{label}] file path {info['source_path']!r} is missing from the references.",
                        }
                    )
            else:
                warnings.append(f"[{label}] has neither a URL nor a file path recorded.")
            if info["source_retrieved_at"] and str(info["source_retrieved_at"])[:10] not in markdown:
                warnings.append(f"[{label}] retrieval date is not shown in the references.")

        # 4) No dangling foreign keys anywhere.
        dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
        checks["foreign_key_violations"] = len(dangling)
        if dangling:
            errors.append(
                {
                    "code": "foreign_key_violations",
                    "message": f"{len(dangling)} dangling foreign-key reference(s) found in the database.",
                }
            )

        unverified = [
            claim["claim_id"] for claim in run_claims if claim["status"] in ("unverified", "needs_review")
        ]
        contradicted = [
            claim["claim_id"] for claim in run_claims if claim["status"] == "contradicted"
        ]
        if not run_claims:
            warnings.append("no claims are registered for this run")
        passed = not errors
        status = "validated" if passed else "failed"
        validated_at = _utc_now()
        validation = {
            "validation_kind": "structural",
            "semantic_support": "not_evaluated",
            "semantic_note": _SEMANTIC_NOTE,
            "passed": passed,
            "status": status,
            "checked": checks,
            "errors": errors,
            "warnings": warnings,
            "claim_statuses": {
                claim["claim_id"]: claim["status"] for claim in run_claims
            },
            "unverified_claims": unverified,
            "contradicted_claims": contradicted,
            "cited_sources": [
                {
                    "label": info["label"],
                    "citation_id": citation_id,
                    "source_id": info["source_id"],
                    "title": info["source_title"],
                    "url": info["source_url"],
                    "path": info["source_path"],
                    "retrieved_at": info["source_retrieved_at"],
                    "source_type": info["source_type"],
                }
                for citation_id, info in resolved_sources.items()
            ],
            "validated_at": validated_at,
        }
        conn.execute(
            "UPDATE reports SET citation_validation_status = ?, validation = ?, validated_at = ? WHERE id = ?",
            (status, _dumps(validation), validated_at, int(report_id)),
        )
    return {
        "report_id": int(report_id),
        "run_id": int(report_row["run_id"]),
        "path": report_row["path"],
        **validation,
    }


# ---------------------------------------------------------------------------
# Enriched source lookup
# ---------------------------------------------------------------------------


def get_source_by_id(
    source_id: int,
    *,
    include_content: bool = True,
    include_passages: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """One stored source with its evidence extras (content blob, identities,
    run links, passage count, staleness) — the Phase 3 view of Phase 1/2 rows."""
    settings = settings or load_settings()
    record = storage.get_source(source_id, include_content=include_content, settings=settings)
    if record is None:
        return None
    with storage.open_db(settings) as conn:
        row = conn.execute(
            "SELECT canonical_url, content_id, raw_hash, last_checked_at, retrieved_at, created_at, "
            "path, url, final_url, extraction_status, content, metadata FROM sources WHERE id = ?",
            (int(source_id),),
        ).fetchone()
        if row is None:  # pragma: no cover - deleted between reads
            return None
        content = None
        if row["content_id"]:
            content_row = conn.execute(
                """
                SELECT id AS content_id, content_hash, raw_hash, extraction_status,
                       created_at, updated_at, storage_location, file_size_bytes
                FROM source_contents WHERE id = ?
                """,
                (int(row["content_id"]),),
            ).fetchone()
            if content_row is not None:
                content = dict(content_row)
        identities = [
            {"identity": item["identity"], "kind": item["identity_kind"]}
            for item in conn.execute(
                "SELECT identity, identity_kind FROM source_identities WHERE source_id = ? ORDER BY identity",
                (int(source_id),),
            ).fetchall()
        ]
        run_ids = [
            int(item["run_id"])
            for item in conn.execute(
                "SELECT run_id FROM research_run_sources WHERE source_id = ? ORDER BY run_id",
                (int(source_id),),
            ).fetchall()
        ]
        passage_rows = conn.execute(
            """
            SELECT id, passage_index, locator, page, heading, start_offset, end_offset,
                   passage_hash, content_hash, text
            FROM passages WHERE source_id = ? ORDER BY passage_index, id
            """,
            (int(source_id),),
        ).fetchall()
    stale = source_is_stale(row, settings)
    record["evidence"] = {
        "content": content,
        "identities": identities,
        "run_ids": run_ids,
        "passage_count": len(passage_rows),
        "passages_indexed": bool(passage_rows),
        "stale": stale,
    }
    if include_passages:
        record["passages"] = [dict(item) for item in passage_rows]
    return record






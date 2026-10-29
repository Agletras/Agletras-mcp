"""SQLite-backed store for research sources (citations and caching).

One database file, one connection per call, no ORM. Phase 1/2 de-duplicate
`sources` by SHA-256 of the extracted text so saving the same page or PDF
twice returns the existing record. Phase 3 adds the evidence-library tables
(research runs, source identities, reusable content blobs, passages, claims,
citations, reports) through versioned, incremental migrations; see
:data:`SCHEMA_VERSION` and :data:`MIGRATIONS`.

Connection setup (every call): foreign-key enforcement ON, WAL journal mode
(concurrent readers/writers, crash-safe), and a busy timeout so concurrent
tool calls wait instead of failing immediately. Every operation runs in one
implicit transaction and commits at the end of the ``with`` block; any error
rolls the whole block back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from .config import Settings, load_settings
from .errors import ToolError
from .source_record import SourceRecord
from .textutil import excerpt

LOGGER = logging.getLogger(__name__)

SOURCE_KINDS = ("url", "pdf", "note", "search_result", "doc")

#: Current schema version. v1 = Phase 1/2 source store; v2 = Phase 3 evidence
#: library (runs, identities, contents, passages, claims, citations, reports);
#: v3 = Phase 4 research planner (run plans, subquestions, execution log);
#: v4 = Phase 6 verification and synthesis (claim verifications, claim
#: classification, report template/maps/config); v5 = Phase 7 CLI run
#: checkpoints and stage tracking.
SCHEMA_VERSION = 7

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    url TEXT,
    path TEXT,
    content TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- Phase 2 unified-record columns (see source_record.SourceRecord)
    source_type TEXT NOT NULL DEFAULT '',
    original_url TEXT,
    final_url TEXT,
    content_type TEXT,
    file_size_bytes INTEGER,
    publication_date TEXT,
    retrieved_at TEXT,
    extraction_status TEXT NOT NULL DEFAULT 'success',
    error_code TEXT,
    error_message TEXT,
    metadata TEXT NOT NULL DEFAULT '',
    normalized_sha256 TEXT
);
CREATE INDEX IF NOT EXISTS idx_sources_sha256 ON sources(sha256);
CREATE INDEX IF NOT EXISTS idx_sources_kind ON sources(kind);
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""

_PHASE2_COLUMNS = (
    ("source_type", "TEXT NOT NULL DEFAULT ''"),
    ("original_url", "TEXT"),
    ("final_url", "TEXT"),
    ("content_type", "TEXT"),
    ("file_size_bytes", "INTEGER"),
    ("publication_date", "TEXT"),
    ("retrieved_at", "TEXT"),
    ("extraction_status", "TEXT NOT NULL DEFAULT 'success'"),
    ("error_code", "TEXT"),
    ("error_message", "TEXT"),
    ("metadata", "TEXT NOT NULL DEFAULT ''"),
    ("normalized_sha256", "TEXT"),
)

# --- Phase 3 (evidence library) column additions on `sources` --------------
_EVIDENCE_SOURCE_COLUMNS = (
    ("canonical_url", "TEXT"),
    ("content_id", "INTEGER REFERENCES source_contents(id) ON DELETE SET NULL"),
    ("raw_hash", "TEXT"),
    ("last_checked_at", "TEXT"),
)

_EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    config TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS source_contents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL UNIQUE,
    raw_hash TEXT,
    extracted_text TEXT NOT NULL DEFAULT '',
    extraction_status TEXT NOT NULL DEFAULT 'success',
    error_code TEXT,
    error_message TEXT,
    content_type TEXT,
    file_size_bytes INTEGER,
    storage_location TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_identities (
    identity TEXT PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    identity_kind TEXT NOT NULL DEFAULT 'url',
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_run_sources (
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL,
    PRIMARY KEY (run_id, source_id)
);

CREATE TABLE IF NOT EXISTS passages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    content_id INTEGER NOT NULL REFERENCES source_contents(id) ON DELETE RESTRICT,
    content_hash TEXT NOT NULL,
    passage_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    passage_hash TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    page INTEGER,
    heading TEXT,
    locator TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (source_id, content_hash, passage_index)
);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    claim_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unverified',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, claim_text)
);

CREATE TABLE IF NOT EXISTS citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE RESTRICT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    relation TEXT NOT NULL DEFAULT 'supports',
    quote TEXT NOT NULL DEFAULT '',
    locator TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (claim_id, passage_id, relation)
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL,
    format TEXT NOT NULL DEFAULT 'markdown',
    content TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    citation_map TEXT NOT NULL DEFAULT '{}',
    citation_validation_status TEXT NOT NULL DEFAULT 'unvalidated',
    validation TEXT NOT NULL DEFAULT '{}',
    generated_at TEXT NOT NULL,
    validated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON research_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status ON research_runs(status);
CREATE INDEX IF NOT EXISTS idx_sources_canonical_url ON sources(canonical_url);
CREATE INDEX IF NOT EXISTS idx_source_identities_source ON source_identities(source_id);
CREATE INDEX IF NOT EXISTS idx_source_contents_raw ON source_contents(raw_hash);
CREATE INDEX IF NOT EXISTS idx_run_sources_source ON research_run_sources(source_id);
CREATE INDEX IF NOT EXISTS idx_passages_source ON passages(source_id, content_hash);
CREATE INDEX IF NOT EXISTS idx_passages_hash ON passages(passage_hash);
CREATE INDEX IF NOT EXISTS idx_claims_run ON claims(run_id, status);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_citations_claim ON citations(claim_id);
CREATE INDEX IF NOT EXISTS idx_citations_passage ON citations(passage_id);
CREATE INDEX IF NOT EXISTS idx_citations_source ON citations(source_id);
CREATE INDEX IF NOT EXISTS idx_reports_run ON reports(run_id);
CREATE INDEX IF NOT EXISTS idx_reports_generated ON reports(generated_at);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(
    text,
    heading,
    content='passages',
    content_rowid='id',
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS passages_fts_ai AFTER INSERT ON passages BEGIN
    INSERT INTO passages_fts(rowid, text, heading)
    VALUES (new.id, new.text, IFNULL(new.heading, ''));
END;
CREATE TRIGGER IF NOT EXISTS passages_fts_ad AFTER DELETE ON passages BEGIN
    INSERT INTO passages_fts(passages_fts, rowid, text, heading)
    VALUES ('delete', old.id, old.text, IFNULL(old.heading, ''));
END;
CREATE TRIGGER IF NOT EXISTS passages_fts_au AFTER UPDATE ON passages BEGIN
    INSERT INTO passages_fts(passages_fts, rowid, text, heading)
    VALUES ('delete', old.id, old.text, IFNULL(old.heading, ''));
    INSERT INTO passages_fts(rowid, text, heading)
    VALUES (new.id, new.text, IFNULL(new.heading, ''));
END;
"""

_KIND_TO_SOURCE_TYPE = {"url": "web", "pdf": "pdf", "note": "note"}
_SOURCE_TYPE_TO_KIND_FILTER = {
    "web": "url", "html": "url", "pdf": "pdf", "note": "note",
    "search_result": "search_result", "docx": "doc", "xlsx": "doc",
    "csv": "doc", "markdown": "doc", "text": "doc", "other": "doc",
}
_MAX_ORIGINS = 50


def _migrate(conn: sqlite3.Connection) -> None:
    """Add Phase 2 columns to databases created by Phase 1 (idempotent)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(sources)").fetchall()}
    modified = False
    for column, ddl in _PHASE2_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE sources ADD COLUMN {column} {ddl}")
            modified = True
    if modified:
        conn.execute(
            """
            UPDATE sources SET source_type = CASE kind
                WHEN 'url' THEN 'web'
                WHEN 'pdf' THEN 'pdf'
                WHEN 'note' THEN 'note'
                ELSE 'other'
            END
            WHERE source_type = ''
            """
        )


def _add_columns(conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]) -> None:
    """Add missing columns to *table* (idempotent; used by migrations)."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for column, ddl in columns:
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migration_2_evidence_library(conn: sqlite3.Connection) -> None:
    """v2: evidence library tables + FTS5 passage index when available.

    DDL is idempotent so an interrupted run is safe to retry; the migration
    runner records the version only after every statement succeeded.
    """
    _add_columns(conn, "sources", _EVIDENCE_SOURCE_COLUMNS)
    conn.executescript(_EVIDENCE_SCHEMA)
    try:
        conn.executescript(_FTS_SCHEMA)
    except sqlite3.OperationalError as exc:
        # FTS5 is optional; passage search falls back to parameterized LIKE.
        LOGGER.warning("FTS5 unavailable (%s); passage search will use the LIKE fallback", exc)


# --- Phase 4 (research planner) ---------------------------------------------

_PLANNER_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_subquestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    question TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, question)
);

CREATE TABLE IF NOT EXISTS research_run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    subquestion_id INTEGER REFERENCES research_subquestions(id) ON DELETE SET NULL,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_subquestions_run ON research_subquestions(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON research_run_events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_run_events_kind ON research_run_events(run_id, kind);
"""


def _migration_3_research_planner(conn: sqlite3.Connection) -> None:
    """v3: run plans, subquestions, and the append-only execution log.

    The plan itself is a JSON column on ``research_runs``; subquestions and
    run events get their own tables. DDL is idempotent (interrupted
    migrations are retried on the next open).
    """
    _add_columns(conn, "research_runs", (("plan", "TEXT NOT NULL DEFAULT ''"),))
    conn.executescript(_PLANNER_SCHEMA)


# --- Phase 6 (verification and synthesis) -----------------------------------

_PHASE6_SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'source_text_comparison',
    confidence TEXT NOT NULL DEFAULT 'low',
    rationale TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '{}',
    checks TEXT NOT NULL DEFAULT '{}',
    limitations TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claim_verifications_claim
    ON claim_verifications(claim_id, id);
CREATE INDEX IF NOT EXISTS idx_claim_verifications_run
    ON claim_verifications(run_id, status);
"""

_CLAIM_PHASE6_COLUMNS = (
    ("claim_type", "TEXT NOT NULL DEFAULT 'claim'"),
    ("note", "TEXT NOT NULL DEFAULT ''"),
    ("verification_status", "TEXT NOT NULL DEFAULT ''"),
    ("verified_at", "TEXT"),
)

_REPORT_PHASE6_COLUMNS = (
    ("template_version", "TEXT NOT NULL DEFAULT 'phase3'"),
    ("finding_map", "TEXT NOT NULL DEFAULT '{}'"),
    ("image_map", "TEXT NOT NULL DEFAULT '{}'"),
    ("generation_config", "TEXT NOT NULL DEFAULT '{}'"),
)


def _migration_4_verification_and_synthesis(conn: sqlite3.Connection) -> None:
    """v4: claim verifications, claim classification, and report metadata.

    ``claim_verifications`` is an append-only audit trail (one row per
    verification pass per claim); ``claims.verification_status`` caches the
    latest assessment for rendering and is only ever written in the same
    transaction as the audit row, so the two cannot diverge. DDL is
    idempotent (interrupted migrations are retried on the next open).
    """
    conn.executescript(_PHASE6_SCHEMA)
    _add_columns(conn, "claims", _CLAIM_PHASE6_COLUMNS)
    _add_columns(conn, "reports", _REPORT_PHASE6_COLUMNS)


# --- Phase 7 (CLI run sessions and checkpoints) ------------------------------

_PHASE7_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    detail TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_run ON research_checkpoints(run_id, id);
CREATE INDEX IF NOT EXISTS idx_checkpoints_stage ON research_checkpoints(run_id, stage);
"""

_PHASE7_RUN_COLUMNS = (
    ("stage", "TEXT NOT NULL DEFAULT ''"),
    ("updated_at", "TEXT"),
)


def _migration_5_cli_run_sessions(conn: sqlite3.Connection) -> None:
    """v5: run-stage tracking plus append-only checkpoint history.

    ``research_runs.stage`` is the current (or last) pipeline stage and
    ``updated_at`` the last state change; ``research_checkpoints`` records one
    row per stage attempt (started/complete/failed/interrupted/skipped) so the
    CLI can resume without rerunning completed work and keep a retry history.
    Listings and stats tolerate both pre-v5 and post-v5 databases.
    """
    _add_columns(conn, "research_runs", _PHASE7_RUN_COLUMNS)
    conn.executescript(_PHASE7_SCHEMA)


# --- Phase 8 (monitoring and project memory) ---------------------------------

_MONITOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS monitor_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'url',
    target TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    config TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL REFERENCES monitor_targets(id) ON DELETE CASCADE,
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL,
    meaningful INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    etag TEXT,
    last_modified TEXT,
    content_hash TEXT,
    raw_hash TEXT,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    content_id INTEGER REFERENCES source_contents(id) ON DELETE SET NULL,
    previous_snapshot_id INTEGER REFERENCES monitor_snapshots(id) ON DELETE SET NULL,
    chars INTEGER,
    added_terms TEXT NOT NULL DEFAULT '[]',
    removed_terms TEXT NOT NULL DEFAULT '[]',
    ignored_boilerplate TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_monitor_targets_project ON monitor_targets(project, id);
CREATE INDEX IF NOT EXISTS idx_monitor_snapshots_target
    ON monitor_snapshots(target_id, checked_at);
CREATE INDEX IF NOT EXISTS idx_monitor_snapshots_status
    ON monitor_snapshots(target_id, status);
"""

_PROJECT_RUN_COLUMNS = (("project", "TEXT NOT NULL DEFAULT ''"),)


def _migration_6_monitoring_and_projects(conn: sqlite3.Connection) -> None:
    """v6: monitoring targets/snapshots plus the run project label.

    ``research_runs.project`` scopes cross-run retrieval and monitoring in one
    SQLite store (no second database). Snapshots reference the stored evidence
    (``source_id``/``content_id``) so a detected change stays traceable to the
    old and new passages. DDL is idempotent.
    """
    _add_columns(conn, "research_runs", _PROJECT_RUN_COLUMNS)
    conn.executescript(_MONITOR_SCHEMA)


# --- Phase 8 (named repeatable jobs) ------------------------------------------

_JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    question TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_run_id INTEGER REFERENCES research_runs(id) ON DELETE SET NULL,
    run_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS research_job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES research_jobs(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    requested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_runs_job ON research_job_runs(job_id, id);
"""


def _migration_7_jobs(conn: sqlite3.Connection) -> None:
    """v7: named repeatable research requests (job definitions + run history).

    A job stores the question plus the option *references* (profile name,
    project, title) it was created with -- never a resolved options snapshot,
    so re-running it always follows the current profile and branding. The
    append-only ``research_job_runs`` table links each manual run attempt to
    its research run; job removal cascades the history but never the runs.
    """
    conn.executescript(_JOBS_SCHEMA)


MIGRATIONS: tuple[tuple[int, str, Callable[[sqlite3.Connection], None]], ...] = (
    (2, "evidence library (runs, identities, contents, passages, claims, citations, reports)", _migration_2_evidence_library),
    (3, "research planner (plans, subquestions, execution log)", _migration_3_research_planner),
    (4, "claim verification and report synthesis (verifications, claim classification, report metadata)", _migration_4_verification_and_synthesis),
    (5, "CLI run sessions (stage tracking, append-only checkpoints)", _migration_5_cli_run_sessions),
    (6, "monitoring and project memory (targets, snapshots, run project label)", _migration_6_monitoring_and_projects),
    (7, "named repeatable jobs (job definitions and run history)", _migration_7_jobs),
)


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply pending migrations in order, recording each version when done.

    The baseline (v1) is the Phase 1/2 ``sources`` schema, created with
    ``CREATE TABLE IF NOT EXISTS`` above, so recording it is always safe.
    Migration DDL is written to be idempotent (``IF NOT EXISTS`` / guarded
    ``ALTER TABLE``): ``executescript`` commits before it runs, so an
    interrupted migration is simply retried on the next open — it can never
    half-apply a version, because the version row is inserted last.
    """
    applied = {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}
    if 1 not in applied:
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (1, ?, ?)",
            ("baseline source store (Phase 1/2)", _utc_now()),
        )
        conn.commit()
        applied.add(1)
    for version, name, migrate in MIGRATIONS:
        if version in applied:
            continue
        migrate(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, _utc_now()),
        )
        conn.commit()
        LOGGER.info("applied database migration %d: %s", version, name)


def content_hash(content: str) -> str:
    """SHA-256 hex digest of UTF-8 encoded *content*."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def open_db(settings: Settings) -> Iterator[sqlite3.Connection]:
    """Open the database, committing on success and rolling back on error.

    Foreign keys are enforced, WAL journaling keeps readers and writers from
    blocking each other, and a busy timeout lets concurrent tool calls wait
    for a short write lock instead of failing. Migrations run on every open
    (they are no-ops once applied).
    """
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(settings.db_path, timeout=10.0)
    except sqlite3.Error as exc:  # pragma: no cover - untypical filesystem failure
        raise ToolError("storage_error", f"Could not open research database: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error as exc:  # pragma: no cover - only exotic filesystems
            LOGGER.warning("could not enable WAL journaling: %s", exc)
        conn.executescript(_SCHEMA)
        _migrate(conn)
        _run_migrations(conn)
        yield conn
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        if isinstance(exc, sqlite3.IntegrityError):
            raise ToolError("conflict", f"Database constraint rejected the change: {exc}") from exc
        raise ToolError("storage_error", f"SQLite error: {exc}") from exc
    finally:
        conn.close()


# Back-compat private alias (Phase 1/2 internal call sites).
_open = open_db


_LEGACY_SOURCE_TYPE = {"url": "web", "pdf": "pdf", "note": "note", "search_result": "search_result"}


def _unified_fields(row: sqlite3.Row) -> dict[str, Any]:
    """Derive Phase 2 unified-record fields from a row (legacy-tolerant)."""
    source_type = row["source_type"] or _LEGACY_SOURCE_TYPE.get(row["kind"], "other")
    url = row["url"]
    metadata_value = row["metadata"] or ""
    metadata: dict[str, Any] = {}
    if metadata_value:
        try:
            parsed = json.loads(metadata_value)
            if isinstance(parsed, dict):
                # Phase 2 writes JSON objects; anything else is kept under a key.
                metadata = parsed
        except json.JSONDecodeError:
            metadata = {"raw": metadata_value}
    return {
        "source_type": source_type,
        "original_url": row["original_url"] if row["original_url"] is not None else url,
        "final_url": row["final_url"] if row["final_url"] is not None else url,
        "content_type": row["content_type"],
        "file_size_bytes": row["file_size_bytes"],
        "publication_date": row["publication_date"],
        "retrieved_at": row["retrieved_at"],
        "extraction_status": row["extraction_status"] or "success",
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "metadata": metadata,
        "checksum": row["sha256"],
    }


def _summary(row: sqlite3.Row) -> dict[str, Any]:
    unified = _unified_fields(row)
    return {
        "id": row["id"],
        "kind": row["kind"],
        **unified,
        "title": row["title"],
        "url": row["url"],
        "path": row["path"],
        "notes": row["notes"],
        "created_at": row["created_at"],
        "chars": len(row["content"] or ""),
        "excerpt": excerpt(row["content"] or ""),
    }


def _full(row: sqlite3.Row) -> dict[str, Any]:
    record = _summary(row)
    record["sha256"] = row["sha256"]
    record["content"] = row["content"]
    record["extracted_text"] = row["content"]
    record["normalized_sha256"] = row["normalized_sha256"]
    return record


def save_source(
    *,
    kind: str,
    title: str,
    content: str,
    url: str | None = None,
    path: str | None = None,
    notes: str = "",
    settings: Settings | None = None,
) -> tuple[int, bool]:
    """Store a source. Returns ``(source_id, created)``.

    Identical content (same SHA-256) is never stored twice; the existing id is
    returned with ``created=False``.
    """
    settings = settings or load_settings()
    kind = (kind or "").strip().lower()
    if kind not in SOURCE_KINDS:
        raise ToolError(
            "invalid_argument",
            f"kind must be one of {', '.join(SOURCE_KINDS)}; got {kind!r}",
        )
    content = content or ""
    if not content.strip():
        raise ToolError("invalid_argument", "content must not be empty")
    digest = content_hash(content)
    with _open(settings) as conn:
        existing = conn.execute(
            "SELECT id FROM sources WHERE sha256 = ? ORDER BY id LIMIT 1", (digest,)
        ).fetchone()
        if existing is not None:
            return int(existing["id"]), False
        cursor = conn.execute(
            """
            INSERT INTO sources (kind, title, url, path, content, notes, sha256, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (kind, (title or "").strip() or "(untitled)", url, path, content, notes, digest, _utc_now()),
        )
        return int(cursor.lastrowid), True


def _metadata_for_storage(record: SourceRecord, origin: str | None) -> dict[str, Any]:
    """Stored metadata: extraction metadata + the requested origin (if any)."""
    payload = dict(record.metadata or {})
    if origin:
        payload["origins"] = [{"requested": origin, "at": _utc_now()}]
    return payload


def save_record(
    record: SourceRecord,
    *,
    origin: str | None = None,
    notes: str = "",
    settings: Settings | None = None,
) -> tuple[int, bool, str]:
    """Persist a Phase 2 :class:`SourceRecord`.

    Returns ``(source_id, created, match_kind)`` where ``match_kind`` is
    ``"new"``, ``"exact"`` (same content checksum), or ``"near"`` (same
    normalized shape). Duplicates are not stored twice; if *origin* is given,
    the requested origin is appended to the surviving row's
    ``metadata.origins`` so provenance is retained. Rows with empty
    ``extracted_text`` raise ``invalid_argument`` (callers filter those).
    """
    settings = settings or load_settings()
    record.validate()
    content = record.extracted_text or ""
    if not content.strip():
        raise ToolError("invalid_argument", "refusing to save a source with empty extracted text")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    url = record.final_url or record.original_url or None
    params = (
        record.kind(),
        (record.title or "").strip() or "(untitled)",
        url,
        record.file_path,
        content,
        (notes or "").strip(),
        digest,
        _utc_now(),
        record.source_type,
        record.original_url,
        record.final_url,
        record.content_type,
        record.file_size_bytes,
        record.publication_date,
        record.retrieved_at or _utc_now(),
        record.extraction_status,
        record.error_code,
        record.error_message,
        json.dumps(
            _metadata_for_storage(record, origin),
            ensure_ascii=False,
            sort_keys=True,
        ),
        record.normalized_sha256,
    )
    with _open(settings) as conn:
        existing = conn.execute(
            "SELECT id FROM sources WHERE sha256 = ? ORDER BY id LIMIT 1", (digest,)
        ).fetchone()
        match_kind = "exact" if existing is not None else "new"
        if existing is None and record.normalized_sha256:
            existing = conn.execute(
                "SELECT id FROM sources WHERE normalized_sha256 = ? ORDER BY id LIMIT 1",
                (record.normalized_sha256,),
            ).fetchone()
            if existing is not None:
                match_kind = "near"
        if existing is not None:
            source_id = int(existing["id"])
            if origin:
                _append_origin(conn, source_id, origin)
            return source_id, False, match_kind
        cursor = conn.execute(
            """
            INSERT INTO sources (
                kind, title, url, path, content, notes, sha256, created_at,
                source_type, original_url, final_url, content_type,
                file_size_bytes, publication_date, retrieved_at,
                extraction_status, error_code, error_message, metadata,
                normalized_sha256
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
        return int(cursor.lastrowid), True, "new"


def _append_origin(conn: sqlite3.Connection, source_id: int, origin: str) -> None:
    """Record that *origin* was requested again and matched this source."""
    row = conn.execute("SELECT metadata FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        return
    try:
        payload = json.loads(row["metadata"] or "")
        payload = payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        payload = {"raw": row["metadata"]} if row["metadata"] else {}
    origins = payload.setdefault("origins", [])
    entry = {"requested": origin, "at": _utc_now()}
    if entry not in origins and len(origins) < _MAX_ORIGINS:
        origins.append(entry)
    payload["duplicate_origins"] = len(origins) - 1
    conn.execute(
        "UPDATE sources SET metadata = ? WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False, sort_keys=True), source_id),
    )


def append_origin(source_id: int, origin: str, *, settings: Settings | None = None) -> None:
    """Public wrapper: attach duplicate-collection provenance to a stored row."""
    settings = settings or load_settings()
    with _open(settings) as conn:
        _append_origin(conn, source_id, origin)


def set_source_title(source_id: int, title: str, *, settings: Settings | None = None) -> bool:
    """Rename one source (used by extract_document's title argument)."""
    settings = settings or load_settings()
    clean = (title or "").strip()
    if not clean:
        return False
    with _open(settings) as conn:
        cursor = conn.execute(
            "UPDATE sources SET title = ? WHERE id = ?", (clean, int(source_id))
        )
    return cursor.rowcount > 0


def find_existing_hashes(
    hashes: set[str], *, settings: Settings | None = None
) -> dict[str, int]:
    """Map any known sha256/normalized_sha256 values to their source ids."""
    settings = settings or load_settings()
    mapping: dict[str, int] = {}
    hs = sorted(h for h in hashes if h)
    for start in range(0, len(hs), 100):  # bounded chunking for large jobs
        chunk = hs[start : start + 100]
        marks = ",".join("?" * len(chunk))
        with _open(settings) as conn:
            rows = conn.execute(
                f"""
                SELECT id, sha256, normalized_sha256 FROM sources
                WHERE sha256 IN ({marks}) OR normalized_sha256 IN ({marks})
                """,
                (*chunk, *chunk),
            ).fetchall()
        for row in rows:
            mapping[row["sha256"]] = int(row["id"])
            if row["normalized_sha256"]:
                mapping[row["normalized_sha256"]] = int(row["id"])
    return mapping


def set_source_metadata(
    source_id: int, metadata: dict[str, Any], *, settings: Settings | None = None
) -> None:
    """Replace the stored metadata JSON of one source."""
    settings = settings or load_settings()
    with _open(settings) as conn:
        conn.execute(
            "UPDATE sources SET metadata = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True), int(source_id)),
        )


def get_source(
    source_id: int, *, include_content: bool = True, settings: Settings | None = None
) -> dict[str, Any] | None:
    """Return one source record, or ``None`` when the id is unknown."""
    settings = settings or load_settings()
    with _open(settings) as conn:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (int(source_id),)).fetchone()
    if row is None:
        return None
    return _full(row) if include_content else _summary(row)


def list_sources(
    *,
    limit: int | None = None,
    offset: int = 0,
    kind: str | None = None,
    source_type: str | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """List stored sources, newest first (optional kind/source_type filter)."""
    settings = settings or load_settings()
    limit = min(max(int(limit or settings.default_list_limit), 1), settings.max_list_limit)
    offset = max(int(offset), 0)
    sql = "SELECT * FROM sources"
    params: list[Any] = []
    clauses: list[str] = []
    kind_value = (kind or "").strip().lower()
    if kind_value:
        if kind_value not in SOURCE_KINDS:
            raise ToolError(
                "invalid_argument",
                f"kind filter must be one of {', '.join(SOURCE_KINDS)}; got {kind_value!r}",
            )
        clauses.append("kind = ?")
        params.append(kind_value)
    type_value = (source_type or "").strip().lower()
    if type_value:
        clauses.append("(source_type = ? OR (source_type = '' AND kind = ?))")
        params.extend([type_value, _SOURCE_TYPE_TO_KIND_FILTER.get(type_value, "")])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with _open(settings) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_summary(row) for row in rows]


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_sources(
    query: str, *, limit: int | None = None, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Keyword search over title, url, notes, and content (case-insensitive)."""
    settings = settings or load_settings()
    query = (query or "").strip()
    if not query:
        raise ToolError("invalid_argument", "query must not be empty")
    limit = min(max(int(limit or settings.default_list_limit), 1), settings.max_list_limit)
    pattern = _like_pattern(query)
    with _open(settings) as conn:
        rows = conn.execute(
            """
            SELECT * FROM sources
            WHERE title LIKE ? ESCAPE '\\'
               OR IFNULL(url, '') LIKE ? ESCAPE '\\'
               OR notes LIKE ? ESCAPE '\\'
               OR content LIKE ? ESCAPE '\\'
            ORDER BY id DESC LIMIT ?
            """,
            (pattern, pattern, pattern, pattern, limit),
        ).fetchall()
    return [_summary(row) for row in rows]


def ping(settings: Settings | None = None) -> dict[str, Any]:
    """Cheap read-only liveness probe of the research store. Never raises."""
    settings = settings or load_settings()
    try:
        with _open(settings) as conn:
            total = int(conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"])
        size = settings.db_path.stat().st_size if settings.db_path.exists() else 0
        return {
            "ok": True,
            "db_path": str(settings.db_path),
            "total": total,
            "db_size_bytes": size,
        }
    except Exception as exc:  # noqa: BLE001 - health probes must never raise
        LOGGER.warning("storage ping failed: %s", exc)
        return {
            "ok": False,
            "db_path": str(settings.db_path),
            "error": f"{exc.__class__.__name__}: {exc}",
        }


def stats(settings: Settings | None = None) -> dict[str, Any]:
    """Counts and storage location for the research database."""
    settings = settings or load_settings()
    with _open(settings) as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
        by_kind = {
            row["kind"]: row["n"]
            for row in conn.execute(
                "SELECT kind, COUNT(*) AS n FROM sources GROUP BY kind ORDER BY kind"
            ).fetchall()
        }
        evidence = {
            name: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
            for name, table in (
                ("research_runs", "research_runs"),
                ("source_contents", "source_contents"),
                ("source_identities", "source_identities"),
                ("passages", "passages"),
                ("claims", "claims"),
                ("citations", "citations"),
                ("reports", "reports"),
                ("research_subquestions", "research_subquestions"),
                ("research_run_events", "research_run_events"),
                ("claim_verifications", "claim_verifications"),
                ("research_checkpoints", "research_checkpoints"),
            )
        }
        fts_row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'passages_fts'"
        ).fetchone()
        evidence["fts5"] = fts_row is not None
        schema_version = int(
            conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()["v"] or 0
        )
    size = settings.db_path.stat().st_size if settings.db_path.exists() else 0
    return {
        "db_path": str(settings.db_path),
        "schema_version": schema_version,
        "total": int(total),
        "by_kind": by_kind,
        "evidence": evidence,
        "db_size_bytes": size,
    }

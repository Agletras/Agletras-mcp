"""Phase 8: named, repeatable research requests ("jobs").

A job stores a question plus the option *references* it repeats with: an
optional config-file profile name, a project label, a title, and notes. Jobs
never store resolved option snapshots, so re-running one always follows the
*current* profile and branding; a profile that disappeared since the job was
created fails the run loudly instead of silently using stale options.

Running a job is always an explicit, manual action (``agletras jobs run NAME``);
nothing here schedules or triggers runs on its own. The append-only
``research_job_runs`` history links every attempt to its research run, and
removing a job keeps those runs (only the job's own history rows cascade).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import storage
from .config import Settings, load_settings
from .errors import ToolError

#: Bounds sized for CLI/agent use (names are typed by hand).
MAX_NAME_CHARS = 80
MAX_QUESTION_CHARS = 2000
MAX_NOTES_CHARS = 2000


def _utc_now() -> str:
    return storage._utc_now()


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _require_name(name: Any) -> str:
    clean = _clean(name)
    if not clean:
        raise ToolError("invalid_argument", "job name must not be empty")
    if len(clean) > MAX_NAME_CHARS:
        raise ToolError(
            "invalid_argument", f"job name is {len(clean)} characters; the limit is {MAX_NAME_CHARS}"
        )
    return clean


def _require_question(question: Any) -> str:
    clean = _clean(question)
    if not clean:
        raise ToolError("invalid_argument", "job question must not be empty")
    if len(clean) > MAX_QUESTION_CHARS:
        raise ToolError(
            "invalid_argument",
            f"job question is {len(clean)} characters; the limit is {MAX_QUESTION_CHARS}",
        )
    return clean


def _runs_for(conn: sqlite3.Connection, job_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT rjr.run_id, rjr.requested_at, r.status, r.query
        FROM research_job_runs rjr JOIN research_runs r ON r.id = rjr.run_id
        WHERE rjr.job_id = ? ORDER BY rjr.id DESC LIMIT ?
        """,
        (int(job_id), int(limit)),
    ).fetchall()
    return [
        {
            "run_id": int(row["run_id"]),
            "requested_at": row["requested_at"],
            "status": row["status"],
            "query": row["query"],
        }
        for row in rows
    ]


def _job_payload(row: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "job_id": int(row["id"]),
        "name": row["name"],
        "question": row["question"],
        "profile": row["profile"] or "",
        "project": row["project"] or "",
        "title": row["title"] or "",
        "notes": row["notes"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_run_id": int(row["last_run_id"]) if row["last_run_id"] is not None else None,
        "run_count": int(row["run_count"]),
        "runs": _runs_for(conn, int(row["id"])),
    }


def _resolve(conn: sqlite3.Connection, identifier: Any) -> sqlite3.Row | None:
    text = str(identifier if identifier is not None else "").strip()
    if not text:
        return None
    if text.isdigit():
        return conn.execute("SELECT * FROM research_jobs WHERE id = ?", (int(text),)).fetchone()
    return conn.execute("SELECT * FROM research_jobs WHERE name = ?", (text,)).fetchone()


def add_job(
    name: str,
    question: str,
    *,
    profile: str = "",
    project: str = "",
    title: str = "",
    notes: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Create a named job; duplicate names (case-insensitive) are a conflict."""
    settings = settings or load_settings()
    clean_name = _require_name(name)
    clean_question = _require_question(question)
    clean_notes = _clean(notes)
    if len(clean_notes) > MAX_NOTES_CHARS:
        raise ToolError(
            "invalid_argument", f"job notes exceed {MAX_NOTES_CHARS} characters"
        )
    now = _utc_now()
    with storage.open_db(settings) as conn:
        existing = _resolve(conn, clean_name)
        if existing is not None:
            raise ToolError(
                "conflict",
                f"a job named {clean_name!r} already exists (job {int(existing['id'])}); "
                "remove it first or pick another name",
            )
        cursor = conn.execute(
            """
            INSERT INTO research_jobs
                (name, question, profile, project, title, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clean_name,
                clean_question,
                _clean(profile),
                _clean(project),
                _clean(title),
                clean_notes,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM research_jobs WHERE id = ?", (int(cursor.lastrowid),)
        ).fetchone()
        return _job_payload(row, conn)


def list_jobs(*, settings: Settings | None = None) -> dict[str, Any]:
    """All jobs, oldest first, each with its recent run history."""
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        rows = conn.execute("SELECT * FROM research_jobs ORDER BY id").fetchall()
        jobs = [_job_payload(row, conn) for row in rows]
    return {"count": len(jobs), "jobs": jobs}


def get_job(identifier: Any, *, settings: Settings | None = None) -> dict[str, Any] | None:
    """One job by name (case-insensitive) or numeric id, or ``None``."""
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        row = _resolve(conn, identifier)
        return _job_payload(row, conn) if row is not None else None


def remove_job(identifier: Any, *, settings: Settings | None = None) -> dict[str, Any]:
    """Delete a job and its run history; the research runs themselves stay."""
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        row = _resolve(conn, identifier)
        if row is None:
            raise ToolError("not_found", f"no job named {identifier!r}")
        payload = _job_payload(row, conn)
        conn.execute("DELETE FROM research_jobs WHERE id = ?", (int(row["id"]),))
    return {"removed": True, "job": payload}


def record_run(
    job_id: int, run_id: int, *, settings: Settings | None = None
) -> dict[str, Any]:
    """Append one manual run attempt to a job's history."""
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        row = conn.execute("SELECT * FROM research_jobs WHERE id = ?", (int(job_id),)).fetchone()
        if row is None:
            raise ToolError("not_found", f"no job with id {job_id}")
        conn.execute(
            "INSERT INTO research_job_runs (job_id, run_id, requested_at) VALUES (?, ?, ?)",
            (int(job_id), int(run_id), _utc_now()),
        )
        conn.execute(
            """
            UPDATE research_jobs
            SET last_run_id = ?, run_count = run_count + 1, updated_at = ?
            WHERE id = ?
            """,
            (int(run_id), _utc_now(), int(job_id)),
        )
        updated = conn.execute(
            "SELECT * FROM research_jobs WHERE id = ?", (int(job_id),)
        ).fetchone()
        return _job_payload(updated, conn)

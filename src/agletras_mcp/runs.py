"""Phase 7 run sessions: stages, checkpoints, statuses, and resume state.

The Phase 3–6 tools already persist runs, evidence, claims, citations, and
reports in the same SQLite database. Phase 7 adds what the CLI needs to run a
research request *without a model in the loop* and survive interruptions:

- ``research_runs.stage`` / ``research_runs.updated_at``: where the pipeline is.
- ``research_checkpoints``: an append-only row per stage attempt recording
  status (``started``/``complete``/``failed``/``interrupted``/``skipped``),
  attempt number, JSON detail, error, and timing. One checkpoint is one
  transaction, so a crash can never leave a half-written checkpoint.
- :func:`resume_assessment`: whether a run can be resumed, why not when it
  cannot, and the stage states already completed.

Run statuses are extended beyond the tool-level ``running``/``completed``/
``failed`` with ``partial`` (report produced, gaps remain), ``cancelled``
(stop requested), and ``interrupted`` (process died / Ctrl+C). The MCP tools
never write the new values; only the CLI run session does.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import storage
from .config import Settings, load_settings, settings_snapshot
from .errors import ToolError

#: Every run status understood by the CLI (the MCP tools write only the first
#: three; the rest are written by the run session).
RUN_STATUSES = ("running", "completed", "partial", "failed", "cancelled", "interrupted")
TERMINAL_RUN_STATUSES = frozenset(("completed", "partial", "failed", "cancelled", "interrupted"))
#: Checkpoint statuses (append-only history; ``skipped`` marks an
#: intentionally unused optional stage).
CHECKPOINT_STATUSES = ("started", "complete", "failed", "interrupted", "skipped")
#: Statuses that can be resumed without ``force``.
RESUMABLE_STATUSES = frozenset(("running", "interrupted", "cancelled"))

_SECRET_HINTS = ("api_key", "apikey", "secret", "token", "password", "credential")

#: Run-config keys that may legitimately change between a run and its resume;
#: everything else in ``options`` must match for a resume to be safe.
INCOMPATIBLE_KEYS = (
    "depth",
    "max_sources",
    "max_claims",
    "searches_per_subquestion",
    "followup_rounds",
    "results_per_search",
    "verification_method",
    "include_images",
    "subquestions",
)
#: Settings values that change where evidence comes from; a resume with a
#: different provider/endpoint would silently mix sources.
INCOMPATIBLE_SETTINGS = ("search_provider", "search_endpoint", "allow_private_hosts")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _require_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM research_runs WHERE id = ?", (int(run_id),)).fetchone()
    if row is None:
        raise ToolError(
            "not_found",
            f"No research run with id {run_id}; list runs with `agletras list`.",
        )
    return row


def run_payload(row: sqlite3.Row) -> dict[str, Any]:
    """Run fields plus the Phase 7 stage/updated_at (tolerant of pre-v5 rows)."""
    keys = set(row.keys())
    return {
        "run_id": int(row["id"]),
        "query": row["query"],
        "status": row["status"],
        "stage": row["stage"] if "stage" in keys else "",
        "project": (row["project"] or "") if "project" in keys else "",
        "config": _load_object(row["config"]),
        "started_at": row["started_at"],
        "updated_at": row["updated_at"] if "updated_at" in keys else None,
        "completed_at": row["completed_at"],
        "error": row["error"],
    }


# ---------------------------------------------------------------------------
# Run status and stage
# ---------------------------------------------------------------------------


def set_stage(run_id: int, stage: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Record the pipeline stage the run is currently executing."""
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        _require_run(conn, run_id)
        conn.execute(
            "UPDATE research_runs SET stage = ?, updated_at = ? WHERE id = ?",
            (str(stage), _utc_now(), int(run_id)),
        )
        return run_payload(_require_run(conn, run_id))


def set_run_status(
    run_id: int,
    status: str,
    *,
    error: str | None = None,
    stage: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Set a run status (any value in :data:`RUN_STATUSES`).

    ``completed_at`` is written for every terminal status and cleared when a
    terminal run is explicitly reopened (``force`` resume). Each call is one
    transaction. The tool-level ``evidence.finish_run`` is intentionally
    untouched so MCP behavior does not change.
    """
    settings = settings or load_settings()
    clean = (status or "").strip().lower()
    if clean not in RUN_STATUSES:
        raise ToolError("invalid_argument", f"status must be one of {', '.join(RUN_STATUSES)}; got {status!r}")
    with storage.open_db(settings) as conn:
        _require_run(conn, run_id)
        completed_at = _utc_now() if clean in TERMINAL_RUN_STATUSES else None
        if stage is not None:
            conn.execute(
                """
                UPDATE research_runs
                SET status = ?, error = ?, completed_at = ?, updated_at = ?, stage = ?
                WHERE id = ?
                """,
                (clean, error, completed_at, _utc_now(), str(stage), int(run_id)),
            )
        else:
            conn.execute(
                """
                UPDATE research_runs
                SET status = ?, error = ?, completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (clean, error, completed_at, _utc_now(), int(run_id)),
            )
        return run_payload(_require_run(conn, run_id))


def get_run_state(run_id: int, *, settings: Settings | None = None) -> dict[str, Any] | None:
    """Run payload for one id, or ``None`` when it does not exist."""
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        row = conn.execute("SELECT * FROM research_runs WHERE id = ?", (int(run_id),)).fetchone()
    return run_payload(row) if row is not None else None


def list_runs(
    *,
    limit: int = 20,
    status: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Runs, newest first, with evidence/claim/report counts (for ``agletras list``)."""
    settings = settings or load_settings()
    bounded = min(max(int(limit), 1), settings.max_list_limit)
    where = ""
    params: list[Any] = []
    if status:
        clean = (status or "").strip().lower()
        if clean not in RUN_STATUSES:
            raise ToolError("invalid_argument", f"status must be one of {', '.join(RUN_STATUSES)}; got {status!r}")
        where = "WHERE r.status = ?"
        params.append(clean)
    with storage.open_db(settings) as conn:
        rows = conn.execute(
            f"""
            SELECT r.*,
                (SELECT COUNT(*) FROM research_run_sources x WHERE x.run_id = r.id) AS sources_count,
                (SELECT COUNT(*) FROM claims c WHERE c.run_id = r.id) AS claims_count,
                (SELECT COUNT(*) FROM reports rp WHERE rp.run_id = r.id) AS reports_count
            FROM research_runs r
            {where}
            ORDER BY r.id DESC LIMIT ?
            """,
            (*params, bounded),
        ).fetchall()
    listed = [
        {
            **run_payload(row),
            "sources_count": int(row["sources_count"]),
            "claims_count": int(row["claims_count"]),
            "reports_count": int(row["reports_count"]),
        }
        for row in rows
    ]
    return {"count": len(listed), "runs": listed}


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------


def _checkpoint_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "checkpoint_id": int(row["id"]),
        "run_id": int(row["run_id"]),
        "stage": row["stage"],
        "status": row["status"],
        "attempt": int(row["attempt"]),
        "detail": _load_object(row["detail"]),
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "duration_ms": row["duration_ms"],
        "created_at": row["created_at"],
    }


def record_checkpoint(
    run_id: int,
    stage: str,
    status: str,
    *,
    detail: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    attempt: int | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_ms: int | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Append one checkpoint row (single transaction; atomic by construction).

    When *attempt* is omitted it is derived from the existing rows for the
    stage (start count + 1), so retry history stays ordered even if a caller
    never reads the previous rows.
    """
    settings = settings or load_settings()
    clean_status = (status or "").strip().lower()
    if clean_status not in CHECKPOINT_STATUSES:
        raise ToolError(
            "invalid_argument",
            f"checkpoint status must be one of {', '.join(CHECKPOINT_STATUSES)}; got {status!r}",
        )
    clean_stage = (stage or "").strip()
    if not clean_stage:
        raise ToolError("invalid_argument", "stage must not be empty")
    now = _utc_now()
    with storage.open_db(settings) as conn:
        _require_run(conn, run_id)
        if attempt is None:
            row = conn.execute(
                "SELECT MAX(attempt) AS n FROM research_checkpoints WHERE run_id = ? AND stage = ?",
                (int(run_id), clean_stage),
            ).fetchone()
            attempt = int(row["n"] or 0) + 1
        cursor = conn.execute(
            """
            INSERT INTO research_checkpoints (
                run_id, stage, status, attempt, detail, error_code, error_message,
                started_at, finished_at, duration_ms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(run_id),
                clean_stage,
                clean_status,
                int(attempt),
                _dumps(dict(detail or {})),
                error_code,
                error_message,
                started_at or (now if clean_status == "started" else None),
                finished_at or (now if clean_status != "started" else None),
                duration_ms,
                now,
            ),
        )
        checkpoint = conn.execute(
            "SELECT * FROM research_checkpoints WHERE id = ?", (int(cursor.lastrowid),)
        ).fetchone()
        conn.execute(
            "UPDATE research_runs SET updated_at = ?, stage = ? WHERE id = ?",
            (now, clean_stage, int(run_id)),
        )
        return _checkpoint_payload(checkpoint)


def start_checkpoint(
    run_id: int, stage: str, *, detail: dict[str, Any] | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """Record the start of one stage attempt and mark the run's current stage."""
    return record_checkpoint(run_id, stage, "started", detail=detail, settings=settings)


def finish_checkpoint(
    run_id: int,
    stage: str,
    status: str,
    *,
    detail: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    attempt: int | None = None,
    duration_ms: int | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Record the terminal state of one stage attempt."""
    return record_checkpoint(
        run_id,
        stage,
        status,
        detail=detail,
        error_code=error_code,
        error_message=error_message,
        attempt=attempt,
        duration_ms=duration_ms,
        settings=settings,
    )


def checkpoints(run_id: int, *, limit: int = 500, settings: Settings | None = None) -> list[dict[str, Any]]:
    """All checkpoint rows for a run, oldest first (bounded by *limit*)."""
    settings = settings or load_settings()
    bounded = min(max(int(limit), 1), 5000)
    with storage.open_db(settings) as conn:
        _require_run(conn, run_id)
        rows = conn.execute(
            "SELECT * FROM research_checkpoints WHERE run_id = ? ORDER BY id LIMIT ?",
            (int(run_id), bounded),
        ).fetchall()
    return [_checkpoint_payload(row) for row in rows]


def stage_states(run_id: int, *, settings: Settings | None = None) -> dict[str, dict[str, Any]]:
    """Latest checkpoint per stage for a run: ``{stage: checkpoint}``."""
    latest: dict[str, dict[str, Any]] = {}
    for checkpoint in checkpoints(run_id, settings=settings):
        latest[checkpoint["stage"]] = checkpoint
    return latest


def stage_status(run_id: int, stage: str, *, settings: Settings | None = None) -> str | None:
    """Latest checkpoint status for one stage, or ``None`` if never attempted."""
    state = stage_states(run_id, settings=settings).get(stage)
    return state["status"] if state else None


# ---------------------------------------------------------------------------
# Resume assessment
# ---------------------------------------------------------------------------


def _age_days(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        when = datetime.fromisoformat(str(timestamp))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400.0


def resume_assessment(
    run_id: int,
    *,
    settings: Settings | None = None,
    max_age_days: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Can this run be resumed? Returns a structured, explainable answer.

    Not resumable: unknown id; a run managed by the MCP tools (no CLI
    checkpoints); a closed run (``completed``/``partial``) — even with
    ``force``, because re-running a finished report silently would be
    dishonest; a ``failed`` run without ``force``; a stale run beyond
    ``research.resume_max_age_days`` without ``force``.
    """
    settings = settings or load_settings()
    if max_age_days is None:
        max_age_days = settings.research_resume_max_age_days
    with storage.open_db(settings) as conn:
        row = conn.execute("SELECT * FROM research_runs WHERE id = ?", (int(run_id),)).fetchone()
    if row is None:
        return {
            "resumable": False,
            "reason": "not_found",
            "detail": f"No research run with id {run_id}.",
        }
    run = run_payload(row)
    state = stage_states(run_id, settings=settings)
    config = run["config"]
    cli_managed = isinstance(config, dict) and config.get("source") == "cli"
    if not state and not cli_managed:
        return {
            "resumable": False,
            "reason": "not_cli_managed",
            "detail": (
                "This run was created through the MCP tools (OpenCode/agent workflow), "
                "not by `agletras run`, so there are no CLI checkpoints to resume. "
                "Continue it with the research-orchestrator agent, or start a new "
                "`agletras run` for the same question."
            ),
            "run": run,
        }
    status = run["status"]
    if status in ("completed", "partial"):
        return {
            "resumable": False,
            "reason": "already_closed",
            "detail": (
                f"Run {run_id} is already {status} and has its report. Inspect it with "
                f"`agletras report {run_id}` or start a new run with `agletras run \"...\"`."
            ),
            "run": run,
        }
    if status == "failed" and not force:
        return {
            "resumable": False,
            "reason": "failed",
            "detail": (
                f"Run {run_id} failed: {run['error'] or 'no error recorded'}. "
                f"Resume it with `agletras resume {run_id} --force`, or start a new run."
            ),
            "run": run,
        }
    last_activity = run["updated_at"] or run["started_at"]
    if max_age_days and max_age_days > 0 and not force:
        age = _age_days(last_activity)
        if age is not None and age > max_age_days:
            return {
                "resumable": False,
                "reason": "stale",
                "detail": (
                    f"The run's last checkpoint is {age:.1f} days old "
                    f"(limit: research.resume_max_age_days={max_age_days}). "
                    f"Resume anyway with `agletras resume {run_id} --force`, or start a new run."
                ),
                "run": run,
                "last_activity": last_activity,
            }
    pause = next(
        (checkpoint for checkpoint in reversed(list(state.values())) if checkpoint["status"] == "started"),
        None,
    )
    return {
        "resumable": True,
        "reason": "ok",
        "detail": (
            f"Run {run_id} is {status}; resuming from the first incomplete stage "
            "(completed stages are skipped)."
        ),
        "run": run,
        "stage": run["stage"],
        "stages": state,
        "paused_stage": pause["stage"] if pause else None,
        "last_activity": last_activity,
    }


# ---------------------------------------------------------------------------
# Run config snapshots and resume compatibility
# ---------------------------------------------------------------------------


def redact_secrets(value: Any, *, key: str = "") -> Any:
    """Recursively mask values whose key looks like a credential."""
    lowered = key.lower()
    if any(hint in lowered for hint in _SECRET_HINTS):
        return "***redacted***"
    if isinstance(value, dict):
        return {str(k): redact_secrets(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    return value


def build_run_config(options: dict[str, Any], settings: Settings, *, tool_version: str) -> dict[str, Any]:
    """The redacted config snapshot persisted on a CLI research run."""
    snapshot = {
        "source": "cli",
        "tool_version": tool_version,
        "options": {key: redact_secrets(value, key=key) for key, value in options.items()},
        "settings": settings_snapshot(settings),
    }
    return snapshot


def config_differences(stored: dict[str, Any], options: dict[str, Any], settings: Settings) -> list[str]:
    """Human-readable differences that make a resume incompatible."""
    differences: list[str] = []
    stored_options = stored.get("options") if isinstance(stored.get("options"), dict) else {}
    for key in INCOMPATIBLE_KEYS:
        before = stored_options.get(key)
        after = options.get(key)
        if before != after:
            differences.append(f"options.{key}: {before!r} -> {after!r}")
    stored_settings = stored.get("settings") if isinstance(stored.get("settings"), dict) else {}
    for key in INCOMPATIBLE_SETTINGS:
        before = stored_settings.get(key)
        after = getattr(settings, key, None)
        if isinstance(after, tuple):
            after = list(after)
        if before is not None and before != after:
            differences.append(f"settings.{key}: {before!r} -> {after!r}")
    return differences


def run_report_paths(run_id: int, *, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Stored reports for a run (id, title, path, template, validation)."""
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        _require_run(conn, run_id)
        rows = conn.execute(
            """
            SELECT id, title, path, format, template_version, citation_validation_status,
                   generated_at, validated_at
            FROM reports WHERE run_id = ? ORDER BY id
            """,
            (int(run_id),),
        ).fetchall()
    return [
        {
            "report_id": int(row["id"]),
            "title": row["title"],
            "path": row["path"],
            "format": row["format"],
            "template_version": row["template_version"],
            "citation_validation_status": row["citation_validation_status"],
            "generated_at": row["generated_at"],
            "validated_at": row["validated_at"],
        }
        for row in rows
    ]

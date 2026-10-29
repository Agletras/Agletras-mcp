"""Research planner and orchestrator support (Phase 4).

The OpenCode ``research-orchestrator`` agent makes the research decisions;
this module enforces the deterministic invariants and keeps the audit trail
in the same SQLite database as the Phase 1-3 stores:

- **Plans.** One plan per research run: objective, scope, assumptions,
  per-run limits, and non-overlapping subquestions with priorities and
  evaluation statuses (``pending`` -> ``answered`` / ``partially_answered`` /
  ``unanswered`` / ``conflicting``).
- **Limits.** Defaults: at most 8 subquestions, 2 follow-up rounds, 4
  candidate sources per subquestion per round, and 20 unique sources per
  research run. Callers may override them per run through
  :func:`save_plan`; values are clamped to hard ceilings and enforced by
  the tools — the subquestion cap at plan time, the follow-up round cap at
  round start, the candidate cap at event-log time, and the unique-source
  cap inside ``save_evidence`` (see :func:`check_source_capacity`).
- **Execution log.** ``research_run_events`` is an append-only log of tool
  actions, queries, sources saved or failed, evidence, claims, citations,
  gaps, follow-up rounds, errors, and the stopping reason. Secrets are never
  meant to be logged here; callers log descriptors (URLs, queries, ids).
- **Budget and stopping.** :func:`describe_run` reports how much of every
  limit remains and whether the workflow must stop — all subquestions
  answered, or a limit reached with gaps remaining — so the orchestrator
  reports gaps honestly instead of continuing indefinitely.

Nothing here calls a model, invents sources, or decides truth: the planner
records and bounds what the orchestrator does.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from . import storage
from .config import Settings, load_settings
from .errors import ToolError
from .source_record import utc_now_iso

#: Per-run defaults, applied when a plan does not override them.
DEFAULT_LIMITS: dict[str, int] = {
    "max_subquestions": 8,
    "max_followup_rounds": 2,
    "max_candidates_per_subquestion": 4,
    "max_unique_sources": 20,
}

#: Hard ceilings: overrides are clamped down to these values.
LIMIT_CEILINGS: dict[str, int] = {
    "max_subquestions": 16,
    "max_followup_rounds": 5,
    "max_candidates_per_subquestion": 20,
    "max_unique_sources": 100,
}

SUBQUESTION_STATUSES = (
    "pending",
    "answered",
    "partially_answered",
    "unanswered",
    "conflicting",
)
#: Subquestion statuses that still require evidence work.
GAP_STATUSES = ("pending", "partially_answered", "unanswered", "conflicting")

#: Documented event kinds (validated, so the log stays analyzable). The
#: ``followup_round`` kind is only written by :func:`start_followup_round`
#: so the round cap cannot be bypassed by logging.
EVENT_KINDS = (
    "plan",
    "search",
    "source_candidate",
    "source_saved",
    "source_failed",
    "evidence",
    "claim",
    "citation",
    "gap",
    "followup_round",
    "decision",
    "error",
    "stopping",
    "report",
    "note",
)

DEFAULT_EVENT_LIMIT = 100

_MAX_OBJECTIVE_CHARS = 2000
_MAX_SCOPE_CHARS = 4000
_MAX_ASSUMPTIONS = 20
_MAX_ASSUMPTION_CHARS = 500
_MAX_QUESTION_CHARS = 500
_MAX_MESSAGE_CHARS = 2000
_MAX_NOTE_CHARS = 4000
_MAX_PAYLOAD_CHARS = 8000

_LIST_MARKER = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return utc_now_iso()


def _normalize_ws(text: str) -> str:
    return " ".join((text or "").split())


def _load_object(raw: str | None) -> dict[str, Any]:
    """Parse a stored JSON object, never raising on malformed data."""
    try:
        parsed = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _require_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM research_runs WHERE id = ?", (int(run_id),)).fetchone()
    if row is None:
        raise ToolError(
            "not_found",
            f"No research run with id {run_id}; create one with start_research_run first.",
        )
    return row


# ---------------------------------------------------------------------------
# Parsing and validation (pure, unit-testable)
# ---------------------------------------------------------------------------


def parse_subquestions(raw: str | list[Any]) -> list[dict[str, Any]]:
    """Parse subquestions from text (one per line) or a JSON array.

    Text lines may carry ``-``/``*``/``1.``/``1)`` list markers; JSON items
    may be plain strings or objects with ``question``/``text`` and an
    optional positive ``priority`` (1 = highest; defaults to input order).
    Blank lines are skipped and duplicates (case-insensitive) collapse to
    their first occurrence.
    """
    items: list[Any]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ToolError("invalid_argument", f"subquestions JSON is invalid: {exc}") from exc
            if not isinstance(loaded, list):
                raise ToolError("invalid_argument", "subquestions JSON must be an array")
            items = loaded
        else:
            items = text.splitlines()
    elif isinstance(raw, list):
        items = raw
    else:
        raise ToolError("invalid_argument", "subquestions must be text or a JSON array")

    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        priority = len(parsed) + 1
        if isinstance(item, dict):
            question = str(item.get("question") or item.get("text") or "")
            raw_priority = item.get("priority")
            if isinstance(raw_priority, int) and not isinstance(raw_priority, bool) and raw_priority > 0:
                priority = raw_priority
        elif isinstance(item, str):
            question = item
        else:
            raise ToolError(
                "invalid_argument", f"subquestion #{index} must be text or an object"
            )
        question = _normalize_ws(_LIST_MARKER.sub("", question.strip()))
        if not question:
            continue
        if len(question) > _MAX_QUESTION_CHARS:
            raise ToolError(
                "invalid_argument",
                f"subquestion #{index} is {len(question)} characters; the limit is "
                f"{_MAX_QUESTION_CHARS}. Split it into separate subquestions.",
            )
        key = question.lower()
        if key in seen:
            continue
        seen.add(key)
        parsed.append({"question": question, "priority": priority})
    return parsed


def parse_assumptions(raw: str | list[Any]) -> list[str]:
    """Parse assumptions from text (one per line) or a JSON array of strings."""
    items: list[Any]
    if isinstance(raw, list):
        items = raw
    else:
        items = (raw or "").splitlines()
    assumptions: list[str] = []
    for item in items:
        text = _normalize_ws(_LIST_MARKER.sub("", str(item).strip()))
        if not text:
            continue
        if len(text) > _MAX_ASSUMPTION_CHARS:
            raise ToolError(
                "invalid_argument",
                f"assumption is {len(text)} characters; the limit is {_MAX_ASSUMPTION_CHARS}.",
            )
        if text not in assumptions:
            assumptions.append(text)
        if len(assumptions) >= _MAX_ASSUMPTIONS:
            break
    return assumptions


def normalize_limits(raw: dict[str, Any] | None) -> dict[str, int]:
    """Merge per-run overrides over :data:`DEFAULT_LIMITS` and validate them.

    Unknown keys and non-positive/non-integer values are rejected; values
    above the hard ceiling are clamped to it.
    """
    limits = dict(DEFAULT_LIMITS)
    if not raw:
        return limits
    if not isinstance(raw, dict):
        raise ToolError("invalid_argument", "limits must be a JSON object")
    for key, value in raw.items():
        if key not in DEFAULT_LIMITS:
            raise ToolError(
                "invalid_argument",
                f"Unknown limit {key!r}; valid limits: {', '.join(sorted(DEFAULT_LIMITS))}",
            )
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(
                "invalid_argument", f"limit {key!r} must be an integer, got {value!r}"
            )
        if value < 1:
            raise ToolError("invalid_argument", f"limit {key!r} must be >= 1, got {value}")
        limits[key] = min(value, LIMIT_CEILINGS[key])
    return limits


def limits_for_run(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    """The effective limits of one run (plan overrides or the defaults)."""
    row = conn.execute("SELECT plan FROM research_runs WHERE id = ?", (int(run_id),)).fetchone()
    plan = _load_object(row["plan"]) if row is not None else {}
    raw_limits = plan.get("limits") if isinstance(plan.get("limits"), dict) else None
    try:
        return normalize_limits(raw_limits)
    except ToolError:  # pragma: no cover - defensive: stored plans are validated
        return dict(DEFAULT_LIMITS)


# ---------------------------------------------------------------------------
# Budget accounting
# ---------------------------------------------------------------------------


def count_run_sources(conn: sqlite3.Connection, run_id: int) -> int:
    """Distinct sources linked to one run."""
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM research_run_sources WHERE run_id = ?", (int(run_id),)
        ).fetchone()["n"]
    )


def run_links_source(conn: sqlite3.Connection, run_id: int, source_id: int) -> bool:
    """True when *source_id* is already associated with *run_id*."""
    row = conn.execute(
        "SELECT 1 FROM research_run_sources WHERE run_id = ? AND source_id = ?",
        (int(run_id), int(source_id)),
    ).fetchone()
    return row is not None


def count_rounds(conn: sqlite3.Connection, run_id: int) -> int:
    """Follow-up rounds started for one run."""
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM research_run_events WHERE run_id = ? AND kind = 'followup_round'",
            (int(run_id),),
        ).fetchone()["n"]
    )


def count_candidates(conn: sqlite3.Connection, run_id: int, subquestion_id: int) -> int:
    """Candidate sources logged for a subquestion in its current round.

    A :func:`start_followup_round` call that targets the subquestion resets
    its candidate budget; rounds targeting other subquestions do not.
    """
    last_round = conn.execute(
        """
        SELECT MAX(id) AS last_id FROM research_run_events
        WHERE run_id = ? AND kind = 'followup_round' AND subquestion_id = ?
        """,
        (int(run_id), int(subquestion_id)),
    ).fetchone()["last_id"]
    if last_round is None:
        return int(
            conn.execute(
                """
                SELECT COUNT(*) AS n FROM research_run_events
                WHERE run_id = ? AND kind = 'source_candidate' AND subquestion_id = ?
                """,
                (int(run_id), int(subquestion_id)),
            ).fetchone()["n"]
        )
    return int(
        conn.execute(
            """
            SELECT COUNT(*) AS n FROM research_run_events
            WHERE run_id = ? AND kind = 'source_candidate' AND subquestion_id = ? AND id > ?
            """,
            (int(run_id), int(subquestion_id), int(last_round)),
        ).fetchone()["n"]
    )


def check_source_capacity(
    conn: sqlite3.Connection, run_id: int, *, already_linked: bool
) -> dict[str, Any]:
    """Refuse one more unique source when the run's cap is reached.

    Called by the evidence pipeline before a source is linked to a run.
    Re-saving an already-linked source is always allowed (it adds no unique
    source); a new one is rejected with ``limit_reached`` once the run
    already links ``max_unique_sources`` sources — across all subquestions,
    so overlapping research cannot exceed the overall cap.
    """
    limits = limits_for_run(conn, run_id)
    used = count_run_sources(conn, run_id)
    cap = limits["max_unique_sources"]
    if not already_linked and used >= cap:
        raise ToolError(
            "limit_reached",
            f"Research run {run_id} already links {used} unique sources (limit {cap}). "
            "Re-use stored evidence via search_evidence, or raise max_unique_sources "
            "with save_research_plan and document why.",
        )
    return {
        "sources_used": used,
        "max_unique_sources": cap,
        "sources_remaining": max(cap - used, 0),
    }


# ---------------------------------------------------------------------------
# Stopping evaluation (pure)
# ---------------------------------------------------------------------------


def evaluate_stopping(
    subquestions: list[dict[str, Any]],
    limits: dict[str, int],
    *,
    sources_used: int,
    rounds_used: int,
) -> dict[str, Any]:
    """Decide whether the workflow must stop, and why.

    Stops when every subquestion is answered, or when a configured limit is
    reached while gaps remain (the remaining gaps must then be reported
    honestly instead of continuing indefinitely). Limits reached with no
    gaps are covered by the first condition.
    """
    if not subquestions:
        return {
            "should_stop": False,
            "reasons": ["no research plan is recorded"],
            "gaps_remaining": 0,
        }
    gaps = [item for item in subquestions if item["status"] in GAP_STATUSES]
    reasons: list[str] = []
    if not gaps:
        reasons.append("all subquestions are answered")
    else:
        if rounds_used >= limits["max_followup_rounds"]:
            reasons.append(
                f"follow-up round limit reached ({rounds_used}/{limits['max_followup_rounds']}) "
                f"with {len(gaps)} subquestion(s) still open"
            )
        if sources_used >= limits["max_unique_sources"]:
            reasons.append(
                f"unique-source cap reached ({sources_used}/{limits['max_unique_sources']}) "
                f"with {len(gaps)} subquestion(s) still open"
            )
    return {"should_stop": bool(reasons), "reasons": reasons, "gaps_remaining": len(gaps)}


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _insert_event(
    conn: sqlite3.Connection,
    run_id: int,
    kind: str,
    message: str,
    *,
    subquestion_id: int = 0,
    source_id: int = 0,
    payload: dict[str, Any] | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO research_run_events
            (run_id, kind, message, subquestion_id, source_id, payload, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(run_id),
            kind,
            message,
            int(subquestion_id) or None,
            int(source_id) or None,
            _dumps(dict(payload or {})),
            _utc_now(),
        ),
    )
    return int(cursor.lastrowid)


def _event_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": int(row["id"]),
        "kind": row["kind"],
        "message": row["message"],
        "subquestion_id": int(row["subquestion_id"]) if row["subquestion_id"] is not None else None,
        "source_id": int(row["source_id"]) if row["source_id"] is not None else None,
        "payload": _load_object(row["payload"]),
        "created_at": row["created_at"],
    }


def log_event(
    run_id: int,
    kind: str,
    message: str,
    *,
    subquestion_id: int = 0,
    source_id: int = 0,
    payload: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Append one execution-log entry for a research run.

    Validated: the kind must be a documented event kind; ``followup_round``
    is rejected here (use :func:`start_followup_round` so the round cap is
    enforced); ``source_candidate`` requires a subquestion and is capped per
    subquestion per round; referenced subquestion/source ids must exist.
    """
    settings = settings or load_settings()
    clean_kind = (kind or "").strip().lower()
    if clean_kind not in EVENT_KINDS:
        raise ToolError(
            "invalid_argument",
            f"Unknown event kind {kind!r}; valid kinds: {', '.join(EVENT_KINDS)}",
        )
    if clean_kind == "followup_round":
        raise ToolError(
            "invalid_argument",
            "follow-up rounds are logged by start_followup_round, which enforces "
            "the round limit; log the reason there.",
        )
    clean_message = _normalize_ws(message)[:_MAX_MESSAGE_CHARS]
    if not clean_message:
        raise ToolError("invalid_argument", "message must not be empty")
    if payload is not None and not isinstance(payload, dict):
        raise ToolError("invalid_argument", "payload must be a JSON object")
    serialized_payload = _dumps(dict(payload or {}))
    if len(serialized_payload) > _MAX_PAYLOAD_CHARS:
        raise ToolError(
            "invalid_argument",
            f"payload is {len(serialized_payload)} characters; the limit is {_MAX_PAYLOAD_CHARS}.",
        )
    with storage.open_db(settings) as conn:
        _require_run(conn, run_id)
        if subquestion_id and not _subquestion_belongs(conn, run_id, subquestion_id):
            raise ToolError(
                "not_found",
                f"Subquestion {subquestion_id} does not belong to research run {run_id}.",
            )
        if source_id:
            source = conn.execute(
                "SELECT id FROM sources WHERE id = ?", (int(source_id),)
            ).fetchone()
            if source is None:
                raise ToolError("not_found", f"No source with id {source_id}.")
        if clean_kind == "source_candidate":
            if not subquestion_id:
                raise ToolError(
                    "invalid_argument",
                    "source_candidate events must name the subquestion_id they are "
                    "candidates for so the per-subquestion cap can be enforced.",
                )
            limits = limits_for_run(conn, run_id)
            used = count_candidates(conn, int(run_id), int(subquestion_id))
            cap = limits["max_candidates_per_subquestion"]
            if used >= cap:
                raise ToolError(
                    "limit_reached",
                    f"Subquestion {subquestion_id} already logged {used} candidate "
                    f"sources this round (limit {cap}). Stop searching for this "
                    "subquestion in this round.",
                )
        event_id = _insert_event(
            conn,
            run_id,
            clean_kind,
            clean_message,
            subquestion_id=int(subquestion_id),
            source_id=int(source_id),
            payload=payload,
        )
        row = conn.execute(
            "SELECT * FROM research_run_events WHERE id = ?", (event_id,)
        ).fetchone()
        result: dict[str, Any] = {"run_id": int(run_id), **_event_payload(row)}
        if clean_kind == "source_candidate":
            limits = limits_for_run(conn, run_id)
            used = count_candidates(conn, int(run_id), int(subquestion_id))
            result["candidates_used"] = used
            result["candidates_remaining"] = max(
                limits["max_candidates_per_subquestion"] - used, 0
            )
        return result


def start_followup_round(
    run_id: int,
    reason: str,
    *,
    subquestion_id: int = 0,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Begin one bounded follow-up round and log why it was started.

    Fails with ``limit_reached`` once the run's follow-up round limit (default
    2) is exhausted, so the workflow stops instead of looping. A round that
    targets a subquestion also resets that subquestion's candidate budget.
    """
    settings = settings or load_settings()
    clean_reason = _normalize_ws(reason)[:_MAX_MESSAGE_CHARS]
    if not clean_reason:
        raise ToolError("invalid_argument", "reason must not be empty")
    with storage.open_db(settings) as conn:
        _require_run(conn, run_id)
        if subquestion_id and not _subquestion_belongs(conn, run_id, subquestion_id):
            raise ToolError(
                "not_found",
                f"Subquestion {subquestion_id} does not belong to research run {run_id}.",
            )
        limits = limits_for_run(conn, run_id)
        used = count_rounds(conn, int(run_id))
        cap = limits["max_followup_rounds"]
        if used >= cap:
            raise ToolError(
                "limit_reached",
                f"Research run {run_id} already used its {cap} follow-up round(s). "
                "Stop searching and report the remaining gaps honestly in the report.",
            )
        round_number = used + 1
        event_id = _insert_event(
            conn,
            run_id,
            "followup_round",
            clean_reason,
            subquestion_id=int(subquestion_id),
            payload={"round": round_number},
        )
        return {
            "run_id": int(run_id),
            "event_id": event_id,
            "round": round_number,
            "rounds_used": round_number,
            "rounds_remaining": max(cap - round_number, 0),
            "reason": clean_reason,
            "subquestion_id": int(subquestion_id) or None,
            "limits": limits,
        }


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def _subquestion_belongs(conn: sqlite3.Connection, run_id: int, subquestion_id: int) -> bool:
    row = conn.execute(
        "SELECT run_id FROM research_subquestions WHERE id = ?", (int(subquestion_id),)
    ).fetchone()
    return row is not None and int(row["run_id"]) == int(run_id)


def _subquestion_payload(row: sqlite3.Row, *, candidates_used: int | None = None,
                         candidates_remaining: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "subquestion_id": int(row["id"]),
        "run_id": int(row["run_id"]),
        "seq": int(row["seq"]),
        "question": row["question"],
        "priority": int(row["priority"]),
        "status": row["status"],
        "note": row["note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if candidates_used is not None:
        payload["candidates_used"] = candidates_used
        payload["candidates_remaining"] = candidates_remaining
    return payload


def _plan_payload(raw: str, limits: dict[str, int]) -> dict[str, Any] | None:
    plan = _load_object(raw)
    if not plan:
        return None
    return {
        "objective": plan.get("objective", ""),
        "scope": plan.get("scope", ""),
        "assumptions": [str(item) for item in plan.get("assumptions", []) if str(item).strip()],
        "limits": limits,
        "created_at": plan.get("created_at"),
        "updated_at": plan.get("updated_at"),
    }


def save_plan(
    run_id: int,
    *,
    objective: str,
    subquestions: str | list[Any],
    scope: str = "",
    assumptions: str | list[Any] = "",
    limits: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Record (or update) the research plan of one run.

    The first call stores the objective, scope, assumptions, per-run limits,
    and the subquestion list. Saving again updates the plan metadata and
    adds new subquestions; existing subquestions keep their ids, statuses,
    and notes. The subquestion cap is enforced here.
    """
    settings = settings or load_settings()
    clean_objective = _normalize_ws(objective)
    if not clean_objective:
        raise ToolError("invalid_argument", "objective must not be empty")
    if len(clean_objective) > _MAX_OBJECTIVE_CHARS:
        raise ToolError(
            "invalid_argument",
            f"objective is {len(clean_objective)} characters; the limit is {_MAX_OBJECTIVE_CHARS}.",
        )
    if len(scope or "") > _MAX_SCOPE_CHARS:
        raise ToolError(
            "invalid_argument",
            f"scope is {len(scope)} characters; the limit is {_MAX_SCOPE_CHARS}.",
        )
    questions = parse_subquestions(subquestions)
    if not questions:
        raise ToolError("invalid_argument", "at least one subquestion is required")
    merged_limits = normalize_limits(limits)
    cleaned_assumptions = parse_assumptions(assumptions)
    now = _utc_now()
    with storage.open_db(settings) as conn:
        run_row = _require_run(conn, run_id)
        existing_rows = conn.execute(
            "SELECT * FROM research_subquestions WHERE run_id = ? ORDER BY seq, id",
            (int(run_id),),
        ).fetchall()
        existing_by_key = {_normalize_ws(row["question"]).lower(): row for row in existing_rows}
        next_seq = max((int(row["seq"]) for row in existing_rows), default=0) + 1
        created_count = 0
        for index, item in enumerate(questions, start=1):
            key = item["question"].lower()
            if key in existing_by_key:
                continue
            if len(existing_by_key) >= merged_limits["max_subquestions"]:
                raise ToolError(
                    "invalid_argument",
                    f"the plan already has {len(existing_by_key)} subquestions; "
                    f"the limit is {merged_limits['max_subquestions']}. Merge or drop "
                    "subquestions, or raise max_subquestions with a documented reason.",
                )
            cursor = conn.execute(
                """
                INSERT INTO research_subquestions
                    (run_id, seq, question, priority, status, note, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', '', ?, ?)
                """,
                (int(run_id), next_seq, item["question"], int(item["priority"]) or index, now, now),
            )
            row = conn.execute(
                "SELECT * FROM research_subquestions WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
            existing_by_key[key] = row
            next_seq += 1
            created_count += 1
        previous = _load_object(run_row["plan"])
        plan_json = _dumps(
            {
                "objective": clean_objective,
                "scope": (scope or "").strip(),
                "assumptions": cleaned_assumptions,
                "limits": merged_limits,
                "created_at": previous.get("created_at") or now,
                "updated_at": now,
            }
        )
        conn.execute("UPDATE research_runs SET plan = ? WHERE id = ?", (plan_json, int(run_id)))
        _insert_event(
            conn,
            run_id,
            "plan",
            f"plan saved: {len(existing_by_key)} subquestion(s), {created_count} new",
            payload={"limits": merged_limits, "new_subquestions": created_count},
        )
        overview = describe_run(conn, int(run_id))
        return {
            "run_id": int(run_id),
            "plan": overview["plan"],
            "subquestions": overview["subquestions"],
            "limits": merged_limits,
            "created_subquestions": created_count,
            "total_subquestions": len(existing_by_key),
            "budget": overview["budget"],
            "stopping": overview["stopping"],
        }


def update_subquestion(
    run_id: int,
    subquestion_id: int,
    status: str,
    *,
    note: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Set the evaluation status of one subquestion (with a rationale note).

    Status is one of ``pending``, ``answered``, ``partially_answered``,
    ``unanswered``, ``conflicting``. A non-empty note is appended to the
    subquestion's audit trail (newest text kept within the note cap).
    """
    settings = settings or load_settings()
    clean_status = (status or "").strip().lower()
    if clean_status not in SUBQUESTION_STATUSES:
        raise ToolError(
            "invalid_argument",
            f"status must be one of {', '.join(SUBQUESTION_STATUSES)}; got {status!r}",
        )
    clean_note = (note or "").strip()
    with storage.open_db(settings) as conn:
        _require_run(conn, run_id)
        row = conn.execute(
            "SELECT * FROM research_subquestions WHERE id = ?", (int(subquestion_id),)
        ).fetchone()
        if row is None or int(row["run_id"]) != int(run_id):
            raise ToolError(
                "not_found",
                f"Subquestion {subquestion_id} does not belong to research run {run_id}.",
            )
        combined_note = "\n".join(
            part for part in (row["note"] or "", clean_note) if part.strip()
        )
        if len(combined_note) > _MAX_NOTE_CHARS:
            combined_note = combined_note[-_MAX_NOTE_CHARS:]
        conn.execute(
            "UPDATE research_subquestions SET status = ?, note = ?, updated_at = ? WHERE id = ?",
            (clean_status, combined_note, _utc_now(), int(subquestion_id)),
        )
        updated = conn.execute(
            "SELECT * FROM research_subquestions WHERE id = ?", (int(subquestion_id),)
        ).fetchone()
        limits = limits_for_run(conn, run_id)
        candidates = count_candidates(conn, int(run_id), int(subquestion_id))
        return {
            "run_id": int(run_id),
            "subquestion": _subquestion_payload(
                updated,
                candidates_used=candidates,
                candidates_remaining=max(limits["max_candidates_per_subquestion"] - candidates, 0),
            ),
            "stopping": overview_stopping(conn, int(run_id)),
        }


# ---------------------------------------------------------------------------
# Run overview
# ---------------------------------------------------------------------------


def _subquestion_rows(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM research_subquestions WHERE run_id = ? ORDER BY seq, id", (int(run_id),)
    ).fetchall()


def overview_stopping(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    """Evaluate the stopping condition of one run against its current state."""
    limits = limits_for_run(conn, run_id)
    subquestions = [
        _subquestion_payload(row) for row in _subquestion_rows(conn, int(run_id))
    ]
    return evaluate_stopping(
        subquestions,
        limits,
        sources_used=count_run_sources(conn, int(run_id)),
        rounds_used=count_rounds(conn, int(run_id)),
    )


def describe_run(
    conn: sqlite3.Connection, run_id: int, *, event_limit: int = DEFAULT_EVENT_LIMIT
) -> dict[str, Any]:
    """Plan, subquestions, budget, gaps, stopping state, and recent events.

    Used by ``get_research_history`` so the orchestrator can check its budget
    and stop conditions without extra bookkeeping.
    """
    run_row = _require_run(conn, run_id)
    limits = limits_for_run(conn, run_id)
    plan = _plan_payload(run_row["plan"] or "", limits)
    rows = _subquestion_rows(conn, run_id)
    subquestions: list[dict[str, Any]] = []
    for row in rows:
        candidates = count_candidates(conn, int(run_id), int(row["id"]))
        subquestions.append(
            _subquestion_payload(
                row,
                candidates_used=candidates,
                candidates_remaining=max(limits["max_candidates_per_subquestion"] - candidates, 0),
            )
        )
    sources_used = count_run_sources(conn, int(run_id))
    rounds_used = count_rounds(conn, int(run_id))
    gaps = [
        {
            "subquestion_id": item["subquestion_id"],
            "question": item["question"],
            "status": item["status"],
            "note": item["note"],
            "candidates_used": item["candidates_used"],
            "candidates_remaining": item["candidates_remaining"],
        }
        for item in subquestions
        if item["status"] in GAP_STATUSES
    ]
    bounded_events = min(max(int(event_limit), 1), 1000)
    total_events = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM research_run_events WHERE run_id = ?", (int(run_id),)
        ).fetchone()["n"]
    )
    event_rows = conn.execute(
        """
        SELECT * FROM research_run_events WHERE run_id = ?
        ORDER BY id DESC LIMIT ?
        """,
        (int(run_id), bounded_events),
    ).fetchall()
    events = [_event_payload(row) for row in reversed(event_rows)]
    return {
        "plan": plan,
        "subquestions": subquestions,
        "gaps": gaps,
        "budget": {
            "sources_used": sources_used,
            "max_unique_sources": limits["max_unique_sources"],
            "sources_remaining": max(limits["max_unique_sources"] - sources_used, 0),
            "followup_rounds_used": rounds_used,
            "max_followup_rounds": limits["max_followup_rounds"],
            "followup_rounds_remaining": max(limits["max_followup_rounds"] - rounds_used, 0),
            "max_candidates_per_subquestion": limits["max_candidates_per_subquestion"],
            "max_subquestions": limits["max_subquestions"],
        },
        "stopping": evaluate_stopping(
            subquestions, limits, sources_used=sources_used, rounds_used=rounds_used
        ),
        "events": events,
        "event_count": total_events,
        "events_truncated": total_events > len(events),
    }


def run_overview(
    run_id: int, *, settings: Settings | None = None, event_limit: int = DEFAULT_EVENT_LIMIT
) -> dict[str, Any]:
    """:func:`describe_run` for callers that are not already inside a connection."""
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        return describe_run(conn, int(run_id), event_limit=event_limit)

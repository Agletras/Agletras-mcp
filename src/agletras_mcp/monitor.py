"""Repeatable monitoring of public sources with meaningful-change detection.

Phase 8 adds monitoring on top of the existing evidence stack — same SQLite
store, same adapters, same passages — with three explicit rules:

1. **Failures are not deletions.** A failed retrieval is recorded as a failed
   check with its error code; the last known content is left untouched and the
   change report says the target was unreachable. Nothing is inferred.
2. **Only substantive text changes are meaningful.** Advertisements, cookie
   banners, navigation, sharing widgets, and bare timestamps are classified as
   boilerplate *before* the comparison, so a page that only rewrites its
   timestamp reports "no meaningful change" (the ignored lines stay visible).
3. **Every detected change is traceable.** A snapshot references the stored
   evidence rows (``source_id``/``content_id``) for the old and new content,
   their content hashes, and the exact added/removed lines.

Conditional requests (``If-None-Match``/``If-Modified-Since``) are used when
the server supports them; a ``304`` is recorded as ``not_modified`` and never
counted as a change. Local files are compared by extracted text (any supported
Phase 2 document type) using a size+mtime fingerprint as a cheap pre-check.

Checks are independent, so :func:`run_checks` executes them with the bounded
parallel runner and a sequential fallback; every check is idempotent and safe
to repeat.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import adapters, canonurl, evidence, localfs, parallel, storage, webfetch
from .config import Settings, load_settings
from .errors import ToolError

TARGET_KINDS = ("url", "path")
SNAPSHOT_STATUSES = ("initial", "unchanged", "changed", "not_modified", "failed")
#: Snapshots that indicate a *meaningful* difference was found.
_CHANGE_STATUSES = ("changed",)

#: Lines matching any pattern are boilerplate: navigation, ads, cookie
#: banners, sharing widgets, and bare timestamps. They are compared and shown
#: but never treated as a meaningful business change.
_BOILERPLATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("copyright", re.compile(r"(?i)(©|\(c\))\s*\d{0,4}|all rights reserved|copyright")),
    ("cookies", re.compile(r"(?i)\bcookies?\b|we use cookies|accept (all )?cookies|consent")),
    ("legal", re.compile(r"(?i)^\s*(privacy|terms|legal|imprint|impressum|cookie)\b.*(policy|notice|terms|statement)?\s*$")),
    ("auth", re.compile(r"(?i)^\s*(sign|log) ?(in|up|out)\b|^create (an )?account\b|^my account\b")),
    ("navigation", re.compile(r"(?i)^\s*(home|menu|navigation|skip to (main )?content|search|back to top|close|contact|about|sitemap|help)\s*$")),
    ("share", re.compile(r"(?i)^\s*(share|tweet|post|print|email|copy link|facebook|twitter|linkedin|reddit|pinterest|x)\b[\s:–-]*$")),
    ("subscribe", re.compile(r"(?i)^\s*(subscribe|newsletter|follow us|rss|get updates)\b")),
    ("placeholder", re.compile(r"(?i)enable javascript|javascript is (disabled|required)|your browser|advert|sponsored")),
    ("timestamp_only", re.compile(r"(?i)^\s*((last )?(updated|modified|reviewed|changed)|published|posted|date)?\s*[:]?\s*\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}([ t,:]\d{1,2}:\d{2}(:\d{2})?)?\s*(utc|gmt|[+-]\d{2}:?\d{2})?\s*$")),
    ("standalone_number", re.compile(r"^\s*[\d\s.,%–-]{1,24}\s*$")),
)


# ---------------------------------------------------------------------------
# Normalization and comparison
# ---------------------------------------------------------------------------


def _compact(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def classify_line(line: str) -> str:
    """Return the boilerplate reason for *line*, or ``""`` for substantive text."""
    cleaned = _compact(line)
    if not cleaned:
        return "empty"
    for label, pattern in _BOILERPLATE_PATTERNS:
        if pattern.search(cleaned):
            return label
    return ""


def normalize_for_change(text: str) -> dict[str, Any]:
    """Split *text* into substantive and ignored (boilerplate) lines.

    The substantive text is whitespace-normalized without touching word order
    or case; its SHA-256 is the *normalized content hash* used for change
    detection, so ad/navigation churn alone can never trip a change alert.
    """
    substantive: list[str] = []
    ignored: list[dict[str, str]] = []
    for raw in (text or "").splitlines():
        cleaned = _compact(raw)
        if not cleaned:
            continue
        reason = classify_line(cleaned)
        if reason:
            ignored.append({"reason": reason, "line": cleaned[:300]})
        else:
            substantive.append(cleaned)
    body = "\n".join(substantive)
    return {
        "substantive_text": body,
        "substantive_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "substantive_lines": len(substantive),
        "ignored": ignored[:200],
        "ignored_count": len(ignored),
    }


def diff_texts(old_text: str, new_text: str, *, max_items: int = 40) -> dict[str, Any]:
    """Line-level difference between two normalized extracts (bounded)."""
    old = normalize_for_change(old_text)
    new = normalize_for_change(new_text)
    old_lines = [line for line in old["substantive_text"].splitlines() if line]
    new_lines = [line for line in new["substantive_text"].splitlines() if line]
    old_set = set(old_lines)
    new_set = set(new_lines)
    added = [line for line in dict.fromkeys(new_lines) if line not in old_set]
    removed = [line for line in dict.fromkeys(old_lines) if line not in new_set]
    kind = "substantive" if (added or removed) else (
        "boilerplate_only" if old["substantive_hash"] != new["substantive_hash"] else "none"
    )
    # A boilerplate-only difference cannot leave the normalized text equal by
    # construction; the branch above only guards future normalization changes.
    added_ignored = [entry for entry in new["ignored"] if entry not in old["ignored"]]
    removed_ignored = [entry for entry in old["ignored"] if entry not in new["ignored"]]
    return {
        "kind": kind,
        "meaningful": bool(added or removed),
        "added": added[:max_items],
        "removed": removed[:max_items],
        "added_count": len(added),
        "removed_count": len(removed),
        "ignored_added_count": len(added_ignored),
        "ignored_removed_count": len(removed_ignored),
        "ignored_samples": [entry["line"] for entry in (added_ignored + removed_ignored)[:10]],
        "substantive_hash": new["substantive_hash"],
        "previous_substantive_hash": old["substantive_hash"],
        "substantive_lines": new["substantive_lines"],
        "ignored_lines": new["ignored_count"],
    }


def summarize_change(diff: dict[str, Any]) -> str:
    """One human sentence about a diff (never inflates boilerplate noise)."""
    if diff.get("kind") == "none":
        return "No content change detected."
    if diff.get("meaningful"):
        return (
            f"{diff.get('added_count', 0)} substantive line(s) added and "
            f"{diff.get('removed_count', 0)} removed"
            + (
                f"; {diff.get('ignored_added_count', 0) + diff.get('ignored_removed_count', 0)} "
                "boilerplate line(s) also differed (ignored)"
                if diff.get("ignored_added_count") or diff.get("ignored_removed_count")
                else ""
            )
            + "."
        )
    return (
        "No substantive line was added or removed; the remaining difference is line "
        "repetition/ordering or boilerplate (navigation, ads, cookie notices, timestamps) "
        "and is not treated as a meaningful change."
    )


# ---------------------------------------------------------------------------
# Target management
# ---------------------------------------------------------------------------


def _normalize_target(target: str, kind: str) -> tuple[str, str]:
    """Return ``(kind, canonical_identity)`` for a monitoring target."""
    raw = (target or "").strip()
    if not raw:
        raise ToolError("invalid_argument", "target must not be empty")
    requested = (kind or "").strip().lower()
    if requested and requested not in TARGET_KINDS:
        raise ToolError(
            "invalid_argument", f"kind must be one of {', '.join(TARGET_KINDS)}; got {kind!r}"
        )
    if requested == "url" or (not requested and re.match(r"(?i)^https?://", raw)):
        identity = canonurl.canonicalize_url(raw)
        if identity is None:
            raise ToolError("invalid_argument", f"Not a usable http(s) URL: {raw!r}")
        return "url", identity
    if requested == "path" or not re.match(r"(?i)^[a-z][a-z0-9+.-]*://", raw):
        return "path", raw
    raise ToolError(
        "invalid_argument",
        f"Unsupported target {raw!r}: use an http(s) URL or a permitted local file path.",
    )


def _target_key(kind: str, identity: str) -> str:
    if kind == "url":
        return identity
    try:
        resolved = str(Path(identity).expanduser().resolve())
    except OSError:  # pragma: no cover - exotic paths
        resolved = identity
    return f"path:{resolved}"


def _target_payload(row: sqlite3.Row, *, last_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        config = json.loads(row["config"] or "{}")
    except json.JSONDecodeError:
        config = {}
    return {
        "target_id": int(row["id"]),
        "target_key": row["target_key"],
        "name": row["name"] or "",
        "kind": row["kind"],
        "target": row["target"],
        "project": row["project"] or "",
        "config": config if isinstance(config, dict) else {},
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_snapshot": last_snapshot,
    }


def add_target(
    target: str,
    *,
    name: str = "",
    project: str = "",
    kind: str = "",
    config: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Register (or update) one monitoring target; idempotent by identity."""
    settings = settings or load_settings()
    resolved_kind, identity = _normalize_target(target, kind)
    if resolved_kind == "path":
        # Refuse paths outside the configured access roots immediately: a
        # monitoring target that can never be read is better rejected upfront.
        localfs.resolve_allowed(identity, settings)
    key = _target_key(resolved_kind, identity)
    now = evidence._utc_now()
    clean_config = dict(config or {})
    if not isinstance(clean_config, dict):  # pragma: no cover - defensive
        raise ToolError("invalid_argument", "config must be a JSON object")
    with storage.open_db(settings) as conn:
        existing = conn.execute(
            "SELECT * FROM monitor_targets WHERE target_key = ?", (key,)
        ).fetchone()
        if existing is not None:
            updates: dict[str, Any] = {}
            if name:
                updates["name"] = name.strip()
            if project:
                updates["project"] = project.strip()
            if clean_config:
                merged = {**(json.loads(existing["config"] or "{}") or {}), **(clean_config or {})}
                updates["config"] = json.dumps(merged, ensure_ascii=False, sort_keys=True)
            if updating_kind := (kind and kind != existing["kind"]):
                updates["kind"] = resolved_kind  # pragma: no cover - key implies kind
            if updates:
                updates["updated_at"] = now
                assignments = ", ".join(f"{column} = ?" for column in updates)
                conn.execute(
                    f"UPDATE monitor_targets SET {assignments} WHERE id = ?",
                    (*updates.values(), int(existing["id"])),
                )
            row = conn.execute(
                "SELECT * FROM monitor_targets WHERE id = ?", (int(existing["id"]),)
            ).fetchone()
            return {"target": _target_payload(row), "created": False}
        cursor = conn.execute(
            """
            INSERT INTO monitor_targets
                (target_key, name, kind, target, project, config, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                key,
                (name or "").strip() or (identity if resolved_kind == "url" else Path(identity).name),
                resolved_kind,
                identity,
                (project or "").strip(),
                json.dumps(clean_config, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM monitor_targets WHERE id = ?", (int(cursor.lastrowid),)
        ).fetchone()
    return {"target": _target_payload(row), "created": True}


def remove_target(target_id: int, *, settings: Settings | None = None) -> dict[str, Any]:
    """Delete one target and (by cascade) its snapshots."""
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        row = conn.execute(
            "SELECT * FROM monitor_targets WHERE id = ?", (int(target_id),)
        ).fetchone()
        if row is None:
            raise ToolError("not_found", f"No monitoring target with id {target_id}")
        snapshot_count = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM monitor_snapshots WHERE target_id = ?",
                (int(target_id),),
            ).fetchone()["n"]
        )
        conn.execute("DELETE FROM monitor_targets WHERE id = ?", (int(target_id),))
    return {"removed": True, "target_id": int(target_id), "snapshots_removed": snapshot_count}


def _latest_snapshot(conn: sqlite3.Connection, target_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM monitor_snapshots WHERE target_id = ? ORDER BY id DESC LIMIT 1",
        (int(target_id),),
    ).fetchone()


def _latest_content_snapshot(conn: sqlite3.Connection, target_id: int) -> sqlite3.Row | None:
    """The newest snapshot whose content was actually stored (diff base).

    Snapshots that recorded a new hash without storing new evidence (a
    boilerplate-only difference) are skipped so the diff is always computed
    against the last *stored* text — the evidence chain never detaches.
    """
    return conn.execute(
        """
        SELECT * FROM monitor_snapshots
        WHERE target_id = ? AND content_hash IS NOT NULL AND content_id IS NOT NULL
        ORDER BY id DESC LIMIT 1
        """,
        (int(target_id),),
    ).fetchone()


def _snapshot_payload(row: sqlite3.Row | None, *, source: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = {
        "snapshot_id": int(row["id"]),
        "target_id": int(row["target_id"]),
        "checked_at": row["checked_at"],
        "status": row["status"],
        "meaningful": bool(row["meaningful"]),
        "http_status": row["http_status"],
        "etag": row["etag"],
        "last_modified": row["last_modified"],
        "content_hash": row["content_hash"],
        "raw_hash": row["raw_hash"],
        "source_id": row["source_id"],
        "content_id": row["content_id"],
        "previous_snapshot_id": row["previous_snapshot_id"],
        "chars": row["chars"],
        "added_terms": _load_list(row["added_terms"]),
        "removed_terms": _load_list(row["removed_terms"]),
        "ignored_boilerplate": _load_list(row["ignored_boilerplate"]),
        "summary": row["summary"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "created_at": row["created_at"],
    }
    if source is not None:
        payload["source"] = source
    return payload


def _load_list(raw: str | None) -> list[Any]:
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def list_targets(
    *,
    project: str = "",
    include_disabled: bool = True,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """All monitoring targets with their latest snapshot summary."""
    settings = settings or load_settings()
    clauses: list[str] = []
    params: list[Any] = []
    if project:
        clauses.append("project = ?")
        params.append(project.strip())
    if not include_disabled:
        clauses.append("enabled = 1")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with storage.open_db(settings) as conn:
        rows = conn.execute(
            f"SELECT * FROM monitor_targets {where} ORDER BY id", params
        ).fetchall()
        targets = []
        for row in rows:
            latest = _latest_snapshot(conn, int(row["id"]))
            targets.append(_target_payload(row, last_snapshot=_snapshot_payload(latest)))
    return {"count": len(targets), "targets": targets}


def _source_ref(conn: sqlite3.Connection, source_id: int | None) -> dict[str, Any] | None:
    if not source_id:
        return None
    row = conn.execute(
        """
        SELECT id, title, url, path, sha256, retrieved_at, last_checked_at
        FROM sources WHERE id = ?
        """,
        (int(source_id),),
    ).fetchone()
    if row is None:
        return None
    return {
        "source_id": int(row["id"]),
        "title": row["title"],
        "url": row["url"],
        "path": row["path"],
        "content_hash": row["sha256"],
        "retrieved_at": row["retrieved_at"],
        "last_checked_at": row["last_checked_at"],
    }


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------


@dataclass
class _FetchOutcome:
    status: str  # "fetched" | "not_modified"
    text: str = ""
    title: str = ""
    final_url: str = ""
    content_type: str = ""
    etag: str = ""
    last_modified: str = ""
    http_status: int = 200


def _fetch_url_text(
    url: str,
    *,
    etag: str,
    last_modified: str,
    settings: Settings,
) -> _FetchOutcome:
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    final_url, content_type, body, response_headers, status = webfetch.fetch_bytes_with_headers(
        url, settings, headers=headers
    )
    if status == 304:
        return _FetchOutcome(status="not_modified", http_status=304, final_url=final_url)
    if webfetch.looks_like_pdf(content_type, body):
        raise ToolError(
            "unsupported_content",
            f"{final_url} serves a PDF; monitoring currently handles web pages and local documents.",
        )
    lowered = (content_type or "").lower()
    text = webfetch.decode_body(body, content_type)
    title = ""
    if webfetch.looks_like_html(content_type, body):
        title, text = webfetch.html_to_text(text)
    elif not (
        lowered.startswith("text/")
        or lowered.startswith("application/json")
        or "xml" in lowered
        or not lowered
    ):
        raise ToolError(
            "unsupported_content",
            f"Unsupported content type {content_type!r} at {final_url}.",
        )
    return _FetchOutcome(
        status="fetched",
        text=text.strip(),
        title=title,
        final_url=final_url,
        content_type=content_type,
        etag=response_headers.get("ETag", "") or "",
        last_modified=response_headers.get("Last-Modified", "") or "",
        http_status=status,
    )


def _read_local_text(path: str, settings: Settings) -> tuple[str, str, str]:
    resolved = localfs.resolve_allowed(path, settings)
    if resolved.is_dir():
        raise ToolError("invalid_argument", f"Expected a file, got a directory: {resolved}")
    prepared = adapters.prepare_file(resolved, settings)
    record = adapters.record_from_prepared(prepared, settings=settings)
    if record.extraction_status not in ("success", "partial") or not (record.extracted_text or "").strip():
        raise ToolError(
            record.error_code or "extraction_failed",
            record.error_message or f"Could not extract text from {resolved}",
        )
    return record.extracted_text, (record.title or resolved.name), resolved.stat().st_mtime_ns.__str__()


def _store_snapshot_content(
    target: dict[str, Any], text: str, title: str, *, settings: Settings
) -> dict[str, Any]:
    """Persist fetched text as evidence and return the source reference."""
    if target["kind"] == "url":
        outcome = evidence.save_evidence(
            text=text, title=title or target["name"], url=target["target"],
            notes="monitoring snapshot", source_type="web", settings=settings,
        )
    else:
        outcome = evidence.save_evidence(
            text=text, title=title or target["name"], path=target["target"],
            notes="monitoring snapshot", source_type="text", settings=settings,
        )
    return {
        "source_id": int(outcome["source_id"]),
        "content_id": int(outcome.get("content_id") or 0) or None,
        "content_hash": outcome.get("content_hash"),
        "chars": int(outcome.get("chars") or 0),
        "created_source": bool(outcome.get("created_source")),
    }


def check_target(
    target_id: int,
    *,
    settings: Settings | None = None,
    force_content: bool = False,
) -> dict[str, Any]:
    """Check one target once and record a structured snapshot.

    Never raises for retrieval problems: a failure is a recorded snapshot
    (status ``failed``) that leaves earlier evidence intact.
    """
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        row = conn.execute(
            "SELECT * FROM monitor_targets WHERE id = ?", (int(target_id),)
        ).fetchone()
        if row is None:
            raise ToolError("not_found", f"No monitoring target with id {target_id}")
        target = _target_payload(row)
        if not target["enabled"]:
            return {"checked": False, "target": target, "reason": "target is disabled"}
        previous_content = _latest_content_snapshot(conn, int(target_id))
        previous_any = _latest_snapshot(conn, int(target_id))
    previous_id = int(previous_any["id"]) if previous_any is not None else None

    etag = ""
    last_modified = ""
    if previous_any is not None and not force_content:
        etag = previous_any["etag"] or ""
        last_modified = previous_any["last_modified"] or ""

    checked_at = evidence._utc_now()
    fetch: _FetchOutcome | None = None
    error: ToolError | None = None
    try:
        if target["kind"] == "url":
            fetch = _fetch_url_text(
                target["target"], etag=etag, last_modified=last_modified, settings=settings
            )
        else:
            text, title, fingerprint = _read_local_text(target["target"], settings)
            fetch = _FetchOutcome(
                status="fetched", text=text, title=title, final_url=target["target"],
                content_type="", http_status=200, etag="", last_modified=fingerprint,
            )
    except ToolError as exc:
        error = exc
    except OSError as exc:
        error = ToolError("extraction_failed", f"Could not read {target['target']}: {exc}")

    if fetch is not None and fetch.status == "not_modified":
        return _record_snapshot(
            target,
            previous_id=previous_id,
            previous_content=previous_content,
            status="not_modified",
            summary=(
                "HTTP 304 Not Modified: the server confirmed the stored copy is current"
                + (f" (ETag {fetch.etag or etag})" if (fetch.etag or etag) else "")
                + "."
            ),
            http_status=fetch.http_status,
            etag=etag,
            last_modified=last_modified,
            settings=settings,
        )

    if fetch is None:
        assert error is not None
        return _record_snapshot(
            target,
            previous_id=previous_id,
            previous_content=previous_content,
            status="failed",
            summary=(
                f"Retrieval failed ({error.code}). This is a failed check, not evidence that the "
                "source changed or was deleted; the previously stored content is unchanged."
            ),
            error_code=error.code,
            error_message=error.message,
            etag=etag,
            last_modified=last_modified,
            settings=settings,
        )

    normalized = normalize_for_change(fetch.text)
    raw_hash = storage.content_hash(fetch.text)
    previous_hash = previous_content["content_hash"] if previous_content is not None else None

    if previous_content is None:
        stored = _store_snapshot_content(target, fetch.text, fetch.title, settings=settings)
        return _record_snapshot(
            target,
            previous_id=previous_id,
            previous_content=previous_content,
            status="initial",
            summary=(
                f"Baseline stored: {normalized['substantive_lines']} substantive line(s)"
                + (
                    f"; {normalized['ignored_count']} boilerplate line(s) ignored"
                    if normalized["ignored_count"]
                    else ""
                )
                + "."
            ),
            content_hash=normalized["substantive_hash"],
            raw_hash=raw_hash,
            stored=stored,
            http_status=fetch.http_status,
            etag=fetch.etag,
            last_modified=fetch.last_modified,
            settings=settings,
        )

    if previous_hash == normalized["substantive_hash"]:
        # Identical normalized text is never a change — including when the
        # check was forced (``force_content`` only bypasses conditional
        # request shortcuts, it never turns equality into a change).
        _touch_source(previous_content["source_id"], settings)
        return _record_snapshot(
            target,
            previous_id=previous_id,
            previous_content=previous_content,
            status="unchanged",
            summary=(
                "No content change: the extracted text is identical to the stored copy "
                f"({normalized['substantive_lines']} substantive line(s))."
            ),
            content_hash=normalized["substantive_hash"],
            raw_hash=raw_hash,
            source_id=previous_content["source_id"],
            content_id=previous_content["content_id"],
            chars=previous_content["chars"],
            http_status=fetch.http_status,
            etag=fetch.etag or etag,
            last_modified=fetch.last_modified or last_modified,
            settings=settings,
        )

    # Content differs: read the stored older text and build the diff.
    old_text = _stored_text(previous_content, settings=settings)
    diff = diff_texts(old_text, fetch.text)
    stored = None
    if diff["meaningful"] or previous_hash is None:
        stored = _store_snapshot_content(target, fetch.text, fetch.title, settings=settings)
    return _record_snapshot(
        target,
        previous_id=previous_id,
        previous_content=previous_content,
        status="changed",
        summary=summarize_change(diff),
        meaningful=bool(diff["meaningful"]),
        content_hash=normalized["substantive_hash"],
        raw_hash=raw_hash,
        stored=stored,
        diff=diff,
        http_status=fetch.http_status,
        etag=fetch.etag or etag,
        last_modified=fetch.last_modified or last_modified,
        settings=settings,
    )


def _stored_text(snapshot_row: sqlite3.Row, *, settings: Settings) -> str:
    content_id = snapshot_row["content_id"]
    if not content_id:
        return ""
    with storage.open_db(settings) as conn:
        row = conn.execute(
            "SELECT extracted_text FROM source_contents WHERE id = ?", (int(content_id),)
        ).fetchone()
    return row["extracted_text"] if row is not None else ""


def _touch_source(source_id: int | None, settings: Settings) -> None:
    if not source_id:
        return
    with storage.open_db(settings) as conn:
        conn.execute(
            "UPDATE sources SET last_checked_at = ? WHERE id = ?",
            (evidence._utc_now(), int(source_id)),
        )


def _record_snapshot(
    target: dict[str, Any],
    *,
    previous_id: int | None,
    previous_content: sqlite3.Row | None,
    status: str,
    summary: str,
    settings: Settings,
    meaningful: bool = False,
    http_status: int | None = None,
    etag: str = "",
    last_modified: str = "",
    content_hash: str | None = None,
    raw_hash: str | None = None,
    stored: dict[str, Any] | None = None,
    diff: dict[str, Any] | None = None,
    source_id: int | None = None,
    content_id: int | None = None,
    chars: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    now = evidence._utc_now()
    if stored is not None:
        source_id = stored.get("source_id")
        content_id = stored.get("content_id")
        chars = stored.get("chars")
    if content_id is None and previous_content is not None and status in ("not_modified", "failed"):
        content_id = previous_content["content_id"]
    if chars is None:
        chars = (
            previous_content["chars"]
            if previous_content is not None and status in ("not_modified", "failed", "unchanged")
            else None
        )
    if source_id is None and previous_content is not None and status in ("not_modified", "failed"):
        source_id = previous_content["source_id"]
    added = (diff or {}).get("added") or []
    removed = (diff or {}).get("removed") or []
    ignored = (diff or {}).get("ignored_samples") or []
    with storage.open_db(settings) as conn:
        cursor = conn.execute(
            """
            INSERT INTO monitor_snapshots (
                target_id, checked_at, status, meaningful, http_status, etag,
                last_modified, content_hash, raw_hash, source_id, content_id,
                previous_snapshot_id, chars, added_terms, removed_terms,
                ignored_boilerplate, summary, error_code, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(target["target_id"]), now, status, 1 if meaningful else 0,
                http_status, etag or None, last_modified or None,
                content_hash, raw_hash, source_id, content_id,
                previous_id, chars,
                json.dumps(added, ensure_ascii=False), json.dumps(removed, ensure_ascii=False),
                json.dumps(ignored, ensure_ascii=False), summary,
                error_code, error_message, now,
            ),
        )
        snapshot_id = int(cursor.lastrowid)
        conn.execute(
            "UPDATE monitor_targets SET updated_at = ? WHERE id = ?", (now, int(target["target_id"]))
        )
        row = conn.execute(
            "SELECT * FROM monitor_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        source_ref = _source_ref(conn, source_id)
    return {
        "checked": True,
        "target": target,
        "snapshot": _snapshot_payload(row, source=source_ref),
    }


# ---------------------------------------------------------------------------
# Batch runs (bounded parallel with a sequential fallback)
# ---------------------------------------------------------------------------


def run_checks(
    *,
    target_ids: list[int] | None = None,
    project: str = "",
    parallel_mode: bool = True,
    max_workers: int = 4,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Check several targets and return deterministic, per-target outcomes."""
    settings = settings or load_settings()
    if target_ids:
        wanted = [int(item) for item in target_ids]
    else:
        with storage.open_db(settings) as conn:
            clauses = ["enabled = 1"]
            params: list[Any] = []
            if project:
                clauses.append("project = ?")
                params.append(project.strip())
            wanted = [
                int(row["id"])
                for row in conn.execute(
                    f"SELECT id FROM monitor_targets WHERE {' AND '.join(clauses)} ORDER BY id",
                    params,
                ).fetchall()
            ]
    if not wanted:
        return {
            "checked": 0, "parallel": bool(parallel_mode), "counts_by_status": {},
            "snapshots": [], "warnings": ["no matching monitoring targets"],
        }
    tasks = [
        parallel.TaskSpec(
            task_id=f"monitor:{target_id}",
            name=f"monitor check {target_id}",
            run=lambda target_id=target_id: check_target(target_id, settings=settings),
            retries=1,
            retry_codes=("network_error", "rate_limited"),
            metadata={"target_id": target_id},
        )
        for target_id in wanted
    ]
    outcomes = parallel.run_tasks(tasks, parallel=parallel_mode, max_workers=max_workers)
    snapshots: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    warnings: list[str] = []
    for outcome in outcomes:
        if outcome.status == "ok":
            payload = outcome.result
            snapshot = payload.get("snapshot")
            if snapshot:
                counts[snapshot["status"]] = counts.get(snapshot["status"], 0) + 1
                snapshots.append(snapshot)
            else:
                warnings.append(
                    f"target {outcome.metadata.get('target_id')}: {payload.get('reason', 'not checked')}"
                )
        else:
            counts["failed"] = counts.get("failed", 0) + 1
            warnings.append(
                f"target {outcome.metadata.get('target_id')}: {outcome.error_code}: {outcome.error_message}"
            )
    return {
        "checked": len(snapshots),
        "parallel": bool(parallel_mode),
        "counts_by_status": counts,
        "snapshots": snapshots,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Change history and reports
# ---------------------------------------------------------------------------


def list_snapshots(
    target_id: int, *, limit: int = 20, settings: Settings | None = None
) -> dict[str, Any]:
    """Recent snapshots for one target (newest first), with evidence refs."""
    settings = settings or load_settings()
    bounded = min(max(int(limit), 1), settings.max_list_limit)
    with storage.open_db(settings) as conn:
        target_row = conn.execute(
            "SELECT * FROM monitor_targets WHERE id = ?", (int(target_id),)
        ).fetchone()
        if target_row is None:
            raise ToolError("not_found", f"No monitoring target with id {target_id}")
        rows = conn.execute(
            "SELECT * FROM monitor_snapshots WHERE target_id = ? ORDER BY id DESC LIMIT ?",
            (int(target_id), bounded),
        ).fetchall()
        payloads = [
            _snapshot_payload(row, source=_source_ref(conn, row["source_id"])) for row in rows
        ]
    return {"target": _target_payload(target_row), "count": len(payloads), "snapshots": payloads}


def changes(
    *,
    target_id: int = 0,
    project: str = "",
    meaningful_only: bool = True,
    since_days: int = 0,
    limit: int = 50,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Detected changes (old/new evidence, diffs), newest first."""
    settings = settings or load_settings()
    clauses = [f"s.status IN ({', '.join('?' for _ in _CHANGE_STATUSES)})"]
    params: list[Any] = list(_CHANGE_STATUSES)
    if meaningful_only:
        clauses.append("s.meaningful = 1")
    if target_id:
        clauses.append("s.target_id = ?")
        params.append(int(target_id))
    if project:
        clauses.append("t.project = ?")
        params.append(project.strip())
    if since_days and since_days > 0:
        cutoff = datetime.now(timezone.utc).timestamp() - since_days * 86400
        clauses.append("s.checked_at >= ?")
        params.append(datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(timespec="seconds"))
    bounded = min(max(int(limit), 1), settings.max_list_limit * 5)
    with storage.open_db(settings) as conn:
        rows = conn.execute(
            f"""
            SELECT s.*, t.name AS target_name, t.target AS target_value, t.kind AS target_kind,
                   t.project AS target_project
            FROM monitor_snapshots s JOIN monitor_targets t ON t.id = s.target_id
            WHERE {' AND '.join(clauses)}
            ORDER BY s.id DESC LIMIT ?
            """,
            (*params, bounded),
        ).fetchall()
        items = []
        for row in rows:
            previous = None
            if row["previous_snapshot_id"]:
                previous_row = conn.execute(
                    "SELECT * FROM monitor_snapshots WHERE id = ?",
                    (int(row["previous_snapshot_id"]),),
                ).fetchone()
                previous = _snapshot_payload(
                    previous_row, source=_source_ref(conn, previous_row["source_id"]) if previous_row else None
                )
            items.append(
                {
                    "target": {
                        "target_id": int(row["target_id"]),
                        "name": row["target_name"],
                        "target": row["target_value"],
                        "kind": row["target_kind"],
                        "project": row["target_project"],
                    },
                    "change": _snapshot_payload(row, source=_source_ref(conn, row["source_id"])),
                    "previous": previous,
                }
            )
    return {"count": len(items), "meaningful_only": bool(meaningful_only), "changes": items}


def _svg_change_chart(points: list[tuple[str, int]], title: str) -> str:
    """A dependency-free SVG bar chart (only rendered when data exists)."""
    if len(points) < 2:
        return ""
    width, height, pad = 640, 180, 40
    bar_slot = (width - 2 * pad) / len(points)
    bar_width = max(6.0, bar_slot * 0.6)
    max_value = max((value for _label, value in points), default=1) or 1
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'role="img" aria-label="{_xml_escape(title)}">',
        f'<text x="{pad}" y="20" font-family="sans-serif" font-size="13">{_xml_escape(title)}</text>',
    ]
    for index, (label, value) in enumerate(points):
        x = pad + index * bar_slot + (bar_slot - bar_width) / 2
        bar_height = (height - 2 * pad) * (value / max_value)
        y = height - pad - bar_height
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{max(bar_height, 1):.1f}" '
            'fill="#4a6fa5"/>'
        )
        parts.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{height - pad + 14}" font-family="sans-serif" '
            f'font-size="10" text-anchor="middle">{_xml_escape(label)}</text>'
        )
        parts.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{max(y - 4, 30):.1f}" font-family="sans-serif" '
            f'font-size="10" text-anchor="middle">{value}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _xml_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _unique_report_path(settings: Settings, filename: str) -> Path:
    """Confined path inside the reports directory; never overwrites."""
    target = (settings.reports_dir / filename).resolve()
    reports_root = settings.reports_dir.resolve()
    if reports_root != target.parent and reports_root not in target.parents:
        raise ToolError("invalid_argument", "report filename must stay inside the reports directory")
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for counter in range(1, 1000):
        candidate = target.with_name(f"{stem}-{counter}{suffix}")
        if not candidate.exists():
            return candidate
    raise ToolError("storage_error", f"could not find a free filename for {filename}")


def render_change_report(
    *,
    target_ids: list[int] | None = None,
    project: str = "",
    limit: int = 50,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Write a Markdown monitoring report (evidence-linked, failure-honest)."""
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        clauses: list[str] = []
        params: list[Any] = []
        if target_ids:
            clauses.append(f"id IN ({', '.join('?' for _ in target_ids)})")
            params.extend(int(item) for item in target_ids)
        if project:
            clauses.append("project = ?")
            params.append(project.strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(f"SELECT * FROM monitor_targets {where} ORDER BY id", params).fetchall()
        targets = []
        for row in rows:
            latest = _latest_snapshot(conn, int(row["id"]))
            history = conn.execute(
                "SELECT * FROM monitor_snapshots WHERE target_id = ? AND status IN ('changed') "
                "ORDER BY id DESC LIMIT ?",
                (int(row["id"]), min(max(int(limit), 1), 200)),
            ).fetchall()
            targets.append(
                {
                    "target": _target_payload(row),
                    "latest": _snapshot_payload(
                        latest, source=_source_ref(conn, latest["source_id"]) if latest else None
                    ),
                    "changes": [
                        _snapshot_payload(item, source=_source_ref(conn, item["source_id"]))
                        for item in history
                    ],
                }
            )
    if not targets:
        return {
            "created": False,
            "path": None,
            "markdown": "",
            "warnings": ["no monitoring targets matched"],
        }

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [
        "# Monitoring change report",
        "",
        f"*Generated {stamp} by agletras-mcp · {len(targets)} target(s) · "
        "content comparison after normalization (navigation, ads, cookie notices, and bare "
        "timestamps are classified as boilerplate and never treated as changes)*",
        "",
        "## Targets",
        "",
    ]
    for entry in targets:
        target = entry["target"]
        latest = entry["latest"]
        status = (latest or {}).get("status") or "never checked"
        state = "meaningful change" if (latest or {}).get("meaningful") else status
        lines.append(
            f"- **{target['name'] or target['target']}** (`{target['kind']}` id {target['target_id']}"
            + (f"; project `{target['project']}`" if target["project"] else "")
            + f") — last check: {latest['checked_at'] if latest else 'never'}; {state}"
        )
    lines.append("")

    lines.extend(["## Detected changes", ""])
    change_points: list[tuple[str, int]] = []
    any_change = False
    for entry in targets:
        target = entry["target"]
        for change in entry["changes"]:
            any_change = True
            added_count = len(change.get("added_terms") or [])
            removed_count = len(change.get("removed_terms") or [])
            change_points.append((change["checked_at"][:10], added_count + removed_count))
            source = change.get("source") or {}
            lines.append(
                f"### {target['name'] or target['target']} — {change['checked_at']}"
            )
            lines.append("")
            lines.append(f"- **Change:** {change['summary']}")
            lines.append(
                f"- **Evidence (new):** source {source.get('source_id')} "
                f"(content hash `{(source.get('content_hash') or '')[:16]}`, retrieved {source.get('retrieved_at')})"
            )
            previous = change.get("previous_snapshot_id")
            lines.append(f"- **Compared against snapshot:** {previous if previous else 'none (baseline)'}")
            if change.get("added_terms"):
                lines.append("- **Added (substantive):**")
                lines.extend(f"  - {_clip(item)}" for item in change["added_terms"][:10])
            if change.get("removed_terms"):
                lines.append("- **Removed (substantive):**")
                lines.extend(f"  - {_clip(item)}" for item in change["removed_terms"][:10])
            if change.get("ignored_boilerplate"):
                lines.append(
                    "- **Boilerplate also differed (ignored):** "
                    + "; ".join(_clip(item) for item in change["ignored_boilerplate"][:3])
                )
            lines.append("")
    if not any_change:
        lines.append("No meaningful changes were detected for these targets.")
        lines.append("")

    boilerplate_only = [
        entry
        for entry in targets
        if (entry["latest"] or {}).get("status") == "changed"
        and not (entry["latest"] or {}).get("meaningful")
    ]
    if boilerplate_only:
        lines.extend(["## Boilerplate-only differences (not treated as changes)", ""])
        for entry in boilerplate_only:
            latest = entry["latest"]
            lines.append(
                f"- **{entry['target']['name'] or entry['target']['target']}** "
                f"({latest['checked_at']}): {latest['summary']}"
            )
        lines.append("")

    failures = [entry for entry in targets if (entry["latest"] or {}).get("status") == "failed"]
    lines.extend(["## Failed checks (not interpreted as deletion)", ""])
    if failures:
        for entry in failures:
            latest = entry["latest"]
            lines.append(
                f"- **{entry['target']['name'] or entry['target']['target']}**: "
                f"{latest.get('error_code')}: {latest.get('error_message')} "
                "(the previously stored evidence is unchanged; no deletion is inferred)"
            )
    else:
        lines.append("No retrieval failures were recorded for these targets.")
    lines.append("")

    chart = _svg_change_chart(change_points[-12:], "Changed lines per detected change")
    if chart:
        lines.extend(
            [
                "## Change activity",
                "",
                "*Changed substantive lines per change event (newest last); values are the exact "
                "line counts recorded in the snapshots above.*",
                "",
                chart,
                "",
            ]
        )

    lines.extend(
        [
            "## Method and limitations",
            "",
            "- Comparison runs on extracted readable text after whitespace normalization and "
            "boilerplate classification; identical normalized text never produces a change.",
            "- Conditional requests (`If-None-Match`/`If-Modified-Since`) are used when stored; a "
            "`304` is recorded as `not_modified`.",
            "- Failed retrievals are recorded as failures. No change, deletion, or content state is "
            "inferred from a failure.",
            "- No URL liveness check is performed beyond the recorded checks; a target may have "
            "changed between checks without being observed.",
            "",
        ]
    )
    markdown = "\n".join(lines)
    stamp_file = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    scope = "all" if not target_ids else "-".join(str(item) for item in target_ids[:3])
    filename = f"monitor-report-{scope}-{stamp_file}.md"
    path = _unique_report_path(settings, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return {"created": True, "path": str(path), "markdown": markdown, "targets": len(targets), "warnings": []}


def _clip(text: str, limit: int = 200) -> str:
    clean = re.sub(r"\s+", " ", str(text)).strip()
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"

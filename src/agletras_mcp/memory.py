"""Phase 8 project memory: cross-run recall over the existing evidence store.

Memory is not a second database and not a vector index: it is the same SQLite
evidence library (sources, contents, passages, claims, runs) queried *across
research runs*, with two explicit rules:

1. **Every hit is stored evidence.** Recall returns exact stored passages with
   their source id, run links, content hash, and retrieval timestamp. Nothing
   is synthesized, summarized, or invented.
2. **Freshness is explicit.** Each hit carries ``age_days`` computed from the
   stored retrieval timestamp. When a ``max_age_days`` bound is given, hits
   with a *known* age beyond the bound are excluded (and counted); hits whose
   timestamp is missing are kept and flagged ``fresh: null`` (unknown), never
   silently dropped.

``project`` scoping uses the Phase 8 ``research_runs.project`` label: a source
belongs to a project when at least one of its linked runs carries that label.
This keeps unrelated research streams from bleeding into each other without a
second store.

:func:`find_reusable` answers the pipeline's reuse question: *does the library
already hold a fresh, in-scope copy of this URL/path that can be linked into a
new run instead of fetching it again?* It only ever reuses stored sources.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import canonurl, evidence, localfs, storage
from .config import Settings, load_settings
from .errors import ToolError

#: Sources whose extraction failed outright are not reusable as memory.
_USABLE_EXTRACTION = ("success", "partial")


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def age_days(raw: str | None, *, now: datetime | None = None) -> float | None:
    """Whole-day age of an ISO timestamp, or ``None`` when unparsable/absent."""
    parsed = _parse_ts(raw)
    if parsed is None:
        return None
    moment = now or datetime.now(timezone.utc)
    return max((moment - parsed).total_seconds(), 0.0) / 86_400.0


def _source_links(conn: sqlite3.Connection, source_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Run/project links per source id (deterministic order)."""
    if not source_ids:
        return {}
    placeholders = ", ".join("?" for _ in source_ids)
    rows = conn.execute(
        f"""
        SELECT rrs.source_id, rrs.run_id, r.project, r.started_at
        FROM research_run_sources rrs
        JOIN research_runs r ON r.id = rrs.run_id
        WHERE rrs.source_id IN ({placeholders})
        ORDER BY rrs.run_id
        """,
        [int(item) for item in source_ids],
    ).fetchall()
    links: dict[int, dict[str, Any]] = {}
    for row in rows:
        entry = links.setdefault(
            int(row["source_id"]), {"run_ids": [], "projects": [], "last_run_at": None}
        )
        run_id = int(row["run_id"])
        if run_id not in entry["run_ids"]:
            entry["run_ids"].append(run_id)
        project = (row["project"] or "").strip()
        if project and project not in entry["projects"]:
            entry["projects"].append(project)
        started = row["started_at"]
        if started and (entry["last_run_at"] is None or started > entry["last_run_at"]):
            entry["last_run_at"] = started
    return links


def recall(
    query: str,
    *,
    project: str = "",
    limit: int = 10,
    max_age_days: int = 0,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Search stored passages from every run, with provenance and freshness.

    Ranking and matching come from :func:`evidence.search_evidence` (FTS5 with
    a LIKE fallback); recall adds run/project links and age, then applies the
    optional project and freshness filters without rewriting any passage.
    """
    settings = settings or load_settings()
    bounded = min(max(int(limit), 1), settings.max_list_limit)
    # Over-fetch so post-filtering (project/freshness) can still fill `limit`.
    candidates = evidence.search_evidence(
        query, limit=settings.max_list_limit, run_id=0, settings=settings
    )
    passages = list(candidates.get("passages", []))
    with storage.open_db(settings) as conn:
        links = _source_links(conn, [int(item["source_id"]) for item in passages])
    hits: list[dict[str, Any]] = []
    excluded_project = 0
    excluded_stale = 0
    for passage in passages:
        source_id = int(passage["source_id"])
        link = links.get(source_id, {"run_ids": [], "projects": [], "last_run_at": None})
        if project and project.strip() not in link["projects"]:
            excluded_project += 1
            continue
        age = age_days(passage.get("retrieved_at") or link.get("last_run_at"))
        if max_age_days and max_age_days > 0:
            # Bounded freshness: only a known timestamp can prove a hit fresh.
            fresh: bool | None = None if age is None else age <= float(max_age_days)
        else:
            fresh = True if age is not None else None
        if fresh is False:
            excluded_stale += 1
            continue
        hits.append(
            {
                **passage,
                "run_ids": link["run_ids"],
                "projects": link["projects"],
                "age_days": None if age is None else round(age, 3),
                "fresh": fresh,
            }
        )
        if len(hits) >= bounded:
            break
    return {
        "query": query,
        "engine": candidates.get("engine"),
        "project": project.strip(),
        "max_age_days": int(max_age_days or 0),
        "scanned": len(passages),
        "count": len(hits),
        "excluded_by_project": excluded_project,
        "excluded_as_stale": excluded_stale,
        "passages": hits,
    }


def recall_for_question(
    question: str,
    *,
    project: str = "",
    limit: int = 10,
    max_age_days: int = 0,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Recall passages relevant to a research question (terms-based query)."""
    text = (question or "").strip()
    if not text:
        raise ToolError("invalid_argument", "question must not be empty")
    terms = evidence.extract_terms(text, max_terms=12)
    query = " ".join(terms[:8]) if terms else text
    outcome = recall(
        query, project=project, limit=limit, max_age_days=max_age_days, settings=settings
    )
    return {**outcome, "question": text, "query": query}


def find_reusable(
    source: str,
    *,
    project: str = "",
    max_age_days: int = 0,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """A stored, usable, in-scope copy of *source* identity — or a reason why not."""
    settings = settings or load_settings()
    raw = (source or "").strip()
    if not raw:
        raise ToolError("invalid_argument", "source must not be empty")
    if re.match(r"(?i)^https?://", raw):
        identity = canonurl.canonicalize_url(raw)
        if identity is None:
            raise ToolError("invalid_argument", f"Not a usable http(s) URL: {raw!r}")
        kind = "url"
    else:
        try:
            resolved = localfs.resolve_allowed(raw, settings)
        except ToolError as exc:
            # Missing/outside-root local paths simply cannot be reused.
            return {"found": False, "reason": f"the path cannot be reused: {exc.message}", "kind": "path"}
        identity = canonurl.normalize_local_path(resolved)
        kind = "path"
    with storage.open_db(settings) as conn:
        row = conn.execute(
            """
            SELECT s.* FROM source_identities i
            JOIN sources s ON s.id = i.source_id
            WHERE i.identity = ?
            """,
            (identity,),
        ).fetchone()
        if row is None:
            return {"found": False, "reason": "no stored source with this identity", "identity": identity, "kind": kind}
        source_id = int(row["id"])
        link = _source_links(conn, [source_id]).get(
            source_id, {"run_ids": [], "projects": [], "last_run_at": None}
        )
        if (row["extraction_status"] or "success") not in _USABLE_EXTRACTION or not (row["content"] or "").strip():
            return {
                "found": False,
                "reason": "the stored copy has no usable extracted text",
                "identity": identity,
                "kind": kind,
                "source_id": source_id,
            }
        if project and project.strip() not in link["projects"]:
            return {
                "found": False,
                "reason": f"the stored copy is not linked to project {project.strip()!r}",
                "identity": identity,
                "kind": kind,
                "source_id": source_id,
            }
        age = age_days(row["last_checked_at"] or row["retrieved_at"])
        if max_age_days and max_age_days > 0 and (age is None or age > float(max_age_days)):
            return {
                "found": False,
                "reason": (
                    "the stored copy is older than the freshness bound"
                    if age is not None
                    else "the stored copy has no usable timestamp to prove freshness"
                ),
                "identity": identity,
                "kind": kind,
                "source_id": source_id,
                "age_days": None if age is None else round(age, 3),
            }
    return {
        "found": True,
        "reason": "",
        "identity": identity,
        "kind": kind,
        "source_id": source_id,
        "title": row["title"],
        "url": row["url"],
        "path": row["path"],
        "content_hash": row["sha256"],
        "retrieved_at": row["retrieved_at"],
        "last_checked_at": row["last_checked_at"],
        "age_days": None if age is None else round(age, 3),
        "run_ids": link["run_ids"],
        "projects": link["projects"],
        "extraction_status": row["extraction_status"] or "success",
    }


def stats(*, project: str = "", settings: Settings | None = None) -> dict[str, Any]:
    """Counts of what memory holds (optionally scoped to one project)."""
    settings = settings or load_settings()
    scope = project.strip()
    with storage.open_db(settings) as conn:
        if scope:
            project_sources = """
                SELECT s.id FROM sources s
                WHERE EXISTS (
                    SELECT 1 FROM research_run_sources rrs
                    JOIN research_runs r ON r.id = rrs.run_id
                    WHERE rrs.source_id = s.id AND r.project = ?
                )
            """
            sources = int(conn.execute(f"SELECT COUNT(*) AS n FROM ({project_sources})", (scope,)).fetchone()["n"])
            passages = int(
                conn.execute(
                    f"SELECT COUNT(*) AS n FROM passages p WHERE p.source_id IN ({project_sources})",
                    (scope,),
                ).fetchone()["n"]
            )
            claims = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM claims c WHERE EXISTS ("
                    "SELECT 1 FROM research_runs r WHERE r.id = c.run_id AND r.project = ?)",
                    (scope,),
                ).fetchone()["n"]
            )
            runs = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM research_runs WHERE project = ?", (scope,)
                ).fetchone()["n"]
            )
        else:
            sources = int(conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"])
            passages = int(conn.execute("SELECT COUNT(*) AS n FROM passages").fetchone()["n"])
            claims = int(conn.execute("SELECT COUNT(*) AS n FROM claims").fetchone()["n"])
            runs = int(conn.execute("SELECT COUNT(*) AS n FROM research_runs").fetchone()["n"])
    return {
        "project": scope,
        "sources": sources,
        "passages": passages,
        "claims": claims,
        "runs": runs,
    }

"""Phase 8 specialist workers: bounded, role-separated research tasks.

A *specialist* is a deterministic, model-free worker with a fixed role and a
bounded unit of work. Roles and operations:

- ``web_researcher`` — ``search``: one query → ranked candidate URLs;
  ``collect_url``: one URL → stored evidence (Phase 2/3 stack) + run events.
- ``document_analyst`` — ``collect_path``: one permitted local document →
  stored evidence.
- ``image_analyst`` — ``image``: one image query → one analyzed, stored image
  (discovery metadata plus, when a vision endpoint is configured, structured
  visual analysis; never a claim that an image was "seen" without one).
- ``fact_checker`` — ``recheck``: one claim → an independent, read-only
  re-derivation of its verification status from the stored passages. It never
  writes verification rows or citations; disagreements are surfaced for the
  caller to report, never auto-applied.

Execution goes through :func:`parallel.run_tasks`, so the same task list runs
in bounded parallel or on the calling thread (sequential fallback), results
merge in **input order** (deterministic), and one failed worker is reported as
an outcome without aborting the batch. ``run_specialists`` collapses duplicate
source ids and collects warnings; nothing is fabricated when a worker fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import evidence, images, parallel, planner, search, verification
from .config import Settings, load_settings
from .errors import ToolError

#: Stable role names (documented in the README and used in stage details).
ROLES = ("web_researcher", "document_analyst", "image_analyst", "fact_checker")

#: role -> allowed operations.
OPERATIONS: dict[str, tuple[str, ...]] = {
    "web_researcher": ("search", "collect_url"),
    "document_analyst": ("collect_path",),
    "image_analyst": ("image",),
    "fact_checker": ("recheck",),
}


@dataclass(frozen=True)
class Task:
    """One specialist unit of work (immutable; safe to hand to workers)."""

    role: str
    op: str
    task_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    retries: int = 0
    timeout: float | None = None

    def __post_init__(self) -> None:
        if self.role not in OPERATIONS:
            raise ToolError("invalid_argument", f"unknown specialist role {self.role!r}")
        if self.op not in OPERATIONS[self.role]:
            raise ToolError(
                "invalid_argument",
                f"role {self.role!r} does not support operation {self.op!r}",
            )


def search_task(
    *, run_id: int, subquestion_id: int, query: str, max_results: int, round_index: int = 0
) -> Task:
    return Task(
        role="web_researcher",
        op="search",
        task_id=f"s{subquestion_id}:search:{round_index}:{query[:60]}",
        payload={
            "run_id": int(run_id),
            "subquestion_id": int(subquestion_id),
            "query": query,
            "max_results": int(max_results),
            "round": int(round_index),
        },
        retries=1,
    )


def collect_url_task(
    *, run_id: int, subquestion_id: int, url: str, round_index: int = 0, seq: int = 0
) -> Task:
    """One URL-collection unit.

    ``seq`` disambiguates the task id when the same (subquestion, round, URL)
    is admitted more than once (two search phrasings can surface the same
    candidate); the evidence library still de-duplicates at save time.
    """
    return Task(
        role="web_researcher",
        op="collect_url",
        task_id=f"s{subquestion_id}:collect:{round_index}:{int(seq)}:{url[:100]}",
        payload={
            "run_id": int(run_id),
            "subquestion_id": int(subquestion_id),
            "url": url,
            "round": int(round_index),
        },
        retries=1,
    )


def collect_path_task(
    *, run_id: int, subquestion_id: int, path: str, round_index: int = 0
) -> Task:
    return Task(
        role="document_analyst",
        op="collect_path",
        task_id=f"s{subquestion_id}:doc:{round_index}:{path[:120]}",
        payload={
            "run_id": int(run_id),
            "subquestion_id": int(subquestion_id),
            "path": path,
            "round": int(round_index),
        },
        retries=0,
    )


def image_task(*, run_id: int, query: str, max_results: int, round_index: int = 0) -> Task:
    return Task(
        role="image_analyst",
        op="image",
        task_id=f"image:{round_index}:{query[:60]}",
        payload={
            "run_id": int(run_id),
            "query": query,
            "max_results": int(max_results),
            "round": int(round_index),
        },
        retries=0,
    )


def recheck_task(*, run_id: int, claim_id: int) -> Task:
    return Task(
        role="fact_checker",
        op="recheck",
        task_id=f"recheck:{int(claim_id)}",
        payload={"run_id": int(run_id), "claim_id": int(claim_id)},
        retries=0,
    )


# ---------------------------------------------------------------------------
# Workers (deterministic; each returns a plain dict or raises ToolError)
# ---------------------------------------------------------------------------


def _web_search(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    query = str(payload["query"])
    outcome = search.search_web(query, max_results=int(payload["max_results"]), settings=settings)
    urls = [str(item.get("url") or "") for item in outcome["results"] if item.get("url")]
    planner.log_event(
        int(payload["run_id"]),
        "search",
        query,
        subquestion_id=int(payload["subquestion_id"]),
        payload={
            "query": query,
            "provider": outcome["provider"],
            "result_count": outcome["count"],
            "round": int(payload.get("round") or 0),
        },
        settings=settings,
    )
    return {
        "op": "search",
        "query": query,
        "provider": outcome["provider"],
        "count": int(outcome["count"]),
        "urls": urls,
    }


def _collect_source(
    payload: dict[str, Any], settings: Settings, *, source: str, source_kind: str
) -> dict[str, Any]:
    run_id = int(payload["run_id"])
    subquestion_id = int(payload["subquestion_id"])
    round_index = int(payload.get("round") or 0)
    try:
        outcome = evidence.save_evidence(source=source, run_id=run_id, settings=settings)
    except ToolError as exc:
        planner.log_event(
            run_id,
            "source_failed",
            source,
            subquestion_id=subquestion_id,
            payload={"url": source, "error_code": exc.code, "round": round_index},
            settings=settings,
        )
        # A declared failure dict -> the runner records a failed outcome.
        return {"status": "failed", "error": {"code": exc.code, "message": exc.message}}
    source_id = int(outcome["source_id"])
    planner.log_event(
        run_id,
        "source_saved",
        f"saved source {source_id}",
        subquestion_id=subquestion_id,
        source_id=source_id,
        payload={
            "url": outcome.get("final_url") or source,
            "cache_hit": bool(outcome.get("cache_hit")),
            "chars": int(outcome.get("chars") or 0),
            "round": round_index,
            "specialist": source_kind,
        },
        settings=settings,
    )
    return {
        "op": "collect",
        "source_id": source_id,
        "subquestion_id": subquestion_id,
        "source": source,
        "final_url": outcome.get("final_url") or source,
        "cache_hit": bool(outcome.get("cache_hit")),
        "chars": int(outcome.get("chars") or 0),
        "created_source": bool(outcome.get("created_source")),
    }


def _document_collect(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    return _collect_source(
        payload, settings, source=str(payload["path"]), source_kind="document_analyst"
    )


def _image_analyze(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    run_id = int(payload["run_id"])
    query = str(payload["query"])
    found = images.search_images(
        query, max_results=int(payload["max_results"]), settings=settings
    )
    planner.log_event(
        run_id,
        "search",
        f"image search: {query}",
        payload={
            "image_search": True,
            "result_count": found.get("count", 0),
            "round": int(payload.get("round") or 0),
        },
        settings=settings,
    )
    results = list(found.get("results", []))
    if not results:
        return {"status": "failed", "error": {"code": "not_found", "message": "no image results"}}
    result = results[0]
    image_url = result.get("image_url")
    if not image_url:
        return {"status": "failed", "error": {"code": "invalid_output", "message": "result has no image_url"}}
    analysis = images.analyze_image(image_url=image_url, save=True, settings=settings)
    source_id = analysis.get("source_id")
    if not source_id:
        return {"status": "failed", "error": {"code": "no_evidence", "message": "image was not stored"}}
    evidence.save_evidence(source_id=int(source_id), run_id=run_id, settings=settings)
    planner.log_event(
        run_id,
        "evidence",
        f"image evidence saved (source {source_id})",
        source_id=int(source_id),
        payload={"image_url": image_url, "provider": result.get("provider")},
        settings=settings,
    )
    return {
        "op": "image",
        "source_id": int(source_id),
        "image_url": image_url,
        "provider": result.get("provider"),
        "vision_available": bool(analysis.get("vision_available")),
    }


def _fact_recheck(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Re-derive one claim's status from stored passages — read-only."""
    claim = evidence.get_claim(int(payload["claim_id"]), settings=settings)
    if claim is None:
        return {"status": "failed", "error": {"code": "not_found", "message": "no such claim"}}
    run_id = int(payload["run_id"])
    claim_text = claim.get("claim_text") or ""
    citations = list(claim.get("citations", []))
    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()
    for citation in citations:
        passage = citation.get("passage") or {}
        passage_id = int(passage.get("passage_id") or 0)
        if not passage_id or passage_id in seen:
            continue
        seen.add(passage_id)
        candidates.append(
            {
                "passage_id": passage_id,
                "source_id": int((citation.get("source") or {}).get("source_id") or 0),
                "checks": verification.check_claim_against_passage(
                    claim_text, passage.get("text") or ""
                ),
            }
        )
    if claim_text:
        found = evidence.search_evidence(
            claim_text,
            limit=settings.verification_max_candidates,
            run_id=run_id,
            settings=settings,
        )
        for passage in found.get("passages", []):
            passage_id = int(passage["passage_id"])
            if passage_id in seen:
                continue
            seen.add(passage_id)
            candidates.append(
                {
                    "passage_id": passage_id,
                    "source_id": int(passage["source_id"]),
                    "checks": verification.check_claim_against_passage(
                        claim_text, passage.get("text") or ""
                    ),
                }
            )
    citation_summaries = [
        {
            "relation": item.get("relation") or "context",
            "source_id": int((item.get("source") or {}).get("source_id") or 0),
        }
        for item in citations
    ]
    decision = verification.decide_verification(claim_text, citation_summaries, candidates)
    stored_status = (
        claim.get("verification_status") or claim.get("status") or "unverified"
    ).lower()
    recomputed = decision["status"]
    # An unverifiable stored state (never verified) has no status to disagree
    # with; a real verification record must match the fresh derivation.
    comparable = stored_status in (
        "supported",
        "partially_supported",
        "unsupported",
        "unverifiable",
        "contradicted",
        "conflicting_evidence",
    )
    return {
        "op": "recheck",
        "claim_id": int(payload["claim_id"]),
        "stored_status": stored_status,
        "recomputed_status": recomputed,
        "agreement": (not comparable) or recomputed == stored_status,
        "candidates": len(candidates),
        "method": decision.get("method"),
        "rationale": decision.get("rationale"),
    }


_WORKERS = {
    ("web_researcher", "search"): _web_search,
    ("web_researcher", "collect_url"): lambda payload, settings: _collect_source(
        payload, settings, source=str(payload["url"]), source_kind="web_researcher"
    ),
    ("document_analyst", "collect_path"): _document_collect,
    ("image_analyst", "image"): _image_analyze,
    ("fact_checker", "recheck"): _fact_recheck,
}


def dispatch(task: Task, *, settings: Settings) -> dict[str, Any]:
    """Run one task through its role worker. Public for direct/unit use."""
    worker = _WORKERS.get((task.role, task.op))
    if worker is None:  # pragma: no cover - Task validation already prevents this
        raise ToolError("invalid_argument", f"no worker for {task.role!r}/{task.op!r}")
    return worker(task.payload, settings)


def run_specialists(
    tasks: list[Task],
    *,
    parallel_mode: bool = True,
    max_workers: int = 4,
    timeout: float | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Execute a specialist batch; deterministic merge, per-worker isolation.

    Returns per-task outcomes in input order, per-role ok/failed counts, the
    deduplicated source ids produced by successful workers (input order), and
    warnings for every failure. A failed worker never removes another worker's
    result and never aborts the batch.
    """
    settings = settings or load_settings()
    specs = [
        parallel.TaskSpec(
            task_id=task.task_id,
            name=f"{task.role}:{task.op}",
            run=lambda task=task: dispatch(task, settings=settings),
            timeout=task.timeout,
            retries=task.retries,
            metadata={"role": task.role, "op": task.op},
        )
        for task in tasks
    ]
    outcomes = parallel.run_tasks(
        specs, parallel=parallel_mode, max_workers=max_workers, timeout=timeout
    )
    by_role: dict[str, dict[str, int]] = {role: {"ok": 0, "failed": 0} for role in ROLES}
    results: list[dict[str, Any]] = []
    source_ids: list[int] = []
    seen_sources: set[int] = set()
    warnings: list[str] = []
    for task, outcome in zip(tasks, outcomes):
        payload = outcome.as_dict()
        payload["role"] = task.role
        payload["op"] = task.op
        role_counts = by_role.setdefault(task.role, {"ok": 0, "failed": 0})
        if outcome.status == "ok":
            role_counts["ok"] += 1
            result = outcome.result
            source_id = result.get("source_id")
            if source_id and int(source_id) not in seen_sources:
                seen_sources.add(int(source_id))
                source_ids.append(int(source_id))
        else:
            role_counts["failed"] += 1
            warnings.append(
                f"{task.role} {task.task_id}: {outcome.error_code}: {outcome.error_message}"
            )
        results.append(payload)
    return {
        "parallel": bool(parallel_mode),
        "task_count": len(tasks),
        "ok": sum(item["ok"] for item in by_role.values()),
        "failed": sum(item["failed"] for item in by_role.values()),
        "counts_by_role": {role: counts for role, counts in by_role.items() if counts["ok"] or counts["failed"]},
        "source_ids": source_ids,
        "results": results,
        "warnings": warnings,
    }

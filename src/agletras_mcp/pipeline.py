"""Deterministic, model-free research pipeline for ``agletras run`` (Phase 7).

The MCP server deliberately leaves *judgement* to the OpenCode agent. The CLI
cannot call a model, so this pipeline only uses deterministic components that
already exist: the free search adapter (DuckDuckGo HTML / Wikipedia API), the
source-collection and evidence stack (dedupe, cache, passages), the planner's
enforced limits and append-only execution log, the Phase 6 deterministic claim
extraction/verification, and the Phase 6 synthesis report.

Stage sequence (per run; each stage is checkpointed so a resume skips the ones
already complete):

``plan`` → round 0: ``search`` → ``collect`` → (``images``) → ``claims`` →
``verify`` → ``evaluate`` → optional follow-up rounds → ``report``.

Every network or extraction failure is logged and reported, never hidden: a
run with unanswerable subquestions ends as ``partial`` with the gaps in the
report; a run with no evidence at all ends as ``failed``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import evidence, memory, planner, runs, specialists, storage, synthesis, verification
from .config import DEPTH_PRESETS, DEPTHS, VERIFICATION_METHODS, Settings, load_settings
from .errors import ToolError
from .progress import ProgressReporter

#: Terminal exit codes used by the CLI (documented in README and --help).
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DEPENDENCY = 3
EXIT_RESEARCH_FAILED = 4
EXIT_PARTIAL = 5
EXIT_INTERRUPTED = 130

#: Hard safety caps for CLI-resolved budgets (mirrors planner.LIMIT_CEILINGS).
_MAX_SOURCES_CEILING = 100
_MAX_CLAIMS_CEILING = 50
_MAX_ROUNDS_CEILING = 5
_MAX_SEARCHES = 3
_MAX_RESULTS_PER_SEARCH = 10
_MAX_QUERY_VARIANTS = 6


class PipelineInterrupted(Exception):
    """Raised when the process receives SIGTERM (Ctrl+C raises KeyboardInterrupt)."""


@dataclass
class RunOptions:
    """CLI research options, resolved against depth presets and settings."""

    question: str
    depth: str = "standard"
    max_sources: int = 0
    max_claims: int = 0
    searches_per_subquestion: int = 0
    followup_rounds: int = -1
    results_per_search: int = 0
    verification_method: str = "auto"
    include_images: bool = False
    image_results: int = 0
    subquestions: tuple[str, ...] = ()
    title: str = ""
    # --- Phase 8: execution mode (never changes what counts as evidence) ----
    parallel: bool = False
    reuse_evidence: bool = False
    project: str = ""

    def resolved(self, settings: Settings) -> "RunOptions":
        """Fill preset/auto values and clamp everything to hard ceilings."""
        if self.depth not in DEPTHS:
            raise ToolError("invalid_argument", f"depth must be one of {', '.join(DEPTHS)}; got {self.depth!r}")
        method = (self.verification_method or "auto").strip().lower()
        if method not in VERIFICATION_METHODS:
            raise ToolError(
                "invalid_argument",
                f"verification method must be one of {', '.join(VERIFICATION_METHODS)}; got {self.verification_method!r}",
            )
        preset = DEPTH_PRESETS[self.depth]
        max_sources = self.max_sources or preset["max_sources"]
        max_claims = self.max_claims or preset["max_claims"]
        searches = self.searches_per_subquestion or preset["searches_per_subquestion"]
        followups = preset["followup_rounds"] if self.followup_rounds < 0 else self.followup_rounds
        results = self.results_per_search or preset["results_per_search"]
        return RunOptions(
            question=self.question,
            depth=self.depth,
            max_sources=min(max(int(max_sources), 1), _MAX_SOURCES_CEILING),
            max_claims=min(max(int(max_claims), 1), _MAX_CLAIMS_CEILING, settings.verification_max_claims),
            searches_per_subquestion=min(max(int(searches), 1), _MAX_SEARCHES),
            followup_rounds=min(max(int(followups), 0), _MAX_ROUNDS_CEILING),
            results_per_search=min(max(int(results), 1), _MAX_RESULTS_PER_SEARCH, settings.max_search_results),
            verification_method=method,
            include_images=bool(self.include_images),
            image_results=min(max(int(self.image_results or settings.research_image_results), 1), settings.image_max_results),
            subquestions=tuple(part.strip() for part in self.subquestions if part.strip()),
            title=self.title.strip(),
            parallel=bool(self.parallel),
            reuse_evidence=bool(self.reuse_evidence),
            project=(self.project or "").strip(),
        )

    def config_options(self) -> dict[str, Any]:
        """The persisted/compatibility-relevant subset (see runs.INCOMPATIBLE_KEYS)."""
        return {
            "depth": self.depth,
            "max_sources": self.max_sources,
            "max_claims": self.max_claims,
            "searches_per_subquestion": self.searches_per_subquestion,
            "followup_rounds": self.followup_rounds,
            "results_per_search": self.results_per_search,
            "verification_method": self.verification_method,
            "include_images": self.include_images,
            "subquestions": list(self.subquestions),
            "parallel": self.parallel,
            "reuse_evidence": self.reuse_evidence,
            "project": self.project,
        }


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------


def query_variants(question: str, *, limit: int = _MAX_QUERY_VARIANTS) -> list[str]:
    """Deterministic search phrasings for one subquestion (no model involved).

    Variants: the raw question, the significant terms joined, then shorter
    leading term windows (4, 2). Duplicates and empty variants are dropped.
    This is a *rephrasing* budget, not query magic: follow-up rounds consume
    the next unused variant and stop honestly when none remains.
    """
    text = (question or "").strip()
    variants: list[str] = [text] if text else []
    terms = evidence.extract_terms(text, max_terms=12)
    if terms:
        candidates = [" ".join(terms)]
        if len(terms) > 4:
            candidates.append(" ".join(terms[:4]))
        if len(terms) > 2:
            candidates.append(" ".join(terms[:2]))
        for candidate in candidates:
            if candidate.lower() != text.lower() and candidate not in variants:
                variants.append(candidate)
    return variants[:limit]


def _count_claim_rows(run_id: int, settings: Settings) -> int:
    with storage.open_db(settings) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM claims WHERE run_id = ?", (int(run_id),)
        ).fetchone()
    return int(row["n"])


def _count_rounds(run_id: int, settings: Settings) -> int:
    with storage.open_db(settings) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM research_run_events WHERE run_id = ? AND kind = 'followup_round'",
            (int(run_id),),
        ).fetchone()
    return int(row["n"])


def _subquestion_source_map(run_id: int, settings: Settings) -> dict[int, list[int]]:
    """Subquestion id -> saved source ids (from ``source_saved`` run events)."""
    with storage.open_db(settings) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT subquestion_id, source_id
            FROM research_run_events
            WHERE run_id = ? AND kind = 'source_saved'
              AND subquestion_id IS NOT NULL AND source_id IS NOT NULL
            """,
            (int(run_id),),
        ).fetchall()
    mapping: dict[int, list[int]] = {}
    for row in rows:
        mapping.setdefault(int(row["subquestion_id"]), []).append(int(row["source_id"]))
    return mapping


def _run_source_count(run_id: int, settings: Settings) -> int:
    with storage.open_db(settings) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM research_run_sources WHERE run_id = ?", (int(run_id),)
        ).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------


class ResearchSession:
    """Executes (or resumes) the deterministic pipeline for one run."""

    def __init__(
        self,
        run_id: int,
        options: RunOptions,
        settings: Settings,
        reporter: ProgressReporter,
        *,
        restart: bool = False,
    ) -> None:
        self.run_id = int(run_id)
        self.options = options.resolved(settings)
        self.settings = settings
        self.reporter = reporter
        self.restart = bool(restart)
        self.states = runs.stage_states(self.run_id, settings=settings)
        self.results: dict[str, dict[str, Any]] = {}
        self.warnings: list[str] = []

    # -- stage plumbing -----------------------------------------------------

    def stage_done(self, stage: str) -> bool:
        if self.restart:
            return False
        state = self.states.get(stage)
        return bool(state and state["status"] == "complete")

    def run_stage(self, stage: str, action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Run one stage with checkpointing; returns the stage detail dict.

        Already-complete stages are skipped and their stored detail is
        returned, so a resumed run never repeats finished work.
        """
        if self.stage_done(stage):
            self.reporter.stage_skip(stage)
            return self.states[stage].get("detail", {})
        self.reporter.stage_start(stage)
        checkpoint = runs.start_checkpoint(self.run_id, stage, settings=self.settings)
        attempt = checkpoint["attempt"]
        started = time.monotonic()
        try:
            detail = action()
        except KeyboardInterrupt:
            runs.finish_checkpoint(
                self.run_id,
                stage,
                "interrupted",
                attempt=attempt,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_code="interrupted",
                error_message="interrupted by user",
                settings=self.settings,
            )
            raise
        except PipelineInterrupted as exc:
            runs.finish_checkpoint(
                self.run_id,
                stage,
                "interrupted",
                attempt=attempt,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_code="terminated",
                error_message=str(exc),
                settings=self.settings,
            )
            raise
        except ToolError as exc:
            runs.finish_checkpoint(
                self.run_id,
                stage,
                "failed",
                attempt=attempt,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_code=exc.code,
                error_message=exc.message,
                settings=self.settings,
            )
            self.reporter.error(f"{stage}: {exc.message}", code=exc.code, stage=stage)
            raise
        except Exception as exc:  # noqa: BLE001 - checkpoint then re-raise
            runs.finish_checkpoint(
                self.run_id,
                stage,
                "failed",
                attempt=attempt,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_code="internal",
                error_message=f"{exc.__class__.__name__}: {exc}",
                settings=self.settings,
            )
            self.reporter.error(
                f"{stage}: unexpected {exc.__class__.__name__}: {exc}", code="internal", stage=stage
            )
            raise
        duration_ms = int((time.monotonic() - started) * 1000)
        runs.finish_checkpoint(
            self.run_id,
            stage,
            "complete",
            attempt=attempt,
            duration_ms=duration_ms,
            detail=detail,
            settings=self.settings,
        )
        self.states[stage] = {
            "stage": stage,
            "status": "complete",
            "attempt": attempt,
            "detail": detail,
        }
        self.results[stage] = detail
        self.reporter.stage_end(stage, "complete", duration_ms=duration_ms, **self._stage_summary(stage, detail))
        return detail

    def _stage_summary(self, stage: str, detail: dict[str, Any]) -> dict[str, Any]:
        """Short, safe numbers for the human/JSON progress line."""
        keys_by_stage = {
            "plan": ("subquestion_count",),
            "search": ("queries", "result_count"),
            "collect": ("attempted", "saved", "failed", "cache_hits"),
            "images": ("images_saved",),
            "claims": ("registered", "claim_count"),
            "verify": ("verified_count",),
            "evaluate": ("gap_count", "answered"),
            "report": ("report_id", "validation_status"),
        }
        summary: dict[str, Any] = {}
        for key in keys_by_stage.get(stage.split("@")[0], ()):
            if key in detail:
                summary[key] = detail[key]
        return summary

    def stage_result(self, stage: str) -> dict[str, Any]:
        if stage in self.results:
            return self.results[stage]
        state = self.states.get(stage)
        return state.get("detail", {}) if state else {}

    # -- stages -------------------------------------------------------------

    def stage_plan(self) -> dict[str, Any]:
        subquestions = list(self.options.subquestions) or [self.options.question]
        candidate_cap = min(
            max(2, self.options.searches_per_subquestion * self.options.results_per_search),
            planner.LIMIT_CEILINGS["max_candidates_per_subquestion"],
        )
        limits = {
            "max_subquestions": min(max(len(subquestions), 1), planner.LIMIT_CEILINGS["max_subquestions"]),
            # The Phase 4 planner rejects a 0 limit (its bounds start at 1), so
            # a 0-round budget is stored as the minimum representable bound and
            # enforced by the run loop below, which reads options.followup_rounds
            # directly. Depth presets therefore stay honest even at 0.
            "max_followup_rounds": max(
                1, min(self.options.followup_rounds, planner.LIMIT_CEILINGS["max_followup_rounds"])
            ),
            "max_unique_sources": min(self.options.max_sources, _MAX_SOURCES_CEILING),
            "max_candidates_per_subquestion": candidate_cap,
        }
        scope = (
            "Evidence gathered by the model-free `agletras run` pipeline "
            f"(depth '{self.options.depth}') from the configured search provider and directly "
            "retrievable web sources. No language model is used for planning, judgement, or prose."
        )
        assumptions = [
            "Subquestions are the full request unless --subquestions was given; "
            "no model decomposes the question.",
            f"Budgets: {limits['max_unique_sources']} sources, up to "
            f"{limits['max_followup_rounds']} follow-up round(s), {self.options.searches_per_subquestion} "
            "search phrasing(s) per subquestion in the first round.",
        ]
        plan = planner.save_plan(
            self.run_id,
            objective=self.options.question,
            subquestions=[
                {"question": question, "priority": index}
                for index, question in enumerate(subquestions, start=1)
            ],
            scope=scope,
            assumptions=assumptions,
            limits=limits,
            settings=self.settings,
        )
        return {
            "subquestions": [
                {
                    "subquestion_id": item["subquestion_id"],
                    "question": item["question"],
                    "seq": item["seq"],
                }
                for item in plan["subquestions"]
            ],
            "limits": plan["limits"],
            "subquestion_count": len(plan["subquestions"]),
        }

    def stage_search(self, round_index: int) -> dict[str, Any]:
        """Run this round's search phrasings through web_researcher workers.

        In parallel mode the (subquestion, query) units run concurrently with a
        bounded pool; the merge below walks results in build order, so the
        stored run log and stage detail are identical to a sequential run.
        """
        plan = self.stage_result("plan")
        subquestions: list[dict[str, Any]] = list(plan.get("subquestions", []))
        targets = self._round_targets(round_index, subquestions)
        if round_index > 0:
            self._start_followup_rounds(round_index, targets)
        # Which variant slot each subquestion uses this round.
        slot = self.options.searches_per_subquestion + round_index
        tasks: list[specialists.Task] = []
        contexts: list[dict[str, Any]] = []
        for subquestion in targets:
            variants = query_variants(subquestion["question"])
            queries: list[str] = []
            if round_index > 0:
                # A follow-up round consumes the next unused variant only.
                if len(variants) >= slot + 1:
                    queries = [variants[slot]]
            else:
                queries = variants[: self.options.searches_per_subquestion]
            if not queries:
                self.warnings.append(
                    f"subquestion {subquestion['subquestion_id']}: no new query phrasing "
                    f"available at depth '{self.options.depth}'"
                )
                continue
            for query in queries:
                self.reporter.info(
                    "search", query, stage=f"search@{round_index}", data={"subquestion_id": subquestion["subquestion_id"]}
                )
                contexts.append(
                    {
                        "subquestion_id": int(subquestion["subquestion_id"]),
                        "query": query,
                    }
                )
                tasks.append(
                    specialists.search_task(
                        run_id=self.run_id,
                        subquestion_id=int(subquestion["subquestion_id"]),
                        query=query,
                        max_results=self.options.results_per_search,
                        round_index=round_index,
                    )
                )
        batch = specialists.run_specialists(
            tasks,
            parallel_mode=self.options.parallel,
            max_workers=self.settings.collect_concurrency,
            settings=self.settings,
        )
        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for context, outcome in zip(contexts, batch["results"]):
            if outcome["status"] == "ok":
                urls = [str(url) for url in outcome["result"].get("urls", []) if url]
                results.append(
                    {
                        "subquestion_id": context["subquestion_id"],
                        "query": context["query"],
                        "urls": urls[: self.options.results_per_search],
                    }
                )
                continue
            error_code = outcome.get("error_code") or "internal"
            error_message = outcome.get("error_message") or "search failed"
            failures.append(
                {"subquestion_id": context["subquestion_id"], "query": context["query"], "error_code": error_code}
            )
            planner.log_event(
                self.run_id,
                "error",
                f"search failed: {context['query']}",
                subquestion_id=context["subquestion_id"],
                payload={"query": context["query"], "error_code": error_code, "round": round_index},
                settings=self.settings,
            )
            self.reporter.warn(f"search failed ({error_code}): {error_message}")
        return {
            "round": round_index,
            "queries": len(results),
            "result_count": sum(len(item["urls"]) for item in results),
            "failures": failures,
            "results": results,
            "parallel": self.options.parallel,
            "specialists": batch["counts_by_role"],
        }

    def _round_targets(self, round_index: int, subquestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if round_index == 0:
            return subquestions
        gaps = self.stage_result("evaluate").get("gaps", [])
        gap_ids = {int(item["subquestion_id"]) for item in gaps}
        return [item for item in subquestions if int(item["subquestion_id"]) in gap_ids]

    def _start_followup_rounds(self, round_index: int, targets: list[dict[str, Any]]) -> None:
        """Open the follow-up round exactly once, even across resumes."""
        if not targets:
            return
        if _count_rounds(self.run_id, self.settings) >= round_index:
            return  # already logged before an interruption; do not duplicate
        reason = (
            f"round {round_index}: {len(targets)} subquestion(s) remain unanswered or only "
            "partially answered after the previous round; trying the next query phrasing."
        )
        first = targets[0]
        planner.start_followup_round(
            self.run_id, reason, subquestion_id=int(first["subquestion_id"]), settings=self.settings
        )
        self.reporter.info("followup_round", reason, stage=f"search@{round_index}")

    def stage_collect(self, round_index: int) -> dict[str, Any]:
        """Collect this round's candidates, one bounded unit per URL.

        Candidate admission (per-subquestion caps) stays a single sequential
        pre-pass — the cap is read-then-write, so it must not race. Execution
        then proceeds in budget windows of at most ``remaining`` units at a
        time: every unit in a window could in principle succeed, so parallel
        saving can never exceed the run's source budget; units that fail free
        their slot for the next window, exactly like the sequential loop.
        With ``reuse_evidence`` a fresh stored copy of a candidate is linked
        into this run from project memory instead of being fetched again.
        """
        search_detail = self.stage_result(f"search@{round_index}")
        plan = self.stage_result("plan")
        question_by_id = {
            int(item["subquestion_id"]): item["question"] for item in plan.get("subquestions", [])
        }
        slots = max(self.options.max_sources - _run_source_count(self.run_id, self.settings), 0)
        budget_exhausted = False
        # Admission pass: ordered candidate URLs with the candidate-cap event
        # logged exactly once per admission (the same order as Phase 7).
        candidates: list[dict[str, Any]] = []
        for item in search_detail.get("results", []):
            subquestion_id = int(item["subquestion_id"])
            for url in item.get("urls", []):
                try:
                    planner.log_event(
                        self.run_id,
                        "source_candidate",
                        url,
                        subquestion_id=subquestion_id,
                        payload={"url": url, "round": round_index},
                        settings=self.settings,
                    )
                except ToolError as exc:
                    # Candidate cap reached for this subquestion/round: skip politely.
                    self.warnings.append(f"candidate limit reached for subquestion {subquestion_id}: {exc.message}")
                    break
                candidates.append({"subquestion_id": subquestion_id, "url": url})
        attempted = saved = failed = cache_hits = reused = 0
        saved_by_index: dict[int, dict[str, Any]] = {}
        rejected_reuse = 0
        cursor = 0
        while cursor < len(candidates) and slots > 0:
            window = candidates[cursor:cursor + slots]
            to_fetch: list[tuple[int, dict[str, Any]]] = []
            for offset, candidate in enumerate(window):
                index = cursor + offset
                url = candidate["url"]
                subquestion_id = int(candidate["subquestion_id"])
                attempted += 1
                if self.options.reuse_evidence:
                    hit = memory.find_reusable(
                        url,
                        project=self.options.project,
                        max_age_days=self.settings.research_reuse_max_age_days,
                        settings=self.settings,
                    )
                    if hit.get("found"):
                        try:
                            evidence.save_evidence(
                                source_id=int(hit["source_id"]),
                                run_id=self.run_id,
                                notes="reused from project memory",
                                settings=self.settings,
                            )
                        except ToolError:
                            rejected_reuse += 1  # fall back to a real fetch
                        else:
                            slots -= 1
                            saved += 1
                            reused += 1
                            source_id = int(hit["source_id"])
                            saved_by_index[index] = {
                                "source_id": source_id,
                                "subquestion_id": subquestion_id,
                                "url": url,
                                "reused": True,
                            }
                            planner.log_event(
                                self.run_id,
                                "source_saved",
                                f"reused stored source {source_id} for {url}",
                                subquestion_id=subquestion_id,
                                source_id=source_id,
                                payload={
                                    "url": url,
                                    "reused": True,
                                    "age_days": hit.get("age_days"),
                                    "round": round_index,
                                },
                                settings=self.settings,
                            )
                            self.reporter.info(
                                "reuse",
                                url,
                                stage=f"collect@{round_index}",
                                data={"source_id": source_id, "subquestion_id": subquestion_id},
                            )
                            continue
                to_fetch.append((index, candidate))
            tasks = [
                specialists.collect_url_task(
                    run_id=self.run_id,
                    subquestion_id=int(candidate["subquestion_id"]),
                    url=candidate["url"],
                    round_index=round_index,
                    seq=index,
                )
                for index, candidate in to_fetch
            ]
            for index, candidate in to_fetch:
                self.reporter.info(
                    "collect",
                    candidate["url"],
                    stage=f"collect@{round_index}",
                    data={
                        "subquestion_id": int(candidate["subquestion_id"]),
                        "question": question_by_id.get(int(candidate["subquestion_id"]), ""),
                    },
                )
            batch = specialists.run_specialists(
                tasks,
                parallel_mode=self.options.parallel,
                max_workers=self.settings.collect_concurrency,
                settings=self.settings,
            )
            for (index, candidate), outcome in zip(to_fetch, batch["results"]):
                subquestion_id = int(candidate["subquestion_id"])
                if outcome["status"] == "ok":
                    slots -= 1
                    saved += 1
                    source_id = int(outcome["result"]["source_id"])
                    if outcome["result"].get("cache_hit"):
                        cache_hits += 1
                    saved_by_index[index] = {
                        "source_id": source_id,
                        "subquestion_id": subquestion_id,
                        "url": candidate["url"],
                        "reused": False,
                    }
                    continue
                failed += 1
                error_code = outcome.get("error_code") or "internal"
                error_message = outcome.get("error_message") or "collection failed"
                self.reporter.warn(f"source failed ({error_code}): {candidate['url']} - {error_message}")
            cursor += len(window)
        if cursor < len(candidates) and slots <= 0:
            budget_exhausted = True
            self.warnings.append(
                f"source budget ({self.options.max_sources}) reached; remaining candidate URLs were skipped"
            )
        saved_events = [saved_by_index[index] for index in sorted(saved_by_index)]
        return {
            "round": round_index,
            "attempted": attempted,
            "saved": saved,
            "failed": failed,
            "cache_hits": cache_hits,
            "reused": reused,
            "reuse_rejected": rejected_reuse,
            "budget_exhausted": budget_exhausted,
            "saved_sources": saved_events[:100],
            "total_run_sources": _run_source_count(self.run_id, self.settings),
            "parallel": self.options.parallel,
        }

    def stage_images(self, round_index: int) -> dict[str, Any]:
        if not self.options.include_images:
            return {"round": round_index, "images_saved": 0, "skipped": "images disabled"}
        # Routed through the image_analyst specialist so image work gets the
        # same bounded execution, per-worker isolation, and parallel/sequential
        # equivalence as every other collection unit.
        task = specialists.image_task(
            run_id=self.run_id,
            query=self.options.question,
            max_results=self.options.image_results,
            round_index=round_index,
        )
        batch = specialists.run_specialists(
            [task],
            parallel_mode=self.options.parallel,
            max_workers=1,
            settings=self.settings,
        )
        outcome = batch["results"][0]
        if outcome["status"] == "ok":
            result = outcome["result"]
            return {
                "round": round_index,
                "images_saved": 1,
                "source_ids": [int(result["source_id"])],
                "vision_available": bool(result.get("vision_available")),
                "parallel": self.options.parallel,
            }
        code = outcome.get("error_code") or "internal"
        message = outcome.get("error_message") or "image analysis failed"
        if code == "not_found":
            # The provider simply had no results; Phase 5 treated this as an
            # empty (not failed) outcome and so does the pipeline.
            return {
                "round": round_index,
                "images_saved": 0,
                "source_ids": [],
                "skipped": "no image results",
                "parallel": self.options.parallel,
            }
        return {
            "round": round_index,
            "images_saved": 0,
            "source_ids": [],
            "errors": [f"image analysis failed ({code}): {message}"],
            "parallel": self.options.parallel,
        }

    def stage_claims(self, round_index: int) -> dict[str, Any]:
        existing = _count_claim_rows(self.run_id, self.settings)
        remaining = self.options.max_claims - existing
        if remaining <= 0:
            self.reporter.info(
                "claims",
                f"claim budget reached ({existing}/{self.options.max_claims}); no new claims registered",
                stage=f"claims@{round_index}",
            )
            return {"round": round_index, "registered": 0, "claim_count": existing, "budget_reached": True}
        outcome = verification.extract_claims(
            self.run_id, max_claims=remaining, register=True, claim_type="claim"
        )
        registered = outcome.get("registered") or []
        return {
            "round": round_index,
            "candidate_count": int(outcome.get("candidate_count") or 0),
            "registered": len(registered),
            "claim_ids": [int(item["claim_id"]) for item in registered],
            "claim_count": _count_claim_rows(self.run_id, self.settings),
        }

    #: Verification statuses that represent a recorded decision (as opposed to
    #: a claim that was never verified); the fact-checker pass only re-checks
    #: claims whose stored status is a real decision.
    _VERIFIED_STATUSES = (
        "supported",
        "partially_supported",
        "unsupported",
        "unverifiable",
        "contradicted",
        "conflicting_evidence",
    )

    def stage_verify(self, round_index: int) -> dict[str, Any]:
        outcome = verification.verify_claims(
            self.run_id,
            apply_citations=True,
            method=self.options.verification_method,
        )
        detail: dict[str, Any] = {
            "round": round_index,
            "verified_count": int(outcome.get("verified_count") or 0),
            "counts_by_status": outcome.get("counts_by_status") or {},
            "methods_used": outcome.get("methods_used") or [],
        }
        recheck = self._recheck_claims()
        if recheck["claims"]:
            detail["recheck"] = recheck
            if recheck["disagree"]:
                summary = ", ".join(
                    f"claim {item['claim_id']}: stored {item['stored_status']}, "
                    f"re-derived {item['recomputed_status']}"
                    for item in recheck["disagreements"]
                )
                message = (
                    f"independent fact-checker pass disagreed for {recheck['disagree']} "
                    f"claim(s) ({summary}); stored verification records were left "
                    "unchanged (disagreements are surfaced, never auto-applied)."
                )
                self.warnings.append(message)
                planner.log_event(
                    self.run_id,
                    "decision",
                    message,
                    payload={
                        "recheck": {
                            "claims": recheck["claims"],
                            "agree": recheck["agree"],
                            "disagree": recheck["disagree"],
                            "failed": recheck["failed"],
                        }
                    },
                    settings=self.settings,
                )
        return detail

    def _recheck_claims(self) -> dict[str, Any]:
        """Read-only fact-checker pass over this run's verified claims.

        Each recheck independently re-derives a claim's status from the stored
        passages (public Phase 6 checks) without writing verification rows or
        citations. Disagreements with the stored record are reported; the
        stored record is never overwritten by this pass.
        """
        claims = evidence.claims_with_citations(self.run_id, settings=self.settings)
        targets = [
            claim
            for claim in claims
            if str(claim.get("verification_status") or claim.get("status") or "").lower()
            in self._VERIFIED_STATUSES
        ][: self.settings.verification_max_claims]
        if not targets:
            return {"claims": 0, "agree": 0, "disagree": 0, "failed": 0, "disagreements": []}
        tasks = [
            specialists.recheck_task(run_id=self.run_id, claim_id=int(claim["claim_id"]))
            for claim in targets
        ]
        batch = specialists.run_specialists(
            tasks,
            parallel_mode=self.options.parallel,
            max_workers=self.settings.collect_concurrency,
            settings=self.settings,
        )
        agree = disagree = failed = 0
        disagreements: list[dict[str, Any]] = []
        for claim, outcome in zip(targets, batch["results"]):
            if outcome["status"] != "ok":
                failed += 1
                continue
            result = outcome["result"]
            if result.get("agreement"):
                agree += 1
                continue
            disagree += 1
            disagreements.append(
                {
                    "claim_id": int(result["claim_id"]),
                    "stored_status": result.get("stored_status"),
                    "recomputed_status": result.get("recomputed_status"),
                    "rationale": result.get("rationale"),
                }
            )
        return {
            "claims": len(targets),
            "agree": agree,
            "disagree": disagree,
            "failed": failed,
            "disagreements": disagreements[:20],
            "parallel": self.options.parallel,
        }

    def stage_evaluate(self, round_index: int) -> dict[str, Any]:
        subquestion_sources = _subquestion_source_map(self.run_id, self.settings)
        claims = evidence.claims_with_citations(self.run_id, settings=self.settings)
        plan = self.stage_result("plan")
        evaluated: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        for item in plan.get("subquestions", []):
            subquestion_id = int(item["subquestion_id"])
            source_ids = set(subquestion_sources.get(subquestion_id, []))
            statuses: list[str] = []
            considered = 0
            for claim in claims:
                citation_sources = {int(c["source_id"]) for c in claim["citations"]}
                if not citation_sources & source_ids:
                    continue
                considered += 1
                statuses.append((claim.get("verification_status") or claim.get("status") or "unverified").lower())
            has_support = any(status in ("supported", "partially_supported") for status in statuses)
            has_conflict = any(status in ("contradicted", "conflicting_evidence") for status in statuses)
            if not source_ids:
                status = "unanswered"
            elif has_support and has_conflict:
                status = "conflicting"
            elif has_support:
                status = "answered"
            elif statuses:
                status = "partially_answered"
            else:
                status = "partially_answered"
            note = (
                f"model-free pipeline: {len(source_ids)} source(s) collected; "
                f"{considered} claim(s) cite them ({', '.join(sorted(set(statuses))) or 'none verified'})."
            )
            planner.update_subquestion(
                self.run_id, subquestion_id, status, note=note, settings=self.settings
            )
            record = {
                "subquestion_id": subquestion_id,
                "question": item["question"],
                "status": status,
                "sources": len(source_ids),
                "claims_considered": considered,
            }
            evaluated.append(record)
            if status != "answered":
                gaps.append(record)
        answered = sum(1 for item in evaluated if item["status"] == "answered")
        return {
            "round": round_index,
            "subquestions": evaluated,
            "gaps": gaps,
            "gap_count": len(gaps),
            "answered": answered,
        }

    def stage_report(self) -> dict[str, Any]:
        outcome = synthesis.generate_markdown_report(
            self.run_id,
            title=self.options.title,
            include_source_assessment=True,
            settings=self.settings,
        )
        validation = outcome.get("validation") or {}
        return {
            "report_id": int(outcome["report_id"]),
            "path": outcome["path"],
            "template_version": outcome.get("template_version", "phase6"),
            "validation_status": outcome.get("validation_status") or validation.get("status", "unvalidated"),
            "validation_passed": bool(validation.get("passed")),
            "claims_rendered": int(outcome.get("claims_rendered") or 0),
            "unresolved_conflicts": int(outcome.get("unresolved_conflicts") or 0),
            "deduplicated": bool(outcome.get("deduplicated")),
        }

    # -- top level ----------------------------------------------------------

    def execute(self) -> dict[str, Any]:
        started = time.monotonic()
        self.reporter.bind_run(self.run_id)
        runs.set_stage(self.run_id, "plan", settings=self.settings)

        self.run_stage("plan", self.stage_plan)
        plan_rounds = int(self.stage_result("plan").get("limits", {}).get("max_followup_rounds", 0))
        # options.followup_rounds is the effective bound (0 means no follow-up
        # round at all); the stored plan limit is only ever lower-bounded at 1.
        max_rounds = min(plan_rounds, self.options.followup_rounds)
        round_index = 0
        while True:
            self.run_stage(f"search@{round_index}", lambda r=round_index: self.stage_search(r))
            self.run_stage(f"collect@{round_index}", lambda r=round_index: self.stage_collect(r))
            if round_index == 0 and self.options.include_images:
                self.run_stage("images@0", lambda: self.stage_images(0))
            self.run_stage(f"claims@{round_index}", lambda r=round_index: self.stage_claims(r))
            self.run_stage(f"verify@{round_index}", lambda r=round_index: self.stage_verify(r))
            evaluate = self.run_stage(f"evaluate@{round_index}", lambda r=round_index: self.stage_evaluate(r))
            gaps = evaluate.get("gaps", [])
            if round_index >= max_rounds or not gaps:
                break
            remaining_rounds = max_rounds - round_index
            if not any(
                len(query_variants(gap["question"])) > self.options.searches_per_subquestion + round_index
                for gap in gaps
            ):
                self.warnings.append(
                    "no unused query phrasing remains for the gap subquestions; stopping follow-up rounds"
                )
                break
            if _count_rounds(self.run_id, self.settings) >= remaining_rounds + round_index:
                break
            round_index += 1

        report = self.run_stage("report", self.stage_report)

        total_sources = _run_source_count(self.run_id, self.settings)
        final_gaps = self.stage_result(f"evaluate@{round_index}").get("gaps", [])
        evaluated = self.stage_result(f"evaluate@{round_index}").get("subquestions", [])
        total = len(evaluated)
        answered = sum(1 for item in evaluated if item["status"] == "answered")
        duration_ms = int((time.monotonic() - started) * 1000)

        if not report.get("validation_passed"):
            reason = (
                f"report {report.get('report_id')} failed integrity validation "
                f"({report.get('validation_status')}); the run is closed as failed"
            )
            planner.log_event(
                self.run_id,
                "stopping",
                reason,
                payload={"status": "failed", "report_id": report.get("report_id")},
                settings=self.settings,
            )
            runs.set_run_status(
                self.run_id, "failed", error=reason, stage="done", settings=self.settings
            )
            return self._result("failed", EXIT_RESEARCH_FAILED, report, total, answered, final_gaps, duration_ms, reason)
        if total_sources == 0:
            reason = "no source could be collected for this request; nothing to report"
            planner.log_event(
                self.run_id,
                "stopping",
                reason,
                payload={"status": "failed", "saved_sources": 0},
                settings=self.settings,
            )
            runs.set_run_status(
                self.run_id, "failed", error=reason, stage="done", settings=self.settings
            )
            return self._result("failed", EXIT_RESEARCH_FAILED, report, total, answered, final_gaps, duration_ms, reason)

        if final_gaps:
            stop_reason = (
                f"Evidence sufficient for {answered} of {total} subquestion(s); "
                f"{len(final_gaps)} remain unanswered or partially answered. "
                f"Depth '{self.options.depth}' with {max_rounds} follow-up round(s) limits exhausted; "
                "the remaining gaps are reported honestly in the report."
            )
            status = "partial"
            exit_code = EXIT_PARTIAL
        else:
            stop_reason = (
                f"All {total} subquestion(s) answered from {total_sources} source(s); "
                "report generated and integrity-validated."
            )
            status = "completed"
            exit_code = EXIT_OK
        planner.log_event(
            self.run_id,
            "stopping",
            stop_reason,
            payload={
                "status": status,
                "answered": answered,
                "subquestions": total,
                "sources": total_sources,
                "report_id": report.get("report_id"),
            },
            settings=self.settings,
        )
        runs.set_run_status(self.run_id, status, stage="done", settings=self.settings)
        return self._result(status, exit_code, report, total, answered, final_gaps, duration_ms, stop_reason)

    def _result(
        self,
        status: str,
        exit_code: int,
        report: dict[str, Any],
        total: int,
        answered: int,
        gaps: list[dict[str, Any]],
        duration_ms: int,
        stopping_reason: str,
    ) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": status,
            "exit_code": exit_code,
            "query": self.options.question,
            "depth": self.options.depth,
            "sources_count": _run_source_count(self.run_id, self.settings),
            "claims_count": _count_claim_rows(self.run_id, self.settings),
            "subquestions": total,
            "answered": answered,
            "gaps": gaps,
            "report": {
                "report_id": report.get("report_id"),
                "path": report.get("path"),
                "validation_status": report.get("validation_status"),
                "validation_passed": report.get("validation_passed"),
            },
            "stopping_reason": stopping_reason,
            "warnings": self.warnings,
            "duration_ms": duration_ms,
            "log_file": str(self.reporter.log_file_path) if self.reporter.log_file_path else None,
        }


# ---------------------------------------------------------------------------
# Entry points used by the CLI
# ---------------------------------------------------------------------------


def create_run(
    options: RunOptions, settings: Settings, reporter: ProgressReporter, *, tool_version: str
) -> int:
    """Create a CLI research run whose config snapshot is redacted and persisted."""
    resolved = options.resolved(settings)
    run = evidence.start_run(
        query=options.question,
        config=runs.build_run_config(resolved.config_options(), settings, tool_version=tool_version),
        project=resolved.project,
        settings=settings,
    )
    run_id = int(run["run_id"])
    reporter.bind_run(run_id)
    runs.set_run_status(run_id, "running", stage="created", settings=settings)
    reporter.info("run_created", f"research run {run_id} created", stage="created")
    return run_id


def execute_research(
    run_id: int,
    options: RunOptions,
    *,
    settings: Settings,
    reporter: ProgressReporter,
    restart: bool = False,
) -> dict[str, Any]:
    """Run (or continue) the deterministic pipeline for one existing run."""
    session = ResearchSession(run_id, options, settings, reporter, restart=restart)
    try:
        return session.execute()
    except KeyboardInterrupt:
        _mark_interrupted(run_id, settings, reporter, "interrupted by user (SIGINT)")
        return {
            "run_id": run_id,
            "status": "interrupted",
            "exit_code": EXIT_INTERRUPTED,
            "query": options.question,
            "error": "interrupted by user; re-run `agletras resume %d` to continue" % run_id,
            "log_file": str(reporter.log_file_path) if reporter.log_file_path else None,
        }
    except PipelineInterrupted as exc:
        _mark_interrupted(run_id, settings, reporter, str(exc))
        return {
            "run_id": run_id,
            "status": "interrupted",
            "exit_code": EXIT_INTERRUPTED,
            "query": options.question,
            "error": f"{exc}; re-run `agletras resume {run_id}` to continue",
            "log_file": str(reporter.log_file_path) if reporter.log_file_path else None,
        }
    except ToolError as exc:
        _mark_failed(run_id, settings, reporter, exc.code, exc.message)
        return {
            "run_id": run_id,
            "status": "failed",
            "exit_code": EXIT_RESEARCH_FAILED,
            "query": options.question,
            "error": {"code": exc.code, "message": exc.message},
            "log_file": str(reporter.log_file_path) if reporter.log_file_path else None,
        }
    except Exception as exc:  # noqa: BLE001 - final guard: persist honesty, then report
        message = f"{exc.__class__.__name__}: {exc}"
        _mark_failed(run_id, settings, reporter, "internal", message)
        return {
            "run_id": run_id,
            "status": "failed",
            "exit_code": EXIT_RESEARCH_FAILED,
            "query": options.question,
            "error": {"code": "internal", "message": message},
            "log_file": str(reporter.log_file_path) if reporter.log_file_path else None,
        }


def _mark_interrupted(
    run_id: int, settings: Settings, reporter: ProgressReporter, reason: str
) -> None:
    """Persist the interruption (best effort) so the run can be resumed."""
    try:
        planner.log_event(
            run_id,
            "stopping",
            reason,
            payload={"status": "interrupted"},
            settings=settings,
        )
    except Exception:  # noqa: BLE001 - never mask the original interruption
        pass
    try:
        runs.set_run_status(run_id, "interrupted", error=reason, stage="interrupted", settings=settings)
    except Exception:  # noqa: BLE001
        pass
    try:
        reporter.warn(reason)
    except Exception:  # noqa: BLE001
        pass


def _mark_failed(
    run_id: int, settings: Settings, reporter: ProgressReporter, code: str, message: str
) -> None:
    try:
        planner.log_event(
            run_id,
            "stopping",
            f"run failed: {code}: {message}",
            payload={"status": "failed", "error_code": code},
            settings=settings,
        )
    except Exception:  # noqa: BLE001 - the original failure is what matters
        pass
    try:
        runs.set_run_status(run_id, "failed", error=f"{code}: {message}", stage="failed", settings=settings)
    except Exception:  # noqa: BLE001
        pass
    try:
        reporter.error(f"run failed: {code}: {message}", code=code)
    except Exception:  # noqa: BLE001
        pass


def resume_research(
    run_id: int,
    options: RunOptions,
    *,
    settings: Settings,
    reporter: ProgressReporter,
    force: bool = False,
    restart: bool = False,
) -> dict[str, Any]:
    """Resume one run from its last valid checkpoint.

    Refuses when the run is unknown, MCP-managed, closed, stale, or was
    started with different options (see :func:`runs.resume_assessment` and
    :func:`runs.config_differences`); ``force`` overrides failed/stale/
    incompatible states but never a completed run.
    """
    assessment = runs.resume_assessment(run_id, settings=settings, force=force)
    if not assessment.get("resumable"):
        reporter.error(assessment["detail"], code=assessment["reason"])
        return {
            "run_id": run_id,
            "status": "not_resumed",
            "exit_code": EXIT_USAGE,
            "reason": assessment["reason"],
            "error": {"code": assessment["reason"], "message": assessment["detail"]},
        }
    run = assessment["run"]
    stored_config = run.get("config") or {}
    differences = runs.config_differences(stored_config, options.resolved(settings).config_options(), settings)
    if differences and not force:
        message = (
            "the stored run configuration differs from the current one, so resuming would mix "
            "incompatible evidence:\n  " + "\n  ".join(differences) +
            "\nResume with --force to accept the differences, or start a new run."
        )
        reporter.error(message, code="incompatible")
        return {
            "run_id": run_id,
            "status": "not_resumed",
            "exit_code": EXIT_USAGE,
            "reason": "incompatible",
            "differences": differences,
            "error": {"code": "incompatible", "message": message},
        }
    if run["status"] != "running":
        runs.set_run_status(run_id, "running", stage=run.get("stage") or "resumed", settings=settings)
    reporter.bind_run(run_id)
    reporter.info(
        "resume",
        f"resuming run {run_id} (status {run['status']}); completed stages are skipped",
        stage=assessment.get("paused_stage") or run.get("stage") or "",
    )
    result = execute_research(
        run_id, options, settings=settings, reporter=reporter, restart=restart
    )
    result["resumed"] = True
    return result

"""Bounded parallel execution with an explicit sequential fallback (Phase 8).

The Phase 2 collection pipeline already ran retrieval with bounded concurrency;
Phase 8 needs the same discipline in more places (specialist agents, monitoring
checks) with a *defined* contract:

- every task has a stable id, a name, and a callable returning a plain dict;
- results are returned in **input order** regardless of completion order, so
  merges stay deterministic;
- one failed, timed-out, cancelled, or malformed task never aborts the batch —
  it is reported as an explicit outcome;
- concurrency is bounded (``max_workers``) and never grows with the workload;
- a per-task timeout and an overall deadline are enforced while waiting on
  results (waiting is what can be bounded — a running Python thread is only
  abandoned, never killed; the timeout makes that visible instead of silent);
- optional bounded retries are limited to declared retryable error codes.

The sequential mode (``parallel=False``) is a first-class execution path, not a
Degraded parallel mode: it runs the same tasks in the same order on the calling
thread. Callers can therefore compare both paths on identical workloads.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .errors import ToolError

#: Outcome statuses (stable, documented in the README).
TASK_STATUSES = ("ok", "failed", "timeout", "cancelled", "invalid_output", "skipped")

#: Error codes considered transient by default (network-ish failures).
DEFAULT_RETRYABLE = ("network_error", "rate_limited", "storage_error", "timeout")


class TaskCancelled(Exception):
    """Raised inside a task body when the batch deadline has already passed."""


@dataclass
class TaskSpec:
    """One unit of bounded work.

    ``run`` must be a plain callable returning a JSON-able dict. A returned
    ``{"status": "failed", ...}`` dict is treated as a failed outcome (useful
    for workers that catch their own errors); any other dict is an ``ok``
    outcome whose payload is the returned dict.
    """

    task_id: str
    name: str
    run: Callable[[], dict[str, Any]]
    timeout: float | None = None
    retries: int = 0
    retry_codes: tuple[str, ...] = DEFAULT_RETRYABLE
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskOutcome:
    """The structured result of one task (never an exception)."""

    task_id: str
    name: str
    status: str
    started_at: str
    duration_ms: int
    attempts: int
    result: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
        }
        if self.error_code:
            payload["error_code"] = self.error_code
        if self.error_message:
            payload["error_message"] = self.error_message
        if self.status == "ok":
            payload["result"] = self.result
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _error_from(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, ToolError):
        return exc.code, exc.message
    if isinstance(exc, TimeoutError):
        return "timeout", "the task exceeded its time budget"
    return "internal", f"{exc.__class__.__name__}: {exc}"


def _invoke(task: TaskSpec) -> tuple[str, dict[str, Any], str | None, str | None]:
    """Run one attempt; returns ``(status, result, error_code, error_message)``."""
    try:
        result = task.run()
    except BaseException as exc:  # noqa: BLE001 - every failure is an outcome
        code, message = _error_from(exc)
        return "failed", {}, code, message
    if not isinstance(result, dict):
        return (
            "invalid_output",
            {},
            "invalid_output",
            f"task {task.task_id!r} returned {type(result).__name__}, expected a dict",
        )
    if str(result.get("status") or "ok").lower() == "failed":
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        return (
            "failed",
            result,
            str(error.get("code") or result.get("error_code") or "failed"),
            str(error.get("message") or result.get("error_message") or "the task reported failure"),
        )
    return "ok", result, None, None


def run_tasks(
    tasks: list[TaskSpec],
    *,
    parallel: bool = True,
    max_workers: int = 4,
    timeout: float | None = None,
    deadline: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[TaskOutcome]:
    """Execute *tasks* bounded by concurrency, timeouts, and an optional deadline.

    ``parallel=False`` executes on the calling thread in input order (the
    reliability fallback). Duplicate task ids are rejected: merges key on ids,
    so an ambiguous id is a caller bug surfaced early.
    """
    ids = [task.task_id for task in tasks]
    if len(set(ids)) != len(ids):
        raise ToolError("invalid_argument", "task ids must be unique")
    if not tasks:
        return []
    workers = max(1, min(int(max_workers), len(tasks), 32))
    if parallel:
        # Explicit parallel mode: even a single task goes through the pool so
        # timeouts and deadlines are enforced identically at every batch size.
        return _run_parallel(tasks, workers=workers, timeout=timeout, deadline=deadline, should_cancel=should_cancel)
    return _run_sequential(tasks, timeout=timeout, deadline=deadline, should_cancel=should_cancel)


def _run_sequential(
    tasks: list[TaskSpec],
    *,
    timeout: float | None,
    deadline: float | None,
    should_cancel: Callable[[], bool] | None,
) -> list[TaskOutcome]:
    outcomes: list[TaskOutcome] = []
    for task in tasks:
        if should_cancel is not None and should_cancel():
            outcomes.append(_skipped(task, "cancelled before start"))
            continue
        if deadline is not None and time.monotonic() >= deadline:
            outcomes.append(_skipped(task, "batch deadline reached before start"))
            continue
        outcomes.append(_run_one(task, timeout=timeout, deadline=deadline))
    return outcomes


def _run_parallel(
    tasks: list[TaskSpec],
    *,
    workers: int,
    timeout: float | None,
    deadline: float | None,
    should_cancel: Callable[[], bool] | None,
) -> list[TaskOutcome]:
    outcomes_by_index: dict[int, TaskOutcome] = {}
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {}
        for index, task in enumerate(tasks):
            if should_cancel is not None and should_cancel():
                outcomes_by_index[index] = _skipped(task, "cancelled before start")
                continue
            if deadline is not None and time.monotonic() >= deadline:
                outcomes_by_index[index] = _skipped(task, "batch deadline reached before start")
                continue
            futures[pool.submit(_timed_invoke, task, timeout, deadline)] = (index, task)
        for future, (index, task) in futures.items():
            effective_timeout = _effective_timeout(task, timeout, deadline)
            try:
                outcome = future.result(timeout=effective_timeout)
            except TimeoutError:
                future.cancel()
                outcome = TaskOutcome(
                    task_id=task.task_id,
                    name=task.name,
                    status="cancelled" if future.cancelled() else "timeout",
                    started_at=_utc_now(),
                    duration_ms=int((effective_timeout or 0) * 1000),
                    attempts=1,
                    error_code="timeout",
                    error_message=(
                        f"task {task.task_id!r} exceeded its {effective_timeout:g}s budget; "
                        "a running worker thread cannot be killed, so its late side effects "
                        "(if any) are reported separately"
                    ),
                    metadata=task.metadata,
                )
            except BaseException as exc:  # noqa: BLE001 - the pool itself failed
                code, message = _error_from(exc)
                outcome = TaskOutcome(
                    task_id=task.task_id,
                    name=task.name,
                    status="failed",
                    started_at=_utc_now(),
                    duration_ms=0,
                    attempts=1,
                    error_code=code,
                    error_message=message,
                    metadata=task.metadata,
                )
            outcomes_by_index[index] = outcome
    finally:
        # Never wait on abandoned workers: the caller is released at the
        # budget, queued (not yet started) futures are cancelled, and a
        # running worker's late side effects stay visible in its outcome.
        pool.shutdown(wait=False, cancel_futures=True)
    return [outcomes_by_index[index] for index in range(len(tasks))]


def _effective_timeout(task: TaskSpec, timeout: float | None, deadline: float | None) -> float | None:
    candidates: list[float] = []
    if task.timeout is not None:
        candidates.append(float(task.timeout))
    if timeout is not None:
        candidates.append(float(timeout))
    if deadline is not None:
        remaining = deadline - time.monotonic()
        candidates.append(max(remaining, 0.001))
    return min(candidates) if candidates else None


def _timed_invoke(task: TaskSpec, timeout: float | None, deadline: float | None) -> TaskOutcome:
    """Run one task with retries; executed inside the worker thread."""
    started = time.monotonic()
    started_at = _utc_now()
    attempts = 0
    code: str | None = None
    message: str | None = None
    status = "failed"
    result: dict[str, Any] = {}
    while attempts <= max(int(task.retries), 0):
        attempts += 1
        status, result, code, message = _invoke(task)
        if status == "ok":
            break
        retryable = code is not None and code in (task.retry_codes or DEFAULT_RETRYABLE)
        if not retryable or attempts > int(task.retries):
            break
    return TaskOutcome(
        task_id=task.task_id,
        name=task.name,
        status=status,
        started_at=started_at,
        duration_ms=int((time.monotonic() - started) * 1000),
        attempts=attempts,
        result=result,
        error_code=code,
        error_message=message,
        metadata=task.metadata,
    )


def _run_one(task: TaskSpec, *, timeout: float | None, deadline: float | None) -> TaskOutcome:
    """Sequential execution: the timeout is a *budget check*, not a kill signal."""
    started = time.monotonic()
    started_at = _utc_now()
    attempts = 0
    status = "failed"
    result: dict[str, Any] = {}
    code: str | None = None
    message: str | None = None
    while attempts <= max(int(task.retries), 0):
        attempts += 1
        status, result, code, message = _invoke(task)
        if status == "ok":
            break
        retryable = code is not None and code in (task.retry_codes or DEFAULT_RETRYABLE)
        if not retryable or attempts > int(task.retries):
            break
    duration_ms = int((time.monotonic() - started) * 1000)
    outcome = TaskOutcome(
        task_id=task.task_id,
        name=task.name,
        status=status,
        started_at=started_at,
        duration_ms=duration_ms,
        attempts=attempts,
        result=result,
        error_code=code,
        error_message=message,
        metadata=task.metadata,
    )
    budget = _effective_timeout(task, timeout, deadline)
    if outcome.status == "ok" and budget is not None and duration_ms > budget * 1000:
        # Sequential work cannot be interrupted; surface the overrun honestly.
        outcome.metadata = {**outcome.metadata, "budget_overrun_ms": duration_ms, "budget_ms": int(budget * 1000)}
    return outcome


def _skipped(task: TaskSpec, reason: str) -> TaskOutcome:
    return TaskOutcome(
        task_id=task.task_id,
        name=task.name,
        status="skipped",
        started_at=_utc_now(),
        duration_ms=0,
        attempts=0,
        error_code="skipped",
        error_message=reason,
        metadata=task.metadata,
    )

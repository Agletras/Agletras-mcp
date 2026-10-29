"""Tests for the bounded parallel task runner (Phase 8).

The runner is deterministic by contract: outcomes come back in input order,
one failed task never aborts the batch, and the sequential mode is a real
execution path (same tasks, same order, calling thread) that callers can
compare against the parallel one on identical workloads.
"""

from __future__ import annotations

import threading
import time
import unittest

from agletras_mcp import parallel
from agletras_mcp.errors import ToolError


def _task(task_id: str, run, **kwargs) -> parallel.TaskSpec:
    return parallel.TaskSpec(task_id=task_id, name=f"task {task_id}", run=run, **kwargs)


class ValidationTests(unittest.TestCase):
    def test_duplicate_task_ids_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            parallel.run_tasks([_task("a", lambda: {}), _task("a", lambda: {})])
        self.assertEqual(ctx.exception.code, "invalid_argument")
        self.assertIn("unique", ctx.exception.message)

    def test_empty_batch_returns_empty(self) -> None:
        self.assertEqual(parallel.run_tasks([]), [])


class OutcomeTests(unittest.TestCase):
    def test_results_keep_input_order_even_when_completion_differs(self) -> None:
        def slow() -> dict:
            time.sleep(0.15)
            return {"value": "slow"}

        def fast() -> dict:
            return {"value": "fast"}

        tasks = [_task("first", slow), _task("second", fast)]
        for mode in (True, False):
            outcomes = parallel.run_tasks(tasks, parallel=mode)
            self.assertEqual([o.task_id for o in outcomes], ["first", "second"])
            self.assertEqual([o.status for o in outcomes], ["ok", "ok"])
            self.assertEqual(outcomes[0].result["value"], "slow")
            self.assertEqual(outcomes[1].result["value"], "fast")

    def test_one_failure_does_not_abort_the_batch(self) -> None:
        def boom() -> dict:
            raise ToolError("network_error", "simulated outage")

        outcomes = parallel.run_tasks(
            [_task("a", lambda: {"n": 1}), _task("b", boom), _task("c", lambda: {"n": 3})]
        )
        by_id = {o.task_id: o for o in outcomes}
        self.assertEqual(by_id["a"].status, "ok")
        self.assertEqual(by_id["b"].status, "failed")
        self.assertEqual(by_id["b"].error_code, "network_error")
        self.assertIn("simulated outage", by_id["b"].error_message or "")
        self.assertEqual(by_id["c"].status, "ok")

    def test_generic_exception_is_reported_with_internal_code(self) -> None:
        outcomes = parallel.run_tasks([_task("x", lambda: 1 / 0)])
        self.assertEqual(outcomes[0].status, "failed")
        self.assertEqual(outcomes[0].error_code, "internal")
        self.assertIn("ZeroDivisionError", outcomes[0].error_message or "")

    def test_non_dict_result_is_invalid_output(self) -> None:
        outcomes = parallel.run_tasks([_task("x", lambda: ["not", "a", "dict"])])
        self.assertEqual(outcomes[0].status, "invalid_output")
        self.assertEqual(outcomes[0].error_code, "invalid_output")

    def test_worker_reported_failure_is_a_failed_outcome(self) -> None:
        def reports_failure() -> dict:
            return {"status": "failed", "error": {"code": "blocked_host", "message": "denied"}}

        outcomes = parallel.run_tasks([_task("x", reports_failure)])
        self.assertEqual(outcomes[0].status, "failed")
        self.assertEqual(outcomes[0].error_code, "blocked_host")
        self.assertEqual(outcomes[0].error_message, "denied")

    def test_outcome_as_dict_omits_empty_fields(self) -> None:
        outcomes = parallel.run_tasks([_task("x", lambda: {"v": 1})])
        payload = outcomes[0].as_dict()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["result"], {"v": 1})
        self.assertNotIn("error_code", payload)
        self.assertNotIn("metadata", payload)

    def test_metadata_travels_with_the_outcome(self) -> None:
        outcomes = parallel.run_tasks([_task("x", lambda: {}, metadata={"target_id": 7})])
        self.assertEqual(outcomes[0].metadata, {"target_id": 7})
        self.assertEqual(outcomes[0].as_dict()["metadata"], {"target_id": 7})


class RetryTests(unittest.TestCase):
    def test_retryable_failure_is_retried_then_succeeds(self) -> None:
        attempts = {"n": 0}

        def flaky() -> dict:
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise ToolError("network_error", "transient")
            return {"value": "recovered"}

        outcomes = parallel.run_tasks([_task("x", flaky, retries=2)])
        self.assertEqual(outcomes[0].status, "ok")
        self.assertEqual(outcomes[0].attempts, 2)
        self.assertEqual(outcomes[0].result["value"], "recovered")

    def test_non_retryable_failure_is_not_retried(self) -> None:
        attempts = {"n": 0}

        def denied() -> dict:
            attempts["n"] += 1
            raise ToolError("blocked_host", "no")

        outcomes = parallel.run_tasks([_task("x", denied, retries=5)])
        self.assertEqual(outcomes[0].status, "failed")
        self.assertEqual(outcomes[0].attempts, 1)
        self.assertEqual(attempts["n"], 1)

    def test_retries_exhausted_reports_final_failure(self) -> None:
        attempts = {"n": 0}

        def always_fails() -> dict:
            attempts["n"] += 1
            raise ToolError("timeout", "still slow")

        outcomes = parallel.run_tasks([_task("x", always_fails, retries=2)])
        self.assertEqual(outcomes[0].status, "failed")
        self.assertEqual(outcomes[0].attempts, 3)
        self.assertEqual(attempts["n"], 3)

    def test_retry_works_in_sequential_mode_too(self) -> None:
        attempts = {"n": 0}

        def flaky() -> dict:
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise ToolError("storage_error", "busy")
            return {"ok": True}

        outcomes = parallel.run_tasks([_task("x", flaky, retries=1)], parallel=False)
        self.assertEqual(outcomes[0].status, "ok")
        self.assertEqual(outcomes[0].attempts, 2)


class TimeoutAndCancellationTests(unittest.TestCase):
    def test_parallel_timeout_is_recorded_not_raised(self) -> None:
        def sleeper() -> dict:
            time.sleep(0.5)
            return {"value": "late"}

        started = time.monotonic()
        outcomes = parallel.run_tasks([_task("slow", sleeper, timeout=0.05)])
        elapsed = time.monotonic() - started
        self.assertEqual(outcomes[0].status, "timeout")
        self.assertEqual(outcomes[0].error_code, "timeout")
        self.assertIn("cannot be killed", outcomes[0].error_message or "")
        # The caller was released near the budget, not after the worker's nap.
        self.assertLess(elapsed, 0.45)

    def test_fast_sibling_completes_while_sluggish_one_times_out(self) -> None:
        def sleeper() -> dict:
            time.sleep(0.4)
            return {}

        outcomes = parallel.run_tasks(
            [
                _task("slow", sleeper, timeout=0.05),
                _task("fast", lambda: {"value": "ready"}),
            ]
        )
        self.assertEqual(outcomes[0].status, "timeout")
        self.assertEqual(outcomes[1].status, "ok")
        self.assertEqual(outcomes[1].result["value"], "ready")

    def test_sequential_budget_overrun_is_surfaced_honestly(self) -> None:
        def slow() -> dict:
            time.sleep(0.08)
            return {"value": "done"}

        outcomes = parallel.run_tasks([_task("slow", slow, timeout=0.01)], parallel=False)
        self.assertEqual(outcomes[0].status, "ok")  # sequential cannot be interrupted
        self.assertIn("budget_overrun_ms", outcomes[0].metadata)

    def test_cancellation_marks_remaining_tasks_skipped(self) -> None:
        calls = {"n": 0}

        def should_cancel() -> bool:
            calls["n"] += 1
            return calls["n"] > 1

        outcomes = parallel.run_tasks(
            [_task("a", lambda: {"n": 1}), _task("b", lambda: {"n": 2})],
            parallel=False,
            should_cancel=should_cancel,
        )
        self.assertEqual(outcomes[0].status, "ok")
        self.assertEqual(outcomes[1].status, "skipped")
        self.assertEqual(outcomes[1].error_code, "skipped")

    def test_passed_deadline_skips_every_task(self) -> None:
        outcomes = parallel.run_tasks(
            [_task("a", lambda: {"n": 1})], deadline=time.monotonic() - 1.0
        )
        self.assertEqual(outcomes[0].status, "skipped")
        self.assertIn("deadline", outcomes[0].error_message or "")


class ConcurrencyTests(unittest.TestCase):
    def test_concurrency_never_exceeds_max_workers(self) -> None:
        active = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def worker() -> dict:
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            time.sleep(0.03)
            with lock:
                active["now"] -= 1
            return {}

        tasks = [_task(f"t{i}", worker) for i in range(8)]
        outcomes = parallel.run_tasks(tasks, max_workers=2)
        self.assertTrue(all(o.status == "ok" for o in outcomes))
        self.assertLessEqual(active["peak"], 2)
        self.assertEqual([o.task_id for o in outcomes], [f"t{i}" for i in range(8)])

    def test_parallel_is_faster_than_sequential_on_identical_workload(self) -> None:
        def worker() -> dict:
            time.sleep(0.15)
            return {}

        tasks = [_task(f"t{i}", worker) for i in range(4)]
        started = time.monotonic()
        parallel.run_tasks(tasks, parallel=False)
        sequential_time = time.monotonic() - started
        started = time.monotonic()
        parallel.run_tasks(tasks, parallel=True, max_workers=4)
        parallel_time = time.monotonic() - started
        self.assertLess(parallel_time, sequential_time)
        self.assertLess(parallel_time, 0.45)
        self.assertGreaterEqual(sequential_time, 0.5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

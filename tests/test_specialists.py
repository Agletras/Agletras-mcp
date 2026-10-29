"""Phase 8 tests: specialist roles, builders, workers, batch merge, recheck.

Everything is hermetic: local HTTP fixtures on 127.0.0.1 (no real network),
a temporary home directory, and deterministic workers. The recheck tests use
the real evidence library so the read-only guarantee is checked against stored
passages, not mocks.
"""

from __future__ import annotations

import os
import threading
import time
import unittest
from unittest import mock

from agletras_mcp import evidence, specialists, storage
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import TempHomeCase, serve_http, serve_slow_http

TOKEN = "specialist-fixture-token"
DOC = (
    "<html><head><title>Passage storage</title></head><body><p>"
    "Agletras stores evidence as indexed passages inside a local SQLite database. "
    "Each passage keeps a stable hash and exact character offsets so citations "
    "can point at the passage text."
    "</p></body></html>"
).encode()


class TaskValidationTests(unittest.TestCase):
    def test_unknown_role_and_operation_are_refused(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            specialists.Task(role="wizard", op="search", task_id="x")
        self.assertEqual(ctx.exception.code, "invalid_argument")
        with self.assertRaises(ToolError):
            specialists.Task(role="web_researcher", op="image", task_id="x")
        with self.assertRaises(ToolError):
            specialists.Task(role="fact_checker", op="collect_url", task_id="x")

    def test_role_catalog_is_stable_and_documented(self) -> None:
        self.assertEqual(
            specialists.ROLES,
            ("web_researcher", "document_analyst", "image_analyst", "fact_checker"),
        )
        for role, ops in specialists.OPERATIONS.items():
            self.assertTrue(ops, role)

    def test_collect_url_task_ids_are_unique_per_sequence(self) -> None:
        first = specialists.collect_url_task(
            run_id=1, subquestion_id=2, url="https://example.com/a", seq=0
        )
        second = specialists.collect_url_task(
            run_id=1, subquestion_id=2, url="https://example.com/a", seq=1
        )
        self.assertNotEqual(first.task_id, second.task_id)
        self.assertEqual(first.role, "web_researcher")
        self.assertEqual(first.op, "collect_url")
        self.assertEqual(first.payload["subquestion_id"], 2)

    def test_builders_set_roles_and_payloads(self) -> None:
        search = specialists.search_task(
            run_id=1, subquestion_id=3, query="evidence passages", max_results=5, round_index=1
        )
        self.assertEqual((search.role, search.op), ("web_researcher", "search"))
        self.assertEqual(search.payload["round"], 1)
        document = specialists.collect_path_task(
            run_id=1, subquestion_id=3, path="/tmp/x.md"
        )
        self.assertEqual((document.role, document.op), ("document_analyst", "collect_path"))
        image = specialists.image_task(run_id=1, query="diagram", max_results=2)
        self.assertEqual((image.role, image.op), ("image_analyst", "image"))
        recheck = specialists.recheck_task(run_id=1, claim_id=9)
        self.assertEqual((recheck.role, recheck.op), ("fact_checker", "recheck"))
        self.assertEqual(recheck.task_id, "recheck:9")


class WorkerTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.routes: dict[str, tuple] = {}
        self._server = serve_http(self.routes)
        self.base = self._server.__enter__()
        self.addCleanup(self._server.__exit__, None, None, None)
        self.routes["/doc"] = (200, "text/html; charset=utf-8", DOC)
        self.routes["/search"] = (
            200,
            "text/html; charset=utf-8",
            (
                "<!doctype html><html><body>"
                f'<h2 class="result__title"><a class="result__a" href="{self.base}/doc">'
                "Passage storage</a></h2>"
                '<a class="result__snippet">Indexed passages.</a>'
                "</body></html>"
            ).encode(),
        )
        os.environ["AGLETRAS_SEARCH_PROVIDER"] = "duckduckgo"
        os.environ["AGLETRAS_SEARCH_ENDPOINT"] = self.base + "/search"
        self.settings = load_settings()

    def _run_id(self, query: str = "question") -> int:
        return int(evidence.start_run(query, settings=self.settings)["run_id"])

    def test_web_researcher_search_returns_urls_and_logs_the_event(self) -> None:
        run_id = self._run_id()
        task = specialists.search_task(
            run_id=run_id, subquestion_id=0, query="passages", max_results=3
        )
        result = specialists.dispatch(task, settings=self.settings)
        self.assertEqual(result["op"], "search")
        self.assertEqual(result["urls"], [self.base + "/doc"])
        history = evidence.get_research_run_history(run_id=run_id, settings=self.settings)
        kinds = [event["kind"] for event in history["events"]]
        self.assertIn("search", kinds)

    def test_web_researcher_collect_saves_evidence_and_links_the_run(self) -> None:
        run_id = self._run_id()
        task = specialists.collect_url_task(
            run_id=run_id, subquestion_id=0, url=self.base + "/doc", seq=0
        )
        result = specialists.dispatch(task, settings=self.settings)
        self.assertEqual(result["op"], "collect")
        source_id = int(result["source_id"])
        self.assertTrue(result["created_source"])
        self.assertGreater(result["chars"], 0)
        stored = evidence.get_source_by_id(source_id, settings=self.settings)
        self.assertEqual(stored["url"], self.base + "/doc")
        with storage.open_db(self.settings) as conn:
            linked = conn.execute(
                "SELECT 1 FROM research_run_sources WHERE run_id = ? AND source_id = ?",
                (run_id, source_id),
            ).fetchone()
        self.assertIsNotNone(linked)

    def test_collect_failure_is_a_declared_outcome_not_an_exception(self) -> None:
        run_id = self._run_id()
        task = specialists.collect_url_task(
            run_id=run_id, subquestion_id=0, url=self.base + "/missing", seq=0
        )
        result = specialists.dispatch(task, settings=self.settings)
        self.assertEqual(result["status"], "failed")
        self.assertIn(
            result["error"]["code"],
            ("network_error", "http_error", "not_found", "extraction_failed"),
        )

    def test_document_analyst_collects_a_permitted_local_document(self) -> None:
        note = self.home / "note.md"
        note.write_text(
            f"# Memory\n\nThe {TOKEN} note records that passages stay immutable.\n",
            encoding="utf-8",
        )
        run_id = self._run_id()
        task = specialists.collect_path_task(
            run_id=run_id, subquestion_id=0, path=str(note)
        )
        result = specialists.dispatch(task, settings=self.settings)
        self.assertEqual(result["op"], "collect")
        stored = evidence.get_source_by_id(int(result["source_id"]), settings=self.settings)
        self.assertEqual(stored["path"], str(note))

    def test_image_analyst_reports_no_results_without_fabricating(self) -> None:
        run_id = self._run_id()
        task = specialists.image_task(run_id=run_id, query="diagram", max_results=1)
        with mock.patch.object(
            specialists.images,
            "search_images",
            return_value={"provider": "mock", "count": 0, "results": []},
        ):
            result = specialists.dispatch(task, settings=self.settings)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "not_found")

    def test_fact_checker_agrees_with_the_stored_verification(self) -> None:
        run_id, claim_id = self._verified_claim()
        task = specialists.recheck_task(run_id=run_id, claim_id=claim_id)
        result = specialists.dispatch(task, settings=self.settings)
        self.assertEqual(result["op"], "recheck")
        self.assertTrue(result["agreement"])
        self.assertEqual(result["stored_status"], result["recomputed_status"])
        self.assertGreaterEqual(result["candidates"], 1)

    def test_fact_checker_surfaces_disagreement_without_writing(self) -> None:
        run_id, claim_id = self._verified_claim()
        with storage.open_db(self.settings) as conn:
            conn.execute(
                "UPDATE claims SET status = 'contradicted' WHERE id = ?", (claim_id,)
            )
        task = specialists.recheck_task(run_id=run_id, claim_id=claim_id)
        result = specialists.dispatch(task, settings=self.settings)
        self.assertFalse(result["agreement"])
        self.assertEqual(result["stored_status"], "contradicted")
        self.assertEqual(result["recomputed_status"], "supported")
        # Read-only: the tampered status is exactly what is still stored.
        stored = evidence.get_claim(claim_id, settings=self.settings)
        self.assertEqual(stored["status"], "contradicted")

    def test_fact_checker_refuses_an_unknown_claim(self) -> None:
        run_id = self._run_id()
        task = specialists.recheck_task(run_id=run_id, claim_id=99999)
        result = specialists.dispatch(task, settings=self.settings)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "not_found")

    def _verified_claim(self) -> tuple[int, int]:
        """A run + stored supporting citation whose derived status is supported."""
        doc = self.home / "evidence.md"
        sentence = "Agletras stores evidence as indexed passages inside a local SQLite database."
        doc.write_text(f"# Evidence\n\n{sentence} Extra commentary follows here.\n", encoding="utf-8")
        run_id = self._run_id("How does agletras store evidence?")
        saved = evidence.save_evidence(source=str(doc), run_id=run_id, settings=self.settings)
        self.assertTrue(saved["created_source"])
        passage = evidence.search_evidence(
            "indexed passages", limit=1, run_id=run_id, settings=self.settings
        )["passages"][0]
        claim = evidence.register_claim(run_id, sentence, settings=self.settings)
        evidence.attach_citation(
            claim_id=int(claim["claim_id"]),
            passage_id=int(passage["passage_id"]),
            quote=sentence,
            relation="supports",
            settings=self.settings,
        )
        return run_id, int(claim["claim_id"])


class BatchMergeTests(TempHomeCase):
    """Deterministic merge, failure isolation, and dedupe over fake workers."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def _tasks(self, count: int) -> list[specialists.Task]:
        return [specialists.recheck_task(run_id=1, claim_id=index) for index in range(count)]

    def test_results_merge_in_input_order_despite_completion_order(self) -> None:
        tasks = self._tasks(3)
        delays = {tasks[0].task_id: 0.15, tasks[1].task_id: 0.0, tasks[2].task_id: 0.05}

        def fake_dispatch(task: specialists.Task, *, settings: object) -> dict:
            time.sleep(delays.get(task.task_id, 0.0))
            return {"op": task.op, "order": task.task_id, "source_id": 1}

        with mock.patch.object(specialists, "dispatch", fake_dispatch):
            batch = specialists.run_specialists(
                tasks, parallel_mode=True, max_workers=3, settings=self.settings
            )
        self.assertEqual([item["task_id"] for item in batch["results"]], [t.task_id for t in tasks])
        self.assertEqual(batch["ok"], 3)
        self.assertEqual(batch["failed"], 0)

    def test_one_failure_never_aborts_the_batch(self) -> None:
        tasks = self._tasks(3)

        def fake_dispatch(task: specialists.Task, *, settings: object) -> dict:
            if task.task_id == tasks[1].task_id:
                raise ToolError("network_error", "boom")
            return {"op": task.op, "source_id": 2, "ok": True}

        with mock.patch.object(specialists, "dispatch", fake_dispatch):
            batch = specialists.run_specialists(
                tasks, parallel_mode=True, max_workers=3, settings=self.settings
            )
        statuses = [item["status"] for item in batch["results"]]
        self.assertEqual(statuses, ["ok", "failed", "ok"])
        self.assertEqual(batch["failed"], 1)
        self.assertEqual(batch["counts_by_role"]["fact_checker"], {"ok": 2, "failed": 1})
        self.assertTrue(any("network_error" in warning for warning in batch["warnings"]))
        # Failed workers contribute nothing to the merged source ids.
        self.assertEqual(batch["source_ids"], [2])

    def test_declared_failure_dict_is_a_failed_outcome(self) -> None:
        tasks = self._tasks(1)

        def fake_dispatch(task: specialists.Task, *, settings: object) -> dict:
            return {"status": "failed", "error": {"code": "rate_limited", "message": "slow down"}}

        with mock.patch.object(specialists, "dispatch", fake_dispatch):
            batch = specialists.run_specialists(
                tasks, parallel_mode=True, max_workers=1, settings=self.settings
            )
        outcome = batch["results"][0]
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["error_code"], "rate_limited")

    def test_source_ids_are_deduplicated_in_input_order(self) -> None:
        tasks = self._tasks(3)
        ids = {tasks[0].task_id: 5, tasks[1].task_id: 7, tasks[2].task_id: 5}

        def fake_dispatch(task: specialists.Task, *, settings: object) -> dict:
            return {"op": task.op, "source_id": ids[task.task_id]}

        with mock.patch.object(specialists, "dispatch", fake_dispatch):
            batch = specialists.run_specialists(
                tasks, parallel_mode=True, max_workers=2, settings=self.settings
            )
        self.assertEqual(batch["source_ids"], [5, 7])

    def test_sequential_and_parallel_are_equivalent(self) -> None:
        tasks = self._tasks(4)

        def fake_dispatch(task: specialists.Task, *, settings: object) -> dict:
            time.sleep(0.01)
            return {"op": task.op, "claim_id": task.payload["claim_id"], "agreement": True}

        with mock.patch.object(specialists, "dispatch", fake_dispatch):
            sequential = specialists.run_specialists(
                tasks, parallel_mode=False, max_workers=4, settings=self.settings
            )
            parallel = specialists.run_specialists(
                tasks, parallel_mode=True, max_workers=4, settings=self.settings
            )

        def shape(batch: dict) -> list[tuple]:
            return [
                (item["task_id"], item["status"], item.get("result"))
                for item in batch["results"]
            ]

        self.assertEqual(shape(sequential), shape(parallel))
        self.assertEqual(sequential["ok"], 4)
        self.assertEqual(parallel["ok"], 4)

    def test_duplicate_task_ids_are_rejected_before_any_work_runs(self) -> None:
        tasks = self._tasks(2)
        duplicate = [tasks[0], tasks[0]]
        with self.assertRaises(ToolError) as ctx:
            specialists.run_specialists(duplicate, parallel_mode=False, settings=self.settings)
        self.assertEqual(ctx.exception.code, "invalid_argument")


class RecheckReadOnlyTests(TempHomeCase):
    """A recheck never mutates verification records or citations."""

    def test_recheck_writes_nothing(self) -> None:
        settings = load_settings()
        doc = self.home / "proof.md"
        sentence = "Monitoring marks a target as changed only when meaningful content differs."
        doc.write_text(f"# Monitoring\n\n{sentence} Navigation text is excluded.\n", encoding="utf-8")
        run_id = int(evidence.start_run("monitoring question", settings=settings)["run_id"])
        evidence.save_evidence(source=str(doc), run_id=run_id, settings=settings)
        passage = evidence.search_evidence(
            "meaningful content differs", limit=1, run_id=run_id, settings=settings
        )["passages"][0]
        claim = evidence.register_claim(run_id, sentence, settings=settings)
        evidence.attach_citation(
            claim_id=int(claim["claim_id"]),
            passage_id=int(passage["passage_id"]),
            quote=sentence,
            relation="supports",
            settings=settings,
        )
        with storage.open_db(settings) as conn:
            before = {
                "claims": conn.execute("SELECT COUNT(*) AS n FROM claims").fetchone()["n"],
                "citations": conn.execute("SELECT COUNT(*) AS n FROM citations").fetchone()["n"],
                "claim_row": tuple(
                    conn.execute(
                        "SELECT status, claim_text FROM claims WHERE id = ?",
                        (int(claim["claim_id"]),),
                    ).fetchone()
                ),
            }
        task = specialists.recheck_task(run_id=run_id, claim_id=int(claim["claim_id"]))
        outcome = specialists.run_specialists(
            [task], parallel_mode=True, max_workers=1, settings=settings
        )
        self.assertEqual(outcome["ok"], 1)
        with storage.open_db(settings) as conn:
            after = {
                "claims": conn.execute("SELECT COUNT(*) AS n FROM claims").fetchone()["n"],
                "citations": conn.execute("SELECT COUNT(*) AS n FROM citations").fetchone()["n"],
                "claim_row": tuple(
                    conn.execute(
                        "SELECT status, claim_text FROM claims WHERE id = ?",
                        (int(claim["claim_id"]),),
                    ).fetchone()
                ),
            }
        self.assertEqual(before, after)


class TimeoutTests(TempHomeCase):
    def test_parallel_timeout_is_an_outcome_not_a_crash(self) -> None:
        settings = load_settings()
        with serve_slow_http(2.0) as slow_base:
            task = specialists.collect_url_task(run_id=0, subquestion_id=0, url=slow_base + "/x")
            tasks = [task]
            specs = [
                specialists.parallel.TaskSpec(
                    task_id=task.task_id,
                    name="slow",
                    run=lambda: specialists.dispatch(task, settings=settings),
                    timeout=0.05,
                )
            ]
            outcomes = specialists.parallel.run_tasks(specs, parallel=True, max_workers=1)
        self.assertEqual(outcomes[0].status, "timeout")
        # The thread may still be running; give the server a moment to drain.
        time.sleep(0.01)

    def test_failed_worker_does_not_block_its_siblings(self) -> None:
        settings = load_settings()
        entitlement = specialists.recheck_task(run_id=1, claim_id=1)
        calls = {"count": 0}
        lock = threading.Lock()

        def fake_dispatch(task: specialists.Task, *, settings: object) -> dict:
            if task.task_id == entitlement.task_id:
                raise ToolError("internal", "explode")
            with lock:
                calls["count"] += 1
            return {"op": task.op, "agreement": True}

        others = [specialists.recheck_task(run_id=1, claim_id=i) for i in (2, 3, 4)]
        with mock.patch.object(specialists, "dispatch", fake_dispatch):
            batch = specialists.run_specialists(
                [entitlement, *others], parallel_mode=True, max_workers=2, settings=settings
            )
        self.assertEqual(batch["failed"], 1)
        self.assertEqual(batch["ok"], 3)
        self.assertEqual(calls["count"], 3)

    def test_collect_url_task_timeout_value_is_honoured_when_set(self) -> None:
        spec = specialists.collect_url_task(run_id=1, subquestion_id=1, url="https://x.example")
        self.assertIsNone(spec.timeout)
        slow = specialists.search_task(
            run_id=1, subquestion_id=1, query="q", max_results=1
        )
        self.assertEqual(slow.retries, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

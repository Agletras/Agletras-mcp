#!/usr/bin/env python3
"""Phase 8 benchmark: measure what the new features actually buy.

Runs entirely offline against a throwaway local HTTP server and a temporary
``$AGLETRAS_HOME`` (no network, no project data touched) and measures, on
identical workloads:

1. scheduler parallelism -- the same sleep workload, sequential vs 4 workers;
2. specialist collection -- the same number of equally slow URLs, sequential
   vs parallel (distinct URL sets per mode; no cache hits either way);
3. monitoring checks -- the same equally slow targets, sequential vs parallel;
4. evidence reuse -- two identical pipeline runs, the second with
   ``--reuse-evidence``; counts real HTTP fetches saved and time saved;
5. resume safety -- a completed run marked interrupted, resumed: completed
   stages are skipped and zero extra document fetches happen;
6. memory retrieval -- precision/recall@5 of ``memory.recall`` over a labeled
   corpus (24 passages, 6 topics, 4 relevant each).

Usage:
    .venv/bin/python scripts/evaluate_phase8.py            # human summary
    .venv/bin/python scripts/evaluate_phase8.py --json     # machine JSON
    .venv/bin/python scripts/evaluate_phase8.py --out docs/phase8-measurements.md

The Markdown file is *generated from the measured numbers*; nothing in it is
hand-tuned or estimated.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agletras_mcp import __version__  # noqa: E402  (after sys.path setup)

QUESTION = "How does agletras store evidence passages and attach citations to claims?"
SUBQUESTION_ONE = "How are evidence passages stored?"
SUBQUESTION_TWO = "How are citations attached to claims?"

DOC_ONE = (
    "<html><head><title>Evidence storage</title></head><body><p>"
    "Agletras stores evidence as indexed passages inside a local SQLite database. "
    "Each passage keeps a stable SHA-256 hash and its exact character offsets, so "
    "citations can point at the passage text."
    "</p></body></html>"
).encode()
DOC_TWO = (
    "<html><head><title>Citations</title></head><body><p>"
    "Citations attach one exact stored passage to one claim and record the relation. "
    "Verification status is derived from the citation relations, so a claim is never "
    "marked supported by registration alone."
    "</p></body></html>"
).encode()

SLOW_DELAY = 0.12
SLEEP_DELAY = 0.10
WORKERS = 4
SLOW_COUNT = 8
MONITOR_COUNT = 6
TOPICS = ("heliotrope", "obsidian", "quartzite", "marlin", "pelican", "tundra")
RELEVANT_PER_TOPIC = 4


class Fixture:
    """Mutable routes + per-path request counters for the local server."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes, float]] = {}
        self.counts: dict[str, int] = {}

    def add(self, path: str, body: bytes, *, delay: float = 0.0) -> None:
        self.routes[path] = (200, body, delay)

    def doc_requests(self) -> int:
        return self.counts.get("/doc-one", 0) + self.counts.get("/doc-two", 0)

    def total_requests(self) -> int:
        return sum(self.counts.values())


def start_server(fixture: Fixture) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:  # noqa: N802 - http.server API
            path = self.path.split("?")[0]
            with lock:
                fixture.counts[path] = fixture.counts.get(path, 0) + 1
            route = fixture.routes.get(path)
            if route is None:
                status, body, delay = 404, b"not found", 0.0
            else:
                status, body, delay = route
            if delay:
                time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            self._serve()

        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._serve()

        def log_message(self, fmt, *args) -> None:  # noqa: A002
            return

    lock = threading.Lock()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def build_search_page(base: str) -> bytes:
    def ddg(url: str) -> str:
        return "//duckduckgo.com/l/?uddg=" + urllib.parse.quote(url, safe="")

    return (
        "<!doctype html><html><body>"
        f'<h2 class="result__title"><a class="result__a" href="{ddg(base + "/doc-one")}">'
        "Evidence storage</a></h2>"
        '<a class="result__snippet">How evidence passages are stored.</a>'
        f'<h2 class="result__title"><a class="result__a" href="{ddg(base + "/doc-two")}">'
        "Citations</a></h2>"
        '<a class="result__snippet">How citations attach passages to claims.</a>'
        "</body></html>"
    ).encode()


def invoke(cli, argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.run(argv)
        except SystemExit as exc:  # pragma: no cover - help paths
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


def _verdict(speedup: float) -> str:
    if speedup >= 1.2:
        return "keep (measurably faster)"
    if speedup >= 1.05:
        return "keep (slightly faster)"
    return "reconsider (not measurably faster)"


def bench_scheduler() -> dict:
    from agletras_mcp import parallel

    def tasks() -> list:
        return [
            parallel.TaskSpec(
                task_id=f"sleep-{index}",
                name=f"sleep {index}",
                run=lambda index=index: (time.sleep(SLEEP_DELAY), index)[1],
            )
            for index in range(SLOW_COUNT)
        ]

    started = time.perf_counter()
    sequential = parallel.run_tasks(tasks(), parallel=False)
    sequential_s = time.perf_counter() - started
    started = time.perf_counter()
    concurrent = parallel.run_tasks(tasks(), parallel=True, max_workers=WORKERS)
    parallel_s = time.perf_counter() - started
    assert [item.result for item in sequential] == [item.result for item in concurrent]
    return {
        "workload": f"{SLOW_COUNT} sleep tasks of {SLEEP_DELAY:.2f}s",
        "sequential_s": round(sequential_s, 4),
        "parallel_s": round(parallel_s, 4),
        "speedup": round(sequential_s / parallel_s, 2) if parallel_s else None,
        "workers": WORKERS,
    }


def bench_collect(base: str, settings) -> dict:
    from agletras_mcp import evidence, planner, specialists

    run_id = int(evidence.start_run("Phase 8 parallel collection benchmark", settings=settings)["run_id"])
    plan = planner.save_plan(
        run_id,
        objective="Benchmark sequential vs parallel URL collection.",
        subquestions=[SUBQUESTION_ONE, SUBQUESTION_TWO],
        settings=settings,
    )
    subquestion_id = int(plan["subquestions"][0]["subquestion_id"])

    def batch(prefix: str) -> list:
        return [
            specialists.collect_url_task(
                run_id=run_id,
                subquestion_id=subquestion_id,
                url=f"{base}/{prefix}-{index}",
                seq=index,
            )
            for index in range(SLOW_COUNT)
        ]

    started = time.perf_counter()
    sequential = specialists.run_specialists(batch("seq"), parallel_mode=False, settings=settings)
    sequential_s = time.perf_counter() - started
    started = time.perf_counter()
    concurrent = specialists.run_specialists(
        batch("par"), parallel_mode=True, max_workers=WORKERS, settings=settings
    )
    parallel_s = time.perf_counter() - started
    assert sequential["ok"] == SLOW_COUNT and concurrent["ok"] == SLOW_COUNT, (
        sequential["warnings"],
        concurrent["warnings"],
    )
    return {
        "workload": f"{SLOW_COUNT} URLs served with {SLOW_DELAY:.2f}s latency (distinct URL sets per mode)",
        "sequential_s": round(sequential_s, 4),
        "parallel_s": round(parallel_s, 4),
        "speedup": round(sequential_s / parallel_s, 2) if parallel_s else None,
        "sources_saved_sequential": len(sequential["source_ids"]),
        "sources_saved_parallel": len(concurrent["source_ids"]),
        "workers": WORKERS,
    }


def bench_monitoring(base: str, fixture: Fixture, settings) -> dict:
    from agletras_mcp import monitor

    for index in range(MONITOR_COUNT):
        monitor.add_target(
            f"{base}/watch-{index}", name=f"watch {index}", project="bench", settings=settings
        )
    started = time.perf_counter()
    sequential = monitor.run_checks(project="bench", parallel_mode=False, settings=settings)
    sequential_s = time.perf_counter() - started
    started = time.perf_counter()
    concurrent = monitor.run_checks(
        project="bench", parallel_mode=True, max_workers=WORKERS, settings=settings
    )
    parallel_s = time.perf_counter() - started
    assert sequential["checked"] == MONITOR_COUNT and concurrent["checked"] == MONITOR_COUNT

    # Change-detection precision: exactly one target changes, zero false alerts.
    fixture.add("/watch-3", b"<html><body>Renewed finding: the rate changed.</body></html>", delay=SLOW_DELAY)
    after = monitor.run_checks(project="bench", parallel_mode=True, max_workers=WORKERS, settings=settings)
    statuses = [snapshot["status"] for snapshot in after["snapshots"]]
    changes = monitor.changes(project="bench", settings=settings)
    return {
        "workload": f"{MONITOR_COUNT} targets served with {SLOW_DELAY:.2f}s latency",
        "sequential_s": round(sequential_s, 4),
        "parallel_s": round(parallel_s, 4),
        "speedup": round(sequential_s / parallel_s, 2) if parallel_s else None,
        "changed_after_edit": statuses.count("changed"),
        "unchanged_after_edit": statuses.count("unchanged"),
        "false_alerts": statuses.count("changed") - 1,
        "changes_listed": changes["count"],
        "workers": WORKERS,
    }


def bench_reuse_and_resume(fixture: Fixture, settings) -> dict:
    from agletras_mcp import cli, runs

    common = [
        "run",
        QUESTION,
        "--depth",
        "quick",
        "--subquestions",
        SUBQUESTION_ONE,
        "--subquestions",
        SUBQUESTION_TWO,
        "--json",
    ]
    before = fixture.doc_requests()
    code, out, err = invoke(cli, common)
    assert code == 0, err
    first = json.loads(out)
    first_doc_requests = fixture.doc_requests() - before

    before = fixture.doc_requests()
    code, out, err = invoke(cli, [*common, "--reuse-evidence"])
    assert code == 0, err
    second = json.loads(out)
    second_doc_requests = fixture.doc_requests() - before

    # Resume: mark the completed second run as interrupted, then resume it.
    runs.set_run_status(second["run_id"], "interrupted", settings=settings)
    before = fixture.doc_requests()
    code, out, err = invoke(cli, ["resume", str(second["run_id"]), "--json"])
    assert code == 0, err
    resumed = json.loads(out)
    resume_doc_requests = fixture.doc_requests() - before
    assert resumed["status"] == "completed", resumed

    return {
        "first_run_duration_ms": first.get("duration_ms"),
        "second_run_duration_ms": second.get("duration_ms"),
        "doc_fetches_first": first_doc_requests,
        "doc_fetches_second_reuse": second_doc_requests,
        "doc_fetches_saved": first_doc_requests - second_doc_requests,
        "reused_sources_second_run": second.get("sources_count"),
        "resume_duration_ms": resumed.get("duration_ms"),
        "resume_doc_fetches": resume_doc_requests,
        "resumed": bool(resumed.get("resumed")),
    }


def bench_retrieval(settings) -> dict:
    from agletras_mcp import evidence, memory, planner

    run_id = int(evidence.start_run("Phase 8 retrieval benchmark", settings=settings)["run_id"])
    # The default cap is 20 unique sources per run; this labeled corpus needs
    # more, so the limit is raised explicitly for the benchmark run.
    planner.save_plan(
        run_id,
        objective="Retrieval benchmark corpus.",
        subquestions=["Benchmark retrieval"],
        limits={"max_unique_sources": len(TOPICS) * RELEVANT_PER_TOPIC + 4},
        settings=settings,
    )
    for topic in TOPICS:
        for index in range(RELEVANT_PER_TOPIC):
            evidence.save_evidence(
                run_id=run_id,
                title=f"{topic} passage {index}",
                text=(
                    f"The {topic} marker number {index} records a distinct observation, "
                    f"and only the {topic} topic uses this wording."
                ),
                settings=settings,
            )
    precisions, recalls = [], []
    per_topic: dict[str, dict] = {}
    for topic in TOPICS:
        hits = memory.recall(topic, limit=5, settings=settings)["passages"]
        relevant = [hit for hit in hits if topic in (hit["text"] or "").lower()]
        precision = len(relevant) / len(hits) if hits else 0.0
        recall = len(relevant) / RELEVANT_PER_TOPIC
        precisions.append(precision)
        recalls.append(recall)
        per_topic[topic] = {
            "returned": len(hits),
            "relevant": len(relevant),
            "precision_at_5": round(precision, 4),
            "recall_at_5": round(recall, 4),
        }
    return {
        "corpus": f"{len(TOPICS)} topics x {RELEVANT_PER_TOPIC} passages",
        "queries": len(TOPICS),
        "mean_precision_at_5": round(sum(precisions) / len(precisions), 4),
        "mean_recall_at_5": round(sum(recalls) / len(recalls), 4),
        "per_topic": per_topic,
    }


def render_markdown(results: dict[str, dict]) -> str:
    def speedup_line(key: str) -> str:
        item = results[key]
        return (
            f"| {item['workload']} | {item['sequential_s']:.3f} | {item['parallel_s']:.3f} "
            f"| {item['speedup']:.2f}x | {_verdict(item['speedup'])} |"
        )

    reuse = results["reuse_resume"]
    retrieval = results["retrieval"]
    monitoring = results["monitoring"]
    lines = [
        "# Phase 8 measurements (generated)",
        "",
        "Generated by `scripts/evaluate_phase8.py` on a throwaway local HTTP server and a",
        "temporary `$AGLETRAS_HOME`. These are real measured numbers, not estimates.",
        "",
        f"- environment: Python {results['environment']['python']}, agletras-mcp "
        f"{results['environment']['version']}, workers={WORKERS}",
        f"- scheduler workload: {results['scheduler']['workload']}",
        "",
        "## Parallel vs sequential (identical workloads)",
        "",
        "| workload | sequential (s) | parallel (s) | speedup | verdict |",
        "| --- | --- | --- | --- | --- |",
        speedup_line("scheduler"),
        speedup_line("collect"),
        speedup_line("monitoring"),
        "",
        "Parallelism is kept only where it is measurably faster; the default stays",
        "sequential and `--parallel` is opt-in.",
        "",
        "## Evidence reuse (same question, second run with `--reuse-evidence`)",
        "",
        f"- first run: {reuse['doc_fetches_first']} document fetches in {reuse['first_run_duration_ms']} ms",
        f"- second run (reuse): {reuse['doc_fetches_second_reuse']} document fetches in "
        f"{reuse['second_run_duration_ms']} ms",
        f"- document fetches saved: {reuse['doc_fetches_saved']} "
        f"(sources linked in the second run: {reuse['reused_sources_second_run']})",
        "",
        "## Resume safety",
        "",
        f"- completed stages skipped: resumed run finished in {reuse['resume_duration_ms']} ms "
        f"with {reuse['resume_doc_fetches']} additional document fetches",
        f"- status after resume: completed (resumed flag: {str(reuse['resumed']).lower()})",
        "",
        "## Monitoring change detection",
        "",
        f"- targets checked: {MONITOR_COUNT}; one edited, {monitoring['changed_after_edit']} "
        f"reported changed, {monitoring['unchanged_after_edit']} unchanged, "
        f"{monitoring['false_alerts']} false alerts",
        f"- `monitor_changes` listed {monitoring['changes_listed']} change(s)",
        "",
        "## Memory retrieval (labeled corpus)",
        "",
        f"- {retrieval['corpus']}, {retrieval['queries']} queries",
        f"- mean precision@5: {retrieval['mean_precision_at_5']}",
        f"- mean recall@5: {retrieval['mean_recall_at_5']}",
        "",
        "| topic | returned | relevant | precision@5 | recall@5 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for topic, item in retrieval["per_topic"].items():
        lines.append(
            f"| {topic} | {item['returned']} | {item['relevant']} | "
            f"{item['precision_at_5']} | {item['recall_at_5']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 8 offline benchmark")
    parser.add_argument("--json", action="store_true", help="print the full results as JSON")
    parser.add_argument(
        "--out",
        metavar="PATH",
        default="",
        help="write the generated Markdown measurements to this path",
    )
    args = parser.parse_args()

    fixture = Fixture()
    fixture.add("/doc-one", DOC_ONE)
    fixture.add("/doc-two", DOC_TWO)
    for index in range(SLOW_COUNT):
        fixture.add(f"/seq-{index}", b"<html><body>sequential page</body></html>", delay=SLOW_DELAY)
        fixture.add(f"/par-{index}", b"<html><body>parallel page</body></html>", delay=SLOW_DELAY)
    for index in range(MONITOR_COUNT):
        fixture.add(
            f"/watch-{index}",
            f"<html><body>Baseline {index} substantive finding.</body></html>".encode(),
            delay=SLOW_DELAY,
        )
    server, base = start_server(fixture)
    fixture.add("/search", build_search_page(base))

    home = tempfile.mkdtemp(prefix="agletras-phase8-bench-", dir="/tmp/opencode")
    env = dict(
        os.environ,
        AGLETRAS_HOME=home,
        AGLETRAS_SEARCH_PROVIDER="duckduckgo",
        AGLETRAS_SEARCH_ENDPOINT=base + "/search",
        AGLETRAS_ALLOW_PRIVATE_HOSTS="1",
        AGLETRAS_SEARCH_MIN_INTERVAL="0",
        AGLETRAS_COLLECT_BACKOFF="0",
    )
    saved_env = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        from agletras_mcp.config import load_settings

        settings = load_settings()
        results: dict[str, dict] = {
            "environment": {
                "python": sys.version.split()[0],
                "version": __version__,
                "home": home,
            },
            "scheduler": bench_scheduler(),
            "collect": bench_collect(base, settings),
            "monitoring": bench_monitoring(base, fixture, settings),
            "reuse_resume": bench_reuse_and_resume(fixture, settings),
            "retrieval": bench_retrieval(settings),
        }
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        server.shutdown()
        server.server_close()

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        for name in ("scheduler", "collect", "monitoring"):
            item = results[name]
            print(
                f"{name}: sequential {item['sequential_s']:.3f}s vs parallel "
                f"{item['parallel_s']:.3f}s ({item['speedup']:.2f}x) -- {_verdict(item['speedup'])}"
            )
        reuse = results["reuse_resume"]
        print(
            f"reuse: doc fetches {reuse['doc_fetches_first']} -> {reuse['doc_fetches_second_reuse']} "
            f"({reuse['doc_fetches_saved']} saved); resume fetched {reuse['resume_doc_fetches']} documents"
        )
        retrieval = results["retrieval"]
        print(
            f"retrieval: precision@5 {retrieval['mean_precision_at_5']}, "
            f"recall@5 {retrieval['mean_recall_at_5']}"
        )

    if args.out:
        target = Path(args.out)
        if not target.is_absolute():
            target = PROJECT_ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_markdown(results), encoding="utf-8")
        print(f"measurements written to {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

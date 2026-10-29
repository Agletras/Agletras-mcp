#!/usr/bin/env python3
"""Offline end-to-end demo of the `agletras` CLI research pipeline.

Runs the real pipeline (plan -> search -> collect -> claims -> verify ->
evaluate -> validated Markdown report) against a throwaway local HTTP
server, so it works with no network access and no external services:

    .venv/bin/python scripts/demo_research.py            # human output
    .venv/bin/python scripts/demo_research.py --json     # machine output
    .venv/bin/python scripts/demo_research.py --report   # also print the report

The mock search endpoint serves two local HTML pages about how agletras
stores evidence and attaches citations. State lives in a temporary
$AGLETRAS_HOME; nothing touches the project's data/ or reports/ directories.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
os.environ.setdefault("PYTHONPATH", str(PROJECT_ROOT / "src"))

QUESTION = "How does agletras store evidence passages and attach citations to claims?"

DOC_ONE = (
    "<html><head><title>Evidence storage</title></head><body><p>"
    "Agletras stores evidence as indexed passages inside a local SQLite database. "
    "Each passage keeps a stable SHA-256 hash and its exact character offsets, so "
    "citations can point at the passage text. The passage index uses FTS5 "
    "full-text search when the SQLite build supports it."
    "</p></body></html>"
).encode()

DOC_TWO = (
    "<html><head><title>Citations</title></head><body><p>"
    "Citations attach one exact stored passage to one claim and record the relation, "
    "such as supports or contradicts. Verification status is derived from the citation "
    "relations, so a claim is never marked supported by registration alone. Reports "
    "render stable citation identifiers and list the references section."
    "</p></body></html>"
).encode()


def build_search_page(base: str) -> bytes:
    def ddg(url: str) -> str:
        return "//duckduckgo.com/l/?uddg=" + urllib.parse.quote(url, safe="")

    return (
        "<!doctype html><html><body>"
        f'<h2 class="result__title"><a class="result__a" href="{ddg(base + "/doc-one")}">Evidence storage</a></h2>'
        '<a class="result__snippet">How evidence passages are stored.</a>'
        f'<h2 class="result__title"><a class="result__a" href="{ddg(base + "/doc-two")}">Citations</a></h2>'
        '<a class="result__snippet">How citations attach passages to claims.</a>'
        "</body></html>"
    ).encode()


def start_mock_server() -> tuple[threading.Thread, str]:
    """Serve /search (DuckDuckGo-style links) and two local documents."""
    import threading as _t

    routes: dict[str, tuple] = {}

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:  # noqa: N802 - http.server API
            path = self.path.split("?")[0]
            if path == "/doc-one":
                status, body = 200, DOC_ONE
            elif path == "/doc-two":
                status, body = 200, DOC_TWO
            elif path == "/search":
                status, body = 200, build_search_page(BASE[0])
            else:
                status, body = 404, b"not found"
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self._serve()

        def do_POST(self) -> None:  # noqa: N802 - search form
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._serve()

        def log_message(self, fmt, *args) -> None:  # noqa: A002
            return  # quiet

    BASE = ["http://127.0.0.1:0"]  # filled in after the port is known
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    BASE[0] = f"http://127.0.0.1:{server.server_port}"
    thread = _t.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread, BASE[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline agletras research demo")
    parser.add_argument("--json", action="store_true", help="print the run result as JSON")
    parser.add_argument("--report", action="store_true", help="also print the generated report")
    parser.add_argument("--depth", choices=("quick", "standard", "thorough"), default="quick")
    args = parser.parse_args()

    from agletras_mcp import cli  # imported late so --help stays fast

    home = tempfile.mkdtemp(prefix="agletras-demo-", dir="/tmp/opencode")
    thread, base = start_mock_server()
    env = dict(
        os.environ,
        AGLETRAS_HOME=home,
        AGLETRAS_SEARCH_PROVIDER="duckduckgo",
        AGLETRAS_SEARCH_ENDPOINT=base + "/search",
        AGLETRAS_ALLOW_PRIVATE_HOSTS="1",
        AGLETRAS_SEARCH_MIN_INTERVAL="0",
        AGLETRAS_COLLECT_BACKOFF="0",
    )
    old_environ = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        code, out, _err = _invoke(cli, [
            "run",
            QUESTION,
            "--depth",
            args.depth,
            "--subquestions",
            "How are evidence passages stored?",
            "--subquestions",
            "How are citations attached to claims?",
            "--json",
        ])
        if code != 0:
            print(f"demo: research run failed with exit code {code}", file=sys.stderr)
            return code

        result = json.loads(out)
        if args.json:
            print(out, end="")
        else:
            print(
                f"run {result['run_id']}: {result['status']} "
                f"(sources={result['sources_count']}, claims={result['claims_count']}, "
                f"answered={result['answered']}/{result['subquestions']})"
            )
            print(f"report: {result['report']['path']} (validation: {result['report']['validation_status']})")
            print(f"log:    {result['log_file']}")

        if args.report or not args.json:
            run_id = result["run_id"]
            code2, out2, _ = _invoke(cli, ["report", str(run_id)])
            if code2 == 0:
                print(out2, end="" if out2.endswith("\n") else "\n")
        return 0
    finally:
        for key, value in old_environ.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        thread.join(timeout=5)


def _invoke(cli, argv: list[str]) -> tuple[int, str, str]:
    """Run the CLI in-process, capturing stdout/stderr."""
    import io
    from contextlib import redirect_stderr, redirect_stdout

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.run(argv)
        except SystemExit as exc:  # pragma: no cover - defensive
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


if __name__ == "__main__":
    raise SystemExit(main())

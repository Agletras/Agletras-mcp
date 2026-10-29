#!/usr/bin/env python3
"""Launch the Agletras MCP server with the project's virtualenv.

OpenCode can start a local MCP server without an activated shell.  This tiny
launcher locates ``.venv`` next to the project, re-executes the server with
that interpreter when it exists, and otherwise falls back to the current
interpreter with ``src/`` on ``sys.path``.

Usage (also used by ``opencode.json``)::

    python scripts/run_mcp.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = (
    PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if os.name == "nt"
    else PROJECT_ROOT / ".venv" / "bin" / "python"
)


def main() -> int:
    if VENV_PYTHON.exists():
        try:
            same = Path(sys.executable).resolve() == VENV_PYTHON.resolve()
        except OSError:
            same = False
        if not same:
            return subprocess.call(
                [str(VENV_PYTHON), "-m", "agletras_mcp", *sys.argv[1:]],
                stdin=sys.stdin,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )

    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from agletras_mcp.__main__ import main as server_main

    server_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Entry point: ``python -m agletras_mcp`` (or the ``agletras-mcp`` script).

Runs the MCP server over the stdio transport, which is how OpenCode launches
local MCP servers. Logging goes to stderr only.
"""

from __future__ import annotations

import asyncio
import logging
import sys


def main() -> None:
    """Run the agletras MCP server over stdio."""
    from . import __version__
    from .config import load_settings
    from .server import server

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    logging.getLogger(__name__).info(
        "agletras-mcp %s starting (db=%s)", __version__, settings.db_path
    )
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()

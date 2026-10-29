"""Error types shared by all agletras-mcp tools.

Every tool failure raises :class:`ToolError`; the server layer converts it into
a uniform JSON error envelope (see :mod:`agletras_mcp.results`).
"""

from __future__ import annotations


class ToolError(Exception):
    """A tool failure with a stable machine-readable code.

    Codes used by this project:
    ``invalid_argument``, ``not_found``, ``network_error``, ``too_large``,
    ``unsupported_content``, ``pdf_error``, ``storage_error``,
    ``empty_selection``, ``rate_limited``, ``blocked_host``,
    ``too_many_redirects``, ``provider_error``, ``conflict``, ``internal``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

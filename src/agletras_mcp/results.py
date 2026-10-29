"""Uniform JSON envelopes for all MCP tool results.

Convention: every tool returns a single JSON string. Success payloads have
``"ok": true``; failures have ``"ok": false`` and an ``error`` object with a
stable ``code`` and human-readable ``message``. Tool failures are returned as
data (not protocol errors) so models can react to them.
"""

from __future__ import annotations

import json
from typing import Any


def ok(**fields: Any) -> str:
    """Serialize a success payload."""
    payload: dict[str, Any] = {"ok": True}
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False, default=str)


def fail(code: str, message: str, **fields: Any) -> str:
    """Serialize an error payload."""
    payload: dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False, default=str)

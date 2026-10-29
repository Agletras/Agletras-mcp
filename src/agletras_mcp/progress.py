"""Structured progress reporting for the CLI (Phase 7).

Two output channels are kept strictly apart:

- **Human-readable progress** (default) or **JSON Lines events**
  (``--log-format json``) go to *stderr*, never stdout, so the CLI's final
  machine-readable result on stdout stays parseable.
- **Run log files** are written under ``logs_dir`` as JSON Lines, separate
  from the Markdown reports in ``reports_dir``.

Every event carries the run id when known, the stage, a UTC timestamp, an
event type, and — for finished stages — the duration. Messages are clipped and
credential-bearing URLs are sanitized (``https://user:secret@host`` becomes
``https://user:***@host``); callers must still never pass secrets as data.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

VERBOSITY_LEVELS = {"quiet": 0, "normal": 1, "verbose": 2}
LOG_FORMATS = ("human", "json")

_MAX_MESSAGE = 500
_CREDENTIAL_URL = re.compile(r"(?i)\b(https?://)([^/\s:@]+):([^@\s/]+)@")


def sanitize_text(text: str) -> str:
    """Clip one log message, collapse whitespace, and strip credentials in URLs."""
    clean = _CREDENTIAL_URL.sub(r"\1\2:***@", str(text or ""))
    clean = re.sub(r"\s+", " ", clean.replace("\r", " ").replace("\n", " ")).strip()
    if len(clean) > _MAX_MESSAGE:
        clean = clean[: _MAX_MESSAGE - 3] + "..."
    return clean


def _safe_data(data: dict[str, Any] | None) -> dict[str, Any]:
    """Shape event data into short, JSON-safe values (never dumps documents)."""
    safe: dict[str, Any] = {}
    for key, value in (data or {}).items():
        if value is None or value == "":
            continue
        if isinstance(value, (int, float, bool)):
            safe[str(key)] = value
        elif isinstance(value, (list, tuple)):
            items = [sanitize_text(str(item)) for item in list(value)[:20]]
            safe[str(key)] = items
        elif isinstance(value, dict):
            safe[str(key)] = {
                str(k): (v if isinstance(v, (int, float, bool)) else sanitize_text(str(v)))
                for k, v in list(value.items())[:20]
            }
        else:
            safe[str(key)] = sanitize_text(str(value))
    return safe


class ProgressReporter:
    """One reporter per CLI invocation; never writes to stdout."""

    def __init__(
        self,
        *,
        run_id: int = 0,
        verbosity: str = "normal",
        log_format: str = "human",
        stream: TextIO | None = None,
        log_file_path: str | Path | None = None,
    ) -> None:
        if verbosity not in VERBOSITY_LEVELS:
            raise ValueError(f"verbosity must be one of {', '.join(VERBOSITY_LEVELS)}")
        if log_format not in LOG_FORMATS:
            raise ValueError(f"log_format must be one of {', '.join(LOG_FORMATS)}")
        self.run_id = int(run_id or 0)
        self.verbosity = verbosity
        self.log_format = log_format
        self.stream = stream or sys.stderr
        self._file: TextIO | None = None
        self.log_file_path: Path | None = None
        # Every record is buffered until a log file is attached (the CLI only
        # learns the run id — and therefore the default file name — after the
        # run row exists), then the backlog is flushed to the file.
        self._buffer: list[dict[str, Any]] = []
        if log_file_path:
            self.attach_log_file(log_file_path)
        self._started = time.monotonic()

    # -- emission -----------------------------------------------------------

    _BUFFER_LIMIT = 2000

    def attach_log_file(self, log_file_path: str | Path) -> Path:
        """Open (append) a JSONL log file and flush the buffered records into it."""
        if self._file is not None:
            self.close()
        path = Path(log_file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")
        for record in self._buffer:
            self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()
        self.log_file_path = path
        return path

    def _emit(self, event: str, message: str, *, level: str = "info", **fields: Any) -> dict[str, Any]:
        threshold = {"debug": 2, "info": 1, "warning": 0, "error": 0}[level]
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "level": level,
        }
        if self.run_id:
            record["run_id"] = self.run_id
        for key in ("stage", "status", "duration_ms"):
            value = fields.get(key)
            if value not in (None, ""):
                record[key] = value
        if message:
            record["message"] = sanitize_text(message)
        data = fields.get("data")
        if isinstance(data, dict) and data:
            record["data"] = _safe_data(data)
        self._buffer.append(record)
        if len(self._buffer) > self._BUFFER_LIMIT:
            del self._buffer[: len(self._buffer) - self._BUFFER_LIMIT]
        if self._file is not None:
            self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._file.flush()
        if VERBOSITY_LEVELS[self.verbosity] >= threshold:
            self._render(record)
        return record

    def _render(self, record: dict[str, Any]) -> None:
        if self.log_format == "json":
            self.stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.stream.flush()
            return
        stamp = record["ts"][11:19]
        parts = [f"[{stamp}]"]
        if record.get("run_id"):
            parts.append(f"run={record['run_id']}")
        if record.get("stage"):
            parts.append(f"stage={record['stage']}")
        parts.append(record["event"])
        if record.get("status"):
            parts.append(f"({record['status']})")
        line = " ".join(parts)
        if record.get("message"):
            line += f": {record['message']}"
        if record.get("duration_ms") is not None:
            line += f" [{record['duration_ms']} ms]"
        self.stream.write(line + "\n")
        self.stream.flush()

    # -- convenience --------------------------------------------------------

    def bind_run(self, run_id: int) -> None:
        self.run_id = int(run_id)

    def debug(self, message: str, **data: Any) -> dict[str, Any]:
        return self._emit("debug", message, level="debug", data=data)

    def info(self, event: str, message: str = "", **fields: Any) -> dict[str, Any]:
        return self._emit(event, message, level="info", **fields)

    def warn(self, message: str, **data: Any) -> dict[str, Any]:
        return self._emit("warning", message, level="warning", data=data)

    def error(self, message: str, *, code: str = "", stage: str = "", **data: Any) -> dict[str, Any]:
        payload = dict(data)
        if code:
            payload["error_code"] = code
        return self._emit("error", message, level="error", stage=stage, data=payload)

    def stage_start(self, stage: str, message: str = "", **data: Any) -> None:
        self._emit("stage_start", message, stage=stage, status="started", data=data)

    def stage_end(
        self,
        stage: str,
        status: str,
        *,
        message: str = "",
        duration_ms: int | None = None,
        **data: Any,
    ) -> None:
        self._emit("stage_end", message, stage=stage, status=status, duration_ms=duration_ms, data=data)

    def stage_skip(self, stage: str, message: str = "already completed; skipping") -> None:
        self._emit("stage_end", message, stage=stage, status="skipped", data={})

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            finally:
                self._file = None

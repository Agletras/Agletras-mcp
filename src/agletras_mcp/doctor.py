"""Bounded health checks behind ``agletras doctor``.

Every check is cheap, local, and never mutates research data: it opens the
database (creating it if missing, as any command would), probes writable
directories, imports the required packages, and — only when a model endpoint
is explicitly configured — makes a single bounded TCP connect. Secrets never
appear in results: endpoint URLs are reduced to scheme/host/port and API keys
are only referenced by their environment variable *name*.

Failures that make the tool unusable (runtime, packages, config, paths,
database, MCP startup) are ``required``; feature-specific problems (vision,
model endpoints, OpenCode wiring) are ``optional`` and never flip the exit
code on their own.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import json
import os
import socket
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__, storage
from .config import (
    PROJECT_ROOT,
    ConfigError,
    Settings,
    load_settings,
    settings_snapshot,
    validate_settings,
)

# ---------------------------------------------------------------------------
# Result plumbing
# ---------------------------------------------------------------------------

CHECK_OK = "ok"
CHECK_WARN = "warn"
CHECK_FAIL = "fail"
CHECK_SKIPPED = "skipped"

#: Exit codes returned by :func:`run_doctor` (mirror the CLI contract).
EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_DEPENDENCY = 3

MINIMUM_PACKAGES: dict[str, str] = {"mcp": "2.3", "pypdf": "6.21"}


@dataclass
class CheckResult:
    """One diagnostic check: what ran, what it found, and how to fix it."""

    name: str
    status: str
    required: bool
    detail: str
    remediation: str = ""
    duration_ms: int = 0
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "required": self.required,
            "detail": self.detail,
            "remediation": self.remediation,
            "duration_ms": self.duration_ms,
            "data": self.data,
        }


def _parse_version(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(raw).split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _version_at_least(found: str, minimum: str) -> bool:
    have, want = _parse_version(found), _parse_version(minimum)
    return have[: len(want)] >= want


def _safe_endpoint(endpoint: str) -> str:
    """``scheme://host:port`` only — never userinfo, path, or query."""
    parts = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    scheme = parts.scheme or "http"
    return f"{scheme}://{host}{port}" if host else "<unparseable endpoint>"


def _tcp_probe(endpoint: str, timeout: float = 2.0) -> dict[str, Any]:
    """One bounded TCP connect; sends no bytes and logs no credentials."""
    parts = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
    if not host:
        return {"reachable": False, "error": "endpoint has no host"}
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return {"reachable": True, "latency_ms": int((time.monotonic() - started) * 1000)}
    except OSError as exc:
        return {"reachable": False, "error": f"{exc.__class__.__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_runtime() -> CheckResult:
    version = sys.version_info
    detail = (
        f"Python {version.major}.{version.minor}.{version.micro} "
        f"({sys.executable}); platform {sys.platform}"
    )
    if version < (3, 11):
        return CheckResult(
            "runtime",
            CHECK_FAIL,
            True,
            detail,
            remediation="Install Python 3.11 or newer (pyproject requires >=3.11).",
        )
    return CheckResult("runtime", CHECK_OK, True, detail, data={"executable": sys.executable})


def _check_dependencies() -> CheckResult:
    problems: list[str] = []
    found: dict[str, str] = {}
    for package, minimum in MINIMUM_PACKAGES.items():
        try:
            module = importlib.import_module(package)
        except ImportError as exc:
            problems.append(f"{package} is not importable ({exc})")
            continue
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            version = getattr(module, "__version__", "unknown")
        found[package] = version
        if version != "unknown" and not _version_at_least(version, minimum):
            problems.append(f"{package} {version} is older than the required >={minimum}")
    detail = ", ".join(f"{name} {version}" for name, version in sorted(found.items()))
    if problems:
        return CheckResult(
            "dependencies",
            CHECK_FAIL,
            True,
            "; ".join(problems),
            remediation=(
                "Run `pip install -e .` inside the project virtualenv, or "
                "`pip install 'mcp>=2.3,<3' 'pypdf>=6.21,<7'`."
            ),
            data={"packages": found},
        )
    return CheckResult("dependencies", CHECK_OK, True, detail, data={"packages": found})


def _check_config(
    settings: Settings | None, config_error: str | None = None
) -> CheckResult:
    if config_error:
        return CheckResult(
            "config",
            CHECK_FAIL,
            True,
            config_error,
            remediation=(
                "Fix the config file (or unset AGLETRAS_CONFIG), then run "
                "`agletras config validate` to re-check without starting doctor."
            ),
        )
    if settings is None:  # defensive: the caller normally loads settings first
        try:
            settings = load_settings()
        except ConfigError as exc:
            return CheckResult(
                "config",
                CHECK_FAIL,
                True,
                str(exc),
                remediation="Fix the config file, then re-run `agletras doctor`.",
            )
    report = validate_settings(settings)
    source = str(settings.config_path) if settings.config_path else "built-in defaults + environment"
    data = {
        "config_file": str(settings.config_path) if settings.config_path else None,
        "source": source,
        "errors": report["errors"],
        "warnings": report["warnings"],
    }
    if report["errors"]:
        return CheckResult(
            "config",
            CHECK_FAIL,
            True,
            "; ".join(report["errors"]),
            remediation="Fix the listed keys with `agletras config show` and `agletras config validate`.",
            data=data,
        )
    warnings = list(report["warnings"])
    if settings.allow_private_hosts:
        warnings.append(
            "fetch.allow_private_hosts=true: the SSRF guard allows private/loopback hosts"
        )
    detail = f"configuration valid (source: {source})"
    if warnings:
        return CheckResult(
            "config",
            CHECK_WARN,
            True,
            detail + "; warnings: " + "; ".join(warnings),
            remediation="Review the warnings above; they do not block research.",
            data=data,
        )
    return CheckResult("config", CHECK_OK, True, detail, data=data)


def _check_paths(settings: Settings) -> CheckResult:
    entries: dict[str, str] = {}
    problems: list[str] = []
    for label, path in (
        ("data_dir", settings.data_dir),
        ("reports_dir", settings.reports_dir),
        ("cache_dir", settings.cache_dir),
        ("logs_dir", settings.logs_dir),
        ("db_path (parent)", settings.db_path.parent),
    ):
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if path.exists() and not path.is_dir():
            problems.append(f"{label}={path} exists and is not a directory")
            entries[label] = "not a directory"
        elif not os.access(probe, os.W_OK):
            problems.append(f"{label}={path} is not writable (nearest existing parent: {probe})")
            entries[label] = "not writable"
        elif path.exists():
            entries[label] = "writable"
        else:
            entries[label] = f"missing; {probe} is writable so it will be created"
    if problems:
        return CheckResult(
            "paths",
            CHECK_FAIL,
            True,
            "; ".join(problems),
            remediation=(
                "Create the directories or point paths.* at a writable location "
                "(config file or AGLETRAS_DATA_DIR / AGLETRAS_REPORTS_DIR / "
                "AGLETRAS_CACHE_DIR / AGLETRAS_LOGS_DIR)."
            ),
            data=entries,
        )
    return CheckResult("paths", CHECK_OK, True, "all storage directories are writable", data=entries)


def _check_database(settings: Settings) -> CheckResult:
    try:
        with storage.open_db(settings) as conn:
            version = int(
                conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()["v"] or 0
            )
            quick = conn.execute("PRAGMA quick_check").fetchone()[0]
    except (sqlite3.Error, OSError) as exc:
        return CheckResult(
            "database",
            CHECK_FAIL,
            True,
            f"cannot open {settings.db_path}: {exc.__class__.__name__}: {exc}",
            remediation=(
                "Check that paths.data_dir is a writable directory and that the file is a "
                "readable/writable SQLite database, or move it aside (it is recreated on "
                "the next run). Existing research history lives there, so back it up before "
                "deleting anything."
            ),
        )
    fts_enabled = False
    probe = sqlite3.connect(":memory:")
    try:
        fts_enabled = bool(
            probe.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()[0]
        )
    finally:
        probe.close()
    data = {
        "db_path": str(settings.db_path),
        "schema_version": version,
        "expected_schema_version": storage.SCHEMA_VERSION,
        "sqlite_version": sqlite3.sqlite_version,
        "fts5": fts_enabled,
    }
    if quick != "ok":
        return CheckResult(
            "database",
            CHECK_FAIL,
            True,
            f"PRAGMA quick_check reported: {quick}",
            remediation="Back up and restore the database, or start with a fresh file.",
            data=data,
        )
    if version > storage.SCHEMA_VERSION:
        return CheckResult(
            "database",
            CHECK_FAIL,
            True,
            (
                f"{settings.db_path} uses schema v{version}, newer than this build "
                f"(v{storage.SCHEMA_VERSION}); refusing to downgrade"
            ),
            remediation="Upgrade agletras-mcp to the version that wrote this database.",
            data=data,
        )
    return CheckResult(
        "database",
        CHECK_OK,
        True,
        f"SQLite {sqlite3.sqlite_version}; schema v{version} (current); quick_check ok",
        data=data,
    )


def _check_search_adapter(settings: Settings) -> CheckResult:
    from . import search

    provider = settings.search_provider
    if provider not in search.PROVIDERS:
        return CheckResult(
            "search_adapter",
            CHECK_FAIL,
            True,
            f"unknown search provider {provider!r}; available: {', '.join(sorted(search.PROVIDERS))}",
            remediation="Set search.provider (or AGLETRAS_SEARCH_PROVIDER) to a listed provider.",
        )
    detail = f"{provider!r} adapter available (endpoint: {_safe_endpoint(settings.search_endpoint) if settings.search_endpoint else 'provider default'})"
    return CheckResult("search_adapter", CHECK_OK, True, detail, data={"providers": sorted(search.PROVIDERS)})


def _check_images(settings: Settings) -> CheckResult:
    from .config import IMAGE_SEARCH_PROVIDERS

    provider = settings.image_search_provider
    if provider not in IMAGE_SEARCH_PROVIDERS:
        return CheckResult(
            "images",
            CHECK_FAIL,
            False,
            f"unknown image search provider {provider!r}; available: {', '.join(IMAGE_SEARCH_PROVIDERS)}",
            remediation=f"Set images.search_provider to one of: {', '.join(IMAGE_SEARCH_PROVIDERS)}.",
        )
    vision_configured = bool(settings.vision_model_endpoint.strip())
    if not vision_configured:
        return CheckResult(
            "images",
            CHECK_WARN if settings.research_include_images else CHECK_SKIPPED,
            False,
            (
                "image search is available, but no vision model endpoint is configured, "
                "so analyze_image will report vision_unavailable and never pretend the "
                "image was inspected"
                + (" (research.include_images=true)" if settings.research_include_images else "")
            ),
            remediation=(
                "Optional: set images.vision_model_endpoint (a local/OpenAI-compatible "
                "endpoint) to enable visual analysis. Image discovery and metadata work "
                "without it."
            ),
        )
    return CheckResult(
        "images",
        CHECK_OK,
        False,
        f"image search provider {provider!r}; vision endpoint {_safe_endpoint(settings.vision_model_endpoint)}",
    )


def _check_model_endpoints(settings: Settings) -> CheckResult:
    endpoints = {
        "verification.model_endpoint": settings.verification_model_endpoint.strip(),
        "images.vision_model_endpoint": settings.vision_model_endpoint.strip(),
    }
    configured = {name: value for name, value in endpoints.items() if value}
    if not configured:
        return CheckResult(
            "model_endpoints",
            CHECK_SKIPPED,
            False,
            "no model endpoints configured; the local pipeline is 100% model-free",
            data={"configured": []},
        )
    results: dict[str, Any] = {}
    failures: list[str] = []
    for name, endpoint in configured.items():
        probe = _tcp_probe(endpoint)
        results[name] = {"endpoint": _safe_endpoint(endpoint), **probe}
        if not probe["reachable"]:
            failures.append(f"{name} at {_safe_endpoint(endpoint)} is unreachable ({probe.get('error')})")
    if failures:
        return CheckResult(
            "model_endpoints",
            CHECK_FAIL,
            False,
            "; ".join(failures),
            remediation=(
                "Start the configured local model server, or clear the endpoint setting "
                "to stay model-free. CLI research works without any model."
            ),
            data=results,
        )
    return CheckResult(
        "model_endpoints",
        CHECK_OK,
        False,
        f"{len(configured)} configured endpoint(s) reachable",
        data=results,
    )


async def _mcp_probe(env: dict[str, str], timeout: float) -> dict[str, Any]:
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agletras_mcp"],
        cwd=str(PROJECT_ROOT),
        env=env,
    )

    async def _run() -> dict[str, Any]:
        async with Client(params) as client:
            catalog = await client.list_tools()
            names = sorted(tool.name for tool in catalog.tools)
            health_result = await client.call_tool("health_check", {})
            payload: dict[str, Any] = {}
            for block in health_result.content:
                if getattr(block, "type", "") == "text":
                    try:
                        payload = json.loads(block.text)
                    except json.JSONDecodeError:
                        payload = {}
                    break
            return {"tools": names, "health": payload}

    async with asyncio.timeout(timeout):
        return await _run()


def _check_mcp_server(settings: Settings, *, timeout: float) -> CheckResult:
    from .server import TOOL_NAMES

    env = dict(os.environ)
    if settings.config_path is not None:
        env["AGLETRAS_CONFIG"] = str(settings.config_path)
    started = time.monotonic()
    try:
        probe = asyncio.run(_mcp_probe(env, timeout))
    except (TimeoutError, asyncio.TimeoutError):
        return CheckResult(
            "mcp_server",
            CHECK_FAIL,
            True,
            f"the MCP server did not answer within {timeout:.0f}s",
            remediation=(
                "Run `python -m agletras_mcp` manually to see stderr; a stuck database "
                "or an unreadable config file are the usual causes."
            ),
        )
    except Exception as exc:  # noqa: BLE001 - any transport failure is the result
        return CheckResult(
            "mcp_server",
            CHECK_FAIL,
            True,
            f"could not start/initialize the MCP server: {exc.__class__.__name__}: {exc}",
            remediation=(
                "Run `python -m agletras_mcp` manually and check stderr; verify the "
                "virtualenv is active and `pip install -e .` succeeded."
            ),
        )
    elapsed = int((time.monotonic() - started) * 1000)
    discovered = set(probe["tools"])
    missing = sorted(set(TOOL_NAMES) - discovered)
    health = probe.get("health") or {}
    if missing:
        return CheckResult(
            "mcp_server",
            CHECK_FAIL,
            True,
            f"server started but does not expose {len(missing)} documented tool(s): {', '.join(missing)}",
            remediation="Reinstall the package (`pip install -e .`) and restart OpenCode.",
            duration_ms=elapsed,
            data={"tool_count": len(discovered)},
        )
    # health_check reports ok=true even with a degraded store; the honest
    # signal is the explicit status ("ok" vs "degraded").
    health_status = health.get("status")
    if not health.get("ok") or (health_status is not None and health_status != "ok"):
        return CheckResult(
            "mcp_server",
            CHECK_FAIL,
            True,
            f"stdio startup works, but health_check reported: {health_status or 'no payload'}",
            remediation="Check the database/result above for the underlying cause.",
            duration_ms=elapsed,
            data={"tool_count": len(discovered)},
        )
    return CheckResult(
        "mcp_server",
        CHECK_OK,
        True,
        f"stdio startup + tool discovery OK ({len(discovered)} tools); health_check ok",
        duration_ms=elapsed,
        data={"tool_count": len(discovered), "transport": "stdio"},
    )


def _opencode_mcp_entry(data: Any) -> dict[str, Any] | None:
    """The agletras entry in OpenCode's config (``mcp.servers`` or flat ``mcp``)."""
    if not isinstance(data, dict):
        return None
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        return None
    servers = mcp.get("servers")
    if isinstance(servers, dict):
        entry = servers.get("agletras")
    else:
        entry = mcp.get("agletras")
    return entry if isinstance(entry, dict) else None


def _check_opencode(settings: Settings) -> CheckResult:
    """Inspect OpenCode wiring read-only; never edit anyone's config."""
    candidates = [PROJECT_ROOT / "opencode.json"]
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    user_config = (
        Path(xdg) / "opencode" / "opencode.json"
        if xdg
        else Path.home() / ".config" / "opencode" / "opencode.json"
    )
    if user_config != candidates[0]:
        candidates.append(user_config)

    found: dict[str, Any] = {}
    registered = False
    problems: list[str] = []
    for path in candidates:
        if not path.is_file():
            found[str(path)] = "missing"
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            found[str(path)] = f"unreadable ({exc})"
            problems.append(f"{path}: {exc}")
            continue
        entry = _opencode_mcp_entry(data)
        if entry is None:
            found[str(path)] = "present, no agletras MCP entry"
            continue
        command = entry.get("command")
        if isinstance(command, list) and command:
            binary = str(command[0])
            resolved = Path(binary).expanduser() if binary.startswith(("/", "~", ".")) else None
            if resolved is not None and not resolved.exists():
                problems.append(f"{path}: command binary {binary} does not exist")
                found[str(path)] = "agletras entry with a missing binary"
            else:
                registered = True
                found[str(path)] = "agletras stdio entry present"
        else:
            found[str(path)] = "agletras entry has no command list"
            problems.append(f"{path}: agletras entry has no command list")
    if problems:
        return CheckResult(
            "opencode",
            CHECK_WARN,
            False,
            "; ".join(problems),
            remediation="Fix the agletras entry using the example in README (OpenCode MCP section).",
            data=found,
        )
    if registered:
        return CheckResult(
            "opencode",
            CHECK_OK,
            False,
            "OpenCode config registers the agletras stdio server",
            data=found,
        )
    return CheckResult(
        "opencode",
        CHECK_WARN,
        False,
        "no OpenCode config with an agletras MCP entry found (CLI use is unaffected)",
        remediation=(
            "To use the server from OpenCode, copy the example from the README "
            "into opencode.json (project) or ~/.config/opencode/opencode.json (user)."
        ),
        data=found,
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_doctor(
    settings: Settings | None = None,
    *,
    config_error: str | None = None,
    include_mcp: bool = True,
    mcp_timeout: float = 30.0,
    on_check: Any | None = None,
) -> dict[str, Any]:
    """Run every check and return a structured report (never raises).

    ``on_check(result)`` (if given) is called after each check so the CLI can
    stream progress to stderr while the report itself stays on stdout.
    """
    checks: list[CheckResult] = []

    def add(result: CheckResult) -> None:
        checks.append(result)
        if callable(on_check):
            try:
                on_check(result)
            except Exception:  # noqa: BLE001 - progress reporting must not break checks
                pass

    add(_check_runtime())
    add(_check_dependencies())

    if settings is None and config_error is None:
        try:
            settings = load_settings()
        except ConfigError as exc:
            config_error = str(exc)
    if settings is not None:
        add(_check_config(settings, config_error))
        add(_check_paths(settings))
        add(_check_database(settings))
        add(_check_search_adapter(settings))
        add(_check_images(settings))
        add(_check_model_endpoints(settings))
        if include_mcp:
            add(_check_mcp_server(settings, timeout=mcp_timeout))
        add(_check_opencode(settings))
    else:
        add(_check_config(settings, config_error))

    required_failures = [c.name for c in checks if c.required and c.status == CHECK_FAIL]
    optional_failures = [c.name for c in checks if not c.required and c.status == CHECK_FAIL]
    warnings = [c.name for c in checks if c.status == CHECK_WARN]

    config_check = next((c for c in checks if c.name == "config"), None)
    if config_error or (config_check is not None and config_check.status == CHECK_FAIL):
        exit_code = EXIT_CONFIG
    elif required_failures:
        exit_code = EXIT_DEPENDENCY
    else:
        exit_code = EXIT_OK

    return {
        "ok": exit_code == EXIT_OK,
        "status": "ok" if exit_code == EXIT_OK else ("invalid_config" if exit_code == EXIT_CONFIG else "degraded"),
        "exit_code": exit_code,
        "version": __version__,
        "checks": [check.as_dict() for check in checks],
        "required_failures": required_failures,
        "optional_failures": optional_failures,
        "warnings": warnings,
        "settings": settings_snapshot(settings) if settings is not None else None,
    }


def render_human(report: dict[str, Any]) -> str:
    """A compact human-readable rendering of a doctor report."""
    icons = {CHECK_OK: "OK", CHECK_WARN: "WARN", CHECK_FAIL: "FAIL", CHECK_SKIPPED: "SKIP"}
    lines = [f"agletras doctor - version {report['version']}"]
    for check in report["checks"]:
        marker = icons.get(check["status"], check["status"].upper())
        scope = "required" if check["required"] else "optional"
        lines.append(f"[{marker:4}] {check['name']:16} ({scope}) {check['detail']}")
        if check["remediation"] and check["status"] in (CHECK_FAIL, CHECK_WARN):
            lines.append(f"       fix: {check['remediation']}")
    if report["required_failures"]:
        lines.append(
            "result: DEGRADED - required checks failed: " + ", ".join(report["required_failures"])
        )
    elif report["optional_failures"] or report["warnings"]:
        lines.append("result: OK with warnings - see the entries above")
    else:
        lines.append("result: OK - everything required is available")
    return "\n".join(lines)

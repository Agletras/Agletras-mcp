"""The ``agletras`` command-line interface.

Runs the same research pipeline the MCP tools use, but from a terminal and
without a model in the loop: it plans subquestions, searches with the free
configured provider, saves evidence, extracts and verifies claims, and writes
the same kind of validated Markdown report the agent workflow produces.

Contract:
- stdout carries only the final result (report, JSON payload, human summary);
- stderr carries progress (human or JSONL) and errors;
- exit codes: 0 success, 2 usage/config, 3 dependency, 4 research failed,
  5 partial (report produced, gaps remain), 130 interrupted.

Neither the CLI nor the report generation ever claims success when the report
or its persistence failed: the pipeline validates every report and the exit
code reflects what actually happened.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import (
    __version__,
    doctor as doctor_mod,
    evidence,
    jobs,
    monitor,
    runs,
    storage,
    synthesis,
    visualize,
)
from .config import (
    ConfigError,
    Settings,
    load_branding,
    load_profiles,
    load_settings,
    settings_snapshot,
    validate_settings,
)
from .errors import ToolError
from .pipeline import (
    EXIT_DEPENDENCY,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_RESEARCH_FAILED,
    EXIT_USAGE,
    PipelineInterrupted,
    RunOptions,
    create_run,
    execute_research,
    resume_research,
)
from .progress import ProgressReporter

PROG = "agletras"

DESCRIPTION = (
    "Zero-cost research from the command line: search, collect evidence, "
    "extract and verify claims, and write a validated Markdown report."
)

EPILOG = """\
exit codes:
  0    success (command completed, report generated)
  2    usage or configuration error
  3    missing dependency or unhealthy environment (doctor)
  4    research failed (no usable report)
  5    partial: report generated but gaps remain
  130  interrupted (Ctrl+C / SIGTERM); resume with `agletras resume RUN_ID`

examples:
  agletras run "How does MCP standardize tool discovery?" --depth quick
  agletras status 4
  agletras resume 4 --force
  agletras report 4
  agletras export 4
  agletras jobs add weekly-mcp "What changed in MCP this week?" --profile quick
  agletras jobs run weekly-mcp
  agletras monitor add https://example.com/pricing --project acme
  agletras monitor check --project acme
  agletras monitor report
  agletras config show
  agletras doctor
"""

_RESEARCH_ATTRS = (
    "depth",
    "max_sources",
    "max_claims",
    "searches_per_subquestion",
    "followup_rounds",
    "results_per_search",
    "verification",
    "images",
    "no_images",
    "image_results",
    "subquestions",
    "title",
    # Phase 8: execution mode, memory reuse, project scope, profiles
    "parallel",
    "sequential",
    "reuse_evidence",
    "no_reuse",
    "project",
    "profile",
)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _add_global_flags(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    default: Any = argparse.SUPPRESS if suppress else None

    def add(*names: str, **kwargs: Any) -> None:
        parser.add_argument(*names, default=default, **kwargs)

    add(
        "--config",
        metavar="PATH",
        help="TOML configuration file (default: $AGLETRAS_CONFIG or $AGLETRAS_HOME/agletras.toml)",
    )
    add("--json", action="store_true", help="print the final result as JSON on stdout")
    add("-q", "--quiet", action="store_true", help="suppress progress on stderr (errors only)")
    add("-v", "--verbose", action="store_true", help="include debug progress on stderr")
    add(
        "--log-format",
        choices=("human", "json"),
        help="progress format on stderr (default: logging.format, 'human')",
    )
    add("--log-file", metavar="PATH", help="append structured JSONL progress to this file")
    add(
        "--non-interactive",
        action="store_true",
        help="accepted for scripting; the CLI never prompts either way",
    )


def _add_research_options(parser: argparse.ArgumentParser) -> None:
    def add(*names: str, **kwargs: Any) -> None:
        parser.add_argument(*names, default=argparse.SUPPRESS, **kwargs)

    add("--depth", choices=("quick", "standard", "thorough"), help="research budget preset")
    add("--max-sources", type=int, metavar="N", help="hard cap on saved sources (0 = preset)")
    add("--max-claims", type=int, metavar="N", help="hard cap on registered claims (0 = preset)")
    add(
        "--searches-per-subquestion",
        type=int,
        metavar="N",
        help="search calls per subquestion per round (0 = preset)",
    )
    add(
        "--followup-rounds",
        type=int,
        metavar="N",
        help="follow-up rounds (-1 = preset, 0 = none, N = at most N)",
    )
    add("--results-per-search", type=int, metavar="N", help="search results fetched per call (0 = preset)")
    add("--verification", metavar="METHOD", help="claim verification method (auto, deterministic, ...)")
    add("--images", action="store_true", help="also collect image metadata for the question")
    add("--no-images", action="store_true", help="disable image collection (overrides config)")
    add("--image-results", type=int, metavar="N", help="images fetched when images are enabled")
    add(
        "--subquestions",
        action="append",
        metavar="TEXT",
        help="explicit subquestions, ';'-separated; repeatable (default: the whole question)",
    )
    add("--title", metavar="TEXT", help="report title (default: derived from the question)")
    # Phase 8: execution mode, evidence reuse, and project scoping.
    add(
        "--parallel",
        action="store_true",
        help="run specialists (searches, collections, checks) in a bounded thread pool",
    )
    add("--sequential", action="store_true", help="force sequential execution (the default)")
    add(
        "--reuse-evidence",
        action="store_true",
        help="link fresh stored copies from project memory instead of re-fetching them",
    )
    add("--no-reuse", action="store_true", help="disable evidence reuse (overrides config)")
    add(
        "--project",
        metavar="NAME",
        help="project label stored on the run; scopes memory reuse and recall",
    )
    add(
        "--profile",
        metavar="NAME",
        help="config-file [profiles.<name>] preset for this run (explicit flags win)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_global_flags(parser, suppress=False)
    parser.add_argument(
        "--version", action="version", version=f"agletras-mcp {__version__} (Python {sys.version.split()[0]})"
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    common = argparse.ArgumentParser(add_help=False)
    _add_global_flags(common, suppress=True)

    p_run = sub.add_parser(
        "run",
        parents=[common],
        help="run a research question end to end",
        description="Plan, search, collect evidence, extract/verify claims, and report.",
    )
    p_run.add_argument("question", help="the research question (quote it)")
    _add_research_options(p_run)

    p_resume = sub.add_parser(
        "resume",
        parents=[common],
        help="continue an interrupted or failed CLI run",
        description=(
            "Continue a run created by `agletras run` from its last checkpoint. "
            "Completed stages are skipped; evidence already stored is reused. "
            "Runs created by the MCP agent workflow cannot be resumed here."
        ),
    )
    p_resume.add_argument("run_id", type=int, help="run id from `agletras run` or `agletras list`")
    p_resume.add_argument(
        "--force",
        action="store_true",
        help="resume even when the run is failed, stale, or was configured differently",
    )
    p_resume.add_argument(
        "--restart",
        action="store_true",
        help="explicitly re-run every stage from the start (keeps cached evidence; no blind reruns otherwise)",
    )
    _add_research_options(p_resume)

    p_status = sub.add_parser(
        "status",
        parents=[common],
        help="show one run's state, stages, and resumability",
        description="Read-only view: status, stage checkpoints, reports, and whether resume would work.",
    )
    p_status.add_argument("run_id", type=int, help="run id")
    p_list = sub.add_parser(
        "list",
        parents=[common],
        help="list recent runs (newest first)",
    )
    p_list.add_argument("--limit", type=int, metavar="N", help="maximum rows (default: storage.default_list_limit)")
    p_list.add_argument("--status", metavar="STATUS", help="filter by run status")

    p_report = sub.add_parser(
        "report",
        parents=[common],
        help="print a run's stored Markdown report",
        description=(
            "Print the run's most recent stored report from the database; the JSON form "
            "(with --json) adds path, validation status, and file-integrity metadata."
        ),
    )
    p_report.add_argument("run_id", type=int, help="run id")

    p_profiles = sub.add_parser(
        "profiles",
        parents=[common],
        help="list config-file research profiles",
        description=(
            "Named research presets from [profiles.<name>] tables in the config file. "
            "Use one with `agletras run ... --profile NAME`; explicit flags always win."
        ),
    )
    p_profiles.add_argument("name", nargs="?", help="show one profile's full definition")

    p_export = sub.add_parser(
        "export",
        parents=[common],
        help="export a run's evidence map, timeline, and CSV tables",
        description=(
            "Write derived artifacts for one run: a Mermaid evidence map and source "
            "timeline plus CSV tables for sources, claims, and citations. "
            "Everything is rendered from stored evidence; nothing is invented."
        ),
    )
    p_export.add_argument("run_id", type=int, help="run id")
    p_export.add_argument(
        "--format",
        action="append",
        choices=visualize.FORMATS,
        help="artifact family: all, mermaid, csv (repeatable; default: all)",
    )
    p_export.add_argument(
        "--out",
        metavar="DIR",
        help="output directory (default: reports/visuals/run-RUN_ID)",
    )

    p_jobs = sub.add_parser(
        "jobs",
        parents=[common],
        help="define and re-run named research requests",
        description=(
            "Named, repeatable research requests. A job stores a question plus an "
            "optional config-file profile, project label, and title; `jobs run` "
            "executes it manually with the current profile and branding."
        ),
    )
    jobs_sub = p_jobs.add_subparsers(dest="jobs_command", metavar="ACTION")
    jobs_sub.add_parser("list", parents=[common], help="list jobs (default action)")
    p_jobs_add = jobs_sub.add_parser("add", parents=[common], help="define a job")
    p_jobs_add.add_argument("name", help="job name (unique, case-insensitive)")
    p_jobs_add.add_argument("question", help="the research question to repeat")
    p_jobs_add.add_argument("--profile", metavar="NAME", help="config-file profile to use")
    p_jobs_add.add_argument("--project", metavar="NAME", help="project label for the runs")
    p_jobs_add.add_argument("--title", metavar="TEXT", help="report title override")
    p_jobs_add.add_argument("--notes", metavar="TEXT", help="free-form note")
    p_jobs_show = jobs_sub.add_parser("show", parents=[common], help="show one job and its runs")
    p_jobs_show.add_argument("name", help="job name or id")
    p_jobs_run = jobs_sub.add_parser("run", parents=[common], help="run a job now")
    p_jobs_run.add_argument("name", help="job name or id")
    p_jobs_remove = jobs_sub.add_parser(
        "remove", parents=[common], help="delete a job (its research runs stay)"
    )
    p_jobs_remove.add_argument("name", help="job name or id")

    p_monitor = sub.add_parser(
        "monitor",
        parents=[common],
        help="watch URLs/local files for meaningful changes",
        description=(
            "Register monitoring targets and check them manually for meaningful content "
            "changes. Checks use conditional requests when the server supports them, ignore "
            "navigation/ads/timestamp-only churn, and never treat a failure as a deletion. "
            "Targets are never checked automatically: run `monitor check` yourself."
        ),
    )
    monitor_sub = p_monitor.add_subparsers(dest="monitor_command", metavar="ACTION")
    p_monitor_list = monitor_sub.add_parser(
        "list", parents=[common], help="list targets and their latest check (default action)"
    )
    p_monitor_list.add_argument("--project", metavar="NAME", help="only targets with this project label")
    p_monitor_add = monitor_sub.add_parser(
        "add", parents=[common], help="register or update a monitoring target"
    )
    p_monitor_add.add_argument("target", help="http(s) URL or a permitted local file")
    p_monitor_add.add_argument("--name", metavar="TEXT", help="human label (default: host or file name)")
    p_monitor_add.add_argument("--project", metavar="NAME", help="project label scoping change queries")
    p_monitor_add.add_argument("--kind", choices=monitor.TARGET_KINDS, help="force url/path when ambiguous")
    p_monitor_remove = monitor_sub.add_parser(
        "remove", parents=[common], help="delete a target and its snapshots"
    )
    p_monitor_remove.add_argument("target_id", type=int, help="target id from `agletras monitor`")
    p_monitor_check = monitor_sub.add_parser(
        "check", parents=[common], help="check targets for changes now (manual action)"
    )
    p_monitor_check.add_argument(
        "target_id", nargs="*", type=int, help="target ids (default: every enabled target)"
    )
    p_monitor_check.add_argument("--project", metavar="NAME", help="only enabled targets with this project label")
    p_monitor_check.add_argument(
        "--sequential", action="store_true", help="run checks one by one (default: bounded pool)"
    )
    p_monitor_changes = monitor_sub.add_parser(
        "changes", parents=[common], help="list detected meaningful changes"
    )
    p_monitor_changes.add_argument("--target", type=int, metavar="ID", help="restrict to one target")
    p_monitor_changes.add_argument("--project", metavar="NAME", help="restrict to a project label")
    p_monitor_changes.add_argument(
        "--all", action="store_true", help="also show boilerplate-only churn (not treated as changes)"
    )
    p_monitor_changes.add_argument("--since-days", type=int, metavar="N", help="only changes detected in the last N days")
    p_monitor_changes.add_argument("--limit", type=int, metavar="N", help="maximum changes (default 50)")
    p_monitor_report = monitor_sub.add_parser(
        "report", parents=[common], help="write a Markdown monitoring report"
    )
    p_monitor_report.add_argument(
        "target_id", nargs="*", type=int, help="target ids (default: every target)"
    )
    p_monitor_report.add_argument("--project", metavar="NAME", help="only targets with this project label")

    p_config = sub.add_parser(
        "config",
        parents=[common],
        help="inspect and validate the effective configuration",
        description="Show the effective settings (secrets masked) or validate them.",
    )
    config_sub = p_config.add_subparsers(dest="config_command", metavar="ACTION")
    config_sub.add_parser("show", parents=[common], help="print the effective settings (secrets masked)")
    config_sub.add_parser("validate", parents=[common], help="validate the effective settings")

    p_doctor = sub.add_parser(
        "doctor",
        parents=[common],
        help="check runtime, packages, config, storage, adapters, MCP startup",
        description=(
            "Bounded diagnostic: Python/runtime, required packages, config validity, "
            "writable directories, SQLite schema, search adapters, optional vision/"
            "model endpoints (only when configured), MCP stdio startup + tool "
            "discovery, and OpenCode wiring. Never prints secrets."
        ),
    )
    p_doctor.add_argument("--skip-mcp", action="store_true", help="skip the MCP stdio startup check")
    p_doctor.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="MCP startup timeout (default: 30)",
    )

    sub.add_parser(
        "version",
        parents=[common],
        help="print the version and environment",
        description="Version, Python, and database schema of this build.",
    )

    sub.add_parser(
        "serve",
        parents=[common],
        help="run the MCP server over stdio (same as `python -m agletras_mcp`)",
        description=(
            "Run the MCP stdio server exactly as OpenCode launches it. Useful to "
            "verify the launch command documented in the README."
        ),
    )
    return parser


def _opt(args: argparse.Namespace, name: str, default: Any = None) -> Any:
    return getattr(args, name, default)


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _error(message: str) -> None:
    print(f"{PROG}: error: {message}", file=sys.stderr)


def _make_reporter(
    args: argparse.Namespace,
    settings: Settings,
    *,
    run_id: int = 0,
    log_path: str | Path | None = None,
) -> ProgressReporter:
    if _opt(args, "quiet", False):
        verbosity = "quiet"
    elif _opt(args, "verbose", False):
        verbosity = "verbose"
    else:
        verbosity = "normal"
    log_format = _opt(args, "log_format") or settings.log_format
    return ProgressReporter(
        run_id=run_id,
        verbosity=verbosity,
        log_format=log_format,
        stream=sys.stderr,
        log_file_path=log_path,
    )


def _install_sigterm_handler() -> None:
    """Turn SIGTERM into the same interruption path as Ctrl+C."""

    def _handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        raise PipelineInterrupted("interrupted by SIGTERM")

    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _handler)
        except (ValueError, OSError):  # not the main thread / unsupported
            pass


def _load_settings_or_report(args: argparse.Namespace) -> tuple[Settings | None, int]:
    try:
        settings = load_settings(config_path=_opt(args, "config"))
    except ConfigError as exc:
        _error(f"configuration error: {exc}")
        return None, EXIT_USAGE
    return settings, EXIT_OK


def _validate_or_report(settings: Settings) -> int:
    report = validate_settings(settings)
    if report["errors"]:
        _error("invalid configuration:")
        for item in report["errors"]:
            print(f"  - {item}", file=sys.stderr)
        print(
            f"run `{PROG} config show` to inspect the effective settings, "
            f"or `{PROG} config validate` for the full report",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return EXIT_OK


def _log_path_for(args: argparse.Namespace, settings: Settings, run_id: int) -> Path:
    explicit = _opt(args, "log_file") or settings.log_file or ""
    if explicit:
        return Path(str(explicit)).expanduser().resolve()
    return (settings.logs_dir / f"run-{run_id}.jsonl").resolve()


def _print_payload(payload: dict[str, Any], args: argparse.Namespace, human: str) -> None:
    if _opt(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(human)


# ---------------------------------------------------------------------------
# Commands: research
# ---------------------------------------------------------------------------


def _split_subquestions(raw: Sequence[str] | None) -> tuple[str, ...]:
    parts: list[str] = []
    for chunk in raw or ():
        for piece in re.split(r"[;\n]", str(chunk)):
            piece = piece.strip()
            if piece:
                parts.append(piece)
    return tuple(parts)


def _build_options_from_args(
    args: argparse.Namespace,
    settings: Settings,
    *,
    question: str,
    profile: Mapping[str, Any] | None = None,
    branding: Mapping[str, str] | None = None,
) -> RunOptions:
    profile = profile or {}
    branding = branding or {}
    if getattr(args, "images", False) and getattr(args, "no_images", False):
        raise ToolError("invalid_argument", "--images and --no-images are mutually exclusive")
    if getattr(args, "images", False):
        include_images: bool | None = True
    elif getattr(args, "no_images", False):
        include_images = False
    else:
        include_images = None
    if getattr(args, "parallel", False) and getattr(args, "sequential", False):
        raise ToolError("invalid_argument", "--parallel and --sequential are mutually exclusive")
    if getattr(args, "parallel", False):
        parallel: bool | None = True
    elif getattr(args, "sequential", False):
        parallel = False
    else:
        parallel = None
    if getattr(args, "reuse_evidence", False) and getattr(args, "no_reuse", False):
        raise ToolError("invalid_argument", "--reuse-evidence and --no-reuse are mutually exclusive")
    if getattr(args, "reuse_evidence", False):
        reuse: bool | None = True
    elif getattr(args, "no_reuse", False):
        reuse = False
    else:
        reuse = None

    def pick(arg_name: str, profile_key: str, fallback: Any) -> Any:
        """Explicit CLI flag > profile value > settings/env/config fallback."""
        raw = getattr(args, arg_name, None)
        if raw is not None:
            return raw
        if profile_key in profile:
            return profile[profile_key]
        return fallback

    title = str(pick("title", "title", ""))
    if not title and branding.get("name"):
        title = f"{branding['name']}: {question}"
    return RunOptions(
        question=question,
        depth=str(pick("depth", "depth", settings.research_depth)),
        max_sources=int(pick("max_sources", "max_sources", settings.research_max_sources)),
        max_claims=int(pick("max_claims", "max_claims", settings.research_max_claims)),
        searches_per_subquestion=int(
            pick(
                "searches_per_subquestion",
                "searches_per_subquestion",
                settings.research_searches_per_subquestion,
            )
        ),
        followup_rounds=int(
            pick("followup_rounds", "followup_rounds", settings.research_followup_rounds)
        ),
        results_per_search=int(
            pick("results_per_search", "results_per_search", settings.research_results_per_search)
        ),
        verification_method=str(
            pick("verification", "verification_method", settings.research_verification_method)
        ),
        include_images=(
            bool(profile.get("include_images"))
            if include_images is None and "include_images" in profile
            else (settings.research_include_images if include_images is None else include_images)
        ),
        image_results=int(pick("image_results", "image_results", settings.research_image_results)),
        subquestions=_split_subquestions(getattr(args, "subquestions", None)),
        title=title,
        parallel=bool(profile.get("parallel", False)) if parallel is None else parallel,
        reuse_evidence=bool(profile.get("reuse_evidence", False)) if reuse is None else reuse,
        project=str(pick("project", "project", "")),
    )


def _has_explicit_research_args(args: argparse.Namespace) -> bool:
    return any(getattr(args, name, None) is not None for name in _RESEARCH_ATTRS)


def _options_from_stored(
    state: dict[str, Any], settings: Settings
) -> RunOptions:
    """Rebuild the resolved options a run was created with (for compatible resume)."""
    stored = ((state.get("config") or {}).get("options")) if isinstance(state.get("config"), dict) else {}
    stored = stored if isinstance(stored, dict) else {}

    def pick(name: str, fallback: Any) -> Any:
        value = stored.get(name)
        return fallback if value is None else value

    return RunOptions(
        question=str(state.get("query") or ""),
        depth=str(pick("depth", settings.research_depth)),
        max_sources=int(pick("max_sources", settings.research_max_sources)),
        max_claims=int(pick("max_claims", settings.research_max_claims)),
        searches_per_subquestion=int(
            pick("searches_per_subquestion", settings.research_searches_per_subquestion)
        ),
        followup_rounds=int(pick("followup_rounds", settings.research_followup_rounds)),
        results_per_search=int(pick("results_per_search", settings.research_results_per_search)),
        verification_method=str(pick("verification_method", settings.research_verification_method)),
        include_images=bool(pick("include_images", settings.research_include_images)),
        image_results=settings.research_image_results,
        subquestions=tuple(str(item) for item in (pick("subquestions", []) or ())),
        title=str(pick("title", "")),
        parallel=bool(pick("parallel", False)),
        reuse_evidence=bool(pick("reuse_evidence", False)),
        project=str(pick("project", "")),
    )


def _human_run_result(result: dict[str, Any]) -> str:
    lines = [f"run {result.get('run_id')}: {result.get('status')}"]
    if result.get("query"):
        lines.append(f"question: {result['query']}")
    report = result.get("report") or {}
    if report.get("path"):
        validation = report.get("validation_status") or "unvalidated"
        lines.append(f"report: {report['path']} (validation: {validation})")
    counts = []
    if "sources_count" in result:
        counts.append(f"sources={result['sources_count']}")
    if "claims_count" in result:
        counts.append(f"claims={result['claims_count']}")
    if "subquestions" in result:
        counts.append(f"subquestions={result['subquestions']} (answered={result.get('answered', 0)})")
    if counts:
        lines.append(" ".join(counts))
    if result.get("duration_ms") is not None:
        lines.append(f"duration: {result['duration_ms'] / 1000:.1f}s")
    if result.get("log_file"):
        lines.append(f"log: {result['log_file']}")
    for warning in result.get("warnings") or ():
        lines.append(f"warning: {warning}")
    error = result.get("error")
    if error:
        if isinstance(error, dict):
            lines.append(f"error: {error.get('code')}: {error.get('message')}")
        else:
            lines.append(f"error: {error}")
    if result.get("stopping_reason"):
        lines.append(f"stopping reason: {result['stopping_reason']}")
    return "\n".join(lines)


def _resolve_profile(args: argparse.Namespace) -> dict[str, Any]:
    """The selected ``--profile`` definition, or ``{}`` when none was requested."""
    name = str(getattr(args, "profile", "") or "").strip()
    if not name:
        return {}
    profiles = load_profiles()
    if name not in profiles:
        available = ", ".join(sorted(profiles)) or "none defined"
        raise ToolError(
            "invalid_argument",
            f"unknown profile {name!r}; available profiles: {available}",
        )
    return profiles[name]


def _cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    question = str(getattr(args, "question", "") or "").strip()
    if not question:
        _error("the research question must not be empty")
        return EXIT_USAGE
    try:
        profile = _resolve_profile(args)
        options = _build_options_from_args(
            args, settings, question=question, profile=profile, branding=load_branding()
        )
        options.resolved(settings)  # validate depth/ceilings before any long task
    except ToolError as exc:
        _error(f"{exc.code}: {exc.message}")
        return EXIT_USAGE
    except ConfigError as exc:
        _error(f"configuration error: {exc}")
        return EXIT_USAGE

    code, _ = _execute_run_options(args, settings, options)
    return code


def _execute_run_options(
    args: argparse.Namespace, settings: Settings, options: RunOptions
) -> tuple[int, int]:
    """Create and execute a run for resolved options; return (exit_code, run_id).

    Shared by ``run`` and ``jobs run``: the same reporter, signal handling,
    resume hints, and result rendering apply to both.
    """
    reporter = _make_reporter(args, settings)
    _install_sigterm_handler()
    run_id = 0
    try:
        run_id = create_run(options, settings, reporter, tool_version=__version__)
        reporter.attach_log_file(_log_path_for(args, settings, run_id))
        result = execute_research(run_id, options, settings=settings, reporter=reporter)
    except KeyboardInterrupt:
        if run_id:
            print(
                f"{PROG}: error: interrupted; resume with `{PROG} resume {run_id}`",
                file=sys.stderr,
            )
        else:
            _error("interrupted before the run was created")
        return EXIT_INTERRUPTED, run_id
    except PipelineInterrupted as exc:
        if run_id:
            print(f"{PROG}: error: {exc}; resume with `{PROG} resume {run_id}`", file=sys.stderr)
        else:
            _error(str(exc))
        return EXIT_INTERRUPTED, run_id
    except ToolError as exc:
        _error(f"{exc.code}: {exc.message}")
        return EXIT_RESEARCH_FAILED, run_id
    finally:
        reporter.close()

    _print_payload(result, args, _human_run_result(result))
    return int(result.get("exit_code", EXIT_RESEARCH_FAILED)), run_id


def _cmd_resume(args: argparse.Namespace, settings: Settings) -> int:
    run_id = int(getattr(args, "run_id", 0) or 0)
    state = runs.get_run_state(run_id, settings=settings)
    if state is None:
        _error(f"no research run with id {run_id}; see `{PROG} list`")
        return EXIT_USAGE

    if _has_explicit_research_args(args):
        try:
            profile = _resolve_profile(args)
            options = _build_options_from_args(
                args,
                settings,
                question=str(state.get("query") or ""),
                profile=profile,
                branding=load_branding(),
            )
            options.resolved(settings)
        except ToolError as exc:
            _error(f"{exc.code}: {exc.message}")
            return EXIT_USAGE
        except ConfigError as exc:
            _error(f"configuration error: {exc}")
            return EXIT_USAGE
    else:
        options = _options_from_stored(state, settings)

    reporter = _make_reporter(args, settings, run_id=run_id)
    reporter.attach_log_file(_log_path_for(args, settings, run_id))
    _install_sigterm_handler()
    try:
        result = resume_research(
            run_id,
            options,
            settings=settings,
            reporter=reporter,
            force=bool(getattr(args, "force", False)),
            restart=bool(getattr(args, "restart", False)),
        )
    finally:
        reporter.close()

    _print_payload(result, args, _human_run_result(result))
    return int(result.get("exit_code", EXIT_RESEARCH_FAILED))


# ---------------------------------------------------------------------------
# Commands: inspection
# ---------------------------------------------------------------------------


def _human_status(payload: dict[str, Any]) -> str:
    run = payload["run"]
    lines = [
        f"run {run['run_id']}: {run['status']} (stage: {run.get('stage') or '-'})",
        f"question: {run.get('query')}",
        f"started: {run.get('started_at')}  updated: {run.get('updated_at') or '-'}",
    ]
    if run.get("completed_at"):
        lines.append(f"completed: {run['completed_at']}")
    if run.get("error"):
        lines.append(f"error: {run['error']}")
    counts = payload.get("counts") or {}
    if counts:
        lines.append(
            "evidence: sources={sources} claims={claims} (unverified={unverified}) "
            "verifications={verifications} reports={reports}".format(
                sources=counts.get("sources", 0),
                claims=counts.get("claims", 0),
                unverified=counts.get("unverified", 0),
                verifications=counts.get("verifications", 0),
                reports=counts.get("reports", 0),
            )
        )
    budget = payload.get("budget") or {}
    if budget:
        lines.append(
            "budget: follow-up rounds {}/{}; sources {}/{}; subquestions max {}".format(
                budget.get("followup_rounds_used", 0),
                budget.get("max_followup_rounds", 0),
                budget.get("sources_used", 0),
                budget.get("max_unique_sources", 0),
                budget.get("max_subquestions", 0),
            )
        )
    subquestions = payload.get("subquestions") or []
    for item in subquestions:
        lines.append(f"  [{item['status']}] {item['question']}")
    checkpoints = payload.get("checkpoints") or []
    if checkpoints:
        latest: dict[str, str] = {}
        for checkpoint in checkpoints:
            latest[checkpoint["stage"]] = f"{checkpoint['status']} (attempt {checkpoint['attempt']})"
        lines.append("stages: " + ", ".join(f"{stage}={state}" for stage, state in latest.items()))
    reports = payload.get("reports") or []
    for report in reports:
        lines.append(f"report {report['report_id']}: {report['path']} ({report['citation_validation_status']})")
    assessment = payload.get("resumable") or {}
    lines.append(f"resumable: {'yes' if assessment.get('resumable') else 'no'} - {assessment.get('detail', '')}")
    return "\n".join(lines)


def _cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    run_id = int(getattr(args, "run_id", 0) or 0)
    state = runs.get_run_state(run_id, settings=settings)
    if state is None:
        _error(f"no research run with id {run_id}; see `{PROG} list`")
        return EXIT_USAGE
    history = evidence.get_research_run_history(run_id=run_id, settings=settings)
    payload = {
        "run": state,
        "budget": history.get("budget"),
        "counts": history.get("counts"),
        "plan": history.get("plan"),
        "subquestions": history.get("subquestions", []),
        "checkpoints": runs.checkpoints(run_id, limit=50, settings=settings),
        "reports": runs.run_report_paths(run_id, settings=settings),
        "resumable": runs.resume_assessment(run_id, settings=settings),
    }
    _print_payload(payload, args, _human_status(payload))
    return EXIT_OK


def _human_list(payload: dict[str, Any]) -> str:
    rows = payload.get("runs") or []
    if not rows:
        return "no research runs recorded"
    lines = []
    for run in rows:
        query = (run.get("query") or "").replace("\n", " ")
        if len(query) > 58:
            query = query[:57] + "…"
        lines.append(
            "{id:>4}  {status:<11} {started:<20} sources={sources:<3} claims={claims:<3} "
            'reports={reports:<2} "{query}"'.format(
                id=run["run_id"],
                status=run["status"],
                started=(run.get("started_at") or "")[:19],
                sources=run.get("sources_count", 0),
                claims=run.get("claims_count", 0),
                reports=run.get("reports_count", 0),
                query=query,
            )
        )
    return "\n".join(lines)


def _cmd_list(args: argparse.Namespace, settings: Settings) -> int:
    limit = getattr(args, "limit", None) or settings.default_list_limit
    try:
        payload = runs.list_runs(limit=int(limit), status=getattr(args, "status", None), settings=settings)
    except ToolError as exc:
        _error(f"{exc.code}: {exc.message}")
        return EXIT_USAGE
    _print_payload(payload, args, _human_list(payload))
    return EXIT_OK


def _cmd_report(args: argparse.Namespace, settings: Settings) -> int:
    run_id = int(getattr(args, "run_id", 0) or 0)
    state = runs.get_run_state(run_id, settings=settings)
    if state is None:
        _error(f"no research run with id {run_id}; see `{PROG} list`")
        return EXIT_USAGE
    reports = runs.run_report_paths(run_id, settings=settings)
    if not reports:
        _error(
            f"run {run_id} has no stored report (status: {state['status']}); "
            f"nothing to print. Inspect it with `{PROG} status {run_id}`."
        )
        return EXIT_RESEARCH_FAILED
    report = reports[-1]
    payload = synthesis.get_markdown_report(report["report_id"], include_content=True, settings=settings)
    if not payload.get("file_exists") or not payload.get("file_hash_matches"):
        print(
            f"{PROG}: warning: the report file on disk is missing or its hash differs from the "
            f"stored copy; printing the validated stored content instead",
            file=sys.stderr,
        )
    content = payload.get("content") or ""
    if _opt(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(content if content.endswith("\n") else content + "\n")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Commands: config / profiles / doctor / misc
# ---------------------------------------------------------------------------


def _human_config(payload: dict[str, Any]) -> str:
    lines = [f"config file: {payload.get('config_file') or '(none - defaults + environment)'}"]
    settings = payload.get("settings") or {}
    for key in sorted(settings):
        lines.append(f"{key} = {settings[key]}")
    return "\n".join(lines)


def _cmd_profiles(args: argparse.Namespace, settings: Settings) -> int:
    try:
        profiles = load_profiles()
        branding = load_branding()
    except ConfigError as exc:
        _error(f"configuration error: {exc}")
        return EXIT_USAGE
    name = str(getattr(args, "name", "") or "").strip()
    if name:
        if name not in profiles:
            available = ", ".join(sorted(profiles)) or "none defined"
            _error(f"unknown profile {name!r}; available profiles: {available}")
            return EXIT_USAGE
        payload = {"profile": name, "values": profiles[name], "branding": branding}
        lines = [f"profile {name}:"]
        lines.extend(f"  {key} = {value}" for key, value in sorted(profiles[name].items()))
        if not profiles[name]:
            lines.append("  (no options: all defaults apply)")
        _print_payload(payload, args, "\n".join(lines))
        return EXIT_OK
    payload = {
        "count": len(profiles),
        "profiles": profiles,
        "branding": branding,
    }
    lines = [f"profiles: {len(profiles)}"]
    for profile_name in sorted(profiles):
        rendered = ", ".join(
            f"{key}={value}" for key, value in sorted(profiles[profile_name].items())
        ) or "(no options)"
        lines.append(f"  {profile_name}: {rendered}")
    if not profiles:
        lines.append("  (define [profiles.<name>] tables in the config file)")
    if branding.get("name"):
        lines.append(f"branding: {branding['name']}")
    _print_payload(payload, args, "\n".join(lines))
    return EXIT_OK


def _cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    run_id = int(getattr(args, "run_id", 0) or 0)
    formats = tuple(getattr(args, "format", None) or ("all",))
    out_dir = getattr(args, "out", None)
    try:
        payload = visualize.export_visuals(
            run_id, out_dir=out_dir, formats=formats, settings=settings
        )
    except ToolError as exc:
        _error(f"{exc.code}: {exc.message}")
        return EXIT_USAGE
    except (ValueError, OSError) as exc:
        _error(f"export failed: {exc}")
        return EXIT_USAGE
    counts = payload["counts"]
    lines = [
        f"run {run_id}: exported {len(payload['files'])} file(s) to {payload['directory']}",
        f"counts: sources={counts['sources']} claims={counts['claims']} citations={counts['citations']}",
    ]
    lines.extend(f"  {item['name']} ({item['bytes']} bytes)" for item in payload["files"])
    _print_payload(payload, args, "\n".join(lines))
    return EXIT_OK


def _jobs_run(args: argparse.Namespace, settings: Settings) -> int:
    """Execute a stored job with the current profile and branding."""
    identifier = getattr(args, "name", "")
    job = jobs.get_job(identifier, settings=settings)
    if job is None:
        _error(f"no job named {identifier!r}; see `{PROG} jobs`")
        return EXIT_USAGE
    profile: dict[str, Any] = {}
    if job["profile"]:
        profiles = load_profiles()
        if job["profile"] not in profiles:
            available = ", ".join(sorted(profiles)) or "none defined"
            _error(
                f"job {job['name']!r} references profile {job['profile']!r}, which is not "
                f"defined in the config file; available profiles: {available}"
            )
            return EXIT_USAGE
        profile = profiles[job["profile"]]
    namespace = argparse.Namespace(
        project=job["project"] or None,
        title=job["title"] or None,
    )
    try:
        options = _build_options_from_args(
            namespace,
            settings,
            question=job["question"],
            profile=profile,
            branding=load_branding(),
        )
        options.resolved(settings)
    except ToolError as exc:
        _error(f"{exc.code}: {exc.message}")
        return EXIT_USAGE
    except ConfigError as exc:
        _error(f"configuration error: {exc}")
        return EXIT_USAGE
    code, run_id = _execute_run_options(args, settings, options)
    if run_id:
        try:
            jobs.record_run(job["job_id"], run_id, settings=settings)
        except ToolError as exc:  # pragma: no cover - storage failure after a run
            _error(f"could not record the job history: {exc.code}: {exc.message}")
    return code


def _cmd_jobs(args: argparse.Namespace, settings: Settings) -> int:
    action = str(getattr(args, "jobs_command", None) or "list")
    try:
        if action == "list":
            payload = jobs.list_jobs(settings=settings)
            lines = [f"jobs: {payload['count']}"]
            for job in payload["jobs"]:
                detail = []
                if job["profile"]:
                    detail.append(f"profile={job['profile']}")
                if job["project"]:
                    detail.append(f"project={job['project']}")
                suffix = f" [{', '.join(detail)}]" if detail else ""
                lines.append(
                    f"  {job['name']}: {job['question']}{suffix} (runs={job['run_count']})"
                )
            if not payload["jobs"]:
                lines.append(f"  (define one with `{PROG} jobs add NAME QUESTION`)")
            _print_payload(payload, args, "\n".join(lines))
            return EXIT_OK

        if action == "add":
            profile_name = str(getattr(args, "profile", "") or "").strip()
            if profile_name:
                profiles = load_profiles()
                if profile_name not in profiles:
                    available = ", ".join(sorted(profiles)) or "none defined"
                    _error(
                        f"unknown profile {profile_name!r}; available profiles: {available}"
                    )
                    return EXIT_USAGE
            payload = jobs.add_job(
                getattr(args, "name", ""),
                getattr(args, "question", ""),
                profile=profile_name,
                project=str(getattr(args, "project", "") or ""),
                title=str(getattr(args, "title", "") or ""),
                notes=str(getattr(args, "notes", "") or ""),
                settings=settings,
            )
            lines = [
                f"job {payload['job_id']} ({payload['name']}) added: {payload['question']}"
            ]
            if payload["profile"]:
                lines.append(f"  profile = {payload['profile']}")
            if payload["project"]:
                lines.append(f"  project = {payload['project']}")
            _print_payload(payload, args, "\n".join(lines))
            return EXIT_OK

        if action == "show":
            identifier = getattr(args, "name", "")
            job = jobs.get_job(identifier, settings=settings)
            if job is None:
                _error(f"no job named {identifier!r}; see `{PROG} jobs`")
                return EXIT_USAGE
            lines = [f"job {job['job_id']} ({job['name']}): {job['question']}"]
            for key in ("profile", "project", "title", "notes"):
                if job.get(key):
                    lines.append(f"  {key} = {job[key]}")
            lines.append(f"  created = {job['created_at']}  updated = {job['updated_at']}")
            lines.append(f"  runs = {job['run_count']}")
            for run in job["runs"]:
                lines.append(
                    f"  run {run['run_id']}: {run['status']} (requested {run['requested_at']})"
                )
            _print_payload(job, args, "\n".join(lines))
            return EXIT_OK

        if action == "remove":
            payload = jobs.remove_job(getattr(args, "name", ""), settings=settings)
            job = payload["job"]
            _print_payload(payload, args, f"job {job['name']} removed (research runs kept)")
            return EXIT_OK

        if action == "run":
            return _jobs_run(args, settings)

        _error(f"usage: {PROG} jobs {{list|add|show|run|remove}}")
        return EXIT_USAGE
    except ToolError as exc:
        _error(f"{exc.code}: {exc.message}")
        return EXIT_USAGE
    except ConfigError as exc:
        _error(f"configuration error: {exc}")
        return EXIT_USAGE


def _monitor_state(snapshot: dict[str, Any]) -> str:
    """One human phrase for a snapshot row (never invents a deletion)."""
    if not snapshot:
        return "never checked"
    status = snapshot.get("status") or "unknown"
    if status == "changed":
        return "meaningful change" if snapshot.get("meaningful") else "boilerplate-only difference (not a change)"
    if status == "failed":
        return f"failed ({snapshot.get('error_code') or 'error'}; not a change or deletion)"
    return {
        "initial": "baseline stored",
        "unchanged": "unchanged",
        "not_modified": "not modified (304)",
    }.get(status, status)


def _cmd_monitor(args: argparse.Namespace, settings: Settings) -> int:
    action = str(getattr(args, "monitor_command", None) or "list")
    project = str(getattr(args, "project", "") or "")
    try:
        if action == "list":
            payload = monitor.list_targets(project=project, settings=settings)
            lines = [f"monitor targets: {payload['count']}"]
            for target in payload["targets"]:
                latest = target.get("last_snapshot") or {}
                suffix = f" [project={target['project']}]" if target["project"] else ""
                lines.append(
                    f"  {target['target_id']}: {target['name'] or target['target']} "
                    f"({target['kind']}; last check {latest.get('checked_at') or 'never'}: "
                    f"{_monitor_state(latest)}{suffix})"
                )
            if not payload["targets"]:
                lines.append(f"  (register one with `{PROG} monitor add URL_OR_FILE`)")
            _print_payload(payload, args, "\n".join(lines))
            return EXIT_OK

        if action == "add":
            payload = monitor.add_target(
                getattr(args, "target", ""),
                name=str(getattr(args, "name", "") or ""),
                project=project,
                kind=str(getattr(args, "kind", "") or ""),
                settings=settings,
            )
            target = payload["target"]
            lines = [
                f"target {target['target_id']} ({target['name']}) {('added' if payload['created'] else 'updated')}: "
                f"{target['target']} [{target['kind']}]"
            ]
            if target["project"]:
                lines.append(f"  project = {target['project']}")
            lines.append(f"  nothing is fetched until `{PROG} monitor check`")
            _print_payload(payload, args, "\n".join(lines))
            return EXIT_OK

        if action == "remove":
            payload = monitor.remove_target(int(getattr(args, "target_id", 0) or 0), settings=settings)
            _print_payload(
                payload,
                args,
                f"target {payload['target_id']} removed "
                f"({payload['snapshots_removed']} snapshot(s) deleted)",
            )
            return EXIT_OK

        if action == "check":
            payload = monitor.run_checks(
                target_ids=[int(item) for item in (getattr(args, "target_id", None) or [])],
                project=project,
                parallel_mode=not bool(getattr(args, "sequential", False)),
                settings=settings,
            )
            counts = payload["counts_by_status"]
            lines = [
                f"checked {payload['checked']} target(s) "
                f"({'sequential' if not payload['parallel'] else 'parallel'}): "
                + (", ".join(f"{key}={counts[key]}" for key in sorted(counts)) or "none")
            ]
            for snapshot in payload["snapshots"]:
                lines.append(
                    f"  target {snapshot['target_id']}: {_monitor_state(snapshot)} — "
                    f"{snapshot.get('summary') or ''}"
                )
            lines.extend(f"  warning: {item}" for item in payload["warnings"])
            _print_payload(payload, args, "\n".join(lines))
            return EXIT_OK

        if action == "changes":
            payload = monitor.changes(
                target_id=int(getattr(args, "target", 0) or 0),
                project=project,
                meaningful_only=not bool(getattr(args, "all", False)),
                since_days=int(getattr(args, "since_days", 0) or 0),
                limit=int(getattr(args, "limit", 50) or 50),
                settings=settings,
            )
            lines = [f"changes: {payload['count']}"]
            for item in payload["changes"]:
                change = item["change"]
                lines.append(
                    f"  target {item['target']['target_id']} "
                    f"({item['target']['name'] or item['target']['target']}) at {change['checked_at']}: "
                    f"{change.get('summary') or change.get('status')}"
                )
                lines.extend(f"    + {text}" for text in (change.get("added_terms") or [])[:5])
                lines.extend(f"    - {text}" for text in (change.get("removed_terms") or [])[:5])
            if not payload["changes"]:
                lines.append("  (no changes detected)")
            _print_payload(payload, args, "\n".join(lines))
            return EXIT_OK

        if action == "report":
            payload = monitor.render_change_report(
                target_ids=[int(item) for item in (getattr(args, "target_id", None) or [])],
                project=project,
                settings=settings,
            )
            if payload["created"]:
                lines = [
                    f"monitoring report written: {payload['path']}",
                    f"  targets: {payload['targets']}",
                ]
                lines.extend(f"  warning: {item}" for item in payload["warnings"])
            else:
                lines = ["no monitoring report written: " + "; ".join(payload["warnings"])]
            _print_payload(payload, args, "\n".join(lines))
            return EXIT_OK

        _error(f"usage: {PROG} monitor {{list|add|remove|check|changes|report}}")
        return EXIT_USAGE
    except ToolError as exc:
        _error(f"{exc.code}: {exc.message}")
        return EXIT_USAGE


def _cmd_config(args: argparse.Namespace, settings: Settings) -> int:
    action = getattr(args, "config_command", None)
    if action not in ("show", "validate"):
        _error("usage: agletras config {show|validate}")
        return EXIT_USAGE
    if action == "show":
        payload = {
            "config_file": str(settings.config_path) if settings.config_path else None,
            "settings": settings_snapshot(settings),
        }
        _print_payload(payload, args, _human_config(payload))
        return EXIT_OK
    report = validate_settings(settings)
    payload = {
        "ok": report["ok"],
        "errors": report["errors"],
        "warnings": report["warnings"],
        "config_file": report["config_file"],
    }
    if _opt(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        lines = [f"config file: {payload['config_file'] or '(none - defaults + environment)'}"]
        lines += [f"error: {item}" for item in payload["errors"]]
        lines += [f"warning: {item}" for item in payload["warnings"]]
        lines.append("configuration valid" if payload["ok"] else "configuration INVALID")
        print("\n".join(lines))
    return EXIT_OK if payload["ok"] else EXIT_USAGE


def _cmd_doctor(args: argparse.Namespace) -> int:
    settings: Settings | None = None
    config_error: str | None = None
    try:
        settings = load_settings(config_path=_opt(args, "config"))
    except ConfigError as exc:
        config_error = str(exc)
    reporter = None
    if settings is not None:
        reporter = _make_reporter(args, settings)

    def on_check(result: Any) -> None:
        if reporter is None:
            return
        reporter.info(
            "doctor_check",
            f"{result.name}: {result.status}",
            stage=result.name,
            status=result.status,
            duration_ms=result.duration_ms,
        )

    try:
        payload = doctor_mod.run_doctor(
            settings,
            config_error=config_error,
            include_mcp=not bool(getattr(args, "skip_mcp", False)),
            mcp_timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            on_check=on_check,
        )
    finally:
        if reporter is not None:
            reporter.close()
    _print_payload(payload, args, doctor_mod.render_human(payload))
    return int(payload["exit_code"])


def _cmd_version(args: argparse.Namespace) -> int:
    payload = {
        "name": "agletras-mcp",
        "version": __version__,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "schema_version": storage.SCHEMA_VERSION,
    }
    _print_payload(
        payload,
        args,
        f"agletras-mcp {payload['version']} (Python {payload['python']}; schema v{payload['schema_version']})",
    )
    return EXIT_OK


def _cmd_serve(args: argparse.Namespace) -> int:
    config = _opt(args, "config")
    if config:
        os.environ["AGLETRAS_CONFIG"] = str(Path(str(config)).expanduser())
    from .__main__ import main as serve_main

    serve_main()
    return EXIT_OK


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, dispatch, and return the process exit code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    command = getattr(args, "command", None)
    if command is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    if command == "version":
        return _cmd_version(args)
    if command == "serve":
        return _cmd_serve(args)
    if command == "doctor":
        return _cmd_doctor(args)

    settings, code = _load_settings_or_report(args)
    if settings is None:
        return code
    if command != "config":
        code = _validate_or_report(settings)
        if code != EXIT_OK:
            return code

    dispatch = {
        "run": _cmd_run,
        "resume": _cmd_resume,
        "status": _cmd_status,
        "list": _cmd_list,
        "report": _cmd_report,
        "profiles": _cmd_profiles,
        "export": _cmd_export,
        "jobs": _cmd_jobs,
        "monitor": _cmd_monitor,
        "config": _cmd_config,
    }
    handler = dispatch.get(command)
    if handler is None:  # pragma: no cover - argparse rejects unknown commands
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        return int(handler(args, settings))
    except KeyboardInterrupt:
        _error("interrupted")
        return EXIT_INTERRUPTED


def main() -> None:
    """Console-script entry point (``agletras``)."""
    sys.exit(run())


if __name__ == "__main__":  # pragma: no cover
    main()

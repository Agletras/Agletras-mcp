"""Runtime configuration for the agletras research toolkit.

Every path and limit can be set through (highest precedence first):

1. explicit CLI arguments (``agletras`` passes them as *overrides*),
2. environment variables (``AGLETRAS_*``),
3. a TOML configuration file (``AGLETRAS_CONFIG``, or ``$AGLETRAS_HOME/agletras.toml``
   / ``<project>/agletras.toml`` when it exists),
4. built-in defaults.

Settings are read fresh on every :func:`load_settings` call, which keeps the
unit tests hermetic. The configuration *file* is strictly validated: unknown
sections/keys and wrong value types raise :class:`ConfigError` instead of being
silently ignored. Environment variables keep their historical lenient parsing
(an unparsable value falls back to the next source) and are checked by
:func:`validate_settings`, which the CLI and ``agletras doctor`` run before any
long task starts.

Secrets never live in settings: model credentials are referenced by the
*name* of the environment variable that holds them (``*_api_key_env``), and
:func:`settings_snapshot` masks any key that looks secret before persisting.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping

PACKAGE_DIR = Path(__file__).resolve().parent
# src/agletras_mcp/config.py -> src -> project root
PROJECT_ROOT = PACKAGE_DIR.parents[1]

_MIB = 1024 * 1024

#: Values accepted by ``research.depth`` and their budget presets. Explicit
#: research.* settings override individual preset values.
DEPTH_PRESETS: dict[str, dict[str, int]] = {
    "quick": {
        "max_sources": 4,
        "max_claims": 10,
        "searches_per_subquestion": 1,
        "followup_rounds": 0,
        "results_per_search": 4,
    },
    "standard": {
        "max_sources": 8,
        "max_claims": 25,
        "searches_per_subquestion": 2,
        "followup_rounds": 1,
        "results_per_search": 5,
    },
    "thorough": {
        "max_sources": 16,
        "max_claims": 50,
        "searches_per_subquestion": 3,
        "followup_rounds": 2,
        "results_per_search": 6,
    },
}
DEPTHS = tuple(DEPTH_PRESETS)

SEARCH_PROVIDERS = ("duckduckgo", "wikipedia")
IMAGE_SEARCH_PROVIDERS = ("wikimedia_commons",)
LOG_FORMATS = ("human", "json")
VERIFICATION_METHODS = ("auto", "deterministic")


class ConfigError(Exception):
    """Invalid configuration: unknown keys, bad types, or unusable values."""


@dataclass(frozen=True)
class Settings:
    """Resolved paths and limits for one invocation (server or CLI)."""

    project_root: Path
    data_dir: Path
    reports_dir: Path
    cache_dir: Path
    db_path: Path
    fetch_timeout: float
    fetch_max_bytes: int
    fetch_max_chars: int
    pdf_max_bytes: int
    pdf_max_pages: int
    pdf_max_chars: int
    default_list_limit: int
    max_list_limit: int
    user_agent: str
    # --- Phase 1: SSRF policy, fetch bounds, and web search ---
    allow_private_hosts: bool
    allowed_domains: tuple[str, ...]
    max_redirects: int
    max_chars_cap: int
    search_provider: str
    search_endpoint: str
    search_min_interval: float
    max_search_results: int
    # --- Phase 2: source-collection pipeline ---
    local_roots: tuple[Path, ...]
    collect_concurrency: int
    collect_max_items: int
    collect_max_retries: int
    collect_backoff: float
    doc_max_chars: int
    folder_max_files: int
    folder_max_depth: int
    folder_max_file_bytes: int
    office_max_cells: int
    # --- Phase 3: evidence library ---
    evidence_cache_ttl: int
    evidence_passage_chars: int
    evidence_fts: str
    # --- Phase 5: image research and visual analysis ---
    image_search_provider: str
    image_max_bytes: int
    image_max_dimension: int
    image_max_results: int
    vision_provider: str
    vision_model: str
    vision_model_endpoint: str
    vision_api_key_env: str
    # --- Phase 6: claim verification and report synthesis ---
    verification_model_endpoint: str
    verification_model: str
    verification_api_key_env: str
    verification_max_claims: int
    verification_max_candidates: int
    report_max_findings: int
    # --- Phase 7: CLI, configuration file, run pipeline, logging ---
    logs_dir: Path
    log_format: str
    log_file: str
    config_path: Path | None
    research_depth: str
    research_max_sources: int
    research_max_claims: int
    research_results_per_search: int
    research_searches_per_subquestion: int
    research_followup_rounds: int
    research_verification_method: str
    research_include_images: bool
    research_image_results: int
    research_resume_max_age_days: int
    # --- Phase 8: advanced agents and product features ---
    research_reuse_max_age_days: int


# ---------------------------------------------------------------------------
# Declarative specification (env var, config file section/key, kind, default)
# ---------------------------------------------------------------------------

_Scalar = str | int | float | bool | tuple[str, ...] | Path | tuple[Path, ...]


class _Field:
    __slots__ = ("name", "env", "section", "key", "kind", "default")

    def __init__(
        self,
        name: str,
        env: str,
        section: str,
        key: str,
        kind: str,
        default: Any,
    ) -> None:
        self.name = name
        self.env = env
        self.section = section
        self.key = key
        self.kind = kind  # str|int|float|bool|csv|path|path_list
        self.default = default  # constant, or callable(ctx) for dependent paths


# NOTE: keep this table in sync with the Settings dataclass; a test asserts it.
_SPEC: tuple[_Field, ...] = (
    # paths (handled specially: home/db_path defaults depend on other values)
    _Field("data_dir", "AGLETRAS_DATA_DIR", "paths", "data_dir", "path", "home/data"),
    _Field("reports_dir", "AGLETRAS_REPORTS_DIR", "paths", "reports_dir", "path", "home/reports"),
    _Field("cache_dir", "AGLETRAS_CACHE_DIR", "paths", "cache_dir", "path", "home/cache"),
    _Field("logs_dir", "AGLETRAS_LOGS_DIR", "paths", "logs_dir", "path", "home/logs"),
    _Field("db_path", "AGLETRAS_DB_PATH", "paths", "db_path", "path", "data_dir/research.db"),
    _Field("local_roots", "AGLETRAS_LOCAL_ROOTS", "paths", "local_roots", "path_list", "project_root"),
    # fetch
    _Field("fetch_timeout", "AGLETRAS_FETCH_TIMEOUT", "fetch", "timeout", "float", 20.0),
    _Field("fetch_max_bytes", "AGLETRAS_FETCH_MAX_BYTES", "fetch", "max_bytes", "int", 10 * _MIB),
    _Field("fetch_max_chars", "AGLETRAS_FETCH_MAX_CHARS", "fetch", "max_chars", "int", 50_000),
    _Field("max_chars_cap", "AGLETRAS_MAX_CHARS_CAP", "fetch", "max_chars_cap", "int", 500_000),
    _Field("max_redirects", "AGLETRAS_MAX_REDIRECTS", "fetch", "max_redirects", "int", 10),
    _Field(
        "user_agent",
        "AGLETRAS_USER_AGENT",
        "fetch",
        "user_agent",
        "str",
        "agletras-mcp/0.2.0 (local zero-cost research tool)",
    ),
    # security policy
    _Field("allow_private_hosts", "AGLETRAS_ALLOW_PRIVATE_HOSTS", "security", "allow_private_hosts", "bool", False),
    _Field("allowed_domains", "AGLETRAS_ALLOWED_DOMAINS", "security", "allowed_domains", "csv", ()),
    # search
    _Field("search_provider", "AGLETRAS_SEARCH_PROVIDER", "search", "provider", "str", "duckduckgo"),
    _Field("search_endpoint", "AGLETRAS_SEARCH_ENDPOINT", "search", "endpoint", "str", ""),
    _Field("search_min_interval", "AGLETRAS_SEARCH_MIN_INTERVAL", "search", "min_interval", "float", 2.0),
    _Field("max_search_results", "AGLETRAS_MAX_SEARCH_RESULTS", "search", "max_results", "int", 10),
    # pdf
    _Field("pdf_max_bytes", "AGLETRAS_PDF_MAX_BYTES", "pdf", "max_bytes", "int", 100 * _MIB),
    _Field("pdf_max_pages", "AGLETRAS_PDF_MAX_PAGES", "pdf", "max_pages", "int", 200),
    _Field("pdf_max_chars", "AGLETRAS_PDF_MAX_CHARS", "pdf", "max_chars", "int", 200_000),
    # storage listing
    _Field("default_list_limit", "AGLETRAS_DEFAULT_LIST_LIMIT", "storage", "default_list_limit", "int", 20),
    _Field("max_list_limit", "AGLETRAS_MAX_LIST_LIMIT", "storage", "max_list_limit", "int", 200),
    # collection pipeline
    _Field("collect_concurrency", "AGLETRAS_COLLECT_CONCURRENCY", "collect", "concurrency", "int", 4),
    _Field("collect_max_items", "AGLETRAS_COLLECT_MAX_ITEMS", "collect", "max_items", "int", 50),
    _Field("collect_max_retries", "AGLETRAS_COLLECT_MAX_RETRIES", "collect", "max_retries", "int", 2),
    _Field("collect_backoff", "AGLETRAS_COLLECT_BACKOFF", "collect", "backoff", "float", 0.5),
    _Field("doc_max_chars", "AGLETRAS_DOC_MAX_CHARS", "collect", "doc_max_chars", "int", 200_000),
    _Field("folder_max_files", "AGLETRAS_FOLDER_MAX_FILES", "folders", "max_files", "int", 200),
    _Field("folder_max_depth", "AGLETRAS_FOLDER_MAX_DEPTH", "folders", "max_depth", "int", 6),
    _Field("folder_max_file_bytes", "AGLETRAS_FOLDER_MAX_FILE_BYTES", "folders", "max_file_bytes", "int", 20 * _MIB),
    _Field("office_max_cells", "AGLETRAS_OFFICE_MAX_CELLS", "folders", "office_max_cells", "int", 20_000),
    # evidence library
    _Field("evidence_cache_ttl", "AGLETRAS_EVIDENCE_CACHE_TTL", "evidence", "cache_ttl", "int", 86_400),
    _Field("evidence_passage_chars", "AGLETRAS_EVIDENCE_PASSAGE_CHARS", "evidence", "passage_chars", "int", 1200),
    _Field("evidence_fts", "AGLETRAS_EVIDENCE_FTS", "evidence", "fts", "str", "auto"),
    # images
    _Field("image_search_provider", "AGLETRAS_IMAGE_SEARCH_PROVIDER", "images", "search_provider", "str", "wikimedia_commons"),
    _Field("image_max_bytes", "AGLETRAS_IMAGE_MAX_BYTES", "images", "max_bytes", "int", 10 * _MIB),
    _Field("image_max_dimension", "AGLETRAS_IMAGE_MAX_DIMENSION", "images", "max_dimension", "int", 4096),
    _Field("image_max_results", "AGLETRAS_IMAGE_MAX_RESULTS", "images", "max_results", "int", 10),
    _Field("vision_provider", "AGLETRAS_VISION_PROVIDER", "images", "vision_provider", "str", "none"),
    _Field("vision_model", "AGLETRAS_VISION_MODEL", "images", "vision_model", "str", "nemotron-3.5-super-vl-free"),
    _Field("vision_model_endpoint", "AGLETRAS_VISION_MODEL_ENDPOINT", "images", "vision_model_endpoint", "str", ""),
    _Field("vision_api_key_env", "AGLETRAS_VISION_API_KEY_ENV", "images", "vision_api_key_env", "str", "AGLETRAS_VISION_API_KEY"),
    # verification
    _Field("verification_model_endpoint", "AGLETRAS_VERIFICATION_MODEL_ENDPOINT", "verification", "model_endpoint", "str", ""),
    _Field("verification_model", "AGLETRAS_VERIFICATION_MODEL", "verification", "model", "str", "unspecified"),
    _Field("verification_api_key_env", "AGLETRAS_VERIFICATION_API_KEY_ENV", "verification", "api_key_env", "str", "AGLETRAS_VERIFICATION_API_KEY"),
    _Field("verification_max_claims", "AGLETRAS_VERIFICATION_MAX_CLAIMS", "verification", "max_claims", "int", 50),
    _Field("verification_max_candidates", "AGLETRAS_VERIFICATION_MAX_CANDIDATES", "verification", "max_candidates", "int", 8),
    _Field("report_max_findings", "AGLETRAS_REPORT_MAX_FINDINGS", "reports", "max_findings", "int", 60),
    # Phase 7: logging and the CLI research pipeline
    _Field("log_format", "AGLETRAS_LOG_FORMAT", "logging", "format", "str", "human"),
    _Field("log_file", "AGLETRAS_LOG_FILE", "logging", "file", "str", ""),
    _Field("research_depth", "AGLETRAS_RESEARCH_DEPTH", "research", "depth", "str", "standard"),
    _Field("research_max_sources", "AGLETRAS_RESEARCH_MAX_SOURCES", "research", "max_sources", "int", 0),
    _Field("research_max_claims", "AGLETRAS_RESEARCH_MAX_CLAIMS", "research", "max_claims", "int", 0),
    _Field("research_results_per_search", "AGLETRAS_RESEARCH_RESULTS_PER_SEARCH", "research", "results_per_search", "int", 0),
    _Field("research_searches_per_subquestion", "AGLETRAS_RESEARCH_SEARCHES_PER_SUBQUESTION", "research", "searches_per_subquestion", "int", 0),
    _Field("research_followup_rounds", "AGLETRAS_RESEARCH_FOLLOWUP_ROUNDS", "research", "followup_rounds", "int", -1),
    _Field("research_verification_method", "AGLETRAS_RESEARCH_VERIFICATION_METHOD", "research", "verification_method", "str", "auto"),
    _Field("research_include_images", "AGLETRAS_RESEARCH_INCLUDE_IMAGES", "research", "include_images", "bool", False),
    _Field("research_image_results", "AGLETRAS_RESEARCH_IMAGE_RESULTS", "research", "image_results", "int", 3),
    _Field("research_resume_max_age_days", "AGLETRAS_RESEARCH_RESUME_MAX_AGE_DAYS", "research", "resume_max_age_days", "int", 30),
    # Phase 8: how old a stored copy may be to be reused across runs/projects.
    _Field("research_reuse_max_age_days", "AGLETRAS_RESEARCH_REUSE_MAX_AGE_DAYS", "research", "reuse_max_age_days", "int", 7),
)

_SPEC_BY_ENV = {field.env: field for field in _SPEC}
_SPEC_BY_NAME = {field.name: field for field in _SPEC}
#: config file layout: section -> key -> field (for unknown-key diagnostics)
_SECTIONS: dict[str, dict[str, _Field]] = {}
for _field in _SPEC:
    _SECTIONS.setdefault(_field.section, {})[_field.key] = _field

#: Settings fields that are not configurable (fixed by the installation).
_FIXED_FIELDS = ("project_root",)
_FIXED = {"project_root": PROJECT_ROOT}

#: Phase 8 named research profiles: allowed keys inside a ``[profiles.<name>]``
#: table and the type each value must have. Profiles are *option presets*, not
#: a second settings layer: the CLI resolves explicit flags over profile values
#: over the ``[research]`` settings.
PROFILE_KEYS: dict[str, str] = {
    "depth": "str",
    "max_sources": "int",
    "max_claims": "int",
    "searches_per_subquestion": "int",
    "followup_rounds": "int",
    "results_per_search": "int",
    "verification_method": "str",
    "include_images": "bool",
    "image_results": "int",
    "parallel": "bool",
    "reuse_evidence": "bool",
    "project": "str",
    "title": "str",
}

#: Phase 8 branding: allowed keys inside ``[branding]``.
BRANDING_KEYS = ("name", "primary_color", "footer")


# ---------------------------------------------------------------------------
# Parsing helpers (lenient for env, strict for config files)
# ---------------------------------------------------------------------------


def _parse_bool(raw: Any) -> bool | None:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if not text:
        return None
    return text in ("1", "true", "yes", "on")


def _parse_csv(raw: Any) -> tuple[str, ...] | None:
    if isinstance(raw, (list, tuple)):
        parts = [str(item) for item in raw]
    else:
        text = str(raw).replace(";", ",")
        if not text.strip():
            return None
        parts = text.split(",")
    return tuple(part.strip().lower() for part in parts if str(part).strip())


def _coerce_env(field: _Field, raw: str) -> _Scalar | None:
    """Parse an environment value; unparsable values fall back (None)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        if field.kind == "str":
            return raw
        if field.kind == "int":
            return int(raw)
        if field.kind == "float":
            return float(raw)
        if field.kind == "bool":
            return _parse_bool(raw)
        if field.kind == "csv":
            return _parse_csv(raw)
        if field.kind == "path":
            return Path(raw).expanduser().resolve()
        if field.kind == "path_list":
            roots: list[Path] = []
            for part in raw.replace(";", ",").split(","):
                part = part.strip()
                if part:
                    roots.append(Path(part).expanduser().resolve())
            return tuple(roots) or None
    except (TypeError, ValueError):
        return None
    return None  # pragma: no cover - all kinds handled


def _coerce_strict(field: _Field, value: Any, *, source: str, base_dir: Path | None) -> _Scalar:
    """Parse a config-file/CLI value; raise ConfigError when unusable."""
    label = f"{field.section}.{field.key}" if "." not in source else source
    if field.kind == "str":
        if not isinstance(value, str):
            raise ConfigError(f"{label} must be a string, got {type(value).__name__}")
        return value.strip()
    if field.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{label} must be an integer, got {type(value).__name__}")
        return int(value)
    if field.kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{label} must be a number, got {type(value).__name__}")
        return float(value)
    if field.kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{label} must be true or false, got {type(value).__name__}")
        return value
    if field.kind == "csv":
        if isinstance(value, str):
            parsed = _parse_csv(value)
            return parsed or ()
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return _parse_csv(value) or ()
        raise ConfigError(f"{label} must be a string or a list of strings")
    if field.kind == "path":
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{label} must be a non-empty path string")
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = (base_dir or PROJECT_ROOT) / path
        return path.resolve()
    if field.kind == "path_list":
        if isinstance(value, str):
            items = [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            items = [item.strip() for item in value if item.strip()]
        else:
            raise ConfigError(f"{label} must be a comma-separated string or a list of paths")
        if not items:
            raise ConfigError(f"{label} must contain at least one path")
        resolved: list[Path] = []
        for item in items:
            path = Path(item).expanduser()
            if not path.is_absolute():
                path = (base_dir or PROJECT_ROOT) / path
            resolved.append(path.resolve())
        return tuple(resolved)
    raise ConfigError(f"{label}: unsupported kind {field.kind!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Config file loading
# ---------------------------------------------------------------------------


def default_config_path() -> Path:
    """The config file used when none is given: ``$AGLETRAS_HOME/agletras.toml``."""
    home_env = os.environ.get("AGLETRAS_HOME", "").strip()
    home = Path(home_env).expanduser() if home_env else PROJECT_ROOT
    return home / "agletras.toml"


def _validate_profile(name: str, table: dict[str, Any], path: Path) -> dict[str, Any]:
    """Strictly validate one ``[profiles.<name>]`` table."""
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{path}: profile names must be non-empty strings")
    clean: dict[str, Any] = {}
    for key, value in table.items():
        kind = PROFILE_KEYS.get(key)
        if kind is None:
            raise ConfigError(
                f"{path}: unknown key profiles.{name}.{key}; valid keys: "
                + ", ".join(sorted(PROFILE_KEYS))
            )
        if kind == "bool":
            if not isinstance(value, bool):
                raise ConfigError(f"{path}: profiles.{name}.{key} must be true or false")
        elif kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"{path}: profiles.{name}.{key} must be an integer")
            floor = -1 if key == "followup_rounds" else 0
            if value < floor:
                raise ConfigError(
                    f"{path}: profiles.{name}.{key} must be >= {floor} (got {value})"
                )
        else:
            if not isinstance(value, str):
                raise ConfigError(f"{path}: profiles.{name}.{key} must be a string")
            if key == "depth" and value.strip().lower() not in DEPTHS:
                raise ConfigError(
                    f"{path}: profiles.{name}.depth must be one of {', '.join(DEPTHS)}; got {value!r}"
                )
            if key == "verification_method" and value.strip().lower() not in VERIFICATION_METHODS:
                raise ConfigError(
                    f"{path}: profiles.{name}.verification_method must be one of "
                    f"{', '.join(VERIFICATION_METHODS)}; got {value!r}"
                )
        clean[key] = value
    return clean


def _validate_branding(table: dict[str, Any], path: Path) -> dict[str, str]:
    """Strictly validate the ``[branding]`` table (report/job titling)."""
    clean: dict[str, str] = {}
    for key, value in table.items():
        if key not in BRANDING_KEYS:
            raise ConfigError(
                f"{path}: unknown key branding.{key}; valid keys: " + ", ".join(BRANDING_KEYS)
            )
        if not isinstance(value, str):
            raise ConfigError(f"{path}: branding.{key} must be a string")
        clean[key] = value.strip()
    if "name" in clean and not clean["name"]:
        raise ConfigError(f"{path}: branding.name must not be empty")
    color = clean.get("primary_color", "")
    if color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ConfigError(
            f"{path}: branding.primary_color must be a #rrggbb hex color; got {color!r}"
        )
    return {key: value for key, value in clean.items() if value}


def _load_config_file(path: Path) -> tuple[dict[str, Any], Path]:
    """Parse and validate a TOML config file into ``{settings_field: value}``."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    if not isinstance(data, dict):  # pragma: no cover - tomllib always yields a dict
        raise ConfigError(f"{path}: configuration root must be a table")
    values: dict[str, Any] = {}
    home_value: Path | None = None
    # `paths.home` is resolved first: other relative paths follow it.
    paths_table = data.get("paths")
    if isinstance(paths_table, dict) and "home" in paths_table:
        raw_home = paths_table["home"]
        if not isinstance(raw_home, str) or not raw_home.strip():
            raise ConfigError("paths.home must be a non-empty path string")
        home_value = Path(raw_home).expanduser()
        if not home_value.is_absolute():
            home_value = path.parent / home_value
        home_value = home_value.resolve()
    base_dir = home_value or path.parent
    profiles: dict[str, dict[str, Any]] = {}
    branding: dict[str, str] = {}
    for section, table in data.items():
        if section == "profiles":
            if not isinstance(table, dict):
                raise ConfigError(f"{path}: [profiles] must be a table of profile tables")
            for name, profile_table in table.items():
                if not isinstance(profile_table, dict):
                    raise ConfigError(f"{path}: [profiles.{name}] must be a table of settings")
                profiles[str(name)] = _validate_profile(str(name), profile_table, path)
            continue
        if section == "branding":
            if not isinstance(table, dict):
                raise ConfigError(f"{path}: [branding] must be a table of key = value settings")
            branding = _validate_branding(table, path)
            continue
        if section == "paths" and isinstance(table, dict):
            unknown = set(table) - {key for key in _SECTIONS["paths"]} - {"home"}
            if unknown:
                raise ConfigError(
                    f"{path}: unknown key(s) in [paths]: {', '.join(sorted(unknown))} "
                    f"(valid: home, {', '.join(sorted(_SECTIONS['paths']))})"
                )
        if not isinstance(table, dict):
            raise ConfigError(f"{path}: [{section}] must be a table of key = value settings")
        if section not in _SECTIONS:
            raise ConfigError(
                f"{path}: unknown section [{section}]; valid sections: "
                + ", ".join(sorted(_SECTIONS))
            )
        for key, value in table.items():
            if section == "paths" and key == "home":
                continue
            field = _SECTIONS[section].get(key)
            if field is None:
                raise ConfigError(
                    f"{path}: unknown key {section}.{key}; valid keys in [{section}]: "
                    + ", ".join(sorted(_SECTIONS[section]))
                )
            values[field.name] = _coerce_strict(field, value, source=f"file:{section}.{key}", base_dir=base_dir)
    values["_home"] = home_value if home_value is not None else None
    values["_profiles"] = profiles
    values["_branding"] = branding
    return values, path


# ---------------------------------------------------------------------------
# Settings assembly
# ---------------------------------------------------------------------------


def _env_value(field: _Field) -> _Scalar | None:
    raw = os.environ.get(field.env)
    if raw is None:
        return None
    return _coerce_env(field, raw)


def load_settings(
    *,
    overrides: Mapping[str, Any] | None = None,
    config_path: str | Path | None = None,
) -> Settings:
    """Build settings from CLI overrides, environment, config file, defaults.

    Precedence: overrides > environment > config file > built-in defaults.
    ``config_path`` (or ``AGLETRAS_CONFIG``) selects an explicit file; without
    one, ``$AGLETRAS_HOME/agletras.toml`` is used when it exists.
    """
    overrides = dict(overrides or {})
    unknown = set(overrides) - set(_SPEC_BY_NAME) - set(_FIXED_FIELDS) - {"home"}
    if unknown:
        raise ConfigError(f"unknown settings override(s): {', '.join(sorted(unknown))}")

    explicit = str(config_path).strip() if config_path else os.environ.get("AGLETRAS_CONFIG", "").strip()
    file_values: dict[str, Any] = {"_home": None}
    config_file: Path | None = None
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.exists():
            raise ConfigError(f"configuration file not found: {candidate}")
        file_values, config_file = _load_config_file(candidate)
    else:
        candidate = default_config_path()
        if candidate.is_file():
            file_values, config_file = _load_config_file(candidate)

    home_env = os.environ.get("AGLETRAS_HOME", "").strip()
    home_override = overrides.get("home")
    if home_override is not None:
        home = Path(str(home_override)).expanduser().resolve()
    elif home_env:
        home = Path(home_env).expanduser().resolve()
    elif file_values.get("_home") is not None:
        home = Path(file_values["_home"])
    else:
        home = PROJECT_ROOT

    def resolve(field: _Field) -> _Scalar:
        if field.name in overrides:
            value = overrides[field.name]
            if isinstance(value, Path):
                return value.expanduser().resolve()
            if field.kind in ("csv", "path_list") and isinstance(value, (list, tuple)):
                if field.kind == "csv":
                    return _parse_csv(list(value)) or ()
                return tuple(Path(item).expanduser().resolve() for item in value) or (PROJECT_ROOT,)
            return _coerce_strict(field, value, source=field.name, base_dir=None)
        env_value = _env_value(field)
        if env_value is not None:
            return env_value
        if field.name in file_values:
            return file_values[field.name]
        default = field.default
        if isinstance(default, str) and default in ("home/data", "home/reports", "home/cache", "home/logs"):
            return (home / default.split("/", 1)[1]).resolve()
        if default == "data_dir/research.db":
            return (resolved["data_dir"] / "research.db").resolve()  # type: ignore[operator]
        if default == "project_root":
            return (PROJECT_ROOT,) if field.kind == "path_list" else PROJECT_ROOT
        return default

    resolved: dict[str, Any] = {}
    # data_dir must resolve before db_path's dependent default; the spec order
    # guarantees it (see the table above).
    for field in _SPEC:
        resolved[field.name] = resolve(field)
    # A few enum-ish values are case-insensitive across env/config/CLI.
    for lowered in ("evidence_fts", "log_format", "research_depth", "research_verification_method"):
        resolved[lowered] = str(resolved[lowered]).strip().lower()

    values: dict[str, Any] = {
        **{name: _FIXED[name] for name in _FIXED_FIELDS},
        **resolved,
        "config_path": config_file,
    }
    return Settings(**values)


def _meta_config_values(config_path: str | Path | None = None) -> dict[str, Any]:
    """Raw validated config-file values (for profiles/branding), or ``{}``."""
    explicit = (
        str(config_path).strip()
        if config_path
        else os.environ.get("AGLETRAS_CONFIG", "").strip()
    )
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.exists():
            raise ConfigError(f"configuration file not found: {candidate}")
        values, _ = _load_config_file(candidate)
        return values
    candidate = default_config_path()
    if candidate.is_file():
        values, _ = _load_config_file(candidate)
        return values
    return {}


def load_profiles(config_path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Named research profiles from the config file (empty when none).

    Profiles are validated option presets (``[profiles.<name>]``); the CLI
    resolves explicit flags over a selected profile over the ``[research]``
    settings. Values are returned exactly as validated — nothing is applied
    until a caller uses them.
    """
    values = _meta_config_values(config_path)
    profiles = values.get("_profiles") or {}
    return {str(name): dict(table) for name, table in profiles.items()}


def load_branding(config_path: str | Path | None = None) -> dict[str, str]:
    """Validated ``[branding]`` values (report/job titling), or ``{}``."""
    values = _meta_config_values(config_path)
    branding = values.get("_branding") or {}
    return {str(key): str(value) for key, value in branding.items()}


# ---------------------------------------------------------------------------
# Validation and redaction
# ---------------------------------------------------------------------------

_SECRET_HINT = ("api_key", "apikey", "secret", "token", "password", "credential")
_URL_CREDENTIAL = re.compile(r"(?i)\b(https?://)([^/\s:@]+):([^@\s/]+)@")


def settings_snapshot(settings: Settings) -> dict[str, Any]:
    """A JSON-serializable settings dump with secret-looking values masked.

    Two layers of protection: fields whose *name* looks like a credential are
    fully redacted, and credentials embedded in URL values
    (``https://user:pass@host``) are masked even for fields with neutral names.
    """
    snapshot: dict[str, Any] = {}
    for field in fields(settings):
        value = getattr(settings, field.name)
        if isinstance(value, Path):
            snapshot[field.name] = str(value)
        elif isinstance(value, tuple):
            snapshot[field.name] = [str(item) for item in value]
        else:
            snapshot[field.name] = value
        if any(hint in field.name.lower() for hint in _SECRET_HINT):
            snapshot[field.name] = "***redacted***"
        elif isinstance(snapshot[field.name], str):
            snapshot[field.name] = _URL_CREDENTIAL.sub(r"\1\2:***@", snapshot[field.name])
    return snapshot


def redact_value(name: str, value: Any) -> Any:
    """Mask one value when its *name* looks like a credential reference."""
    if any(hint in (name or "").lower() for hint in _SECRET_HINT):
        return "***redacted***"
    return value


def validate_settings(settings: Settings | None = None) -> dict[str, Any]:
    """Check the effective configuration; returns errors, warnings, and info.

    Environment parsing stays lenient for backwards compatibility (a malformed
    value falls back to the default), so this function is what the CLI and
    ``agletras doctor`` run to catch unusable values *before* a long task.
    """
    settings = settings or load_settings()
    errors: list[str] = []
    warnings: list[str] = []

    def positive_int(name: str, value: int, *, minimum: int = 1) -> None:
        if value < minimum:
            errors.append(f"{name}={value} must be >= {minimum}")

    def non_negative(name: str, value: int | float) -> None:
        if value < 0:
            errors.append(f"{name}={value} must be >= 0")

    if settings.fetch_timeout <= 0:
        errors.append(f"fetch.timeout={settings.fetch_timeout} must be > 0 seconds")
    positive_int("fetch.max_bytes", settings.fetch_max_bytes, minimum=1024)
    positive_int("fetch.max_chars", settings.fetch_max_chars, minimum=1000)
    positive_int("fetch.max_chars_cap", settings.max_chars_cap, minimum=1000)
    positive_int("fetch.max_redirects", settings.max_redirects)
    if settings.max_chars_cap < settings.fetch_max_chars:
        warnings.append(
            f"fetch.max_chars_cap={settings.max_chars_cap} is below fetch.max_chars="
            f"{settings.fetch_max_chars}; argument caps will truncate at the lower value"
        )
    positive_int("pdf.max_bytes", settings.pdf_max_bytes, minimum=1024)
    positive_int("pdf.max_pages", settings.pdf_max_pages)
    positive_int("pdf.max_chars", settings.pdf_max_chars, minimum=1000)
    positive_int("storage.default_list_limit", settings.default_list_limit)
    positive_int("storage.max_list_limit", settings.max_list_limit)
    if settings.default_list_limit > settings.max_list_limit:
        errors.append(
            f"storage.default_list_limit={settings.default_list_limit} exceeds "
            f"storage.max_list_limit={settings.max_list_limit}"
        )
    if settings.search_provider not in SEARCH_PROVIDERS:
        errors.append(
            f"search.provider={settings.search_provider!r} is unknown; "
            f"valid providers: {', '.join(SEARCH_PROVIDERS)}"
        )
    non_negative("search.min_interval", settings.search_min_interval)
    positive_int("search.max_results", settings.max_search_results)
    if settings.collect_concurrency < 1 or settings.collect_concurrency > 32:
        errors.append(f"collect.concurrency={settings.collect_concurrency} must be in 1..32")
    positive_int("collect.max_items", settings.collect_max_items)
    non_negative("collect.max_retries", settings.collect_max_retries)
    non_negative("collect.backoff", settings.collect_backoff)
    positive_int("collect.doc_max_chars", settings.doc_max_chars, minimum=1000)
    positive_int("folders.max_files", settings.folder_max_files)
    non_negative("folders.max_depth", settings.folder_max_depth)
    positive_int("folders.max_file_bytes", settings.folder_max_file_bytes, minimum=1024)
    positive_int("folders.office_max_cells", settings.office_max_cells)
    non_negative("evidence.cache_ttl", settings.evidence_cache_ttl)
    positive_int("evidence.passage_chars", settings.evidence_passage_chars, minimum=200)
    if settings.evidence_fts not in ("auto", "off"):
        errors.append(f"evidence.fts={settings.evidence_fts!r} must be 'auto' or 'off'")
    if settings.image_search_provider not in IMAGE_SEARCH_PROVIDERS:
        errors.append(
            f"images.search_provider={settings.image_search_provider!r} is unknown; "
            f"valid providers: {', '.join(IMAGE_SEARCH_PROVIDERS)}"
        )
    positive_int("images.max_bytes", settings.image_max_bytes, minimum=1024)
    positive_int("images.max_dimension", settings.image_max_dimension)
    positive_int("images.max_results", settings.image_max_results)
    positive_int("verification.max_claims", settings.verification_max_claims)
    positive_int("verification.max_candidates", settings.verification_max_candidates)
    positive_int("reports.max_findings", settings.report_max_findings)
    if settings.log_format not in LOG_FORMATS:
        errors.append(f"logging.format={settings.log_format!r} must be one of {', '.join(LOG_FORMATS)}")
    if settings.research_depth not in DEPTHS:
        errors.append(f"research.depth={settings.research_depth!r} must be one of {', '.join(DEPTHS)}")
    preset_depth = settings.research_depth if settings.research_depth in DEPTHS else "standard"
    positive_int(
        "research.max_sources",
        settings.research_max_sources or DEPTH_PRESETS[preset_depth]["max_sources"],
    )
    if settings.research_followup_rounds < -1:
        errors.append(
            f"research.followup_rounds={settings.research_followup_rounds} must be -1 (auto), 0, or a positive count"
        )
    if settings.research_verification_method not in VERIFICATION_METHODS:
        errors.append(
            f"research.verification_method={settings.research_verification_method!r} must be one of "
            f"{', '.join(VERIFICATION_METHODS)}"
        )
    positive_int("research.image_results", settings.research_image_results)
    non_negative("research.resume_max_age_days", settings.research_resume_max_age_days)
    non_negative("research.reuse_max_age_days", settings.research_reuse_max_age_days)

    # Path-level checks (existence handled by doctor; here check writability).
    for label, path in (
        ("paths.data_dir", settings.data_dir),
        ("paths.reports_dir", settings.reports_dir),
        ("paths.cache_dir", settings.cache_dir),
        ("paths.logs_dir", settings.logs_dir),
    ):
        probe = path if path.exists() else path.parent
        if path.exists() and not path.is_dir():
            errors.append(f"{label}={path} exists and is not a directory")
        elif not os.access(probe, os.W_OK):
            errors.append(f"{label}={path} is not writable")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "config_file": str(settings.config_path) if settings.config_path else None,
        "settings": settings_snapshot(settings),
    }

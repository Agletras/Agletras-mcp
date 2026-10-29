"""Local file/folder access policy and directory scanning (Phase 2).

Every local file read goes through:
- *explicit access roots* (``AGLETRAS_LOCAL_ROOTS``, comma-separated; default:
  the project root). Paths outside the roots — including paths that merely
  resolve through a symlink to somewhere outside — are refused with
  ``access_denied``;
- skip rules for temporary/hidden files with a recorded reason (nothing is
  silently dropped);
- caps on file count, directory depth, and per-file size.

Logs never contain file *contents*, only paths and counts.

Extension → source-type mapping (the adapter router uses it):

===================================  ==============
Extensions                           source_type
===================================  ==============
``.pdf``                             pdf
``.md .markdown .mdown .mkd``        markdown
``.txt .text .rst .log``             text
``.csv .tsv``                        csv
``.docx``                            docx
``.xlsx .xlsm``                      xlsx
``.html .htm``                       html_file
===================================  ==============
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

from .config import Settings
from .errors import ToolError

# Temporary/lock/editor/artifact patterns that must never be collected.
_TEMP_SUFFIXES = (
    ".tmp", ".temp", ".swp", ".swo", ".part", ".partial", ".crdownload",
    ".download", ".bak", ".autorecover",
)
_TEMP_PREFIXES = ("~", "$", ".#")  # ~lock files, Office owner files, editor scratch
_TEMP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}

EXTENSION_TO_TYPE: dict[str, str] = {
    ".pdf": "pdf",
    ".md": "markdown", ".markdown": "markdown", ".mdown": "markdown", ".mkd": "markdown",
    ".txt": "text", ".text": "text", ".rst": "text", ".log": "text",
    ".csv": "csv", ".tsv": "csv",
    ".docx": "docx",
    ".xlsx": "xlsx", ".xlsm": "xlsx",
    ".html": "html_file", ".htm": "html_file",
}
SUPPORTED_EXTENSIONS = frozenset(EXTENSION_TO_TYPE)


def allowed_roots(settings: Settings) -> list[Path]:
    """Configured local access roots (absolute, existing)."""
    return [root for root in settings.local_roots if root.exists()]


def _contained(resolved: Path, roots: list[Path]) -> bool:
    return any(resolved == root or root in resolved.parents for root in roots)


def resolve_allowed(path: Path | str, settings: Settings) -> Path:
    """Resolve *path* fully and enforce that it stays inside an access root.

    ``..`` traversal, symlink escapes, and accesses outside the configured
    roots all surface here as ``access_denied`` (``not_found`` for missing
    paths). Symlinks are resolved first, so a link pointing outside cannot
    borrow an innocuous in-roots location.
    """
    raw = Path(str(path)).expanduser()
    resolved = raw.resolve(strict=False)
    if not resolved.exists():
        raise ToolError("not_found", f"Local path not found: {resolved}")
    roots = allowed_roots(settings)
    if not roots:
        raise ToolError(
            "access_denied",
            "No local access roots configured; the operator must set AGLETRAS_LOCAL_ROOTS.",
        )
    if not _contained(resolved, roots):
        roots_text = ", ".join(str(root) for root in roots)
        raise ToolError(
            "access_denied",
            f"Path {resolved} is outside the permitted local roots: {roots_text}. "
            "A root must be added to AGLETRAS_LOCAL_ROOTS to allow it.",
        )
    return resolved


def is_temp_or_hidden(path: Path) -> str | None:
    """Skip reason for temp/hidden files, or ``None`` when none applies."""
    name = path.name
    if name.startswith(".") or name.endswith("~"):
        return "hidden_or_backup_file"
    lowered = name.lower()
    if lowered in _TEMP_NAMES or any(lowered.endswith(suffix) for suffix in _TEMP_SUFFIXES):
        return "temporary_file"
    if name.startswith(_TEMP_PREFIXES):
        return "temporary_or_lock_file"
    return None


def _iter_directory(
    directory: Path, roots: list[Path], *, recursive: bool, max_depth: int, current_depth: int = 0
) -> Iterator[tuple[Path, str | None]]:
    """Depth-bounded walk. Yields ``(path, skip_reason|None)`` entries.

    Symlinked directories are never followed across a root boundary; they
    yield one skip entry with the reason instead. Regular files yield with no
    reason (later checks may still skip them).
    """
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except (PermissionError, OSError) as exc:
        raise ToolError("access_denied", f"Directory not readable: {directory}") from exc
    for entry in entries:
        path = Path(entry.path)
        if entry.is_dir(follow_symlinks=False):
            if not recursive or current_depth >= max_depth:
                continue
            if entry.is_symlink():
                resolved = Path(os.path.realpath(path))
                if not _contained(resolved, roots):
                    yield path, "symlink_outside_roots"
                    continue
            yield from _iter_directory(
                path, roots, recursive=recursive, max_depth=max_depth, current_depth=current_depth + 1
            )
        else:
            yield path, None


def scan_folder(
    folder: Path | str,
    settings: Settings,
    *,
    recursive: bool = True,
    extensions: tuple[str, ...] = (),
    max_files: int | None = None,
) -> dict[str, Any]:
    """List supported files under *folder*, with skip reasons for everything else.

    Returns ``{"picked": [Path...], "skipped": [{"path", "reason"}...],
    "truncated": bool}``. Caps: ``AGLETRAS_FOLDER_MAX_FILES`` (per call unless
    ``max_files`` is smaller), ``AGLETRAS_FOLDER_MAX_DEPTH``, and
    ``AGLETRAS_FOLDER_MAX_FILE_BYTES``.
    """
    directory = Path(folder)
    raw_extensions = [e.strip().lower() for e in extensions if e.strip()]
    normalized: set[str] = set()
    for ext in raw_extensions:
        normalized.add(ext if ext.startswith(".") else f".{ext}")
    allowed_exts = frozenset(normalized) or SUPPORTED_EXTENSIONS
    limit = settings.folder_max_files if max_files is None else min(max_files, settings.folder_max_files)
    roots = [directory, *allowed_roots(settings)]

    picked: list[Path] = []
    skipped: list[dict[str, str]] = []
    truncated = False

    for path, walk_skip in _iter_directory(
        directory, roots, recursive=recursive, max_depth=settings.folder_max_depth
    ):
        if walk_skip:
            skipped.append({"path": str(path), "reason": walk_skip})
            continue
        if path.is_symlink():
            resolved = Path(os.path.realpath(path))
            if not _contained(resolved, roots):
                skipped.append({"path": str(path), "reason": "symlink_outside_roots"})
                continue
        reason = is_temp_or_hidden(path)
        if reason:
            skipped.append({"path": str(path), "reason": reason})
            continue
        extension = path.suffix.lower()
        if extension not in allowed_exts:
            skipped.append({"path": str(path), "reason": "unsupported_extension"})
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            skipped.append({"path": str(path), "reason": f"unreadable: {exc.__class__.__name__}"})
            continue
        if size > settings.folder_max_file_bytes:
            skipped.append({"path": str(path), "reason": "too_large"})
            continue
        if len(picked) >= limit:
            truncated = True
            continue
        picked.append(path)
    return {"picked": picked, "skipped": skipped, "truncated": truncated}

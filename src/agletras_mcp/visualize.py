"""Phase 8: evidence visualizations and data exports.

Everything rendered here is derived exclusively from stored runs, sources,
claims, citations, and passages. The module never invents nodes, edges,
timestamps, counts, or labels: missing values are omitted (or rendered as an
explicit "not recorded" note) instead of being guessed.

Three artifact families are produced:

* :func:`render_evidence_map` -- a Mermaid flowchart where nodes are the
  run's sources and claims and edges are the stored citation relations
  (supports / partially_supports / contradicts / context / needs_review).
* :func:`render_timeline` -- a Mermaid timeline of recorded run and source
  timestamps (retrieval times; publication dates are shown as annotations
  only when present).
* CSV exports (sources / claims / citations) with full provenance columns.

:func:`export_visuals` writes the artifacts into ``reports/visuals/run-<id>/``
(or an explicit directory) and reports back the written files and counts.
Exports are derived artifacts: they are overwritten on re-export, unlike
research reports, which are append-only evidence.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any, Sequence

from . import evidence
from .config import Settings, load_settings

#: Maximum characters of free text kept in one Mermaid label.
MAX_LABEL_CHARS = 72

#: Relative directory (inside ``reports_dir``) used for default exports.
VISUALS_DIRNAME = "visuals"

_FORMAT_FILES = {
    "mermaid": ("evidence-map.mmd", "timeline.mmd"),
    "csv": ("sources.csv", "claims.csv", "citations.csv"),
}
FORMATS = ("all", "mermaid", "csv")

_WS_RE = re.compile(r"\s+")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")

_RELATION_ARROWS = {
    "supports": "-->|supports|",
    "partially_supports": "-->|partially supports|",
    "contradicts": "-.->|contradicts|",
    "context": "-.->|context|",
    "needs_review": "-.->|needs review|",
}
_RELATION_FALLBACK = "-.->|relation not recorded|"


def _text(value: Any) -> str:
    """Single-line plain text: no control characters, no runs of whitespace."""
    if value is None:
        return ""
    cleaned = _WS_RE.sub(" ", str(value)).strip()
    return "".join(ch for ch in cleaned if ch.isprintable())


def _clip(text: str, *, max_chars: int = MAX_LABEL_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _mermaid_text(value: Any, *, max_chars: int = MAX_LABEL_CHARS) -> str:
    """Escape text for a quoted Mermaid node label (entity codes)."""
    text = _clip(_text(value), max_chars=max_chars)
    # '#' first: introduced entity codes must not be re-escaped.
    text = text.replace("#", "#35;")
    text = text.replace("&", "#amp;")
    text = text.replace('"', "#quot;")
    text = text.replace("<", "#lt;")
    text = text.replace(">", "#gt;")
    return text


def _timeline_text(value: Any, *, max_chars: int = MAX_LABEL_CHARS) -> str:
    """Plain text for a timeline entry; ':' is the entry separator."""
    text = _clip(_text(value), max_chars=max_chars)
    return text.replace(":", " -")


def _csv_text(value: Any) -> str:
    return _text(value)


def _domain(url: str | None) -> str:
    """Host of a stored URL, or '' -- never guessed for local paths."""
    if not url:
        return ""
    match = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#]+)", url.strip())
    if not match:
        return ""
    host = match.group(1).rsplit("@", 1)[-1].split(":", 1)[0]
    return host.lower()


def _date_part(timestamp: Any) -> str:
    """The ``YYYY-MM-DD`` part of a recorded timestamp (else sanitized text)."""
    text = _text(timestamp)
    if not text:
        return ""
    match = _DATE_RE.match(text)
    if match:
        return match.group(0)
    return _timeline_text(text, max_chars=32)


def build_dataset(run_id: int, *, settings: Settings | None = None) -> dict[str, Any]:
    """Load everything the renderers need for one run (read-only, one snapshot).

    Raises :class:`~agletras_mcp.errors.ToolError` (``not_found``) when the run
    does not exist. Claims carry their full citation provenance; sources carry
    the staleness flag computed by the evidence layer.
    """
    settings = settings or load_settings()
    history = evidence.get_research_run_history(run_id=int(run_id), settings=settings)
    claims = evidence.claims_with_citations(int(run_id), settings=settings)
    return {
        "run": history["run"],
        "sources": history.get("sources") or [],
        "claims": claims,
        "counts": history.get("counts") or {},
    }


def _edge_label(relation: Any) -> str:
    return _RELATION_ARROWS.get(str(relation or "").strip().lower(), _RELATION_FALLBACK)


def render_evidence_map(dataset: dict[str, Any]) -> str:
    """A Mermaid flowchart: sources, claims, and the recorded citation edges."""
    run = dataset.get("run") or {}
    sources = dataset.get("sources") or []
    claims = dataset.get("claims") or []
    lines = [
        "%% Evidence map: every node and edge comes from stored sources and citations.",
        "flowchart LR",
    ]
    run_id = run.get("run_id")
    query = _mermaid_text(run.get("query") or "", max_chars=96)
    lines.append(f'    RUN["Run {run_id}: {query}"]')
    if not sources and not claims:
        lines.append('    NONE["No sources or claims recorded for this run"]')
        lines.append("    RUN --- NONE")
        return "\n".join(lines) + "\n"

    source_ids: set[int] = set()
    for source in sources:
        source_id = int(source["source_id"])
        source_ids.add(source_id)
        title = _mermaid_text(source.get("title") or "(untitled source)")
        domain = _domain(source.get("url"))
        suffix = f" ({domain})" if domain else ""
        stale = " [stale]" if source.get("stale") else ""
        lines.append(f'    S{source_id}["Source {source_id}: {title}{suffix}{stale}"]')
        lines.append(f"    RUN --> S{source_id}")

    referenced: set[int] = set()
    edges: list[str] = []
    for claim in claims:
        claim_id = int(claim["claim_id"])
        status = _mermaid_text(claim.get("status") or "unknown", max_chars=32)
        text = _mermaid_text(claim.get("claim_text") or "")
        lines.append(f'    C{claim_id}["Claim {claim_id} [{status}]: {text}"]')
        for citation in claim.get("citations") or []:
            source_id = int(citation["source_id"])
            if source_id not in source_ids:
                continue  # a citation always points to a stored source; defensive
            referenced.add(source_id)
            edges.append(
                f"    S{source_id} {_edge_label(citation.get('relation'))} C{claim_id}"
            )
    lines.extend(edges)
    return "\n".join(lines) + "\n"


def render_timeline(dataset: dict[str, Any]) -> str:
    """A Mermaid timeline of recorded run and source retrieval timestamps."""
    run = dataset.get("run") or {}
    sources = dataset.get("sources") or []
    run_id = run.get("run_id")
    events: list[tuple[str, str]] = []
    run_events: list[tuple[str, str]] = []
    for key, label in (("started_at", "run started"), ("completed_at", "run completed")):
        stamp = _date_part(run.get(key))
        if stamp:
            run_events.append((stamp, label))

    def _sort_key(source: dict[str, Any]) -> tuple[str, int]:
        return (_text(source.get("retrieved_at")), int(source["source_id"]))

    for source in sorted(sources, key=_sort_key):
        stamp = _date_part(source.get("retrieved_at"))
        if not stamp:
            continue  # no recorded retrieval time: omit rather than invent
        title = _timeline_text(source.get("title") or "(untitled source)")
        published = _date_part(source.get("publication_date"))
        annotation = f" (published {published})" if published else ""
        stale = " [stale]" if source.get("stale") else ""
        events.append((stamp, f"Source {source['source_id']}: {title}{annotation}{stale}"))

    headline = _timeline_text(run.get("query") or "", max_chars=96)
    lines = ["%% Source timeline: only recorded timestamps appear here.", "timeline"]
    if run_events or events:
        lines.append(f"    title Run {run_id}: {headline}")
    if run_events:
        lines.append("    section Run")
        lines.extend(f"        {stamp} : {label}" for stamp, label in run_events)
    if events:
        lines.append("    section Sources")
        lines.extend(f"        {stamp} : {label}" for stamp, label in events)
    if not run_events and not events:
        lines.append("    title No timestamped events recorded for this run")
    return "\n".join(lines) + "\n"


def _csv_document(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(header))
    for row in rows:
        writer.writerow([_csv_text(cell) if isinstance(cell, str) else cell for cell in row])
    return buffer.getvalue()


def render_sources_csv(dataset: dict[str, Any]) -> str:
    """One row per source of the run, with provenance and citation counts."""
    cited: dict[int, int] = {}
    for claim in dataset.get("claims") or []:
        seen: set[int] = set()
        for citation in claim.get("citations") or []:
            seen.add(int(citation["source_id"]))
        for source_id in seen:
            cited[source_id] = cited.get(source_id, 0) + 1
    rows: list[list[Any]] = []
    for source in dataset.get("sources") or []:
        source_id = int(source["source_id"])
        rows.append(
            [
                source_id,
                source.get("title") or "",
                source.get("url") or "",
                source.get("canonical_url") or "",
                source.get("path") or "",
                source.get("source_type") or "",
                source.get("kind") or "",
                source.get("extraction_status") or "",
                source.get("retrieved_at") or "",
                source.get("publication_date") or "",
                source.get("content_hash") or "",
                "true" if source.get("stale") else "false",
                int(source.get("passages") or 0),
                cited.get(source_id, 0),
            ]
        )
    return _csv_document(
        (
            "source_id",
            "title",
            "url",
            "canonical_url",
            "path",
            "source_type",
            "kind",
            "extraction_status",
            "retrieved_at",
            "publication_date",
            "sha256",
            "stale",
            "passages",
            "cited_claims",
        ),
        rows,
    )


def _relation_counts(claim: dict[str, Any]) -> tuple[int, int, int]:
    support = partial = contradict = 0
    for citation in claim.get("citations") or []:
        relation = str(citation.get("relation") or "").strip().lower()
        if relation == "supports":
            support += 1
        elif relation == "partially_supports":
            partial += 1
        elif relation == "contradicts":
            contradict += 1
    return support, partial, contradict


def render_claims_csv(dataset: dict[str, Any]) -> str:
    """One row per claim, with its stored status and citation tallies."""
    rows: list[list[Any]] = []
    for claim in dataset.get("claims") or []:
        support, partial, contradict = _relation_counts(claim)
        rows.append(
            [
                int(claim["claim_id"]),
                claim.get("claim_type") or "claim",
                claim.get("status") or "",
                claim.get("verification_status") or "",
                claim.get("verified_at") or "",
                len(claim.get("citations") or []),
                support,
                partial,
                contradict,
                claim.get("created_at") or "",
                claim.get("updated_at") or "",
                claim.get("claim_text") or "",
            ]
        )
    return _csv_document(
        (
            "claim_id",
            "claim_type",
            "status",
            "verification_status",
            "verified_at",
            "citations",
            "supporting",
            "partially_supporting",
            "contradicting",
            "created_at",
            "updated_at",
            "claim_text",
        ),
        rows,
    )


def render_citations_csv(dataset: dict[str, Any]) -> str:
    """One row per stored citation with passage and source provenance."""
    rows: list[list[Any]] = []
    for claim in dataset.get("claims") or []:
        for citation in claim.get("citations") or []:
            rows.append(
                [
                    int(claim["claim_id"]),
                    int(citation["citation_id"]),
                    citation.get("relation") or "",
                    citation.get("quote") or "",
                    int(citation["source_id"]),
                    citation.get("source_title") or "",
                    citation.get("source_url") or "",
                    citation.get("source_path") or "",
                    int(citation["passage_id"]),
                    citation.get("passage_hash") or "",
                    citation.get("content_hash") or "",
                    citation.get("locator") or citation.get("passage_locator") or "",
                    citation.get("page") if citation.get("page") is not None else "",
                    citation.get("heading") or "",
                    citation.get("start_offset") if citation.get("start_offset") is not None else "",
                    citation.get("end_offset") if citation.get("end_offset") is not None else "",
                ]
            )
    return _csv_document(
        (
            "claim_id",
            "citation_id",
            "relation",
            "quote",
            "source_id",
            "source_title",
            "source_url",
            "source_path",
            "passage_id",
            "passage_hash",
            "content_hash",
            "locator",
            "page",
            "heading",
            "start_offset",
            "end_offset",
        ),
        rows,
    )


def export_visuals(
    run_id: int,
    *,
    out_dir: str | Path | None = None,
    formats: Sequence[str] = ("all",),
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Write Mermaid and/or CSV artifacts for one run; return what was written.

    Default location: ``<reports_dir>/visuals/run-<id>/``. Files are derived
    artifacts and are overwritten on re-export; nothing about the run changes.
    """
    settings = settings or load_settings()
    requested = {str(item).strip().lower() for item in formats or ("all",)}
    unknown = requested - set(FORMATS)
    if unknown:
        raise ValueError(f"unknown export format(s): {', '.join(sorted(unknown))}")
    if "all" in requested:
        requested = {"mermaid", "csv"}
    dataset = build_dataset(int(run_id), settings=settings)
    directory = (
        Path(out_dir)
        if out_dir is not None
        else settings.reports_dir / VISUALS_DIRNAME / f"run-{int(run_id)}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    builders = {
        "evidence-map.mmd": render_evidence_map,
        "timeline.mmd": render_timeline,
        "sources.csv": render_sources_csv,
        "claims.csv": render_claims_csv,
        "citations.csv": render_citations_csv,
    }
    wanted = [name for kind in sorted(requested) for name in _FORMAT_FILES[kind]]
    files: list[dict[str, Any]] = []
    for name in wanted:
        content = builders[name](dataset)
        path = directory / name
        path.write_text(content, encoding="utf-8")
        files.append({"name": name, "path": str(path), "bytes": len(content.encode("utf-8"))})
    claims = dataset.get("claims") or []
    return {
        "run_id": int(run_id),
        "query": (dataset.get("run") or {}).get("query") or "",
        "directory": str(directory),
        "formats": sorted(requested),
        "files": files,
        "counts": {
            "sources": len(dataset.get("sources") or []),
            "claims": len(claims),
            "citations": sum(len(claim.get("citations") or []) for claim in claims),
        },
    }

"""Verified synthesis reports (Phase 6).

Ties claim verification, conflict analysis, and source assessment into one
deterministic Markdown report and checks its integrity before it is handed
back:

- **Generation** (:func:`generate_markdown_report`) renders the ten-section
  Phase 6 template from stored facts only (no model-written prose). Reports
  are written inside the configured reports directory, never overwritten, and
  identical content is stored once (deduplicated by content hash).
- **Integrity validation** (:func:`validate_phase6_report`) re-checks the
  structural citations (via :func:`agletras_mcp.evidence.validate_report_citations`)
  *and* the Phase 6 invariants: every claim has a stable finding id, status
  labels are present, supported statements have citations, model assessments
  cite stored passages, conflicts and image attributions are rendered, and the
  report states what was not checked. Validation reports errors, warnings, and
  the statements that remain unresolved. It never judges truth.
- **Retrieval** (:func:`get_markdown_report`) returns one stored report with
  its validation, section list, and on-disk integrity status.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import evidence, planner, report as report_mod, storage, verification
from .config import Settings, load_settings
from .errors import ToolError

#: Sections every Phase 6 report must contain (the H1 title/metadata block is
#: section 1; image attribution is the tenth).
PHASE6_REQUIRED_SECTIONS = (
    "Executive summary",
    "Scope and methodology",
    "Key findings",
    "Evidence comparison",
    "Conflicting evidence",
    "Limitations and open questions",
    "Conclusion",
    "Sources and references",
    "Image attribution",
)

_PHASE6_TEMPLATE = report_mod.PHASE6_TEMPLATE_VERSION
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_FINDING_RE = re.compile(r"\[F(\d+)\]")
_IMAGE_RE = re.compile(r"\[IMG(\d+)\]")
_STATUS_ORDER = ("supported", "partially_supported", "conflicting_evidence", "contradicted",
                 "unsupported", "unverifiable", "unverified", "needs_review")


def _load_object(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _load_list(raw: str | None) -> list[Any]:
    try:
        parsed = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _utc_now() -> str:
    from .source_record import utc_now_iso

    return utc_now_iso()


# ---------------------------------------------------------------------------
# Collection helpers
# ---------------------------------------------------------------------------


def _run_payload(row: Any) -> dict[str, Any]:
    return {
        "run_id": int(row["id"]),
        "query": row["query"],
        "status": row["status"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }


def _run_sources(conn: Any, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.* FROM research_run_sources rrs JOIN sources s ON s.id = rrs.source_id
        WHERE rrs.run_id = ? ORDER BY rrs.added_at, s.id
        """,
        (int(run_id),),
    ).fetchall()
    return [
        {
            "source_id": int(row["id"]),
            "title": row["title"],
            "url": row["url"],
            "path": row["path"],
            "source_type": row["source_type"],
            "publication_date": row["publication_date"],
            "retrieved_at": row["retrieved_at"],
            "extraction_status": row["extraction_status"],
        }
        for row in rows
    ]


def _run_images(conn: Any, run_id: int) -> list[dict[str, Any]]:
    """Image discovery records linked to the run (attribution fields only).

    Image sources are identified by their record columns *or* the stored
    metadata (``metadata.kind == "image"``), which is where the Phase 5 image
    integration records the discovery type.
    """
    rows = conn.execute(
        """
        SELECT s.* FROM research_run_sources rrs JOIN sources s ON s.id = rrs.source_id
        WHERE rrs.run_id = ? ORDER BY s.id
        """,
        (int(run_id),),
    ).fetchall()
    images: list[dict[str, Any]] = []
    for row in rows:
        metadata = _load_object(row["metadata"])
        if (
            row["source_type"] != "image"
            and row["kind"] != "image"
            and metadata.get("kind") != "image"
            and metadata.get("source_type") != "image"
        ):
            continue
        missing = [str(item) for item in metadata.get("missing_metadata") or [] if str(item).strip()]
        if not metadata.get("creator") and "creator" not in missing:
            missing.append("creator")
        if not metadata.get("license_name") and "license" not in missing:
            missing.append("license")
        if not (metadata.get("source_page_url") or row["url"]) and "source_page" not in missing:
            missing.append("source_page")
        images.append(
            {
                "source_id": int(row["id"]),
                "title": row["title"],
                "image_url": metadata.get("image_url") or row["url"],
                "source_page_url": metadata.get("source_page_url"),
                "creator": metadata.get("creator"),
                "license_name": metadata.get("license_name"),
                "license_url": metadata.get("license_url"),
                "attribution": metadata.get("attribution"),
                "provider": metadata.get("provider"),
                "retrieved_at": row["retrieved_at"] or metadata.get("retrieved_at"),
                "width": metadata.get("width"),
                "height": metadata.get("height"),
                "mime_type": metadata.get("mime_type") or row["content_type"],
                "missing_metadata": missing,
            }
        )
    return images


def _event_counts(conn: Any, run_id: int) -> dict[str, int]:
    return {
        row["kind"]: int(row["n"])
        for row in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM research_run_events WHERE run_id = ? GROUP BY kind",
            (int(run_id),),
        ).fetchall()
    }


def _stopping_reason(conn: Any, run_id: int) -> str:
    row = conn.execute(
        """
        SELECT message FROM research_run_events
        WHERE run_id = ? AND kind = 'stopping' ORDER BY id DESC LIMIT 1
        """,
        (int(run_id),),
    ).fetchone()
    return row["message"] if row is not None else ""


# ---------------------------------------------------------------------------
# Path confinement
# ---------------------------------------------------------------------------


def _confined_path(settings: Settings, filename: str) -> Path:
    """Resolve *filename* inside the reports directory (path-traversal safe)."""
    reports_dir = Path(settings.reports_dir).expanduser().resolve()
    candidate = (reports_dir / filename).resolve()
    if candidate != reports_dir and reports_dir not in candidate.parents:
        raise ToolError(
            "invalid_argument",
            "Report paths must stay inside the configured reports directory "
            f"({reports_dir}).",
        )
    return candidate


def _unique_report_path(settings: Settings, filename: str) -> Path:
    """Never overwrite an existing report file; add a numeric suffix instead."""
    candidate = _confined_path(settings, filename)
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for counter in range(2, 1000):
        alternative = _confined_path(settings, f"{stem}-{counter}{suffix}")
        if not alternative.exists():
            return alternative
    raise ToolError(
        "storage_error",
        f"Could not find a free report filename for {filename!r}; clean up old reports first.",
    )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def generate_markdown_report(
    run_id: int,
    *,
    title: str = "",
    include_source_assessment: bool = True,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Render, store, and validate a Phase 6 verified-synthesis report.

    All content is derived from stored rows (plans, claims, citations,
    passages, verification records, conflicts, image metadata). Identical
    report content for the same run reuses the stored report instead of
    writing a duplicate file, and an existing file is never overwritten.
    """
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        run_row = conn.execute(
            "SELECT * FROM research_runs WHERE id = ?", (int(run_id),)
        ).fetchone()
        if run_row is None:
            raise ToolError(
                "not_found",
                f"No research run with id {run_id}; create one with start_research_run first.",
            )
        run = _run_payload(run_row)
        claims = evidence._claims_with_citations(conn, int(run_id))
        verifications = verification.latest_verifications_in_conn(conn, int(run_id))
        overview = planner.describe_run(conn, int(run_id))
        event_counts = _event_counts(conn, int(run_id))
        stopping_reason = _stopping_reason(conn, int(run_id))
        run_sources = _run_sources(conn, int(run_id))
        images = _run_images(conn, int(run_id))
        source_assessment = (
            verification.source_quality_in_conn(conn, int(run_id))
            if include_source_assessment
            else {}
        )
        conflicts = verification.conflicts_in_conn(conn, int(run_id))

    capability = verification.verification_capability(settings)
    display_title = (title or "").strip() or f"Research run {run_id}: {run['query']}"
    generation_config = {
        "generated_by": "agletras_phase6",
        "template_version": _PHASE6_TEMPLATE,
        "verification": capability,
        "thresholds": {
            "min_relevance": verification.MIN_RELEVANCE,
            "partial": verification.PARTIAL_THRESHOLD,
            "support": verification.SUPPORT_THRESHOLD,
        },
        "extraction_method": "deterministic_sentence_marker_scoring",
        "statement_count": len(claims),
        "source_count": len(run_sources),
        "conflict_count": conflicts["count"],
        "unresolved_conflict_count": conflicts["unresolved_count"],
        "include_source_assessment": bool(include_source_assessment),
    }
    warnings: list[str] = []
    if len(claims) > settings.report_max_findings:
        warnings.append(
            f"the run has {len(claims)} statements; the report renders all of them "
            f"(AGLETRAS_REPORT_MAX_FINDINGS={settings.report_max_findings} is a size warning, "
            "not a truncation limit)"
        )

    markdown, maps = report_mod.build_phase6_markdown(
        run,
        claims,
        verifications=verifications,
        title=display_title,
        plan=overview["plan"],
        subquestions=overview["subquestions"],
        gaps=overview["gaps"],
        budget=overview["budget"],
        stopping_reason=stopping_reason,
        methodology={
            "searches_logged": event_counts.get("search", 0),
            "retrieval_failures": event_counts.get("source_failed", 0),
        },
        run_sources=run_sources,
        source_assessment=source_assessment,
        conflicts=conflicts["conflicts"],
        images=images,
        verification_context={
            "capability": capability,
            "thresholds": generation_config["thresholds"],
            "extraction_method": generation_config["extraction_method"],
        },
    )

    content_hash = storage.content_hash(markdown)
    with storage.open_db(settings) as conn:
        existing = conn.execute(
            """
            SELECT id, path FROM reports
            WHERE run_id = ? AND content_hash = ? AND template_version = ?
            ORDER BY id LIMIT 1
            """,
            (int(run_id), content_hash, _PHASE6_TEMPLATE),
        ).fetchone()
        if existing is not None and existing["path"] and Path(existing["path"]).exists():
            prior = evidence.get_report(int(existing["id"]), settings=settings)
            return {
                "report_id": int(existing["id"]),
                "run_id": int(run_id),
                "title": (prior or {}).get("title", display_title),
                "path": existing["path"],
                "filename": Path(existing["path"]).name,
                "template_version": _PHASE6_TEMPLATE,
                "markdown": markdown,
                "finding_map": maps["finding_map"],
                "citation_map": maps["citation_map"],
                "image_map": maps["image_map"],
                "generation_config": generation_config,
                "created": False,
                "deduplicated": True,
                "warnings": warnings,
                "validation": (prior or {}).get("validation", {}),
                "validation_status": (prior or {}).get("citation_validation_status", "unvalidated"),
                "claims_rendered": len(claims),
                "counts_by_status": _count_statuses(claims, verifications),
                "unresolved_conflicts": conflicts["unresolved_count"],
            }

        payload = evidence.save_report_metadata_in_conn(
            conn,
            int(run_id),
            title=display_title,
            path="",
            content=markdown,
            citation_map=maps["citation_map"],
            finding_map=maps["finding_map"],
            image_map=maps["image_map"],
            generation_config=generation_config,
            template_version=_PHASE6_TEMPLATE,
        )
        report_id = int(payload["report_id"])

    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"report-run{run_id}-{report_mod.slugify(display_title)}-{stamp}.md"
    path = _unique_report_path(settings, filename)
    try:
        path.write_text(markdown, encoding="utf-8")
    except OSError as exc:
        raise ToolError("storage_error", f"Could not write report file: {exc}") from exc

    with storage.open_db(settings) as conn:
        conn.execute("UPDATE reports SET path = ? WHERE id = ?", (str(path), report_id))
    planner.log_event(
        int(run_id),
        "report",
        f"Phase 6 report {report_id} generated ({len(claims)} statement(s), template {_PHASE6_TEMPLATE})",
        payload={"report_id": report_id, "template": _PHASE6_TEMPLATE},
        settings=settings,
    )
    validation = validate_phase6_report(report_id, settings=settings)
    stored = evidence.get_report(report_id, settings=settings) or payload
    return {
        "report_id": report_id,
        "run_id": int(run_id),
        "title": stored["title"],
        "path": str(path),
        "filename": path.name,
        "template_version": _PHASE6_TEMPLATE,
        "markdown": markdown,
        "finding_map": maps["finding_map"],
        "citation_map": maps["citation_map"],
        "image_map": maps["image_map"],
        "generation_config": generation_config,
        "created": True,
        "deduplicated": False,
        "warnings": warnings + list(validation.get("warnings") or []),
        "validation": validation,
        "validation_status": validation["status"],
        "claims_rendered": len(claims),
        "counts_by_status": _count_statuses(claims, verifications),
        "unresolved_conflicts": conflicts["unresolved_count"],
    }


def _count_statuses(
    claims: list[dict[str, Any]], verifications: dict[int, dict[str, Any]]
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for claim in claims:
        record = verifications.get(int(claim["claim_id"]))
        status = (
            str(record["status"])
            if record is not None and record.get("status")
            else str(claim.get("verification_status") or claim.get("status") or "unverified")
        )
        counts[status] = counts.get(status, 0) + 1
    return {status: counts[status] for status in sorted(counts, key=_status_rank)}


def _status_rank(status: str) -> int:
    rank = verification.VERIFICATION_RANK.get(status)
    return rank if rank is not None else 99


# ---------------------------------------------------------------------------
# Extended integrity validation
# ---------------------------------------------------------------------------


def validate_phase6_report(
    report_id: int, *, settings: Settings | None = None
) -> dict[str, Any]:
    """Validate the Phase 6 invariants on top of structural citation checks.

    Runs :func:`evidence.validate_report_citations` first (identifiers,
    passage hashes/offsets, quotes, provenance, foreign keys), then checks the
    synthesis-specific guarantees: required sections, stable finding ids,
    verification records and methods, supported statements having citations,
    model assessments referencing stored passages, conflict rendering, image
    attribution, and the explicit "no liveness check" statement. Produces an
    ``errors`` / ``warnings`` / ``unresolved`` summary; it never judges truth.
    """
    settings = settings or load_settings()
    report_row = evidence.get_report(report_id, settings=settings)
    if report_row is None:
        raise ToolError("not_found", f"No report with id {report_id}")
    if report_row.get("template_version") != _PHASE6_TEMPLATE:
        base = evidence.validate_report_citations(report_id, settings=settings)
        return {
            **base,
            "validation_kind": "structural",
            "template_version": report_row.get("template_version") or "phase3",
            "phase6_checks": False,
            "note": (
                "This report uses an earlier template; only structural citation "
                "validation was run. Generate a Phase 6 report for the extended checks."
            ),
        }

    base = evidence.validate_report_citations(report_id, settings=settings)
    errors: list[dict[str, Any]] = list(base.get("errors") or [])
    warnings: list[str] = list(base.get("warnings") or [])
    checks: dict[str, Any] = {"structural": base.get("checked") or {}}

    with storage.open_db(settings) as conn:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (int(report_id),)).fetchone()
        if row is None:  # pragma: no cover - deleted between reads
            raise ToolError("not_found", f"No report with id {report_id}")
        run_id = int(row["run_id"])
        markdown = row["content"] or ""
        finding_map = _load_object(row["finding_map"])
        image_map = _load_object(row["image_map"])
        generation_config = _load_object(row["generation_config"])
        claims = evidence._claims_with_citations(conn, run_id)
        verifications = verification.latest_verifications_in_conn(conn, run_id)
        conflicts = verification.conflicts_in_conn(conn, run_id)

        sections = _SECTION_RE.findall(markdown)
        checks["sections"] = sections
        missing_sections = [
            section for section in PHASE6_REQUIRED_SECTIONS if section not in sections
        ]
        checks["missing_sections"] = missing_sections
        for section in missing_sections:
            errors.append(
                {
                    "code": "missing_section",
                    "message": f"Required section '## {section}' is missing from the report.",
                }
            )

        # --- Finding identifiers ------------------------------------------
        claim_by_id = {int(claim["claim_id"]): claim for claim in claims}
        mapped_ids = {int(value) for value in finding_map.values()}
        checks["finding_map_size"] = len(finding_map)
        missing_labels = [
            claim_id for claim_id in claim_by_id if claim_id not in mapped_ids
        ]
        if missing_labels:
            errors.append(
                {
                    "code": "finding_map_incomplete",
                    "message": "Claims without a stable finding id: "
                    + ", ".join(str(item) for item in sorted(missing_labels)),
                }
            )
        for label, claim_id in finding_map.items():
            if int(claim_id) not in claim_by_id:
                errors.append(
                    {
                        "code": "finding_map_mismatch",
                        "message": f"[{label}] maps to claim {claim_id}, which is not in this run.",
                    }
                )
            elif not re.search(rf"(?<![A-Za-z0-9]){re.escape(label)}(?![0-9])", markdown):
                errors.append(
                    {
                        "code": "finding_identifier_not_rendered",
                        "message": f"[{label}] is registered but does not appear in the report.",
                    }
                )
        for number in sorted({int(item) for item in _FINDING_RE.findall(markdown)}):
            if f"F{number}" not in finding_map:
                errors.append(
                    {
                        "code": "unresolved_finding_identifier",
                        "message": f"[F{number}] appears in the report but resolves to no claim.",
                    }
                )

        # --- Verification records -----------------------------------------
        status_counts: dict[str, int] = {}
        missing_records: list[int] = []
        invalid_methods: list[int] = []
        stale_caches: list[int] = []
        model_without_evidence: list[int] = []
        supported_without_citation: list[int] = []
        for claim in claims:
            claim_id = int(claim["claim_id"])
            record = verifications.get(claim_id)
            effective = (
                str(record["status"])
                if record is not None and record.get("status")
                else str(claim.get("verification_status") or claim.get("status") or "unverified")
            )
            status_counts[effective] = status_counts.get(effective, 0) + 1
            if claim.get("verification_status") and record is None:
                missing_records.append(claim_id)
            if record is not None:
                if str(record.get("status")) not in verification.VERIFICATION_STATUSES:
                    errors.append(
                        {
                            "code": "invalid_verification_status",
                            "message": f"Claim {claim_id} has verification status "
                            f"{record.get('status')!r}, which is not documented.",
                        }
                    )
                if str(record.get("method")) not in verification.VERIFICATION_METHODS:
                    invalid_methods.append(claim_id)
                if claim.get("verification_status") and claim["verification_status"] != record["status"]:
                    stale_caches.append(claim_id)
                model_block = (record.get("checks") or {}).get("model") or {}
                assessment = model_block.get("assessment") if isinstance(model_block, dict) else None
                if record.get("method") == "model":
                    ids = (assessment or {}).get("evidence_passage_ids") or []
                    if not ids:
                        model_without_evidence.append(claim_id)
                    for passage_id in ids:
                        passage = conn.execute(
                            """
                            SELECT p.id FROM passages p
                            JOIN research_run_sources rrs ON rrs.source_id = p.source_id
                            WHERE p.id = ? AND rrs.run_id = ?
                            """,
                            (int(passage_id), run_id),
                        ).fetchone()
                        if passage is None:
                            model_without_evidence.append(claim_id)
                            break
            if effective in ("supported", "partially_supported"):
                has_support = any(
                    citation.get("relation") in ("supports", "partially_supports")
                    for citation in claim.get("citations", [])
                )
                if not has_support:
                    supported_without_citation.append(claim_id)
                label = verification.VERIFICATION_LABELS.get(effective, effective.upper())
                if label not in markdown:
                    errors.append(
                        {
                            "code": "verification_label_missing",
                            "message": f"Claim {claim_id} is {effective} but the report never "
                            f"shows the label {label!r}.",
                        }
                    )
            if str(claim.get("claim_type") or "") == "inference" and not str(
                claim.get("note") or ""
            ).strip():
                warnings.append(
                    f"claim {claim_id} is an inference without recorded reasoning; the report "
                    "marks the reasoning as not recorded"
                )
        checks["counts_by_status"] = status_counts
        checks["verification_records"] = len(verifications)
        for claim_id in missing_records:
            errors.append(
                {
                    "code": "verification_record_missing",
                    "message": f"Claim {claim_id} has a cached verification status but no audit record.",
                }
            )
        if invalid_methods:
            errors.append(
                {
                    "code": "invalid_verification_method",
                    "message": "Undocumented verification method(s) on claim(s): "
                    + ", ".join(str(item) for item in invalid_methods),
                }
            )
        if stale_caches:
            warnings.append(
                "cached verification status differs from the latest audit row on claim(s): "
                + ", ".join(str(item) for item in stale_caches)
            )
        if model_without_evidence:
            errors.append(
                {
                    "code": "model_assessment_without_stored_evidence",
                    "message": "Model-based verification on claim(s) "
                    + ", ".join(str(item) for item in sorted(set(model_without_evidence)))
                    + " does not reference stored passages of this run.",
                }
            )
        if supported_without_citation:
            warnings.append(
                "supported/partially supported statement(s) without a rendered supporting citation: "
                + ", ".join(str(item) for item in supported_without_citation)
                + " (run verify_claims with apply_citations=true to attach them)"
            )

        # --- Conflicts rendered -------------------------------------------
        conflicted = [
            claim for claim in claims
            if any(citation.get("relation") == "contradicts" for citation in claim.get("citations", []))
            or str(claim.get("verification_status")) in ("contradicted", "conflicting_evidence")
        ]
        checks["conflicted_claims"] = [int(claim["claim_id"]) for claim in conflicted]
        conflict_index = markdown.find("## Conflicting evidence")
        if conflicted:
            if conflict_index < 0:
                errors.append(
                    {
                        "code": "conflicts_not_rendered",
                        "message": "The run contains contradicted/conflicting claims but the report "
                        "has no 'Conflicting evidence' section.",
                    }
                )
            else:
                for claim in conflicted:
                    if markdown.find(str(claim["claim_text"]), conflict_index) < 0:
                        errors.append(
                            {
                                "code": "conflict_claim_not_rendered",
                                "message": f"Claim {claim['claim_id']} is contested but is not shown "
                                "in the conflicting-evidence section.",
                            }
                        )
        checks["conflict_count"] = conflicts["count"]
        checks["unresolved_conflict_count"] = conflicts["unresolved_count"]

        # --- Images -------------------------------------------------------
        checks["images_mapped"] = len(image_map)
        for label, source_id in image_map.items():
            source = conn.execute(
                """
                SELECT s.* FROM sources s
                JOIN research_run_sources rrs ON rrs.source_id = s.id
                WHERE s.id = ? AND rrs.run_id = ?
                """,
                (int(source_id), run_id),
            ).fetchone()
            if source is None:
                errors.append(
                    {
                        "code": "image_map_mismatch",
                        "message": f"[{label}] maps to source {source_id}, which is not linked to "
                        "this run.",
                    }
                )
                continue
            if not re.search(rf"(?<![A-Za-z0-9]){re.escape(label)}(?![0-9])", markdown):
                errors.append(
                    {
                        "code": "image_identifier_not_rendered",
                        "message": f"[{label}] is registered but the image attribution is missing.",
                    }
                )
            metadata = _load_object(source["metadata"])
            for field, code, label_text in (
                ("creator", "image_creator_missing", "creator"),
                ("license_name", "image_license_missing", "license"),
            ):
                if not metadata.get(field):
                    warnings.append(
                        f"[{label}] image source {source_id} has no {label_text} metadata; the "
                        "report states it as not recorded."
                    )
            if not (metadata.get("source_page_url") or source["url"]):
                warnings.append(
                    f"[{label}] image source {source_id} has no source page URL recorded."
                )
        for number in sorted({int(item) for item in _IMAGE_RE.findall(markdown)}):
            if f"IMG{number}" not in image_map:
                errors.append(
                    {
                        "code": "unresolved_image_identifier",
                        "message": f"[IMG{number}] appears in the report but resolves to no image record.",
                    }
                )

        # --- Method transparency ------------------------------------------
        if "no liveness check was performed" not in markdown.lower():
            errors.append(
                {
                    "code": "liveness_disclaimer_missing",
                    "message": "The report does not state that URL liveness was not checked.",
                }
            )
        if not generation_config.get("verification"):
            warnings.append(
                "generation config does not record the verification capability; method "
                "transparency may be incomplete"
            )
        checks["generation_config"] = generation_config

        unresolved = {
            "claims": [
                {
                    "claim_id": int(claim["claim_id"]),
                    "status": (
                        str(verifications[int(claim["claim_id"])]["status"])
                        if int(claim["claim_id"]) in verifications
                        else str(claim.get("verification_status") or claim.get("status") or "unverified")
                    ),
                    "claim_text": claim["claim_text"],
                }
                for claim in claims
                if (
                    (
                        str(verifications[int(claim["claim_id"])]["status"])
                        if int(claim["claim_id"]) in verifications
                        else str(claim.get("verification_status") or claim.get("status") or "unverified")
                    )
                    in ("unsupported", "unverifiable", "unverified", "needs_review",
                        "contradicted", "conflicting_evidence")
                )
            ],
            "unresolved_conflicts": int(conflicts["unresolved_count"]),
            "open_questions": [
                {
                    "claim_id": int(claim["claim_id"]),
                    "question": claim["claim_text"],
                }
                for claim in claims
                if str(claim.get("claim_type") or "") == "question"
            ],
        }

        passed = not errors
        status = "validated" if passed else "failed"
        validated_at = _utc_now()
        validation = {
            "validation_kind": "structural+phase6_integrity",
            "template_version": _PHASE6_TEMPLATE,
            "semantic_support": "not_evaluated",
            "semantic_note": evidence._SEMANTIC_NOTE,
            "passed": passed,
            "status": status,
            "phase6_checks": True,
            "checked": checks,
            "errors": errors,
            "warnings": warnings,
            "unresolved": unresolved,
            "validated_at": validated_at,
        }
        conn.execute(
            "UPDATE reports SET citation_validation_status = ?, validation = ?, validated_at = ? "
            "WHERE id = ?",
            (status, json.dumps(validation, ensure_ascii=False, sort_keys=True, default=str),
             validated_at, int(report_id)),
        )

    return {
        "report_id": int(report_id),
        "run_id": run_id,
        "path": report_row.get("path"),
        "template_version": _PHASE6_TEMPLATE,
        **validation,
    }


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def get_markdown_report(
    report_id: int,
    *,
    include_content: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """One stored report with validation, sections, and on-disk status."""
    settings = settings or load_settings()
    stored = evidence.get_report(report_id, settings=settings)
    if stored is None:
        raise ToolError("not_found", f"No report with id {report_id}")
    with storage.open_db(settings) as conn:
        row = conn.execute(
            "SELECT content, content_hash FROM reports WHERE id = ?", (int(report_id),)
        ).fetchone()
    content = row["content"] if row is not None else ""
    stored_hash = row["content_hash"] if row is not None else ""
    path = Path(stored.get("path") or "")
    file_exists = bool(stored.get("path")) and path.exists()
    file_hash_matches = False
    if file_exists:
        try:
            file_hash_matches = storage.content_hash(path.read_text(encoding="utf-8")) == stored_hash
        except OSError:
            file_exists = False
    payload: dict[str, Any] = {
        **stored,
        "sections": _SECTION_RE.findall(content or ""),
        "character_count": len(content or ""),
        "line_count": (content or "").count("\n") + 1 if content else 0,
        "file_exists": file_exists,
        "file_hash_matches": file_hash_matches,
        "finding_map": stored.get("finding_map") or {},
        "citation_map": stored.get("citation_map") or {},
        "image_map": stored.get("image_map") or {},
    }
    if include_content:
        payload["content"] = content
    return payload

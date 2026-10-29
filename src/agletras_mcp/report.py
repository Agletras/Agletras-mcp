"""Markdown research report generation from stored sources."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from . import storage
from .config import Settings, load_settings
from .errors import ToolError

LOGGER = logging.getLogger(__name__)


def slugify(title: str) -> str:
    """Filesystem-safe slug for report filenames."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return slug[:60].strip("-") or "report"


# ---------------------------------------------------------------------------
# Phase 3: claims + citations report (stable [Sn] identifiers)
# ---------------------------------------------------------------------------

_RELATION_MARKERS = {
    "partially_supports": " (partial)",
    "contradicts": " (contradicts)",
    "context": " (context)",
    "needs_review": " (needs review)",
}

_CONTESTED_STATUSES = ("unverified", "needs_review", "contradicted")

_LIMIT_LABELS = {
    "max_subquestions": "subquestions",
    "max_followup_rounds": "follow-up rounds",
    "max_candidates_per_subquestion": "candidate sources per subquestion per round",
    "max_unique_sources": "unique sources",
}


def _format_limits(limits: dict[str, Any]) -> str:
    parts = [
        f"{int(value)} {_LIMIT_LABELS.get(key, key)}"
        for key, value in sorted(limits.items())
        if isinstance(value, int)
    ]
    return ", ".join(parts)


def _short(text: str, limit: int = 160) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "\u2026"


def _provenance_line(source: dict[str, Any], locator: str) -> str:
    location = source.get("source_url") or source.get("source_path") or "(no URL/path recorded)"
    details: list[str] = []
    if source.get("source_publication_date"):
        details.append(f"published {str(source['source_publication_date'])[:10]}")
    if source.get("source_retrieved_at"):
        details.append(f"retrieved {str(source['source_retrieved_at'])[:10]}")
    if locator:
        details.append(f"locator: {locator}")
    suffix = f" ({'; '.join(details)})" if details else ""
    return f"{location}{suffix}"


def build_evidence_markdown(
    run: dict[str, Any],
    claims: list[dict[str, Any]],
    *,
    title: str = "",
    generated_at: datetime | None = None,
    plan: dict[str, Any] | None = None,
    subquestions: list[dict[str, Any]] | None = None,
    gaps: list[dict[str, Any]] | None = None,
    budget: dict[str, Any] | None = None,
    stopping: dict[str, Any] | None = None,
    stopping_reason: str = "",
    methodology: dict[str, Any] | None = None,
    run_sources: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, int]]:
    """Render a claims + citations report with stable ``[Sn]`` identifiers.

    Returns ``(markdown, citation_map)`` where ``citation_map`` maps each
    rendered ``S<n>`` label to its citation database id. Unverified,
    needs-review, and contradicted claims are marked inline and listed in a
    dedicated section — uncertainty is always visible.

    The optional Phase 4 context parameters (all stored facts from the run's
    plan, budget, subquestions, and source links — never model-generated
    prose) add the research question/scope, methodology, unanswered
    questions, and the reviewed-source list. Omitting them renders exactly
    the Phase 3 report.
    """
    stamp = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    heading = (title or "").strip() or f"Research run {run['run_id']}: {run['query']}"
    citation_map: dict[str, int] = {}
    lines: list[str] = [
        f"# {heading}",
        "",
        f"*Generated {stamp} by agletras-mcp · research run `{run['run_id']}` · query: “{run['query']}”*",
        "",
    ]

    # --- Phase 4: research question, scope, and recorded assumptions -------
    if plan:
        lines.extend(["## Research question and scope", ""])
        lines.append(f"- **Question:** {run['query']}")
        objective = str(plan.get("objective") or "").strip()
        if objective:
            lines.append(f"- **Objective:** {objective}")
        scope = str(plan.get("scope") or "").strip()
        if scope:
            lines.append(f"- **Scope:** {scope}")
        assumptions = [str(item) for item in plan.get("assumptions") or [] if str(item).strip()]
        if assumptions:
            lines.append("- **Recorded assumptions:** " + "; ".join(assumptions))
        limits = plan.get("limits") if isinstance(plan.get("limits"), dict) else {}
        if limits:
            lines.append(f"- **Run limits:** {_format_limits(limits)}")
        lines.append("")

    if methodology:
        lines.extend(["## Methodology", ""])
        collected = methodology.get("sources_collected")
        if collected is not None:
            lines.append(f"- Sources collected and stored for this run: {collected}")
        searches = int(methodology.get("searches_logged") or 0)
        if searches:
            lines.append(f"- Web searches logged in the run history: {searches}")
        max_rounds = int(methodology.get("max_followup_rounds") or 0)
        if max_rounds:
            used = int(methodology.get("followup_rounds_used") or 0)
            lines.append(f"- Follow-up rounds used: {used} of {max_rounds}")
        failures = int(methodology.get("retrieval_failures") or 0)
        if failures:
            lines.append(f"- Retrieval failures logged: {failures}")
        claims_registered = methodology.get("claims_registered")
        if claims_registered is not None:
            lines.append(
                f"- Claims registered: {claims_registered} with "
                f"{int(methodology.get('citations_attached') or 0)} citation(s) attached"
            )
        if stopping_reason:
            lines.append(f"- Research stopped because: {stopping_reason}")
        lines.append("")

    lines.extend(["## Claims", ""])
    if not claims:
        lines.append("*No claims are registered for this research run.*")
        lines.append("")
    for index, claim in enumerate(claims, start=1):
        parts = [f"{index}. {claim['claim_text']}"]
        for citation in claim["citations"]:
            label = f"S{len(citation_map) + 1}"
            citation_map[label] = int(citation["citation_id"])
            parts.append(f" [{label}]")
            if citation.get("quote"):
                parts.append(f" “{_short(citation['quote'])}”")
            marker = _RELATION_MARKERS.get(citation.get("relation", ""), "")
            if marker:
                parts.append(marker)
        status = claim.get("status") or "unverified"
        if status == "unverified":
            reason = (
                "no supporting evidence attached"
                if not claim["citations"]
                else "no citation establishes support"
            )
            parts.append(f" — **UNVERIFIED** ({reason})")
        elif status == "partially_supported":
            parts.append(" — **PARTIALLY SUPPORTED**")
        elif status == "contradicted":
            parts.append(" — **CONTRADICTED by evidence**")
        elif status == "needs_review":
            parts.append(" — **NEEDS REVIEW**")
        lines.append("".join(parts))
    lines.append("")

    contested = [claim for claim in claims if claim.get("status") in _CONTESTED_STATUSES]
    if contested:
        lines.extend(["## Claims without established support", ""])
        for claim in contested:
            if claim["status"] == "unverified":
                reason = (
                    "no supporting evidence attached"
                    if not claim["citations"]
                    else "attached evidence does not establish support"
                )
            elif claim["status"] == "needs_review":
                reason = "attached evidence requires review"
            else:
                reason = "contradicted by attached evidence"
            lines.append(f"- {claim['claim_text']} — {claim['status'].upper()}: {reason}")
        lines.append("")

    if gaps:
        lines.extend(["## Unanswered questions", ""])
        for gap in gaps:
            note = str(gap.get("note") or "").strip()
            entry = f"- {gap.get('question', '(unnamed)')} — **{str(gap.get('status', 'open')).upper()}**"
            if note:
                entry += f": {note}"
            lines.append(entry)
        lines.append("")

    if run_sources:
        lines.extend(["## Sources reviewed", ""])
        for source in run_sources:
            location = (
                source.get("url") or source.get("path") or "(no URL/path recorded)"
            )
            details: list[str] = []
            if source.get("publication_date"):
                details.append(f"published {str(source['publication_date'])[:10]}")
            if source.get("retrieved_at"):
                details.append(f"retrieved {str(source['retrieved_at'])[:10]}")
            if source.get("source_type"):
                details.append(str(source["source_type"]))
            suffix = f" ({'; '.join(details)})" if details else ""
            lines.append(
                f"- **{source.get('title') or '(untitled source)'}** — {location}{suffix}"
            )
        lines.append("")

    lines.extend(["## References", ""])
    if not citation_map:
        lines.append("*No citations are attached to this report.*")
    by_id = {
        int(citation["citation_id"]): (claim, citation)
        for claim in claims
        for citation in claim["citations"]
    }
    for label, citation_id in citation_map.items():
        _claim, citation = by_id[citation_id]
        locator = citation.get("passage_locator") or citation.get("locator") or ""
        source = {
            "source_url": citation.get("source_url"),
            "source_path": citation.get("source_path"),
            "source_retrieved_at": citation.get("source_retrieved_at"),
            "source_publication_date": citation.get("source_publication_date"),
        }
        source_title = citation.get("source_title") or "(untitled source)"
        lines.append(f"- **[{label}]** {source_title} — {_provenance_line(source, locator)}")
    lines.append("")
    return "\n".join(lines), citation_map


# ---------------------------------------------------------------------------
# Phase 6: verified synthesis report (10-section template, stable identifiers)
# ---------------------------------------------------------------------------

PHASE6_TEMPLATE_VERSION = "phase6"

_CLAIM_TYPE_LABELS = {
    "claim": "CLAIM",
    "finding": "FINDING",
    "inference": "INFERENCE",
    "unverified": "UNVERIFIED STATEMENT",
    "question": "OPEN QUESTION",
}

_METHOD_LABELS = {
    "source_text_comparison": "deterministic claim-vs-passage comparison",
    "deterministic_checks": "deterministic structural/citation checks",
    "model": "configured verification model (structured, passage-cited)",
    "model_fallback_deterministic": "deterministic fallback (model unavailable or result rejected)",
}

_EXTRACTION_METHOD = (
    "deterministic sentence splitting and marker scoring over the stored source texts; "
    "extraction proposes candidate statements and never asserts support"
)

_NOT_PROOF = (
    "Statuses are assessments over the stored passages, not proof of truth; absence of a "
    "contradiction is not evidence of correctness."
)


def _claim_type_label(claim_type: str | None) -> str:
    key = (claim_type or "claim").strip().lower()
    return _CLAIM_TYPE_LABELS.get(key, key.upper() or "CLAIM")


def _method_label(method: str | None) -> str:
    clean = (method or "").strip()
    return _METHOD_LABELS.get(clean, clean or "unspecified")


def _status_label(status: str | None) -> str:
    from . import verification as verification_mod

    clean = (status or "").strip()
    return verification_mod.VERIFICATION_LABELS.get(clean, clean.upper() or "UNKNOWN")


def _pipes(text: str, limit: int = 140) -> str:
    return _short(text, limit).replace("|", "\\|")


def _status_key(claim: dict[str, Any], verification: dict[str, Any] | None) -> str:
    """Effective verification status for one claim (record first, then cache/derived)."""
    if verification is not None and verification.get("status"):
        return str(verification["status"])
    cached = str(claim.get("verification_status") or "").strip()
    if cached:
        return cached
    return str(claim.get("status") or "unverified")


def _status_display(claim: dict[str, Any], verification: dict[str, Any] | None) -> str:
    """Human label that always states *where* the status comes from."""
    if verification is not None and verification.get("status"):
        return _status_label(verification["status"])
    cached = str(claim.get("verification_status") or "").strip()
    if cached:
        return _status_label(cached) + " (cached on the claim; audit row missing)"
    derived = str(claim.get("status") or "unverified")
    if derived in _CONTESTED_STATUSES or derived in ("supported", "partially_supported"):
        return _status_label(derived) + " (citation relations; no verification record)"
    return "UNVERIFIED"


def _phase6_identifiers(
    claims: list[dict[str, Any]], images: list[dict[str, Any]]
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Stable ``[F#]``/``[S#]``/``[IMG#]`` maps, assigned in database-id order."""
    finding_map = {
        f"F{position}": int(claim["claim_id"])
        for position, claim in enumerate(claims, start=1)
    }
    citation_map: dict[str, int] = {}
    for claim in claims:
        for citation in claim.get("citations", []):
            label = f"S{len(citation_map) + 1}"
            citation_map[label] = int(citation["citation_id"])
    image_map = {
        f"IMG{position}": int(image["source_id"])
        for position, image in enumerate(
            sorted(images, key=lambda item: int(item["source_id"])), start=1
        )
    }
    return finding_map, citation_map, image_map


def _citation_labels(
    claim: dict[str, Any], label_by_citation: dict[int, str]
) -> list[tuple[str, dict[str, Any]]]:
    pairs: list[tuple[str, dict[str, Any]]] = []
    for citation in claim.get("citations", []):
        label = label_by_citation.get(int(citation["citation_id"]))
        if label:
            pairs.append((label, citation))
    return pairs


def _render_claim_entry(
    claim: dict[str, Any],
    verification: dict[str, Any] | None,
    *,
    finding_label: str,
    label_by_citation: dict[int, str],
    show_evidence: bool = True,
    extra_limitations: list[str] | None = None,
) -> list[str]:
    """One statement block (H4) with its verification record and evidence."""
    status_label = _status_display(claim, verification)
    lines = [
        f"#### {finding_label} — {_claim_type_label(claim.get('claim_type'))} · {status_label}",
        "",
    ]
    pairs = _citation_labels(claim, label_by_citation)
    markers = " ".join(f"[{label}]" for label, _citation in pairs)
    statement = str(claim.get("claim_text") or "")
    if markers:
        statement = f"{statement} {markers}"
    lines.append(f"**Statement:** {statement}")
    lines.append("")
    if verification is not None:
        details = [
            f"method: {_method_label(verification.get('method'))}",
            f"confidence: {verification.get('confidence') or 'low'}",
            f"recorded: {str(verification.get('created_at') or '')[:19] or 'unknown'}",
        ]
        lines.append(f"- **Verification:** {status_label} · " + " · ".join(details))
        rationale = str(verification.get("rationale") or "").strip()
        if rationale:
            lines.append(f"- Recorded rationale: {rationale}")
    else:
        lines.append(
            "- **Verification record:** none — the status above is derived from the attached "
            "citation relations only."
        )
    note = str(claim.get("note") or "").strip()
    if str(claim.get("claim_type") or "") == "inference":
        if note:
            lines.append(f"- Stated reasoning: {note}")
        else:
            lines.append(
                "- Stated reasoning: **not recorded** — treat this inference as unsupported reasoning."
            )
    elif note:
        lines.append(f"- Note: {note}")
    limitations = [str(item) for item in (verification or {}).get("limitations") or [] if str(item).strip()]
    for item in extra_limitations or []:
        if item not in limitations:
            limitations.append(str(item))
    if limitations:
        lines.append("- Limitations: " + "; ".join(limitations))
    if show_evidence:
        if pairs:
            for label, citation in pairs:
                marker = _RELATION_MARKERS.get(citation.get("relation", ""), "").strip()
                quote = (citation.get("quote") or "").strip()
                locator = citation.get("passage_locator") or citation.get("locator") or ""
                location = (
                    citation.get("source_url")
                    or citation.get("source_path")
                    or "(no URL/path recorded)"
                )
                source_title = citation.get("source_title") or "(untitled source)"
                details: list[str] = [f"*{source_title}*", location]
                if locator:
                    details.append(f"locator: {locator}")
                if citation.get("source_publication_date"):
                    details.append(f"published {str(citation['source_publication_date'])[:10]}")
                if citation.get("source_retrieved_at"):
                    details.append(f"retrieved {str(citation['source_retrieved_at'])[:10]}")
                suffix = f" ({'; '.join(details[1:])})" if len(details) > 1 else ""
                quote_text = f"“{_short(quote, 200)}” — " if quote else ""
                marker_text = f" {marker}" if marker else ""
                lines.append(f"- Evidence [{label}]: {quote_text}{details[0]}{suffix}{marker_text}")
        else:
            lines.append("- Evidence: none attached.")
    lines.append("")
    return lines


def _render_source_quality_table(source_assessment: dict[str, Any]) -> list[str]:
    lines = [
        "### Source quality indicators (heuristics, not truth)",
        "",
        "| Source | Type | Published | Retrieved | Relevance to question | Directness | Age (days) | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    shared: dict[int, str] = {}
    for group in source_assessment.get("shared_independence_groups") or []:
        for source_id in group.get("source_ids") or []:
            shared[int(source_id)] = str(group.get("independence_group") or "")
    for source in source_assessment.get("sources") or []:
        notes: list[str] = []
        if source.get("potentially_stale"):
            notes.append("potentially stale")
        duplicates = source.get("duplicate_content_of") or []
        if duplicates:
            notes.append(
                "duplicate content of source(s) " + ", ".join(str(item) for item in duplicates)
            )
        if not source.get("has_publication_date"):
            notes.append("no publication date")
        group = shared.get(int(source.get("source_id", 0)), "")
        if group:
            notes.append(f"shares independence group with other sources (`{group}`)")
        lines.append(
            "| {title} (`#{sid}`) | {stype} | {published} | {retrieved} | {relevance} | {directness} | {age} | {notes} |".format(
                title=_pipes(str(source.get("title") or "(untitled source)"), 60),
                sid=source.get("source_id"),
                stype=_pipes(str(source.get("source_type") or ""), 24),
                published=str(source.get("publication_date") or "—")[:10],
                retrieved=str(source.get("retrieved_at") or "—")[:10],
                relevance=(
                    f"{float(source.get('relevance_to_run_query') or 0.0):.0%}"
                    if source.get("relevance_to_run_query") is not None
                    else "—"
                ),
                directness=_pipes(str(source.get("directness") or "unknown"), 30),
                age=(
                    source.get("age_days")
                    if source.get("age_days") is not None
                    else "—"
                ),
                notes=_pipes("; ".join(notes) or "—", 120),
            )
        )
    lines.append("")
    note = str(source_assessment.get("note") or "").strip()
    if note:
        lines.append(f"*{note}*")
        lines.append("")
    return lines


def _render_conflict_block(
    index: int,
    conflict: dict[str, Any],
    *,
    claim: dict[str, Any] | None,
    finding_label: str,
    label_by_citation: dict[int, str],
) -> list[str]:
    claim_text = str((claim or {}).get("claim_text") or conflict.get("claim_text") or "")
    kind = str(conflict.get("kind") or "conflict")
    if claim is not None and claim_text:
        heading = f"### Conflict {index}: [{finding_label}] {_short(claim_text, 120)}"
    elif conflict.get("question"):
        heading = f"### Conflict {index}: subquestion — {_short(str(conflict['question']), 120)}"
    else:
        heading = f"### Conflict {index}: [{finding_label or 'unmapped'}] {_short(claim_text, 120)}"
    lines = [heading, ""]
    status = str(conflict.get("status") or "")
    resolved = bool(conflict.get("resolved"))
    state = "RESOLVED" if resolved else "UNRESOLVED"
    lines.append(f"- **Status:** {_status_label(status)} · {state} · source: {kind}")
    resolution = str(conflict.get("resolution") or "").strip()
    if resolution:
        lines.append(f"- Resolution: {resolution}")
    differences = conflict.get("differences") or {}
    if isinstance(differences, dict):
        dates = [str(item) for item in differences.get("publication_dates") or [] if str(item)]
        types = [str(item) for item in differences.get("source_types") or [] if str(item)]
        independence = str(differences.get("independence") or "").strip()
        bits: list[str] = []
        if dates:
            bits.append("publication dates seen: " + ", ".join(dates))
        if types:
            bits.append("source types: " + ", ".join(types))
        if independence:
            bits.append(independence)
        basis = str(differences.get("basis") or "").strip()
        if basis:
            bits.append(f"basis: {basis}")
        if bits:
            lines.append("- Differences that may explain it: " + "; ".join(bits))
    sides = conflict.get("sides") or []
    if sides:
        lines.append("- Evidence on each side:")
        for side in sides:
            relation = str(side.get("relation") or "?")
            label = ""
            citation_id = side.get("citation_id")
            if citation_id is not None and int(citation_id) in label_by_citation:
                label = f"[{label_by_citation[int(citation_id)]}] "
            elif claim is not None and side.get("passage_id") is not None:
                for candidate_label, citation in _citation_labels(claim, label_by_citation):
                    if int(citation.get("passage_id", -1)) == int(side["passage_id"]):
                        label = f"[{candidate_label}] "
                        break
            quote = str(side.get("quote") or "").strip()
            quote_text = f"“{_short(quote, 200)}” — " if quote else ""
            location = (
                side.get("source_url") or side.get("source_path") or "(no URL/path recorded)"
            )
            details: list[str] = []
            if side.get("locator"):
                details.append(f"locator: {side['locator']}")
            if side.get("publication_date"):
                details.append(f"published {str(side['publication_date'])[:10]}")
            if side.get("retrieved_at"):
                details.append(f"retrieved {str(side['retrieved_at'])[:10]}")
            suffix = f" ({'; '.join(details)})" if details else ""
            lines.append(
                f"  - {relation}: {label}{quote_text}"
                f"*{side.get('source_title') or '(untitled source)'}* — {location}{suffix}"
            )
    lines.append("")
    return lines


def build_phase6_markdown(
    run: dict[str, Any],
    claims: list[dict[str, Any]],
    *,
    verifications: dict[int, dict[str, Any]] | None = None,
    title: str = "",
    generated_at: datetime | None = None,
    plan: dict[str, Any] | None = None,
    subquestions: list[dict[str, Any]] | None = None,
    gaps: list[dict[str, Any]] | None = None,
    budget: dict[str, Any] | None = None,
    stopping_reason: str = "",
    methodology: dict[str, Any] | None = None,
    run_sources: list[dict[str, Any]] | None = None,
    source_assessment: dict[str, Any] | None = None,
    conflicts: list[dict[str, Any]] | dict[str, Any] | None = None,
    images: list[dict[str, Any]] | None = None,
    verification_context: dict[str, Any] | None = None,
    template_version: str = PHASE6_TEMPLATE_VERSION,
) -> tuple[str, dict[str, dict[str, int]]]:
    """Render the Phase 6 verified-synthesis report.

    Ten sections: title/metadata, executive summary, scope and methodology,
    key findings (facts / inferences / unverified statements kept apart),
    evidence comparison tables, conflicting evidence, limitations and open
    questions, conclusion, sources and references (stable ``[Sn]``), and
    image attribution (``[IMG#]``). Statements carry stable ``[F#]`` ids.

    Every rendered statement keeps its verification status, method,
    confidence, rationale, and limitations — and unverified statements are
    labelled as such, never presented as supported. Returns
    ``(markdown, {"citation_map": ..., "finding_map": ..., "image_map": ...})``.
    """
    from . import verification as verification_mod

    verifications = verifications or {}
    source_assessment = source_assessment or {}
    verification_context = verification_context or {}
    images = list(images or [])
    if isinstance(conflicts, dict):
        conflicts = list(conflicts.get("conflicts") or [])
    conflicts = list(conflicts or [])

    claims_sorted = sorted(claims, key=lambda claim: int(claim["claim_id"]))
    finding_map, citation_map, image_map = _phase6_identifiers(claims_sorted, images)
    finding_by_claim = {claim_id: label for label, claim_id in finding_map.items()}
    label_by_citation = {citation_id: label for label, citation_id in citation_map.items()}

    effective: dict[int, str] = {
        int(claim["claim_id"]): _status_key(claim, verifications.get(int(claim["claim_id"])))
        for claim in claims_sorted
    }
    counts: dict[str, int] = {}
    for status in effective.values():
        counts[status] = counts.get(status, 0) + 1
    methods_used: dict[str, int] = {}
    for claim in claims_sorted:
        verification = verifications.get(int(claim["claim_id"]))
        if verification is not None:
            method = str(verification.get("method") or "unspecified")
            methods_used[method] = methods_used.get(method, 0) + 1
    capability = verification_context.get("capability") or {}
    verified_records = sum(1 for claim in claims_sorted if int(claim["claim_id"]) in verifications)

    stamp = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    heading = (title or "").strip() or f"Research run {run['run_id']}: {run['query']}"
    lines: list[str] = [
        f"# {heading}",
        "",
        f"*Generated {stamp} by agletras-mcp · research run `{run['run_id']}` · "
        f"query: “{run['query']}” · template: {template_version} · "
        f"run status: {run.get('status') or 'unknown'}*",
        "",
    ]

    # --- 2. Executive summary ---------------------------------------------
    status_line = " · ".join(
        f"{verification_mod.VERIFICATION_LABELS.get(status, status.upper())}: {counts[status]}"
        for status in sorted(counts, key=lambda item: verification_mod.VERIFICATION_RANK.get(item, 99))
    )
    if "model" in methods_used:
        model_note = (
            f"a configured verification model ({capability.get('model') or 'unspecified model'}) "
            f"assessed {methods_used.get('model', 0)} statement(s); every result was validated "
            "against stored passages"
        )
    else:
        model_note = (
            "no verification model was configured or used; every status comes from transparent "
            "deterministic checks over the stored passages"
        )
    supported_claims = [c for c in claims_sorted if effective[int(c["claim_id"])] == "supported"]
    partial_count = counts.get("partially_supported", 0)
    uncertain = sum(
        counts.get(status, 0)
        for status in (
            "partially_supported",
            "conflicting_evidence",
            "contradicted",
            "unsupported",
            "unverifiable",
            "unverified",
            "needs_review",
        )
    )
    unresolved_conflicts = sum(1 for item in conflicts if not item.get("resolved"))
    open_question_claims = [c for c in claims_sorted if str(c.get("claim_type") or "") == "question"]

    lines.extend(["## Executive summary", ""])
    lines.append(f"- **Research question:** {run['query']}")
    lines.append(
        f"- **Evidence base:** {len(run_sources or [])} source(s) linked to this run; "
        f"{len(citation_map)} citation(s) rendered; {len(claims_sorted)} statement(s) registered"
        + (f", {verified_records} with a verification record" if verified_records else "")
        + "."
    )
    lines.append(f"- **Verification:** {model_note}.")
    if status_line:
        lines.append(f"- **Claim statuses:** {status_line}.")
    else:
        lines.append("- **Claim statuses:** no statements are registered for this research run.")
    if supported_claims:
        headline = "; ".join(
            f"[{finding_by_claim[int(claim['claim_id'])]}] {_short(claim['claim_text'], 140)}"
            for claim in supported_claims[:5]
        )
        lines.append(
            f"- **Headline supported findings ({len(supported_claims)}):** {headline}"
        )
    else:
        lines.append(
            "- **Headline supported findings:** none — no statement is currently marked SUPPORTED."
        )
    if uncertain:
        partial_text = f" (including {partial_count} partially supported)" if partial_count else ""
        lines.append(
            f"- **Largest gaps:** {uncertain} statement(s) are not fully supported"
            f"{partial_text}, {unresolved_conflicts} conflict(s) remain unresolved, and "
            f"{len(gaps or [])} subquestion(s) are open."
        )
    else:
        lines.append(
            "- **Largest gaps:** none recorded — every statement is marked SUPPORTED by the "
            "stored evidence."
        )
    lines.append(f"- **Reliability note:** {_NOT_PROOF}")
    lines.append("")

    # --- 3. Scope and methodology ------------------------------------------
    lines.extend(["## Scope and methodology", ""])
    lines.append(f"- **Question:** {run['query']}")
    if plan:
        objective = str(plan.get("objective") or "").strip()
        if objective:
            lines.append(f"- **Objective:** {objective}")
        scope = str(plan.get("scope") or "").strip()
        if scope:
            lines.append(f"- **Scope:** {scope}")
        assumptions = [str(item) for item in plan.get("assumptions") or [] if str(item).strip()]
        if assumptions:
            lines.append("- **Recorded assumptions:** " + "; ".join(assumptions))
        limits = plan.get("limits") if isinstance(plan.get("limits"), dict) else {}
        if limits:
            lines.append(f"- **Run limits:** {_format_limits(limits)}")
    lines.append(
        "- **Statement extraction:** "
        + str(verification_context.get("extraction_method") or _EXTRACTION_METHOD)
        + "."
    )
    if methods_used:
        breakdown = " · ".join(
            f"{_method_label(method)}: {count}"
            for method, count in sorted(methods_used.items())
        )
        lines.append(f"- **Verification methods used:** {breakdown}.")
    else:
        lines.append(
            "- **Verification methods used:** none — no statement carries a verification record "
            "yet; run verify_claims to produce statuses."
        )
    thresholds = verification_context.get("thresholds") or {
        "min_relevance": verification_mod.MIN_RELEVANCE,
        "partial": verification_mod.PARTIAL_THRESHOLD,
        "support": verification_mod.SUPPORT_THRESHOLD,
    }
    lines.append(
        "- **Deterministic thresholds:** term coverage ≥ "
        f"{float(thresholds.get('support', 0.0)):.0%} can support a statement, ≥ "
        f"{float(thresholds.get('partial', 0.0)):.0%} is partial, and below "
        f"{float(thresholds.get('min_relevance', 0.0)):.0%} a passage is treated as unrelated; "
        "number/date agreement, hedging (overgeneralization), polarity, and contradiction "
        "relations override the score."
    )
    if subquestions:
        answered = sum(1 for item in subquestions if item.get("status") == "answered")
        lines.append(
            f"- **Subquestions tracked:** {len(subquestions)} (answered: {answered}; see the "
            "open questions below)."
        )
    if budget:
        lines.append(
            f"- **Throughput:** {int(budget.get('sources_used') or 0)} of "
            f"{int(budget.get('max_unique_sources') or 0)} unique sources used; "
            f"{int(budget.get('followup_rounds_used') or 0)} of "
            f"{int(budget.get('max_followup_rounds') or 0)} follow-up rounds used."
        )
    if methodology:
        searches = int(methodology.get("searches_logged") or 0)
        failures = int(methodology.get("retrieval_failures") or 0)
        if searches or failures:
            lines.append(
                f"- **Search activity:** {searches} search(es) logged; {failures} retrieval "
                "failure(s) recorded."
            )
    if stopping_reason:
        lines.append(f"- **Research stopped because:** {stopping_reason}")
    lines.append(
        "- **What this report does not do:** it does not treat popularity, ranking, or the "
        "newest date as truth; it never resolves source disagreements by preference; and it "
        "never upgrades an unsupported statement into a finding."
    )
    lines.append("")

    # --- 4. Key findings ----------------------------------------------------
    lines.extend(["## Key findings", ""])
    if not claims_sorted:
        lines.append("*No statements are registered for this research run.*")
        lines.append("")

    def render_group(header: str, note: str, selected: list[dict[str, Any]], **kwargs: Any) -> None:
        lines.extend([f"### {header}", ""])
        if note:
            lines.append(f"*{note}*")
            lines.append("")
        if not selected:
            lines.append("None.")
            lines.append("")
            return
        for claim in selected:
            lines.extend(
                _render_claim_entry(
                    claim,
                    verifications.get(int(claim["claim_id"])),
                    finding_label=finding_by_claim[int(claim["claim_id"])],
                    label_by_citation=label_by_citation,
                    **kwargs,
                )
            )

    supported_group = [c for c in claims_sorted if effective[int(c["claim_id"])] == "supported"]
    partial_group = [
        c for c in claims_sorted if effective[int(c["claim_id"])] == "partially_supported"
    ]
    discrete = [
        c
        for c in claims_sorted
        if str(c.get("claim_type") or "") == "inference"
        and effective[int(c["claim_id"])] not in ("supported", "partially_supported")
    ]
    unverified_group = [
        c
        for c in claims_sorted
        if c not in supported_group
        and c not in partial_group
        and c not in discrete
        and str(c.get("claim_type") or "") != "question"
        and effective[int(c["claim_id"])] not in ("contradicted", "conflicting_evidence")
    ]
    render_group("Supported statements (facts)", "", supported_group)
    render_group("Partially supported statements", "", partial_group)
    render_group(
        "Inferences (reasoned, not verified)",
        "Inferences are the researcher's reasoning over the evidence; they are never facts. "
        "Each one states its reasoning where recorded.",
        discrete,
        show_evidence=False,
    )
    render_group(
        "Unverified and unevaluable statements",
        "These statements are NOT asserted as true: their evidence is absent, insufficient, "
        "or does not establish them.",
        unverified_group,
    )

    # --- 5. Evidence comparison --------------------------------------------
    lines.extend(["## Evidence comparison", "", "### Statement status overview", ""])
    lines.append(
        "| ID | Type | Statement | Verification status | Confidence | Method | Citations | Contradiction |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for claim in claims_sorted:
        verification = verifications.get(int(claim["claim_id"]))
        contradictions = sum(
            1 for citation in claim.get("citations", []) if citation.get("relation") == "contradicts"
        )
        lines.append(
            "| [{label}] | {ctype} | {statement} | {status} | {confidence} | {method} | {citations} | {contradiction} |".format(
                label=finding_by_claim[int(claim["claim_id"])],
                ctype=_claim_type_label(claim.get("claim_type")),
                statement=_pipes(str(claim.get("claim_text") or "")),
                status=_status_display(claim, verification),
                confidence=(verification or {}).get("confidence") or "—",
                method=_pipes(_method_label((verification or {}).get("method")), 60) if verification else "—",
                citations=len(claim.get("citations", [])),
                contradiction=contradictions or "—",
            )
        )
    if not claims_sorted:
        lines.append("| — | — | (no statements registered) | — | — | — | — | — |")
    lines.append("")
    if source_assessment.get("sources"):
        lines.extend(_render_source_quality_table(source_assessment))
    else:
        lines.extend(
            [
                "### Source quality indicators (heuristics, not truth)",
                "",
                "*No source assessment was recorded for this run; run analyze_conflicts with "
                "source inclusion to add the metadata-based quality indicators.*",
                "",
            ]
        )

    # --- 6. Conflicting evidence -------------------------------------------
    lines.extend(["## Conflicting evidence", ""])
    conflict_claim_ids: set[int] = set()
    for item in conflicts:
        if item.get("claim_id") is not None:
            conflict_claim_ids.add(int(item["claim_id"]))
    contested_claims = [
        claim
        for claim in claims_sorted
        if effective[int(claim["claim_id"])] in ("contradicted", "conflicting_evidence")
        and int(claim["claim_id"]) not in conflict_claim_ids
    ]
    if not conflicts and not contested_claims:
        lines.append(
            "No conflicting evidence was detected in the stored material for this run. This is "
            "**not proof of consistency** — conflicting sources may simply not have been "
            "collected or recognised."
        )
        lines.append("")
    for index, conflict in enumerate(conflicts, start=1):
        claim_id = conflict.get("claim_id")
        claim = None
        if claim_id is not None:
            claim = next(
                (item for item in claims_sorted if int(item["claim_id"]) == int(claim_id)), None
            )
        lines.extend(
            _render_conflict_block(
                index,
                conflict,
                claim=claim,
                finding_label=(
                    finding_by_claim.get(int(claim_id), "") if claim_id is not None else ""
                ),
                label_by_citation=label_by_citation,
            )
        )
    for claim in contested_claims:
        verification = verifications.get(int(claim["claim_id"]))
        fallback = {
            "kind": "statement_status",
            "claim_id": int(claim["claim_id"]),
            "claim_text": claim["claim_text"],
            "status": effective[int(claim["claim_id"])],
            "sides": [],
            "differences": (verification or {}).get("evidence"),
            "resolved": False,
            "resolution": str((verification or {}).get("rationale") or "").strip()
            or "contradicted by attached evidence; no conflict record was stored for this statement",
        }
        lines.extend(
            _render_conflict_block(
                len(conflicts) + contested_claims.index(claim) + 1,
                fallback,
                claim=claim,
                finding_label=finding_by_claim[int(claim["claim_id"])],
                label_by_citation=label_by_citation,
            )
        )
    if conflicts:
        lines.append(
            "Contradictions found during verification are never auto-attached as citations and "
            "never resolved by preference; they stay visible for review."
        )
        lines.append("")

    # --- 7. Limitations and open questions ---------------------------------
    lines.extend(["## Limitations and open questions", "", "### Method limitations", ""])
    lines.append(
        "- All statuses come from comparing the claim wording with the exact stored passage "
        "text; they record what the checks could establish, not truth."
    )
    lines.append(
        f"- {_NOT_PROOF} A missing contradiction is not verification."
    )
    if not capability.get("model_available"):
        lines.append(
            "- No verification model was configured (AGLETRAS_VERIFICATION_MODEL_ENDPOINT unset), "
            "so no model-assisted assessment was possible; the deterministic checks are the "
            "complete method record."
        )
    else:
        lines.append(
            "- Model-assisted verification is opt-in; rejected or failed model responses fall "
            "back to the deterministic result, and that fallback is recorded per statement."
        )
    limitation_claims = [
        (claim, verifications.get(int(claim["claim_id"])))
        for claim in claims_sorted
        if (verifications.get(int(claim["claim_id"])) or {}).get("limitations")
    ]
    for claim, verification in limitation_claims:
        items = "; ".join(str(item) for item in verification.get("limitations") or [])
        lines.append(
            f"- [{finding_by_claim[int(claim['claim_id'])]}] {items}"
        )
    lines.append("")
    lines.extend(["### Source-quality cautions", ""])
    cautions = [str(item) for item in source_assessment.get("cautions") or [] if str(item).strip()]
    if cautions:
        for caution in cautions:
            lines.append(f"- {caution}")
    else:
        lines.append("None recorded.")
    lines.append("")
    lines.extend(["### Unresolved conflicts", ""])
    if unresolved_conflicts:
        labels = sorted(
            {
                finding_by_claim[int(item["claim_id"])]
                for item in conflicts
                if not item.get("resolved") and item.get("claim_id") is not None
                and int(item["claim_id"]) in finding_by_claim
            }
        )
        lines.append(
            f"{unresolved_conflicts} conflict(s) remain unresolved"
            + (f" ({', '.join(labels)})" if labels else "")
            + "; the stored evidence does not decide between the interpretations."
        )
    else:
        lines.append("No unresolved conflicts are recorded.")
    lines.append("")
    lines.extend(["### Open questions", ""])
    open_items: list[str] = []
    for claim in open_question_claims:
        note = str(claim.get("note") or "").strip()
        entry = (
            f"- [{finding_by_claim[int(claim['claim_id'])]}] {claim['claim_text']}"
            + (f" — {note}" if note else "")
        )
        open_items.append(entry)
    for gap in gaps or []:
        status = str(gap.get("status") or "open").upper()
        note = str(gap.get("note") or "").strip()
        entry = f"- {gap.get('question', '(unnamed)')} — **{status}**"
        if note:
            entry += f": {note}"
        open_items.append(entry)
    if open_items:
        lines.extend(open_items)
    else:
        lines.append("No open questions are recorded.")
    lines.append("")
    lines.extend(["### What remains unknown", ""])
    not_supported = [
        claim
        for claim in claims_sorted
        if effective[int(claim["claim_id"])] != "supported"
        and str(claim.get("claim_type") or "") != "question"
    ]
    if not_supported:
        labels = ", ".join(
            f"[{finding_by_claim[int(claim['claim_id'])]}]" for claim in not_supported
        )
        lines.append(
            f"{len(not_supported)} statement(s) are not fully supported by the stored evidence "
            f"({labels}); their statuses say exactly what could and could not be established. "
            "Where the evidence was insufficient, no conclusion is invented to fill the gap."
        )
    else:
        lines.append(
            "Every registered statement is marked SUPPORTED by the stored evidence; the "
            "remaining unknowns are the open questions above."
        )
    lines.append("")

    # --- 8. Conclusion ------------------------------------------------------
    lines.extend(["## Conclusion", ""])

    def conclusion_line(kind: str, selected: list[dict[str, Any]]) -> str:
        labels = "; ".join(
            f"[{finding_by_claim[int(claim['claim_id'])]}] {_short(claim['claim_text'], 120)}"
            for claim in selected
        )
        return f"- **{kind} ({len(selected)}):** {labels}" if selected else f"- **{kind}:** none."

    lines.append(conclusion_line("Established by the stored evidence", supported_group))
    lines.append(conclusion_line("Partially established", partial_group))
    contested_all = [c for c in claims_sorted if effective[int(c["claim_id"])] in ("contradicted", "conflicting_evidence")]
    lines.append(conclusion_line("Contradicted or conflicted", contested_all))
    lines.append(
        conclusion_line(
            "Not established or not evaluable",
            unverified_group + [c for c in discrete if effective[int(c["claim_id"])] not in ("supported", "partially_supported")],
        )
    )
    lines.append(
        "- **Boundary of this conclusion:** it asserts exactly the statuses above and nothing "
        "more; statements without a SUPPORTED status are not asserted as true, and unresolved "
        "conflicts are left open."
    )
    lines.append("")

    # --- 9. Sources and references -----------------------------------------
    lines.extend(["## Sources and references", ""])
    lines.append(
        "Citation identifiers are stable within this report: `[Sn]` links a statement to one "
        "exact stored passage; `[F#]` is a statement; `[IMG#]` is an image record. URLs are "
        "recorded exactly as retrieved — no liveness check was performed."
    )
    lines.append("")
    if citation_map:
        citation_by_id = {
            int(citation["citation_id"]): (claim, citation)
            for claim in claims_sorted
            for citation in claim.get("citations", [])
        }
        for label in sorted(citation_map, key=lambda item: int(item[1:])):
            citation_id = citation_map[label]
            _claim, citation = citation_by_id.get(int(citation_id), (None, None))
            if citation is None:
                lines.append(f"- **[{label}]** citation {citation_id} (record missing).")
                continue
            locator = citation.get("passage_locator") or citation.get("locator") or ""
            source = {
                "source_url": citation.get("source_url"),
                "source_path": citation.get("source_path"),
                "source_retrieved_at": citation.get("source_retrieved_at"),
                "source_publication_date": citation.get("source_publication_date"),
            }
            lines.append(
                f"- **[{label}]** {citation.get('source_title') or '(untitled source)'} — "
                f"{_provenance_line(source, locator)}"
            )
    else:
        lines.append("*No citations are attached to this report.*")
    lines.append("")
    if run_sources:
        lines.extend(["### All sources linked to this run", ""])
        for source in run_sources:
            location = source.get("url") or source.get("path") or "(no URL/path recorded)"
            details: list[str] = []
            if source.get("source_type"):
                details.append(str(source["source_type"]))
            if source.get("publication_date"):
                details.append(f"published {str(source['publication_date'])[:10]}")
            if source.get("retrieved_at"):
                details.append(f"retrieved {str(source['retrieved_at'])[:10]}")
            suffix = f" ({'; '.join(details)})" if details else ""
            lines.append(
                f"- **{source.get('title') or '(untitled source)'}** (`#{int(source['source_id'])}`) "
                f"— {location}{suffix}"
            )
        lines.append("")

    # --- 10. Image attribution ---------------------------------------------
    lines.extend(["## Image attribution", ""])
    if images:
        lines.append(
            "Images are discovery records (URL, creator, license, attribution as reported by the "
            "provider); missing metadata is stated, never invented."
        )
        lines.append("")
        for label in sorted(image_map, key=lambda item: int(item[3:])):
            image = next(
                item for item in images if int(item["source_id"]) == image_map[label]
            )
            lines.append(f"### [{label}] {image.get('title') or '(untitled image)'}")
            lines.append("")
            lines.append(
                f"- Creator: {image.get('creator') or 'not recorded'}"
            )
            lines.append(
                f"- License: {image.get('license_name') or 'not recorded'}"
                + (f" ({image['license_url']})" if image.get("license_url") else "")
            )
            attribution = str(image.get("attribution") or "").strip()
            if attribution:
                lines.append(f"- Attribution: {attribution}")
            if image.get("source_page_url"):
                lines.append(f"- Source page: {image['source_page_url']}")
            if image.get("image_url"):
                lines.append(f"- Image URL: {image['image_url']}")
            if image.get("retrieved_at"):
                lines.append(f"- Retrieved: {str(image['retrieved_at'])[:19]}")
            if image.get("width") and image.get("height"):
                lines.append(f"- Dimensions: {image['width']}×{image['height']}")
            if image.get("mime_type"):
                lines.append(f"- MIME type: {image['mime_type']}")
            missing = [str(item) for item in image.get("missing_metadata") or []]
            if missing:
                lines.append(f"- Missing metadata: {', '.join(missing)}")
            lines.append("")
    else:
        lines.append("*No images are attached to this research run.*")
        lines.append("")

    return "\n".join(lines), {
        "citation_map": citation_map,
        "finding_map": finding_map,
        "image_map": image_map,
    }


def build_markdown(
    title: str,
    sources: list[dict[str, Any]],
    *,
    include_excerpts: bool = True,
    generated_at: datetime | None = None,
) -> str:
    """Render a citation list for *sources* as Markdown."""
    stamp = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [
        f"# {title}",
        "",
        f"*Generated {stamp} by agletras-mcp · sources: {len(sources)}*",
        "",
        "## Sources",
        "",
    ]
    for index, source in enumerate(sources, start=1):
        label = source.get("title") or source.get("url") or source.get("path") or f"source {source['id']}"
        location = source.get("url") or source.get("path") or ""
        heading = f"{index}. **{label}**"
        if location and location != label:
            heading += f" — {location}"
        lines.append(heading)
        meta = [
            f"id `{source['id']}`",
            f"kind: {source['kind']}",
            f"saved: {str(source.get('created_at', ''))[:10]}",
        ]
        if source.get("notes"):
            meta.append(f"notes: {source['notes']}")
        lines.append(f"   - {' · '.join(meta)}")
        if include_excerpts and source.get("excerpt"):
            lines.append(f"   - excerpt: “{source['excerpt']}”")
    lines.append("")
    return "\n".join(lines)


def export_report(
    title: str,
    *,
    source_ids: list[int] | None = None,
    query: str | None = None,
    limit: int = 20,
    include_excerpts: bool = True,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Build and save a Markdown report. Returns path and content metadata."""
    settings = settings or load_settings()
    title = (title or "").strip()
    if not title:
        raise ToolError("invalid_argument", "title must not be empty")

    warnings: list[str] = []
    sources: list[dict[str, Any]] = []
    if source_ids:
        for source_id in source_ids:
            record = storage.get_source(int(source_id), settings=settings)
            if record is None:
                warnings.append(f"source id {source_id} not found")
            else:
                sources.append(record)
    elif query and query.strip():
        sources = [
            storage.get_source(summary["id"], settings=settings) or summary
            for summary in storage.search_sources(query, limit=limit, settings=settings)
        ]
    else:
        # Default: the newest sources. list_sources returns summaries only, so
        # fetch full records (with content) for proper excerpts.
        summaries = storage.list_sources(limit=limit, settings=settings)
        sources = [
            storage.get_source(summary["id"], settings=settings) or summary
            for summary in summaries
        ]

    if not sources:
        raise ToolError(
            "empty_selection",
            "No sources matched the selection; save sources first or adjust the query.",
        )

    markdown = build_markdown(title, sources, include_excerpts=include_excerpts)
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = settings.reports_dir / f"{slugify(title)}-{stamp}.md"
    path.write_text(markdown, encoding="utf-8")
    LOGGER.info("wrote report %s (%d sources)", path, len(sources))
    return {
        "path": str(path),
        "filename": path.name,
        "sources_included": len(sources),
        "source_ids": [source["id"] for source in sources],
        "markdown": markdown,
        "warnings": warnings,
    }

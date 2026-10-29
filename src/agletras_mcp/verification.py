"""Claim extraction, evidence mapping, and verification (Phase 6).

Turns collected research material into statements that are explicitly
*checked* against the stored evidence — and keeps the uncertainty visible:

- **Extraction** (:func:`extract_claims`) is a transparent, deterministic
  sentence splitter and marker scorer. It never invents claims and never
  registers anything unless asked. A model is *not* required and is never
  assumed available.
- **Evidence mapping** links every candidate statement to the stored passages
  that discuss it, with a coverage score, the exact quote when the text
  matches verbatim, and full source provenance (URL/path, title, dates,
  content hash, locator).
- **Verification** (:func:`verify_claims`) evaluates each claim against the
  *exact passage text* (never just titles or URLs) using documented,
  deterministic checks: term coverage, verbatim quoting, number and date
  agreement, absolute/hedged quantification (possible overgeneralization),
  and polarity (negation) mismatches. Statuses:

  ===================== ===========================================================
  ``supported``         The cited/checked passage establishes the claim wording.
  ``partially_supported``The evidence establishes only part of the claim
                        (missing details/numbers, hedged wording, weaker scope).
  ``contradicted``      Relevant evidence directly conflicts with the claim
                        (attached ``contradicts`` citations, or a number/date
                        disagreement with no supporting evidence).
  ``conflicting_evidence`` Credible independent sources disagree and the
                        disagreement is left unresolved — never papered over.
  ``unsupported``       Relevant evidence exists but does not establish the claim.
  ``unverifiable``      The available evidence is insufficient to evaluate it
                        (nothing relevant was collected, or only context).
  ===================== ===========================================================

- **Honesty rules.** A missing contradiction is never proof of truth; a
  model-generated explanation is never verification; absence of a verification
  record stays visible in the report. The deterministic checks record what
  they can and cannot decide (``limitations``). Model-based verification is
  OPT-IN (``AGLETRAS_VERIFICATION_MODEL_ENDPOINT``) and its responses must be
  structured and reference stored passage ids — otherwise the result is
  rejected and the deterministic assessment stands, with the reason recorded.

- **Source quality** (:func:`assess_sources`) reports metadata-driven,
  clearly-labelled heuristics (relevance, directness, staleness, duplicate/
  syndicated content, methodology keywords). Quality indicators are kept
  strictly separate from verification statuses: popularity and ranking are
  never treated as truth.

- **Conflicts** (:func:`analyze_conflicts`) lists every disagreement with the
  evidence on both sides and says when the stored material cannot decide which
  interpretation is right.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from . import canonurl, evidence, planner, storage
from .config import Settings, load_settings
from .errors import ToolError
from .source_record import utc_now_iso

LOGGER = logging.getLogger(__name__)

#: Documented verification statuses (PART 3 vocabulary).
VERIFICATION_STATUSES = (
    "supported",
    "partially_supported",
    "contradicted",
    "conflicting_evidence",
    "unsupported",
    "unverifiable",
)
#: Human-readable labels used in reports and tool output.
VERIFICATION_LABELS = {
    "supported": "SUPPORTED",
    "partially_supported": "PARTIALLY SUPPORTED",
    "contradicted": "CONTRADICTED BY EVIDENCE",
    "conflicting_evidence": "CONFLICTING EVIDENCE",
    "unsupported": "UNSUPPORTED",
    "unverifiable": "UNVERIFIABLE",
}
#: Which method produced a verification result.
VERIFICATION_METHODS = (
    "source_text_comparison",  # deterministic claim-vs-passage comparison
    "deterministic_checks",    # structural/citation-relation checks decided it
    "model",                   # configured model assessment (structured, cited)
    "model_fallback_deterministic",  # model configured but failed/rejected
)
#: Status ordering for deterministic report ordering (most established first).
VERIFICATION_RANK = {
    "supported": 0,
    "partially_supported": 1,
    "conflicting_evidence": 2,
    "contradicted": 3,
    "unsupported": 4,
    "unverifiable": 5,
    "needs_review": 6,   # Phase 3 claim status without a verification record
    "unverified": 7,
}

#: Coverage thresholds (fraction of the claim's searchable terms present in a
#: passage). Documented so results are reproducible and reviewable.
MIN_RELEVANCE = 0.25   # below this a passage is not about the claim at all
PARTIAL_THRESHOLD = 0.50
SUPPORT_THRESHOLD = 0.85

CONFIDENCE_LEVELS = ("high", "medium", "low")

_MODEL_RATIONALE_CHARS = 2000
_MAX_CANDIDATES = 8
_MAX_EXTRACT_CHARS = 200_000

_STOPWORDS = frozenset(
    """a an the and or but if then else of in on at to for with by from as is are was were be
    been being it its this that these those not no do does did can could will would should may
    might must have has had he she they we you i how what when where why who which whose whom
    about into over under than too very just also""".split()
)

_UNIVERSAL_TERMS = frozenset(
    """all every always never only none nobody everyone everything universally
    exclusively solely entirely completely totally first largest smallest best
    worst most least highest lowest""".split()
)
_HEDGE_TERMS = frozenset(
    """some most many several often sometimes typically generally usually may can
    could might about approximately nearly around estimated roughly up
    largely partly partially among within""".split()
)
_NEGATIONS = frozenset(
    """not no never none nor cannot can't don't doesn't didn't isn't aren't wasn't weren't
    without lacks lack lacking denied denies deny refused refuse""".split()
)
_DECLARATIVE_CUES = frozenset(
    """is are was were has have had can will includes include included consists
    comprise comprises contains contain represents represent measures measure
    reached reach reported reports showed show found finds increases increase
    increased decreases decrease decreased grew grow grew rose rise fell fall
    remains remain became become holds hold produces produce""".split()
)
_ATTRIBUTION_CUES = (
    "according to", "reported", "reports", "states", "stated", "found",
    "study", "survey", "data", "measured", "estimate", "estimates",
)
_COMPARATIVE_CUES = frozenset(
    """more less higher lower larger smaller greater fewer increased decreased
    growth decline compared versus than""".split()
)
_METHODOLOGY_CUES = (
    "method", "methodology", "sample", "sampling", "dataset", "data set",
    "experiment", "participants", "respondents", "measurement", "protocol",
    "survey", "questionnaire", "peer-reviewed", "statistical",
)
#: URL path fragments suggesting a primary document vs reporting.
_PRIMARY_HINTS = ("/paper", "/papers/", "/publication", "/report", "/data", "/statistics", "/spec")
_SECONDARY_HINTS = ("/blog", "/news", "/press", "/opinion", "/wiki", "reddit", "medium.com")

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")
_NUMBER_RE = re.compile(
    r"(?<![\w.])(\d{1,3}(?:[,.]\d{3})+(?:[,.]\d+)?|\d+(?:[.,]\d+)?)\s?"
    r"(%|percent|per cent|million|billion|thousand|bn|kg|km|mi|ft|cm|mm|m2|m²|m|s|h|years?|days?|months?)?",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2}|2100)\b")
_WORD_RE = re.compile(r"[\w']+", re.UNICODE)
_SPELLED_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "dozen": 12,
}

_EXTRACT_MARKERS = {
    "number": 2.0,
    "date": 1.5,
    "percent": 0.5,
    "attribution": 0.75,
    "comparative": 0.5,
    "declarative": 1.0,
    "proper_noun": 0.5,
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return utc_now_iso()


def _normalize_ws(text: str) -> str:
    return " ".join((text or "").split())


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


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


def _require_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM research_runs WHERE id = ?", (int(run_id),)).fetchone()
    if row is None:
        raise ToolError(
            "not_found",
            f"No research run with id {run_id}; create one with start_research_run first.",
        )
    return row


def _terms(text: str, *, limit: int = 40) -> list[str]:
    return evidence.extract_terms(text, max_terms=limit)


def _term_present(term: str, haystack: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", haystack, flags=re.UNICODE))


# ---------------------------------------------------------------------------
# Pure text comparison (deterministic; unit-testable without a database)
# ---------------------------------------------------------------------------


def split_sentences(text: str) -> list[str]:
    """Split *text* into trimmed sentences (headings and list items included)."""
    chunks: list[str] = []
    for raw_line in (text or "").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        for part in _SENTENCE_SPLIT.split(line):
            cleaned = _normalize_ws(part)
            if cleaned:
                chunks.append(cleaned)
    return chunks


def extract_candidates(text: str, *, max_claims: int = 25) -> list[dict[str, Any]]:
    """Pick candidate factual statements from *text* (deterministic).

    A candidate is a declarative sentence with at least one evidence marker
    (number, date, attribution cue, comparative, declarative verb, or proper
    noun). Questions, headings-only fragments, and very short/long lines are
    skipped. Candidate ids are positional (``C1``, ``C2``, ...) and stable for
    identical input.
    """
    seen: set[str] = set()
    scored: list[tuple[float, int, str, list[str]]] = []
    for index, sentence in enumerate(split_sentences(text)):
        if sentence.endswith("?") or len(sentence) < 30 or len(sentence) > 500:
            continue
        if sentence.startswith("#") or re.match(r"^[-*•]\s*$", sentence):
            continue
        markers: list[str] = []
        score = 0.0
        if _NUMBER_RE.search(sentence):
            markers.append("number")
            score += _EXTRACT_MARKERS["number"]
        if _YEAR_RE.search(sentence):
            markers.append("date")
            score += _EXTRACT_MARKERS["date"]
        if "%" in sentence or "percent" in sentence.lower():
            markers.append("percent")
            score += _EXTRACT_MARKERS["percent"]
        lowered = sentence.lower()
        if any(cue in lowered for cue in _ATTRIBUTION_CUES):
            markers.append("attribution")
            score += _EXTRACT_MARKERS["attribution"]
        words = {word.strip("'").lower() for word in _WORD_RE.findall(sentence)}
        if words & _COMPARATIVE_CUES:
            markers.append("comparative")
            score += _EXTRACT_MARKERS["comparative"]
        if words & _DECLARATIVE_CUES:
            markers.append("declarative")
            score += _EXTRACT_MARKERS["declarative"]
        if any(word[:1].isupper() and index > 0 for word in sentence.split()[1:]):
            markers.append("proper_noun")
            score += _EXTRACT_MARKERS["proper_noun"]
        if score <= 0:
            continue
        key = _normalize_ws(sentence).lower()
        if key in seen:
            continue
        seen.add(key)
        scored.append((score, index, sentence, markers))
    scored.sort(key=lambda item: (-item[0], item[1]))
    bounded = max(1, min(int(max_claims), 100))
    return [
        {"candidate_id": f"C{position}", "text": sentence, "score": round(score, 3), "markers": markers}
        for position, (score, _index, sentence, markers) in enumerate(scored[:bounded], start=1)
    ]


def _normalize_number(raw: str) -> float | None:
    """Parse a numeric token tolerantly (thousand separators, comma decimals)."""
    text = raw.strip()
    if not text:
        return None
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):  # 1,234.5
        text = text.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", text):  # 1.234,5 (European)
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value == value and abs(value) != float("inf") else None


def _context_words(text: str, start: int, end: int, *, window: int = 3) -> list[str]:
    """Non-stopword words surrounding one number (used to judge conflicts)."""
    left = _WORD_RE.findall((text or "")[max(0, start - 40):start])[-window:]
    right = _WORD_RE.findall((text or "")[end:end + 40])[:window]
    words: list[str] = []
    for word in left + right:
        lowered = word.strip("'").lower()
        if len(lowered) < 2 or lowered.isdigit() or lowered in _STOPWORDS:
            continue
        if lowered not in words:
            words.append(lowered)
    return words[:6]


def _numbers(text: str) -> list[dict[str, Any]]:
    """Numeric values with optional units, spans, context, and spelled numbers."""
    found: list[dict[str, Any]] = []
    for match in _NUMBER_RE.finditer(text or ""):
        value = _normalize_number(match.group(1))
        if value is None:
            continue
        found.append(
            {
                "raw": match.group(0).strip(),
                "value": value,
                "unit": (match.group(2) or "").strip().lower(),
                "start": match.start(),
                "end": match.end(),
                "context": _context_words(text or "", match.start(), match.end()),
            }
        )
    for word, value in _SPELLED_NUMBERS.items():
        for spelled in re.finditer(rf"(?<!\w){word}(?!\w)", (text or "").lower()):
            found.append(
                {
                    "raw": word,
                    "value": float(value),
                    "unit": "",
                    "start": spelled.start(),
                    "end": spelled.end(),
                    "context": _context_words(text or "", spelled.start(), spelled.end()),
                }
            )
    return found


def _numbers_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if abs(float(left["value"]) - float(right["value"])) > max(1e-6, abs(left["value"]) * 1e-6):
        return False
    left_unit, right_unit = left.get("unit", ""), right.get("unit", "")
    if not left_unit or not right_unit:
        return True
    return left_unit == right_unit


def _best_quote(passage_text: str, terms: list[str]) -> dict[str, Any] | None:
    """The passage sentence with the highest claim-term coverage (exact slice)."""
    best: dict[str, Any] | None = None
    position = 0
    for raw in (passage_text or "").split("\n"):
        stripped = raw.strip()
        if not stripped:
            position += len(raw) + 1
            continue
        start = position + (len(raw) - len(raw.lstrip()))
        end = start + len(stripped)
        if terms:
            covered = sum(1 for term in terms if _term_present(term, stripped.lower()))
            coverage = covered / len(terms)
        else:
            coverage = 0.0
        if best is None or coverage > best["coverage"]:
            best = {"text": stripped, "coverage": round(coverage, 4), "start": start, "end": end}
        position += len(raw) + 1
    return best


def check_claim_against_passage(claim_text: str, passage_text: str) -> dict[str, Any]:
    """Deterministic comparison of one claim against one exact passage.

    Returns a transparent record of what was compared — coverage of the
    claim's terms, missing terms, number/date agreement, quantification and
    polarity flags — never a judgement of truth beyond that comparison.
    """
    claim = _normalize_ws(claim_text)
    terms = _terms(claim)
    haystack = _normalize_ws(passage_text).lower()
    covered = [term for term in terms if _term_present(term, haystack)]
    missing = [term for term in terms if term not in covered]
    coverage = round(len(covered) / len(terms), 4) if terms else 0.0

    claim_numbers = _numbers(claim)
    passage_numbers = _numbers(passage_text)
    numbers_matched: list[str] = []
    numbers_missing: list[str] = []
    for number in claim_numbers:
        if any(_numbers_match(number, other) for other in passage_numbers):
            numbers_matched.append(number["raw"])
        else:
            numbers_missing.append(number["raw"])

    claim_years = sorted({int(year) for year in _YEAR_RE.findall(claim)})
    passage_years = sorted({int(year) for year in _YEAR_RE.findall(passage_text)})
    years_missing = [year for year in claim_years if year not in passage_years]
    years_conflicting = bool(claim_years and passage_years and not (set(claim_years) & set(passage_years)))

    claim_words = {word.strip("'").lower() for word in _WORD_RE.findall(claim)}
    passage_words = {word.strip("'").lower() for word in _WORD_RE.findall(passage_text)}
    universal_claim = sorted(claim_words & _UNIVERSAL_TERMS)
    universal_passage = sorted(passage_words & _UNIVERSAL_TERMS)
    hedges_passage = sorted(passage_words & _HEDGE_TERMS)
    overgeneralization = bool(universal_claim and not (set(universal_claim) & set(universal_passage)))

    negation_claim = bool(claim_words & _NEGATIONS)
    negation_passage = bool(passage_words & _NEGATIONS)
    polarity_mismatch = negation_claim != negation_passage

    verbatim = evidence.quote_matches(claim, passage_text)
    quote = _best_quote(passage_text, terms)

    flags: list[str] = []
    if numbers_missing:
        flags.append("claim_numbers_missing_from_passage")
    if years_conflicting:
        flags.append("claim_years_conflict_with_passage")
    elif years_missing:
        flags.append("claim_years_missing_from_passage")
    if overgeneralization:
        flags.append("possible_overgeneralization")
    if polarity_mismatch:
        flags.append("polarity_mismatch")
    if coverage >= SUPPORT_THRESHOLD and missing:
        flags.append("minor_terms_missing")

    # A missing claim number counts as a *conflict* only when a passage number
    # states a different value for the same subject (shared context words and
    # compatible units); otherwise it is a detail the passage simply lacks.
    numbers_conflicting = False
    for missing_number in claim_numbers:
        if any(_numbers_match(missing_number, other) for other in passage_numbers):
            continue
        for other in passage_numbers:
            if _numbers_match(missing_number, other):
                continue
            left_unit = missing_number.get("unit", "")
            right_unit = other.get("unit", "")
            if left_unit and right_unit and left_unit != right_unit:
                continue
            if set(missing_number.get("context") or []) & set(other.get("context") or []):
                numbers_conflicting = True
                break
        if numbers_conflicting:
            break
    numbers_conflicting = bool(numbers_conflicting and coverage >= 0.60)
    conflict_signals: list[str] = []
    if numbers_conflicting:
        conflict_signals.append("numbers_disagree")
    if years_conflicting and coverage >= 0.60:
        conflict_signals.append("years_disagree")

    return {
        "coverage": coverage,
        "covered_terms": covered,
        "missing_terms": missing,
        "claim_numbers": [number["raw"] for number in claim_numbers],
        "numbers_matched": numbers_matched,
        "numbers_missing": numbers_missing,
        "claim_years": claim_years,
        "passage_years": passage_years,
        "years_missing": years_missing,
        "years_conflicting": years_conflicting,
        "universal_terms_in_claim": universal_claim,
        "hedges_in_passage": hedges_passage,
        "possible_overgeneralization": overgeneralization,
        "polarity_mismatch": polarity_mismatch,
        "verbatim_claim": verbatim,
        "conflict_signals": conflict_signals,
        "flags": flags,
        "quote": quote,
    }


def decide_verification(
    claim_text: str,
    citations: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Decide the verification status from citations + checked candidates.

    Pure and documented; the caller may override with a validated model
    assessment. Conservative by construction: contradictions win, unresolved
    disagreements are reported as conflicts, and thresholds are explicit.
    """
    relations = {citation.get("relation", "") for citation in citations}
    contradictions = [c for c in citations if c.get("relation") == "contradicts"]
    supports = [
        c for c in citations if c.get("relation") in ("supports", "partially_supports")
    ]

    if contradictions:
        contradiction_groups = {
            c.get("independence_group") or f"source:{c.get('source_id')}" for c in contradictions
        }
        support_groups = {
            c.get("independence_group") or f"source:{c.get('source_id')}" for c in supports
        }
        independent_support = bool(support_groups - contradiction_groups)
        if independent_support:
            return {
                "status": "conflicting_evidence",
                "method": "deterministic_checks",
                "confidence": "low",
                "rationale": (
                    f"{len(supports)} supporting and {len(contradictions)} contradicting "
                    "citation(s) from independent sources; the disagreement is unresolved."
                ),
                "limitations": [
                    "Credible sources disagree; no stored evidence decides which interpretation is right.",
                ],
            }
        return {
            "status": "contradicted",
            "method": "deterministic_checks",
            "confidence": "medium",
            "rationale": (
                f"{len(contradictions)} contradicting citation(s) are attached and no "
                "independent supporting evidence is attached."
            ),
            "limitations": [],
        }

    if not candidates:
        return {
            "status": "unverifiable",
            "method": "deterministic_checks",
            "confidence": "low",
            "rationale": (
                "No stored passage for this run could be compared with the claim; the "
                "available evidence is insufficient to evaluate it."
            ),
            "limitations": ["Nothing relevant to this claim was collected in the run."],
        }

    ranked = sorted(candidates, key=lambda item: (-item["checks"]["coverage"], item["passage_id"]))
    best = ranked[0]
    best_coverage = best["checks"]["coverage"]
    if best_coverage < MIN_RELEVANCE:
        return {
            "status": "unverifiable",
            "method": "source_text_comparison",
            "confidence": "low",
            "rationale": (
                "No stored passage covers the claim's terms closely enough to evaluate it "
                f"(best coverage {best_coverage:.0%} < {MIN_RELEVANCE:.0%})."
            ),
            "limitations": ["The collected evidence does not discuss this claim."],
            "best_passage_id": best["passage_id"],
        }

    best_group = best.get("independence_group") or f"source:{best['source_id']}"
    conflicting = [
        candidate
        for candidate in ranked[1:]
        if candidate["checks"]["conflict_signals"]
        and (candidate.get("independence_group") or f"source:{candidate['source_id']}") != best_group
    ]
    best_blocks_support = bool(best["checks"]["flags"])

    # The best-covering passage discusses the same subject but disagrees on
    # numbers/dates: that is a contradiction, unless an independent passage
    # supports the claim — then the disagreement stays open.
    best_conflict = bool(best["checks"]["conflict_signals"]) and best_coverage >= 0.60
    supporting_others = [
        candidate
        for candidate in ranked[1:]
        if candidate["checks"]["coverage"] >= PARTIAL_THRESHOLD
        and not candidate["checks"]["conflict_signals"]
        and (candidate.get("independence_group") or f"source:{candidate['source_id']}") != best_group
    ]
    if best_conflict:
        signals = ", ".join(best["checks"]["conflict_signals"])
        if supporting_others:
            other = supporting_others[0]
            return {
                "status": "conflicting_evidence",
                "method": "source_text_comparison",
                "confidence": "low",
                "rationale": (
                    f"The most relevant passage ({best['passage_id']}, coverage "
                    f"{best_coverage:.0%}) disagrees with the claim ({signals}) while an "
                    f"independent passage ({other['passage_id']}) supports it; the conflict is "
                    "left unresolved."
                ),
                "limitations": [
                    "Different dates, scopes, or methods may explain the disagreement; the "
                    "stored metadata cannot decide it.",
                ],
                "best_passage_id": best["passage_id"],
                "conflicting_passage_id": other["passage_id"],
            }
        return {
            "status": "contradicted",
            "method": "source_text_comparison",
            "confidence": "medium",
            "rationale": (
                f"The most relevant stored passage ({best['passage_id']}, coverage "
                f"{best_coverage:.0%}) covers the claim's subject but disagrees with it "
                f"({signals}), and no passage supports it."
            ),
            "limitations": ["Scope or date differences may explain the disagreement."],
            "best_passage_id": best["passage_id"],
        }

    if conflicting and best_coverage >= PARTIAL_THRESHOLD and not best_blocks_support:
        other = conflicting[0]
        return {
            "status": "conflicting_evidence",
            "method": "source_text_comparison",
            "confidence": "low",
            "rationale": (
                f"One passage supports the claim (coverage {best_coverage:.0%}) while an "
                f"independent source disagrees ({', '.join(other['checks']['conflict_signals'])}, "
                f"coverage {other['checks']['coverage']:.0%}); the conflict is left unresolved."
            ),
            "limitations": [
                "Different dates, scopes, or methods may explain the disagreement; the "
                "stored metadata cannot decide it.",
            ],
            "best_passage_id": best["passage_id"],
            "conflicting_passage_id": other["passage_id"],
        }

    if best_coverage >= SUPPORT_THRESHOLD and not best_blocks_support:
        confidence = "high" if (best["checks"]["verbatim_claim"] or best_coverage >= 0.95) else "medium"
        return {
            "status": "supported",
            "method": "source_text_comparison",
            "confidence": confidence,
            "rationale": (
                f"Passage {best['passage_id']} covers {best_coverage:.0%} of the claim's terms"
                + (
                    " and reproduces it verbatim."
                    if best["checks"]["verbatim_claim"]
                    else ", with every number and date in the claim present in the passage."
                )
            ),
            "limitations": [],
            "best_passage_id": best["passage_id"],
        }

    if best_coverage >= PARTIAL_THRESHOLD or best_blocks_support:
        reasons: list[str] = []
        if best["checks"]["missing_terms"]:
            reasons.append("missing terms: " + ", ".join(best["checks"]["missing_terms"][:6]))
        for flag in best["checks"]["flags"]:
            if flag != "minor_terms_missing":
                reasons.append(flag)
        return {
            "status": "partially_supported",
            "method": "source_text_comparison",
            "confidence": "medium",
            "rationale": (
                f"Passage {best['passage_id']} covers {best_coverage:.0%} of the claim's "
                "terms but does not establish the full wording"
                + (f" ({'; '.join(reasons)})." if reasons else ".")
            ),
            "limitations": reasons,
            "best_passage_id": best["passage_id"],
        }

    return {
        "status": "unsupported",
        "method": "source_text_comparison",
        "confidence": "low",
        "rationale": (
            f"Relevant passages exist (best coverage {best_coverage:.0%}) but none establishes "
            "the claim; no adequate supporting evidence was found."
        ),
        "limitations": ["No passage in the run states or implies this claim."],
        "best_passage_id": best["passage_id"],
    }


# ---------------------------------------------------------------------------
# Model-based verification (opt-in; structured and validated or rejected)
# ---------------------------------------------------------------------------


def verification_capability(settings: Settings | None = None) -> dict[str, Any]:
    """Report whether model-based verification is configured (never assumed)."""
    settings = settings or load_settings()
    endpoint = getattr(settings, "verification_model_endpoint", "") or ""
    model = getattr(settings, "verification_model", "") or "unspecified"
    available = bool(endpoint)
    return {
        "model_available": available,
        "model": model if available else None,
        "model_endpoint": endpoint or None,
        "reason": (
            "verification model endpoint configured"
            if available
            else "no verification model endpoint configured "
            "(AGLETRAS_VERIFICATION_MODEL_ENDPOINT); using the transparent "
            "deterministic checks only"
        ),
    }


def parse_model_verification_response(
    response: Any, allowed_passage_ids: set[int]
) -> dict[str, Any]:
    """Validate a model verification response; reject anything unstructured.

    Required shape: ``{"status": <one of VERIFICATION_STATUSES>, "rationale":
    <non-empty text>, "evidence": [<passage id from the provided candidates>,
    ...]}``; an optional ``confidence`` must be one of high/medium/low. A
    response that does not reference stored evidence is rejected — a plausible
    explanation is not verification.
    """
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError as exc:
            raise ToolError("provider_error", f"verification model returned non-JSON: {exc}") from exc
    if not isinstance(response, dict):
        raise ToolError("provider_error", "verification model returned no JSON object")
    status = str(response.get("status", "")).strip().lower()
    if status not in VERIFICATION_STATUSES:
        raise ToolError(
            "provider_error",
            f"verification model status must be one of {', '.join(VERIFICATION_STATUSES)}; "
            f"got {status!r}",
        )
    rationale = _normalize_ws(str(response.get("rationale") or ""))
    if not rationale:
        raise ToolError("provider_error", "verification model gave no rationale")
    raw_evidence = response.get("evidence")
    if not isinstance(raw_evidence, list) or not raw_evidence:
        raise ToolError(
            "provider_error",
            "verification model must cite at least one stored passage id in 'evidence'",
        )
    evidence_ids: list[int] = []
    for item in raw_evidence:
        try:
            passage_id = int(item)
        except (TypeError, ValueError) as exc:
            raise ToolError(
                "provider_error", f"verification model evidence ids must be integers; got {item!r}"
            ) from exc
        if passage_id not in allowed_passage_ids:
            raise ToolError(
                "provider_error",
                f"verification model cited passage {passage_id}, which is not one of the "
                "candidate passages provided; the assessment is rejected.",
            )
        if passage_id not in evidence_ids:
            evidence_ids.append(passage_id)
    confidence = str(response.get("confidence", "medium")).strip().lower() or "medium"
    if confidence not in CONFIDENCE_LEVELS:
        confidence = "medium"
    return {
        "status": status,
        "rationale": rationale[:_MODEL_RATIONALE_CHARS],
        "evidence_passage_ids": evidence_ids,
        "confidence": confidence,
    }


def _call_verification_model(
    claim_text: str,
    candidates: list[dict[str, Any]],
    settings: Settings,
) -> Any:
    """POST the claim + candidate passages to the configured model endpoint.

    Only reached when ``AGLETRAS_VERIFICATION_MODEL_ENDPOINT`` is set. The API
    key, when configured, is read from the environment variable named by
    ``AGLETRAS_VERIFICATION_API_KEY_ENV`` and never logged.
    """
    import os

    endpoint = getattr(settings, "verification_model_endpoint", "") or ""
    if not endpoint:
        raise ToolError("provider_error", "no verification model endpoint configured")
    api_key_env = getattr(settings, "verification_api_key_env", "") or "AGLETRAS_VERIFICATION_API_KEY"
    api_key = os.environ.get(api_key_env, "")
    model = getattr(settings, "verification_model", "") or "unspecified"
    prompt = (
        "Assess the claim against the provided stored passages only. Reply with JSON: "
        '{"status": one of ["supported","partially_supported","contradicted",'
        '"conflicting_evidence","unsupported","unverifiable"], "rationale": "why", '
        '"evidence": [passage ids you used], "confidence": "high|medium|low"}. '
        "Cite only passage ids from the provided list. Never invent evidence."
    )
    payload = {
        "model": model,
        "prompt": prompt,
        "claim": claim_text,
        "passages": [
            {
                "passage_id": candidate["passage_id"],
                "source_title": candidate.get("source_title"),
                "locator": candidate.get("locator"),
                "text": candidate.get("text", ""),
            }
            for candidate in candidates
        ],
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": settings.user_agent,
            **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.fetch_timeout) as response:
            raw = response.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise ToolError(
            "provider_error", f"verification model returned HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ToolError(
            "network_error", f"verification model endpoint unreachable: {exc}"
        ) from exc
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ToolError(
            "provider_error", f"verification model returned unparsable JSON: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Candidate gathering and independence grouping
# ---------------------------------------------------------------------------


def _origin_group(url: str | None, path: str | None, content_hash: str | None) -> tuple[str, str]:
    """(independence group, note) for one evidence source.

    Identical content is one group (pages repeating the same material are not
    independent corroboration); otherwise sources from the same site/publisher
    or the same local folder share an origin group so they are not counted as
    fully independent.
    """
    if content_hash:
        # Content hash is the strongest signal: identical text = same material.
        pass
    if url:
        try:
            parsed = urllib.parse.urlsplit(url)
            host = (parsed.hostname or "").lower()
            labels = [label for label in host.split(".") if label]
            domain = ".".join(labels[-2:]) if len(labels) >= 2 else host
            if domain:
                return f"origin:{domain}", f"same publisher/site as other {domain} sources"
        except ValueError:
            pass
    if path:
        parent = str(canonurl.normalize_local_path(path) or "").rsplit("/", 1)[0]
        if parent:
            return f"folder:{parent}", "same local folder as other sources"
    return f"content:{(content_hash or 'unknown')[:16]}", ""


def _candidate_payload(
    kind: str,
    *,
    passage_id: int,
    source_id: int,
    source_title: str,
    source_url: str | None,
    source_path: str | None,
    source_type: str | None,
    publication_date: str | None,
    retrieved_at: str | None,
    content_hash: str | None,
    locator: str,
    text: str,
    checks: dict[str, Any],
    from_citation: bool = False,
    citation_id: int | None = None,
    relation: str | None = None,
    quote: str = "",
) -> dict[str, Any]:
    group, group_note = _origin_group(source_url, source_path, content_hash)
    return {
        "kind": kind,
        "passage_id": int(passage_id),
        "source_id": int(source_id),
        "source_title": source_title,
        "source_url": source_url,
        "source_path": source_path,
        "source_type": source_type,
        "publication_date": publication_date,
        "retrieved_at": retrieved_at,
        "content_hash": content_hash,
        "locator": locator,
        "text": text,
        "independence_group": group,
        "independence_note": group_note,
        "from_citation": from_citation,
        "citation_id": citation_id,
        "relation": relation,
        "quote": quote,
        "checks": checks,
    }


def _gather_candidates(
    claim: dict[str, Any], *, run_id: int, settings: Settings, limit: int = _MAX_CANDIDATES
) -> list[dict[str, Any]]:
    """Candidate passages for one claim: attached citations + run-scoped search.

    Every candidate is checked against the exact passage text (never the title
    or URL alone). Citations already attached always participate, so an
    explicit contradiction can never be missed.
    """
    candidates: dict[int, dict[str, Any]] = {}
    for citation in claim.get("citations", []):
        checks = check_claim_against_passage(claim["claim_text"], citation.get("passage_text") or "")
        candidate = _candidate_payload(
            "citation",
            passage_id=citation["passage_id"],
            source_id=citation["source_id"],
            source_title=citation.get("source_title") or "",
            source_url=citation.get("source_url"),
            source_path=citation.get("source_path"),
            source_type=citation.get("source_type"),
            publication_date=citation.get("source_publication_date"),
            retrieved_at=citation.get("source_retrieved_at"),
            content_hash=citation.get("content_hash"),
            locator=citation.get("passage_locator") or citation.get("locator") or "",
            text=citation.get("passage_text") or "",
            checks=checks,
            from_citation=True,
            citation_id=citation.get("citation_id"),
            relation=citation.get("relation"),
            quote=citation.get("quote") or "",
        )
        candidates[int(citation["passage_id"])] = candidate

    query_terms = _terms(claim["claim_text"])
    if query_terms:
        try:
            found = evidence.search_evidence(
                claim["claim_text"], limit=limit, run_id=run_id, settings=settings
            )
        except ToolError:
            found = {"passages": []}
        for passage in found.get("passages", []):
            passage_id = int(passage["passage_id"])
            if passage_id in candidates:
                continue
            checks = check_claim_against_passage(claim["claim_text"], passage["text"])
            candidates[passage_id] = _candidate_payload(
                "search",
                passage_id=passage_id,
                source_id=int(passage["source_id"]),
                source_title=passage.get("title") or "",
                source_url=passage.get("url"),
                source_path=passage.get("path"),
                source_type=passage.get("source_type"),
                publication_date=passage.get("publication_date"),
                retrieved_at=passage.get("retrieved_at"),
                content_hash=passage.get("content_hash"),
                locator=passage.get("locator") or "",
                text=passage.get("text") or "",
                checks=checks,
            )
    ranked = sorted(
        candidates.values(),
        key=lambda item: (
            not item["from_citation"],
            -item["checks"]["coverage"],
            item["passage_id"],
        ),
    )
    return ranked[: max(1, limit)]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _verification_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "verification_id": int(row["id"]),
        "claim_id": int(row["claim_id"]),
        "run_id": int(row["run_id"]),
        "status": row["status"],
        "status_label": VERIFICATION_LABELS.get(row["status"], row["status"].upper()),
        "method": row["method"],
        "confidence": row["confidence"],
        "rationale": row["rationale"],
        "evidence": _load_object(row["evidence"]),
        "checks": _load_object(row["checks"]),
        "limitations": _load_list(row["limitations"]),
        "created_at": row["created_at"],
    }


def _save_verification(
    conn: sqlite3.Connection, claim_id: int, run_id: int, result: dict[str, Any]
) -> dict[str, Any]:
    """Insert one verification audit row and cache its status on the claim."""
    cursor = conn.execute(
        """
        INSERT INTO claim_verifications
            (claim_id, run_id, status, method, confidence, rationale, evidence, checks,
             limitations, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(claim_id),
            int(run_id),
            result["status"],
            result["method"],
            result.get("confidence", "low"),
            result.get("rationale", ""),
            _dumps(result.get("evidence", {})),
            _dumps(result.get("checks", {})),
            json.dumps(list(result.get("limitations", [])), ensure_ascii=False),
            _utc_now(),
        ),
    )
    conn.execute(
        "UPDATE claims SET verification_status = ?, verified_at = ?, updated_at = ? WHERE id = ?",
        (result["status"], _utc_now(), _utc_now(), int(claim_id)),
    )
    row = conn.execute(
        "SELECT * FROM claim_verifications WHERE id = ?", (int(cursor.lastrowid),)
    ).fetchone()
    return _verification_payload(row)


def latest_verifications_in_conn(
    conn: sqlite3.Connection, run_id: int
) -> dict[int, dict[str, Any]]:
    """Latest verification per claim of a run (``{claim_id: payload}``)."""
    rows = conn.execute(
        """
        SELECT cv.* FROM claim_verifications cv
        JOIN (
            SELECT claim_id, MAX(id) AS max_id FROM claim_verifications
            WHERE run_id = ? GROUP BY claim_id
        ) latest ON latest.max_id = cv.id
        ORDER BY cv.claim_id
        """,
        (int(run_id),),
    ).fetchall()
    return {int(row["claim_id"]): _verification_payload(row) for row in rows}


# ---------------------------------------------------------------------------
# Claim extraction and evidence mapping
# ---------------------------------------------------------------------------


def _run_source_texts(conn: sqlite3.Connection, run_id: int, *, max_chars: int) -> list[tuple[dict[str, Any], str]]:
    rows = conn.execute(
        """
        SELECT s.id, s.title, s.url, s.path, s.content FROM research_run_sources rrs
        JOIN sources s ON s.id = rrs.source_id WHERE rrs.run_id = ?
        ORDER BY s.id
        """,
        (int(run_id),),
    ).fetchall()
    collected: list[tuple[dict[str, Any], str]] = []
    remaining = max_chars
    for row in rows:
        if remaining <= 0:
            break
        text = (row["content"] or "")[:remaining]
        remaining -= len(text)
        collected.append(
            (
                {
                    "source_id": int(row["id"]),
                    "title": row["title"],
                    "url": row["url"],
                    "path": row["path"],
                },
                text,
            )
        )
    return collected


def extract_claims(
    run_id: int,
    *,
    text: str = "",
    source_ids: list[int] | None = None,
    max_claims: int = 25,
    register: bool = False,
    claim_type: str = "claim",
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Extract candidate factual statements and map them to stored evidence.

    Source material, in priority order: explicit ``text``; the contents of
    ``source_ids``; otherwise every source linked to the run. Extraction is
    deterministic (sentence splitter + marker scoring); registration is
    explicit (``register=true``) and claims always start UNVERIFIED. Each
    candidate is mapped to the stored passages discussing it, with coverage,
    locator, source provenance, and the exact quote when it occurs verbatim.
    """
    settings = settings or load_settings()
    cap = max(1, min(int(max_claims), settings.verification_max_claims, 100))
    claim_type = (claim_type or "claim").strip().lower()
    if claim_type not in evidence.CLAIM_TYPES:
        raise ToolError(
            "invalid_argument",
            f"claim_type must be one of {', '.join(evidence.CLAIM_TYPES)}; got {claim_type!r}",
        )

    segments: list[tuple[str, str]] = []  # (label, text)
    with storage.open_db(settings) as conn:
        _require_run(conn, run_id)
        if text.strip():
            segments.append(("text", text[:_MAX_EXTRACT_CHARS]))
            source_mode = "text"
        else:
            rows = []
            if source_ids:
                for source_id in source_ids:
                    row = conn.execute(
                        "SELECT * FROM sources WHERE id = ?", (int(source_id),)
                    ).fetchone()
                    if row is None:
                        raise ToolError("not_found", f"No source with id {source_id}")
                    linked = conn.execute(
                        "SELECT 1 FROM research_run_sources WHERE run_id = ? AND source_id = ?",
                        (int(run_id), int(source_id)),
                    ).fetchone()
                    if linked is None:
                        raise ToolError(
                            "invalid_argument",
                            f"Source {source_id} is not linked to research run {run_id}; "
                            "save it as evidence for this run first.",
                        )
                    rows.append(row)
            else:
                rows = [
                    conn.execute("SELECT * FROM sources WHERE id = ?", (int(row["source_id"]),)).fetchone()
                    for row in conn.execute(
                        "SELECT source_id FROM research_run_sources WHERE run_id = ? ORDER BY source_id",
                        (int(run_id),),
                    ).fetchall()
                ]
            remaining = _MAX_EXTRACT_CHARS
            for row in rows:
                if row is None or remaining <= 0:
                    continue
                content = (row["content"] or "")[:remaining]
                remaining -= len(content)
                segments.append((row["title"] or f"source {row['id']}", content))
            source_mode = "sources"

    combined = "\n\n".join(segment_text for _label, segment_text in segments)
    candidates = extract_candidates(combined, max_claims=cap)

    registered: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for candidate in candidates:
        mapping: list[dict[str, Any]] = []
        query_terms = _terms(candidate["text"])
        if query_terms:
            try:
                found = evidence.search_evidence(
                    candidate["text"], limit=3, run_id=run_id, settings=settings
                )
            except ToolError:
                found = {"passages": []}
            for passage in found.get("passages", []):
                checks = check_claim_against_passage(candidate["text"], passage["text"])
                entry = _candidate_payload(
                    "search",
                    passage_id=int(passage["passage_id"]),
                    source_id=int(passage["source_id"]),
                    source_title=passage.get("title") or "",
                    source_url=passage.get("url"),
                    source_path=passage.get("path"),
                    source_type=passage.get("source_type"),
                    publication_date=passage.get("publication_date"),
                    retrieved_at=passage.get("retrieved_at"),
                    content_hash=passage.get("content_hash"),
                    locator=passage.get("locator") or "",
                    text=passage.get("text") or "",
                    checks=checks,
                )
                mapping.append(
                    {
                        "passage_id": entry["passage_id"],
                        "source_id": entry["source_id"],
                        "source_title": entry["source_title"],
                        "source_url": entry["source_url"],
                        "source_path": entry["source_path"],
                        "locator": entry["locator"],
                        "coverage": checks["coverage"],
                        "quote": (
                            candidate["text"] if checks["verbatim_claim"]
                            else (checks.get("quote") or {}).get("text", "")
                        ),
                        "verbatim": checks["verbatim_claim"],
                    }
                )
        results_entry: dict[str, Any] = {
            **candidate,
            "evidence_matches": mapping,
            "best_evidence": mapping[0] if mapping else None,
        }
        if register:
            claim = evidence.register_claim(
                run_id,
                candidate["text"],
                claim_type=claim_type,
                note=f"extracted from {source_mode} by deterministic marker scoring",
                settings=settings,
            )
            results_entry["claim_id"] = claim["claim_id"]
            results_entry["claim_status"] = claim["status"]
            registered.append({"candidate_id": candidate["candidate_id"], "claim_id": claim["claim_id"]})
        results.append(results_entry)

    result = {
        "run_id": int(run_id),
        "source_mode": source_mode,
        "segments": len(segments),
        "candidate_count": len(results),
        "candidates": results,
        "registered": registered,
        "registration_requested": bool(register),
        "claim_type": claim_type,
        "extraction_method": "deterministic_sentence_marker_scoring",
        "note": (
            "Extraction finds candidate statements only. It does not verify them and "
            "never registers support; run verify_claims against stored evidence."
        ),
    }
    with storage.open_db(settings) as conn:
        _require_run(conn, run_id)
        planner.log_event(
            run_id,
            "decision",
            f"claim extraction: {len(results)} candidate(s), {len(registered)} registered",
            payload={"source_mode": source_mode, "candidates": len(results)},
            settings=settings,
        )
    return result


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _attach_support(
    claim: dict[str, Any], candidate: dict[str, Any], relation: str, method: str, settings: Settings
) -> dict[str, Any] | None:
    """Attach one verification-driven citation (deterministic, documented).

    Only ``supports`` (full match) and ``partially_supports`` (partial match)
    are attached, and only when no support citation exists yet. Contradictions
    and conflicts are never auto-attached: they stay visible as verification
    results for a human/orchestrator decision.
    """
    existing_relations = {citation.get("relation") for citation in claim.get("citations", [])}
    if existing_relations & {"supports", "partially_supports", "contradicts"}:
        return None
    quote = claim["claim_text"] if candidate["checks"]["verbatim_claim"] else (
        (candidate["checks"].get("quote") or {}).get("text", "")
    )
    metadata = {
        "verified_by": "agletras_phase6",
        "method": method,
        "coverage": candidate["checks"]["coverage"],
        "passage_id": candidate["passage_id"],
    }
    try:
        attached = evidence.attach_citation(
            claim["claim_id"],
            candidate["passage_id"],
            source_id=candidate["source_id"],
            quote=quote,
            relation=relation,
            metadata=metadata,
            settings=settings,
        )
    except ToolError as exc:  # pragma: no cover - defensive: candidates come from stored passages
        LOGGER.warning(
            "could not attach verification citation to claim %s: %s", claim["claim_id"], exc.message
        )
        return None
    return attached


def verify_claims(
    run_id: int,
    *,
    claim_ids: list[int] | None = None,
    apply_citations: bool = True,
    method: str = "auto",
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Evaluate claims of a run against the stored evidence.

    Each claim is compared with the exact text of its candidate passages
    (attached citations always participate; additional passages come from the
    run-scoped passage search). The result is an explicit status from
    :data:`VERIFICATION_STATUSES` with method, confidence, rationale,
    supporting/conflicting passage references, the deterministic checks, and
    the limitations of the method. With ``apply_citations=true`` (default),
    a *decisive* full or partial match additionally attaches the corresponding
    ``supports``/``partially_supports`` citation with the exact quote and the
    method recorded — nothing is ever attached for weak, absent, or
    contradictory evidence.

    ``method`` is ``"auto"`` (use the configured verification model when one
    exists, else deterministic checks) or ``"deterministic"`` (never call a
    model). Model responses are validated against the stored passages; an
    unavailable or invalid model falls back to the deterministic result with
    the failure recorded.
    """
    settings = settings or load_settings()
    method = (method or "auto").strip().lower()
    if method not in ("auto", "deterministic"):
        raise ToolError("invalid_argument", f"method must be 'auto' or 'deterministic', got {method!r}")
    caps = verification_capability(settings)
    use_model = method == "auto" and caps["model_available"]
    max_claims = settings.verification_max_claims

    with storage.open_db(settings) as conn:
        _require_run(conn, run_id)
    claims = evidence.claims_with_citations(run_id, settings=settings)
    if claim_ids:
        wanted = {int(claim_id) for claim_id in claim_ids}
        claims = [claim for claim in claims if claim["claim_id"] in wanted]
        missing = wanted - {claim["claim_id"] for claim in claims}
        if missing:
            raise ToolError(
                "not_found",
                f"Claim(s) {', '.join(str(item) for item in sorted(missing))} do not belong "
                f"to research run {run_id}.",
            )
    if len(claims) > max_claims:
        raise ToolError(
            "limit_reached",
            f"Research run {run_id} has {len(claims)} claims; one verification call is "
            f"bounded to {max_claims} (AGLETRAS_VERIFICATION_MAX_CLAIMS). Verify a subset "
            "with claim_ids.",
        )

    results: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    methods_used: dict[str, int] = {}
    for claim in claims:
        candidates = _gather_candidates(
            claim,
            run_id=int(run_id),
            settings=settings,
            limit=max(1, int(settings.verification_max_candidates)),
        )

        deterministic = decide_verification(claim["claim_text"], claim.get("citations", []), candidates)
        status = deterministic["status"]
        final_method = deterministic["method"]
        confidence = deterministic["confidence"]
        rationale = deterministic["rationale"]
        limitations = list(deterministic.get("limitations", []))
        model_record: dict[str, Any] | None = None

        if use_model and candidates:
            allowed = {candidate["passage_id"] for candidate in candidates}
            try:
                raw = _call_verification_model(claim["claim_text"], candidates[:6], settings)
                parsed = parse_model_verification_response(raw, allowed)
            except ToolError as exc:
                model_record = {
                    "attempted": True,
                    "available": False,
                    "error": {"code": exc.code, "message": exc.message},
                    "assessment": None,
                }
                final_method = "model_fallback_deterministic"
                limitations.append(
                    f"model-based verification unavailable ({exc.code}); the deterministic "
                    "assessment is reported instead"
                )
            else:
                # A model assessment never overrides a decisive contradiction
                # established by explicit citation relations.
                if deterministic["status"] == "contradicted" and parsed["status"] != "contradicted":
                    model_record = {
                        "attempted": True,
                        "available": True,
                        "assessment": parsed,
                        "note": (
                            "model assessment recorded but the deterministic contradiction "
                            "(explicit contradicts citation) is reported as the status"
                        ),
                    }
                else:
                    status = parsed["status"]
                    final_method = "model"
                    confidence = parsed["confidence"]
                    rationale = parsed["rationale"]
                    model_record = {"attempted": True, "available": True, "assessment": parsed}

        best = None
        ranked = sorted(candidates, key=lambda item: (-item["checks"]["coverage"], item["passage_id"]))
        if ranked:
            best = ranked[0]

        evidence_payload: dict[str, Any] = {
            "passages_checked": len(candidates),
            "supporting_passage_ids": [
                candidate["passage_id"]
                for candidate in ranked
                if candidate["checks"]["coverage"] >= PARTIAL_THRESHOLD
                and not candidate["checks"]["conflict_signals"]
            ][:5],
            "conflicting_passage_ids": [
                candidate["passage_id"]
                for candidate in ranked
                if candidate["checks"]["conflict_signals"]
            ][:5],
            "best_passage": (
                {
                    "passage_id": best["passage_id"],
                    "source_id": best["source_id"],
                    "source_title": best["source_title"],
                    "source_url": best["source_url"],
                    "source_path": best["source_path"],
                    "locator": best["locator"],
                    "coverage": best["checks"]["coverage"],
                    "quote": (
                        claim["claim_text"]
                        if best["checks"]["verbatim_claim"]
                        else (best["checks"].get("quote") or {}).get("text", "")
                    ),
                    "content_hash": best["content_hash"],
                }
                if best
                else None
            ),
            "candidates": [
                {
                    "passage_id": candidate["passage_id"],
                    "source_id": candidate["source_id"],
                    "source_title": candidate["source_title"],
                    "source_url": candidate["source_url"],
                    "source_path": candidate["source_path"],
                    "source_type": candidate["source_type"],
                    "publication_date": candidate["publication_date"],
                    "locator": candidate["locator"],
                    "content_hash": candidate["content_hash"],
                    "independence_group": candidate["independence_group"],
                    "from_citation": candidate["from_citation"],
                    "relation": candidate["relation"],
                    "checks": candidate["checks"],
                }
                for candidate in candidates
            ],
        }

        attached_citation: dict[str, Any] | None = None
        if apply_citations and best is not None:
            if status == "supported":
                attached_citation = _attach_support(claim, best, "supports", final_method, settings)
            elif status == "partially_supported":
                attached_citation = _attach_support(
                    claim, best, "partially_supports", final_method, settings
                )

        result = {
            "status": status,
            "method": final_method,
            "confidence": confidence,
            "rationale": rationale,
            "limitations": limitations,
            "evidence": evidence_payload,
            "checks": {
                "deterministic_assessment": deterministic,
                "thresholds": {
                    "min_relevance": MIN_RELEVANCE,
                    "partial": PARTIAL_THRESHOLD,
                    "support": SUPPORT_THRESHOLD,
                },
                "method_requested": method,
                "model": model_record,
            },
        }
        with storage.open_db(settings) as conn:
            payload = _save_verification(conn, int(claim["claim_id"]), int(run_id), result)
        results.append(
            {
                "claim_id": int(claim["claim_id"]),
                "claim_text": claim["claim_text"],
                "claim_type": claim.get("claim_type", "claim"),
                "derived_support_status": claim["status"],
                "verification": payload,
                "citation_attached": attached_citation["citation"]["citation_id"]
                if attached_citation
                else None,
                "citation_relation": (
                    attached_citation["citation"]["relation"] if attached_citation else None
                ),
                "derived_status_after": (
                    attached_citation["claim"]["status"] if attached_citation else claim["status"]
                ),
            }
        )
        counts[status] = counts.get(status, 0) + 1
        methods_used[final_method] = methods_used.get(final_method, 0) + 1
        planner.log_event(
            int(run_id),
            "claim",
            f"claim {claim['claim_id']} verified: {status} ({final_method})",
            payload={
                "claim_id": int(claim["claim_id"]),
                "status": status,
                "method": final_method,
                "citation_attached": bool(attached_citation),
            },
            settings=settings,
        )

    return {
        "run_id": int(run_id),
        "verified_count": len(results),
        "counts_by_status": counts,
        "methods_used": methods_used,
        "model_configured": caps["model_available"],
        "model_available": caps["model_available"],
        "method_requested": method,
        "deterministic_only": not caps["model_available"] or method == "deterministic",
        "claims": results,
        "note": (
            "Verification statuses are assessments over the stored passages, not proof: "
            "statuses record what the deterministic checks (or the configured model, with "
            "stored-evidence references) could establish, and every limitation stays visible."
        ),
    }


# ---------------------------------------------------------------------------
# Source quality (kept separate from claim verification statuses)
# ---------------------------------------------------------------------------


def _directness_heuristic(url: str | None, source_type: str | None) -> tuple[str, str]:
    if source_type in ("pdf", "csv", "docx", "xlsx", "markdown", "text"):
        return "primary_document", "non-web document type"
    lowered = (url or "").lower()
    if any(hint in lowered for hint in _PRIMARY_HINTS):
        return "likely_primary", "URL path suggests a document/data publication"
    if any(hint in lowered for hint in _SECONDARY_HINTS):
        return "likely_secondary", "URL path suggests reporting/summary content"
    if source_type == "search_result":
        return "snippet", "search-result snippet only (not a fetched source)"
    return "unknown", "no reliable primary/secondary signal in stored metadata"


def _age_days(publication_date: str | None, now: datetime) -> float | None:
    parsed = evidence._parse_iso(publication_date)
    if parsed is None:
        return None
    return (now - parsed).total_seconds() / 86_400


def source_quality_in_conn(
    conn: sqlite3.Connection, run_id: int, *, now: datetime | None = None
) -> dict[str, Any]:
    """Metadata-driven source-quality indicators for one run's sources.

    Heuristics only — clearly labelled as such and *separate* from claim
    verification statuses. Popularity/ranking is never treated as truth, and
    identical content (syndication/copies) is grouped so repeated material is
    not double-counted as independent corroboration.
    """
    moment = now or datetime.now(timezone.utc)
    run_row = conn.execute("SELECT * FROM research_runs WHERE id = ?", (int(run_id),)).fetchone()
    if run_row is None:
        raise ToolError("not_found", f"No research run with id {run_id}")
    query_terms = _terms(run_row["query"])
    rows = conn.execute(
        """
        SELECT s.* FROM research_run_sources rrs JOIN sources s ON s.id = rrs.source_id
        WHERE rrs.run_id = ? ORDER BY s.id
        """,
        (int(run_id),),
    ).fetchall()

    hashes: dict[str, list[int]] = {}
    for row in rows:
        hashes.setdefault(row["sha256"] or "", []).append(int(row["id"]))
    content_duplicates = {
        digest: ids for digest, ids in hashes.items() if digest and len(ids) > 1
    }

    sources: list[dict[str, Any]] = []
    groups: dict[str, list[int]] = {}
    cautions: list[str] = []
    for row in rows:
        source_id = int(row["id"])
        text_sample = (row["content"] or "")[:20_000]
        relevance = 0.0
        if query_terms:
            covered = sum(1 for term in query_terms if _term_present(term, text_sample.lower()))
            relevance = round(covered / len(query_terms), 4)
        directness, directness_basis = _directness_heuristic(row["url"], row["source_type"])
        age = _age_days(row["publication_date"], moment)
        stale = age is not None and age > 730  # older than ~2 years: potentially stale
        lowered = text_sample.lower()
        methodology_terms = [cue for cue in _METHODOLOGY_CUES if cue in lowered]
        group, group_note = _origin_group(row["url"], row["path"], row["sha256"])
        groups.setdefault(group, []).append(source_id)
        duplicate_of = [
            other_id
            for other_id in content_duplicates.get(row["sha256"] or "", [])
            if other_id != source_id
        ]
        entry = {
            "source_id": source_id,
            "title": row["title"],
            "url": row["url"],
            "path": row["path"],
            "source_type": row["source_type"],
            "publication_date": row["publication_date"],
            "retrieved_at": row["retrieved_at"],
            "content_hash": row["sha256"],
            "relevance_to_run_query": relevance,
            "directness": directness,
            "directness_basis": directness_basis,
            "age_days": round(age, 1) if age is not None else None,
            "potentially_stale": stale,
            "methodology_keywords": methodology_terms[:8],
            "independence_group": group,
            "independence_note": group_note,
            "duplicate_content_of": duplicate_of,
            "has_publication_date": bool(row["publication_date"]),
            "has_url_or_path": bool(row["url"] or row["path"]),
        }
        if stale:
            cautions.append(
                f"source {source_id} was published {int(age)} days ago and may be stale for "
                "time-sensitive questions"
            )
        if duplicate_of:
            cautions.append(
                f"source {source_id} repeats the same content as source(s) "
                f"{', '.join(str(item) for item in duplicate_of)}; do not count them as "
                "independent corroboration"
            )
        if not row["publication_date"]:
            cautions.append(
                f"source {source_id} has no publication date; recency cannot be assessed"
            )
        sources.append(entry)

    group_payload = [
        {"independence_group": group, "source_ids": sorted(ids), "count": len(ids)}
        for group, ids in sorted(groups.items())
        if len(ids) > 1
    ]
    return {
        "run_id": int(run_id),
        "source_count": len(sources),
        "sources": sources,
        "shared_independence_groups": group_payload,
        "duplicate_content_groups": [
            {"content_hash": digest, "source_ids": ids}
            for digest, ids in sorted(content_duplicates.items())
        ],
        "cautions": cautions,
        "note": (
            "Quality indicators are heuristics over stored metadata. They are kept separate "
            "from claim verification statuses; popularity or ranking is never treated as "
            "truth, and copies of the same underlying material are not independent evidence."
        ),
    }


def assess_sources(run_id: int, *, settings: Settings | None = None) -> dict[str, Any]:
    """Public wrapper around :func:`source_quality_in_conn` for one run."""
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        return source_quality_in_conn(conn, int(run_id))


# ---------------------------------------------------------------------------
# Conflict analysis
# ---------------------------------------------------------------------------


def conflicts_in_conn(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    """Every disagreement visible in the stored evidence for one run.

    Three sources of conflict, all kept unresolved unless the evidence itself
    settles them: claims with both supporting and contradicting citations,
    verification results that detected number/date disagreements between
    independent sources, and planner subquestions explicitly marked
    ``conflicting``.
    """
    claims = evidence._claims_with_citations(conn, int(run_id))
    verifications = latest_verifications_in_conn(conn, int(run_id))
    conflicts: list[dict[str, Any]] = []

    def side(citation: dict[str, Any]) -> dict[str, Any]:
        group, group_note = _origin_group(
            citation.get("source_url"), citation.get("source_path"), citation.get("content_hash")
        )
        return {
            "relation": citation.get("relation"),
            "citation_id": citation.get("citation_id"),
            "source_id": citation.get("source_id"),
            "source_title": citation.get("source_title"),
            "source_url": citation.get("source_url"),
            "source_path": citation.get("source_path"),
            "source_type": citation.get("source_type"),
            "publication_date": citation.get("source_publication_date"),
            "retrieved_at": citation.get("source_retrieved_at"),
            "locator": citation.get("passage_locator") or citation.get("locator") or "",
            "quote": citation.get("quote") or "",
            "content_hash": citation.get("content_hash"),
            "independence_group": group,
            "independence_note": group_note,
        }

    for claim in claims:
        supporting = [c for c in claim["citations"] if c.get("relation") in ("supports", "partially_supports")]
        contradicting = [c for c in claim["citations"] if c.get("relation") == "contradicts"]
        if supporting and contradicting:
            support_groups = {side(c)["independence_group"] for c in supporting}
            contradiction_groups = {side(c)["independence_group"] for c in contradicting}
            independent = bool(support_groups - contradiction_groups)
            sides = [side(c) for c in supporting] + [side(c) for c in contradicting]
            dates = sorted(
                {str(item["publication_date"])[:10] for item in sides if item["publication_date"]}
            )
            types = sorted({str(item["source_type"]) for item in sides if item["source_type"]})
            conflicts.append(
                {
                    "kind": "citation_relations",
                    "claim_id": claim["claim_id"],
                    "claim_text": claim["claim_text"],
                    "status": "conflicting_evidence" if independent else "contradicted",
                    "sides": sides,
                    "differences": {
                        "publication_dates": dates,
                        "source_types": types,
                        "independence": (
                            "sources are independent of each other"
                            if independent
                            else "the supporting and contradicting citations trace to the same "
                            "underlying material/publisher, so this is not an independent conflict"
                        ),
                    },
                    "resolved": False if independent else True,
                    "resolution": (
                        "unresolved: the stored evidence contains both support and contradiction "
                        "from independent sources; no stored rule or metadata selects the right one"
                        if independent
                        else "contradicted by evidence with no independent support"
                    ),
                }
            )
        elif contradicting:
            sides = [side(c) for c in contradicting]
            conflicts.append(
                {
                    "kind": "citation_relations",
                    "claim_id": claim["claim_id"],
                    "claim_text": claim["claim_text"],
                    "status": "contradicted",
                    "sides": sides,
                    "differences": {"publication_dates": [], "source_types": [], "independence": "n/a"},
                    "resolved": True,
                    "resolution": "contradicted by the attached evidence; no supporting citation",
                }
            )

    for claim in claims:
        verification = verifications.get(claim["claim_id"])
        if verification is None:
            continue
        if verification["status"] == "conflicting_evidence":
            evidence_block = verification.get("evidence", {})
            conflicts.append(
                {
                    "kind": "verification",
                    "claim_id": claim["claim_id"],
                    "claim_text": claim["claim_text"],
                    "status": "conflicting_evidence",
                    "sides": _verification_sides(evidence_block),
                    "differences": {
                        "basis": verification.get("rationale", ""),
                        "deterministic": (
                            verification.get("checks", {}).get("deterministic_assessment", {})
                        ),
                    },
                    "resolved": False,
                    "resolution": (
                        "unresolved: independent stored sources disagree; scope, date, or "
                        "method differences cannot be decided from the stored metadata"
                    ),
                }
            )
        elif verification["status"] == "contradicted":
            evidence_block = verification.get("evidence", {})
            conflicts.append(
                {
                    "kind": "verification",
                    "claim_id": claim["claim_id"],
                    "claim_text": claim["claim_text"],
                    "status": "contradicted",
                    "sides": _verification_sides(evidence_block),
                    "differences": {"basis": verification.get("rationale", "")},
                    "resolved": False,
                    "resolution": (
                        "contradicted by the compared evidence; the underlying scope/date "
                        "difference (if any) is not resolvable from stored metadata"
                    ),
                }
            )

    subquestion_rows = conn.execute(
        "SELECT * FROM research_subquestions WHERE run_id = ? AND status = 'conflicting' ORDER BY seq",
        (int(run_id),),
    ).fetchall()
    for row in subquestion_rows:
        conflicts.append(
            {
                "kind": "subquestion",
                "subquestion_id": int(row["id"]),
                "question": row["question"],
                "status": "conflicting",
                "sides": [],
                "differences": {"note": row["note"]},
                "resolved": False,
                "resolution": "the research plan marks this question as contested",
            }
        )

    conflicts.sort(key=lambda item: (item.get("claim_id", 10**9), item["kind"]))
    independent_count = sum(
        1 for item in conflicts if item["status"] in ("conflicting_evidence",) and not item["resolved"]
    )
    return {
        "run_id": int(run_id),
        "count": len(conflicts),
        "unresolved_count": sum(1 for item in conflicts if not item["resolved"]),
        "independent_conflict_count": independent_count,
        "conflicts": conflicts,
        "note": (
            "Conflicts stay visible and are never resolved by preference: not by popularity, "
            "not by simply choosing the newest source. When the stored evidence cannot decide, "
            "the conflict is reported as unresolved."
        ),
    }


def _verification_sides(evidence_block: dict[str, Any]) -> list[dict[str, Any]]:
    sides: list[dict[str, Any]] = []
    supporting = set(evidence_block.get("supporting_passage_ids") or [])
    conflicting = set(evidence_block.get("conflicting_passage_ids") or [])
    for candidate in evidence_block.get("candidates", [])[:8]:
        passage_id = int(candidate.get("passage_id", 0))
        if passage_id in supporting:
            relation = "supports (check coverage)"
        elif passage_id in conflicting:
            relation = "conflicts (" + ", ".join(candidate.get("checks", {}).get("conflict_signals", [])) + ")"
        else:
            continue
        sides.append(
            {
                "relation": relation,
                "passage_id": passage_id,
                "source_id": candidate.get("source_id"),
                "source_title": candidate.get("source_title"),
                "source_url": candidate.get("source_url"),
                "source_path": candidate.get("source_path"),
                "source_type": candidate.get("source_type"),
                "publication_date": candidate.get("publication_date"),
                "locator": candidate.get("locator"),
                "quote": (candidate.get("checks", {}).get("quote") or {}).get("text", ""),
                "coverage": candidate.get("checks", {}).get("coverage"),
                "content_hash": candidate.get("content_hash"),
                "independence_group": candidate.get("independence_group"),
            }
        )
    return sides


def analyze_conflicts(
    run_id: int, *, include_sources: bool = True, settings: Settings | None = None
) -> dict[str, Any]:
    """Identify claims with conflicting evidence and source-linked findings.

    Returns every conflict with the evidence on each side (source, date,
    locator, quote, content hash, independence group), the differences that
    may explain it (dates, scopes, source types), whether it remains
    unresolved, and — when requested — the source-quality indicators for the
    run's sources (heuristics, kept separate from verification statuses).
    """
    settings = settings or load_settings()
    with storage.open_db(settings) as conn:
        result = conflicts_in_conn(conn, int(run_id))
        payload: dict[str, Any] = dict(result)
        if include_sources:
            payload["source_assessment"] = source_quality_in_conn(conn, int(run_id))
    planner.log_event(
        int(run_id),
        "decision",
        f"conflict analysis: {result['count']} conflict(s), {result['unresolved_count']} unresolved",
        payload={"conflicts": result["count"], "unresolved": result["unresolved_count"]},
        settings=settings,
    )
    return payload

"""Generate the labelled Phase 6 sample report at samples/phase6-sample-report.md.

Runs the real Phase 6 pipeline (evidence library -> claim extraction ->
verification -> conflict analysis -> synthesis report) inside a throwaway
directory under /tmp, then copies the rendered Markdown into samples/ with a
sample label prepended. No network access and no external services: every
"source" is synthetic text or a synthetic image metadata record, and all URLs
use reserved example domains.

Usage:
    .venv/bin/python scripts/make_phase6_sample.py
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

WORK = Path("/tmp/opencode/phase6-sample")
shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)

os.environ["AGLETRAS_HOME"] = str(WORK)
os.environ["AGLETRAS_DATA_DIR"] = str(WORK / "data")
os.environ["AGLETRAS_DB_PATH"] = str(WORK / "data" / "research.db")
os.environ["AGLETRAS_REPORTS_DIR"] = str(WORK / "reports")
os.environ["AGLETRAS_CACHE_DIR"] = str(WORK / "cache")
os.environ["AGLETRAS_LOCAL_ROOTS"] = str(WORK)
for key in list(os.environ):
    if key.startswith("AGLETRAS_VERIFICATION_"):
        del os.environ[key]

from agletras_mcp import evidence, images, planner, synthesis, storage, verification  # noqa: E402
from agletras_mcp.config import load_settings  # noqa: E402

settings = load_settings()

SOURCE_A = """Widget Mark III — Technical Specification (synthetic sample)

The Widget Mark III is a compact measuring instrument. The Widget Mark III was released in 2019 and replaced the Mark II in the manufacturer's catalogue.

The Widget Mark III weighs 1.2 kg and its housing is made of anodised aluminium. The Widget Mark III operates at ambient temperatures up to 45 °C and ships with a 5000 mAh battery. A full charge takes 90 minutes with the included dock.

Connectivity includes Wi-Fi 6 and Bluetooth 5.2. The Widget Mark III is manufactured in Smalltown by Example Industries.
"""

SOURCE_B = """Widget Mark III — Independent Field Review (synthetic sample)

The Widget Mark III launched in 2021, two years later than the dates printed on early promotional material. In our lab the Widget Mark III weighed 1.4 kg, somewhat heavier than the figure quoted by the manufacturer.

Field tests confirmed that the Widget Mark III operates at ambient temperatures up to 45 °C. Battery endurance was 11 hours of continuous use in our standard test.

The review unit was a retail sample purchased in 2021, funded from the reviewers' own budget.
"""

# --- 1. Run, plan, and evidence --------------------------------------------
run = evidence.start_run(
    "What are the documented properties of the Widget Mark III?",
    config={"note": "synthetic Phase 6 sample"},
    settings=settings,
)
run_id = run["run_id"]

plan = planner.save_plan(
    run_id,
    objective=(
        "Summarize the documented properties of the Widget Mark III (release date, "
        "physical and operating specifications), keeping disagreements visible."
    ),
    scope="Product documentation and independent reviews; synthetic sample data only.",
    assumptions="Example Industries and the Widget Mark III are fictional sample entities.",
    subquestions=[
        "What release date and physical specifications does the Widget Mark III have?",
        "What operating temperature range does the Widget Mark III support?",
        "What battery and charging characteristics does the Widget Mark III have?",
    ],
    settings=settings,
)
sub_q1, sub_q2, sub_q3 = [item["subquestion_id"] for item in plan["subquestions"]]

evidence.save_evidence(
    run_id=run_id,
    text=SOURCE_A,
    title="Widget Mark III — Technical Specification (synthetic)",
    url="https://manufacturer.example/widgets/mark-iii/spec",
    settings=settings,
)
evidence.save_evidence(
    run_id=run_id,
    text=SOURCE_B,
    title="Widget Mark III — Independent Field Review (synthetic)",
    url="https://review.example.org/widget-mark-iii-field-review",
    settings=settings,
)

# Synthetic image discovery record so the sample shows the attribution section.
image = images._store_image_as_evidence(
    images.ImageMetadata(
        image_url="https://upload.example.org/widgets/mark-iii-product.png",
        source_page_url="https://manufacturer.example/widgets/mark-iii/gallery",
        title="Widget Mark III product photograph (synthetic sample)",
        creator="Sample Photographer",
        retrieved_at="2026-10-18T09:00:00Z",
        width=800,
        height=600,
        mime_type="image/png",
        license_name="CC BY-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-sa/4.0/",
        attribution="Sample Photographer / CC BY-SA 4.0 (synthetic sample record)",
        provider="wikimedia_commons",
        missing_metadata=[],
    ),
    settings=settings,
)
with storage.open_db(settings) as conn:
    evidence._associate_run(conn, run_id, image["source_id"])

# --- 2. Candidate extraction (demonstration pass; nothing registered) ------
verification.extract_claims(run_id, max_claims=10, register=False, settings=settings)

# --- 3. Claims (including one inference with stated reasoning) -------------
claims: dict[str, int] = {}
for text, claim_type, note in (
    ("The Widget Mark III operates at ambient temperatures up to 45 °C.", "claim", ""),
    ("The Widget Mark III was released in 2019.", "claim", ""),
    (
        "The Widget Mark III supports wireless charging and has a 5000 mAh battery.",
        "claim",
        "",
    ),
    (
        "A companion mobile app requires a paid subscription to unlock all measurement modes.",
        "claim",
        "",
    ),
    (
        "The heavier quoted figure suggests a different production batch.",
        "inference",
        "Inference from the stored figures (1.2 kg quoted vs 1.4 kg measured in the lab); "
        "neither source states a cause.",
    ),
):
    claim = evidence.register_claim(
        run_id, text, claim_type=claim_type, note=note, settings=settings
    )
    claims[text] = claim["claim_id"]

# One manual context citation, so the sample shows a human-attached relation.
inference_id = claims["The heavier quoted figure suggests a different production batch."]
found = evidence.search_evidence("weighed 1.4 kg", run_id=run_id, settings=settings)
b_passage = next(item for item in found["passages"] if "1.4 kg" in item["text"])
evidence.attach_citation(
    inference_id,
    b_passage["passage_id"],
    source_id=b_passage["source_id"],
    quote="In our lab the Widget Mark III weighed 1.4 kg",
    relation="context",
    settings=settings,
)

# --- 4. Verification + conflict analysis -----------------------------------
verification.verify_claims(run_id, apply_citations=True, method="deterministic", settings=settings)
verification.analyze_conflicts(run_id, include_sources=True, settings=settings)

# --- 5. Subquestion evaluation + stopping reason ----------------------------
planner.update_subquestion(
    run_id,
    sub_q1,
    "conflicting",
    note="Manufacturer states 2019; the independent review states 2021. Left unresolved.",
    settings=settings,
)
planner.update_subquestion(
    run_id,
    sub_q2,
    "answered",
    note="Both stored sources agree the operating limit is 45 °C.",
    settings=settings,
)
planner.update_subquestion(
    run_id,
    sub_q3,
    "partially_answered",
    note="Battery capacity confirmed; charging details rest on the manufacturer alone.",
    settings=settings,
)
planner.log_event(
    run_id,
    "stopping",
    "Stopped after the planned subquestions were evaluated: two resolved, one left "
    "conflicting (release date) with the disagreement documented in the report.",
    payload={"status": "completed", "reason": "planned subquestions evaluated"},
    settings=settings,
)
evidence.finish_run(run_id, status="completed", settings=settings)

# --- 6. Synthesis report (validated) ----------------------------------------
report = synthesis.generate_markdown_report(
    run_id,
    title="Widget Mark III — Verified Synthesis Sample",
    include_source_assessment=True,
    settings=settings,
)

target = REPO_ROOT / "samples" / "phase6-sample-report.md"
label = (
    "<p><strong>SAMPLE REPORT — synthetic test data.</strong> This file was generated by the "
    "real Phase 6 pipeline (<code>extract_claims</code> → <code>verify_claims</code> → "
    "<code>analyze_conflicts</code> → <code>generate_markdown_report</code>) from fabricated "
    "fixtures in a throwaway database. The entities, sources, and URLs (example domains) are "
    "not real; the labels, sections, and citation markers are exactly what the tools render. "
    "It is documentation, not a research result.</p>\n\n"
)
target.write_text(label + Path(report["path"]).read_text(encoding="utf-8"), encoding="utf-8")

print("report_id:", report["report_id"])
print("validation_status:", report["validation_status"], "passed:", report["validation"]["passed"])
print("counts_by_status:", report["counts_by_status"])
print("unresolved_conflicts:", report["unresolved_conflicts"])
print("written:", target)

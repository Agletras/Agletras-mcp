"""Phase 6 tests: synthesis report generation, integrity validation, retrieval.

All tests are hermetic: temporary ``AGLETRAS_HOME``, no external network.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from agletras_mcp import evidence, images, report as report_mod, storage, synthesis, verification
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import TempHomeCase

NOTE_TEXT = (
    "Widgets are small mechanical components used in many devices. "
    "Standard widgets are manufactured in several sizes. "
    "The average widget weighs 250 grams according to the manufacturer survey."
)

VERBATIM_CLAIM = "Widgets are small mechanical components used in many devices."
UNSUPPORTED_CLAIM = "Widgets are assembled in Europe by hand."


class _FixedDatetime(datetime):
    """datetime with a frozen now() so report content is byte-stable."""

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - test helper
        return datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)


class Phase6SynthesisCase(TempHomeCase):
    """Shared helpers: runs, seeded claims, generation wrappers."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def new_run(self, query: str = "How are widgets made?") -> dict:
        return evidence.start_run(query, settings=self.settings)

    def add_source(self, run_id: int, text: str = NOTE_TEXT, *,
                   title: str = "Widget findings",
                   url: str = "https://example.org/widgets") -> dict:
        return evidence.save_evidence(
            text=text, title=title, url=url, run_id=run_id, settings=self.settings
        )

    def seeded_run(self) -> tuple[dict, dict, dict]:
        """Run with one source, one verbatim-supported claim, one unsupported claim."""
        run = self.new_run()
        self.add_source(run["run_id"])
        supported = evidence.register_claim(
            run["run_id"], VERBATIM_CLAIM, settings=self.settings
        )
        unsupported = evidence.register_claim(
            run["run_id"], UNSUPPORTED_CLAIM, settings=self.settings
        )
        verification.verify_claims(run["run_id"], settings=self.settings)
        return run, supported, unsupported

    def generate(self, run_id: int, **kwargs) -> dict:
        return synthesis.generate_markdown_report(run_id, settings=self.settings, **kwargs)

    def _rewrite_report(self, report_id: int, transform) -> str:
        """Replace the stored + on-disk report content (and keep the hash in sync)."""
        with storage.open_db(self.settings) as conn:
            row = conn.execute(
                "SELECT * FROM reports WHERE id = ?", (int(report_id),)
            ).fetchone()
            content = transform(row["content"] or "")
            conn.execute(
                "UPDATE reports SET content = ?, content_hash = ? WHERE id = ?",
                (content, storage.content_hash(content), int(report_id)),
            )
        path = Path(row["path"] or "")
        if path.exists():
            path.write_text(content, encoding="utf-8")
        return content


# ---------------------------------------------------------------------------
# Template shape: 10 sections, stable identifiers, status labels
# ---------------------------------------------------------------------------


class TemplateTests(Phase6SynthesisCase):
    def test_ten_sections_stable_ids_and_status_labels(self) -> None:
        run, supported_claim, unsupported_claim = self.seeded_run()
        result = self.generate(run["run_id"], title="Widget synthesis")
        markdown = result["markdown"]
        self.assertTrue(markdown.startswith("# Widget synthesis"))
        sections = synthesis._SECTION_RE.findall(markdown)
        for required in synthesis.PHASE6_REQUIRED_SECTIONS:
            self.assertIn(required, sections)
        # Stable [F#] ids in claim-id order and a rendered [S#] citation.
        self.assertEqual(
            result["finding_map"],
            {"F1": supported_claim["claim_id"], "F2": unsupported_claim["claim_id"]},
        )
        self.assertIn("[F1]", markdown)
        self.assertIn("[F2]", markdown)
        self.assertEqual(list(result["citation_map"].keys()), ["S1"])
        self.assertIn("[S1]", markdown)
        self.assertIn("SUPPORTED", markdown)
        self.assertIn("UNSUPPORTED", markdown)
        self.assertIn("no liveness check was performed", markdown)

    def test_kind_groups_keep_facts_and_unverified_apart(self) -> None:
        run, _, _ = self.seeded_run()
        result = self.generate(run["run_id"])
        markdown = result["markdown"]
        facts_section = markdown.split("### Supported statements (facts)")[1]
        facts_section = facts_section.split("### Partially supported statements")[0]
        self.assertIn(VERBATIM_CLAIM, facts_section)
        self.assertIn("F1 — CLAIM", facts_section)
        unverified_section = markdown.split("### Unverified and unevaluable statements")[1]
        unverified_section = unverified_section.split("## Evidence comparison")[0]
        self.assertIn(UNSUPPORTED_CLAIM, unverified_section)
        self.assertIn("NOT asserted as true", unverified_section)

    def test_inference_without_reasoning_is_labelled(self) -> None:
        run = self.new_run()
        self.add_source(run["run_id"])
        evidence.register_claim(
            run["run_id"],
            "Widgets are probably made in small batches.",
            claim_type="inference",
            settings=self.settings,
        )
        result = self.generate(run["run_id"])
        markdown = result["markdown"]
        self.assertIn("### Inferences (reasoned, not verified)", markdown)
        self.assertIn("Stated reasoning: **not recorded**", markdown)
        warnings = " ".join(result["validation"]["warnings"])
        self.assertIn("inference without recorded reasoning", warnings)


# ---------------------------------------------------------------------------
# Generation: storage, validation, dedupe, overwrite safety, path confinement
# ---------------------------------------------------------------------------


class GenerationTests(Phase6SynthesisCase):
    def test_generate_stores_validates_and_writes_file(self) -> None:
        run, _, _ = self.seeded_run()
        result = self.generate(run["run_id"])
        self.assertTrue(result["created"])
        self.assertFalse(result["deduplicated"])
        self.assertEqual(result["template_version"], "phase6")
        self.assertEqual(result["counts_by_status"], {"supported": 1, "unsupported": 1})
        path = Path(result["path"])
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8"), result["markdown"])
        self.assertIn(str(self.settings.reports_dir.resolve()), str(path.resolve()))
        validation = result["validation"]
        self.assertTrue(validation["passed"])
        self.assertEqual(validation["status"], "validated")
        self.assertEqual(validation["errors"], [])
        self.assertTrue(validation["phase6_checks"])
        stored = evidence.get_report(result["report_id"], settings=self.settings)
        self.assertEqual(stored["template_version"], "phase6")
        self.assertEqual(set(stored["finding_map"]), {"F1", "F2"})
        self.assertEqual(stored["generation_config"]["generated_by"], "agletras_phase6")
        with storage.open_db(self.settings) as conn:
            kinds = {
                row["kind"]
                for row in conn.execute(
                    "SELECT kind FROM research_run_events WHERE run_id = ?",
                    (run["run_id"],),
                ).fetchall()
            }
        self.assertIn("report", kinds)

    def test_identical_content_is_deduplicated_and_files_never_overwritten(self) -> None:
        run, _, _ = self.seeded_run()
        with mock.patch.object(report_mod, "datetime", _FixedDatetime), mock.patch.object(
            synthesis, "datetime", _FixedDatetime
        ):
            first = self.generate(run["run_id"])
            second = self.generate(run["run_id"])
        self.assertFalse(second["created"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["report_id"], first["report_id"])
        self.assertEqual(second["path"], first["path"])
        files = sorted(self.settings.reports_dir.glob("report-run*.md"))
        self.assertEqual(len(files), 1)

    def test_unique_path_confines_and_never_overwrites(self) -> None:
        name = "report-run9-test-20260101-000000.md"
        first = synthesis._unique_report_path(self.settings, name)
        self.assertEqual(first.name, name)
        self.settings.reports_dir.mkdir(parents=True, exist_ok=True)
        first.write_text("existing", encoding="utf-8")
        second = synthesis._unique_report_path(self.settings, name)
        self.assertEqual(second.name, "report-run9-test-20260101-000000-2.md")
        self.assertNotEqual(first, second)
        with self.assertRaises(ToolError) as ctx:
            synthesis._confined_path(self.settings, "../escape.md")
        self.assertEqual(ctx.exception.code, "invalid_argument")
        with self.assertRaises(ToolError) as ctx:
            synthesis._confined_path(self.settings, "/etc/passwd")
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_report_max_findings_warns_but_does_not_truncate(self) -> None:
        import os

        os.environ["AGLETRAS_REPORT_MAX_FINDINGS"] = "1"
        settings = load_settings()
        run = evidence.start_run("Widget sizes?", settings=settings)
        evidence.save_evidence(
            text=NOTE_TEXT, title="Widget findings", url="https://example.org/widgets",
            run_id=run["run_id"], settings=settings,
        )
        evidence.register_claim(run["run_id"], VERBATIM_CLAIM, settings=settings)
        evidence.register_claim(
            run["run_id"], "Standard widgets are manufactured in several sizes.",
            settings=settings,
        )
        result = synthesis.generate_markdown_report(run["run_id"], settings=settings)
        self.assertTrue(any("size warning" in item for item in result["warnings"]))
        self.assertIn("[F1]", result["markdown"])
        self.assertIn("[F2]", result["markdown"])

    def test_conflicts_render_as_unresolved_and_validate(self) -> None:
        run = self.new_run()
        self.add_source(run["run_id"], text="The bridge is 1200 meters long.",
                        title="Bridge A", url="https://a.example/bridge")
        self.add_source(run["run_id"], text="The bridge is 1450 meters long.",
                        title="Bridge B", url="https://b.example/bridge")
        claim = evidence.register_claim(
            run["run_id"], "The bridge is 1200 meters long.", settings=self.settings
        )
        passage_a = evidence.search_evidence(
            "1200", run_id=run["run_id"], settings=self.settings
        )["passages"][0]
        passage_b = evidence.search_evidence(
            "1450", run_id=run["run_id"], settings=self.settings
        )["passages"][0]
        evidence.attach_citation(
            claim["claim_id"], passage_a["passage_id"], relation="supports",
            quote="The bridge is 1200 meters long.", settings=self.settings,
        )
        evidence.attach_citation(
            claim["claim_id"], passage_b["passage_id"], relation="contradicts",
            quote="The bridge is 1450 meters long.", settings=self.settings,
        )
        result = self.generate(run["run_id"])
        markdown = result["markdown"]
        section = markdown.split("## Conflicting evidence")[1]
        self.assertIn("The bridge is 1200 meters long.", section)
        self.assertIn("UNRESOLVED", section)
        self.assertEqual(result["validation"]["errors"], [])
        self.assertGreaterEqual(result["unresolved_conflicts"], 1)
        self.assertGreaterEqual(
            result["validation"]["unresolved"]["unresolved_conflicts"], 1
        )

    def test_unknown_ids_raise_not_found(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.generate(999)
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(ToolError) as ctx:
            synthesis.validate_phase6_report(999, settings=self.settings)
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(ToolError) as ctx:
            synthesis.get_markdown_report(999, settings=self.settings)
        self.assertEqual(ctx.exception.code, "not_found")


# ---------------------------------------------------------------------------
# Integrity validation: tampering, model evidence, images, delegation
# ---------------------------------------------------------------------------


class IntegrityTests(Phase6SynthesisCase):
    def test_missing_section_is_an_error(self) -> None:
        run, _, _ = self.seeded_run()
        generated = self.generate(run["run_id"])
        self._rewrite_report(
            generated["report_id"],
            lambda text: text.replace("## Conclusion", "## Concluded"),
        )
        validation = synthesis.validate_phase6_report(
            generated["report_id"], settings=self.settings
        )
        codes = {item["code"] for item in validation["errors"]}
        self.assertIn("missing_section", codes)
        self.assertEqual(validation["status"], "failed")
        self.assertFalse(validation["passed"])

    def test_unresolved_finding_identifier_is_an_error(self) -> None:
        run, _, _ = self.seeded_run()
        generated = self.generate(run["run_id"])
        self._rewrite_report(
            generated["report_id"],
            lambda text: text + "\n[F999] ghost statement\n",
        )
        validation = synthesis.validate_phase6_report(
            generated["report_id"], settings=self.settings
        )
        codes = {item["code"] for item in validation["errors"]}
        self.assertIn("unresolved_finding_identifier", codes)

    def test_content_hash_mismatch_is_detected(self) -> None:
        run, _, _ = self.seeded_run()
        generated = self.generate(run["run_id"])
        with storage.open_db(self.settings) as conn:
            row = conn.execute(
                "SELECT content FROM reports WHERE id = ?", (generated["report_id"],)
            ).fetchone()
            conn.execute(
                "UPDATE reports SET content = ? WHERE id = ?",
                (row["content"] + "\nTAMPER\n", generated["report_id"]),
            )
        validation = synthesis.validate_phase6_report(
            generated["report_id"], settings=self.settings
        )
        codes = {item["code"] for item in validation["errors"]}
        self.assertIn("report_content_hash_mismatch", codes)

    def test_model_assessment_must_reference_stored_run_passages(self) -> None:
        run, supported_claim, _ = self.seeded_run()
        with storage.open_db(self.settings) as conn:
            conn.execute(
                """
                INSERT INTO claim_verifications
                    (claim_id, run_id, status, method, confidence, rationale, evidence,
                     checks, limitations, created_at)
                VALUES (?, ?, 'supported', 'model', 'medium', 'stub rationale', '{}', ?, '[]', ?)
                """,
                (
                    supported_claim["claim_id"], run["run_id"],
                    json.dumps({"model": {"assessment": {"evidence_passage_ids": [99999]}}}),
                    "2026-10-18T00:00:00+00:00",
                ),
            )
        result = self.generate(run["run_id"])
        codes = {item["code"] for item in result["validation"]["errors"]}
        self.assertIn("model_assessment_without_stored_evidence", codes)

    def test_image_attribution_and_missing_metadata_warnings(self) -> None:
        run = self.new_run()
        complete = images.ImageMetadata(
            image_url="https://images.example/widget.png",
            source_page_url="https://commons.example/widget",
            title="Widget diagram",
            creator="A. Researcher",
            retrieved_at="2026-10-18T00:00:00+00:00",
            width=640, height=480, mime_type="image/png",
            license_name="CC BY-SA 4.0",
            license_url="https://creativecommons.org/licenses/by-sa/4.0/",
            attribution="A. Researcher, CC BY-SA 4.0",
            provider="wikimedia_commons",
            missing_metadata=[],
        )
        sparse = images.ImageMetadata(
            image_url="https://images.example/anonymous.jpg",
            source_page_url="https://commons.example/anonymous",
            title="Anonymous photo",
            creator=None,
            retrieved_at="2026-10-18T00:00:00+00:00",
            width=800, height=600, mime_type="image/jpeg",
            license_name=None, license_url=None, attribution=None,
            provider="wikimedia_commons",
            missing_metadata=["creator", "license"],
        )
        stored_complete = images._store_image_as_evidence(complete, self.settings)
        stored_sparse = images._store_image_as_evidence(sparse, self.settings)
        with storage.open_db(self.settings) as conn:
            evidence._associate_run(conn, run["run_id"], stored_complete["source_id"])
            evidence._associate_run(conn, run["run_id"], stored_sparse["source_id"])
        result = self.generate(run["run_id"])
        markdown = result["markdown"]
        self.assertEqual(
            result["image_map"],
            {"IMG1": stored_complete["source_id"], "IMG2": stored_sparse["source_id"]},
        )
        self.assertIn("[IMG1]", markdown)
        self.assertIn("A. Researcher", markdown)
        self.assertIn("CC BY-SA 4.0", markdown)
        self.assertIn("Creator: not recorded", markdown)
        self.assertIn("License: not recorded", markdown)
        self.assertEqual(result["validation"]["errors"], [])
        warnings = " ".join(result["validation"]["warnings"])
        self.assertIn("has no creator metadata", warnings)
        self.assertIn("has no license metadata", warnings)

    def test_phase3_report_delegates_to_structural_validation(self) -> None:
        run = self.new_run()
        self.add_source(run["run_id"])
        evidence.register_claim(run["run_id"], VERBATIM_CLAIM, settings=self.settings)
        result = evidence.generate_report(run["run_id"], settings=self.settings)
        validation = synthesis.validate_phase6_report(
            result["report_id"], settings=self.settings
        )
        self.assertFalse(validation["phase6_checks"])
        self.assertEqual(validation["validation_kind"], "structural")
        self.assertEqual(validation["template_version"], "phase3")
        self.assertIn("note", validation)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


class RetrievalTests(Phase6SynthesisCase):
    def test_get_report_flags_and_file_deletion(self) -> None:
        run, _, _ = self.seeded_run()
        generated = self.generate(run["run_id"])
        report = synthesis.get_markdown_report(
            generated["report_id"], include_content=True, settings=self.settings
        )
        self.assertTrue(report["file_exists"])
        self.assertTrue(report["file_hash_matches"])
        for required in synthesis.PHASE6_REQUIRED_SECTIONS:
            self.assertIn(required, report["sections"])
        self.assertEqual(report["content"], generated["markdown"])
        self.assertGreater(report["character_count"], 0)
        self.assertEqual(set(report["finding_map"]), {"F1", "F2"})
        Path(generated["path"]).unlink()
        after = synthesis.get_markdown_report(
            generated["report_id"], settings=self.settings
        )
        self.assertFalse(after["file_exists"])
        self.assertFalse(after["file_hash_matches"])
        self.assertNotIn("content", after)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

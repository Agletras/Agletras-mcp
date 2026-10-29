"""Tests for the Phase 8 evidence visualizations and CSV exports.

Every renderer must derive its output exclusively from stored runs, sources,
claims, citations, and passages: these tests assert that nodes/edges/rows
match the store exactly, that missing metadata stays blank (never invented),
and that re-exports overwrite derived artifacts deterministically.
"""

from __future__ import annotations

import csv
import io
import unittest
from pathlib import Path

from agletras_mcp import evidence, storage, visualize
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import TempHomeCase

TEXT_A = (
    "Solar panels convert sunlight into electricity using photovoltaic cells. "
    "A single panel produces direct current that an inverter turns into grid power."
)
TEXT_B = (
    "In 2024 the cost of solar panels fell sharply, contrary to some earlier "
    "forecasts of rising module prices."
)
CLAIM_SUPPORT = "Solar panels convert sunlight into electricity."
CLAIM_CONTRA = "The cost of solar panels rose in 2024."


class VisualizeCase(TempHomeCase):
    """Shared fixtures: a run with two sources, two claims, three citations."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def make_run(self, query: str = "How do solar panels work, and what do they cost?") -> int:
        return int(evidence.start_run(query, settings=self.settings)["run_id"])

    def save_text(
        self, run_id: int, title: str, text: str, *, url: str = "", source_type: str = "note"
    ) -> int:
        payload = evidence.save_evidence(
            run_id=run_id,
            title=title,
            text=text,
            url=url,
            source_type=source_type,
            settings=self.settings,
        )
        return int(payload["source_id"])

    def passage_for(self, run_id: int, query: str) -> dict:
        results = evidence.search_evidence(query, run_id=run_id, settings=self.settings)
        self.assertTrue(results["passages"], f"no passage matched {query!r}")
        return results["passages"][0]

    def add_citation(
        self, claim_id: int, passage: dict, relation: str, quote: str
    ) -> None:
        evidence.attach_citation(
            claim_id,
            int(passage["passage_id"]),
            quote=quote,
            relation=relation,
            settings=self.settings,
        )

    def fixture_run(self) -> tuple[int, dict]:
        """A run with: claim 1 supported, claim 2 contradicted."""
        run_id = self.make_run()
        source_a = self.save_text(
            run_id, "Solar basics", TEXT_A, url="https://energytest.example/solar-basics"
        )
        source_b = self.save_text(
            run_id, "Module price review", TEXT_B, url="https://pricetest.example/2024-review"
        )
        passage_a = self.passage_for(run_id, "photovoltaic")
        passage_b = self.passage_for(run_id, "contrary")
        claim_one = int(
            evidence.register_claim(run_id, CLAIM_SUPPORT, settings=self.settings)["claim_id"]
        )
        claim_two = int(
            evidence.register_claim(run_id, CLAIM_CONTRA, settings=self.settings)["claim_id"]
        )
        self.add_citation(claim_one, passage_a, "supports", "photovoltaic cells")
        self.add_citation(claim_one, passage_b, "context", "cost of solar panels fell")
        self.add_citation(claim_two, passage_b, "contradicts", "cost of solar panels fell")
        ids = {"source_a": source_a, "source_b": source_b, "claim_one": claim_one, "claim_two": claim_two}
        return run_id, ids

    def set_source_timestamps(
        self, source_id: int, *, retrieved_at: str | None, publication_date: str | None
    ) -> None:
        with storage.open_db(self.settings) as conn:
            conn.execute(
                "UPDATE sources SET retrieved_at = ?, publication_date = ? WHERE id = ?",
                (retrieved_at, publication_date, source_id),
            )
            conn.commit()

    def csv_rows(self, document: str) -> list[dict[str, str]]:
        reader = csv.DictReader(io.StringIO(document))
        return list(reader)


class EvidenceMapTests(VisualizeCase):
    def test_nodes_and_edges_mirror_the_stored_citations(self) -> None:
        run_id, ids = self.fixture_run()
        dataset = visualize.build_dataset(run_id, settings=self.settings)
        chart = visualize.render_evidence_map(dataset)
        self.assertTrue(chart.startswith("%% Evidence map"))
        self.assertIn("flowchart LR", chart)
        self.assertIn(f'RUN["Run {run_id}:', chart)
        for source_id in (ids["source_a"], ids["source_b"]):
            self.assertIn(f"S{source_id}[", chart)
        self.assertIn(f"C{ids['claim_one']}[", chart)
        self.assertIn(f"C{ids['claim_two']}[", chart)
        self.assertIn(f"S{ids['source_a']} -->|supports| C{ids['claim_one']}", chart)
        self.assertIn(f"S{ids['source_b']} -.->|context| C{ids['claim_one']}", chart)
        self.assertIn(f"S{ids['source_b']} -.->|contradicts| C{ids['claim_two']}", chart)
        # No invented edges: exactly one edge per stored citation.
        self.assertEqual(chart.count("-->|") + chart.count("-.->|"), 3)

    def test_status_and_titles_appear_in_labels(self) -> None:
        run_id, _ = self.fixture_run()
        chart = visualize.render_evidence_map(
            visualize.build_dataset(run_id, settings=self.settings)
        )
        self.assertIn("[supported]", chart)
        self.assertIn("[contradicted]", chart)
        self.assertIn("Solar basics", chart)
        self.assertIn("energytest.example", chart)

    def test_labels_escape_and_clip_untrusted_text(self) -> None:
        run_id = self.make_run('Question with "quotes" and #hash <angles> ' + "x" * 200)
        self.save_text(run_id, 'Title "quoted" #1 <b>', TEXT_A)
        claim_id = int(
            evidence.register_claim(
                run_id, 'Claim with "quotes", a #hash, <b>tags</b>, and ' + "y" * 200,
                settings=self.settings,
            )["claim_id"]
        )
        passage = self.passage_for(run_id, "photovoltaic")
        self.add_citation(claim_id, passage, "supports", "photovoltaic cells")
        chart = visualize.render_evidence_map(
            visualize.build_dataset(run_id, settings=self.settings)
        )
        label_lines = [line for line in chart.splitlines() if "[" in line and '"' in line]
        self.assertTrue(label_lines)
        for line in label_lines:
            # Exactly the two structural quotes of a ["..."] label survive.
            self.assertEqual(line.count('"'), 2, line)
        self.assertIn("#quot;", chart)
        self.assertIn("#35;", chart)
        self.assertIn("#lt;", chart)
        self.assertNotIn("<b>", chart)
        self.assertIn("...", chart)  # long text is clipped, not truncated silently

    def test_empty_run_renders_an_honest_placeholder(self) -> None:
        run_id = self.make_run("Nothing collected yet")
        chart = visualize.render_evidence_map(
            visualize.build_dataset(run_id, settings=self.settings)
        )
        self.assertIn("No sources or claims recorded", chart)
        self.assertNotIn("S1[", chart)
        self.assertNotIn("C1[", chart)

    def test_rendering_is_deterministic(self) -> None:
        run_id, _ = self.fixture_run()
        dataset = visualize.build_dataset(run_id, settings=self.settings)
        self.assertEqual(
            visualize.render_evidence_map(dataset), visualize.render_evidence_map(dataset)
        )

    def test_unknown_run_raises_not_found(self) -> None:
        with self.assertRaises(ToolError) as caught:
            visualize.build_dataset(9999, settings=self.settings)
        self.assertEqual(caught.exception.code, "not_found")


class TimelineTests(VisualizeCase):
    def test_timeline_lists_recorded_timestamps_only(self) -> None:
        run_id, ids = self.fixture_run()
        self.set_source_timestamps(
            ids["source_a"], retrieved_at="2026-03-01T10:00:00Z", publication_date="2020-05-05"
        )
        self.set_source_timestamps(
            ids["source_b"], retrieved_at="2026-03-02T11:30:00Z", publication_date=None
        )
        timeline = visualize.render_timeline(
            visualize.build_dataset(run_id, settings=self.settings)
        )
        self.assertIn("timeline", timeline)
        self.assertIn("section Run", timeline)
        self.assertIn("section Sources", timeline)
        self.assertIn(
            f"2026-03-01 : Source {ids['source_a']}: Solar basics (published 2020-05-05)", timeline
        )
        self.assertIn(f"2026-03-02 : Source {ids['source_b']}: Module price review", timeline)
        # No invented publication date for source_b.
        self.assertNotIn("Module price review (published", timeline)

    def test_sources_without_retrieval_timestamps_are_omitted(self) -> None:
        run_id, ids = self.fixture_run()
        self.set_source_timestamps(ids["source_a"], retrieved_at=None, publication_date="1999-01-01")
        timeline = visualize.render_timeline(
            visualize.build_dataset(run_id, settings=self.settings)
        )
        self.assertNotIn("Solar basics", timeline)
        self.assertIn("Module price review", timeline)

    def test_no_timestamped_events_renders_a_clear_title(self) -> None:
        run_id = self.make_run("Timestampless")
        # started_at is NOT NULL: an empty value records "unknown" honestly.
        with storage.open_db(self.settings) as conn:
            conn.execute("UPDATE research_runs SET started_at = '' WHERE id = ?", (run_id,))
            conn.commit()
        timeline = visualize.render_timeline(
            visualize.build_dataset(run_id, settings=self.settings)
        )
        self.assertIn("No timestamped events recorded", timeline)


class CsvExportTests(VisualizeCase):
    def test_sources_csv_columns_counts_and_provenance(self) -> None:
        run_id, ids = self.fixture_run()
        document = visualize.render_sources_csv(
            visualize.build_dataset(run_id, settings=self.settings)
        )
        rows = self.csv_rows(document)
        self.assertEqual(len(rows), 2)
        by_id = {int(row["source_id"]): row for row in rows}
        row_a = by_id[ids["source_a"]]
        self.assertEqual(row_a["title"], "Solar basics")
        self.assertEqual(row_a["url"], "https://energytest.example/solar-basics")
        self.assertEqual(row_a["cited_claims"], "1")
        self.assertEqual(row_a["stale"], "false")
        self.assertEqual(len(row_a["sha256"]), 64)
        self.assertTrue(row_a["retrieved_at"])
        row_b = by_id[ids["source_b"]]
        self.assertEqual(row_b["cited_claims"], "2")
        # Missing publication dates stay blank: nothing is invented.
        self.assertEqual(row_a["publication_date"], "")
        self.assertEqual(row_b["publication_date"], "")

    def test_claims_csv_status_and_relation_tallies(self) -> None:
        run_id, ids = self.fixture_run()
        document = visualize.render_claims_csv(
            visualize.build_dataset(run_id, settings=self.settings)
        )
        rows = {int(row["claim_id"]): row for row in self.csv_rows(document)}
        one = rows[ids["claim_one"]]
        two = rows[ids["claim_two"]]
        self.assertEqual(one["claim_text"], CLAIM_SUPPORT)
        self.assertEqual(one["status"], "supported")
        self.assertEqual(one["citations"], "2")
        self.assertEqual(one["supporting"], "1")
        self.assertEqual(one["partially_supporting"], "0")
        self.assertEqual(one["contradicting"], "0")
        self.assertEqual(two["claim_text"], CLAIM_CONTRA)
        self.assertEqual(two["status"], "contradicted")
        self.assertEqual(two["contradicting"], "1")

    def test_citations_csv_matches_stored_passages(self) -> None:
        run_id, _ = self.fixture_run()
        dataset = visualize.build_dataset(run_id, settings=self.settings)
        rows = self.csv_rows(visualize.render_citations_csv(dataset))
        self.assertEqual(len(rows), 3)
        stored_passages = {
            int(claim["claim_id"]): claim["citations"] for claim in dataset["claims"]
        }
        for row in rows:
            claim_id = int(row["claim_id"])
            citation = next(
                item
                for item in stored_passages[claim_id]
                if int(item["citation_id"]) == int(row["citation_id"])
            )
            self.assertEqual(row["relation"], citation["relation"])
            self.assertEqual(row["quote"], citation["quote"])
            self.assertEqual(int(row["source_id"]), int(citation["source_id"]))
            self.assertEqual(int(row["passage_id"]), int(citation["passage_id"]))
            self.assertEqual(row["passage_hash"], citation["passage_hash"])
            self.assertEqual(row["content_hash"], citation["content_hash"])

    def test_claims_csv_without_claims_has_only_the_header(self) -> None:
        run_id = self.make_run("No claims")
        document = visualize.render_claims_csv(
            visualize.build_dataset(run_id, settings=self.settings)
        )
        self.assertEqual(len(self.csv_rows(document)), 0)
        self.assertTrue(document.startswith("claim_id,"))


class ExportVisualsTests(VisualizeCase):
    def test_export_writes_all_files_with_metadata(self) -> None:
        run_id, _ = self.fixture_run()
        target = self.home / "exports"
        payload = visualize.export_visuals(run_id, out_dir=target, settings=self.settings)
        self.assertEqual(payload["run_id"], run_id)
        self.assertEqual(payload["formats"], ["csv", "mermaid"])
        self.assertEqual(payload["counts"], {"sources": 2, "claims": 2, "citations": 3})
        names = {item["name"] for item in payload["files"]}
        self.assertEqual(
            names,
            {"evidence-map.mmd", "timeline.mmd", "sources.csv", "claims.csv", "citations.csv"},
        )
        for item in payload["files"]:
            path = Path(item["path"])
            self.assertTrue(path.is_file(), item["name"])
            self.assertEqual(path.stat().st_size, item["bytes"])
            self.assertGreater(item["bytes"], 0)
        self.assertEqual(Path(payload["directory"]), target)

    def test_export_format_subset_and_default_directory(self) -> None:
        run_id, _ = self.fixture_run()
        payload = visualize.export_visuals(
            run_id, formats=("mermaid",), settings=self.settings
        )
        self.assertEqual(payload["formats"], ["mermaid"])
        names = {item["name"] for item in payload["files"]}
        self.assertEqual(names, {"evidence-map.mmd", "timeline.mmd"})
        expected_dir = self.settings.reports_dir / "visuals" / f"run-{run_id}"
        self.assertEqual(Path(payload["directory"]), expected_dir)
        self.assertTrue((expected_dir / "evidence-map.mmd").is_file())
        self.assertFalse((expected_dir / "claims.csv").exists())

    def test_export_reflects_new_claims_after_reexport(self) -> None:
        run_id, ids = self.fixture_run()
        target = self.home / "exports2"
        visualize.export_visuals(run_id, out_dir=target, settings=self.settings)
        first = (target / "claims.csv").read_text(encoding="utf-8")
        self.assertNotIn("Re-exported claim", first)
        evidence.register_claim(run_id, "A re-exported claim.", settings=self.settings)
        visualize.export_visuals(run_id, out_dir=target, settings=self.settings)
        second = (target / "claims.csv").read_text(encoding="utf-8")
        self.assertIn("A re-exported claim.", second)
        # Derived files are refreshed, not appended to.
        self.assertEqual(second.count("A re-exported claim."), 1)

    def test_export_overwrites_tampered_derived_files(self) -> None:
        run_id, _ = self.fixture_run()
        target = self.home / "exports3"
        visualize.export_visuals(run_id, out_dir=target, settings=self.settings)
        chart = target / "evidence-map.mmd"
        original = chart.read_text(encoding="utf-8")
        chart.write_text("tampered\n", encoding="utf-8")
        visualize.export_visuals(run_id, out_dir=target, settings=self.settings)
        self.assertEqual(chart.read_text(encoding="utf-8"), original)

    def test_export_unknown_run_and_unknown_format(self) -> None:
        with self.assertRaises(ToolError) as caught:
            visualize.export_visuals(424242, out_dir=self.home / "x", settings=self.settings)
        self.assertEqual(caught.exception.code, "not_found")
        run_id, _ = self.fixture_run()
        with self.assertRaises(ValueError):
            visualize.export_visuals(run_id, formats=("pdf",), settings=self.settings)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Tests for Phase 3 report rendering: stable [Sn] identifiers and honest
uncertainty markers (never presented as verified)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from agletras_mcp.report import build_evidence_markdown, slugify

RUN = {"run_id": 3, "query": "How do widgets work?"}


def make_claim(
    claim_id: int,
    text: str,
    *,
    status: str = "unverified",
    citations: list[dict] | None = None,
) -> dict:
    return {
        "claim_id": claim_id,
        "run_id": RUN["run_id"],
        "claim_text": text,
        "status": status,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "citations": citations or [],
    }


def make_citation(
    citation_id: int,
    *,
    relation: str = "supports",
    quote: str = "",
    source_title: str = "Widget manual",
    source_url: str | None = "https://example.com/widgets",
    source_path: str | None = None,
    passage_locator: str = "page 2",
    locator: str = "",
) -> dict:
    return {
        "citation_id": citation_id,
        "claim_id": 1,
        "relation": relation,
        "quote": quote,
        "locator": locator,
        "passage_id": 10 + citation_id,
        "passage_text": "Widgets are small components.",
        "passage_index": 0,
        "passage_hash": "h" * 64,
        "content_hash": "c" * 64,
        "start_offset": 0,
        "end_offset": 10,
        "page": None,
        "heading": None,
        "passage_locator": passage_locator,
        "source_id": 7,
        "source_title": source_title,
        "source_url": source_url,
        "source_path": source_path,
        "source_type": "web",
        "source_kind": "url",
        "source_retrieved_at": "2026-02-01T10:00:00+00:00",
        "source_publication_date": "2026-01-15T00:00:00+00:00",
    }


class EvidenceMarkdownTests(unittest.TestCase):
    def render(self, claims: list[dict], title: str = "Widget Report"):
        return build_evidence_markdown(
            RUN, claims, title=title, generated_at=datetime(2026, 3, 1, tzinfo=timezone.utc)
        )

    def test_empty_claims_render_placeholders(self) -> None:
        markdown, citation_map = self.render([])
        self.assertEqual(citation_map, {})
        self.assertIn("# Widget Report", markdown)
        self.assertIn("No claims are registered", markdown)
        self.assertIn("No citations are attached", markdown)
        self.assertIn("research run `3`", markdown)
        self.assertIn("How do widgets work?", markdown)

    def test_supported_claim_renders_quote_and_references(self) -> None:
        claim = make_claim(
            1,
            "Widgets are small components.",
            status="supported",
            citations=[make_citation(11, quote="Widgets are small components.")],
        )
        markdown, citation_map = self.render([claim])
        self.assertEqual(citation_map, {"S1": 11})
        self.assertIn("[S1]", markdown)
        self.assertIn("“Widgets are small components.”", markdown)
        self.assertIn("**[S1]** Widget manual", markdown)
        self.assertIn("https://example.com/widgets", markdown)
        self.assertIn("published 2026-01-15", markdown)
        self.assertIn("retrieved 2026-02-01", markdown)
        self.assertIn("locator: page 2", markdown)
        self.assertNotIn("UNVERIFIED", markdown)

    def test_labels_are_sequential_and_stable(self) -> None:
        claims = [
            make_claim(
                1,
                "First claim.",
                status="supported",
                citations=[make_citation(21), make_citation(22, relation="partially_supports")],
            ),
            make_claim(
                2,
                "Second claim.",
                status="contradicted",
                citations=[make_citation(23, relation="contradicts")],
            ),
        ]
        markdown, citation_map = self.render(claims)
        self.assertEqual(citation_map, {"S1": 21, "S2": 22, "S3": 23})
        self.assertIn("1. First claim. [S1] [S2]", markdown)
        self.assertIn("2. Second claim. [S3]", markdown)
        self.assertIn("(partial)", markdown)
        self.assertIn("(contradicts)", markdown)

    def test_uncertain_claims_are_visibly_marked(self) -> None:
        claims = [
            make_claim(1, "Unbacked claim."),
            make_claim(
                2,
                "Reviewed claim.",
                status="needs_review",
                citations=[make_citation(31, relation="needs_review")],
            ),
            make_claim(
                3,
                "Disputed claim.",
                status="contradicted",
                citations=[make_citation(32, relation="contradicts")],
            ),
            make_claim(
                4,
                "Partial claim.",
                status="partially_supported",
                citations=[make_citation(33, relation="partially_supports")],
            ),
        ]
        markdown, _map = self.render(claims)
        self.assertIn("**UNVERIFIED** (no supporting evidence attached)", markdown)
        self.assertIn("**NEEDS REVIEW**", markdown)
        self.assertIn("**CONTRADICTED by evidence**", markdown)
        self.assertIn("**PARTIALLY SUPPORTED**", markdown)
        self.assertIn("## Claims without established support", markdown)
        self.assertIn("Reviewed claim. — NEEDS_REVIEW", markdown)
        self.assertIn("Disputed claim. — CONTRADICTED", markdown)
        self.assertNotIn("UNVERIFIED** (attached evidence", markdown)

    def test_context_only_claim_is_unverified_with_reason(self) -> None:
        claim = make_claim(
            1,
            "Context-only claim.",
            status="unverified",
            citations=[make_citation(41, relation="context")],
        )
        markdown, citation_map = self.render([claim])
        self.assertEqual(citation_map, {"S1": 41})
        self.assertIn("**UNVERIFIED** (no citation establishes support)", markdown)
        self.assertIn("(context)", markdown)
        self.assertIn("does not establish support", markdown)

    def test_source_without_url_or_path_is_labeled(self) -> None:
        claim = make_claim(
            1,
            "File claim.",
            status="supported",
            citations=[
                make_citation(
                    51, source_url=None, source_path="/home/user/notes.md", source_title="Notes"
                )
            ],
        )
        markdown, _map = self.render([claim])
        self.assertIn("/home/user/notes.md", markdown)
        bare = make_claim(
            2,
            "Bare claim.",
            status="supported",
            citations=[make_citation(52, source_url=None, source_path=None)],
        )
        markdown, _map = self.render([bare])
        self.assertIn("(no URL/path recorded)", markdown)

    def test_long_quotes_are_shortened(self) -> None:
        quote = "word " * 100
        claim = make_claim(
            1, "Long quote claim.", status="supported", citations=[make_citation(61, quote=quote)]
        )
        markdown, _map = self.render([claim])
        self.assertIn("\u2026", markdown)
        self.assertNotIn("word " * 100, markdown)

    def test_slugify_is_filesystem_safe(self) -> None:
        self.assertEqual(slugify("Widget Report: Phase 3!"), "widget-report-phase-3")
        self.assertEqual(slugify(""), "report")


if __name__ == "__main__":
    unittest.main()

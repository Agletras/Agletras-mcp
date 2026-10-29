"""Tests for passage extraction (Phase 3): offsets, hashes, locators."""

from __future__ import annotations

import unittest

from agletras_mcp import passages
from agletras_mcp.passages import make_locator, passage_hash, split_passages


class PassageSplitTests(unittest.TestCase):
    def test_offsets_and_hashes_are_exact(self) -> None:
        text = "## One\nFirst paragraph with words.\n\n## Two\nSecond paragraph with more words."
        result = split_passages(text)
        self.assertEqual(len(result), 2)
        for passage in result:
            with self.subTest(index=passage.index):
                self.assertEqual(text[passage.start : passage.end], passage.text)
                self.assertEqual(passage_hash(passage.text), passage.passage_hash)
                self.assertEqual(len(passage.passage_hash), 64)

    def test_paragraphs_sharing_context_pack_together(self) -> None:
        text = "First paragraph with words.\n\nSecond paragraph with more words."
        result = split_passages(text)
        self.assertEqual(len(result), 1)
        passage = result[0]
        self.assertEqual(text[passage.start : passage.end], passage.text)
        self.assertIn("First paragraph", passage.text)
        self.assertIn("Second paragraph", passage.text)

    def test_empty_and_whitespace_text_yields_nothing(self) -> None:
        self.assertEqual(split_passages(""), [])
        self.assertEqual(split_passages("   \n\n  \t"), [])

    def test_deterministic_split(self) -> None:
        text = "\n\n".join(f"Paragraph {i} has some content to pack." for i in range(20))
        first = split_passages(text)
        second = split_passages(text)
        self.assertEqual(
            [(p.start, p.end, p.passage_hash) for p in first],
            [(p.start, p.end, p.passage_hash) for p in second],
        )

    def test_page_markers_set_the_page_and_are_consumed(self) -> None:
        text = "--- page 1/2 ---\nAlpha body text here.\n--- page 2/2 ---\nBeta body text here."
        result = split_passages(text)
        self.assertEqual([p.page for p in result], [1, 2])
        for passage in result:
            self.assertNotIn("---", passage.text)
            self.assertEqual(passage.locator, make_locator(passage.page, None))

    def test_markdown_headings_become_section_locators(self) -> None:
        text = "# Introduction\nIntro body text here.\n\n## Results\nResult body text here."
        result = split_passages(text)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].heading, "Introduction")
        self.assertEqual(result[1].heading, "Results")
        self.assertEqual(result[1].locator, "section: Results")
        # Passages are exact slices: the heading line stays in the text.
        self.assertTrue(result[0].text.startswith("# Introduction"))
        self.assertEqual(text[result[1].start : result[1].end], result[1].text)

    def test_small_passages_pack_up_to_the_cap(self) -> None:
        paragraph = "Short line of text."
        text = "\n\n".join([paragraph] * 4)
        packed = split_passages(text, max_chars=200)
        self.assertLessEqual(len(packed), 2)
        for passage in packed:
            self.assertLessEqual(len(passage.text), 200)

    def test_long_paragraph_splits_at_sentences(self) -> None:
        sentence = "This is one complete sentence with a clear boundary. "
        text = sentence * 60  # ~3300 chars
        result = split_passages(text, max_chars=500)
        self.assertGreater(len(result), 1)
        for passage in result[:-1]:
            with self.subTest(index=passage.index):
                self.assertLessEqual(len(passage.text), 500)
                self.assertTrue(passage.text.endswith("."), msg=passage.text[-30:])

    def test_trailing_mini_passage_merges_back(self) -> None:
        text = "A full paragraph of reasonable length and content. tiny"
        result = split_passages(text, max_chars=500, min_chars=120)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].text.endswith("tiny"))

    def test_oversized_paragraph_without_sentence_breaks_is_cut(self) -> None:
        text = "x" * 900 + " " + "y" * 400
        result = split_passages(text, max_chars=600)
        self.assertGreater(len(result), 1)
        for passage in result:
            self.assertEqual(text[passage.start : passage.end], passage.text)
            self.assertTrue(passage.text)

    def test_max_passages_cap_is_enforced(self) -> None:
        original = passages.MAX_PASSAGES
        try:
            passages.MAX_PASSAGES = 3
            text = "\n\n".join(
                f"## Section {i}\nParagraph number {i} with distinct content." for i in range(10)
            )
            result = split_passages(text, max_chars=500)
            self.assertEqual(len(result), 3)
        finally:
            passages.MAX_PASSAGES = original


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Generate the sample research PDF used by tests and manual verification.

Pure standard library: builds a deterministic, spec-valid PDF (with correct
xref offsets and /Info metadata) so PDF extraction can be verified without
downloading anything or installing typesetting tools.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SAMPLE_TITLE = "Agentic Research Pipelines: A Sample Paper"
SAMPLE_AUTHOR = "A. Author, B. Author — Agletras Research (fictional sample)"

SAMPLE_PAGES: list[list[str]] = [
    [
        SAMPLE_TITLE,
        SAMPLE_AUTHOR,
        "",
        "Abstract",
        "This sample PDF exists to verify the agletras-mcp extract_pdf tool.",
        "It contains distinctive markers: AGLETRAS_SAMPLE_2026 and PDF_OK_TOKEN.",
        "",
        "Keywords: agentic research, MCP, zero-cost tooling, Linux",
    ],
    [
        "1. Introduction",
        "The built-in web search backend was unavailable during Phase 0 auditing,",
        "so retrieval targets explicit URLs and locally provided documents.",
        "AGLETRAS_SAMPLE_2026 appears on this page as a second-page marker.",
        "",
        "2. Conclusion",
        "PDF text extraction is verified end to end with pypdf.",
    ],
]


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _content_stream(lines: list[str]) -> str:
    parts = ["BT", "/F1 14 Tf", "20 TL", "72 720 Td"]
    for index, line in enumerate(lines):
        if index:
            parts.append("T*")
        parts.append(f"({_escape(line)}) Tj")
    parts.append("ET")
    return "\n".join(parts)


def build_pdf(
    pages: list[list[str]],
    *,
    title: str = SAMPLE_TITLE,
    author: str = SAMPLE_AUTHOR,
    subject: str = "Sample PDF for agletras-mcp verification",
) -> bytes:
    """Build a small, valid PDF from one or more pages of text lines."""
    if not pages:
        pages = [[]]
    n_pages = len(pages)
    page_obj_nums = [3 + 2 * index for index in range(n_pages)]
    content_obj_nums = [4 + 2 * index for index in range(n_pages)]
    font_num = 3 + 2 * n_pages
    info_num = font_num + 1

    objects: list[str] = []
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}

    def add_object(number: int, body: str) -> None:
        offsets[number] = len(data)
        data.extend(f"{number} 0 obj\n".encode("ascii"))
        data.extend(body.encode("cp1252", errors="replace"))
        data.extend(b"\nendobj\n")

    kids = " ".join(f"{number} 0 R" for number in page_obj_nums)
    add_object(1, "<< /Type /Catalog /Pages 2 0 R >>")
    add_object(2, f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>")
    for index, lines in enumerate(pages):
        content = _content_stream(lines)
        add_object(
            page_obj_nums[index],
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_obj_nums[index]} 0 R "
                f"/Resources << /Font << /F1 {font_num} 0 R >> >> >>"
            ),
        )
        add_object(
            content_obj_nums[index],
            f"<< /Length {len(content.encode('cp1252', errors='replace'))} >>\nstream\n{content}\nendstream",
        )
    add_object(font_num, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    add_object(
        info_num,
        (
            f"<< /Title ({_escape(title)}) /Author ({_escape(author)}) "
            f"/Subject ({_escape(subject)}) /Creator (agletras-mcp sample generator) >>"
        ),
    )

    total_objects = len(offsets)
    xref_pos = len(data)
    data.extend(f"xref\n0 {total_objects + 1}\n".encode("ascii"))
    data.extend(b"0000000000 65535 f \n")
    for number in range(1, total_objects + 1):
        data.extend(f"{offsets[number]:010d} 00000 n \n".encode("ascii"))
    data.extend(
        (
            f"trailer\n<< /Size {total_objects + 1} /Root 1 0 R /Info {info_num} 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(data)


def main() -> None:
    target_dir = PROJECT_ROOT / "samples"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "sample-research-paper.pdf"
    target.write_bytes(build_pdf(SAMPLE_PAGES))
    print(f"wrote {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

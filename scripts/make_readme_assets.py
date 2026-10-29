#!/usr/bin/env python3
"""Generate the SVG charts used by README.md.

The charts are built from the measured values recorded in
``docs/phase8-measurements.md`` and one stored verification run.  They are
plain SVG (no Matplotlib, no external fonts, no network) so they render on
GitHub in both light and dark mode.

Run from the project root:

    python scripts/make_readme_assets.py
"""

from __future__ import annotations

import math
from html import escape
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSETS = PROJECT_ROOT / "docs" / "assets"

FONT = (
    "ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "Helvetica, Arial, sans-serif"
)

STYLE = f"""
    text {{ font-family: {FONT}; }}
    .title {{ font-size: 24px; font-weight: 700; fill: #1f2328; }}
    .subtitle {{ font-size: 14px; fill: #57606a; }}
    .label {{ font-size: 13px; fill: #1f2328; }}
    .muted {{ font-size: 12px; fill: #57606a; }}
    .small {{ font-size: 11px; fill: #57606a; }}
    .value {{ font-size: 14px; font-weight: 700; fill: #1f2328; }}
    .big {{ font-size: 32px; font-weight: 700; fill: #1f2328; }}
    .count {{ font-size: 20px; font-weight: 700; fill: #1f2328; }}
    .card {{ fill: #f6f8fa; stroke: #d0d7de; stroke-width: 1; }}
    .chip {{ fill: #ffffff; stroke: #d0d7de; stroke-width: 1; }}
    .grid {{ stroke: #d0d7de; stroke-width: 1; }}
    .baseline {{ stroke: #bf8700; stroke-width: 2; stroke-dasharray: 5 5; }}
    .track {{ fill: none; stroke: #d8dee4; }}
    .blue {{ fill: #2f81f7; }}
    .green {{ fill: #2da44e; }}
    .purple {{ fill: #8250df; }}
    .amber {{ fill: #bf8700; }}
    .red {{ fill: #cf222e; }}
    .gray {{ fill: #8b949e; }}
    .ring-blue {{ fill: none; stroke: #2f81f7; }}
    .ring-green {{ fill: none; stroke: #2da44e; }}
    @media (prefers-color-scheme: dark) {{
      .title, .label, .value, .big, .count {{ fill: #e6edf3; }}
      .subtitle, .muted, .small {{ fill: #8b949e; }}
      .card {{ fill: #161b22; stroke: #30363d; }}
      .chip {{ fill: #0d1117; stroke: #30363d; }}
      .grid {{ stroke: #30363d; }}
      .track {{ stroke: #30363d; }}
    }}
"""


def _text(x: float, y: float, value: str, cls: str = "label",
          anchor: str = "start", extra: str = "") -> str:
    return (
        f'<text x="{x:g}" y="{y:g}" class="{cls}" '
        f'text-anchor="{anchor}"{extra}>{escape(value)}</text>'
    )


def _svg(width: int, height: int, title: str, desc: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" role="img" '
        f'aria-labelledby="title desc">\n'
        f"  <title id=\"title\">{escape(title)}</title>\n"
        f"  <desc id=\"desc\">{escape(desc)}</desc>\n"
        f"  <style>{STYLE}</style>\n"
        f'  <rect class="card" x="1" y="1" width="{width - 2}" '
        f'height="{height - 2}" rx="16"/>\n'
        f"{body}\n"
        f"</svg>\n"
    )


def _ring(cx: float, cy: float, radius: float, value: float,
          ring_class: str, label: str, sublabel: str) -> str:
    circumference = 2 * math.pi * radius
    dash = circumference * value
    return "\n".join(
        [
            f'<circle cx="{cx:g}" cy="{cy:g}" r="{radius:g}" '
            f'class="track" stroke-width="18"/>',
            f'<circle cx="{cx:g}" cy="{cy:g}" r="{radius:g}" '
            f'class="{ring_class}" stroke-width="18" stroke-linecap="round" '
            f'stroke-dasharray="{dash:.2f} {circumference - dash:.2f}" '
            f'transform="rotate(-90 {cx:g} {cy:g})"/>',
            _text(cx, cy + 4, f"{value:.2f}", "big", "middle"),
            _text(cx, cy + 30, f"{value * 100:.0f}%", "muted", "middle"),
            _text(cx, cy + radius + 52, label, "label", "middle"),
            _text(cx, cy + radius + 72, sublabel, "small", "middle"),
        ]
    )


def retrieval_accuracy() -> str:
    width, height = 960, 470
    body: list[str] = [
        _text(40, 50, "Project-memory retrieval accuracy", "title"),
        _text(
            40,
            78,
            "Labeled corpus: 6 topics × 4 passages · 6 queries · top-5 retrieval",
            "subtitle",
        ),
        _ring(300, 215, 68, 1.0, "ring-blue", "Precision@5", "6 of 6 topics perfect"),
        _ring(660, 215, 68, 1.0, "ring-green", "Recall@5", "6 of 6 topics perfect"),
    ]

    topics = ["heliotrope", "obsidian", "quartzite", "marlin", "pelican", "tundra"]
    for index, topic in enumerate(topics):
        x = 40 + index * 144
        body.append(
            f'<rect class="chip" x="{x}" y="370" width="136" height="54" rx="10"/>'
        )
        body.append(_text(x + 12, 393, topic, "small"))
        body.append(_text(x + 12, 414, "P 1.00 · R 1.00", "value"))

    body.append(
        _text(
            40,
            450,
            "Measured by scripts/evaluate_phase8.py on a temporary store · "
            "synthetic labeled corpus · no external network.",
            "small",
        )
    )
    return _svg(
        width,
        height,
        "Project-memory retrieval accuracy",
        "Precision at 5 and recall at 5 are both 1.00 across six labeled topics.",
        "\n".join(body),
    )


def parallel_speedup() -> str:
    width, height = 960, 430
    chart_x, chart_y, chart_w, chart_h = 330, 120, 540, 220
    max_value = 5.0
    body: list[str] = [
        _text(40, 50, "Parallel execution speedup", "title"),
        _text(
            40,
            78,
            "Same workload, sequential vs bounded parallel execution · 4 workers",
            "subtitle",
        ),
    ]

    for tick in range(0, 6):
        x = chart_x + tick * chart_w / 5
        body.append(
            f'<line class="grid" x1="{x:g}" y1="{chart_y}" '
            f'x2="{x:g}" y2="{chart_y + chart_h}"/>'
        )
        body.append(_text(x, 362, f"{tick}×", "small", "middle"))

    baseline_x = chart_x + chart_w / 5
    body.append(
        f'<line class="baseline" x1="{baseline_x:g}" y1="{chart_y - 8}" '
        f'x2="{baseline_x:g}" y2="{chart_y + chart_h + 6}"/>'
    )
    body.append(_text(baseline_x + 8, 114, "1× = sequential", "small"))

    rows = [
        ("8 sleep tasks", "0.802 s → 0.202 s", 3.97, "blue"),
        ("8 URLs, 0.12 s latency", "1.074 s → 0.287 s", 3.74, "green"),
        ("6 monitored targets", "0.797 s → 0.278 s", 2.87, "purple"),
    ]
    for index, (name, times, speedup, color) in enumerate(rows):
        y = 148 + index * 70
        bar_w = speedup / max_value * chart_w
        body.append(
            f'<rect class="{color}" x="{chart_x}" y="{y}" width="{bar_w:.2f}" '
            f'height="38" rx="7"/>'
        )
        body.append(_text(chart_x - 16, y + 17, name, "label", "end"))
        body.append(_text(chart_x - 16, y + 34, times, "small", "end"))
        body.append(
            _text(chart_x + bar_w + 10, y + 24, f"{speedup:.2f}×", "value")
        )

    body.append(_text(chart_x, 362, "0×", "small", "middle"))
    body.append(
        _text(
            40,
            405,
            "Measured on a throwaway local HTTP server · parallel mode is opt-in; "
            "sequential execution remains the default.",
            "small",
        )
    )
    return _svg(
        width,
        height,
        "Parallel execution speedup",
        "Measured speedups of 3.97x, 3.74x, and 2.87x over sequential execution.",
        "\n".join(body),
    )


def verification_outcomes() -> str:
    width, height = 960, 500
    total = 51
    outcomes = [
        ("Supported", "Directly supported by exact passages", 23, "green"),
        ("Partially supported", "Covered, but not fully established", 21, "amber"),
        ("Contradicted", "Stored evidence disagrees", 4, "red"),
        ("Conflicting evidence", "Independent sources disagree", 3, "purple"),
        ("Unsupported", "No supporting passage found", 0, "gray"),
        ("Unverifiable", "Evidence cannot settle the claim", 0, "gray"),
    ]

    body: list[str] = [
        _text(40, 50, "Claim verification outcomes", "title"),
        _text(
            40,
            78,
            "One research run · 51 claims · deterministic checks over exact stored passages",
            "subtitle",
        ),
        '<defs><clipPath id="barclip"><rect x="40" y="120" width="880" '
        'height="55" rx="12"/></clipPath></defs>',
    ]

    x = 40.0
    for name, _description, count, color in outcomes[:4]:
        segment = count / total * 880
        body.append(
            f'<rect class="{color}" x="{x:.2f}" y="120" width="{segment:.2f}" '
            f'height="55" clip-path="url(#barclip)"/>'
        )
        label = f"{count} {name.lower()}" if segment > 150 else str(count)
        body.append(
            _text(x + segment / 2, 153, label, "value", "middle",
                  ' fill="#ffffff"')
        )
        x += segment

    legend_y = 210
    legend = [
        (40, "Supported", 23, "green"),
        (280, "Partially supported", 21, "amber"),
        (570, "Contradicted", 4, "red"),
        (770, "Conflicting", 3, "purple"),
    ]
    for lx, name, count, color in legend:
        body.append(
            f'<rect class="{color}" x="{lx}" y="{legend_y - 9}" width="10" '
            f'height="10" rx="2"/>'
        )
        body.append(
            _text(lx + 18, legend_y, f"{name} — {count}", "small")
        )

    for index, (name, description, count, color) in enumerate(outcomes):
        column = index % 2
        row = index // 2
        cx = 40 + column * 460
        cy = 244 + row * 62
        body.append(
            f'<rect class="chip" x="{cx}" y="{cy}" width="420" height="52" rx="10"/>'
        )
        body.append(
            f'<rect class="{color}" x="{cx}" y="{cy}" width="5" height="52" rx="2"/>'
        )
        body.append(_text(cx + 18, cy + 22, name, "label"))
        body.append(_text(cx + 18, cy + 39, description, "small"))
        body.append(
            _text(cx + 402, cy + 33, str(count), "count", "end")
        )

    body.append(
        _text(
            40,
            470,
            "Statuses are evidence-relative labels, not truth probabilities. "
            "Contradictions and conflicts are never auto-resolved.",
            "small",
        )
    )
    body.append(
        _text(
            40,
            486,
            "The supported total includes one claim supported through citation "
            "relations without a separate verification record.",
            "small",
        )
    )
    return _svg(
        width,
        height,
        "Claim verification outcomes",
        "A stacked bar and status cards summarize 51 claim outcomes from one research run.",
        "\n".join(body),
    )


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    assets = {
        "retrieval-accuracy.svg": retrieval_accuracy(),
        "parallel-speedup.svg": parallel_speedup(),
        "verification-outcomes.svg": verification_outcomes(),
    }
    for name, content in assets.items():
        target = ASSETS / name
        target.write_text(content, encoding="utf-8")
        print(f"wrote {target.relative_to(PROJECT_ROOT)} ({len(content)} bytes)")


if __name__ == "__main__":
    main()

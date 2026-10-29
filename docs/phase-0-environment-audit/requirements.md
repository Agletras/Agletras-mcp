# Phase 0 — Requirements Specification (Deliverable C) & Project Readiness & Risks (Deliverable F)

- **Project:** Agentic Research + MCP · Linux workstation · **₹0 budget**
- **Date:** 2026-10-17
- **Companion documents:** `environment-inventory.md` (inventory, capabilities, models), `dependency-plan.md` (dependency plan), `audit-checklist.md` (checklist, evidence, limitations)
- **Hard exclusions:** No Ollama (not detected, not used, not recommended, excluded from architecture). No paid APIs, paid search, paid hosting, mandatory subscriptions, token-based services.

---

## C. Requirements Specification — Minimum Viable Product

### C.1 Project goal (restated)

An **agentic research system** built on OpenCode that uses **MCP (Model Context Protocol)** tooling to collect sources, extract content, track citations, store results locally, and export research reports — operating entirely on **already-available, verified zero-cost components** on this machine.

### C.2 Smallest practical MVP scope

The MVP is deliberately narrow. Everything below maps to a **verified** environment capability.

| ID | Requirement | Environment mapping (verified) | MVP? |
|---|---|---|---|
| MR-1 | Agent runtime executes research tasks with tool use (files, shell, HTTP fetch) | OpenCode v2.0.29 + 10 free models; agentic write+shell test passed; `webfetch` works inside agent sessions | **Yes** |
| MR-2 | Research orchestration: a lead agent fans out to subagents for parallel research/exploration | Built-in agents: `build`, `plan`, `explore` (subagent), `general` (subagent) | **Yes** |
| MR-3 | A project MCP server exposing research tools (e.g., fetch source, save source, list sources, export) | Node.js/npm/npx **and** Python 3.14 + venv+pip available to build/run either Python or Node MCP server | **Yes** |
| MR-4 | Source collection: retrieve specific URLs and extract readable text | `webfetch` tool (verified), `curl`/`wget` (verified), Python `requests` (verified) | **Yes** |
| MR-5 | Web discovery/search of *new* sources | **BLOCKED** — built-in Web Search (Firecrawl-backed) returns empty results at both harness and agent-session level | **Deferred** → Phase 1 decision: either retest, or implement a custom zero-cost search tool inside the project MCP server (e.g., public APIs such as Wikipedia/arXiv/DuckDuckGo HTML endpoint — to be evaluated for terms/limits) |
| MR-6 | Citation and source tracking (ID, URL, title, retrieved-at, content hash, notes) | Implement in the project's own SQLite store + JSON/Markdown sidecars; no external dependency needed | **Yes** |
| MR-7 | Local research storage and caching | SQLite 3.54 (CLI + Python module verified) | **Yes** |
| MR-8 | PDF text extraction | **BLOCKED** — poppler not installed | Phase 1 install candidate (`poppler`) **only if** PDFs are in scope |
| MR-9 | Optional OCR for scanned documents/images | `tesseract` absent | Post-MVP optional |
| MR-10 | Image discovery and image metadata analysis | ImageMagick + Python Pillow + libexif verified; image *search* depends on MR-5 | Metadata **Yes** (basic); image discovery **deferred with MR-5** |
| MR-11 | Export research reports | Markdown native; HTML via Python stdlib; archives via tar/zip | **Yes** (Markdown/HTML). Pandoc/PDF export optional later |
| MR-12 | Terminal + filesystem tooling for the agent and scripts | Verified end-to-end (write/shell test `AGENTIC_OK`) | **Yes** |
| MR-13 | Zero-cost operation | All models tested are ₹0; no credentials exist or are needed; no paid services in plan | **Yes — hard constraint** |

### C.3 Non-functional requirements

1. **Budget:** ₹0. Only locally installed free tooling and verified free model access. No payment method required anywhere in the flow.
2. **No Ollama:** excluded absolutely (architecture, dependencies, scripts, docs).
3. **Local-first data:** research data lives in the project directory (SQLite + files). No cloud storage.
4. **No hidden credentials:** the system must run with **zero configured credentials** (currently true — `opencode auth list` empty).
5. **Reproducibility:** record exact model IDs, variants, and fallbacks in project config (models drift — see E.3 in inventory).
6. **Safety:** keep agent permissions restrictive; `session.permissions=autoaccept` currently auto-approves — Phase 1 should consider tightening before unattended runs.
7. **Plain-text outputs:** Markdown + JSON + SQLite only.

### C.4 Readiness assessment (Section 6 of the audit brief)

| Capability area | Readiness | Basis |
|---|---|---|
| Agentic research workflows | **READY (with MR-5 caveat)** | Agentic tool-use test passed; subagents exist; free models proven |
| MCP tool orchestration | **READY to build** (nothing to *reuse* — no MCP servers exist) | Both viable stacks present: Node 24 + npm/npx; Python 3.14 + venv/pip; SQLite available |
| Source collection & citation tracking | **READY for explicit URLs**; discovery blocked | `webfetch`/curl/requests verified; storage verified; search backend failing |
| Local file & document processing | **PARTIAL** | Filesystem OK; **PDF tooling absent**; office-format libs absent (venv-installable later) |
| PDF text extraction | **BLOCKED until Phase 1 install** | poppler not installed; free & small (pacman) |
| Optional OCR | **NOT READY (optional)** | tesseract absent; free & small; CPU-fine on a 6-core laptop |
| SQLite research storage/caching | **READY** | CLI + Python module verified; OpenCode itself already uses SQLite |
| Image discovery & metadata analysis | **PARTIAL** | Metadata/dimensions: ImageMagick + Pillow ready; discovery blocked with MR-5 |
| Research report export | **READY (Markdown/HTML)** | Markdown native; Python stdlib HTML; tar/zip present; pandoc/PDF optional later |

**Recommended minimum component set for the MVP:**
OpenCode (present) + 1 primary free model with 2 fallbacks (present) + project MCP server built with **one** stack (choose Python venv **or** Node — not both) + SQLite (present) + `webfetch`/curl (present) + Markdown export (present). **New installs needed for the bare MVP: none.** New installs needed only if PDF/OCR enter scope (`poppler`, optionally `tesseract`).

---

## F. Risks and Blockers (verified)

| # | Risk / blocker | Severity | Evidence (verified) | Mitigation (within ₹0) |
|---|---|---|---|---|
| R-1 | **Web search returns no results** (harness tool and in-agent Firecrawl-backed search) | **High** (if discovery is core to MVP) | 3/3 harness queries and 2/2 agent-session searches returned empty | Phase 1: re-test (may be transient backend issue); otherwise implement custom search tool in project MCP server using genuinely free public APIs (evaluate terms/limits); use known-URL retrieval meanwhile |
| R-2 | **No PDF tooling** (poppler absent) | Medium | `command -v pdftotext pdfinfo` → not found | Phase 1: install `poppler` (free, pacman) **only after user approval**; or scope MVP to HTML/text sources |
| R-3 | **No OCR** | Low (post-MVP) | `tesseract` absent | Optional later: `tesseract` + `tesseract-data-eng` |
| R-4 | **No pandoc** (PDF/DOCX export blocked) | Low | absent | MVP exports Markdown/HTML; pandoc optional later |
| R-5 | **Free-model tier limits unknown; model list drifts** | Medium | `exo-free` used historically, now gone; quotas not documented locally | Fallback chain of ≥3 working free models; pin IDs in project config; re-run `opencode models` check at Phase 1 start |
| R-6 | **`session.permissions=autoaccept` wide open** | Medium (safety) | `cli.json` verified | Tighten permissions before unattended agent runs (Phase 1 config task, user-approved) |
| R-7 | **No MCP servers exist yet** | Not a defect — scope | `opencode mcp list` empty | Build project MCP server in Phase 1 (runtime stacks verified working) |
| R-8 | **No git identity configured** | Low | `~/.gitconfig` missing | Set name/email before first commit (Phase 1, user input needed) |
| R-9 | **System pip absent (Arch policy)** | Low | `EXTERNALLY-MANAGED`; venv workaround verified | Always use venvs; never `sudo pip` |
| R-10 | **No browser automation** | Low | No browser binaries; `browser` permission denied by default | Keep out of MVP; static fetch covers research needs; revisit in a later phase if justified |
| R-11 | **ISP data cap unknown** | Low | Not auditable from host | Hosted free models + text-heavy workflows keep bandwidth modest |
| R-12 | Loaded dGPU (NVIDIA) unused | Info | `lspci` | Intentionally unused: no local inference needed; keeps ₹0 and avoids driver/ML complexity (and stays Ollama-free by architecture) |

### Open questions for the user (Phase 1 kickoff)
1. Is **web discovery/search** a hard MVP requirement (vs. research from known/supplied URLs)? → determines urgency of R-1 workaround.
2. Are **PDF inputs** in scope for the MVP? → determines whether `poppler` gets installed.
3. Preferred MCP server stack: **Python** (venv, stdlib-heavy) or **Node** (npm, npx)? → build only one.
4. Should agent permissions be tightened from `autoaccept` before running unattended research jobs?

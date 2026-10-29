# Phase 0 — Dependency Plan (Deliverable D)

- **Project:** Agentic Research + MCP · **₹0 software/service budget**
- **Date:** 2026-10-17
- **Policy:** Nothing is installed during Phase 0. Everything below marked "later" requires explicit user approval in a later phase. No Ollama, ever. No paid services, ever.

---

## D.1 Already available and reusable (no action, no install)

| Component | Verified status | Role in the MVP |
|---|---|---|
| **OpenCode v2.0.29** | Working; agentic tool-use verified end-to-end | Agent runtime + orchestration host for MCP |
| **10 free models** (`opencode` provider) | All 10 tested successfully (exit 0, correct output); zero cost | Research reasoning; use a primary + ≥2 fallbacks |
| **built-in agents** (`build`, `plan`, `explore`, `general`) | Present; subagents usable | Research orchestration (fan-out/parallel exploration) |
| **`webfetch` tool** | Verified at harness level *and* inside agent sessions | Source retrieval from known URLs |
| **Python 3.14.8 + venv (bundled pip 26.2.1)** | Verified working | Primary stack candidate for the project MCP server |
| **Node.js 24.22.0 + npm 11.19.1 + npx** | Verified working; npmjs registry configured | Alternative stack for the MCP server (choose ONE stack) |
| **SQLite 3.54 (CLI + Python module)** | Verified | Research storage, caching, citation index |
| **git 2.56.0** | Installed (identity not yet set) | Version control of project source |
| **jq, rg, file, curl, wget, tar/zip** | All verified | JSON processing, search-in-files, HTTP, archiving |
| **ImageMagick 7.1.2, Pillow 12.3.0** | Verified | Image dimensions/basic analysis/thumbnails |
| **Markdown authoring** | Produced these docs | Default report format |
| **python3 stdlib** (`sqlite3`, `json`, `urllib`, `hashlib`, `csv`, `html.parser`) | Present by definition | Zero-dependency backbone of the MCP server and export pipeline |

**Net new dependencies strictly required for the bare MVP: none.** ✅

## D.2 Required later (Phase 1+ — free, open-source, small; each needs approval)

| Dependency | Why it is genuinely needed | Install path (later) | Cost |
|---|---|---|---|
| **MCP SDK — one of:**<br>• Python: `mcp` package (pip, venv)<br>• Node: `@modelcontextprotocol/sdk` (npm) | The project's purpose is an MCP server for research tooling; OpenCode currently has **zero** MCP servers, and nothing in the base OS speaks MCP. Choose **one** stack matching your preference. | `python3 -m venv .venv && .venv/bin/pip install mcp` **or** `npm install @modelcontextprotocol/sdk` (project-local, no `-g`) | ₹0 |
| **poppler** (`pdftotext`, `pdfinfo`, `pdftoppm`) | **Only if** PDFs are in MVP scope. Currently the single biggest document gap. Small, classic, battle-tested. | `sudo pacman -S poppler` (paru available if needed) | ₹0 |
| **git identity configuration** (not a package) | Commits are impossible without `user.name`/`user.email` | `git config` (user's choice of identity) | ₹0 |

**Stack decision guidance:** Python venv is recommended if the MVP is "research + storage + export" (stdlib covers HTTP/JSON/SQLite; only the `mcp` package is added). Node is recommended only if you prefer the TS/JS ecosystem. **Do not install both stacks' dependencies** — that duplicates capability.

## D.3 Optional enhancements (defer until a concrete need appears)

| Dependency | Why you *might* want it | Trigger condition | Cost |
|---|---|---|---|
| **tesseract** + `tesseract-data-eng` | OCR for scanned PDFs/images | Only when scanned documents enter the corpus | ₹0 |
| **exiftool** | Rich image metadata (EXIF/GPS/dates) beyond what Pillow/libexif expose | Only if image metadata depth matters | ₹0 |
| **pandoc** | Multi-format export (DOCX/EPUB/PDF chains) | Only if Markdown/HTML export proves insufficient | ₹0 |
| **`requests`** (in venv) | Convenience HTTP vs stdlib `urllib` | Only if `urllib` friction is real; system already has requests 2.34.2 if venv uses `--system-site-packages` (not recommended) | ₹0 |
| **html5lib / lxml** | More robust HTML parsing | Only if stdlib `html.parser` breaks on real pages | ₹0 |
| **RSS support** (`feedparser`) | Auto-discovery of sources via feeds | Only after MR-5 (search/discovery) has a direction | ₹0 |
| **DuckDuckGo/wikipedia/arxiv client** | Custom search tooling inside the MCP server | Only after re-testing built-in search in Phase 1 (R-1) | ₹0 but **check each service's ToS/limits first** |

## D.4 Unnecessary dependencies to avoid (duplication, cost, or constraint violations)

| Avoid | Reason |
|---|---|
| **Ollama** (and any Ollama-dependent component) | **Hard project constraint.** Excluded from architecture, code, scripts, and docs. Local models are also unnecessary here: free hosted models are already verified working. |
| Paid LLM APIs (token-billed), paid search APIs, paid scraping services | Violates ₹0 budget. |
| Paid cloud infra (VM/storage/managed services) | Nothing in the MVP needs it; local-first design. |
| Mandatory subscriptions of any kind | ₹0 constraint. |
| **bun, uv, deno, pnpm/yarn** | npm/npx + venv+pip already verified; adding another package manager duplicates capability. |
| **Docker / docker-compose** for the MVP | Docker is installed, but the MCP server and scripts run natively; containers add complexity with zero benefit at this scale. |
| **LangChain / LlamaIndex / AutoGen / CrewAI** | OpenCode's own agents + subagents + MCP already provide orchestration; these frameworks would add large dependency trees and abstract over what we control natively. |
| **Browser automation (Playwright/Puppeteer/Selenium)** | No browser installed; `browser` tool denied by default; static `webfetch` covers current needs. Revisit only on a proven need. |
| **MongoDB/PostgreSQL/Redis** | SQLite (verified) satisfies storage/caching for a single-user research tool. |
| **Second vector/search/indexing engines (Elasticsearch etc.)** | Massive overkill; SQLite FTS (built-in, check availability at Phase 1) likely suffices. |
| **Duplicate HTTP stacks both system-wide and venv-installed** | Keep one path: stdlib first; add `requests` in venv only if truly needed. |
| System-wide `pip` installation | Arch policy (`EXTERNALLY-MANAGED`); venvs are the correct path. |

## D.5 Free-service assessment (confirmed vs. merely "free tiers")

**Confirmed free (verified by execution in this environment, 2026-10-17):**

| Service | What was verified | Caveats to record |
|---|---|---|
| OpenCode Zen free models via local OpenCode | 10/10 model IDs responded with exit 0; SDK reports $0.00 costs; **no credentials required**; no payment method present | Quotas/limits not documented in-environment; model roster changes over time (`exo-free` already deprecated). Not a contractual guarantee — re-verify each phase. |
| Local tooling (Python/Node/SQLite/poppler-to-be/etc.) | Verified present or free from distro repos | Arch-based rolling release: updates can change package versions between phases. |
| Direct web retrieval (`curl`/`wget`/`webfetch`) | Verified 200s and successful page extraction | Respect each site's robots/ToS when building automated collectors; rate-limit politely. |

**Merely "has a free tier" / not confirmed:**

| Service | Status |
|---|---|
| Firecrawl-backed built-in Web Search (as experienced through OpenCode) | **Failing** (returns empty) regardless of its commercial positioning; treat as unavailable until re-verified. Any future reliance on a service like this needs terms/limits review and must remain on a free plan. |
| Any third-party search/scrape API considered for MR-5 | Not evaluated yet; each candidate must be checked for ToS, rate limits, key requirements, and true ₹0 sustainability before adoption. |

**Explicitly out of scope:** anything requiring a credit card, paid quota, trial that converts to paid, or token billing.

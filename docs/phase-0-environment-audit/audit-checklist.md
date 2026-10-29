# Phase 0 — Audit Checklist, Evidence Log & Limitations (Deliverable G)

- **Project:** Agentic Research + MCP · Linux workstation · ₹0 budget
- **Audit date:** 2026-10-17, ~19:15–19:30 IST (host clock: `2026-10-17T19:19:34+05:30`)
- **Auditor:** OpenCode v2.0.29 session (model `opencode/deepseek-v4.1-flash-free`)
- **Safety statement:** Phase 0 was strictly read-only. **Nothing was installed, configured, downloaded, or committed.** No git repository was initialized. No secrets were read out or displayed (`service.json` key was listed-by-name only; `auth.json` does not exist). **No Ollama command was ever issued; Ollama was never detected, run, configured, recommended, or referenced in any test.** No paid operations were performed.

---

## G. Completion Checklist

Legend: ✅ passed · 🟡 partial · ❌ failed/blocked · ⚪ N/A

### Section 1 — Environment inspection

| # | Check | Result | Evidence |
|---|---|---|---|
| 1.1 | `command -v opencode python3 node npm npx bun uv` | ✅ done | Found: opencode, python3, node, npm, npx. **Not found: bun, uv** |
| 1.2 | `opencode mcp list` | ✅ done | Output: `No MCP servers configured` |
| 1.3 | Distro | ✅ | Arch-based rolling distribution (`/etc/os-release`) |
| 1.4 | Kernel | ✅ | 6.18.56-1-lts (`uname -a`) |
| 1.5 | CPU architecture | ✅ | x86_64; 6 cores / 12 threads (`lscpu`, `nproc`) |
| 1.6 | Shell | ✅ | fish 4.9.3 default; audit commands executed with bash |
| 1.7 | OpenCode version | ✅ | v2.0.29 (`opencode --version`) |
| 1.8 | Node/npm/npx versions & function | ✅ | node v24.22.0, npm 11.19.1, npx 11.19.1 (direct version calls) |
| 1.9 | Python version & function | ✅ | 3.14.8; venv created; `pip 26.2.1` inside venv |
| 1.10 | Bun availability | ✅ (verified absent) | `command -v bun` empty |
| 1.11 | uv availability | ✅ (verified absent) | `command -v uv` empty; no `~/.local/bin` |
| 1.12 | Git function | ✅ | git 2.56.0; **no global identity configured** (recorded) |
| 1.13 | Other dev tools | ✅ | jq 1.8.2, curl 8.22.0, wget 1.25.0, rg 15.2.0, file 5.48, sqlite3 3.54.0, ffmpeg n9.0.2, ImageMagick 7.1.2-33, fzf, bat, tar/zip/unzip/bsdtar, iconv |
| 1.14 | CPU cores | ✅ | 12 threads (6 cores) |
| 1.15 | RAM | ✅ | 16 GiB total, ~11 GiB available, 15 GiB swap |
| 1.16 | Disk capacity | ✅ | 86 GB free of 122 GB on `/` (shared with `/home`) |
| 1.17 | Python venv & package management | ✅ | venv works with bundled pip; **system pip absent** (Arch `EXTERNALLY-MANAGED`) |
| 1.18 | Project dir & repo structure | ✅ | `/home/user/agletras-mcp` **empty, no `.git`** → reports created at this path; no existing files overwritten |

### Section 2 — OpenCode configuration & models

| # | Check | Result | Evidence |
|---|---|---|---|
| 2.1 | Config files located | ✅ | `~/.config/opencode/{cli.json,service.json}`; no `opencode.json(c)` anywhere; `opencode debug config` lists only config dirs |
| 2.2 | Providers & model identifiers | ✅ | Single provider `opencode`; 10 model IDs (`opencode models`) |
| 2.3 | Default model / model-selection state | ✅ | No global pin; recent: `deepseek-v4.1-flash-free` (variant `max`) etc. (`model.json`) |
| 2.4 | Agents | ✅ | `build`, `plan` (primary); `explore`, `general` (subagents); hidden: `compaction`, `summary`, `title` |
| 2.5 | Tools available to agents | ✅ | Verified empirically: file write, shell exec, WebFetch worked in a live agent run |
| 2.6 | Plugins | ✅ (none) | `opencode plugin list` → "No plugins found" |
| 2.7 | Permissions | ✅ | build/plan: allow `*`; `*.env` reads = ask; **`browser` = deny**; `plan` cannot edit. Session permission mode: `autoaccept` (flagged as R-6) |
| 2.8 | MCP configuration | ✅ (none) | `opencode mcp list` → none; would be added in Phase 1 |
| 2.9 | Search / filesystem / terminal availability | ✅ | `webfetch` ✅ (harness + agent); filesystem+terminal ✅ (agentic test); **`websearch` ❌ (see 4.x)** |
| 2.10 | Credentials | ✅ | `opencode auth list` → **no authenticated integrations**; no keys exist locally for this project |
| 2.11 | Model test — inference on every listed model | ✅ **10/10** | `opencode run -m opencode/<id> "Reply with exactly: PING_OK…"` → all exit 0, exact `PING_OK`; raw outputs in `/tmp/opencode/phase0/*.out|.exit` |
| 2.12 | Agentic integration test (tool calling) | ✅ | `glm-5.3-flash-free` with `--auto` created `/tmp/opencode/phase0/agentic-test.txt` and ran `cat` → `AGENTIC_OK` verified on disk |
| 2.13 | Paid-usage avoidance in tests | ✅ | Only zero-cost models used; no new credentials; no config changes |
| 2.14 | Vision (image input) testing | 🟡 | VL-labelled model responds to text; image-input path **untested** (deliberately: would require generating test artifacts; deferred) |

### Section 3 — MCP servers & research capabilities

| # | Check | Result | Evidence / finding |
|---|---|---|---|
| 3.1 | Enumerate MCP servers | ✅ | **None configured** ("No MCP servers configured") — verified twice |
| 3.2 | Web search capability | ❌ **FAILING** | Harness `websearch`: 3/3 queries → "No search results found". Agent-session "Web Search via Firecrawl": 2/2 searches → empty results |
| 3.3 | Webpage retrieval / content extraction | ✅ | Harness webfetch fetched `example.com`; agent WebFetch fetched and reported "Example Domain"; full markdown extraction verified on `opencode.ai/docs` |
| 3.4 | Filesystem operations | ✅ | Agentic write+read test passed; standard file tooling present |
| 3.5 | Research orchestration | ✅ (capability present) | `explore`/`general` subagents listed; not exercised for a real research task in Phase 0 (no implementation phase) |
| 3.6 | Citation & source tracking | ⚪ (not testable yet) | No citation component exists yet; SQLite + Markdown available to build it (MVP MR-6) |
| 3.7 | PDF & document processing | ❌ absent | poppler/tesseract/pandoc all missing (see 4.x) |
| 3.8 | Image search & visual research | 🟡 | Local image tools present (ImageMagick/Pillow); image *search* depends on failing search (3.2) |
| 3.9 | Metadata extraction | 🟡 | ImageMagick identify/Pillow work; **exiftool absent**; PDF metadata (pdfinfo) absent |
| 3.10 | Browser automation | ❌ unavailable | No browser binaries; `browser` permission denied |
| 3.11 | Git & local project management | ✅ | git 2.56.0 functional; identity unset; used only for read-only checks |
| 3.12 | Unknown MCP scripts / server-definition changes | ✅ none performed | Compliant with safety rules |

### Section 4 — Document utilities & Section 5 — Zero-cost constraints

| # | Check | Result | Evidence |
|---|---|---|---|
| 4.1 | `pdftotext` | ❌ absent | `command -v` → not found (poppler not installed) |
| 4.2 | `tesseract` | ❌ absent | `command -v` → not found |
| 4.3 | `exiftool` | ❌ absent | `command -v` → not found (libexif library present, no CLI) |
| 4.4 | `pandoc` | ❌ absent | `command -v` → not found |
| 4.5 | `jq` | ✅ | 1.8.2 — categorized Essential |
| 4.6 | `curl` | ✅ | 8.22.0; live HTTPS tests 200 OK — Essential |
| 4.7 | `wget` | ✅ | 1.25.0 — Optional |
| 4.8 | `git` | ✅ | 2.56.0 — Essential for the project (identity pending) |
| 4.9 | Python libraries audit | ✅ done | System has requests/PyYAML/Pillow; **missing** pypdf, bs4, lxml, jinja2, pydantic, pytest, python-magic, etc. → venv plan |
| 4.10 | ₹0 budget compliance in findings | ✅ | All proposed components are free/local; no paid service proposed; "free tier" vs "confirmed free" separated in `dependency-plan.md` §D.5 |
| 4.11 | Ollama exclusion | ✅ | Never detected/invoked; excluded from all documents |

### Section 6 — Readiness & Section 7–9 — Deliverables produced

| # | Check | Result | Evidence |
|---|---|---|---|
| 6.1 | Readiness assessment performed | ✅ | `requirements.md` §C.4 with per-capability verdicts |
| 6.2 | Deliverable A — environment inventory | ✅ | `environment-inventory.md` §A (tables incl. verification evidence + action required) |
| 6.3 | Deliverable B — verified capabilities | ✅ | `environment-inventory.md` §B (works / configured-only / failing / absent) |
| 6.4 | Deliverable C — requirements spec | ✅ | `requirements.md` §C (MR-1…MR-13 + non-functional) |
| 6.5 | Deliverable D — dependency plan | ✅ | `dependency-plan.md` (available / required later / optional / avoid + justifications) |
| 6.6 | Deliverable E — model & provider assessment | ✅ | `environment-inventory.md` §E (10/10 tested; credentials/cost caveats) |
| 6.7 | Deliverable F — risks & blockers | ✅ | `requirements.md` §F (12 risks, R-1 high) |
| 6.8 | Deliverable G — this checklist | ✅ | This file |
| 6.9 | Reports saved without overwriting anything | ✅ | Target directory did not exist before; workspace was empty; nothing overwritten |
| 6.10 | No git init / no commits | ✅ | Compliant |
| 6.11 | Phase 1 implementation not started | ✅ | Compliant — no app code, no installs, no config edits |

---

## Errors encountered (and how they were handled)

| Error | Handling |
|---|---|
| `python3 -m pip --version` → "No module named pip" | Expected on Arch (externally managed). Verified the correct path instead: `python3 -m venv` + bundled pip 26.2.1. Recorded as R-9, not a failure. |
| `websearch` tool returned "No search results found" for all queries | Re-tested with 2 more queries at harness level **and** verified the same behavior inside an agent session (via WebFetch/Web Search transcript). Recorded as verified failure R-1 — **not** reported as "search works". |
| `git config --global --list` → "unable to read config file" | No global gitconfig exists; recorded as an action item (set identity later), not an error. |
| `opencode mcp list` produced trailing shell noise (`hBc`) in the captured exit line | Cosmetic shell-capture artifact only; the substantive output line ("No MCP servers configured") was clear and was cross-verified by a second run. |
| `ls ~/.config/opencode/mcp*` → "No such file" | Confirms absence; consistent with "no MCP servers configured". |

## Limitations of this audit (recorded honestly)

1. **Repository identity:** the working directory was empty with no git metadata, so no existing "Agentic Research + MCP repository" could be identified. Per instructions, reports were saved under the clearly named directory `docs/phase-0-environment-audit/` in the project workspace.
2. **Model tests were availability tests** (exit code + exact expected output), plus one agentic tool-use test. They do **not** measure reasoning/research quality, long-context behavior, or reliability under load.
3. **Free-tier quotas and terms could not be verified from the host** — "free" is confirmed only as of the audit date in this environment (zero cost fields, no billing, successful inference).
4. **Search failures may be transient** (backend/rate-limit). Re-verification is a Phase 1 task (R-1).
5. **Vision input capability** of the VL model was not exercised.
6. **OCR/PDF capabilities** were not testable because the utilities are absent; "absent" is the verified state.
7. **No outbound bandwidth measurements** were taken (cannot infer ISP caps from the device side).
8. Standard caveat: rolling-release distro — package/model availability can drift between phases; re-check at the start of each phase.

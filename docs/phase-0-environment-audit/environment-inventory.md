# Phase 0 — Environment Inventory (Deliverable A) & Verified Capabilities (Deliverable B) & Model Assessment (Deliverable E)

- **Project:** Agentic Research + MCP
- **Host:** Linux workstation (Arch-based, Sway) · `user@host`
- **Audit date:** 2026-10-17 (IST, UTC+05:30)
- **Auditor:** OpenCode v2.0.29 agent session (model: `opencode/deepseek-v4.1-flash-free`)
- **Method:** Read-only inspection. No packages installed, no configuration changed, no paid operations, **no Ollama command issued, detected, or referenced in testing**. Model tests used only zero-cost models already configured in OpenCode.
- **Repository note:** `/home/user/agletras-mcp` was inspected and found **empty and not a Git repository**. It is treated as the project workspace root for these reports (see `audit-checklist.md` → Limitations).

---

## A. Environment Inventory

Legend for *Verification evidence*: all commands were executed during this audit; outputs summarized. Raw model-test outputs preserved in `/tmp/opencode/phase0/`.

### A.1 Operating system, hardware, shell

| Component | Installed/configured | Version/status | Verification evidence | Action required |
|---|---|---|---|---|
| Linux distribution | Yes | Arch-based rolling distribution | `cat /etc/os-release` | None |
| Kernel | Yes | 6.18.56-1-lts (`#1 SMP PREEMPT_DYNAMIC`) | `uname -a` | None |
| Architecture | Yes | x86_64 | `uname -m`, `lscpu` | None |
| Shell (login) | Yes | fish 4.9.3 (`/usr/bin/fish`) | `$SHELL`, `fish --version` | None |
| Desktop/compositor | Yes | swayfx 0.6 (based on sway 1.12.0) | `sway --version` | None |
| CPU | Yes | x86_64, 6 cores / 12 threads | `lscpu`, `nproc` (12) | None — ample for this project |
| RAM | Yes | 16 GiB total; ~11 GiB available | `free -h`, `free -m` | None |
| Swap | Yes | 15 GiB, unused | `free -h` | None |
| Disk | Yes | 122 GB filesystem for `/` and `/home`; **86 GB available** (29% used) | `df -h / /home` | Monitor only |
| GPU | Yes | Discrete GPU + integrated GPU | `lspci` | Not needed — project uses free hosted models, not local inference |
| User privileges | `user` (non-root), `sudo` presumed available for later installs | `id -un` | Use `sudo`/`paru` only in later, approved phases |
| Fonts | 1,065 font entries | `fc-list \| wc -l` | None |

### A.2 OpenCode installation and configuration

| Component | Installed/configured | Version/status | Verification evidence | Action required |
|---|---|---|---|---|
| OpenCode | Yes | **v2.0.29** at `~/.opencode/bin/opencode` | `opencode --version`; `command -v opencode` | Keep; upgrade optional, not required |
| Background service | Running | `http://127.0.0.1:49374` | `opencode service status` | None |
| Config directory | Yes | `~/.config/opencode/` contains only `cli.json`, `service.json` | directory listing | None |
| `cli.json` | Yes | `debug.devtools=true`, `session.permissions="autoaccept"`, `theme.mode="dark"` | `cat ~/.config/opencode/cli.json` (contains no secrets) | **Review `autoaccept`** — auto-approves tool actions; consider stricter permissions later |
| `service.json` | Yes | Contains a service authentication key (value **withheld/redacted**; not printed) | `jq keys` only | None |
| Global/project `opencode.json(c)` | **Not present** | No model/provider/MCP config files exist | `opencode debug config` → only config *directories* listed | Phase 1 will add project config for the MCP server |
| Model providers configured | Provider `opencode` only (OpenCode Zen free models) | 10 model IDs listed by CLI | `opencode models` | None — all 10 tested working, zero cost |
| Authenticated integrations | **None** | "No authenticated integrations" | `opencode auth list` | None — free models work **without** credentials; do not add paid keys |
| Agents | 7 (2 primary visible, 2 subagents, 3 hidden) | `build` (default), `plan` (read-only), `explore` (subagent), `general` (subagent), hidden: `compaction`, `summary`, `title` | `opencode debug agents` | None for Phase 0 |
| Agent permissions | `build`: allow `*`, except `*.env` read = ask, `browser` = **deny**. `plan`: edit denied, `browser` = deny | `opencode debug agents \| jq` | Browser tool unavailable by default; keep for MVP |
| Plugins | **None** | "No plugins found" | `opencode plugin list` | None |
| Skills | **None installed** | `skill(s)`/`skills` dirs absent under config dirs | `ls` checks | None |
| MCP servers | **None configured** | "No MCP servers configured" | `opencode mcp list` | **Phase 1:** add the project's own MCP server definition (after user approval) |
| Recent/default model state | Default interactive choice not globally pinned | `model.json` "recent": `deepseek-v4.1-flash-free` (variant `max`), `space-bunny-free`, `big-pickle`; `exo-free` appears in history but **no longer exists** in the current model list | `~/.local/state/opencode/model.json` | Pin a default model + fallbacks in project config during Phase 1 |
| Session storage | SQLite | `opencode.db` (16 MB) + WAL in `~/.local/share/opencode/` | `opencode debug paths` | None — reuse for research session storage if desired |

### A.3 Runtimes, package management, developer tools

| Component | Installed/configured | Version/status | Verification evidence | Action required |
|---|---|---|---|---|
| Python | Yes | **3.14.8** (`/usr/bin/python3`) | `python3 --version` | Use as base interpreter |
| Python venv | Yes | `python3 -m venv` works; venv **bundles pip 26.2.1** | Created test venv in `/tmp/opencode/phase0/testvenv`; `pip --version` OK | Use venvs for all Python deps (system is externally managed) |
| System pip | **Absent (by design)** | `No module named pip`; `/usr/lib/python3.14/EXTERNALLY-MANAGED` present | `python3 -m pip --version` | Do **not** install system pip; use venvs or distro packages |
| Node.js | Yes | v24.22.0 at `~/.local/opt/node-v24.22.0-linux-x64/bin` | `node --version` | None |
| npm / npx | Yes | npm 11.19.1, npx 11.19.1; registry `https://registry.npmjs.org/` | `npm --version`, `npx --version`, `npm config get registry` | None — npx available for Node-based MCP servers later |
| Bun | **Not installed** | `command -v bun` → not found | — | Not required (npm/npx present) |
| uv / uvx | **Not installed** | `command -v uv` → not found; `~/.local/bin` does not exist | — | Not required (venv + bundled pip suffices) |
| Git | Yes | 2.56.0 | `git --version` | **Set `user.name`/`user.email` before any future commit** (no global gitconfig found) |
| GitHub CLI (`gh`), git-lfs, shellcheck | **Not installed** | `command -v` → not found | — | Unnecessary for MVP |
| SQLite (CLI + Python) | Yes | CLI 3.54.0; Python `sqlite3` module 3.54.0 | `sqlite3 --version`; `python3 -c "import sqlite3"` | None — storage/caching requirement already satisfied |
| Docker | Yes | 29.9.0 (daemon reachable), `docker-compose` present | `docker version` → `server=29.9.0` | **Unnecessary for MVP** — native runtimes suffice |
| pacman / paru | Yes | paru 2.1.0 (`libalpm 16.0.1`) | `command -v pacman paru`; `paru --version` | Use only in later phases with explicit approval |
| jq | Yes | 1.8.2 | `jq --version` | None |
| curl / wget | Yes | curl 8.22.0 (OpenSSL 3.6.5, HTTP/2); wget 1.25.0 | `curl --version`, `wget --version` | None |
| ripgrep / file / fzf / bat | Yes | rg 15.2.0; file-5.48; fzf; bat | `rg --version`, `file --version` | None (`fd` not installed; rg suffices) |
| Archives | Yes | tar, zip, unzip, bsdtar; `iconv` present | `command -v` | None — report archiving/export OK |
| ffmpeg | Yes | n9.0.2 | `ffmpeg -version` | Optional (media later) |
| ImageMagick | Yes | 7.1.2-33 (magick/convert/identify) | `magick --version` | Optional — thumbnails/basic image inspection |
| libexif | Installed (library only) | No CLI (`exif`/`exiftool` absent) | `command -v exif exiftool` → not found | Optional later: install `exiftool` for rich image metadata |
| Browsers | **None** (no chromium/chrome/firefox) | `command -v` → not found | — | Browser automation **out of MVP scope** |

### A.4 Document utilities (audited individually, per Section 4)

| Utility | Installed | Version/status | Verification evidence | MVP categorization |
|---|---|---|---|---|
| `pdftotext` / `pdfinfo` / `pdftoppm` (poppler) | **No** | All absent | `command -v` → not found; no `poppler` package | **Potentially needed later** → install in Phase 1 **only if** PDFs are in MVP scope |
| `tesseract` (OCR) | **No** | Absent | `command -v tesseract` → not found | Optional enhancement (scanned docs/images) |
| `exiftool` | **No** | Absent (libexif lib present, no CLI) | `command -v exiftool` → not found | Optional enhancement |
| `pandoc` | **No** | Absent | `command -v pandoc` → not found | Optional enhancement (multi-format export) |
| `jq` | Yes | 1.8.2 | `jq --version` | **Essential** (JSON processing) |
| `curl` | Yes | 8.22.0 | `curl --version`; live HTTPS 200 tests | **Essential** (HTTP fetch/API verification) |
| `wget` | Yes | 1.25.0 | `wget --version` | Optional (curl covers most needs) |
| `git` | Yes | 2.56.0 | `git --version` | **Essential** (versioning the project later; identity must be set) |
| `ghostscript` (`gs`), `qpdf`, `mutool` | No | Absent | `command -v` → not found | Unnecessary for now |
| Python: `requests` | Yes (system) | 2.34.2; live HTTP 200 verified | `python3 -c "import requests; ..."` → 200 | Optional (stdlib `urllib` can suffice; venv install if desired) |
| Python: `PyYAML` | Yes (system) | import OK | `python3 -c "import yaml"` | Optional |
| Python: Pillow | Yes (system) | 12.3.0 | `python3 -c "import PIL"` | Optional (image metadata/dimension work) |
| Python: `pypdf`/`PyPDF2`/`pdfminer`, `bs4`, `lxml`, `feedparser`, `jinja2`, `pydantic`, `jsonschema`, `typer`, `rich`, `pytest`, `python-magic`, `openpyxl`, `python-docx`, `dateutil` | **No** | All missing from system interpreter | import checks | Install into a **venv** per-need in Phase 1+; do not touch system interpreter |

### A.5 Network capability

| Component | Status | Verification evidence | Action required |
|---|---|---|---|
| Internet egress | **Working** | `curl -sI https://opencode.ai` → HTTP/2 200; `https://duckduckgo.com` → 200; `ping 1.1.1.1` → 0% loss; Python `requests` → HTTP 200 | None |
| Proxy variables | None set | `env \| grep -i proxy` → empty | None |
| npm registry reachability | Default registry configured; Node/npm functional | `npm config get registry` | None |
| Data/quota limits of connection | **Unknown** | Not auditable from host | User should note ISP data caps (hosted models minimize bandwidth) |

---

## B. Verified Capabilities

### B.1 Works now (capability **verified by execution** during this audit)

| Capability | Verified how | Result |
|---|---|---|
| Free LLM inference via OpenCode Zen provider (`opencode`) | `opencode run -m opencode/<model>` ×10 models, trivial prompts | **All 10 models returned correct output, exit code 0** (see Section E) |
| Agentic tool calling (write file + run shell command) | `opencode run -m opencode/glm-5.3-flash-free --auto "…create file… run cat…"` | File created with `AGENTIC_OK`; `cat` output `AGENTIC_OK` |
| Filesystem access in agent sessions | Same test (write + read verified) | Works |
| Terminal/shell execution in agent sessions | Same test (`cat` executed via agent tool) | Works |
| Web page retrieval (`webfetch` tool, harness level) | Fetched `https://example.com` as text | Works |
| Web page retrieval (`WebFetch` tool, inside agent session) | Agent fetched `https://example.com`, reported heading "Example Domain" | Works |
| Raw HTTP/HTTPS from host (`curl`, `wget`, Python `requests`) | Live 200 responses (opencode.ai, duckduckgo.com, example.com) | Works |
| SQLite storage (CLI + Python) | `sqlite3 --version`; `import sqlite3` | Works |
| Python venv + pip provisioning | Created `/tmp/opencode/phase0/testvenv`; pip 26.2.1 | Works |
| Node/npm/npx toolchain | Version checks; registry configured | Works |
| Subagent orchestration | `explore` and `general` subagents listed and usable | Configured + usable |
| Markdown report authoring/export | This report written to project `docs/` successfully | Works |

### B.2 Configured but **not** fully verified

| Capability | State |
|---|---|
| Vision/visual research via `nemotron-3.5-super-vl-free` | Model responds to text (**tested**); image-input capability **not tested** (no image-input harness test performed) |
| Free-tier quotas/limits of OpenCode Zen models | Not documented in this environment; **unknown** — cannot be verified from host |
| `session.permissions="autoaccept"` behavior | Configured; observed working in `--auto` test; long-run risk not assessed |
| Browser tool (`browser` permission = deny; no browser installed) | Configured as **denied**; not testable; deliberately out of MVP scope |
| PDF/document pipeline (any tool) | **All PDF utilities absent** — nothing to test |

### B.3 Configured and **failing** (verified failures)

| Capability | Evidence | Impact |
|---|---|---|
| **Web search** (harness `websearch` tool) | 3 separate queries ("Garuda Linux official website", "OpenCode AI coding agent", "Linux kernel latest release") → all returned `No search results found`. | Search-based research flows **blocked** |
| **Web search** (inside agent session — "Web Search via Firecrawl") | Agent attempted 2 searches ("Garuda Linux", "Garuda Linux Arch-based operating system") → both returned empty; agent declined to fabricate results. | Same backend issue appears at both layers |

> Interpretation: Internet access is fine and page retrieval works, but the **search backend returns no results at both the harness level and inside agent sessions**. This is exactly why "internet works" ≠ "search tool works". Re-verify in Phase 1; plan a zero-cost replacement if it stays broken (see `dependency-plan.md`).

### B.4 Not available (verified absent)

- Ollama — **excluded by project constraint; never invoked, never detected**.
- Browser automation (no browser binaries; `browser` permission denied).
- MCP servers (none configured → no MCP capability currently exists in this environment).
- PDF text extraction / OCR / exiftool metadata / pandoc export (utilities absent).
- System-wide pip (Arch external management) — use venvs instead.
- bun, uv, gh, git-lfs (absent; not required).
- Git identity (no `~/.gitconfig`).

---

## E. Model and Provider Assessment

### E.1 Provider summary

| Provider | Auth status | Models | Cost classification |
|---|---|---|---|
| `opencode` (OpenCode Zen free tier) | **No credentials configured** (`opencode auth list` → "No authenticated integrations") — and inference **succeeds anyway** | 10 listed, 10 tested | **$0.00 / zero-cost as of audit date** (SDK cost fields: input 0, output 0, cache 0) |

No other providers are configured. **No credentials of any kind were required and none were added.**

### E.2 Tested models (all ₹0 — real inference, not just listing)

Test method: `opencode run -m opencode/<id> "Reply with exactly: PING_OK and nothing else"` — exit code + exact output recorded. Raw outputs: `/tmp/opencode/phase0/<model>.out` and `.exit`.

| # | Model ID | Status | Evidence | Variants | Cost |
|---|---|---|---|---|---|
| 1 | `opencode/deepseek-v4.1-flash-free` | ✅ **Successfully tested** | exit 0, `PING_OK` (also the model hosting this audit session) | low/high/max | 0 |
| 2 | `opencode/glm-5.3-flash-free` | ✅ **Successfully tested** (plus agentic write+shell test, and webfetch test) | exit 0, `PING_OK`; `AGENTIC_OK` file test passed | low/high/max | 0 |
| 3 | `opencode/space-bunny-free` | ✅ **Successfully tested** | exit 0, `PING_OK` | low…max | 0 |
| 4 | `opencode/beam-free` | ✅ **Successfully tested** | exit 0, `PING_OK` | low…max | 0 |
| 5 | `opencode/big-pickle` | ✅ **Successfully tested** | exit 0, `PING_OK` | none | 0 |
| 6 | `opencode/longcat-2.5-preview-free` | ✅ **Successfully tested** | exit 0, `PING_OK` | none | 0 |
| 7 | `opencode/mimo-v2.6-flash-free` | ✅ **Successfully tested** | exit 0, `PING_OK` | none | 0 |
| 8 | `opencode/nemotron-3-ultra-free` | ✅ **Successfully tested** (listed by CLI; not shown by the SDK models endpoint) | exit 0, `PING_OK` | low…max | 0 |
| 9 | `opencode/nemotron-3.5-lightning-free` | ✅ **Successfully tested** (CLI-only listing, as above) | exit 0, `PING_OK` | low…max | 0 |
| 10 | `opencode/nemotron-3.5-super-vl-free` | ✅ **Successfully tested for text**; vision input untested | exit 0, `PING_OK` | none/minimal…xhigh | 0 |

**Classification counts:** 10 successfully tested · 0 configured-but-untested (except vision modality of one model) · 0 unavailable/failing · 0 requiring credentials · 0 status-unknown.

### E.3 Model caveats (important for planning)

1. **"Free" was verified empirically, within this environment**, on 2026-10-17: zero cost fields in `opencode models` output, no billing/auth configured, successful inference. This does **not** constitute a guarantee from the provider; free-tier terms and quotas were not auditable from the host.
2. **Model list drift is real:** `model.json` history shows `opencode/exo-free` was previously used but **no longer exists** in the current list. Design the project with a **fallback model chain**, not a single hard dependency.
3. **Two CLI-listed models are missing from the SDK models endpoint** (`nemotron-3-ultra-free`, `nemotron-3.5-lightning-free`) — likely filtered as non-newest. Both work. Prefer CLI availability checks.
4. **Copying model output faithfully:** trivial-prompt tests verify *availability*, not *research quality*. Model selection for the research pipeline should be decided by Phase 1 benchmarks, within zero-cost constraints.
5. **Excluded:** Ollama (per constraint — never detected, installed, or referenced in any test) and any paid/token-based API.

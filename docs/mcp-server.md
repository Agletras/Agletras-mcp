# Agletras MCP Server — Reference & Verification Log

- **Version:** 0.2.0 · **Built:** 2026-10-17 · **Status:** connected to OpenCode and verified end-to-end
- **Runtime:** Python 3.14.8 in project venv (`.venv`) · **Dependencies:** `mcp==2.3.0`, `pypdf==6.21.1` (both pure Python)
- **Constraints:** zero-cost, no credentials, no paid services, **no Ollama** (not imported, not referenced, not required)

## 1. Design decisions

| Decision | Rationale |
|---|---|
| **Python + venv** (not Node) | One stack only, per the Phase 0 dependency plan. venv + bundled pip was verified working; the MCP server fits the research/storage/export workload well. |
| **MCP Python SDK v2** (`MCPServer`) | Official SDK; v2 renamed `FastMCP` → `MCPServer`. The stdio transport is exactly what OpenCode launches. |
| **pypdf for PDFs** | Pure Python, no system libraries (poppler/ghostscript absent and not needed), actively maintained, handles text + metadata. Zero-cost and lightweight — matches "install only what is necessary". |
| **stdlib HTTP (`urllib`) + `html.parser`** | Avoids adding `requests`/`bs4`/`lxml` when the standard library covers fetch + readable-text extraction. Keeps the dependency surface at exactly two packages. |
| **SQLite via stdlib `sqlite3`** | Already available and verified in Phase 0; single-file store, zero configuration, easy to inspect with the `sqlite3` CLI. |
| **`codemode: false` in OpenCode config** | Exposes tools natively (`agletras_extract_pdf` …) so any model — including small free models — can call them without writing Code Mode JavaScript. Remove the line to switch to Code Mode. |
| **Uniform JSON envelopes** | Every tool returns one JSON string with `ok` first; failures are data (not protocol errors) so models can recover gracefully. |
| **Err on the side of structure** | Tool code raises `ToolError(code, message)`; a single decorator converts failures into envelopes and logs unexpected exceptions to **stderr** (stdout belongs to the MCP protocol). |

### Phase 2 decisions

| Decision | Rationale |
|---|---|
| **DOCX/XLSX via stdlib `zipfile` + `xml.etree`** | OOXML packages are zips of XML parts; parsing them directly needs no third-party library. Honors the Phase 0 plan ("net new dependencies strictly required: none") — zero new packages, no CVE surface from OOXML parsers, formulas never evaluated (cached values only), macros impossible (no execution engine exists in the code path). |
| **CSV via stdlib `csv`** | Same reasoning; CSV/TSV render as pipe tables so an LLM reads them like spreadsheets. |
| **Extend the schema, don't replace it** | Phase 2 columns are `ALTER TABLE`-added onto the existing `sources` table by a `_migrate()` step with the Phase 1 keys (`kind`, `content`, `url`, `path`, `title`, `notes`, `sha256`, `created_at`) untouched; readers always derive the unified fields back. Old databases keep working in place. |
| **`kind` extended, not forked** | `kind` gains two coarse values (`doc`, `search_result`) while `source_type` is the precise one (`web`, `search_result`, `pdf`, `markdown`, `text`, `csv`, `docx`, `xlsx`). `list_sources` can filter by either. |
| **Model-free de-duplication** | Exact SHA-256 + a case/punctuation-insensitive shape hash + a bounded word-set Jaccard scan (≥ 0.90). Deterministic, zero-cost, no model in the loop; duplicate *origins* are appended to the surviving row's `metadata.origins` instead of re-storing the content. |
| **Content-type first, URL guessing second** | What a server returned decides parsing (a `.pdf` URL serving HTML parses as a page; a generic content type serving `%PDF-` magic is routed to the PDF path). Mirrors the fetch_url/extract_pdf split without duplicating download logic — PDF URLs reuse the same cache. |
| **Snippets are not pages** | Search-query ingestion stores `search_result` records containing title, URL, and snippet only (`metadata.snippet_only: true`); the snippet text is never treated as full page content, and never fetched as one. |
| **Explicit access roots** | Local reads are confined to `AGLETRAS_LOCAL_ROOTS` (resolved absolute, symlink-aware, traversal-proof). Out-of-root paths fail with `access_denied` *before* being opened; folder scans never follow symlinks out of the roots, and every skipped file is reported with a reason. |
| **Retries only for transient failures** | One retryer (`collect.attempt_with_retries`) classifies failures via `http_status` (404→`not_found`, 401/403→`access_denied`, 429→`rate_limited`, ≥500→`server_error`, fetch timeouts→`network_error`); only `rate_limited`/`server_error`/`network_error` are retried, bounded and backed off. |

### Phase 3 decisions

| Decision | Rationale |
|---|---|
| **Versioned migrations on the same database** | Phase 3 extends the existing `data/research.db` instead of forking a second store: `schema_migrations` records v1 (Phase 1/2 baseline) and v2 (evidence library). Migration DDL is `IF NOT EXISTS`/guarded-`ALTER` idempotent and the version row is written last, so an interrupted migration is simply retried on the next open. Old databases upgrade in place; `storage.stats()` reports `schema_version`. |
| **Identity table, not a rewritten `sources` row** | `source_identities(identity PK → source_id)` maps a canonical URL or resolved path to exactly one row. De-duplication is identity-first (the same URL always finds its row) while the original/final URLs stay untouched on the record. |
| **Content-addressed blobs shared across identities** | `source_contents` is keyed by SHA-256 of the extracted text (unique `content_hash`); two *different* URLs serving identical text share one blob but keep **separate `sources` rows** — separate provenance, URLs, and citation targets never collapse into one. `raw_hash` keeps the raw-byte hash when a retrieval produced bytes. |
| **Conservative cache with a no-loss fallback** | A stored record is served without refetching while fresh: `AGLETRAS_EVIDENCE_CACHE_TTL` (default 24 h) plus a local-file `{size, mtime_ns}` fingerprint. `refresh: true` forces refetching. A changed body becomes a *new* content blob and new passages — earlier passages/citations/run links are never erased (`passages` are unique per `(source_id, content_hash, passage_index)`). If a refresh fails, the stored evidence is returned unchanged with `stale: true`, `refresh_error`, and a warning instead of being lost. |
| **Passages are exact, immutable slices** | `passages.py` splits text deterministically (model-free): PDF `--- page N/M ---` markers are consumed and set `page`; Markdown headings set the section; paragraphs pack up to a soft cap, oversized ones split at sentence boundaries. Every passage stores `start_offset`/`end_offset` (exact `content[start:end]`), its SHA-256, and a human locator — so a citation can always be traced back byte-for-byte. |
| **FTS5 with a tested LIKE fallback** | Passage search uses FTS5/BM25 (`passages_fts`, external-content triggers) when the SQLite build supports it; `AGLETRAS_EVIDENCE_FTS=off` forces the parameterized-`LIKE` path (both are covered by tests). Results are re-ranked by term coverage so exact-term matches win. |
| **Claims: status is derived, never asserted** | `claims.status` is recomputed from the relations of attached citations (`contradicts` > `needs_review` > `supports`+`partially_supports` > `supports` > `partially_supports` > context-only/no citations = `unverified`). Registering a claim can never mark it supported, and attaching `context` never establishes support. |
| **Citations require an exact passage** | `citations.passage_id` is `NOT NULL` with `RESTRICT` deletes; an optional `source_id` is cross-checked against the passage; a non-empty `quote` must occur in the passage (whitespace-normalized) or the whole call fails with `invalid_argument`. Fabricated quotations and wrong-page claims cannot be stored. |
| **Structural validation stated as such** | `validate_report` checks identifier resolution, passage hashes/offsets/quotes, claim-status consistency, reference provenance, and `PRAGMA foreign_key_check`; the response says `validation_kind: "structural"` and `semantic_support: "not_evaluated"` — it never claims a statement is true. Unverified/needs-review/contradicted claims are rendered with explicit markers and listed separately in every report. |

### Phase 4 decisions

| Decision | Rationale |
|---|---|
| **The agent plans; the server enforces** | The MCP server never sees OpenCode's selected model or chat context, so planning and evidence judgement live in the project agent `.opencode/agents/research-orchestrator.md` (mode `primary`, **no** `model` field → it inherits the session's selected model). The server owns what must be deterministic: caps, statuses, the append-only log, budget/stopping math, and report composition. |
| **Plan on the run; subquestions in their own table** | `research_runs.plan` stores the objective/scope/assumptions/limits JSON; `research_subquestions` stores each question with priority, status, and an append-only evaluation note. Re-saving a plan updates the metadata and *adds* questions — existing ids, statuses, and notes survive, so research history is never rewritten. |
| **Append-only `research_run_events` log** | One row per tool action, query, candidate, saved/failed source, evidence, claim, citation, gap, round, error, decision, stopping reason, or report. Events may reference their subquestion/source (`ON DELETE SET NULL`, so the log never blocks cleanup); payloads are validated JSON with size caps; `get_research_history(run_id)` returns the last 100 events plus a total count. |
| **Limits are enforced at the point of action** | Defaults 8 subquestions / 2 follow-up rounds / 4 candidate sources per subquestion per round / 20 unique sources per run; overridable per run via `save_research_plan(limits=…)` and clamped to hard ceilings (16 / 5 / 20 / 100). Enforced at plan time (subquestion cap), at `log_run_event(kind=source_candidate)` (candidate cap; a round targeting the subquestion resets its budget), at `start_followup_round` (round cap), and inside `save_evidence` before anything is written (unique-source cap; a refused save leaves no partial rows). Refusals are `limit_reached` with a message saying what remains and how to proceed. |
| **Stopping is computed, not vibes** | The pure function `planner.evaluate_stopping` says stop when every subquestion is answered, or when a limit is reached with gaps remaining, and returns the reasons. History and reports expose it, so the workflow cannot loop indefinitely and remaining gaps are reported honestly. |
| **Reports describe the method from stored facts only** | When a plan exists, `build_evidence_markdown` adds research question/scope, a methodology section (stored source/search/failure counts, rounds used, claims/citations, stopping reason), unanswered questions with their notes, and a "Sources reviewed" list — all read from SQLite. No model-written prose enters the report body, so nothing in it can be fabricated. |

### Phase 6 decisions

| Decision | Rationale |
|---|---|
| **Verification is opt-in and never assumed** | Deterministic checks over the exact stored passages always run; a verification model is only called when `AGLETRAS_VERIFICATION_MODEL_ENDPOINT` is explicitly configured and `method="auto"`. A model response must be structured (status + rationale + stored passage ids) or the whole assessment is rejected and the deterministic result is stored with method `model_fallback_deterministic`. A model can never override a contradiction established by citation relations, and the OpenCode-selected model is never assumed callable from the server. |
| **Statuses are explicit and evidence-bound** | `claim_verifications` (append-only audit: one row per verification pass per claim) records status, method, confidence, rationale, evidence passage ids, and limitations; `claims.verification_status` caches the latest assessment in the same transaction, so the two cannot diverge. Statuses: `supported`, `partially_supported`, `contradicted`, `conflicting_evidence`, `unsupported`, `unverifiable`. Claims without a record keep their Phase 3 derived status; nothing is silently upgraded, and citations are only auto-attached for decisive full/partial matches — contradictions and conflicts are never auto-attached. |
| **Deterministic checks with documented thresholds** | Claim-vs-passage comparison is term coverage plus number/date/negation signals with module constants: best coverage < 0.25 → `unverifiable`, ≥ 0.50 → at least `partially_supported`, ≥ 0.85 (with numbers/dates confirmed) → `supported`; conflict signals on the closest passage produce `contradicted`, or `conflicting_evidence` when an independent passage supports the claim. Every default outcome is reproducible with no hidden model judgement. |
| **Conflicts are surfaced, never resolved by preference** | `analyze_conflicts` reports supporting and contradicting sides (claim citations, verification disagreements, subquestions marked `conflicting`) with source, locator, quote, content hash, and an independence group (domain / folder / content hash) so one source re-published twice cannot pose as two witnesses. Source-quality indicators are metadata heuristics, explicitly not truth scores, and stay separate from verification statuses. |
| **A separate verifications table, not a rewritten claim row** | Migration v4 adds `claim_verifications` plus `claim_type`/`note`/`verification_status`/`verified_at` on claims and `template_version`/`finding_map`/`image_map`/`generation_config` on reports. The Phase 3 derived `claims.status` is untouched, so citation-derived and verification-derived state stay independently inspectable. |
| **The report is rendered from stored facts, with stable ids** | `build_phase6_markdown` renders ten sections from SQLite rows only: `[F#]` per claim (claim-id order), `[Sn]` per citation, `[IMG#]` per image source (source-id order). Status labels (`SUPPORTED`, `PARTIALLY SUPPORTED`, `CONTRADICTED BY EVIDENCE`, `CONFLICTING EVIDENCE`, `UNSUPPORTED`, `UNVERIFIABLE`) plus method, confidence, rationale, and limitations stay attached to every statement; facts, inferences, and unverified statements are kept apart. |
| **Integrity validation extends structural validation, still never truth** | `synthesis.validate_phase6_report` runs the Phase 3 structural checks, then the synthesis guarantees (required sections, finding-id resolution, verification records/methods, supported-with-citation, model assessments citing stored passages, conflict rendering, image attribution, liveness disclaimer) and returns `errors`/`warnings`/`unresolved`. It states `validation_kind: "structural+phase6_integrity"` and never judges semantics; older-template reports delegate to the structural check and say so. |
| **Reports are deduplicated and confined** | Identical content (same run + content hash + template) returns the existing report instead of writing a duplicate file; a new generation gets a unique numeric suffix and never overwrites an existing file; all paths are confined under the reports directory (`..`/traversal refused). `report_max_findings` is a size warning, never a truncation. |

### Phase 7 decisions

| Decision | Rationale |
|---|---|
| **The CLI reuses the Phase 3–6 pipeline; nothing is forked** | `agletras run` calls the same `evidence`, `planner`, `verification`, and `synthesis` entry points the MCP tools use, so CLI and agent runs produce the same evidence model, citation rules, statuses, and report template. Phase 4 tool behavior is untouched (e.g. planner limits still start at 1; a 0-round CLI budget is stored as the minimum representable bound and enforced by the run loop). |
| **Model-free by construction** | Subquestions are the full question (or `--subquestions`); search phrasings are deterministic query variants; no planning, judgement, or prose model is called. Depth presets bound the work (quick/standard/thorough) and gaps are reported honestly. |
| **Checkpoints before claims** | One append-only `research_checkpoints` row per stage attempt, single transaction. Resume skips completed stages and retries interrupted ones; identity-keyed evidence de-duplication keeps re-runs from doubling sources/citations/reports. |
| **Resume refuses to mix incompatible evidence** | `resume_assessment` explains every refusal (unknown id, MCP-managed run, already closed, failed/stale without `--force`, different run config). `--force` overrides the safe refusals, never a completed run; `--restart` is the only way to re-run everything. |
| **stdout is the result; stderr is the story** | Final output (report/JSON/summary) on stdout; progress (human or JSONL) and errors on stderr. Structured JSONL run logs live in `logs/`, separate from reports. |
| **Config file is strict, env stays lenient** | TOML (`$AGLETRAS_CONFIG` or `$AGLETRAS_HOME/agletras.toml`) is validated key-by-key; unknown sections/keys/types fail fast with exit 2. Environment parsing keeps its historical fallback-to-default leniency; `config validate`/`doctor` are where bad env values get caught. |
| **Secrets are redacted by name and by pattern** | `***redacted***` for secret-named settings, URL-credential masking for endpoints, sanitized progress messages; `doctor` reduces endpoints to `scheme://host:port`. No secrets are read, stored, or printed anywhere in the CLI. |
| **Doctor never lies and never raises** | Every check is bounded (local, quick, non-mutating); model endpoints are TCP-probed only when explicitly configured; required vs optional failures are explicit; the MCP check spawns the real server exactly as OpenCode does and fails on a degraded `health_check`. |
| **Exit codes as the contract** | 0 success · 2 usage/config · 3 dependency/unhealthy · 4 research failed · 5 partial · 130 interrupted. Arguments are validated before any network work; a run never claims success when its report or persistence failed. |

### Phase 8 decisions

| Decision | Rationale |
|---|---|
| **Parallelism is opt-in, bounded, and kept only where measured faster** | `parallel.run_tasks` runs independent tasks (searches, per-URL collections, image queries, monitoring checks) in a bounded thread pool with per-task timeouts, a deadline and cancellation, per-task failure isolation, and sequential fallback (`parallel=False` or worker count 1). Outcomes are returned in submission order with deterministic merges, and tests pin result parity with the sequential path. The default stays sequential; `--parallel` or a profile `parallel = true` enables it. `scripts/evaluate_phase8.py` measures sequential vs parallel on identical workloads and records the verdicts in `docs/phase8-measurements.md`. |
| **Monitoring is manual by design and never destructive** | Targets are registered explicitly and fetched only when `monitor_check`/`agletras monitor check` runs — no daemon, no scheduler, no background process. Every check writes one immutable `monitor_snapshots` row (`initial`/`unchanged`/`changed`/`not_modified`/`failed`); a failed retrieval is recorded as a failure and **never** interpreted as a change or deletion (the previously stored evidence stays untouched). |
| **Change detection compares normalized substantive text** | Navigation, ads, cookie banners, sharing widgets, and bare timestamps are classified as boilerplate *before* comparison; the normalized substantive hash drives equality, so timestamp-only rewrites are `unchanged`. Detected changes store both sides (old/new source and content ids, hashes) plus added/removed substantive terms and the ignored boilerplate samples for review. Conditional requests (`If-None-Match`/`If-Modified-Since`) are used when stored; `304` is `not_modified`. |
| **Project memory is recall + reuse over the same store** | `search_memory` returns verbatim passages from every run with run links, projects, and honest freshness (`fresh: null` when age is unknown; a bounded `max_age_days` excludes only known-stale hits). `--reuse-evidence` links fresh stored copies (`research.reuse_max_age_days`, default 7) into a new run instead of re-fetching, keeping the original retrieval timestamps and provenance visible. No second database, no embeddings — ranking stays keyword/FTS. |
| **Visualizations are derived artifacts, overwritten on re-export** | `visualize.export_visuals` renders a Mermaid evidence map, a source timeline, and sources/claims/citations CSVs from stored rows only (escaping/clipping, honest empty placeholder, no invented nodes; sources without timestamps are omitted from the timeline). Re-export overwrites its own output directory; research reports keep their never-overwrite rule. |
| **Profiles are strict option presets; branding only labels output** | `[profiles.<name>]` tables are validated key-by-key (unknown keys/types fail fast with exit 2). Precedence: explicit CLI flags > profile > `[research]` settings. `[branding] name` prefixes the default report title (`"Name: question"`); `primary_color`/`footer` are formatting labels. |
| **Jobs store references, never resolved snapshots** | `research_jobs` keeps the question plus the profile/project/title it was created with; `jobs run` resolves them at run time, so a job always follows the current profile and branding, and a missing profile fails loudly (exit 2, available profiles listed) instead of guessing. `research_job_runs` keeps per-run history; deleting a job cascades its history but never its research runs. |
| **Schema stays one SQLite store (v6 + v7, additive)** | v6 adds `monitor_targets`/`monitor_snapshots` and `research_runs.project`; v7 adds `research_jobs`/`research_job_runs`. Both are in-place additive migrations; Phase 1–7 tables, tools, and behavior are untouched, and no new runtime dependency was added (stdlib + existing deps only). |

## 2. Tool contracts

All tools live in `src/agletras_mcp/server.py`; core logic lives in the sibling modules (`webfetch`, `search`, `netpolicy`, `pdf`, `storage`, `report`; for Phase 3 `canonurl`, `passages`, `evidence`; for Phase 4 `planner`; for Phase 5 `images`; for Phase 6 `verification`, `synthesis`; for Phase 8 `parallel`, `specialists`, `memory`, `monitor`, `visualize`, `jobs`). Names below are the OpenCode-visible names (`agletras_` prefix added by OpenCode from the server name `agletras`).

### `agletras_health_check` *(Phase 1)*
Liveness probe with zero network activity.

Returns `status` (`ok` / `degraded` — degraded means the storage probe failed, the server still runs), `server`, `version`, `transport` (`stdio`), `tools` (every registered tool name), `storage` ({ok, db_path, total, db_size_bytes} or {ok:false, error}), and `network` (always "not tested - health_check performs no external requests" — no connectivity is ever claimed).

### `agletras_search_web` *(Phase 1)*
Open-web search through a free provider adapter (`src/agletras_mcp/search.py`), selected by `AGLETRAS_SEARCH_PROVIDER` (`duckduckgo` default = HTML endpoint, `wikipedia` = official MediaWiki API). All requests go through the SSRF policy, the byte/time caps, and the shared User-Agent.

| Arg | Type | Default | Notes |
|---|---|---|---|
| `query` | string | — | required, non-empty after strip |
| `max_results` | int | 5 | 1..`AGLETRAS_MAX_SEARCH_RESULTS` (default cap 10); oversized values are clamped, non-positive are `invalid_argument` |

Response: `{ok, query, provider, count, results: [{title, url, snippet, source}]}` — `url` is the decoded, de-duplicated target (DuckDuckGo `uddg=` redirect wrappers resolved), `source` identifies the provider. Sponsored/ad containers are dropped, snippets attach to their own result. An empty page is a success with `count: 0`; **results are never fabricated**. Challenge/anomaly pages and HTTP 429 raise `rate_limited`; unexpected provider crashes raise `provider_error`. Politeness pacing (`AGLETRAS_SEARCH_MIN_INTERVAL`, default 2 s) keeps consecutive searches apart.

### `agletras_fetch_url`
Fetch an http(s) page; HTML is converted to readable text (scripts/styles/nav clutter removed); content-type aware. Every outbound request — including PDF URL downloads and search providers — is validated by the SSRF guard (`src/agletras_mcp/netpolicy.py`); see section 6.

| Arg | Type | Default | Notes |
|---|---|---|---|
| `url` | string | — | http/https only; no `user:pass@`; localhost/private/reserved/CGNAT/multicast targets refused |
| `max_chars` | int | 0 (=50000) | ≥ 0 else `invalid_argument`; clamped to `AGLETRAS_MAX_CHARS_CAP` (500000) |
| `save` | bool | false | persist to the store |
| `title` / `notes` | string | "" | display title (defaults to page `<title>`), notes |

Example response: `{"ok": true, "url": "...", "final_url": "...", "content_type": "text/html", "title": "Example Domain", "chars": 156, "truncated": false, "text": "...", "source_id": 1, "saved": true, "created": true}`

PDF URLs are detected (content-type or `%PDF-` magic) and rejected with a pointer to `agletras_extract_pdf`.

### `agletras_extract_pdf`
Extract text page by page from a local PDF (`path`) or an `http(s)` URL (downloaded to `cache/`, hash-named, reused).

| Arg | Type | Default | Notes |
|---|---|---|---|
| `source` | string | — | absolute path, path relative to project root, or URL |
| `max_pages` / `max_chars` | int | 0 (200 / 200000) | limits |
| `save` | bool | false | persist extracted text |
| `title` / `notes` | string | "" | title defaults to PDF metadata title |

Response includes `page_count`, `pages_extracted`, `has_text`, per-page markers (`--- page 1/2 ---`), `metadata` (title/author/…), and `warnings` (page limit, failed pages, scanned-PDF advisory). Encrypted PDFs, corrupt files, and non-PDF inputs produce typed errors.

### `agletras_collect_sources` *(Phase 2)*

Run the collection pipeline over many sources in one call: http(s) URLs, supported local document paths, and/or a web-search query. Per-source extraction with bounded concurrency; failures are isolated (one bad source never aborts the job).

| Arg | Type | Default | Notes |
|---|---|---|---|
| `sources` | string | "" | newline- or comma-separated list, or a JSON array of strings; each item is an http(s) URL or a permitted local path |
| `search` | string | "" | optional web-search query → `search_result` snippet records (never fetched as pages) |
| `search_limit` | int | 0 (=5) | search results to ingest |
| `max_chars` | int | 0 (=per-type default) | per-source text cap, clamped to `AGLETRAS_MAX_CHARS_CAP` |
| `save` | bool | true | persist successful extractions |
| `include_text` | bool | false | full extracted text per source instead of excerpts |
| `concurrency` | int | 0 (=`AGLETRAS_COLLECT_CONCURRENCY`) | max parallel retrieval workers |

Response: `{ok, summary:{requested, succeeded, partial, failed, unsupported, skipped, duplicates, saved, elapsed_ms}, results:[…], warnings?}`. Each result is the unified record: `label` (what was requested), provenance (`source_type`, `title`, `original_url`, `final_url`, `file_path`, `publication_date`, `retrieved_at`, `content_type`, `file_size_bytes`, `checksum`), `extraction_status`, `error_code`/`error_message` on failure, `metadata`, `warnings`, `chars`, `excerpt`, and — when saved — `source_id`/`created`; duplicates carry `duplicate_of`/`duplicate_kind` (with `duplicate: "within_job"` when nothing was stored). Inputs over `AGLETRAS_COLLECT_MAX_ITEMS` are dropped with a job warning.

Example request: `{"sources": "https://example.com/a page, samples/paper.pdf\ndocs/notes.md", "search": "model context protocol", "save": true}`.

### `agletras_collect_local_folder` *(Phase 2)*

Collect every supported file under one covered folder through the same pipeline. Recursive by default; temporary/hidden/unsupported files are skipped **with reasons** (reported, never silent).

| Arg | Type | Default | Notes |
|---|---|---|---|
| `folder` | string | — | directory inside an access root (`AGLETRAS_LOCAL_ROOTS`); outside → `access_denied`; not a directory → `invalid_argument` |
| `recursive` | bool | true | bounded by `AGLETRAS_FOLDER_MAX_DEPTH` |
| `extensions` | string | "" | optional comma-separated allowlist ("md,csv"; dotted forms accepted) |
| `max_files` | int | 0 (=200) | collected-file cap; excess sets `scan_truncated` |
| `save` / `include_text` | bool | true / false | forwarded to the pipeline |

Response adds to the collect summary: `folder`, `recursive`, `files_picked`, `files_skipped`, `scan_truncated`, plus a top-level `skipped` list of `{path, reason}` entries (`temporary_file`, `hidden_or_backup_file`, `unsupported_extension`, `too_large`, `symlink_outside_roots`).

### `agletras_extract_document` *(Phase 2)*

Extract one document into the unified record: local path (pdf/md/txt/csv/docx/xlsx/html) or http(s) URL — the single-source version of the pipeline.

| Arg | Type | Default | Notes |
|---|---|---|---|
| `source` | string | — | covered local path or http(s) URL |
| `max_chars` | int | 0 (=per-type default) | text cap |
| `save` | bool | false | persist to the store |
| `title` / `notes` | string | "" | optional display title/notes used when saving |

Unlike `collect_sources`, a failed/unsupported extraction raises a typed error envelope (`not_found`, `access_denied`, `unsupported_content`, …). Saving uses the same SHA-256 dedupe as everything else and returns `source_id`, `saved`, `created`.

### `agletras_save_source`
Store `{kind: url|pdf|note, title, content, url?, path?, notes?}`. Content is SHA-256 de-duplicated: re-saving identical text returns the existing `source_id` with `created: false`.

### `agletras_get_source` / `agletras_list_sources` / `agletras_search_sources`
Standard retrieval over the SQLite store. `list` is newest-first with a coarse `kind` filter (`url`/`pdf`/`note`/`doc`/`search_result`) or a precise `source_type` filter (`web`/`search_result`/`pdf`/`markdown`/`text`/`csv`/`docx`/`xlsx`), and offset paging; `search` does case-insensitive keyword matching over title/url/notes/content with `%`/`_` escaping. Every record carries both the Phase 1 keys (`kind`, `content`, `url`, `path`, …) and the Phase 2 unified fields (`source_type`, `original_url`/`final_url`, `publication_date`, `retrieved_at`, `content_type`, `file_size_bytes`, `extraction_status`, `error_code`, `metadata` incl. `origins`, …). `get_source` additionally exposes Phase 3 evidence extras under `source.evidence` (`content` blob, `identities`, `run_ids`, `passage_count`, `stale`) and, with `include_passages: true`, the indexed `passages` themselves (ids, offsets, hashes, locators).

### `agletras_export_report`
Builds a Markdown report (title, UTC timestamp, numbered citation list with ids, kinds, dates, notes, and optional excerpts) and writes it to `reports/<slug>-<UTC-timestamp>.md`.

| Arg | Type | Default | Notes |
|---|---|---|---|
| `title` | string | — | required; also used for the filename |
| `source_ids` | string | "" | comma-separated ids (e.g. `"1,4,9"`) |
| `query` | string | "" | keyword selection when ids are empty |
| `limit` | int | 20 | selection cap |
| `include_excerpts` | bool | true | excerpt per citation |

Unknown ids are reported in `warnings`; an empty selection is an `empty_selection` error.

### `agletras_stats`
`{db_path, schema_version, total, by_kind, db_size_bytes, evidence:{research_runs, source_contents, source_identities, passages, claims, citations, reports, research_subquestions, research_run_events, fts5}}` — cheap health check, used in the OpenCode verification below.

### `agletras_start_research_run` *(Phase 3)*

Create the research run that evidence, claims, and reports attach to.

| Arg | Type | Default | Notes |
|---|---|---|---|
| `query` | string | — | required, non-empty after strip |
| `config` | string | "" | optional JSON object stored verbatim for provenance (malformed JSON → `invalid_argument`) |

Response: `{ok, run_id, query, status: "running", config, started_at, completed_at, error}`. `get_research_history` closes the loop: with no `run_id` it lists runs (newest first) with `sources_count`/`claims_count`/`unverified_count`/`reports_count`; with a `run_id` it returns the full trace (`sources` with passage counts and staleness, `claims` by status, `unverified_claims`, `reports`, `counts`).

### `agletras_save_evidence` *(Phase 3)*

Save one piece of evidence with identity de-duplication, content-cache reuse, and passage indexing. Exactly one mode must be provided.

| Arg | Type | Default | Notes |
|---|---|---|---|
| `source` | string | "" | an http(s) URL or permitted local path: retrieved with the Phase 2 adapters |
| `source_id` | int | 0 | promote an existing Phase 1/2 source row (must have stored text) |
| `text` | string | "" | raw text to store directly |
| `title` / `notes` | string | "" | optional title override / appended notes |
| `url` / `path` | string | "" | provenance when saving raw `text` (also forms the identity) |
| `run_id` | int | 0 | associate with a research run (validated) |
| `refresh` | bool | false | ignore the cache and refetch/re-extract |
| `max_chars` | int | 0 (=per-type default) | text cap |

Response: `{ok, source_id, run_id, title, kind, source_type, url, original_url, final_url, path, canonical_url, identity, identity_kind, identities, content_id, content_hash, raw_hash, retrieved_at, last_checked_at, publication_date, extraction_status, chars, passages_saved, passages_total, created_source, content_changed, cache_hit, fetched, stale, warnings, refresh_error?}`. `cache_hit: true` means the stored copy was served without retrieving it; `content_changed` marks a refresh that produced new text (a new `source_contents` blob — earlier passages and citations survive); `stale` + `refresh_error` mark a failed refresh that kept the earlier evidence. Unusable/degenerate inputs raise typed errors (`invalid_argument` for zero/two modes or empty text, `not_found` for unknown ids, `unsupported_content`/`access_denied`/`network_error` from the retrieval layer).

### `agletras_search_evidence` *(Phase 3)*

Find exact passages by keyword or phrase. Stopwords are dropped and up to 12 terms are used; ranking is term-coverage first, then engine rank.

| Arg | Type | Default | Notes |
|---|---|---|---|
| `query` | string | — | required; must contain at least one searchable term |
| `limit` | int | 10 | 1..`AGLETRAS_MAX_LIST_LIMIT` |
| `run_id` | int | 0 | restrict to sources associated with one run |

Response: `{ok, query, engine: "fts5"|"like", count, passages:[{passage_id, source_id, title, url, path, kind, source_type, retrieved_at, content_hash, passage_hash, locator, page, heading, start_offset, end_offset, text, score, rank?}]}`. An empty result is a success with `count: 0` — results are never fabricated.

### `agletras_register_claim` *(Phase 3)*

Register a factual claim on a run. Args: `run_id`, `claim_text` (whitespace-normalized, ≤2000 chars). Response: `{ok, claim_id, run_id, claim_text, status: "unverified", created_at, updated_at, created}`. Registering the identical text on the same run returns the existing claim with `created: false`; registration **never** marks a claim supported. Unknown `run_id` → `not_found`.

### `agletras_attach_citation` *(Phase 3)*

Attach one exact passage to a claim and re-derive the claim's status.

| Arg | Type | Default | Notes |
|---|---|---|---|
| `claim_id` | int | — | the claim to cite |
| `passage_id` | int | — | exact passage id from `search_evidence`; required |
| `source_id` | int | 0 | optional cross-check: must equal the passage's source or `invalid_argument` |
| `quote` | string | "" | optional exact excerpt; must occur in the passage text (whitespace-insensitive, typographic quote glyphs equated with ASCII) or `invalid_argument` — fabricated quotations are refused |
| `locator` | string | "" | optional override (defaults to the passage's locator) |
| `relation` | string | `supports` | one of `supports`, `partially_supports`, `contradicts`, `context`, `needs_review` |
| `metadata` | string | "" | optional JSON object stored with the citation |

Response: `{ok, created, claim, derived_status, citation:{…, quote_matches_passage, passage:{…}, source:{…}}}`. The claim's stored `status` is always recomputed from all citation relations. Missing passage/claim → `not_found`.

### `agletras_get_claim_citations` *(Phase 3)*

One claim with every attached citation: relation, quote (+`quote_matches_passage`), locator, the exact passage (text, hash, offsets, page/heading/locator), and full source provenance (id, title, URL/path, type, retrieval/publication dates), plus `derived_status`. Unknown `claim_id` → `not_found`.

### `agletras_generate_report` *(Phase 3)*

Render a claims/citations Markdown report for a run, store it (row + file under `reports/`), and run the structural validation. Args: `run_id`, `title` (optional; defaults to the run query). Response: `{ok, report_id, run_id, title, path, filename, citation_map: {"S1": citation_id, …}, markdown, claims_rendered, validation, citation_validation_status}`. Unverified/needs-review/contradicted claims are marked inline (`**UNVERIFIED**`, `**PARTIALLY SUPPORTED**`, `**CONTRADICTED by evidence**`, `**NEEDS REVIEW**`) and listed under "Claims without established support"; the references section carries URL/path, publication/retrieval dates, and locators. Generation marks the run `completed` and never fabricates a citation, quote, or page number.

### `agletras_validate_report` *(Phase 3, extended in Phase 6)*

Re-run structural validation on a stored report (`report_id`). Checks: report content hash vs. stored copy and on-disk file; every rendered `[Sn]` resolves through the stored `citation_map` to a citation of this run; each citation's passage exists, belongs to its source, matches its hash and content offsets, and contains its stored quote; every claim is rendered and its status is consistent with its citation relations; references carry available provenance; and `PRAGMA foreign_key_check` is clean. Response: `{ok, report_id, run_id, path, validation_kind: "structural", semantic_support: "not_evaluated", passed, status: "validated"|"failed", checked, errors, warnings, claim_statuses, unverified_claims, contradicted_claims, cited_sources, validated_at}` — and it updates `reports.citation_validation_status`. It never judges whether a statement is true.

For a Phase 6 synthesis report the same call runs the **extended integrity checks** and reports `validation_kind: "structural+phase6_integrity"` / `phase6_checks: true` (see the Phase 6 tool section); Phase 3/4 reports keep the structural-only path and response (`template_version: "phase3"`).

### `agletras_save_research_plan` *(Phase 4)*

Record (or update) the plan of a run: `objective` (required), `subquestions` (required; one per line — optional `-`/`*`/`1.`/`1)` list markers are stripped — or a JSON array of strings / `{"question", "priority"}` objects), optional `scope`, `assumptions` (one per line), and `limits` (JSON object; unknown keys and bad values are refused, over-ceiling values are clamped). The subquestion cap (default 8) is enforced here; saving again updates the plan metadata and *adds* new questions while existing ones keep their ids, statuses, and notes. Response: `{ok, run_id, plan, subquestions, limits, created_subquestions, total_subquestions, budget, stopping}`. A `plan` event is appended to the run log.

### `agletras_update_subquestion` *(Phase 4)*

Set one subquestion's evaluation status: `run_id`, `subquestion_id`, `status` (`pending`|`answered`|`partially_answered`|`unanswered`|`conflicting`), optional `note` (appended to the audit trail; newest text kept within the cap). Response: `{ok, run_id, subquestion, stopping}`. Unknown or cross-run ids → `not_found`; any other status → `invalid_argument`.

### `agletras_log_run_event` *(Phase 4)*

Append one execution-log entry: `run_id`, `kind` (`plan`, `search`, `source_candidate`, `source_saved`, `source_failed`, `evidence`, `claim`, `citation`, `gap`, `followup_round`, `decision`, `error`, `stopping`, `report`, `note`), `message` (required), optional `subquestion_id` / `source_id` (both validated against the run/store), and optional JSON `payload` (object; size-capped). `source_candidate` requires a subquestion and is capped per subquestion per round; `followup_round` is refused here — use `start_followup_round`. Response: `{ok, run_id, event_id, kind, message, subquestion_id, source_id, payload, created_at}` plus `candidates_used` / `candidates_remaining` for candidate events. Never log credentials or secrets.

### `agletras_start_followup_round` *(Phase 4)*

Begin one bounded follow-up round and log why it was started: `run_id`, `reason` (required), optional `subquestion_id` (the gap it targets; resets that subquestion's candidate budget). Refuses with `limit_reached` once the round limit (default 2) is used. Response: `{ok, run_id, event_id, round, rounds_used, rounds_remaining, reason, subquestion_id, limits}`.

### `agletras_finish_research_run` *(Phase 4)*

Close a run with its stopping reason: `run_id`, `status` (`completed`|`failed`, default `completed`), `reason` (required), optional `error`. Logs a `stopping` event (status and error in its payload) and marks the run completed/failed; `finish_run` is idempotent, so calling it after `generate_report` only adds the stopping event. Response: `{ok, run, stopping_event}`.

## Phase 5: image research and visual analysis *(new)*

### `agletras_search_images`

Discover images through a free, documented source (Wikimedia Commons API by default; `AGLETRAS_IMAGE_SEARCH_PROVIDER`). No API key, no scraping of search-engine result pages.

| Arg | Type | Default | Notes |
|---|---|---|---|
| `query` | string | — | required, non-empty natural-language search terms |
| `provider` | string | `AGLETRAS_IMAGE_SEARCH_PROVIDER` ("wikimedia_commons") | image source name |
| `max_results` | int | `AGLETRAS_IMAGE_MAX_RESULTS` (10) | 1..hard cap; oversized clamped, non-positive invalid |

Response: `{ok, query, provider, count, provider_limitation?, results:[{result_id, image_url, source_page_url, title, caption, creator, retrieved_at, width, height, mime_type, license_name, license_url, attribution, provider, missing_metadata, size_bytes}]}`.

Each result's `result_id` is a stable hash of the image URL. `missing_metadata` lists fields the source did not state (width, height, creator, license, …); such values are `null`/`"unknown"` — never invented. Results are deduplicated by image URL. Provider failures are reported via `provider_limitation` with an empty results list, never fabricated data.

### `agletras_analyze_image`

Analyze one or more images and return discovery metadata plus structured visual analysis split into `observations` (visible), `inferences` (uncertain), `unverified_claims`, and `limitations`.

| Arg | Type | Default | Notes |
|---|---|---|---|
| `image_url` | string | "" | http(s) image URL (SSRF-guarded) |
| `image_urls` | string | "" | optional JSON array of image URLs for comparison |
| `local_path` | string | "" | approved local image path (within `AGLETRAS_LOCAL_ROOTS`) |
| `source_id` | int | 0 | id of an already-stored image source |
| `save` | bool | false | persist the primary image's discovery metadata as evidence |
| `prompt` | string | "" | optional custom vision-model prompt (only used when vision is enabled) |

Exactly one of `image_url`/`image_urls`/`local_path`/`source_id` is required (multiple URLs allowed for comparison).

Response (single image): `{ok, capability, vision_model, vision_available, image_id, image_url, source_page_url, metadata:{…}, observations:[{image_id, image_url, text, confidence}], inferences:[…], unverified_claims:[…], limitations:[…], source_id?}`.

`capability` is `vision_available` when `AGLETRAS_VISION_MODEL_ENDPOINT` is configured, otherwise `vision_unavailable` — the image was NOT inspected and discovery metadata is still returned. Vision uses the configurable model `nemotron-3.5-super-vl-free` (via `AGLETRAS_VISION_MODEL`) only against the explicitly configured endpoint; the OpenCode-selected model is never assumed callable from the MCP server. Observations are linked to their image id/source URL in comparison mode.

Vision is never used to assert a product's authenticity, exact specifications, manufacturer, or performance from appearance alone; image content/OCR is treated as untrusted data. Discovery does not imply permission to republish — license/attribution fields must be respected.

## Phase 6: claim verification and synthesis reports *(new)*

### `agletras_extract_claims`

Extract candidate factual statements from a run's stored sources (or explicit `text`) with a deterministic sentence splitter and marker scorer — no model is required or assumed — and map each candidate to the stored passages that discuss it (term coverage, locator, exact verbatim quote where present). Extraction never verifies and never attaches citations; candidates registered as claims always start UNVERIFIED.

| Arg | Type | Default | Notes |
|---|---|---|---|
| `run_id` | int | — | required; unknown id → `not_found` |
| `text` | string | "" | extract from this explicit text instead of stored sources |
| `source_ids` | string | "" | restrict extraction to these source ids (JSON array or comma-separated); sources must be linked to the run |
| `max_claims` | int | 0 (=25) | candidates returned; negative → `invalid_argument`; bounded by `AGLETRAS_VERIFICATION_MAX_CLAIMS` (50) |
| `register` | bool | false | persist candidates as claims (text-deduplicated per run; always `unverified`) |
| `claim_type` | string | `claim` | classification for registration: `claim`, `finding`, `inference`, `unverified`, `question` (validated before the run lookup) |

Response: `{ok, run_id, source_mode, segments, candidate_count, candidates:[{candidate_id ("C1"…), text, score, markers, evidence_matches:[{passage_id, source_id, source_url/source_path, locator, coverage, quote, verbatim}], best_evidence}], registered:[{candidate_id, claim_id}], registration_requested, claim_type, extraction_method: "deterministic_sentence_marker_scoring", note}`. A `decision` event is appended to the run log. Marker scoring makes this a *candidate generator*: it can miss statements and suggest trivial ones — it is never treated as complete.

### `agletras_verify_claims`

Evaluate a run's claims against the exact stored passage text and record one explicit status per claim, with method, confidence, rationale, evidence references, and limitations. Deterministic checks run by default; a verification model is only called when `AGLETRAS_VERIFICATION_MODEL_ENDPOINT` is configured and `method="auto"`, and its response must cite stored passage ids or it is rejected (the deterministic result is recorded with method `model_fallback_deterministic`).

| Arg | Type | Default | Notes |
|---|---|---|---|
| `run_id` | int | — | required; unknown id → `not_found` |
| `claim_ids` | string | "" | subset of claim ids (JSON array or comma-separated); default is every claim of the run, bounded by `AGLETRAS_VERIFICATION_MAX_CLAIMS` (50) |
| `apply_citations` | bool | true | attach `supports`/`partially_supports` citations for decisive matches (never for contradictions/conflicts) |
| `method` | string | `auto` | `auto` (use the configured verification model when available, else deterministic) or `deterministic` (never call a model); anything else → `invalid_argument` (validated first) |

Statuses: `supported` (best coverage ≥ `SUPPORT_THRESHOLD` 0.85, numbers/dates confirmed), `partially_supported` (≥ 0.50 or a blocking conflict signal), `contradicted` (closest passage disagrees and nothing supports), `conflicting_evidence` (independent passages disagree), `unsupported` (relevant passages but none establishes the claim), `unverifiable` (best coverage < `MIN_RELEVANCE` 0.25). Response: `{ok, run_id, verified_count, counts_by_status, methods_used, model_configured, model_available, method_requested, deterministic_only, claims:[{claim_id, claim_text, claim_type, derived_support_status, verification:{status, method, confidence, rationale, limitations, evidence:{…, candidates:[…]}, checks:{deterministic_assessment, thresholds:{min_relevance, partial, support}, method_requested, model}}, citation_attached, citation_relation, derived_status_after}], note}`. Auto-attached citations carry `metadata.verified_by = "agletras_phase6"` and always a quote that occurs in the passage. A `claim` event per claim is appended to the run log.

### `agletras_analyze_conflicts`

List every disagreement visible in a run's stored evidence: claims with both supporting and contradicting citations, verification results that detected number/date/negation disagreements between independent sources, and subquestions marked `conflicting` — each with the evidence on both sides (source, URL/path, date, locator, quote, content hash, independence group), the differences that may explain it, and whether it remains unresolved. A `decision` event is appended to the run log.

| Arg | Type | Default | Notes |
|---|---|---|---|
| `run_id` | int | — | required; unknown id → `not_found` |
| `include_sources` | bool | true | include `source_assessment`: metadata-driven source-quality indicators (directness/age/metadata heuristics), kept separate from verification statuses |

Response: `{ok, run_id, count, unresolved_count, independent_conflict_count, conflicts:[{kind, claim_id?, question?, status, resolved, resolution, sides:[{relation, …evidence…}], differences}], source_assessment?, note}`. Conflicts are never resolved by preference — not by popularity, not by simply choosing the newest source.

### `agletras_generate_markdown_report`

Render, store, and integrity-validate the ten-section Phase 6 synthesis report for a run. All content is derived from stored rows (plans, claims, citations, passages, verification records, conflicts, image metadata); the file is written inside the reports directory, identical content is deduplicated (same run + content hash + template returns the existing report), and existing files are never overwritten (unique numeric suffix).

| Arg | Type | Default | Notes |
|---|---|---|---|
| `run_id` | int | — | required; unknown id → `not_found` |
| `title` | string | "" | optional title (default derived from the run query) |
| `include_source_assessment` | bool | true | include the source-quality indicator tables |

Sections (with stable `[F#]` findings, `[Sn]` citations, `[IMG#]` images): title/metadata; executive summary (question, evidence base, verification method used, status counts, headline supported findings); scope and methodology (stored facts only); key findings (facts / inferences / unverified statements kept apart, each with status label, method, confidence, rationale, limitations); evidence comparison (status overview + `[F#]`/`[Sn]`/source/locator/relation tables + optional source-quality indicators); conflicting evidence (unresolved conflicts stay visible); limitations and open questions; conclusion (status-derived); sources and references (all run sources + `[Sn]` references with provenance and the "no liveness check was performed" statement); image attribution. Response: `{ok, report_id, run_id, title, path, filename, template_version, markdown, finding_map, citation_map, image_map, generation_config, created, deduplicated, warnings, validation, validation_status, claims_rendered, counts_by_status, unresolved_conflicts}`; a `report` event is appended to the run log. `report_max_findings` (60) produces a **warning** when exceeded — it never truncates. Generation does not mark the run completed; close it with `finish_research_run`.

### `agletras_get_report`

Fetch one stored report (either template) with metadata, validation status, sections, and identifier maps. Args: `report_id` (required; unknown → `not_found`), `include_content` (default false). Response: `{ok, report_id, run_id, title, path, format, template_version, finding_map, citation_map, image_map, citation_validation_status, validation, generated_at, validated_at, sections, character_count, line_count, file_exists, file_hash_matches, content?}`. `file_exists`/`file_hash_matches` compare the on-disk file against the stored content hash without rewriting anything.

## Phase 8: project memory, monitoring, jobs, visualizations *(new)*

Four MCP tools extend the catalog to **38**; the rest of Phase 8 is CLI-only (`agletras export|profiles|jobs|monitor`) so the product features work with no model in the loop. Corresponding modules: `parallel`, `specialists`, `memory`, `monitor`, `visualize`, `jobs`.

### `agletras_search_memory` *(Phase 8)*

Recall stored evidence passages across **every** run (project memory) with full provenance and honest freshness flags — the server-side counterpart of `search_evidence`, which stays run-scoped. Matching/ranking reuse `search_evidence` (FTS5 with the LIKE fallback); recall only adds run links, project labels, and age, and never rewrites a passage.

| Arg | Type | Default | Notes |
|---|---|---|---|
| `query` | string | — | required keywords/phrase |
| `project` | string | "" | only passages linked to runs tagged with this project |
| `limit` | int | 10 | bounded by `max_list_limit` (200) |
| `max_age_days` | int | 0 (= no bound) | excludes only passages whose age is **known** to exceed it; unknown-age passages stay, marked `fresh: null` |

Response: `{ok, query, engine, project, max_age_days, scanned, count, excluded_by_project, excluded_as_stale, passages:[{…passage fields…, run_ids, projects, age_days, fresh}]}`.

### `agletras_monitor_add_target` *(Phase 8)*

Register or update a monitoring target: an http(s) URL or a permitted local file. **Nothing is fetched when a target is added** — targets never check themselves; use `monitor_check` (or `agletras monitor check`) for manual runs. Idempotent by canonical identity (canonical URL for pages, resolved path for files), so re-adding updates the label/project instead of duplicating a row. Paths outside the configured access roots are refused upfront (`access_denied`).

| Arg | Type | Default | Notes |
|---|---|---|---|
| `target` | string | — | required; URL or permitted local file |
| `name` | string | "" | label (default: host for URLs, file name for paths) |
| `project` | string | "" | project label that scopes checks and change queries |
| `kind` | string | "" | force `url`/`path` when the target alone is ambiguous |

Response: `{ok, target: {target_id, target_key, name, kind, target, project, config, enabled, created_at, updated_at, last_snapshot}, created}`.

### `agletras_monitor_check` *(Phase 8)*

Check monitoring targets now (the manual run): conditional HTTP requests (`If-None-Match`/`If-Modified-Since`, where stored), normalized content hashes, and meaningful-change detection that ignores navigation/ads/cookie/timestamp churn. Every target is checked independently through the bounded pool (per-target failure isolation; `parallel=false` forces sequential), and each check writes one immutable snapshot. A `304` is `not_modified`; a failed fetch is `failed` — **never** a change or a deletion.

| Arg | Type | Default | Notes |
|---|---|---|---|
| `target_ids` | string | "" | ids as JSON array or comma/space-separated; empty = every enabled target (optionally project-filtered) |
| `project` | string | "" | only enabled targets with this project label |
| `parallel` | bool | true | bounded parallel checks; false runs the sequential fallback |

Response: `{ok, checked, parallel, counts_by_status, snapshots:[{snapshot_id, target_id, checked_at, status, meaningful, http_status, etag, last_modified, content_hash, raw_hash, source_id, content_id, previous_snapshot_id, chars, added_terms, removed_terms, ignored_boilerplate, summary, error_code, error_message}], warnings}`.

### `agletras_monitor_changes` *(Phase 8)*

List detected changes, newest first, with both sides of the comparison: the change snapshot (with its evidence reference, hashes, and added/removed substantive terms) and the previous snapshot it was compared against. Unchanged, not-modified, and failed checks are not changes and never appear here (with `meaningful_only=false`, boilerplate-only churn is included).

| Arg | Type | Default | Notes |
|---|---|---|---|
| `target_id` | int | 0 (= every target) | restrict to one monitoring target |
| `project` | string | "" | restrict to targets with this project label |
| `meaningful_only` | bool | true | only meaningful changes |
| `since_days` | int | 0 (= no bound) | only changes detected in the last N days |
| `limit` | int | 50 | bounded by 5×`max_list_limit` |

Response: `{ok, count, meaningful_only, changes:[{target: {target_id, name, target, kind, project}, change: {…snapshot…, source}, previous: {…snapshot…, source} | null}]}`.

## 3. Error codes

Failures are returned as `{"ok": false, "error": {"code": ..., "message": ...}}`:

| Code | Meaning |
|---|---|
| `invalid_argument` | bad input (empty query, non-numeric ids, wrong kind, non-http scheme, negative `max_chars`, `max_results` < 1, embedded credentials, unsupported URL scheme in `collect_sources`, folder that is not a directory; Phase 6: unknown `claim_type`, unknown verification `method`, negative `max_claims`) |
| `not_found` | missing file or unknown source id |
| `access_denied` | *(Phase 2)* local path is outside the configured access roots — including `..` traversal or a symlink that resolves outside — or an HTTP 401/403 |
| `network_error` | connection/DNS failure, timeout (message includes status/URL); retriable |
| `http_error` | *(Phase 2)* HTTP status outside the mapped classes |
| `rate_limited` | provider rate limiting: HTTP 429 or a search challenge/anomaly page; retriable |
| `blocked_host` | SSRF guard: private/loopback/link-local/reserved/CGNAT/multicast target, disallowed domain, or blocked redirect destination (redirect destinations are re-validated in the pipeline too) |
| `too_many_redirects` | redirect chain exceeded the configured cap (default 10) |
| `provider_error` | a search provider crashed unexpectedly or returned unparsable data (recorded on the `search_result` record when ingestion fails); *(Phase 6)* the configured verification model returned no JSON object / no rationale (the deterministic result is then recorded instead, method `model_fallback_deterministic`; `"no verification model endpoint configured"` is raised only on direct non-auto calls) |
| `too_large` | response/PDF/folder file exceeds the configured byte cap |
| `unsupported_content` | PDF passed to fetch_url, non-PDF file to extract_pdf, binary content type, unsupported local extension |
| `empty_file` | *(Phase 2)* parsed document contains no extractable rows/text |
| `malformed_file` | *(Phase 2)* unreadable/corrupt structure (bad zip, broken XML, empty XLSX shared-strings mismatch) |
| `empty_content` | *(Phase 2)* structure parsed but no usable text (e.g. image-only scanned pages → partial + OCR advisory) |
| `server_error` | HTTP ≥ 500; retried up to `AGLETRAS_COLLECT_MAX_RETRIES` |
| `pdf_error` | unreadable or password-encrypted PDF |
| `storage_error` | SQLite failure (or a Phase 6 report file that could not be written) |
| `conflict` | *(Phase 3)* a database constraint rejected the write (duplicate identity/claim, dangling foreign key, deleting cited evidence) — the whole transaction rolled back; *(Phase 8)* duplicate job name (case-insensitive) or recording a job run against an unknown research run |
| `limit_reached` | *(Phase 4)* a per-run research limit refused the action (subquestion cap, follow-up rounds, candidate sources per subquestion per round, or unique sources per run); the message says what remains and how to proceed |
| `empty_selection` | report requested with no matching sources |
| `internal` | unexpected exception (logged with traceback to stderr) |

## 4. Storage schema

Phase 1 base (unchanged):

```sql
CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- 'url' | 'pdf' | 'note' | 'doc' | 'search_result'  (extended in Phase 2)
    title TEXT NOT NULL DEFAULT '',
    url TEXT, path TEXT,
    content TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL,            -- de-duplication key
    created_at TEXT NOT NULL         -- UTC ISO-8601
);
```

Phase 2 adds columns via a `_migrate()` step at startup (an `ALTER TABLE` per new column, applied only when missing — old databases upgrade in place, nothing is rebuilt):

```sql
source_type TEXT,      -- precise type: web/search_result/pdf/markdown/text/csv/docx/xlsx
original_url TEXT,     -- what was requested
final_url TEXT,        -- after redirect hops
content_type TEXT,
file_size_bytes INTEGER,
publication_date TEXT, -- from explicit metadata only; NULL when the source states none (never invented)
retrieved_at TEXT,     -- UTC ISO-8601
extraction_status TEXT NOT NULL DEFAULT 'success',
error_code TEXT, error_message TEXT,
metadata TEXT          -- JSON object: page counts, sheet stats, snippet_only, html_title, origins[, …]
```

`metadata.origins` is the provenance trail for duplicates: the first stored row keeps `[{requested, at}]` for itself and gains one entry for every later re-request that hit the same content (`created: false` in the response). Dedupe keys: `sha256` (exact) plus the Phase 2 near-duplicate checks performed at save time.

### Phase 3 (v2) — evidence library tables

Added by migration v2 (`storage.MIGRATIONS`), applied in one transaction with the version row written last; `sources` gains four columns (`canonical_url`, `content_id` → `source_contents` `ON DELETE SET NULL`, `raw_hash`, `last_checked_at`).

```sql
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);

CREATE TABLE research_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running',   -- running|completed|failed
    config TEXT NOT NULL DEFAULT '{}', started_at TEXT NOT NULL, completed_at TEXT, error TEXT);

CREATE TABLE source_contents (                                     -- content-addressed cache
    id INTEGER PRIMARY KEY AUTOINCREMENT, content_hash TEXT NOT NULL UNIQUE,  -- sha256(text)
    raw_hash TEXT, extracted_text TEXT NOT NULL DEFAULT '',
    extraction_status TEXT NOT NULL DEFAULT 'success', error_code TEXT, error_message TEXT,
    content_type TEXT, file_size_bytes INTEGER, storage_location TEXT,
    metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE source_identities (                                   -- canonical URL / resolved path
    identity TEXT PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    identity_kind TEXT NOT NULL DEFAULT 'url', created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);

CREATE TABLE research_run_sources (
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL, PRIMARY KEY (run_id, source_id));

CREATE TABLE passages (                                            -- immutable exact slices
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    content_id INTEGER NOT NULL REFERENCES source_contents(id) ON DELETE RESTRICT,
    content_hash TEXT NOT NULL, passage_index INTEGER NOT NULL,
    text TEXT NOT NULL, passage_hash TEXT NOT NULL,                -- sha256(passage text)
    start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,    -- content[start:end] == text
    page INTEGER, heading TEXT, locator TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    UNIQUE (source_id, content_hash, passage_index));

CREATE TABLE claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    claim_text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unverified',  -- derived, never asserted
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE (run_id, claim_text));

CREATE TABLE citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE RESTRICT,   -- no citation without a passage
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    relation TEXT NOT NULL DEFAULT 'supports',    -- supports|partially_supports|contradicts|context|needs_review
    quote TEXT NOT NULL DEFAULT '', locator TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
    UNIQUE (claim_id, passage_id, relation));

CREATE TABLE reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '', path TEXT NOT NULL, format TEXT NOT NULL DEFAULT 'markdown',
    content TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL,
    citation_map TEXT NOT NULL DEFAULT '{}',                       -- {"S1": citation_id, …}
    citation_validation_status TEXT NOT NULL DEFAULT 'unvalidated', -- unvalidated|validated|failed
    validation TEXT NOT NULL DEFAULT '{}', generated_at TEXT NOT NULL, validated_at TEXT);
```

Indexes cover run status/date, canonical URLs, identities, content raw hashes, passages (source+content, hash), claims (run+status, status), citations (claim/passage/source), and reports (run, date). Connections always enable `PRAGMA foreign_keys = ON` plus WAL journaling and a busy timeout; every operation runs in one transaction that commits at the end or rolls back entirely (constraint violations surface as the `conflict` error code). When the SQLite build has FTS5, `passages_fts` (external-content FTS5 on `passages`, kept in sync by triggers) powers passage search; otherwise — or with `AGLETRAS_EVIDENCE_FTS=off` — the parameterized `LIKE` fallback is used.

### Phase 4 (v3) — research planner tables

Added by migration v3 (`storage.MIGRATIONS`), same idempotent-transaction pattern; `research_runs` gains one column.

```sql
plan TEXT NOT NULL DEFAULT ''   -- added to research_runs: JSON {objective, scope, assumptions, limits, created_at, updated_at}

CREATE TABLE research_subquestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    question TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,       -- 1 = highest; defaults to input order
    status TEXT NOT NULL DEFAULT 'pending',    -- pending|answered|partially_answered|unanswered|conflicting
    note TEXT NOT NULL DEFAULT '',             -- append-only evaluation trail
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (run_id, question));

CREATE TABLE research_run_events (             -- append-only execution log
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
    subquestion_id INTEGER REFERENCES research_subquestions(id) ON DELETE SET NULL,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    payload TEXT NOT NULL DEFAULT '{}',        -- validated JSON object
    created_at TEXT NOT NULL);
```

Indexes: `(run_id, seq)` on subquestions; `(run_id, id)` and `(run_id, kind)` on events. Unique-source accounting reads `research_run_sources`; follow-up rounds, candidate counts, and the budget/stopping state are derived from the tables above — no counters are stored separately, so the log and the state can never disagree.

Inspect directly with `sqlite3 data/research.db` if desired. Source keyword search (`search_sources`) still uses `LIKE`; passage search uses FTS5 when available.

### Phase 6 (v4) — claim verifications and report metadata
Added by migration v4 (`storage.MIGRATIONS`), same idempotent-transaction pattern; `claims` and `reports` gain columns.

```sql
CREATE TABLE IF NOT EXISTS claim_verifications (       -- append-only audit: one row per verification pass
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    status TEXT NOT NULL,                              -- supported|partially_supported|contradicted|conflicting_evidence|unsupported|unverifiable
    method TEXT NOT NULL DEFAULT 'source_text_comparison',
    confidence TEXT NOT NULL DEFAULT 'low',
    rationale TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '{}',               -- evidence passage ids / candidate checks
    checks TEXT NOT NULL DEFAULT '{}',                 -- deterministic assessment + thresholds
    limitations TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL);

-- claims gains:   claim_type TEXT NOT NULL DEFAULT 'claim'
--                 note TEXT NOT NULL DEFAULT ''
--                 verification_status TEXT NOT NULL DEFAULT ''  -- cached latest assessment (written with its audit row)
--                 verified_at TEXT
-- reports gains:  template_version TEXT NOT NULL DEFAULT 'phase3'  -- 'phase6' for synthesis reports
--                 finding_map TEXT NOT NULL DEFAULT '{}'           -- {"F1": claim_id, …}
--                 image_map TEXT NOT NULL DEFAULT '{}'             -- {"IMG1": source_id, …}
--                 generation_config TEXT NOT NULL DEFAULT '{}'
```

Indexes: `(claim_id, id)` and `(run_id, status)` on `claim_verifications`. The Phase 3 `claims.status` (derived from citation relations) is untouched; verification status is a separate, explicit layer, so both remain independently inspectable. The Phase 3 `citation_map` is joined by `finding_map`/`image_map` for the synthesis template. Phase 5 added no tables: image sources are regular `sources` rows tagged `metadata.kind = "image"`.

### Phase 7 (v5) — CLI run sessions

Migration v5 (same database, in place) adds the CLI run-session state. The Phase 1–6 tables and columns are untouched, and the tool-level `evidence.finish_run` still writes only `running`/`completed`/`failed` — the CLI writes `partial`/`cancelled`/`interrupted` through its own `runs.set_run_status`.

```sql
research_runs gains:  stage TEXT NOT NULL DEFAULT ''        -- current pipeline stage
                      updated_at TEXT                      -- last checkpoint/status change

CREATE TABLE research_checkpoints (                           -- append-only stage attempts
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,                                     -- plan | search@N | collect@N | images@0 |
                                                             -- claims@N | verify@N | evaluate@N | report
    status TEXT NOT NULL,                                    -- started|complete|failed|interrupted|skipped
    attempt INTEGER NOT NULL,                                -- per-stage retry counter
    detail TEXT NOT NULL DEFAULT '{}',                      -- JSON: stage-specific results
    error_code TEXT, error_message TEXT,
    started_at TEXT, finished_at TEXT, duration_ms INTEGER,
    created_at TEXT NOT NULL);
```

One checkpoint row is one transaction (atomic by construction). `stage_states(run_id)` reads the latest row per stage and drives resume; `updated_at` feeds the staleness check (`research.resume_max_age_days`). `SCHEMA_VERSION = 5`; a database with a newer `schema_migrations` row is refused by `doctor` rather than downgraded.

### Phase 8 (v6) — monitoring and project memory

Migration v6 (same database, in place) adds the project label to runs plus monitoring targets and their snapshot history. Targets are identified by a canonical `target_key` (canonical URL or resolved path); every check appends one immutable snapshot that references the stored evidence rows for the compared content, so a detected change stays traceable after the fact.

```sql
research_runs gains:  project TEXT NOT NULL DEFAULT ''        -- scopes memory reuse/recall + monitoring

CREATE TABLE monitor_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_key TEXT NOT NULL UNIQUE,                          -- canonical identity (no duplicate targets)
    name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'url',                         -- url | path
    target TEXT NOT NULL,                                     -- URL or resolved local path
    project TEXT NOT NULL DEFAULT '',
    config TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE monitor_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL REFERENCES monitor_targets(id) ON DELETE CASCADE,
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL,                                     -- initial|unchanged|changed|not_modified|failed
    meaningful INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    etag TEXT, last_modified TEXT,                            -- conditional-request validators
    content_hash TEXT,                                        -- normalized substantive-text hash
    raw_hash TEXT,                                            -- exact extracted-text hash
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    content_id INTEGER REFERENCES source_contents(id) ON DELETE SET NULL,
    previous_snapshot_id INTEGER REFERENCES monitor_snapshots(id) ON DELETE SET NULL,
    chars INTEGER,
    added_terms TEXT NOT NULL DEFAULT '[]',                   -- substantive lines added/removed
    removed_terms TEXT NOT NULL DEFAULT '[]',
    ignored_boilerplate TEXT NOT NULL DEFAULT '[]',           -- what differed but was classified as boilerplate
    summary TEXT NOT NULL DEFAULT '',
    error_code TEXT, error_message TEXT,                      -- failure facts, never a deletion inference
    created_at TEXT NOT NULL
);
```

Indexes: `(project, id)` on targets; `(target_id, checked_at)` and `(target_id, status)` on snapshots. Failures keep their error code/message on the snapshot and leave `source_id`/`content_id` pointing at the last known content.

### Phase 8 (v7) — named repeatable jobs

Migration v7 adds job definitions and their run history. A job stores the question plus the option *references* (profile name, project, title) — never a resolved options snapshot — so `jobs run` always applies the current profile and branding.

```sql
CREATE TABLE research_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,                 -- case-insensitive unique name
    question TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT '',                         -- config-file profile name (resolved at run time)
    project TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    last_run_id INTEGER REFERENCES research_runs(id) ON DELETE SET NULL,
    run_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE research_job_runs (                              -- append-only per-run history
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES research_jobs(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    requested_at TEXT NOT NULL
);
```

Deleting a job cascades its history rows but never its research runs (both FKs to runs are `SET NULL`/`CASCADE`-from-job respectively). `SCHEMA_VERSION = 7`.

## 5. Verification log

### Phase 0 build (2026-10-17, version 0.1.0)

| Check | Command | Result |
|---|---|---|
| Unit + protocol suite | `.venv/bin/python -m unittest discover -s tests -t . -v` | **31/31 OK** (storage, PDF, webfetch, stdio protocol incl. error envelopes and cross-restart persistence) |
| OpenCode connection | `opencode mcp list` | `✓ agletras  connected` |
| Tool call via OpenCode | `opencode run … "Call the agletras_stats tool…"` | Agent discovered and called `agletras_stats`; returned `{"ok": true, "db_path": ".../data/research.db", "total": 0, …}` |
| PDF extraction via OpenCode | `opencode run … "Use agletras_extract_pdf on samples/sample-research-paper.pdf…"` | `page_count: 2`, `has_text: true`, marker `AGLETRAS_SAMPLE_2026` found on both pages, no warnings |
| Workflow via OpenCode | fetch `https://example.com` (save=true) → `agletras_export_report` | source_id `1` saved; report `reports/example-source-check-20261017-140636.md` with 1 citation (see below) |

Report excerpt produced during verification:

```markdown
# Example Source Check

*Generated 2026-10-17 14:06 UTC by agletras-mcp · sources: 1*

## Sources

1. **Example Domain** — https://example.com
   - id `1` · kind: url · saved: 2026-10-17
   - excerpt: “This domain is for use in documentation examples …”
```

### Phase 1 (2026-10-17, health_check / search_web / SSRF-guarded fetch_url)

| Check | Command | Result |
|---|---|---|
| Full suite | `.venv/bin/python -m unittest discover -s tests -t .` | **88/88 OK** (netpolicy, search, webfetch, protocol, storage, PDF) |
| Startup + discovery | manual initialize/`tools/list` over the configured launch command (`…/.venv/bin/python -m agletras_mcp`) | 10 tools exposed; stdout carries protocol JSON only; stderr only logs |
| OpenCode connection (fresh session) | `opencode run --standalone --print-logs …` → server log | `mcp connected server=agletras tools=10` |
| `health_check` via OpenCode | `opencode run --standalone … "Call the agletras_health_check tool…"` | exact JSON relayed: `{"ok": true, "status": "ok", …, "tools": [10 names], "network": "not tested - …"}` |
| `search_web` via OpenCode | `opencode run --standalone … "Use agletras_search_web 'zero-cost research tools' max_results 3"` | 3 real DuckDuckGo results with title+url relayed (model's own output) |
| `fetch_url` via OpenCode | `opencode run --standalone … "Use agletras_fetch_url on https://example.com"` | `final_url / title "Example Domain" / chars 156` |
| Shared-service run (no `--standalone`) | `opencode run … "Call agletras_stats…"` | responded from the new 10-tool server |
| Live smoke (real network, real protocol subprocess) | `python /tmp/opencode/live_smoke.py` | **8/8 passed**: health_check; live DDG search (3 normalized results); empty query → `invalid_argument`; live `example.com` fetch; `localhost` → `blocked_host`; `169.254.169.254` → `blocked_host`; iana.org redirect followed; oversized `max_results` clamped |
| DuckDuckGo delivery path | urllib GET vs POST experiment | GET returns 202 anomaly challenge for this client; the standard POST form returns 200 with results — provider uses the POST form; challenge pages still map to typed `rate_limited` (never parsed/solved) |

Session note: `opencode mcp list` reports status from the *background service*; if a server child was killed (as during this debugging) the service may show a stale `✗` until it is restarted, while fresh sessions (`opencode run`, with or without `--standalone`) connect fine — both verified here.

### Phase 2 (2026-10-17, collect_sources / collect_local_folder / extract_document)

| Check | Command | Result |
|---|---|---|
| Full suite | `.venv/bin/python -m unittest discover -s tests -t .` | **174/174 OK** (88 Phase 1 tests unchanged + Phase 2: source records, officedoc, textdoc, localfs, collect pipeline, collect protocol) |
| Phase 2 unit groups in isolation | `…unittest tests.test_source_record tests.test_officedoc tests.test_textdoc tests.test_localfs tests.test_collect` | 11 + 17 + 6 + 14 + 27 OK |
| Stdio protocol suites | `…unittest tests.test_server_protocol tests.test_collect_protocol` | 7 + 10 OK — real subprocess, 13-tool catalog, `collect_sources` over md/docx/xlsx/csv fixtures, failure isolation, search snippets via a local DDG-style endpoint, `extract_document` save→`get_source` roundtrip with Phase 1 keys intact, `source_type` filter, folder skip reasons + outside-roots denial |
| Startup + discovery (fresh session) | `opencode run --auto … "Call agletras_health_check…"` | `health_check` → `status: "ok"`; tool count **13** (OpenCode-launched stdio subprocess) |
| `collect_sources` live (OpenCode) | `… "Use agletras_collect_sources with sources 'samples/sample-research-paper.pdf, notes-overview-livecheck.md', save=true"` | pdf + markdown both `success` (source_id 3, 2); summary `requested: 2, succeeded: 2, saved: 0, duplicates: 2` — **live dedupe proof**: both files matched the rows created minutes earlier (`duplicate_of: 3/2`, `duplicate_kind: "exact"`, origins appended, nothing re-stored) |
| `collect_local_folder` live (OpenCode) | `… "Use agletras_collect_local_folder with folder 'clfdemo', save=false"` | `files_picked: 2` (`a.md`, `sub/deep.md` — recursive), `files_skipped: 2` with reasons `temporary_file` / `unsupported_extension`, `scan_truncated: false`, both collected files `success` |
| `list_sources` + `extract_document` live (OpenCode) | `… "list_sources source_type markdown, then extract_document clfdemo/a.md save=true"` | filter returned count 0 before save and the saved id after; `extract_document` → `success`, `created: true`, new source id |
| Service-restart path | `kill` of stale server child, then `opencode mcp list` | list showed a transient `✗ failed: Connection closed …` until the background `opencode serve --service` was restarted; after the restart: `✓ agletras connected` and fresh sessions exposed the 13-tool catalog throughout |

Demo paths used in the live checks (`notes-overview-livecheck.md`, `clfdemo/`) were removed afterwards; the store rows they show were produced against the real local database.

Verification honestly *not* performed in this phase: a live network search ingested through `collect_sources` (`search` ingestion is verified against a local DuckDuckGo-style mock endpoint only — consistent with the hermetic-suite constraint; the Phase 1 live smoke covered real DDG for `search_web`).

### Phase 3 (2026-10-17, evidence library: runs / evidence / passages / claims / citations / validated reports)

| Check | Command | Result |
|---|---|---|
| Full suite | `.venv/bin/python -m unittest discover -s tests -t .` | **257/257 OK** (174 Phase 1/2 tests unchanged + 83 Phase 3: canonical URLs, passages, evidence library, report rendering, evidence protocol) |
| Migration on an existing database | copy of the real `data/research.db` (4 Phase 1/2 rows), `storage.stats()` before/after | v1–v2 recorded, all evidence tables created, `PRAGMA foreign_keys` on, second open unchanged (idempotent); the live store is upgraded in place by the next server start |
| Stdio protocol acceptance | `…unittest tests.test_evidence_protocol` | 2/2 OK over the real subprocess: run → local-file evidence → passage search → 2 claims (one supported, one not) → fabricated quote rejected (`invalid_argument`) → exact quote attached (`derived_status: "supported"`) → report generated (`[S1]`, UNVERIFIED marker, validated) → re-validated → history trace with `unverified_claims` → `get_source` evidence extras → **restart** sees the same run/claims/passages; error envelopes for unknown run, empty mode, bad relation, unknown report |
| Cache/dedup behaviors | `…unittest tests.test_evidence` | cache hit while the origin server is gone; `utm_*`/`fbclid` variants share one identity; two URLs with identical text keep separate sources + one shared content blob; TTL expiry attempts a refresh and _keeps_ the old evidence with `stale`/`refresh_error`; modified local file creates a second content blob while the earlier passage/citation survives; `AGLETRAS_EVIDENCE_FTS=off` exercises the LIKE fallback |
| Integrity behaviors | `…unittest tests.test_evidence` | failed write and FK violation roll back (`conflict`); a source with citations cannot be deleted (`RESTRICT`); tampered passage text and tampered claim statuses are reported as validation failures (`passage_hash_mismatch`, `passage_offset_mismatch`, `claim_status_inconsistent`); a citation from another run smuggled into a report is rejected (`citation_wrong_run`) |
| Live OpenCode check (standalone) | `opencode run --standalone -m opencode/glm-5.3-flash-free --auto "Call the agletras_health_check tool and then call agletras_stats…"` | health_check relayed the **22-tool** catalog verbatim; stats relayed `schema_version: 2`, `evidence.fts5: true` and live counts (`total: 5`, `passages: 64`, `claims: 2`, `citations: 1`, `reports: 1`) |
| Full Phase 3 workflow via OpenCode | `opencode run -m opencode/glm-5.3-flash-free --auto` with an agent-driven acceptance prompt (`start_research_run` → `save_evidence` on `docs/mcp-server.md` → `search_evidence` → two `register_claim` calls → `attach_citation` with an exact quote → `generate_report` → `validate_report`) | run `1` (“Phase 3 live acceptance check”) on the real store: source `5` saved with 64 passages; passage `33` located by search; claim “Report validation is structural and does not judge whether a statement is true.” **supported** by the exact quote ``Re-run structural validation on a stored report (`report_id`).``; claim “Agletras requires a paid API key.” left **UNVERIFIED**; report `reports/evidence-run1-phase-3-live-acceptance-20261017-172001.md` rendered with `[S1]` + UNVERIFIED marker, validated (`unverified: [1]`), and re-validated directly from the DB |
| Service path + restart | stale background-service child (13-tool build) identified, service restarted; fresh non-standalone sessions re-checked | `✓ agletras connected`; the session exposed the 22-tool Phase 3 catalog and `schema_version: 2` (the pre-restart child predated the upgrade — same background-service caching caveat noted in Phases 1–2) |

Verification honestly *not* performed in this phase: no semantic/LLM judgment of claim truth exists anywhere (by design — validation is structural and says so; a supported claim means one exact stored passage was attached, not that the statement is true in the world). The live workflow above was model-driven end to end against the real store; the stdio acceptance test additionally drives the same subprocess/protocol path hermetically.

### Phase 4 (2026-10-17, research planner + orchestrator agent)

| Check | Command | Result |
|---|---|---|
| Full suite | `.venv/bin/python -m unittest discover -s tests -t .` | **294/294 OK** (257 Phase 1–3 tests unchanged + 37 Phase 4: 33 planner unit tests, 1 quote-matching test, 3 stdio protocol tests) |
| Migration on an existing database | temp-copy of the real `data/research.db` through the storage/evidence test cases | v1–v3 recorded; `research_runs.plan` plus the `research_subquestions`/`research_run_events` tables added; a second open is a no-op; an interrupted v3 migration retries cleanly |
| Stdio protocol acceptance | `…unittest tests.test_planner_protocol` | 3/3 OK over the real subprocess with hermetic local HTTP fixtures: the **27-tool** catalog; the full workflow (plan → search → candidates/save/failure → evidence → claims with exact quotes → subquestion statuses + gap → `start_followup_round` → targeted evidence → report with question/scope, methodology, unanswered questions and sources reviewed → `finish_research_run` → restart persistence); over-the-wire `limit_reached` refusals for all four caps |
| Live orchestrator run — early stop, no gaps | `opencode run --agent research-orchestrator --standalone --auto -m opencode/glm-5.3-flash-free` with the Eiffel-Tower height/location/completion question | run `2` (real store), fully model-driven through the MCP tools: plan (objective, scope, 3 subquestions, default limits) → 2 logged searches → 3 sources saved (ids 6–8) → 3 claims **supported** with 8 exact-quote citations → a source discrepancy logged as a `decision` event → all 3 subquestions `answered` → report `reports/evidence-run2-…-20261017-175757.md` **validated** → stopped: “Stopped because all subquestions are answered (stopping.should_stop=true)…”; 15 log events, including one `source_failed` (Britannica answered HTTP 403 and the run continued on other sources) |
| Live orchestrator run — honest gap + bounded follow-up | same, with the Eiffel-Tower visitor-numbers question (2024 total + exact January-2024 daily average) | run `3` (real store): 4 planned subquestions → 19 logged searches, 5 sources, 6 claims (5 supported/partially, 1 left **UNVERIFIED with no citations**) → **2/2 follow-up rounds**, each opened with a logged reason about the unfilled January figure → that subquestion left `unanswered` → report `reports/evidence-run3-…-20261017-180743.md` **validated** with the “Unanswered questions” section and the unverified claim marked → stopped: “Evidence sufficient for 3 of 4 subquestions …; follow-up round limit reached (2/2) with the January-2024 exact daily-average gap remaining unfilled … — reported honestly as unanswered.”; 37 log events |
| Post-run re-validation | `evidence.validate_report_citations(2)` / `(3)` (direct call, widened quote check) | both reports `passed: true`, zero errors/warnings; report 3 still lists claim 9 as unverified |

Phase 4 live-run notes: the agent loaded from `.opencode/agents/research-orchestrator.md` and inherited the session model (`glm-5.3-flash-free`; no `model:` field), and the fresh child reported the 27-tool catalog including `save_research_plan`. Run 3 surfaced real-world quote friction — the model quoted French text with an ASCII apostrophe against a passage containing U+2019; the citation was refused exactly as designed and the model recovered with an apostrophe-free excerpt. `quote_matches` was then widened to equate typographic quote glyphs with ASCII (curly quotes/apostrophes and primes only; every other difference still fails), with a new unit test; both live reports re-validate cleanly under it.

Verification honestly *not* performed in this phase: no adversarial/prompt-injection exercise of the orchestrator loop (the server-side invariants — fabricated-quote rejection, all four caps, provenance binding — hold regardless of model behavior, but only normal model behavior was exercised live); the live runs' reports were not independently fact-checked by a human against the cited sources (validation is structural by design and says so); the bounded follow-up was exercised live via the targeted-gap path (rounds 1→2 on one subquestion), with the remaining stopping permutations covered by the hermetic tests.

### Phase 6 (2026-10-18, claim verification + synthesis reports)

| Check | Command | Result |
|---|---|---|
| Full suite | `.venv/bin/python -m unittest discover -s tests -t . -q` | **375/375 OK** (all Phase 1–5 tests unchanged + 54 Phase 6: 36 verification, 16 synthesis, 2 stdio protocol) |
| Phase 6 unit groups in isolation | `…unittest tests.test_phase6_verification tests.test_phase6_synthesis tests.test_phase6_protocol` | 36 + 16 + 2 OK — extraction/mapping, the full status model, independence grouping, number/date disagreement detection, decisive-match citation attachment, opt-in model verification (structured-response validation, rejection → `model_fallback_deterministic`, contradiction override refused), conflicts, source-quality indicators; ten-section rendering with stable `[F#]`/`[Sn]`/`[IMG#]` ids and labels, content dedupe, never-overwrite file naming, `_confined_path`/`_unique_report_path`, `report_max_findings` warn-not-truncate, integrity tamper detection, Phase 3 delegation, retrieval flags |
| Stdio protocol acceptance | `…unittest tests.test_phase6_protocol` | 2/2 OK over the real subprocess with hermetic local HTTP fixtures: the **34-tool** catalog; run → conflicting evidence → `extract_claims` (register) → exact-quote citations → `verify_claims` (status `conflicting_evidence`, method `deterministic_checks`) → `analyze_conflicts` → `generate_markdown_report` → `get_report` → `validate_report` → `finish_research_run` → **restart** sees the same stored report; structured error envelopes over the wire |
| Migration on an existing database | SQLite `.backup` copy of the real `data/research.db` (v3: 13 sources / 3 runs / 11 claims / 16 citations / 3 reports) opened through `storage.open_db` | v1–v4 recorded; `claim_verifications` plus the Phase 6 `claims`/`reports` columns added; **all row counts preserved** (13/3/11/16/3); second open unchanged (idempotent); interrupted-migration retry for v4 covered in `tests.test_evidence` |
| Model-verification safety | `tests.test_phase6_verification` (local mock endpoints only) | structured model response accepted and cross-checked against stored passage ids; missing/invented passage ids rejected with the deterministic result recorded (`model_fallback_deterministic`); a model verdict never overrides a citation-based contradiction; `method="deterministic"` never calls a model |

At the time of writing, the live `data/research.db` is still v3 on disk — the v4 migration is applied in place by the next server start (verified on the backup copy above; additive and idempotent).

Verification honestly *not* performed in this phase: no live model-based verification against a real external endpoint (opt-in by design; exercised only against hermetic mock endpoints); no adversarial tampering beyond the hermetic checks; no independent human fact-check of a synthesis report (validation is structural + Phase 6 integrity by design and says so).

### Phase 7 (2026-10-18, CLI + resumable runs + doctor, version 0.2.0)

| Check | Command | Result |
|---|---|---|
| Full suite | `.venv/bin/python -m unittest discover -s tests -t . -q` | **467/467 OK** (all 375 Phase 1–6 tests unchanged + 92 Phase 7: 16 config file, 21 runs, 10 progress, 20 CLI commands, 10 CLI research, 15 doctor) |
| CLI groups in isolation | `…unittest tests.test_config_file tests.test_runs tests.test_progress tests.test_cli_commands tests.test_cli_research tests.test_doctor` | 92/92 OK — TOML parsing/precedence/redaction/validation, checkpoints/statuses/resume assessment/config snapshots, human+JSONL progress with log-file buffering and credential sanitization, help/version/exit codes, `config show|validate`, `status`/`list`/`report`, doctor required-vs-optional failures, and the full offline pipeline |
| Offline end-to-end run | `AGLETRAS_COLLECT_BACKOFF=0 .venv/bin/python scripts/demo_research.py` | **exit 0**: plan (2 subquestions) → search (local mock DDG endpoint) → 2 sources collected → claims extracted + verified deterministically → both subquestions answered → report generated and integrity-validated (`validated`) → JSONL log written |
| Interruption + resume | `SIGTERM` mid-collect against a slow local fixture, then `agletras resume` | first run **exit 130**, status `interrupted`, resumable; resume **exit 0** `completed` with **no duplicate** sources (2), citations, or reports (1); resume of a closed run refused with `already_closed` |
| Config failure modes | unknown key in `agletras.toml`; `--depth extreme`; `--images --no-images`; bad `--verification` | all rejected with **exit 2** before any network work; `config validate` reports the invalid values |
| Secrets hygiene | config show / doctor / logs with `*_API_KEY_ENV` names and credential-bearing endpoints | secret-named settings print `***redacted***`; endpoints reduced to `scheme://host:port`; URL credentials masked in logs and snapshots |
| MCP startup + tool discovery | `doctor` (with MCP check) | stdio subprocess starts, **34/34** documented tools discovered, `health_check` ok; broken data dir → `mcp_server` fail with exit 3 |
| Clean-environment script | `scripts/clean_env_check.sh` | **ALL CHECKS PASSED** (example config validates, doctor ok in throwaway home, offline demo completes, full suite green) |
| Migration v5 on the live database | `storage.open_db` on a copy of `data/research.db` | `research_checkpoints` table + `research_runs.stage`/`updated_at` added; `SCHEMA_VERSION = 5`; existing rows and Phase 1–6 tool behavior unchanged (all 375 prior tests still pass) |

Honest notes: the CLI pipeline is deterministic and cannot rephrase weak questions or judge source quality beyond stored heuristics; the report header records the run status at generation time (the run row closes only after the report validates); agent-driven runs are intentionally not resumable via the CLI.

### Phase 8 (2026-10-18, product features: specialists / monitoring / memory / visuals / profiles / jobs, version 0.2.0)

| Check | Command | Result |
|---|---|---|
| Full suite | `.venv/bin/python -m unittest discover -s tests -t . -q` (with `AGLETRAS_COLLECT_BACKOFF=0`) | **638/638 OK in ≈89 s** (all 467 Phase 1–7 baseline tests still pass + 171 Phase 8 tests: parallel 20, specialists 22, memory 19, monitoring 28, visualization 18, jobs 15, pipeline 10, stdio protocol 3, config file +16, CLI +20) |
| Clean environment | `scripts/clean_env_check.sh` | **ALL CHECKS PASSED** (example config validates; doctor ok in a throwaway home; offline demo completes; monitor workflow add → baseline → change → report passes; full suite green) |
| Parallel vs sequential | `AGLETRAS_COLLECT_BACKOFF=0 .venv/bin/python scripts/evaluate_phase8.py --out docs/phase8-measurements.md` | identical workloads: scheduler 0.802→0.202 s (3.97×), collection 1.074→0.287 s (3.74×), monitoring 0.797→0.278 s (2.87×) — all "keep (measurably faster)"; outcomes are deterministic and equal to the sequential path (pinned by tests) |
| Evidence reuse + resume | `evaluate_phase8.py` + `test_pipeline_phase8` | second run with `--reuse-evidence`: **2 → 0** document fetches, 2 sources linked; resume: completed stages skipped, **0** additional fetches, finished in 1 ms, status `completed` |
| Monitoring honesty | `evaluate_phase8.py` + `test_monitor` + `test_phase8_protocol` + `MonitorCommandTests` | 6 targets, 1 edited → 1 changed, 5 unchanged, **0 false alerts**; timestamp/nav-only rewrites are `unchanged`; a failed fetch is a `failed` snapshot with the prior evidence intact — never a change or deletion; the local report is written inside the reports dir and never overwritten |
| Memory retrieval | `evaluate_phase8.py` (labeled corpus) | 6 topics × 4 passages, 6 queries: mean precision@5 **1.0**, recall@5 **1.0**; recall keeps provenance (`run_ids`, `projects`) and marks unknown age `fresh: null` |
| MCP startup + tool discovery | `doctor` (with MCP check) | stdio subprocess starts, **38/38** documented tools discovered, `health_check` ok; live database at `schema v7 (current)` |
| Migration v6+v7 on the live database | `doctor` on `data/research.db` | `monitor_targets`/`monitor_snapshots`/`research_runs.project` (v6) and `research_jobs`/`research_job_runs` (v7) added; `SCHEMA_VERSION = 7`; Phase 1–7 rows and behavior unchanged |
| Zero-cost / no new dependencies | `pyproject.toml`, `pip freeze` | still exactly `mcp` + `pypdf` (plus their transitive deps); no paid API calls, no keys required — every Phase 8 feature (including monitoring and memory) runs model-free from the CLI |

Honest notes: parallelism is thread-based, opt-in, and kept per workload — the benchmarked 2.9–4.0× gains are I/O-bound and the default run path stays sequential; monitoring never checks anything by itself (no scheduler/daemon) and an unchanged observation is not a liveness guarantee; memory reuse can link copies up to `reuse_max_age_days` old (their original timestamps stay visible); change detection is normalized line-text comparison, not semantic understanding, and boilerplate classification is pattern-based (raw statuses and both diff sides stay inspectable); Mermaid/CSV exports are derived artifacts, overwritten on re-export, and are not citation-validated. No live verification was performed for the CLI `jobs run` end-to-end against real external sites — job execution reuses the already-verified run pipeline (covered hermetically with local fixtures). Note for live OpenCode sessions: a running OpenCode host that connected the agletras server *before* the Phase 8 tools were added keeps its cached tool catalog (34 entries); fresh spawns — `doctor`'s `mcp_server` check and the stdio protocol suite — discover **38/38** tools, and restarting OpenCode refreshes the session's catalog.

## 6. Safety and boundaries

- **SSRF guard on every outbound request** (`netpolicy.py`, applies to `fetch_url`, PDF URL downloads, and search providers):
  only http/https; no embedded credentials; localhost/loopback, private (RFC1918), link-local (incl. cloud metadata 169.254.169.254), multicast, reserved (incl. TEST-NET), and shared/CGNAT addresses are refused — for literal-IP hosts and for *every DNS-resolved address* (IPv4-mapped IPv6 normalized); **redirects are re-validated per hop before connection**, with a hard chain cap; optional domain allowlist; explicit opt-out only via `AGLETRAS_ALLOW_PRIVATE_HOSTS` (for local development).
  Known limitation, accepted and documented: urllib re-resolves DNS at connect time, so a rebinding resolver could win a race between validation and connection. IP pinning is not supported by the standard library. The guard blocks every ordinary SSRF target.
- **Read-oriented external access:** `fetch_url`/`extract_pdf`/search only read; no uploads, no cookies, no auth headers. No JavaScript is ever executed (plain `html.parser` text extraction); PDFs serve only from genuine `%PDF-` responses.
- **Bounded resource usage:** timeouts (20 s), byte caps (10 MiB fetch / 100 MiB PDF), char caps, page caps, redirect caps, search result caps — all env-overridable.
- **Search politeness:** 2 s pacing between searches; identifying User-Agent on all requests; ad containers dropped from result pages.
- **Path handling:** local PDF paths accept absolute or project-relative locations; only files with a genuine `%PDF-` header are parsed.
- **stdout discipline:** the process logs exclusively to stderr; stdout is reserved for the protocol stream.
- **Permissions:** the server is registered in the *project* `opencode.json`; global OpenCode configuration and other projects are unaffected.

## 7. Reproducing the verification

```bash
cd ~/agletras-mcp

# 1. Tests
AGLETRAS_COLLECT_BACKOFF=0 .venv/bin/python -m unittest discover -s tests -t . -v

# 2. Connection
opencode mcp list

# 3. One-shot tool checks through OpenCode
opencode run -m opencode/glm-5.3-flash-free --auto \
  "Call the agletras_stats tool and reply with the exact JSON it returns."
opencode run -m opencode/glm-5.3-flash-free --auto \
  "Use agletras_extract_pdf on samples/sample-research-paper.pdf and report page_count and the AGLETRAS_SAMPLE_2026 marker."
opencode run -m opencode/glm-5.3-flash-free --auto \
  "Use agletras_collect_sources with sources 'samples/sample-research-paper.pdf, <any locally covered .md file>' and save=true; reply with each result's source_type, extraction_status, source_id and the summary counts."
opencode run -m opencode/glm-5.3-flash-free --auto \
  "Use agletras_collect_local_folder with folder '<a covered demo folder>' and save=false; report files_picked, files_skipped and each skip reason."

# 4. Full orchestrator workflow (Phase 4) — one request, plan → searches →
#    evidence → citations → gap handling → report, all logged in SQLite.
opencode run --agent research-orchestrator --standalone --auto -m opencode/glm-5.3-flash-free \
  "Research <question with verifiable facts> and produce a cited report."
opencode run --standalone -m opencode/glm-5.3-flash-free --auto \
  "Call agletras_get_research_history with run_id <id> and summarize the plan, each subquestion's status, the stopping reason, budget, and the report path."

# 5. Claim verification + synthesis report (Phase 6) — deterministic checks,
#    explicit statuses/conflicts, then the ten-section report + re-validation.
opencode run --standalone -m opencode/glm-5.3-flash-free --auto \
  "For research run <id>, call agletras_extract_claims with register=true, then agletras_verify_claims, then agletras_analyze_conflicts. Reply with each claim's verification status and method and the unresolved conflict count."
opencode run --standalone -m opencode/glm-5.3-flash-free --auto \
  "Call agletras_generate_markdown_report for research run <id>, reply with the report path and validation status, then call agletras_validate_report on that report_id and confirm it passes and that the report states no liveness check was performed."

# 6. Phase 8 product features — benchmark verdicts (regenerates docs/phase8-measurements.md),
#    the model-free monitoring workflow, and the memory/monitoring MCP tools.
AGLETRAS_COLLECT_BACKOFF=0 .venv/bin/python scripts/evaluate_phase8.py --out docs/phase8-measurements.md
.venv/bin/python -m agletras_mcp.cli monitor add README.md --name "Repo readme" --project demo
.venv/bin/python -m agletras_mcp.cli monitor check --project demo
.venv/bin/python -m agletras_mcp.cli monitor changes
.venv/bin/python -m agletras_mcp.cli monitor report
opencode run -m opencode/glm-5.3-flash-free --auto \
  "Call agletras_monitor_add_target with target 'README.md' and name 'Repo readme', then agletras_monitor_check, then agletras_monitor_changes; reply with the check counts and the change count."
opencode run -m opencode/glm-5.3-flash-free --auto \
  "Call agletras_search_memory with query 'evidence passages' and limit 3; reply with the count and each hit's run_ids, projects, and fresh flag."
```

## 8. Future ideas (not in this phase)

- `delete_source` tool (with an explicit "detach citations first" flow); pagination cursors
- Additional free search providers (arXiv API; RSS/feeds behind feedparser)
- Optional OCR integration if `tesseract` is ever installed; `exiftool` metadata
- HTML→Markdown conversion refinements (tables, code blocks)
- Phase 2 follow-ups: legacy binary Office formats (`.doc`/`.xls`) via third-party parsers (only if a dependency-tolerant path is justified), DOCX image/footnote extraction, per-page markdown extraction for PDFs, resumable large-folder collection
- Phase 3 follow-ups: FTS5 for source search (passage search already uses it), optional local embeddings behind an explicit opt-in (deliberately excluded to keep the tool zero-cost and model-free), report diffing across runs, citation export to BibTeX/CSL-JSON, scheduled re-checking of stale sources

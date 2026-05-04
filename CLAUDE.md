# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Python 3.12 + `uv`. CLI `monitorul-ii` with two subcommands: `fetch` (scrape PDFs by date) and `convert` (PDF → markdown with extraction-friendly YAML frontmatter). Hatchling-backed package at `src/monitorul_ii/`.

## Layout

- `src/monitorul_ii/scraper.py` — pure functions: `fetch_index`, `parse_issues`, `download_pdf`, `scrape_day`, plus `_with_retry`. Sha256 + size are computed in the streaming download. No CLI concerns.
- `src/monitorul_ii/converter.py` — pure functions: `parse_filename`, `enrich_meta`, `clean_markdown`, `convert_pdf`, `convert_all`, `collect_pdfs`. Wraps `pymupdf4llm.to_markdown` and applies an MO-specific cleanup pass + YAML frontmatter prepend. No CLI concerns.
- `src/monitorul_ii/classifier.py` — pure functions: `parse_issue_suffix`, `classify`, `classify_file`, `collect_mds`, plus `ClassifyResult` (with `is_ambiguous(threshold)`). Detection rules from `docs/extraction-schema.md` build-order step 1: filename suffix (`c`/`R`) + body markers (`(STENOGRAMA)`, `DEZBATERI PARLAMENTARE`, `ȘEDINȚE COMUNE …`, `(RAPOARTE DE ACTIVITATE)`, `LISTA ÎNTREBĂRILOR ADRESATE`, `SINTEZA LUCRĂRILOR COMISIILOR`). Reads only the first `HEADER_WINDOW_BYTES=10_000` of each MD. `_COMPATIBLE_RUNNERS_UP` suppresses "ambiguous" flagging when the runner-up is structurally implied by the winner (joint_session ⊃ stenogram; report_facsimile is received in joint/single sessions). No CLI concerns.
- `src/monitorul_ii/uploader.py` — `S3Config.from_env()` + `Uploader` (boto3, S3-compatible incl. R2). `upload_if_missing(path, key=None, content_type="application/pdf")` returns `UploadResult(uploaded, etag)`.
- `src/monitorul_ii/db.py` — `DB` wraps the SQLite audit log (`days` + `issues` tables). Owns the resume-gate logic via `should_fetch_index`. Tracks PDFs only — MD conversion state lives on the filesystem + S3 head.
- `src/monitorul_ii/cli.py` — argparse with subparsers. `cmd_fetch` orchestrates download → upload + DB; `cmd_convert` orchestrates pdf-to-md → upload. Entry point `monitorul-ii = "monitorul_ii.cli:main"`. `_ProgressReporter` (fetch) and `_ConvertProgressReporter` (convert) each show a live `rich` bar on stderr when `sys.stderr.isatty()`, falling back to a periodic heartbeat in pipes/cron (`_HEARTBEAT_EVERY=100` days for fetch, `_CONVERT_HEARTBEAT_EVERY=50` PDFs for convert). Ctrl+C in either mode prints a final `interrupted: …` summary and exits 130 — no traceback. For convert, the executor calls `shutdown(wait=True, cancel_futures=True)` on KbdInt so in-flight workers finish their current PDF (no partial `.md` writes) and queued ones are cancelled, avoiding the atexit thread-join race that otherwise dumps a traceback on a second Ctrl+C.
- `src/monitorul_ii/__main__.py` — also runnable via `python -m monitorul_ii`.

## How the scraper talks to the site

There is no documented API. Reverse-engineered from the e-monitor page:

- **Index endpoint**: `POST https://monitoruloficial.ro/ramo_customs/emonitor/get_mo.php` with form body `today=YYYY-MM-DD` and a `Referer: https://monitoruloficial.ro/e-monitor/` header. Returns an HTML fragment — one `<div class="card-body">` per Partea, with `<a href="/Monitorul-Oficial--P<part>--<num>--<year>.html">` links.
- **PDF**: GET that `.html` URL — the response body **is** the PDF (`Content-Type: application/pdf`). The `.html` extension is misleading.
- Issue numbers can carry suffixes (`358Bis`, `12c`) — the parser regex accepts `[0-9A-Za-z]+`.
- Empty days (weekends, no Partea II that day) return zero issues; not an error.

## Commands

- Install / sync deps: `uv sync`
- Fetch PDFs: `uv run monitorul-ii fetch <YYYY-MM-DD> [--until YYYY-MM-DD] [--out DIR] [--part II] [--delay 0.5] [--proxy URL | --no-proxy] [--bucket NAME | --no-upload] [--db PATH | --no-db] [--reverse] [--force] [--rescrape-recent N] [--retry-gone]`
- Convert PDFs to markdown: `uv run monitorul-ii convert <path> [<path> ...] [--force] [-j N | --workers N] [--reverse] [--bucket NAME | --no-upload]` — paths are files or directories; directories are globbed `*.pdf` (non-recursive). Default `-j` is `os.cpu_count()`; conversions run in a `ThreadPoolExecutor`. `--reverse` flips the final PDF list (newest→oldest given the date-prefixed filenames) — same semantics as `fetch --reverse`.
- Classify converted MDs: `uv run monitorul-ii classify <path> [<path> ...] [--outliers] [--ambiguity-threshold MARGIN] [--reverse]` — sweep MDs and emit one JSONL row per file to stdout (`top_type`, `top_score`, `second_type`, `second_score`, `ambiguous`, `all_scores`, `matched_signals`). `--outliers` filters to docs that classified as `other` or are ambiguous (top-vs-second margin below `--ambiguity-threshold`, default 0.2) — those are the unknown unknowns the schema-discovery loop wants to inspect. `--reverse` walks newest→oldest like the other subcommands.
- Test: `uv run pytest` (suite under `tests/`, ~130 unit tests, no network or boto3 — `httpx.MockTransport` for the scraper, `tmp_path`-backed SQLite for the DB, `monkeypatch` for `convert_pdf`).
- Lint: `uv run ruff check`
- Format: `uv run ruff format`

`PROXY_URL` from `.env` (auto-loaded via `python-dotenv`) routes all monitoruloficial.ro traffic through an HTTP/HTTPS proxy. `--proxy` overrides; `--no-proxy` bypasses both. Passwords in the proxy URL are masked in stderr logs.

When the full set of `S3_ENDPOINT` / `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` / `S3_BUCKET` env vars is present, every PDF is also pushed to S3 (Cloudflare R2 works as the S3 endpoint). Upload is per-file and idempotent: `head_object` first, `upload_file` only if missing. `--no-upload` disables the mirror; `--bucket` overrides `S3_BUCKET`. Object key = local filename, flat. Startup does a `head_bucket` fail-fast. ETags are recorded in the DB.

A SQLite audit log at `data/monitorul.db` (override `--db PATH`, disable `--no-db`) gates whether each day's index POST happens. `days.status='ok'` past days short-circuit the index fetch on resume — including weekends and empty days that have zero Partea II issues. The filesystem and bucket still gate per-PDF skip; the DB doesn't pretend to know whether a file actually exists. `--reverse` walks newest→oldest. `--force` ignores the DB skip; `--rescrape-recent N` re-fetches the last N days regardless of status (today is always re-fetched). See `docs/architecture.md` for the schema and resume contract.

Per-request retries: 3 attempts with backoff `1s → 2s → 4s` for transient errors (5xx, 429, transport). 4xx-not-429 / parse / content-type errors raise immediately. Failures are then classified at the call site: **transient → `status='failed'`** (auto-retried on the next run); **permanent → `status='gone'`** (terminal — content-type mismatch or 4xx-not-429, which mean the server doesn't have the resource as a PDF; not auto-retried, treated like `downloaded`/`uploaded` by the resume gate). `--retry-gone` resets every `gone` row back to `pending` for one-off recovery if the source site restores missing documents. `attempts` is informational.

PDFs land directly in `<out>/<YYYY-MM-DD>_MO-P<part>-<num>-<year>.pdf` (no per-day subdirectory — the date is in the filename so everything sorts chronologically in one folder). Re-runs skip files already on disk; partial downloads write to a `.part` file and are renamed atomically on success. Sha256 + size land in the DB during streaming download (or lazily during the existing-file skip path).

`convert` produces `<basename>.md` next to each `<basename>.pdf` via `pymupdf4llm.to_markdown` plus an MO-specific cleanup pass (strips per-page `MONITORUL OFICIAL...` running headers, image placeholders, standalone page numbers; joins hyphenated word breaks; collapses extra blank lines) and a YAML frontmatter prepend (`issue`, `year`, `part`, `published` from the filename; best-effort `chamber`, `session`, `session_date`, `legislature` parsed from the first ~5 KB of body — graceful fallback if any field can't be detected). Idempotent: skip when `.md` exists & non-empty; `--force` re-converts. MDs mirror to S3 with `Content-Type: text/markdown` (same bucket, flat key). The DB is *not* extended for MD state — filesystem + `head_object` cover idempotency.

Conversion is parallelized via `ThreadPoolExecutor(-j N)`. PyMuPDF releases the GIL during PDF parsing so threads scale on multi-core. `cli.py` sets `OMP_NUM_THREADS=1` + `ORT_INTRA_OP_NUM_THREADS=1` via `setdefault` *before* importing `pymupdf4llm`, otherwise the layout model's ORT session auto-spawns its own intra-op pool that fights the outer workers for cores (8 PDFs measured: 110 s default → 28.7 s with the env vars + `-j 8`, 3.8× speedup). Events fire in completion order (not input order); `on_event` runs in the calling thread to keep S3 uploads serialized without locks. Throughput plateaus around `-j 8` on the 20-core test box. **No GPU path** — `pymupdf-layout` hardcodes one of its two ORT sessions to `CPUExecutionProvider`, so `onnxruntime-gpu` would only accelerate half the work; not worth the ~3 GB install.

`convert` shows progress identically to `fetch`: a live `rich` bar on stderr when isatty (PDF count, %, elapsed, ETA, running converted/skipped/errors + s3 totals), or a `progress: …` heartbeat every `_CONVERT_HEARTBEAT_EVERY` PDFs in pipes. The reporter is a context-manager (`with _ConvertProgressReporter(total, counters) as report`); `report.print(line, err=…)` writes lines above the bar (or to stdout/stderr in heartbeat mode), `report.advance()` ticks the bar and emits heartbeats. Counters are a single mutable dict shared by `cmd_convert`'s closure and the reporter — bumping a counter inside `on_event` is reflected in the next bar redraw. Ctrl+C: `convert_all`'s parallel branch wraps `as_completed` in `try/except KeyboardInterrupt` and calls `ex.shutdown(wait=True, cancel_futures=True)` before re-raising; `cmd_convert` catches the KbdInt and prints `interrupted: converted=… skipped=… errors=… | s3 …` (using the live counters) before returning 130. The `wait=True` is intentional — letting in-flight workers finish their current PDF means no half-written `.md` files and no atexit thread-join race that would otherwise dump a traceback.

`classify` is the type detector — step 1 of the extraction pipeline (see `docs/extraction-schema.md` build order). Pure-regex sweep over each MD's filename suffix + first 10 KB of body, scoring each of the six document types (`plenary_stenogram | plenary_joint_session | committee_synthesis | report_facsimile | question_register | other`). Output is one JSONL row per file to stdout with `{file, top_type, top_score, second_type, second_score, ambiguous, all_scores, matched_signals}`; summary counts to stderr. `--outliers` filters to docs that landed in `other` or are flagged ambiguous — the latter excludes structural co-evidence (joint_session ⊃ stenogram, report received in joint session) via `_COMPATIBLE_RUNNERS_UP`. On the 2300+ doc corpus the sweep runs in <2 s and (as of v1.3.0 schema rules) lands every document into a typed bucket with zero residual outliers.

(A `.ruff_cache` is present; no committed config, so ruff defaults apply.)

**After each code change, run `uv run ruff format` and `uv run ruff check --fix` before reporting the task complete.**

**Tests are mandatory for every new feature, in the same change. Non-negotiable.** A "feature" here means any new function, CLI flag, regex, parser branch, DB column, state-transition, or behavior change. The bar is:

- **New pure function** → at least one happy-path test plus one rejection / edge case (empty input, malformed input, boundary).
- **New CLI flag** → one test that exercises the flag's effect (parser-level via `_build_parser` or behavior-level via the helper it toggles). Don't test argparse itself; test the branch it switches on.
- **New regex / parser branch** → one positive sample, one negative sample, and one for any quirk you encoded (e.g. legacy glyph mapping, suffix tolerance).
- **New DB state transition** → one test that drives the transition and asserts the row, plus one for the idempotency / re-entry case if the transition is callable twice.
- **New scraper / converter behavior** → drive `scrape_day` or `convert_all` end-to-end with `httpx.MockTransport` / `monkeypatch.setattr(converter, "convert_pdf", ...)` so the orchestration layer is covered, not just the leaf function.

Tests live in `tests/` mirroring `src/monitorul_ii/` (`test_<module>.py`). `tests/conftest.py` exposes a `db` fixture (`DB(tmp_path/"audit.db")`). **Do not** add tests that touch the network, real S3, or real PDFs — every external boundary has a stub already; use it. **Run `uv run pytest` before reporting the task complete; the suite must be green.**

**If a code change breaks tests, fix the tests in the same change — don't leave a red suite.** When tests fail because the production code's contract changed (renamed paths, refactored APIs, removed helpers), update the tests to match the new contract; don't revert the code or skip the tests. Only treat a test failure as a real bug to fix in production code when the test is asserting still-intended behavior.

**Document every new feature, in the same change. Non-negotiable.** A feature is shipped only when *all three* docs reflect it:

- `README.md` — user-facing prose. Every CLI flag must be described in prose (not just shown in an example), including its semantics, default, and any non-obvious interaction with other flags or env vars. If you added a flag, search README to confirm its name appears in a sentence, not only inside a code fence.
- `CLAUDE.md` (this file) — agent-facing scannable summary. The "Commands" section's CLI signature must include the new flag; the prose paragraph for the relevant subcommand must mention any new behavior, default, or dep.
- `docs/architecture.md` — deep mechanics: *why* the flag exists, what tradeoffs it encodes, what was tried and rejected, measured numbers if relevant.

"Meaningful" = a new CLI flag, a new behavior or default, a new module, a new external dependency, a new env-var the CLI reads or sets, a new failure mode, or anything a future user/agent would otherwise have to read the diff to discover. Touching `pyproject.toml` `[project.dependencies]` always counts.

**Keep CLAUDE.md scannable** — push detailed mechanics into `docs/architecture.md` and link from here. CLAUDE.md is the index; architecture.md is the manual.

**Self-check before reporting the task complete:**

1. `uv run pytest` is green and exercises the new code path (don't trust pre-existing coverage).
2. `uv run ruff format` and `uv run ruff check --fix` clean.
3. For each new flag, `grep -n '<flag-name>' README.md CLAUDE.md docs/architecture.md` shows hits in all three. If any miss, write the missing doc *now*, not "as a follow-up".

## uv-on-snap quirk

`uv` installed via snap buffers stdout when there is no tty, so `uv run <cmd>` may appear silent in non-interactive shells. Pipe through `cat` (e.g. `uv run monitorul-ii --help | cat`) or invoke the venv binary directly (`.venv/bin/monitorul-ii ...`) when you need to see output.

## Release flow

Releases are automated via [release-please](https://github.com/googleapis/release-please) (`.github/workflows/release-please.yml`), triggered on every push to `main`.

- Tags include the component name (`include-component-in-tag: true`)
- Pre-1.0: minor bumps for features (`bump-minor-pre-major: true`)
- Version source of truth for release-please is `.release-please-manifest.json`, **not** `pyproject.toml` — keep them in sync if bumping manually
- Commits must follow Conventional Commits for release-please to pick them up (`feat:`, `fix:`, `chore:`, etc.)

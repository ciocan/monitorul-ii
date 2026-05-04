# monitorul-ii

Scrape [Monitorul Oficial al României](https://monitoruloficial.ro/e-monitor/) Partea a II-a (and other parts), save the PDFs locally, and convert them to extraction-friendly markdown.

## Install

```sh
uv sync
```

## Usage

Two subcommands: `fetch` (download PDFs) and `convert` (PDF → markdown).

### `fetch`

```sh
# single day
uv run monitorul-ii fetch 2026-04-29

# date range, custom output dir
uv run monitorul-ii fetch 2026-04-01 --until 2026-04-30 --out ./pdfs

# multi-year backfill, newest→oldest so a partial run leaves you with the recent stretch
uv run monitorul-ii fetch 2000-01-01 --until 2026-05-04 --reverse

# different Partea (default is II)
uv run monitorul-ii fetch 2026-04-29 --part IV

# bypass the proxy
uv run monitorul-ii fetch 2026-04-29 --no-proxy

# bypass the S3 mirror even when env vars are set
uv run monitorul-ii fetch 2026-04-29 --no-upload

# re-fetch every day's index regardless of DB cache (paranoid mode)
uv run monitorul-ii fetch 2026-04-01 --until 2026-04-30 --force

# pace requests — seconds between successful PDF downloads (default 0.5,
# never applied before the first download or on skips)
uv run monitorul-ii fetch 2026-04-29 --delay 1.0
```

PDFs land in `<out>/<YYYY-MM-DD>_MO-P<part>-<num>-<year>.pdf`. The date is baked into the filename so everything sorts chronologically. Re-runs skip files already on disk; in-flight downloads write to a sibling `.part` file and are renamed atomically only after the body fully streams, so an interrupt or crash never leaves a truncated PDF that future runs would mistake for complete.

Each per-request fetch retries up to 3 times with `1s → 2s → 4s` backoff for transient failures (5xx, 429, transport errors). 4xx-not-429, content-type mismatches, and parse errors fail fast with no retry. After exhaustion the issue is marked `failed` in the DB and auto-retried on the next run.

### `convert`

```sh
# convert every PDF in a directory (skips files that already have a .md sibling)
uv run monitorul-ii convert pdfs/

# one or more specific files
uv run monitorul-ii convert pdfs/2026-04-29_MO-PII-47-2026.pdf

# shell globs work — the date is in the filename
uv run monitorul-ii convert pdfs/2026-04*.pdf

# re-convert files that already have a .md
uv run monitorul-ii convert pdfs/ --force

# skip the S3 mirror
uv run monitorul-ii convert pdfs/ --no-upload

# control conversion parallelism — default is CPU count; set 1 for strictly sequential
uv run monitorul-ii convert pdfs/ -j 4
```

Each `<basename>.pdf` produces `<basename>.md` next to it. The MD opens with a YAML frontmatter block (issue, year, part, published, plus best-effort `chamber`, `session`, `session_date`, `legislature` parsed from the first page), followed by the cleaned body text. Per-page running headers, page numbers, and image placeholders are stripped; soft line breaks are re-flowed; hyphenated word breaks are joined.

`-j N` (or `--workers N`) controls conversion parallelism — default is `os.cpu_count()`; set `-j 1` for strictly sequential. Throughput plateaus around `-j 8` on a 20-core box because the layout model is small per-PDF and past that you mostly add scheduling overhead. The CLI also forces `OMP_NUM_THREADS=1` / `ORT_INTRA_OP_NUM_THREADS=1` at startup so the outer worker pool doesn't compete with onnxruntime's auto-threading (3.8× speedup vs the unfixed defaults). Export those env vars yourself to override.

When the S3 vars are set, MDs mirror to the same bucket alongside the PDFs (flat layout, `Content-Type: text/markdown`). Idempotent in the same way as `fetch`: skip if the local `.md` exists, `head_object` before each upload.

## Progress and interrupts

Both subcommands show a live [`rich`](https://github.com/Textualize/rich) progress bar on stderr when stderr is a terminal, and fall back to a periodic plain-text heartbeat in pipes/CI/cron.

- `fetch` — bar tracks days completed; counters show found / downloaded with cumulative MB / failed, plus S3 uploaded / in-bucket / errors. Per-issue events (`ok`, `skip`, `s3+`, `s3=`, errors) and per-day summary lines scroll above the bar without breaking it. Heartbeat fires every 100 days in pipe mode.
- `convert` — bar tracks PDFs completed; counters show converted / skipped / errors, plus S3 uploaded / in-bucket / errors when uploading. Per-PDF event lines scroll above the bar. Heartbeat fires every 50 PDFs in pipe mode.

Stdout (the final summary line) is unaffected by the tty check, so `monitorul-ii ... > log.txt` keeps a clean machine-readable record while you watch the bar interactively.

`Ctrl+C` prints a final `interrupted: ...` summary line with the totals so far and exits **130** — no traceback, no atexit thread-join race. For `fetch`, the DB-backed resume gate means the next run picks up exactly where you stopped. For `convert`, in-flight worker threads finish their current PDF (a few seconds) before the process exits, so partially-written output never lands on disk; queued-but-not-started PDFs are cancelled cleanly. Re-running picks up at the next missing `.md`.

## Resume / SQLite audit log

A SQLite database at `data/monitorul.db` (override with `--db PATH`, disable with `--no-db`) records every day's index fetch and every PDF's lifecycle (discovered → downloaded → uploaded). Two tables: `days` and `issues`. See [`docs/architecture.md`](docs/architecture.md) for the schema.

The DB makes resumes cheap. Re-running the same date range:

- skips the index POST for any day already marked `ok` and strictly in the past — *including weekends and holidays that had zero Partea II issues*. This is the killer feature for multi-year backfills: a 26-year resume ticks through ~9,600 days at zero network cost up to the first gap.
- always re-fetches today (publications can land in batches).
- always re-attempts days marked `failed` and any per-issue rows still in `pending`/`failed`.

Override knobs:

- `--force` — re-fetch every day's index, ignore DB.
- `--rescrape-recent N` — re-fetch the last N days regardless of status (default `0`).

Per-PDF skip is still gated on the file existing on disk (and on the bucket via `head_object`). The DB is authoritative for "did we already crawl this day's index"; the filesystem is authoritative for "is the PDF actually here." Each layer owns what it can answer cheaply and correctly.

Hashes (`sha256`), file sizes, S3 ETags, and ISO8601 UTC timestamps land in the DB so the audit log is complete. Existing PDFs from before the DB existed are imported lazily on contact (re-scrape sees the file on disk, hashes it once, records `status='downloaded'`).

## Proxy

Set `PROXY_URL` in `.env` (see `.env.example`) to route all monitoruloficial.ro traffic — both the index endpoint and PDF downloads — through an HTTP/HTTPS proxy:

```
PROXY_URL=http://brd-customer-XXX-zone-YYY:PASSWORD@brd.superproxy.io:33335
```

`--proxy URL` on the CLI overrides whatever is in the env. `--no-proxy` bypasses both.

## S3 / R2 mirror

When `S3_ENDPOINT`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, and `S3_BUCKET` are set, every successfully-downloaded PDF is also uploaded to the bucket. Cloudflare R2 is the intended target — point `S3_ENDPOINT` at the R2 endpoint URL and the rest is plain S3 SigV4:

```
S3_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_REGION=auto
S3_BUCKET=monitorul-ii
```

Object keys mirror the local filename (flat layout). Upload is idempotent (`HEAD` first, `PUT` only when missing), so re-runs are cheap. `--bucket NAME` overrides `S3_BUCKET`; `--no-upload` disables the mirror entirely. Startup runs a `head_bucket` check and aborts with exit 2 if the bucket is unreachable or credentials are wrong.

## How it works

The site exposes one undocumented AJAX endpoint that returns the day's index:

- `POST https://monitoruloficial.ro/ramo_customs/emonitor/get_mo.php` with body `today=YYYY-MM-DD` returns an HTML fragment containing `<a href="/Monitorul-Oficial--P<part>--<num>--<year>.html">` links per Partea.
- Following any of those `.html` URLs returns the PDF binary directly (`Content-Type: application/pdf`).

Issue numbers can have suffixes (`358Bis`, `12c`). Empty days (weekends, holidays) return zero issues for Partea II.

## Development

Layout:

| File | Role |
|---|---|
| `src/monitorul_ii/scraper.py` | Pure functions: `fetch_index`, `parse_issues`, `download_pdf`, `scrape_day`, `_with_retry`. No CLI concerns. |
| `src/monitorul_ii/converter.py` | Pure functions: `convert_pdf`, `convert_all`, `clean_markdown`, `enrich_meta`. Wraps `pymupdf4llm`. |
| `src/monitorul_ii/uploader.py` | `S3Config.from_env()` + `Uploader` (boto3, S3-compatible incl. R2). |
| `src/monitorul_ii/db.py` | `DB` — thin SQLite wrapper over `days` + `issues` tables; owns the resume-gate logic. |
| `src/monitorul_ii/cli.py` | argparse, exit codes, the live progress bar / heartbeat, the upload→DB write path. |

`docs/architecture.md` is the deep dive — the site contract, link parser, retry policy, schema, resume contract, and the markdown-cleanup pipeline.

### Setup

```sh
uv sync                          # creates .venv and installs all deps
cp .env.example .env             # fill in PROXY_URL and S3_* vars as needed
```

The CLI is the entry point in `pyproject.toml`. You can also invoke it as a module:

```sh
uv run python -m monitorul_ii fetch 2026-04-29
```

### Lint and format

```sh
uv run ruff check                # lint (--fix to auto-fix)
uv run ruff format               # format
```

No committed `ruff` config — defaults apply. Run both before committing.

### Tests

```sh
uv run pytest
```

Tests live in `tests/` and mirror `src/monitorul_ii/` (`test_<module>.py`). The suite is fast (~0.4 s) and offline: the scraper is driven through `httpx.MockTransport`, `convert_pdf` is monkeypatched away from `pymupdf4llm`, the SQLite audit log runs in `tmp_path`, and S3 calls are unit-tested via `S3Config.from_env` only — no real bucket touched.

New features must ship with tests. The contract is:

- New pure function → happy-path + rejection / edge case.
- New CLI flag → one test exercising the branch it toggles.
- New regex / parser branch → one positive, one negative, one quirk sample.
- New DB state transition → drive the transition and assert the row, plus an idempotency test if the transition is re-entrant.
- New scraper / converter behavior → drive `scrape_day` / `convert_all` end-to-end, not just the leaf.

### `uv` on snap quirk

`uv` installed via snap buffers stdout when there is no tty, so `uv run <cmd>` may appear silent in non-interactive shells (including hooks and scripts). Pipe through `cat` (e.g. `uv run monitorul-ii --help | cat`) or invoke the venv binary directly (`.venv/bin/monitorul-ii ...`) when you need to see output.

### Releases

Automated via [release-please](https://github.com/googleapis/release-please), triggered on every push to `main` (`.github/workflows/release-please.yml`). The workflow opens a release PR that bumps the version and updates the changelog; merging it tags and publishes.

- Commits **must** follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `chore:`, `docs:`, …) for release-please to pick them up. Anything outside that grammar is ignored — no version bump, no changelog entry.
- Pre-1.0: `feat:` bumps the minor; `fix:` bumps the patch.
- Tags include the component name (`monitorul-ii-vX.Y.Z`).
- The version source of truth is `.release-please-manifest.json`, **not** `pyproject.toml` — keep them in sync if you ever bump by hand.

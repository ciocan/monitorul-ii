# monitorul-ii

Scrape [Monitorul Oficial al României](https://monitoruloficial.ro/e-monitor/) Partea a II-a (and other parts) and save the PDFs locally for a given date or date range.

## Install

```sh
uv sync
```

## Usage

```sh
# single day
uv run monitorul-ii 2026-04-29

# date range, custom output dir
uv run monitorul-ii 2026-04-01 --until 2026-04-30 --out ./pdfs

# multi-year backfill, newest→oldest so a partial run leaves you with the recent stretch
uv run monitorul-ii 2000-01-01 --until 2026-05-04 --reverse

# different Partea (default is II)
uv run monitorul-ii 2026-04-29 --part IV

# bypass the proxy
uv run monitorul-ii 2026-04-29 --no-proxy

# bypass the S3 mirror even when env vars are set
uv run monitorul-ii 2026-04-29 --no-upload

# re-fetch every day's index regardless of DB cache (paranoid mode)
uv run monitorul-ii 2026-04-01 --until 2026-04-30 --force
```

PDFs land in `<out>/<YYYY-MM-DD>_MO-P<part>-<num>-<year>.pdf`. The date is baked into the filename so everything sorts chronologically. Re-runs skip files already on disk.

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

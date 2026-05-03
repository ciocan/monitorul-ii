# Architecture

Deep dives. CLAUDE.md has the scannable summary; this file is the reference for changes that touch the scrape mechanism, the parser, or the on-disk layout.

## End-to-end flow

```
CLI (cli.py)
  ├─ load .env (python-dotenv)
  ├─ parse argv (date, --until, --out, --part, --delay, --proxy/--no-proxy,
  │              --bucket/--no-upload, --db/--no-db, --reverse, --force,
  │              --rescrape-recent)
  ├─ resolve proxy:    --proxy > PROXY_URL env > none      (--no-proxy short-circuits)
  ├─ resolve uploader: S3Config.from_env() if all S3_* set; head_bucket fail-fast
  │                    (--no-upload short-circuits, --bucket overrides)
  ├─ open DB:          DB(args.db) creates parent dir, runs CREATE TABLE IF NOT EXISTS,
  │                    sets PRAGMA journal_mode=WAL + foreign_keys=ON
  │                    (--no-db short-circuits — DB-less mode)
  ├─ open httpx.Client with headers preset (UA + Referer) and optional proxy
  └─ for each day in daterange([date..until], reverse=args.reverse):
       scrape_day(client, day, out_dir, part, delay, on_event,
                  db, today, force, rescrape_recent_days)
         ├─ db.should_fetch_index(day, today, …)
         │     False → reconstruct non-terminal Issues from DB (skip the index POST)
         │     True  → continue with fetch
         ├─ _with_retry(fetch_index)        # POST, 3 attempts on transient errors
         ├─ parse_issues(html, part)
         ├─ db.record_day_ok(day, count)    # or record_day_failed on retry exhaustion
         ├─ for each Issue: db.record_issue_discovered(...)   (UPSERT keeps prior status)
         └─ for each Issue:
              ├─ if target exists & non-empty:
              │     emit "skip"
              │     if DB has no sha for this issue → hash file, record_issue_downloaded
              │       (bump_attempts=False — lazy import path)
              └─ else _with_retry(download_pdf) → record_issue_downloaded
                                              or record_issue_failed (after 3 attempts)
       (CLI's on_event closure: after "skip"/"download",
        uploader.upload_if_missing(path) → db.record_issue_uploaded(etag)
        DB short-circuits the head_object when issue.status is already 'uploaded')

       per-day summary line printed unless the day was a pure DB-cached no-op
       heartbeat line every 100 days with elapsed/ETA
```

The split is deliberate: `scraper.py` has no I/O of its own beyond httpx + the filesystem + sqlite3 (via the DB handle), and emits structured `FileEventPayload` records through `on_event`. `db.py` is a thin SQLite wrapper — schema bootstrap, named methods, no ORM, no migration framework. `cli.py` owns argv parsing, stdout/stderr formatting, exit codes, and the upload→DB write path. Tests can drive `scrape_day` directly with a captured-events callback and an in-memory DB.

## Site contract (reverse-engineered)

There is no documented API. Everything below was discovered by inspecting the network tab on `https://monitoruloficial.ro/e-monitor/`.

### Index endpoint

```
POST https://monitoruloficial.ro/ramo_customs/emonitor/get_mo.php
Headers:
  Referer: https://monitoruloficial.ro/e-monitor/   (required — without it the server returns the homepage)
  User-Agent: <any non-empty string>
  X-Requested-With: XMLHttpRequest                  (sent by the page; not strictly required but harmless)
Body (form-encoded):
  today=YYYY-MM-DD
  rand=<float>                                       (cache-buster the page generates; we send a constant)
```

Response is `text/html; charset=UTF-8` — a fragment (no `<html>` wrapper) containing one `<div class="card-body">` per Partea published that day. Each card has a breadcrumb header naming the Partea and a series of `<a>` buttons, one per issue.

Example for `today=2026-04-29`:

```html
<div class="card-body">
  <ol class="breadcrumb">…Partea a II-a…</ol>
  <a class="btn btn-outline-primary"
     href="/Monitorul-Oficial--PII--47--2026.html"
     target="_blank">47</a>
  <a class="btn btn-outline-primary"
     href="/Monitorul-Oficial--PII--48--2026.html"
     target="_blank">48</a>
</div>
```

If a Partea has no issues that day, its card is omitted entirely (not present-but-empty). An empty index — i.e. weekends, public holidays — is a normal response with no error.

### PDF "landing pages"

```
GET https://monitoruloficial.ro/Monitorul-Oficial--P<part>--<num>--<year>.html
```

Response headers:

```
Content-Type: application/pdf
Content-Disposition: inline; filename="Monitorul Oficial Partea a <part> nr. <num>.pdf"
```

The body **is** the PDF binary. The `.html` extension is misleading — there is no HTML stage. We stream the body directly to disk; no second hop.

## Link parser

```python
_LINK_RE = re.compile(
    r'href="(?P<href>/Monitorul-Oficial--P(?P<part>[IVXM]+)--(?P<num>[0-9A-Za-z]+)--(?P<year>\d{4})\.html)"'
)
```

Why regex and not BeautifulSoup: the fragment is mechanically generated and the link form is rigid. The grammar fits in three named groups, and adding a parser dependency for a 100-byte regex would be net-negative. If the site ever changes shape (e.g. wraps links in JSON), swap to a real parser then.

Edge cases the regex must handle:

- **Roman-numeral parts**: `PI`, `PII`, `PIII`, `PIV`, `PVI`, `PVII`, `PIM` (Partea I Maghiară). The `[IVXM]+` charclass matches all of these. `M` is included for safety even though we've never seen it.
- **Issue-number suffixes**: real responses include `47`, `12c`, `358Bis`. `[0-9A-Za-z]+` accepts the lot. The `c` suffix appears on certain Partea II issues (suspected: Camera Deputaților vs Senat sittings — not load-bearing, just preserve as-is).
- **Year is always 4 digits** in observed traffic.

`parse_issues` deduplicates on `(part, number, year)` because the same `<a>` sometimes appears more than once in a card (no observed reason; just defensive).

`Issue.filename(pub_date)` produces `<YYYY-MM-DD>_MO-P<part>-<num>-<year>.pdf`. The publication date prefixes the name so a single flat directory sorts chronologically in `ls`. The trailing `<year>` is the issue's year (almost always equal to the publication year, but kept distinct in case the site ever indexes a late-published issue under its own year).

## Download mechanism

`download_pdf` uses `httpx.Client.stream("GET", …)` and writes 64 KiB chunks. It returns `(size_bytes, sha256_hex)` — the hash is fed by the same chunks that go to disk, so there's no second pass over the file. Two safety properties matter:

1. **Atomic write.** The body is streamed to `<target>.part`, then `Path.replace`d to `<target>`. A crash mid-download leaves a `.part` file on disk; the `target` itself never exists in a half-written state. Idempotency uses `target.exists() and size > 0`, so a stranded `.part` doesn't fool the skip check.
2. **Content-type guard.** If the response isn't `application/pdf` we raise instead of writing. This catches the failure mode where the server returns the homepage (e.g. when the Referer header is dropped or the URL is mistyped) — without the guard we'd silently save a 400 KB HTML file with a `.pdf` extension.

### Retry

`_with_retry(fn, attempts=3, backoff=(1, 2, 4))` wraps both `fetch_index` and `download_pdf`. The retry policy is asymmetric:

- **Retry**: anything for which `_is_transient(exc)` is true — `httpx.HTTPStatusError` with status `>=500` or `429`, plus the rest of the `httpx.HTTPError` family (transport, timeout, remote-protocol, etc.).
- **Don't retry**: 4xx other than 429 (a real "this URL is wrong"), parse errors (zero-issue HTML — handled by returning an empty list, never raises), and content-type mismatches (`RuntimeError` raised by `download_pdf`).

After 3 attempts the last exception bubbles. `scrape_day` catches it and:

- For an index-fetch failure, writes `days.status='failed', last_error=…` and returns a `DayResult` with `found=0, errors=[…]`.
- For a per-PDF failure, writes `issues.status='failed'`, increments `attempts`, sets `last_error`, and proceeds to the next issue. The day's index fetch is unaffected.

Re-running the command auto-retries every `failed` row (no `failed_permanent` distinction). If a row genuinely never works (e.g. a permanent 404 on a parsed link), it stays `failed` forever and gets re-attempted each run; the user notices via `last_error` and can SQL-quarantine if it gets noisy.

## Idempotency

Two layers of state, each authoritative for what it can answer cheaply:

- **Filesystem (+ S3 bucket)**: "is this PDF already here?" The skip check is filename-based — `target.exists() and target.stat().st_size > 0`. Per-PDF gate.
- **SQLite**: "did we already crawl this day's index?" The DB row in `days` records whether `fetch_index` succeeded for a calendar day, *including* days with zero Partea II issues. Per-day gate.

The DB is **not** the source of truth for whether a file exists on disk. If a PDF is deleted and the DB still says `status='downloaded'`, we'll trust the DB on the resume path (and miss re-downloading the file). To force re-download, either pass `--force` or `rm` the corresponding row. This is the explicit tradeoff for a 26-year backfill being free to resume — the DB doesn't run an `os.path.exists()` check on every issue every run.

Implications of the filesystem skip:

- **Renaming the file off disk** (e.g. moving it elsewhere) makes the scraper re-download it on the next run when the day's index is re-fetched. There is no FS-watching layer.
- **A zero-byte file is not treated as downloaded** — it'll be overwritten. This handles the rare case where someone `touch`ed the path or a previous run was killed before any chunks landed.
- **Filename includes the publication date**, so the same issue number on a different date (which shouldn't happen, but) would not collide.

## SQLite audit log

### Schema

Two tables. ISO8601-UTC text for timestamps (easier to debug with `sqlite3` shell than unix epochs). All status enums stored as plain text — no CHECK constraints, the application is the only writer.

```sql
CREATE TABLE days (
    date          TEXT PRIMARY KEY,           -- 'YYYY-MM-DD'
    status        TEXT NOT NULL,              -- 'ok' | 'failed'
    issues_found  INTEGER NOT NULL DEFAULT 0,
    attempted_at  TEXT NOT NULL,
    completed_at  TEXT,                       -- NULL while 'failed'
    last_error    TEXT
);

CREATE TABLE issues (
    date          TEXT NOT NULL REFERENCES days(date),
    part          TEXT NOT NULL,
    number        TEXT NOT NULL,              -- '47', '12c', '358Bis'
    year          INTEGER NOT NULL,
    url           TEXT NOT NULL,
    filename      TEXT NOT NULL,
    status        TEXT NOT NULL,              -- 'pending' | 'downloaded' | 'uploaded' | 'failed'
    size_bytes    INTEGER,
    sha256        TEXT,
    s3_etag       TEXT,
    downloaded_at TEXT,
    uploaded_at   TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    PRIMARY KEY (date, part, number, year)
);

CREATE INDEX idx_issues_status ON issues(status);
```

The `days` row is what makes a 26-year resume cheap: weekends and empty days get `status='ok', issues_found=0` and never need another index POST. `issues_found` lets you query "which days had zero Partea II issues?" without scanning the issues table.

### Resume contract

`db.should_fetch_index(day, today, force, rescrape_recent_days)` returns `True` (= POST the index) when any of the following is true:

1. `force` is set.
2. `day >= today` — today is always re-fetched in case publications appeared since the last run.
3. `day >= today - rescrape_recent_days` — the recent-window override (default 0).
4. The `days` row is missing.
5. The `days` row exists but `status != 'ok'`.

Otherwise return `False` and `scrape_day` reconstructs `Issue` objects from the existing `issues` rows for that day, filtered to non-terminal statuses (`pending`, `failed`). If all rows are already `downloaded`/`uploaded`, the issue list is empty and `scrape_day` is a true no-op for that day — no network, no filesystem reads, no per-day stdout line. Heartbeats every 100 days carry the progress.

### Status state machines

`days.status`: `ok` | `failed`. Set after the index POST resolves. There's no separate `pending` because we don't write the row until the POST returns; if the process dies mid-POST, no row exists and the day looks fresh next run.

`issues.status`:

```
        record_issue_discovered
                |
                v
            pending
            /    \
   download/    \ download
    fail         success
      |           |
      v           v
    failed   downloaded
      ^           |
      |           | upload_if_missing
   (auto-retry)   v
                uploaded
```

`failed` is auto-retried on subsequent runs. `attempts` increments on every terminal transition out of `pending` (download success or failure) — but **not** on lazy-import existing-file detection (`bump_attempts=False`). So `attempts=0, status='downloaded'` reliably means "this PDF was on disk before the DB knew about it" rather than "we got it on the first try."

### Lazy import of pre-existing PDFs

When `scrape_day` finds a target file already on disk, it emits the usual `"skip"` event but also: if the DB has no `sha256` for that issue, hash the file (single-pass 64 KiB reads), `os.stat().st_size`, mtime → `downloaded_at`, write a `record_issue_downloaded(..., bump_attempts=False)`. This silently absorbs PDFs that predate the DB without a separate import command and without an inverse-filename parser.

### Concurrency / writes

Single-writer process. `PRAGMA journal_mode=WAL` + `isolation_level=None` (autocommit per `execute`) — WAL gives us fast many-small-commits over a long run without sacrificing durability for any single write. There is no transaction batching across days; each `record_*` call commits immediately. With ~10k–20k writes over a multi-hour backfill this is fine; if it ever bottlenecks, batch one transaction per day.

The DB connection is held open for the duration of the run. `DB.close()` runs in a `finally` block in `cli.main` so a Ctrl-C still flushes WAL.

### What is *not* in the DB

- **Per-run / provenance rows.** The `attempted_at` timestamps are enough to reconstruct when each day was crawled.
- **A `failed_permanent` status.** All failures are equally retryable; if you don't want to retry a row, SQL it.
- **An `attempts` cap that auto-quarantines after N tries.** `attempts` is informational only.
- **Migrations framework.** The `_DDL` block uses `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`. If the schema ever needs to evolve, add an `ALTER TABLE` ladder keyed off `PRAGMA user_version`.

## Progress events

`scrape_day` accepts an `on_event(payload: FileEventPayload)` callback. The payload carries `kind` (`"skip"` | `"download"` | `"error"`), the `Issue`, the publication `day`, the local `path`, and an optional `detail` string for errors.

The `Issue` and `day` on the payload let `cli.py` write upload state back to the DB (`record_issue_uploaded(..., s3_etag=...)`) without parsing the filename — and library users can build their own sinks (e.g. a metadata pipeline) without re-deriving identity from the path.

`cli.py` prints `skip` / `ok` / `ERR` lines per file plus a per-day summary. Library users (e.g. an importer pipeline) can ignore the callback and just consume `DayResult`.

## Proxy support

Monitorul Oficial sometimes geo-blocks or rate-limits direct traffic. The scraper supports routing through any HTTP/HTTPS proxy:

- `_client(proxy=…)` passes the URL straight to `httpx.Client(proxy=…)`. Both the index POST and the PDF GETs reuse the same client, so they share the same proxy connection.
- The CLI resolves the proxy URL with this precedence: `--proxy` flag → `PROXY_URL` env (loaded from `.env` via `python-dotenv`) → none. `--no-proxy` short-circuits everything.
- Logs print the proxy URL with the password masked (`user:***@host:port`); the raw `.env` value never hits stdout/stderr.
- TLS verification is left at httpx default (system trust store). If a proxy MITMs HTTPS with its own CA, install the CA into the system store rather than disabling verification.

## S3 / R2 mirror

When `S3_ENDPOINT`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, and `S3_BUCKET` are all set, every PDF the scraper touches (whether freshly downloaded or already on disk) is also pushed to S3. R2 is the intended target — it's an S3-compatible service, so boto3 with `endpoint_url=<R2 URL>` and SigV4 just works.

### Module split

`uploader.py` owns all S3 concerns. `scraper.py` knows nothing about S3 — the `on_event` callback in `cli.py` is what wires the two together. This means:

- A library user can call `scrape_day` directly without dragging in boto3.
- Tests for the parser/scraper don't touch the network or boto3.
- Adding a second sink (e.g. archive.org, internal API) is a CLI-layer change, not a scraper change.

### Idempotency

`Uploader.upload_if_missing(path, key=None)`:

1. `head_object(Bucket, Key)` → if 200, return `False` (no upload, file already there).
2. On `404` / `NoSuchKey` / `NotFound`, `upload_file(...)` with `ContentType=application/pdf` and return `True`.
3. Other errors propagate.

The default `key` is `path.name`, mirroring the local flat layout (e.g. `2026-04-29_MO-PII-47-2026.pdf`). The date prefix gives chronological order in any S3 listing tool, so we don't bother with year/month prefixes.

The `head_object`-per-file policy is one extra HTTP RTT per PDF on re-runs. For ranges in the hundreds it's fine; if we ever scrape years at a time, switch to a one-shot `ListObjectsV2` to build an in-memory key set up front.

### Fail-fast

CLI startup calls `uploader.validate()` which does `head_bucket`. If the bucket is unreachable (wrong endpoint, missing creds, typo'd name), the run aborts with exit 2 *before* any scraping starts. This avoids the failure mode where you spend ten minutes downloading and then discover every upload silently failed.

### R2 specifics

- `endpoint_url`: `https://<account-id>.r2.cloudflarestorage.com`
- `region_name`: R2 ignores this but boto3 requires a value — we default to `auto` (matches Cloudflare's docs).
- `signature_version="s3v4"` is set explicitly because some boto3 defaults can fall back to v2 in odd configurations; v4 is the only thing R2 accepts.
- No `ChecksumAlgorithm` or `ServerSideEncryption` extras — R2 is happy with the bare upload.

## Rate-limiting

`scrape_day` sleeps `delay` seconds (default 0.5) **between successful downloads**, not before the first one and not when a file is skipped. This keeps re-runs over already-downloaded ranges fast while staying polite for fresh fetches.

The site has no `robots.txt` (the path returns a generic challenge page) and no published rate limit. 0.5 s/request is a conservative default for a government site; tune via `--delay 0` if scraping a small range you control.

## What is *not* here

- **No HTML parser dependency.** Regex is sufficient given the fragment shape; revisit if the site ever returns a richer payload.
- **No async / concurrency.** Sequential through the proxy — politeness against the site, simpler SQLite write path, no `--workers` flag. The DB makes resumes free, so wall-time isn't critical. Switch to `httpx.AsyncClient` only if we ever need to fan out across many days at once.
- **No tests.** The codebase has none today. `scrape_day(db=None)` keeps the pre-DB contract intact for any future test harness.
- **No `failed_permanent` status / `attempts` cap.** All failures are auto-retried on the next run. If a row never works, you'll see it in `last_error` and can SQL-quarantine.
- **No alembic / migrations framework.** Single `CREATE TABLE IF NOT EXISTS` block runs at startup; future schema changes pin an `ALTER TABLE` ladder to `PRAGMA user_version`.

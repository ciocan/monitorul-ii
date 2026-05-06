# Architecture

Deep dives. CLAUDE.md has the scannable summary; this file is the reference for changes that touch the scrape mechanism, the parser, the markdown converter, or the on-disk layout.

## End-to-end flow

`cli.py` exposes two subcommands. `fetch` is the scraper described below; `convert` is documented in [PDF → markdown conversion](#pdf--markdown-conversion). Both load `.env` and share the same S3 plumbing (`--no-upload`, `--bucket`).

### `fetch`

```
CLI (cli.py: cmd_fetch)
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
       progress: live `rich` bar on stderr when isatty, else heartbeat every 100 days
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

After 3 attempts (or zero, for non-transient errors) the last exception bubbles. `scrape_day` catches it and classifies via `_is_permanent(exc)`:

- **Permanent** = `httpx.HTTPStatusError` with status `4xx` and not `429`, or `RuntimeError` (the content-type guard in `download_pdf`). The server is telling us this URL doesn't resolve to a PDF, full stop. Recorded as `issues.status='gone'`.
- **Transient (post-retry exhaustion)** = anything else. Recorded as `issues.status='failed'`.

For an index-fetch failure, the day row goes `days.status='failed', last_error=…` regardless of classification (a future run will re-walk that day and discover whatever issues it can).

Re-running the command auto-retries `failed` issues but skips `gone` ones — `_issues_from_db` treats `gone` as terminal alongside `downloaded`/`uploaded`. This means a 26-year backfill against a site with a handful of withdrawn documents settles into a stable state: each subsequent run walks zero dead URLs. `--retry-gone` calls `db.reset_gone()` to flip every `gone` row back to `pending` if you want to verify the site has restored them. SQL-quarantine is still available for one-off cases (`UPDATE issues SET status='gone' WHERE …`).

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

Otherwise return `False` and `scrape_day` reconstructs `Issue` objects from the existing `issues` rows for that day, filtered to non-terminal statuses (`pending`, `failed`). If all rows are already `downloaded`/`uploaded`/`gone`, the issue list is empty and `scrape_day` is a true no-op for that day — no network, no filesystem reads, no per-day stdout line. The live progress bar (or 100-day heartbeat in non-tty) carries the progress.

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
- **An `attempts` cap that auto-quarantines after N tries.** `attempts` is informational only — terminal classification is shape-of-error based (`status='gone'` on permanent failures), not count-based.
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

`Uploader.upload_if_missing(path, key=None, content_type="application/pdf")`:

1. `head_object(Bucket, Key)` → if 200, return `UploadResult(uploaded=False, etag=…)` (no upload, file already there).
2. On `404` / `NoSuchKey` / `NotFound`, `upload_file(...)` with the requested `ContentType` and return `UploadResult(uploaded=True, etag=…)`.
3. Other errors propagate.

The default `key` is `path.name`, mirroring the local flat layout (e.g. `2026-04-29_MO-PII-47-2026.pdf`). The date prefix gives chronological order in any S3 listing tool, so we don't bother with year/month prefixes. `cmd_fetch` uses the default `application/pdf`; `cmd_convert` passes `text/markdown`. Both formats live side-by-side in the same bucket — the `Content-Type` distinguishes them and a suffix filter separates them in listings.

The `head_object`-per-file policy is one extra HTTP RTT per PDF on re-runs. For ranges in the hundreds it's fine; if we ever scrape years at a time, switch to a one-shot `ListObjectsV2` to build an in-memory key set up front.

### Fail-fast

CLI startup calls `uploader.validate()` which does `head_bucket`. If the bucket is unreachable (wrong endpoint, missing creds, typo'd name), the run aborts with exit 2 *before* any scraping starts. This avoids the failure mode where you spend ten minutes downloading and then discover every upload silently failed.

### R2 specifics

- `endpoint_url`: `https://<account-id>.r2.cloudflarestorage.com`
- `region_name`: R2 ignores this but boto3 requires a value — we default to `auto` (matches Cloudflare's docs).
- `signature_version="s3v4"` is set explicitly because some boto3 defaults can fall back to v2 in odd configurations; v4 is the only thing R2 accepts.
- No `ChecksumAlgorithm` or `ServerSideEncryption` extras — R2 is happy with the bare upload.

## PDF → markdown conversion

`monitorul-ii convert <paths>...` walks each path (file or directory), runs every `*.pdf` through `pymupdf4llm.to_markdown` plus an MO-specific cleanup pass, prepends a YAML frontmatter block, and writes `<basename>.md` next to the source. Idempotent: skip if the `.md` exists & non-empty; `--force` re-converts. When the S3 vars are set, MDs mirror to the same bucket (flat key, `Content-Type: text/markdown`).

```
CLI (cli.py: cmd_convert)
  ├─ load .env (python-dotenv)
  ├─ parse argv (paths..., --force, -j/--workers, --reverse, --bucket/--no-upload)
  ├─ resolve uploader: same fail-fast head_bucket as `fetch`
  ├─ collect_pdfs(paths, reverse=args.reverse)  # files + non-recursive *.pdf in dirs, dedup'd
  └─ for each pdf:
       ├─ if md exists & non-empty (and not --force) → emit "skip"
       └─ else convert_pdf(pdf, md):
            ├─ pymupdf4llm.to_markdown(pdf)
            ├─ clean_markdown(raw)        # noise stripping (see below)
            ├─ parse_filename(name)       # base IssueMeta from path
            ├─ enrich_meta(body, base)    # best-effort first-page parse
            └─ write frontmatter + body atomically (.part → .md)
       (CLI's on_event closure: after "convert"/"skip",
        uploader.upload_if_missing(md_path, content_type="text/markdown"))
```

### Module split

`converter.py` knows nothing about argv, S3, or the audit DB. The CLI wires `convert_all → on_event → uploader.upload_if_missing` together, mirroring how `fetch` wires `scrape_day → on_event → uploader`. A library user can call `convert_pdf(pdf, md)` directly with no S3 or CLI baggage.

### Engine choice — `pymupdf4llm`

PyMuPDF's MD helper is fast (~100 ms for a 12-page A4 PDF), deterministic, and free. Two alternatives were considered and rejected:

- **`marker`** (ML-based layout analyzer) — better SUMAR/table fidelity, but pulls in PyTorch + ~2 GB of model weights for a tool whose `fetch` half completes in <1 s per PDF. Hold for if/when extraction quality demands it.
- **LLM (Claude API)** — highest quality on unusual layouts, but pays per-page for content `pymupdf4llm` already extracts correctly. Better budget on the *extraction* step downstream than the *conversion* step.

The default is upgradable: swapping engines is a `convert_pdf` body change; the CLI surface and on-disk layout don't move.

### Cleanup pass

`clean_markdown(md)` runs four regex sweeps over the raw `pymupdf4llm` output. Each is intentionally narrow:

| Pattern | Removes / rewrites |
|---|---|
| `_PICTURE_RE` | `**==> picture [WxH] intentionally omitted <==**` placeholder lines (MO PDFs include seal/logo images that are pure noise in markdown). |
| `_RUNNING_HEADER_RE` | `MONITORUL OFICIAL AL ROMÂNIEI...` running header that appears once per page, including in-body. |
| `_PAGE_NUMBER_RE` | Lines containing only a 1–3 digit number (the per-page page number). |
| `_HYPHEN_BREAK_RE` | `<word>-\n<word>` → joined word (line-break hyphenation). |
| `_TRAILING_WS_RE` | Trailing spaces on lines (cosmetic, keeps diffs clean). |
| `_BLANK_LINES_RE` | 3+ consecutive newlines → 2 (single blank line). |

What the cleanup does **not** touch:

- The SUMAR (table of contents) is emitted by `pymupdf4llm` as a one-row two-column markdown table with `<br>`-separated cells. It's ugly but extractable; rewriting it into a numbered list would be Tier 3 work and is fragile across the Senate/Camera/`c`-suffix layout variants.
- Letter-spaced headings like `**PA R T E A  A  I I - A**` are preserved verbatim. The PDF source uses tracking on those characters; collapsing the spaces is heuristic-prone (you'd risk eating real spaces in adjacent prose), and the "PARTEA A II-a" identity is already in the filename + frontmatter.
- Old-encoding glyph drift (`Þ` for `Ț`, `Ã` for `Ă` in pre-2002 PDFs from CP1250→Latin-1 mistranslation) is **not** remapped. The chamber regex is widened to match the corrupted form (`[ȚTÞ]`) where it matters; the body text stays as-emitted.

### Frontmatter

`parse_filename(name)` extracts four always-present fields from the standard `<YYYY-MM-DD>_MO-P<part>-<num>-<year>.pdf` shape:

```yaml
issue: "47"
year: 2026
part: "II"
published: 2026-04-29
```

`enrich_meta(body, base)` then runs four narrow regexes over the first ~5 KB of the cleaned body to add:

- `chamber` — matches `SENATUL` / `SENATULUI` / `CAMERA DEPUTAȚILOR` / `CAMEREI DEPUTAȚILOR` (genitive forms appear in `c`-suffixed commission summaries) plus the `Þ` encoding-corrupted variant. Normalized to `"Senatul"` or `"Camera Deputaților"`.
- `session` — captures `SESIUNEA ...` up to either `(Legislatura ...` or end-of-line.
- `session_date` — parses `Ședința din ziua de <day> <ro_month> <year>` with a Romanian month-name lookup.
- `legislature` — captures the Roman numeral from `Legislatura a <X>-a`.

The enrichment is best-effort: any of the four fields is omitted (frontmatter just doesn't include the key) if its regex doesn't match. If `enrich_meta` itself raises, the converter falls back to the filename-only fields and still emits the body. This matches `fetch`'s "errors don't abort the run" stance — `c`-suffixed commission summaries, for example, never have a `session_date` because the document is structured around `Perioada` instead, and that's fine.

### Why no DB tracking for MDs

`fetch` uses the audit DB for two things `convert` doesn't need:

1. **"Did we already index this calendar day?"** — `convert` operates on local PDF files, not calendar days. There's no equivalent to "empty days are still 'ok'" for conversion.
2. **`head_object` cost amortization** — the DB's `issues.status='uploaded'` short-circuits `head_object` on the PDF mirror path. For MDs the same optimization could be added, but the call cost is one extra RTT per file on re-runs and the volume is small. Skip until it bites.

If a future feature needs per-MD state (e.g. retry budgets for conversion failures, separate `markdown_status` lifecycle), add columns to the existing `issues` table rather than a new one — every MD has a 1:1 relationship to a row already there.

### Idempotency layers

- **Filesystem**: `<basename>.md` exists and `size > 0` → skip. Same shape as PDF skip.
- **Atomic write**: body streams to `<basename>.md.part`, then `Path.replace`. A crash mid-write leaves a `.part`; the canonical path is never half-written.
- **S3**: `head_object(<basename>.md)` before each `upload_file`. Same pattern as PDFs; only the `Content-Type` differs.

### `--reverse`

`collect_pdfs(paths, reverse=True)` reverses the deduped list before returning. The PDF filenames are date-prefixed (`<YYYY-MM-DD>_MO-PII-...pdf`), so a reverse over a directory walk lands newest→oldest — same semantics as `fetch --reverse`. The reversal happens *after* dedup to keep the rest of the function's contract (input-path order preserved, dir contents sorted within each path) untouched in the default case. Useful when a Ctrl+C should leave the recent stretch already converted on a long backfill.

### Progress reporting and Ctrl+C

`_ConvertProgressReporter` mirrors `_ProgressReporter`'s shape but speaks the convert vocabulary. The CLI constructs it as a context manager around the work loop:

```python
with _ConvertProgressReporter(len(pdfs), counters) as report:
    def on_event(p):
        # bump counters[converted|skipped|errors] based on p.kind
        report.print(line)         # scrolls above the bar (or print() in pipe)
        # do upload, bump s3 counters
        report.advance()           # ticks bar + emits heartbeats in pipe mode
    try:
        convert_all(pdfs, force=…, workers=…, on_event=on_event)
    except KeyboardInterrupt:
        report.print(_convert_summary_line(counters, prefix="interrupted: "), err=True)
        return 130
print(_convert_summary_line(counters))
```

Two design points worth keeping intact:

1. **Counters are shared mutable state, not a return value.** `cmd_convert` owns `counters: dict[str,int]` and passes it to the reporter; `on_event` bumps it; the reporter reads it via `_desc()` on every bar redraw. This means a Ctrl+C-interrupted run can still print an accurate "interrupted: converted=N skipped=M …" line — the `summary` returned by `convert_all` is moot in that path. `_convert_summary_line(counters, prefix=…)` is the single formatter for both terminal-completion and Ctrl+C lines so they stay symmetrical.

2. **`convert_all` shuts the pool down with `wait=True, cancel_futures=True` on KbdInt.** Three things would go wrong with simpler approaches:
   - A bare `with ThreadPoolExecutor(...)` block catches the KbdInt at `__exit__`, which then calls `shutdown(wait=True)`. That looks fine until a *second* Ctrl+C lands on the join — Python's `threading._shutdown` atexit handler then hits a `t.join()` that gets interrupted, dumping a traceback (the original user-reported failure).
   - `shutdown(wait=False)` returns immediately but leaves running threads to be joined at interpreter shutdown by the same atexit handler — same race.
   - `shutdown(wait=True, cancel_futures=True)` is the right combination: `cancel_futures=True` drops queued work, `wait=True` blocks for in-flight threads to finish their current `convert_pdf` call (a few seconds each). After this returns, no threads remain for atexit to join. The KbdInt then propagates cleanly out to `cmd_convert`.

The cost is that Ctrl+C isn't instant — you wait up to ~5–10 s for in-flight conversions to finish. The benefit is no partial `.md` writes (atomic-rename guarantees this anyway, but the wait keeps file timing aligned with the printed counters) and a clean exit message.

### Parallelism

`convert_all(pdfs, workers=N)` fans out conversions through `concurrent.futures.ThreadPoolExecutor`. Threads (not processes) work because:

1. `PyMuPDF.Document` and the markdown writer release the GIL during the heavy parsing stage. Threading is enough for genuine CPU parallelism.
2. The worker function `_process_one(pdf, force)` is a pure value→value mapping (returns `(kind, pdf, md, detail)`). It never touches the event callback, the summary counters, or the uploader — the calling thread does, after `as_completed` yields the result. This keeps the S3 upload path single-threaded without locks and preserves the existing `on_event` contract for library users.
3. `as_completed` (rather than `executor.map`) is intentional: events arrive in finish-order, so a slow PDF doesn't stall progress reporting on faster ones. The cost is that lines aren't in input order — acceptable given each line carries the full filename.

The CLI default is `--workers / -j os.cpu_count()`.

`cli.py` sets `OMP_NUM_THREADS=1` and `ORT_INTRA_OP_NUM_THREADS=1` via `os.environ.setdefault` *before* importing `converter` (which transitively imports `pymupdf4llm` → `pymupdf-layout` → `onnxruntime`). This is critical: the layout model's ORT session otherwise auto-spawns its own intra-op thread pool that fights the outer `ThreadPoolExecutor` for cores, and throughput collapses. With both flags set, outer threads cleanly own one core each and the layout model runs single-threaded inside them. Measured speedup on 8 PDFs:

| config | wall | per-PDF |
|---|---|---|
| seq, default ORT threading (old) | 110.10 s | 13.8 s |
| seq, `OMP=1` | 37.01 s | 4.6 s |
| `-j 8`, default ORT | 67.94 s | 8.5 s |
| `-j 8`, `OMP=1` (current default) | **28.70 s** | **3.6 s** |
| `-j 16`, `OMP=1` | 31.55 s | 3.9 s |

So the *biggest* per-PDF win came from disabling ORT auto-threading even in sequential mode (3× faster) — the small layout model just doesn't have enough work for ORT's intra-op pool to amortize its own threading overhead. The outer pool then adds another 1.3× on top. `setdefault` keeps explicit user overrides intact (`OMP_NUM_THREADS=4 monitorul-ii convert ...` still wins).

A `ProcessPoolExecutor` would push further by giving each process its own ORT pool, but pickle/IPC overhead and process startup eat the win for short ranges. If a future workload regularly converts thousands of PDFs in one shot, swap the executor — `_process_one` is already pickle-clean.

### GPU

Not currently. `pymupdf-layout` runs two ORT inference sessions per PDF: the main `session` honors the default provider list (would use CUDA if `onnxruntime-gpu` were installed), but the `feature_extractor` is **hardcoded** to `providers=['CPUExecutionProvider']` in `pymupdf/layout/onnx/BoxRFDGNN.py:221`. So even installing `onnxruntime-gpu` (~3 GB, plus a CUDA 12.x runtime) would only accelerate one of the two inference calls. With layout-model work already reduced to ~3 s/PDF on an 8-thread CPU pool, the marginal GPU win on the bigger of the two sessions wouldn't justify the install weight or the GPU-as-hard-dep on this tool. Revisit if `pymupdf-layout` ever exposes an execution-providers knob.

## Type detector — `monitorul-ii classify`

Step 1 of the extraction pipeline. Sweeps every converted MD and tags it with one of the six document types from `docs/extraction-schema.md` (`plenary_stenogram | plenary_joint_session | committee_synthesis | report_facsimile | question_register | other`). Implemented as `src/monitorul_ii/classifier.py`: pure regex, pure functions, no I/O outside `classify_file` reading the front of one MD.

```
CLI (cli.py: cmd_classify)
  ├─ collect_mds(paths, reverse)        # mirrors collect_pdfs but for *.md
  └─ for each md:
       ├─ classify_file(md):
       │    ├─ open and read first HEADER_WINDOW_BYTES (10_000) of body
       │    ├─ parse_issue_suffix(filename)        # 'c', 'R', 'Bis', or ''
       │    └─ classify(text, suffix) → ClassifyResult
       └─ emit JSONL row → stdout
       (--outliers filter drops confidently-classified rows)
```

### Why pure regex (not ML, not LLM)

The detection rules fit on one page (`docs/extraction-schema.md` build order step 1) and the publisher (Romanian state press) is mechanically consistent about the markers — issue-number suffixes for committee/report genres, parenthesized header markers for plenary/question-register/joint genres. ML buys nothing on a problem this regular and costs replayability: a regex misclassification can be pinned to a single line of code and a single sample. We measured this empirically — the v1.3.0 ruleset sweeps 2300+ docs in <2 s and lands every document into a typed bucket with **zero `other` and zero residual ambiguous classifications** (after the structural-compatibility fix below).

### Score-and-rank instead of priority-pick

Each rule that fires *adds* score evidence rather than short-circuiting. The result carries `top_type`, `top_score`, `second_type`, `second_score`, the full `all_scores` map, and the list of `matched_signals` (which detection rules fired). Two reasons:

- **Audit trail.** A classification of `report_facsimile` from the `R` suffix that *also* sees `(RAPOARTE DE ACTIVITATE)` and `ȘEDINȚE COMUNE` markers tells you the report was reproduced verbatim and was received in a joint session — useful downstream signal that priority-pick would discard.
- **Ambiguity surfacing.** Without all-rules-evaluated, you can't tell apart "decisive" from "I happened to hit one rule first" classifications.

Suffix rules score `1.0` (deterministic from the filename); body markers score `0.85–0.95` (still very high — these are unambiguous header phrases — but leave room below the suffix to express confidence ranking). The `DEZBATERI PARLAMENTARE` marker alone scores `0.65` because it appears on both plenary stenograms *and* question-register documents, so it can't decide between them on its own.

### Structural co-evidence ≠ ambiguity

`is_ambiguous(threshold)` returns `True` when `top_score - second_score < threshold` — *unless* the runner-up is structurally implied by the winner. Two pairs are encoded in `_COMPATIBLE_RUNNERS_UP`:

- `plenary_joint_session` ⊃ `plenary_stenogram` — joint sessions are stenograms; the `(STENOGRAMA)` marker firing alongside `ȘEDINȚE COMUNE …` is co-evidence, not classification doubt.
- `report_facsimile` ⊃ `{plenary_joint_session, plenary_stenogram}` — `R`-suffix reports are received in a (typically joint) session whose markers will also fire.

Without this rule the corpus had 259 "ambiguous" docs that were nothing of the sort — every joint session showed up in the outliers list because joint-session = 0.95 and stenogram-co-evidence = 0.85 fall inside a 0.2 default threshold. With the rule, the outliers filter is precise: it surfaces only docs that genuinely don't classify cleanly.

### `SINTEZA LUCRĂRILOR COMISIILOR` body-marker fallback

Most committee syntheses are detected by their `c` issue suffix (decisive, score `1.0`). One historical doc (`2017-06-27_MO-PII-19-2017.md`) is published as `Nr. 19/C` in the body but the URL routed it under bare `19` upstream, so the suffix was lost. The body-marker rule (`SINTEZA LUCRĂRILOR COMISI[I/EI]…` at score `0.9`) is the safety net that catches this case — and any future upstream metadata drift of the same shape. The score is just below the suffix `1.0` because the suffix is canonical when present; both are well above the 0.0 threshold so either alone classifies confidently.

### Reading window

`HEADER_WINDOW_BYTES = 10_000`. Markers always live in the front matter / first heading block; reading more burns I/O for no win. `errors='replace'` on the read so a corrupted older PDF (CP1250→Latin1 mojibake, e.g. `2014-01-21_MO-PII-3R-2014.md`) doesn't blow up the sweep — those still classify on the suffix.

## Extract pipeline — `monitorul-ii extract`

Step 2 of the extraction pipeline (and Step 1.5 in the revised build order — see `docs/extraction-schema.md`). Reads a converted MD, classifies it (or honours `--type`), dispatches to the per-document-type extractor, and writes a strict-validated `<basename>.extraction.json` sidecar. Schema version 1.5.0 introduces the per-component `extractor_versions` block and the diagnostic `coverage` block — both engineering-driven additions, no body-shape changes from v1.4.0.

### Module split

Subpackage at `src/monitorul_ii/extraction/`, deliberately kept separate from the flat-modules layout the earlier pipeline stages use because extraction has tight internal coupling (every per-type extractor calls Speaker parser, References parser, boilerplate detector, coverage computer, schema validator). The flat-codebase invariant holds at the *pipeline-stage* level: `extraction/` is one new pipeline stage, not six.

| File | Role |
|---|---|
| `extraction/pipeline.py` | The dispatcher — `extract(md_path, *, force, override_type, write)` returning `ExtractResult`. Orchestrates classify → per-type extract → boilerplate merge → coverage → envelope build → schema validate → atomic write. Holds `SCHEMA_VERSION = "1.5.0"` and `EXTRACTOR_LABEL = "regex@1"`. |
| `extraction/envelope.py` | `split_md(text) → (frontmatter, body)` — the single source of truth for "where does the body start." `EnvelopeMeta` + `envelope_meta_from_frontmatter`. Hand-rolled flat YAML parser (no PyYAML dep for a 5-key file). `document_id(meta) → "mo://YYYY/PART/ISSUE"`. |
| `extraction/coverage.py` | `Claim` dataclass (`chars`, `lines`, `kind=record\|boilerplate`, `reason`). `compute_coverage(body, claims)` returns the envelope `coverage` block. Half-open char ranges, 1-indexed inclusive line ranges. Gaps below 20 chars or pure-whitespace are dropped. |
| `extraction/boilerplate.py` | `claim_shared_boilerplate(body) → list[Claim]`. Universal MO preamble patterns: issue-banner, weekday-date, partea/dezbateri/chamber headings (H1 *and* H2 — 2012-era docs render at H2), session label, legislature paren. |
| `extraction/speakers.py` | `parse_questioner(raw) → Speaker dict`. Splits on first comma, recognises deputat/senator titles, accepts both uppercase party acronyms (PNL, SOS România) and older lowercase descriptors (progresist). |
| `extraction/references.py` | Stub for v0.1; the discriminated-union parser (12 Reference variants) lands when the `plenary_stenogram` extractor needs it. Ships a version constant only so the per-component `extractor_versions` block has a slot. |
| `extraction/schema.py` | Loads `extraction_schema.json` once at import (`@lru_cache`), validates against the Draft 2020-12 metaschema. `validate(sidecar)` raises `SchemaError` with path + offending value pre-formatted (`"$.body.questions/0/topic: 'foo' is not …  [value=…]"`). |
| `extraction/extractors/__init__.py` | `EXTRACTORS: dict[DocumentType, ExtractorFn]` registry — types absent from the dict are skipped at the dispatcher. `EXTRACTOR_VERSIONS` parallel dict, copied into each sidecar's `extractor_versions`. |
| `extraction/extractors/question_register.py` | The first per-type extractor. `extract(ctx) → (body_dict, list[Claim])` matching `$defs/QuestionRegisterBody`. Parses addressee headers, question headers, topic, registration_number/registration_date, builds per-question Speaker via `parse_questioner`. |
| `extraction_schema.json` | Canonical machine-readable schema (lives at `src/monitorul_ii/extraction_schema.json` so it ships with the wheel). Strict envelope + question_register body + `OtherBody`. `PendingBody` (`additionalProperties: true`) for the four types whose extractors haven't shipped — tightened as each lands. |

### Why JSON Schema, not Pydantic

Three reasons (Q3 from the design grilling):

1. **The schema doc is already a schema.** `docs/extraction-schema.md` is byte-faithful to JSON Schema; Pydantic models would force a second source of truth in Python with the same drift risk.
2. **Language portability.** A future Postgres ingest, TS frontend, or CI check in any language can validate against the same `.json` file. Pydantic locks validation to Python.
3. **Dep weight.** `jsonschema` is pure-Python, ~1 MB. `pydantic` pulls a Rust core (`pydantic-core`) and tracks Python releases tightly — heavier, more migration churn for a tool whose hot path is regex.

The known JSON Schema downside (cryptic error messages) is mitigated by `_format_error` in `schema.py`: every error string carries `<path>: <message>  [value=<repr>]`, which is enough to debug the violating field without re-reading the schema file by hand.

### Sidecar shape and idempotency

Sidecar path: `<basename>.extraction.json` next to the MD. The triple-suffix (`.extraction.json`) self-documents and avoids colliding with any other JSON sidecar. Built manually (not via `Path.with_suffix`, which rejects multi-dot suffixes).

Atomicity: write to `<basename>.extraction.json.part`, rename on success. The renamed file never appears mid-write. JSON validation runs *before* writing, so a malformed dict never lands on disk; instead, the rejected dict is dumped to `<basename>.rejected.json` for inspection.

**Version-aware idempotency** is the load-bearing pattern (Q2 from the grilling):

```
existing sidecar's schema_version == "1.5.0"      # current
AND existing sidecar's extractor_versions == { boilerplate: "0.1.0", coverage: "0.1.0",
                                                references: "0.1.0", speakers: "0.1.0",
                                                <doc_type>: "0.1.0" }
→ skip: "versions match"
```

Mismatch (any key, any version, including missing keys on the cached side) re-extracts. This means the schema-bump workflow is automatic: bump `SPEAKERS_VERSION` from `0.1.0` to `0.1.1`, re-run `extract pdfs/`, and only the docs whose body content depends on Speakers regenerate. The cost on a clean re-run (every doc up to date) is one open + parse per skipped sidecar — about 1 ms/doc, ~2.5 s for 2300 docs. Acceptable.

`--force` overrides the gate (always re-extract).

### Coverage as diagnostic, not gate

`coverage.claimed_pct` measures how much of the body the extractor accounted for — either via real records (each emitting a `source_span`) or via by-policy boilerplate skips. Anything > 20 chars not in either bucket becomes a `gap` with line numbers + a 200-char preview.

Coverage **never gates writes** (Q6 from the grilling). A doc with 41% coverage is a *signal* the extractor needs work; it's not an *error* in the schema-validation sense. The CLI's `--coverage-below MARGIN` flag adds a JSONL row to stdout for each doc whose `claimed_pct < MARGIN` (with the top-3 gap previews) — same UX as `classify --outliers`. The discovery loop is: lower the threshold, eyeball the gaps, add patterns to `boilerplate.py` or refine the per-type extractor, bump versions, re-run.

On the 51-doc question_register cohort: p50 coverage 99.95%, p10 99.4%, worst 95.15% (a 2022 doc with a heavily-fragmented post-question signature block). Tightening the `_TOPIC_TRIM_RE` patterns and the boilerplate registry are the levers; both bump their respective versions and the affected docs auto-re-extract.

### Source-span coordinate system

Locked in v1.5.0 (Q7): all `lines` and `chars` arrays are 1-indexed and 0-indexed respectively, both relative to the **body text** (post-frontmatter). `content_sha` is sha256 of the body bytes truncated to 12 hex chars (matches the existing PDF-sha convention from `scraper.py`). Frontmatter spans aren't addressable — frontmatter is structured into `metadata`, so no extracted record should reference it. Defining the system once in `coverage.lines_for_range` removes the off-by-one ambiguity that would otherwise drift extractor-by-extractor.

### question_register specifics

The simplest body shape in the schema and the smallest cohort (43-51 docs over 13 years, depending on classify-time). Picked first to shake out scaffolding rather than for corpus impact. Layout is consistent enough that one extractor handles 2012-2026 with three regex tweaks:

- **Addressee headers** at H2 with three forms: personal (`## **Domnului <Name>, <role>**`), with rank prefix (`Domnului general …`, `Doamnei prof. …` — stripped before splitting on first comma), and institutional (`## **Curții de Conturi**`, `## **Băncii Naționale a României**` — no Domnului prefix; `name=null`, the institution text goes into `ministry`). A blacklist (`_ADDRESSEE_BLACKLIST_RE`) filters out boilerplate that shares the `## **TEXT**` shape — the H2 chamber-heading variants of older docs would otherwise mis-fire as institutional addressees.
- **Question headers** with optional `## ` prefix (modern docs use `## 1. **<Q>...**`; 2012-era docs drop the `##`). The bold inner content must contain `, deputat` or `, senator` to match — that disambiguates from numbered bold list items inside question bodies.
- **Topic line variants** (`Obiectul întrebării:`, `Obiectul:`, `Obiect:`, `Subiectul:`, `Subiect:`, `Subiectul întrebării:`, `Întrebare privind …`) all parsed; `_TOPIC_TRIM_RE` strips a trailing salutation (`Stimat[ăe] domn|doamn`, `Domnule ministru|director|...`, `Doamnă ministr|...`) when the convert step joined the topic with the question's opening salutation.

Per-question `source_span` covers `[addressee_header_start, next_question_or_eof)`. The first question after each addressee header includes the addressee header in its span; subsequent questions sharing the same addressee header start at the question header (no double-counting). The qr-specific boilerplate (`LISTA` word and the long `ÎNTREBĂRILOR ADRESATE…` paragraph) emits to `coverage.claimed_by_policy[]` with reason prefixes `question_register.lista_word` / `question_register.list_header_sentence`.

### Why no DB tracking for extractions

Symmetric with `convert` — sidecar's own envelope is the source of truth. Adding an `extractions` table for fast corpus queries (worst-coverage histogram, "which docs need re-run after a `references` bump") is a reasonable v0.2 — but as a *projection* rebuilt from the sidecars, never authoritative. The version-aware idempotency gate reads the existing sidecar's envelope (one open + parse), which is fast enough for 2300-doc batches.

### Sequential, not parallel

`cmd_extract` runs sequentially — no `ThreadPoolExecutor`. Unlike `convert` (where PyMuPDF releases the GIL during PDF parsing), extraction is regex sweeps + JSON building, all pure Python, all GIL-bound. On the question_register cohort the whole 51-doc batch extracts in ~2 s. Adding parallelism would buy nothing and complicate progress-bar plumbing.

### Adding a new per-type extractor

The contract is small:

1. Author `src/monitorul_ii/extraction/extractors/<type>.py` with `extract(ctx) → tuple[BodyDict, list[Claim]]` and an `EXTRACTOR_VERSION = "0.1.0"` constant. (For larger extractors, use a sub-subpackage `extractors/<type>/` with sibling modules per concern — see `extractors/plenary/` for the canonical example.)
2. Register it in `extractors/__init__.py` (`EXTRACTORS[type] = module.extract` + `EXTRACTOR_VERSIONS[type] = module.EXTRACTOR_VERSION`).
3. Tighten the corresponding `$defs/<TypeBody>` in `extraction_schema.json` from `additionalProperties: true` to the strict shape.
4. Bump `schema_version` in both the JSON file and `pipeline.py` if the body shape introduces new keys outside what v1.11.0 already documents.
5. Add fixtures + golden + targeted unit tests under `tests/extraction/`.

The dispatcher picks it up automatically — no changes to `cli.py`, the progress bar, the upload tier, or the version-aware idempotency gate.

## Extract pipeline — plenary types (`plenary_stenogram`, `plenary_joint_session`)

v0.1 ships both plenary types together. Single-chamber plenary covers ~2240 docs (the bulk of the queryable corpus); joint sessions are ~50 docs but structurally distinct (two chairs, parallel bill codes). Per the Q12 design lock, they share machinery via composition: `extractors/plenary/__init__.py` is the orchestrator for `plenary_stenogram`; `extractors/plenary_joint_session.py` is a thin wrapper that calls into the same sub-extractors and augments `session.chambers_present`.

### Module split

Six modules under `extractors/plenary/`:

- **`session.py`** — owns the body's pre-first-speaker span (chair narrative italic block, `Ședința a început` italic line, attendance announce, format markers) and the body suffix (closing phrase → `outcome`, `Ședința s-a încheiat la HH:MM` → `closed_at`). Also claims SUMAR table + `(STENOGRAMA)` marker as plenary-specific boilerplate. Multi-segment chair narratives are detected via `în prima parte` / `Ultima parte` markers; chairs and secretaries are dedup'd across segments. Format detection includes pre-2020 `in_person` default for docs without explicit markers (pandemic-era added the marker universally — see schema § X3-2). Special_procedure detection runs over the body header for explicit markers (`Ședință solemnă consacrată` → `sedinta_solemna`, etc.); v0.1 does not yet derive it from agenda category combinations.
- **`agenda.py`** — 28-category rule table (`_CATEGORY_RULES`) with weighted resolution: specific patterns (oath_taking, government_hour, foreign_address, etc.) carry weight 1.0; generic `bill_debate` baseline carries weight 0.5 so it loses ties to specifics. Multi-match returns runners-up within 0.2 of the winner for confidence-encoding. Category-conditional sub-fields: `confidence_type ∈ {învestitură, cenzură, angajare_răspundere, demitere}` for `government_confidence`; `requested_by_group` for `government_hour`; `reexamination_reason` for any title containing `reexaminare`. SUMAR-driven enumeration via three patterns: (1) `<br>`-separated cells inside one big table cell (modern docs), (2) per-item `|N.|...|page|` rows (multi-table SUMARs), (3) plain `N. Title ........ page` lines (older docs). Body-scan fallback for docs without SUMAR. The partition tail-extension lifts the last SUMAR-marked entry's end to `agenda_end` so unmatched ordinals at the body tail are absorbed (covers SUMAR-parser misses on long agendas).
- **`votes.py`** — 5-stage state machine (open / result / outcome / deferral / quorum). Vote-open patterns: `Supun votului`, `Vă rog să vă pregătiți de vot`, `Să înceapă votul`, `Trecem la vot`, `Vă rog să votați`. Result patterns parse counts per field (`for | for_unanimous | against | abstain | not_voting`); accept Romanian decimal-thousands notation (`1.234`). Window upper bound is the next `## **` speaker header — NOT the next vote-open phrase, because chair sequences like "Supun votului... Să înceapă votul... result" use multiple open-style phrases as part of one event (bounding by next-open would cut the window before the result line). Vote `actual_end` caps at the next newline after the result line — never swallows trailing italic narrators or next-paragraph chair narration. Motion type detector: 8 enum values; `system_check` filter for hardware tests (per v1.3.0 P3-1 finding). Voting method: 7 enum values; null when chair doesn't restate. Unanimous-literal handling: `counts.for = "unanimous"` (string) when chair says only `Mulțumesc` or `Cu unanimitate de voturi` — preserves the protocol verbatim per schema § 8 line 211.
- **`activities.py`** — 2-pass partitioner per Q6 design. Pass 1 splits by `## **NAME:**` speaker headers into turns. Pass 2 within each turn finds embedded events (votes via `votes.detect_votes`, italic blocks discriminated as narrator vs procedural by content patterns, bare deferral phrases not paired with a vote-open) and splits the speech around them, producing speech sub-activities + interleaved event activities. Pass 3 sorts by `source_span.chars[0]` and hard-asserts non-overlap (catches Pass 2 bugs loudly). Implicit-chair speech wrap when no headers present (final_vote_batch items get a `<chair narration>` speaker placeholder). Speech sub-activities populate `references_mentioned[]` via `parse_mentioned_references` on each fragment text. Italic block regex matches both standalone-line `_text_$` and inline parenthetical `_(Aplauze.)_` forms.
- **`interpellations.py`** (v0.2.3) — full per-doc interpellation pipeline: block detection, header parsing with chair / declaration / footer filtering, `question_text` body recovery, `response` pairing with replying minister turns, quote-aware `topic` detection, and (v0.2.3) corrected `addressed_to` capture. Production sweep on full 5551-doc corpus: 275 docs detect a block (4.96% — vs. v0.1's 1/5551), 7180 interpellations emitted, 6465 (90.0%) recover question_text, 25 paired with a minister response, **0 declaration leakage, 0 footer pollution, 0 salutation-only topics**. Coverage holds at p50=0.998 / mean=0.932.

  - **Block detection** (`find_interpellation_block`): 10 chair-declarative transition phrases — `trecem la primirea răspunsurilor la interpelări`, `Începem sesiunea de întrebări/interpelări`, `Declar deschisă (sesiunea/ședința) (consacrată/de) ... interpelări/întrebări` (most common modern form), `Urmează (prezentarea/sesiunea) ... interpelări`, `Deschidem ședința consacrată răspunsurilor`, `Răspunsuri la interpelări.` (line-anchored with negative-lookbehind for `|` to reject SUMAR rows), `trecem la (ultimul) punct (de pe / din) ordinea de zi: întrebări, interpelări`, `## Întrebări orale ...` heading. Combined hit-rate 275 / 5551. Block ends at EOF.

  - **Header regex tightening**: `_INTERP_HEADER_RE` requires the inner to end with `:` (`^##\s+\*\*\s*[^*\n]+?\s*:\s*\*\*\s*$`). v0.1 accepted any `## **anything**` line, which falsely matched the trailing `## **EDITOR: GUVERNUL ROMÂNIEI**` MO footer (colon embedded mid-string). True speaker headers always end with `:**`.

  - **Boilerplate-name guard**: `_is_boilerplate_name` catches the rare `## **ABONAMENTE LA PUBLICAȚIILE OFICIALE ... DISTRIBUȚIE:**` form (which DOES end in `:**`) by length / casing / footer-keyword shape (`ABONAMENTE`, `MONITORUL OFICIAL`, `EDITOR`, `R.A.`, `DISTRIBUȚIE`, all-uppercase ≥30 chars). Skipped headers are claimed as `interpellation_footer_match` boilerplate.

  - **Chair filtering**: `_collect_chair_names` (in `extractors/plenary/__init__.py`) collects names from `session.chair[]` + `secretaries[]` + `chair_segments[*].chair`. `_extract_inline_chair_names` backfills from the chair's self-introduction inside the transition phrase (`conducerea fiind asigurată de subsemnata, NAME, președintele Senatului, asistată de domnul senator X și doamna senator Y, secretari ai Senatului`) — required because session.py's italic chair-narrative detector frequently misses pandemic-era and 2020+ Senate docs where the chair speaks inline rather than via a chair-narrative paragraph. Headers whose parsed name matches a chair name (case-insensitive, whitespace-collapsed) are claimed as `interpellation_chair_turn` boilerplate, not emitted as records.

  - **Pure-declaration filtering**: many docs (especially 2021+ pandemic-era Senate docs) carry "interpellations agenda points" that are actually political-declaration sessions. `_is_pure_declaration_turn` skips turns whose head (first 600 chars) contains a declaration marker AND no interpellation phrasing anywhere in the turn. Markers: `Titlul declarației [+adjectives]`, `Declarație politică (cu titlul|privind|cu tema|adresată)`, `prezint o declarație politică`, bare `Declarație politică.` line, signature-line `Senator/Deputat NAME, ... Declarație politică`. Negative gate (interpellation phrasing): `interpel`, `am o (întrebare|interpelare)`, `adresez (această) (întrebare|interpelare)`, `obiectul (întrebării|interpelării)`. Skipped turns are claimed as `interpellation_pure_declaration` boilerplate.

  - **`question_text`** (v0.2.0+, refined v0.2.1+): 3-step pipeline. (1) Cap at the earliest section-break marker — same speaker continues into a political declaration or next interpellation: `Voi citi (și) declarația politică`, `Trec la a doua/cealaltă (întrebare|interpelare)`, `Titlul declarației [+any-words]`, generalised `Declarație politică ...` (line-anchored OR sentence-internal), `prezint/supun atenției o declarație politică`, signature-form `Senator/Deputat NAME, ... Declarație politică`, `Sunt NAME, senator/deputat ... declarație politică`. (2) Cap at closing markers (`Solicit răspuns`, `Aștept(ăm) răspunsul`, `Doresc un răspuns`, line-anchored `Cu stimă,` / `Cu respect,`). (3) Drop leading pleasantry lines (`Mulțumesc`, `Vă mulțumesc`, `Bună (dimineața/seara/ziua)`, `Doamnă/Domnule președinte`, `Stimați colegi/Stimate colege`, `Doamnelor și domnilor`, `Dragi români`) and topic-preamble lines (`Voi da curs citirii interpelării ...`, `Interpelarea (este adresată/se adresează) ...`, `Obiectul interpelării: ...`, `Adresez (această) (întrebare|interpelare) ...`). Returns null when residue < 40 chars. Confidence ramps 0.7 → 0.85 when qt is filled.

  - **`response` (v0.2.2)**: `_find_responder_headers` matches the responder-header shape `## **NAME** _– role_` (the bold encloses just the name, the italic carries the role). Three concrete forms across the corpus: (1) single-line complete role with `**:**` ending; (2) multi-line, role split across paragraph break, continuation has `## ` prefix and ends with `**:**`; (3) multi-line, continuation has no `## ` prefix. Continuation is searched within the next ~400 chars after the first-line match. Role must contain a recognised responder-role token (`secretar de stat`, `ministru`, `viceprim-ministru`, `prim-ministru`, `consilier`, `șeful`, `director`, `președintele Curții/Consiliului`, `avocatul poporului`, `guvernator`) — guards against italic narrative decoration (`_– deputat PNL_`, `_– președintele_`) being misclassified. Each responder pairs with the most recent emitted questioner whose span ends before the responder's start; `Interpellation.response = {speaker, text}` populates with the parsed Speaker (role injected from regex capture) and `_extract_response_text`-cleaned body. Confidence bumps to ≥0.9 when both qt and response are filled. Orphan responders (no preceding questioner) are claimed as record-class but not attached.

  - **`topic` (v0.2.2)**: `_extract_topic` prefers (1) the FIRST quoted subject (`„...", «...», "..."` — Romanian / French / ASCII quotes, length 8-280) within the first ~2500 chars of the turn; (2) explicit `obiectul (interpelării|întrebării): <text>` / `cu obiectul: <text>` / `tema: ...` capture; (3) fallback to first non-pleasantry, non-salutation, non-italic, non-heading line. `_TOPIC_SKIP_LINE_RE` is more aggressive than `_PLEASANTRY_LINE_RE` — additionally skips `Domnule/Doamna (ministru|secretar de stat|președinte|viceprim-ministru|prim-ministru|deputat|senator)` salutation lines that the v0.1 first-line heuristic mistook for topics. Returns null when no topic candidate is found.

  - **`addressed_to` (v0.2.3)**: three-pattern priority capture against the first 800 chars of the questioner's turn body, with an address-verb anchor (`adresat[ăaã]?`, `adresez[ăaã]?`, `adreseaz[ăã]`, `c[ăa]tre`, `interpel[ăaã]rii`, `întreb[ăaã]rii` — accepts modern, cedilla, and mojibake variants) preceding each pattern within a ~200-char window. Patterns: (1) `Ministerul[ui]? X` — clean ministry name, returned as `Ministerul X`; (2) `ministrul[ui|l]? X` (lowercase initial role-form, with optional `al/a/ale` connector consumed) — transformed to `Ministerul X` because the registry enumerates institutional aliases (`Ministerul Educației`), not role-form aliases (`ministrul educației`); (3) `(doamnei|domnului|doamna|domnul) [ministru[lui|l]] NAME` — bare person fallback for non-ministerial addressees (secretar general al Guvernului, etc.). Pre-v0.2.3, a single regex with non-greedy `(?P<role>[^.,\n]+?)` captures and no trailing anchor produced single-character noise (`'D'`, `'C'`, `'m'`) for ~76% of non-null hits (1383/1953 in production); v0.2.3 captures bounded by sentence punctuation eliminate that mode. The 800-char head bound prevents over-firing on incidental ministry mentions deeper in the question body (the v0.2.2 `Ministerul Culturii au fost sesizate probleme similare la alte` style sentence-bleed).
  - **Defaults**: `addressed_to_normalized` populated by the Tier 4.2 ministry backfill; `interpellation_number` via qr's `Nr. N(.NNN)?[A-Z]?` regex (last match wins); `response_deferred=True` when `(în scris)` notation present; default `genre=interpelare`, flips to `întrebare` only when the questioner block strongly hints at oral-question form.
- **`boilerplate.py`** — plenary-specific patterns: bold-only PARTEA banner (when MD lacks `#` prefix), joint-session `ȘEDINȚE COMUNE...` header line, SUMAR keyword, `Doamnelor și domnilor [deputați și senatori]` chair-address opener. Kept separate from shared `extraction/boilerplate.py` so its bumps only invalidate plenary sidecars (not qr / committee / report sidecars).

### Composition over inheritance for joint session

`extractors/plenary_joint_session.py` is ~30 LOC: detect `chambers_present` via `_JOINT_HEADER_RE`, reuse `session.extract_session` / `agenda.extract_agenda` / `interpellations.extract_interpellations` from `plenary/`, augment the session dict with `chambers_present`, return the joint-shape body. Schema discriminator at the top-level `oneOf` routes the body to `PlenaryJointSessionBody` ($defs/plenary; the only field difference from `PlenaryStenogramBody` is `session.chambers_present: array of string`).

### Coverage targets and measurement

Per Q9: **discovery margin 0.85** (CLI default for `--coverage-below`), **test fixture floor 0.80** (suite asserts ≥0.80 per fixture), **mean target 0.90 documented (ungated)**. No per-doc production gate — coverage stays diagnostic per the existing pipeline contract. Median + p10 are the more useful diagnostic than mean (mean is dragged by catastrophic outliers). Five hand-picked fixtures span:

| Fixture | Type | Coverage |
|---|---|---|
| `2025-10-13_MO-PII-117-2025.md` (multi-segment chairs, joint, 19-item SUMAR) | `plenary_joint_session` | 0.988 |
| `2024-04-22_MO-PII-53-2024.md` (single chair, modern Camera) | `plenary_stenogram` | 0.894 |
| `2025-11-28_MO-PII-150-2025.md` (modern Senat) | `plenary_stenogram` | 0.996 |
| `2017-01-12_MO-PII-6-2017.md` (mid-corpus, post-PHCD adoption, pre-pandemic) | `plenary_stenogram` | 0.995 |
| `2013-01-30_MO-PII-1-2013.md` (older joint, no modern PHCD) | `plenary_joint_session` | 0.9999 |

Smoke on 15 recent 2025-12 plenary samples: 12 of 15 extracted (other 3 classified as different types), all 12 schema-valid; mean coverage 0.81, median 0.997. Outliers (~20% of corpus below 0.85) are discovery-loop work for v0.2 — different layout patterns my regex packs don't yet recognise.

### Helper graduations at v0.1

| Helper | Pre-v0.1 | v0.1 ship | Why |
|---|---|---|---|
| `boilerplate` | 0.1.0 | 0.1.0 | Plenary boilerplate stayed in `extractors/plenary/`, not hoisted |
| `coverage` | 0.1.0 | 0.1.0 | No changes needed |
| `references` | 0.1.0 (stub) | **0.6.0** | 13 strict variants + `unknown` catch-all (emitter active). v0.2.0 shipped 6 (bill, law, oug, og, chamber_resolution, parliamentary_resolution); v0.3.0 graduated the long-tail set (`motion`, `court_decision`, `constitution`, `regulation`, `eu_doc`, `treaty`); v0.4.0 enabled the `unknown` emitter (hint enum `law-ish` / `court-ish` / `eu-doc-ish` / `other`); v0.5.0 added the **`code` variant** (Romanian named codes), **broadened `treaty`** to absorb Acordul/Protocolul/wider Carta forms, and populated **best-effort `subject`** on bill / law / parliamentary_resolution; v0.6.0 adds an **OOR-year demotion guard** (`_VALID_YEAR_BOUNDS` mirrors the schema's per-variant `year` minimum/maximum — strict-variant matches whose parsed year falls outside their bounds are demoted to `UnknownReference` with `hint="law-ish"`/`"court-ish"`; optional-year variants like `motion`/`eu_doc` just nullify `year`). Schema 1.10.0 adds 1 new $def (`CodeReference`) + `BillReference.subject` field. Production sweep on the 5551-doc corpus (v0.5.0): 20,481 code refs graduated; treaty 4,378 → 10,051; unknowns 125,822 → 110,966 (-14,856; `unknown.hint=other` went 3,228 → 0; `unknown.hint=law-ish` went 122,554 → 110,926, dominated by ~96K bare `art. N` cross-references that resist regex graduation). Subject fill rate: bill 50.5%, law 76.8%, parliamentary_resolution 88.9%. Coverage held p50=0.998 / mean=0.932. **v0.6.0 sweep: errors=0** (was 12) — the 12 pre-existing residual schema-validation failures every prior tier left in place are now closed; each had a single OOR-year reference (real-world OCR garbage like `Pl-x 100/2918` or pre-1990 cites of post-1990 instruments like `Ordonanța nr. 51/1988`), all demoted to `unknown.law-ish` per the guard. Stale `.rejected.json` files auto-cleaned by the dispatcher on successful re-extract. |
| `speakers` | 0.1.0 | **0.2.0** | Adds shared primitives (HONORIFIC_RE, PARLIAMENTARY_TITLE_RE, extract_delivery_mode, parse_honorific_speaker) — used by plenary's per-form parsers; qr's `parse_questioner` is unchanged |
| `topics` | (new key) | **0.1.0** | 15 canonical primary topics aligned with parliamentary committees; title-scoped detection only; secondary topics deferred to v0.2 LLM pass |
| `plenary_stenogram` | (new key) | **0.1.0** | First per-type ship |
| `plenary_joint_session` | (new key) | **0.1.0** | First per-type ship |

The flat `_shared_helper_versions()` contract (Q11) means qr sidecars re-extract on first plenary run because their cached `extractor_versions` no longer matches (added `topics` key, bumped `references` and `speakers`). Acceptable cost (~2.5s for 53 qr docs) per the conservative-by-design version-keying contract — over-invalidate on helper-output-shape changes rather than risk stale sidecars when a static dependency declaration drifts.

### Schema deltas at v1.6.0

Strict body shapes for `plenary_stenogram` and `plenary_joint_session` replaced the v1.5.0 `PendingBody` placeholders. New `$defs`: `Reference` (oneOf 7 variants — bill, law, oug, og, chamber_resolution, parliamentary_resolution, unknown), `VoteCounts` (with `for: oneOf [int, "unanimous", null]`), `Topics`, `Attendance`, `ChairSegment`, `PlenarySession`, `PlenaryJointSession` (extends with `chambers_present`), `Activity` (oneOf 5 variants — speech, vote, procedural, narrator, deferral), `Interpellation`, `AgendaItem` (with `category` enum extended by `"other"`), `PlenaryStenogramBody`, `PlenaryJointSessionBody`. `committee_synthesis` and `report_facsimile` continue as `PendingBody` until their extractors land.

### What's deferred to v0.2+

Backfill-registry-dependent fields (always null in v0.1): `Speaker.person_id` (person registry), `QuestionAddressee.ministry_normalized` and `Interpellation.addressed_to_normalized` (ministry registry), `Vote.proposed_by` (bill-sponsor registry), `Vote.nominal_breakdown` (parlament.ro per-MP voting feed), `bill.subject` / `law.subject` / `parliamentary_resolution.subject` (best-effort context labels). Schema-modeled but stubbed: 6 long-tail reference variants, `topics.secondary` (LLM pass), per-topic `extraction` provenance block. **Interpellation completeness shipped at v0.2.2**: `question_text`, `response`, and quote-aware `topic` are all populated; chair / pure-declaration / footer turns are filtered out (claimed as boilerplate, not emitted as records); 0 declaration leakage / 0 footer pollution / 0 salutation-only topics on the full 5551-doc corpus. Cross-document `defers_to` / `resolves` linker for tying cross-session deferrals to their resolving final-vote document still deferred.

### v0.1.x — discovery-loop coverage recovery

After v0.1 shipped on the modern fixture set, a full-corpus sweep over the 5551 plenary MDs surfaced a bottom quartile near zero coverage:

| Cohort | n | Mean | Median | p10 | p25 | p75 | <0.85 | <0.50 |
|---|---|---|---|---|---|---|---|---|
| plenary_stenogram (baseline) | 4075 | 0.689 | 0.991 | 0.001 | 0.081 | 0.999 | 1465 (36%) | 1236 (30%) |
| plenary_joint_session (baseline) | 372 | 0.765 | 0.998 | 0.020 | 0.802 | 1.000 | 97 (26%) | — |

Sampling the bottom-quartile docs revealed three dominant failure patterns rather than ten subtle ones:

1. **Mojibake (~31% of outliers, 2000-2007 cohort)** — older PDFs were converted from a Romanian font that lacked Unicode diacritics. PyMuPDF preserves the legacy bytes so MDs carry `Þ/þ` for `Ț/ț`, `ª/º` for `Ș/ș`, `ã` for `ă`, `Ñ/Ð` for em-dashes/en-dashes. The extraction-side regexes (`_OPENED_AT_DIACRITICS_RE`, `_CHAIR_BLOCK_OPENING_RE`, `_SUMAR_OPENING_RE`, etc.) only matched the modern-diacritic forms, so chair detection / SUMAR detection / time parsing all failed on the same docs.
2. **No agenda markers (~50% of outliers, all eras)** — short sessions (declarations only, response-to-interpellations only, procedural-only) have either a SUMAR with descriptive (non-numbered) entries or no SUMAR at all, AND have no `## **N. Title**` body markers. The previous code path produced empty `agenda_items: []`, so the body's speech turns went un-claimed even though the body had real content (often dozens of `## **NAME:**` speeches).
3. **Trailing footer un-claimed** — the `**EDITOR: GUVERNUL ROMÂNIEI** „Monitorul Oficial" R.A., …` block plus the `**A B O N A M E N T E   L A   P U B L I C A Ț I I L E**` subscription rate-card runs ~500-2000 chars at the end of every doc and was never claimed as boilerplate.

The fix landed in `extractors/plenary/` as four targeted relaxations + one new fallback path. No shared helpers (`speakers.py`, `references.py`, `topics.py`, `extraction/boilerplate.py`) were touched — by design, plenary-only patterns belong in plenary-only modules so version bumps don't invalidate other types' sidecars.

**Diacritic-tolerant regexes (`session.py`):** Added `_SEDINTA_VARIANTS = r"(?:[ŞȘªS]edin[țtţþ]a|[Ss]edinta)"` as a module-level helper accepting the modern Unicode form, the cedilla form (Ş, Ţ — separate Unicode points), the mojibake form (ª, þ), and the diacritic-stripped form (`Sedinta`). `_OPENED_AT_RE` / `_CLOSED_AT_RE` / `_CHAIR_BLOCK_OPENING_RE` / `_ATTENDANCE_RE` / `_CLOSED_PHRASE_RE` / `_SUSPEND_*_RE` / `_ADJOURNED_RE` / `find_sumar_span`'s end-detector all reference the same character classes. Time separator widened from `[.:]` to `[.,:]` for pre-2008 `13,25` style.

**Chair-block phrasing variants (`session.py`):** `_CHAIR_BLOCK_OPENING_RE` accepts three openings: modern `Lucrările au fost conduse`, 2008-era `Lucrările ședinței au fost conduse`, and 2008+ joint-session `Ședința a fost condusă`. Cedilla / mojibake variants of each. `_CHAIR_PERSON_RE` made the rank (`deputat|senator`) optional so the pre-2010 / Senate form `domnul Nicolae Văcăroiu, președintele Senatului` (no rank, role suffix) parses; without an explicit rank, the name is only treated as a chair when a chamber-bearing role suffix follows (prevents over-firing on every `domnul X` mention in the chair-narrative span). Title is inferred from role: `președintele Senatului` → `senator`; `Camerei Deputaților` → `deputat`.

**SUMAR keyword variants (`session.py` + `boilerplate.py`):** `_SUMAR_OPENING_RE` accepts bare `SUMAR`, markdown-prefixed `## SUMAR` (PyMuPDF heading promotion), and pipe-prefixed `|SUMAR<br>...` (some 2007-era table cells). The plenary-boilerplate `sumar_keyword` reason claims all three forms.

**Implicit single-item agenda fallback (`agenda.py`):** When SUMAR-driven enumeration produces zero entries AND body-scan finds no `## **N. Title**` markers AND the post-SUMAR span contains at least one `## **NAME:**` speech header, wrap the entire span as one implicit agenda item with `category="other"`, `confidence=0.4`, `title="Ședința"`. Activities are extracted from the wrapped span normally, so all speeches get claimed via record claims. The `_SPEECH_HEADER_RE` and the requirement that `extract_activities` returns at least one activity prevent the fallback from emitting blank items on truly empty bodies.

**Body-scan agenda regex relaxation (`agenda.py`):** `_BODY_AGENDA_ITEM_RE` made the `**` bold-wrapper optional so older docs' `## N. Title` (no `**`) headers parse. The `## ` prefix stays required to keep numbered lists embedded in speeches (`vă rog: 1. care e...`) from over-firing as agenda items.

**Editor footer boilerplate claim (`boilerplate.py`):** Added `plenary_stenogram.editor_footer` reason matching `\*\*\s*(?:EDITOR\s*:|A\s+B\s+O\s+N\s+A\s+M\s+E\s+N\s+T\s+E)[\s\S]*\Z` — the bold-prefixed editor masthead plus the subscription rate-card, both of which run to end-of-doc.

**Measured improvement** (5551-doc full-corpus sweep, before/after):

| Cohort | Metric | Before | After |
|---|---|---|---|
| plenary_stenogram | mean | 0.689 | **0.913** |
| plenary_stenogram | median | 0.991 | **0.998** |
| plenary_stenogram | p10 | 0.001 | **0.673** |
| plenary_stenogram | p25 | 0.081 | **0.980** |
| plenary_stenogram | <0.85 | 1465 (36%) | **565 (14%)** |
| plenary_stenogram | <0.50 | 1236 (30%) | **307 (8%)** |
| plenary_joint_session | mean | 0.765 | **0.956** |
| plenary_joint_session | <0.85 | 97 (26%) | **24 (6%)** |
| Errors (schema) | total | 11 | 13 |

Spot-check on representative outliers: `2000-02-11_MO-PII-2-2000.md` 0.002 → 0.997, `2008-09-12_MO-PII-73-2008.md` 0.014 → 0.999, `2015-02-23_MO-PII-16-2015.md` 0.014 → 0.999. Modern fixtures unchanged within rounding (`2024-04-22` 0.894, `2025-11-28` 0.996, `2025-10-13` joint 0.988).

The 2 additional schema errors are pre-existing — the references parser correctly extracts `Legea nr. 19/1898` (an 1898 law citation in a 2001 stenogram), but the schema's `law.year: minimum 1990` rejects it. Same root cause as the 11 baseline errors. **(RESOLVED in `references.py` v0.6.0)** the OOR-year demotion guard now routes such cites to `unknown.law-ish` instead of failing the strict variant. All 12 residual schema errors closed; corpus-wide errors=0.

Three pre-2010 fixtures added: `2000-02-11_MO-PII-2-2000.md` (Senatul, mojibake), `2005-02-11_MO-PII-2-2005.md` (Senatul, mojibake), `2008-09-12_MO-PII-73-2008.md` (Senatul, no-N body markers). Test floor 0.50 (vs 0.80 for modern fixtures) — pre-2010 layouts are intentionally lossier than post-2014.

**What's NOT touched** (intentionally): the `_clip_overlaps` Pass-3 step in `activities.py` stays as a clip rather than a hard assertion (the previous hard-assertion variant produced 1462 false errors across the corpus before being relaxed); shared `extraction/boilerplate.py` (which would invalidate qr/committee/report sidecars on a bump); coverage gating (still diagnostic-only).

## Extract pipeline — `committee_synthesis`

v0.1 ships the third per-type extractor — weekly synthesis of one or more parliamentary committees' work, published as MO Partea II issues with a `c` suffix (`13c/2013`, `28c/2025`). Total cohort: 976 docs (plus the closely-related 2003-era single-committee constitutional-revision synthesis sub-genre that uses the same MD layout). Single-file extractor at `src/monitorul_ii/extraction/extractors/committee_synthesis.py` — under 600 LOC including the tested boilerplate, so a sub-subpackage like plenary's wasn't justified.

### Why a partition-first design

A committee_synthesis MD is essentially a sequence of independent committee blocks each opened by `## N. **Comisia X**`. The partitioner is the load-bearing claim: every line between two consecutive committee headers belongs to the preceding committee, which means:

- **Coverage scales with header detection, not with field-level extraction quality.** If we can find every `## N. **Comisia ...**` header, the body is fully claimed by record spans even when half the per-committee fields are null.
- **Best-effort field extractors don't drag coverage down.** A failed date-parse leaves `dates: []` but the block is still claimed.
- **The discovery loop has a stable boundary.** Adding a new field-level parser (joint_with detection, roster table parsing, etc.) doesn't change the partition; it only fills in nulls inside an already-claimed block.

The four header shapes the extractor accepts (with the one quirk relaxation):

```
## 1. **Comisia pentru ...**       — modern (most common)
## **15. Comisia specială ...**    — number inside the bold (older / 2008 era)
## **Comisia pentru ...**          — no number (rare; older single-block)
## 1 **. Comisia pentru ...**      — bold opens between digit and period
                                     (2004-era PDF-MD conversion quirk)
```

Inner content must start with `Comisia` (case-insensitive) so signature lines like `## **Bogdan-Iulian Huțucă**` and the PARTEA banner don't match. The trailing-footer detection (`**EDITOR: PARLAMENTUL ROMÂNIEI`) terminates the last block — any prose after it is footer boilerplate, claimed separately.

### Single-committee fallback

About 1% of the corpus (mainly 2002-2004 docs reporting one committee's multi-session work) opens with `SINTEZA LUCRĂRILOR COMISIEI` (singular) and uses session-date `## N. **Ședința din ziua de ...**` headers instead of committee `## N. **Comisia ...**` headers. The partitioner finds zero matches there. A three-anchor fallback recovers them:

1. **SUMAR's first row** — `^\s*1\.\s*Comisia <name>...PAGE-RANGE` lifts the canonical name from the table of contents.
2. **SINTEZA singular heading** — `\bSINTEZA LUCRĂRILOR COMISIEI <descriptor>**` strips the descriptor genitive and prepends `Comisia` for nominative recovery.
3. **Prose-opening sweep** — `^\s*Comisi[ai] pentru ... s-au întrunit | și-a desfășurat` catches docs that open with prose directly (no SUMAR), e.g. December 2004 joint sub-committee outputs.

When any anchor fires, the entire body (up to the trailing footer) gets claimed as one committee record. Coverage on these docs jumped from 0.0% → 0.95+ on the discovery sweep.

### Date / time / format / purpose detectors

`_parse_dates` uses a trigger-then-window strategy: find `Comisia ... și-a desfășurat lucrările în [zilele/ziua/perioada] de`, then scan the next 200 chars (with markdown emphasis stripped) for a Romanian month name + 4-digit year. Day numbers between the trigger and the month/year become the committee's `dates[]`. Strip-emphasis is the trick that handles the multi-bold split (`**12, 13, 14** și **15 ianuarie 2015**`) uniformly with the single-bold variant. Cedilla forms (`şi-a desfăşurat`) and the older `desfășurat activitatea` variant are both accepted via a single trigger regex.

`_parse_time_windows` matches `HH[.:,]MM\s*[-–—]\s*HH[.:,]MM` and filters unrealistic hour/minute combos so article references like `art. 99.99` don't get classified as times. Modern docs use `15.00`; older docs use `13,25`; my regex accepts all three separators.

`_parse_format` returns `mixed` / `online` / null. The `mixed` regex catches the "atât la sediul Camerei Deputaților ... cât și prin mijloacele electronice" pandemic-era phrase plus the modern shorter `cu prezență fizică și online`. `online` is rare (post-2020 only); pre-2020 docs return null because there's no positive-evidence marker, and consumers default to in_person from the date.

`_parse_purpose` returns one of the 4 enum values or null. `documentare_consultare` fires on the `– documentare și consultare` suffix that 2021+ docs append to agenda items they will *discuss* but not vote on. `audiere_candidați` fires on the `Audierea ... candidat pentru ocuparea funcției` form. `aprobare_raport` is rare; most non-marker blocks are dezbatere_decizie which we leave null.

### Signature extraction with diacritic tolerance

`PREȘEDINTE,` and `SECRETAR,` followed by `**Name**` are the universal signature anchors. Multiple regex patterns handle the four observed layouts:

- Inline: `PREȘEDINTE, **Name**`
- H2-prefixed name: `PREȘEDINTE,\n## **Name**`
- Plain bold name: `PREȘEDINTE,\n**Name**`
- Heading-position SECRETAR (PDF-MD oddity): `## SECRETAR, **Name**`

The diacritic class `[ȘŞS�]` accepts modern Unicode (`Ș`), cedilla (`Ş`), stripped (`S`), and U+FFFD mojibake (`�`) so 2008-era docs with broken UTF-8 still extract signatures. Combined with the four pattern shapes, signature recovery reaches 70-95% across all eras (2008/2018/2022/2025 spot-checks).

### Agenda items + outcome parsing

`_split_agenda` finds numbered items (`^\s*\d+\.\s+...`) inside each committee block. Ordinals are filtered to 1..200 to reject article references like `art. 1.234` that look like agenda items. Restart sequences (committee A's day 1 has items 1-4, day 2 restarts at 1) are accepted; the ordinal filter just catches genuine garbage.

Per-item field extraction runs on the title text only — the small expected anchor — to avoid over-matching cites in dezbateri commentary. `parse_primary_references` is reused for bill/law/OUG cite detection. Implausible years (e.g. `Pl-x 527/2917`, an OCR typo) are filtered locally — schema's `[1990, 2100]` range is the source of truth, but we drop offenders before they reach the validator so a single typo doesn't fail the whole sidecar.

`_detect_committee_role` and `_detect_output_type` map title hooks to enum values. Order matters: `raport preliminar` must beat `raport`, `raport comun suplimentar` must beat `raport comun`. Stem-based regexes handle Romanian inflections (`adoptarea`, `aprobate`, `respinsă` all match their respective outcome verbs).

`_extract_outcome_text` finds the first paragraph after the agenda title that starts with an outcome-lead phrase (`În urma`, `Supusă la vot`, `Proiectul de lege a fost ...`, `Membrii comisiei au hotărât`). Capped at 600 chars to avoid sidecar bloat from multi-paragraph dezbateri narratives. `_parse_vote_summary` then mines this text for outcome / majority / numeric counts (`6 voturi împotrivă și două abțineri` parses correctly via the integer-or-Romanian-numeral handler).

### Coverage targets and measurement

Per Q9 (inherited from plenary): **discovery margin 0.85** (CLI default for `--coverage-below`), **test fixture floor 0.80** (suite asserts ≥0.80 per fixture), **mean target 0.90 documented (ungated)**.

Four hand-picked fixtures span the corpus eras:

| Fixture | Layout | Coverage |
|---|---|---|
| `2008-02-05_MO-PII-1c-2008.md` | Pre-pandemic narrative agenda + per-day numbered roster (`P.N.L.`/`P.S.D.` party-group abbreviations), occasional `�` mojibake on PRE�EDINTE; `Comisia permanentă a Camerei Deputaților și Senatului privind Statutul deputaților` triggers v0.2.0 `special_joint` graduation | 0.9997 |
| `2018-01-05_MO-PII-1c-2018.md` | 20-committee modern narrative-agenda + narrative roster baseline (`au fost prezenți: A, B, C` / `au absentat: D, E`) | 0.9998 |
| `2022-01-04_MO-PII-1c-2022.md` | Pandemic-era 17-committee, mixed format markers, `audiere candidat` purpose, hybrid roster (per-day with role-before-group, narrative with substitution side-comments), `în comun cu Comisia X din Senat` joint clauses | 0.9997 |
| `2025-08-12_MO-PII-28c-2025.md` | Heavily tabular agenda + tabular roster (two-pairs-per-row `|NAME|||STATUS|NAME||STATUS|`), 20 committees; UNESCO permanent joint committee triggers v0.2.0 `special_joint` graduation | 0.9998 |

Discovery-loop sweep over all 976 c-suffix MDs (2000-2026):

| Metric | Value |
|---|---|
| Extracted | 976 / 976 (no errors, no classify mismatches) |
| Mean | 0.9972 |
| Median | 0.9997 |
| p10 | 0.9965 |
| p25 | 0.9985 |
| Min | 0.886 |
| Below 0.85 | 0 |

Mean is well above the target 0.90; median above the target 0.95; the absolute minimum (0.886) clears the test fixture floor 0.80. This is more headroom than plenary v0.1.x had after recovery — partition-first design is structurally easier than per-speech extraction.

### Helper graduations at v0.1

| Helper | Pre-v0.1 | committee_synthesis ship | Why |
|---|---|---|---|
| `boilerplate` | 0.1.0 | 0.1.0 | Committee-specific boilerplate stayed in the per-type module, not hoisted |
| `coverage` | 0.1.0 | 0.1.0 | No changes needed |
| `references` | 0.2.0 | 0.2.0 | Implausible-year filter at the callsite — won't bump references; v0.2 will tighten the year regex |
| `speakers` | 0.2.0 | 0.2.0 | Reuses `make_speaker`; signature parser is committee-local |
| `topics` | 0.1.0 | 0.1.0 | Not yet wired (committees have a single fixed set of topical areas; v0.2 may use the canonical list) |
| `committee_synthesis` | 0.1.0 | **0.2.0** | Roster + joint_with + tabular agenda + special_joint kind shipped (Tier 3) |

The flat helper-version contract triggers re-extraction of every c-suffix sidecar on first v0.2.0 run because the cached `committee_synthesis` version no longer matches. Acceptable cost.

### Schema deltas at v1.7.0

Strict body shape for `committee_synthesis` replaced the v1.6.0 `PendingBody` placeholder. New `$defs`: `CommitteeSynthesisBody`, `CommitteePeriod`, `Committee`, `CommitteeMeeting`, `TimeWindow`, `JointCommittee`, `RosterEntry`, `CommitteeAgendaItem`, `CommitteeVoteSummary`. `report_facsimile` continues as `PendingBody` until its extractor lands.

v0.2.0 fills these `$defs` without a schema bump — every field shape was already reserved.

### What v0.2.0 ships (Tier 3 graduation)

Four fields that v0.1 emitted as null / `[]` are now populated:

#### Roster — three format detectors

`_detect_roster_format(block)` returns one of `tabular | narrative | per_day | None`, then dispatches to a sub-parser. Detection is conservative — when no shape is recognised, the parser emits `[]` (an honest answer the schema accepts).

- **Tabular** (2022/2024-): markdown tables with `Numele și prenumele` header + status cells. Status fingerprints accepted: `Prezent[ă] fizic` / `Prezent[ă] online` / `Prezent[ă] la sediul ...` (2022 synonym for `physical`) / `Absent[ă]` / `Înlocuitor[...]`. The 2025 corpus interleaves two parallel name/status pairs per row (`|NAME1|||STATUS1|NAME2||STATUS2|`); the parser walks each row's cells and pairs each status cell with the nearest preceding name-shaped cell. The 2022 corpus uses an in-cell role suffix (`Oana-Silvia Țoiu – președinte`) — the parser strips this and lifts the role into `intra_committee_role`. Single-token cells like `Neafiliată` / `UDMR` / `PNL` are explicitly filtered (party-group labels look like names but aren't).
- **Narrative** (2018-): four trigger forms accepted: `au fost prezenți: A, B, C`, `au fost prezenți următorii deputați:`, `au fost prezenți N deputați[, și anume]:` (count-and-list, 2018+), `Și-au înregistrat prezența la lucrări următorii deputați:` (modern formal). A follow-on `Domnii deputați A, C au fost prezenți on-line` clause flips matching names from the default `physical` to `online`. `Au absentat motivat: E, F` adds absent entries. Substitution side-comments `NAME – înlocuit[ă] de domnul/doamna deputat NAME` flip the subject to `substituted` and attach the substitute Speaker. Role suffixes `– președinte` / `– vicepreședinte` / `– secretar` are captured into `intra_committee_role`.
- **Per-day** (2008+, niche): numbered list `1. NAME[, ROLE], Grupul parlamentar al X[, ROLE].` after a `- au fost prezenți:` trigger. Two-phase parsing (header regex + tail regex) handles dot-bearing party-group abbreviations (`P.N.L.`, `P.D.-L.`, `P.S.D.`) which would otherwise tangle a single-pass regex. Both 2008-form (role after group) and 2021-form (role before group, with ` – prezent` tail) are accepted.

`_parse_roster` runs all three sub-parsers and merges by name (case-insensitive). Each sub-parser is internally self-gated by its own trigger pattern, so running on a block without its trigger returns [] safely; this lets hybrid blocks (a 2022 doc with a tabular roster for one day plus a narrative summary for another) recover entries from both forms.

Production smoke (976 c-suffix docs): **908 (93%) docs populate roster[], 123,695 total entries** (physical 111,298 / online 1,692 / absent 10,029 / substituted 676).

#### `joint_with[]` — multi-committee parser

`_parse_joint_with(block)` matches `\b[îi]n (?:[șş]edin[țţt][ăa] )?comun[ăa]? cu Comisia ...` and emits `[{name, chamber}]` per joint partner. The chunk extraction splits the captured list **by `Comisia/Comisiei/Comisiilor` starts** (NOT by commas), so multi-clause names like `Comisia juridică, de disciplină și imunități a Camerei Deputaților` stay intact instead of fragmenting at internal commas. Each chunk must have a known committee-name connector as its second token (`pentru` / `juridică` / `de` / `permanentă` / `comună` / `specială` / `parlamentară` / `națională` / `centrală` / `anchetă`) — this rejects verb-phrase imposters (`Comisia a deliberat`) and orphan list tail-fragments. Resolves Romanian conjunction ambiguity:

- `în comun cu Comisia juridică, Comisia pentru sănătate și Comisia pentru afaceri europene` → 3 committees ✓
- `în comun cu Comisia pentru muncă, sănătate și educație` → 1 committee (subject-list `sănătate, educație` chunks don't carry the `Comisia` literal prefix) ✓
- `în comun cu Comisia juridică, comisia a deliberat` → 1 committee (verb-phrase `comisia a deliberat` filtered by the connector check) ✓

**Bill-review false-positive filter**: when the `în comun cu` trigger is preceded within 30 chars by `raport / aviz / sesizare / fond / studiu / raportor`, the match is dropped. These forms (`raport comun cu`, `sesizare în comun cu`, `fond comun cu`) annotate joint *bill review* on an agenda item, not joint *meetings* — and the schema's `joint_with[]` lives on `CommitteeMeeting` so they don't belong here.

`chamber` is per-chunk: `din Camera Deputaților` / `a Camerei Deputaților` / `din cadrul Camerei Deputaților` → `camera`; `din Senat[ul]` / `a Senatului` / `din cadrul Senatului` → `senat`; else null. Trailing-chamber inheritance: when a list has a single chamber tail at the end (`Comisia X, Comisia Y și Comisia Z din Senat`), all chunks inherit that chamber; explicit per-chunk chambers (`Comisia X din Camera Deputaților, Comisia Y din Senat`) override.

Production smoke: **298 (31%) docs populate joint_with[], 891 total entries** (post-bill-review-FP-filter; was 1,359 pre-filter, 50% of which were joint-bill-review false positives).

#### `committee.kind = special_joint | inquiry_joint`

`_classify_kind` extends the v0.1 enum to graduate joint Camera+Senat permanent committees from `permanent` to `special_joint`. The joint marker fires on `\bcomun[ăaã]\b` (the `comună` modifier in `Comisia permanentă comună a Camerei...`) OR `Camerei Deputaților și Senatului` (the co-anchor used by some pre-2010 names that omit the explicit `comună`). The cohort: ~10-30 docs per year referencing the UNESCO permanent joint committee, the Statutul Deputaților și Senatorilor permanent joint committee, the securitate națională permanent joint committee, plus the Comisia comună de revizuire a Constituției (1993).

Production smoke: **262 committee blocks classified as `special_joint`** (was 0 under v0.1), **0 inquiry_joint** (no joint inquiry committees observed in the corpus).

#### Tabular agenda fallback

`_build_tabular_agenda(block, ...)` runs only when `_split_agenda` returns 0 numbered items. It walks the block line-by-line; after a header row matching `|Nr|...|PL-x|...` (or any of the `Titlu` / `Scopul` / `Rezoluție` co-anchors), subsequent table rows are parsed by content fingerprint:

- First numeric cell → `ordinal`
- Cell containing a `PL-x` / `Pl-x` token → `primary_references[]` (via `parse_primary_references`)
- Cell starting with `Raport` / `Aviz` / `Studiu` / `Proiect de opinie` / `Amânare` → role/output_type
- Cell starting with `În urma...` / `Aprobat` / `Respin[gs]` / `cu majoritate / unanimitate de voturi` → `outcome_text`
- Longest remaining cell → `title`

This is robust to column re-orderings and variable inter-cell empty padding (the 2025 corpus uses 5–14 cells per row depending on PDF→MD pagination quirks). Production smoke on 2024+ cohort (85 docs): **88% of committees now have populated `agenda_items`** (was ~75% under v0.1's narrative-only parser).

### Coverage and corpus stability

v0.2.0 production smoke on all 976 c-suffix docs: **0 errors**, **mean coverage 0.9972** (v0.1: 0.9972), **median 0.9997** (v0.1: 0.9997), **p25 0.9985** (v0.1: 0.9985), **min 0.886** (v0.1: 0.886), **0 docs below 0.85**. Coverage is structurally invariant — the new parsers fill in fields *inside* the partition's already-claimed record spans, so adding them doesn't move the coverage needle. The fixture floor (0.80) is unchanged; per-fixture coverage is identical to v0.1.

### What's still deferred to v0.3+

- **Per-day meeting splits** — v0.1/0.2 emit one `meetings[]` entry per committee block even when the synthesis covers 4 days. v0.3 may split when per-day rosters genuinely diverge.
- **Tabular roster intra_committee_role** — the tabular roster form doesn't natively encode roles (only the per-day numbered form does); inferring roles from chair signature + committee role tables is a v0.3 goal.
- **Roster ⇄ chair cross-link** — when a roster entry name matches the committee's `chair` Speaker, the entry's `intra_committee_role` should auto-fill to `președinte`. v0.3.
- **Joint chamber discrimination per chunk** — currently the `chamber` field is detected once at the clause level (`din Camera Deputaților` / `din Senat[ul]`) and applied to every joint partner. The corpus has occasional mixed-chamber clauses (`Comisia X din Camera Deputaților, Comisia Y din Senat`) where this loses information. Acceptable for v0.2 — the population is small.

## Extract pipeline — `report_facsimile`

v0.1 ships the fifth — and final — per-type extractor. Cohort: 52 docs across 2014-2024, all `R`-suffix MO Partea II issues containing annual / activity reports from constitutional bodies (CSAT, SRI, SIE, BNR, ANCOM, ANRE, Avocatul Poporului, Consiliul Legislativ, SRTv/SRR, Curtea de Conturi, etc.) reproduced verbatim. Single-file extractor at `src/monitorul_ii/extraction/extractors/report_facsimile.py` — under 350 LOC; the body shape is intentionally minimal (report metadata + heading outline + excerpt), the lightest of the five per-type extractors.

### Why "minimal body shape"

The spec is explicit: "the full text remains in the sidecar markdown." A `report_facsimile` document IS the report — it's not extracted as records of speeches / votes / agenda items because the report's internal structure varies by issuing institution (CSAT chapters, SRI capitole, ANCOM ordered sections — no shared shape). Forcing a structured record-array would either (a) over-fit one institution's outline, or (b) produce nulls and noise. The pragmatic choice: extract metadata + a heading outline so consumers can search-and-snippet, leave the body verbatim, defer institutional sub-shape extraction to specialised tools per institution.

This shapes the coverage strategy: a single record claim spans the entire `(RAPOARTE DE ACTIVITATE)` genre marker through the trailing footer (or EOF). The full report content gets claimed once as one record, no per-section invention. Combined with rf-specific boilerplate (universal page-running headers, NOTĂ disclaimer, SUMAR block), coverage clears 0.97 on every doc tested.

### Title harvesting + issuing-body discrimination

Title hunt order: SUMAR row first (cleanest single-line form), `## **RAPORT ...**` body heading second.

**v0.2.0 surface-form expansion.** v0.1.0 covered four SUMAR-row tail forms (`(în|pe) anul YYYY`); v0.2.0 extends the year-tail anchor to a five-cohort alternation that covers every observed surface across the 52-doc corpus:

```
Raportul X privind activitatea desfășurată în anul YYYY     (CSAT — preposition before X)
Raport privind activitatea desfășurată de X în anul YYYY    (SRI/SIE)
Raport asupra activității desfășurate de X în anul YYYY     (Consiliul Legislativ)
Raport de activitate al X (pe|în|pentru) anul YYYY          (SRTv/SRR, ANRE, ANCOM-pentru, ASF-pentru)
Raportul X (privind|asupra) <topic> ... din [DD] month YYYY (AEP election-day form)
Raportul privind activitatea X în anul YYYY                 (AEP — no `desfășurată de`)
```

The `pentru anul YYYY` extension graduates 5 ANCOM docs + 2 ASF docs + 1 SRTv doc that v0.1 missed because their SUMAR rows used the alternative connector. The `din [DD] month YYYY` form covers AEP election reports whose SUMAR ties the report to a specific election day rather than a calendar year — 3 docs across 2014. The `Raportul privind activitatea X` (no `desfășurată de`) and the broadened `Raportul X (privind|asupra) <topic>` patterns (was `Raportul X privind activitatea desfășurată` in v0.1) handle 6 AEP docs.

**`<br>` linebreak residue.** MD-converted multi-line SUMAR table cells often render the row body with `<br>` between the institution name and the year-tail (e.g. `Raportul de activitate al ANRE<br>pe anul 2013` for ANRE 13R/14R-2016). Pre-v0.2 the title regex matched but `_extract_issuing_body`'s `\s+pe anul` lookahead failed against `<br>pe anul`. v0.2 strips `<br>` in `_clean_title` (so the canonical title is reader-friendly) and again in `_extract_issuing_body` (defensive — handles fallback titles too).

**5-pattern issuing-body walk** (priority-ordered):

1. `Raport(ul) privind activitatea desfășurată de X` — SRI/SIE form.
2. `Raport(ul) privind activitatea X` *without* `desfășurată` — AEP form (negative-lookahead `(?!desf...)` guards pattern 1).
3. `Raport(ul) asupra activității desfășurate de X` — Consiliul Legislativ.
4. `Raport(ul) de activitate al X (pe|în|pentru) anul YYYY` — SRTv/SRR/ANCOM/ANRE/ANAD/ASF.
5. `Raportul X (privind|asupra) <topic>` — CSAT, plus AEP election-report variants whose topic is `alegerile`/`referendumul`/`organizarea`/`determinarea`. Non-greedy body capture stops at the first `privind` / `asupra`.

Each pattern stops at the year-tail or topic preposition to keep the body label tight.

Reporting period: `în anul YYYY` / `pe anul YYYY` / `pentru anul YYYY` → Jan 1 – Dec 31 of YYYY. Multi-year `în perioada YYYY-YYYY` widens both endpoints (rare; only a couple of CSAT bi-annuals in the cohort). **v0.2.1 date-form fallback**: when the title carries `din [DD] month YYYY` instead of an annual form (AEP election-day reports — `din 9 decembrie 2012`, `din iunie 2012`, `din 29 iulie 2012`), the year inside the date becomes the reporting year. Title-scoped because body-internal dates are too noisy to treat as reporting-year anchors.

### Reception session

Every R-suffix doc in the corpus is received in joint session — Parliament receives the report at a `ȘEDINȚE COMUNE ALE CAMEREI DEPUTAȚILOR ȘI SENATULUI` session. The body line `## **Ședința din ziua de DD month YYYY**` (cedilla-tolerant for older docs) gives the reception date; frontmatter `session_date` is the fallback. `session_kind` = `joint` when the joint header is present, else falls back to frontmatter `chamber`. `received_in_document` (back-link to the receiving stenogram's `mo://YYYY/PART/ISSUE` document_id) stays null in v0.1 — the cross-document linker is a v0.2 pass that joins receiving stenograms to received reports.

### Coverage targets and measurement

Per Q9: **discovery margin 0.85**, **test fixture floor 0.80**, **mean target 0.90 documented (ungated)**.

Nine hand-picked fixtures span the cohort eras and (v0.2.0) the recovered surface forms:

| Fixture | Layout | Coverage |
|---|---|---|
| `2014-01-20_MO-PII-1R-2014.md` | CSAT 2010, image-only PDF (page-residue body) | 0.970 |
| `2014-04-24_MO-PII-13R-2014.md` | SRI 2007, modern body with H2 chapter headings | 0.999 |
| `2014-06-16_MO-PII-19R-2014.md` | AEP local-elections 2012; SUMAR `din iunie 2012` + `Raportul X asupra <topic>` (v0.2) | 0.97+ |
| `2014-06-17_MO-PII-21R-2014.md` | AEP referendum 2012; SUMAR `din 29 iulie 2012` + `Raportul X privind <topic>` (v0.2) | 0.97+ |
| `2016-05-23_MO-PII-4R-2016.md` | ANCOM 2010; SUMAR `pentru anul 2010` (v0.2) | 0.97+ |
| `2016-05-30_MO-PII-13R-2016.md` | ANRE 2013; title body with `<br>pe anul 2013` residue (v0.2) | 0.97+ |
| `2016-07-27_MO-PII-19R-2016.md` | AEP 2013; `Raportul privind activitatea X` no-`desfășurată` form (v0.2) | 0.97+ |
| `2017-10-24_MO-PII-1R-2017.md` | Consiliul Legislativ 2010, mid-cohort | 0.999 |
| `2024-04-09_MO-PII-1R-2024.md` | ANCOM 2019, modern Camera-published, 100+ headings | 0.9998 |

Discovery-loop sweep over all 52 R-suffix MDs (2014-2024):

| Metric | Value |
|---|---|
| Extracted | 52 / 52 (zero errors, zero classify mismatches) |
| Mean | 0.9985 |
| Median | 0.9994 |
| p10 | 0.9972 |
| p25 | 0.9989 |
| Min | 0.9700 |
| Below 0.85 | 0 |

Tightest coverage band of the five per-type extractors — the partition-trivial design (one big record claim) means there's no room for layout-specific failure modes to creep in. The 2014 image-only docs (lowest coverage at 0.97) hit that floor because their body is mostly page-running-header lines and the heading regex finds nothing useful inside; 0.97 is essentially the theoretical max for those given the source PDF's lossy conversion.

### Helper graduations at v0.1

| Helper | Pre-v0.1 | report_facsimile ship | Why |
|---|---|---|---|
| `boilerplate` | 0.1.0 | 0.1.0 | RF-specific boilerplate stayed in the per-type module |
| `coverage` | 0.1.0 | 0.1.0 | No changes |
| `references` | 0.2.0 | 0.2.0 | Not used (reports don't carry bill cites in their metadata) |
| `speakers` | 0.2.0 | 0.2.0 | Not used (no speakers in report metadata; full text stays unstructured) |
| `topics` | 0.1.0 | 0.1.0 | Not used (institutional reports don't map cleanly to the closed plenary topic set) |
| `report_facsimile` | (new key) | **0.1.0** | First per-type ship |

Adding the `report_facsimile` key to `EXTRACTOR_VERSIONS` triggers re-extraction of every existing sidecar on first run because the cached `extractor_versions` dict on those sidecars now lacks the new key (Q11 conservative-by-design contract). Acceptable — the corpus is now small enough that a full re-extract is fast and the version-keying contract is the load-bearing safety net.

### Schema deltas at v1.8.0

Strict body shape for `report_facsimile` replaced the v1.7.0 `PendingBody` placeholder. New `$defs`: `ReportFacsimileBody`, `ReportMetadata`, `ReportingPeriod`, `ReceivedAt`. Reuses existing `Heading` $def from `OtherBody`. With this bump, all six document types have strict body shapes; `PendingBody` is no longer referenced by the discriminator (kept as a $def placeholder for future types that may need staged graduation).

### Schema deltas at v1.9.0

Six long-tail reference variants graduated from `unknown` to strict shapes — `motion`, `court_decision`, `constitution`, `regulation`, `eu_doc`, `treaty`. New `$defs`: `MotionReference`, `CourtDecisionReference`, `ConstitutionReference`, `RegulationReference`, `EuDocReference`, `TreatyReference`. The `Reference` discriminated `oneOf` grew from 7 to 13 variants (12 strict + `unknown`). v0.4.0 of `references.py` then enabled the `unknown` emitter in `parse_mentioned_references` so cite-shaped spans that don't classify into any strict variant surface in the body's `references_mentioned[]` array (with hint enum `law-ish | court-ish | eu-doc-ish | other`). Production sweep on 5551 docs surfaced 125,822 unknowns — 122,554 law-ish (95,801 of those are bare `art. N` cross-references that require a future cross-doc linker, not a regex variant), 3,228 other (Acordul / Protocolul / Carta — graduated in v1.10.0), 40 court-ish.

### Schema deltas at v1.10.0

Tier-1 reference graduations completed. Three changes, all additive:

1. **`code` variant** — Romanian named codes promoted out of `unknown.hint=law-ish`. New $def `CodeReference` with required `code_kind` enum (16 entries: muncii / fiscal / civil / penal / procedura_civila / procedura_penala / administrativ / silvic / aerian / rutier / vamal / comercial / familiei / navigatiei / consumului / insolventei) and optional `article` field. Two regex forms: forward `Codul[ui] X (art. N)?` and reverse `art. N din Codul[ui] X`. Diacritic-folded enum keeps the schema stable across Unicode, cedilla, and mojibake input variants. Discriminator `oneOf` grew from 13 to 14 variants (13 strict + `unknown`). Production count: 20,481 code refs — fiscal 6,947, penal 4,420, muncii 2,964, silvic 1,367, procedura_penala 1,358, administrativ 933, civil 907, procedura_civila 878, others tail.

2. **Broaden `treaty` variant** — Acordul/Acordului and Protocolul/Protocolului forms (which v1.9.0 emitted as `unknown.hint=other`) now flow to the strict `treaty` variant. Connectors: `Acordul (de la / dintre / privind / asupra) X`, `Protocolul (de la / adițional / opțional / nr. N) X`. The `Carta` enum widened to also catch `Carta europeană a autonomiei locale`, `Carta europeană a limbilor regionale`, `Carta albă`, plus the broader `Carta de la X` opener. Schema's `TreatyReference.name` field is just `string, minLength: 1` so no schema bump was needed for the Carta widening. **Boundary-lookahead extension**: the existing `_TREATY_BOUNDARY_LA` was extended to also terminate on `și`, `iar`, `dar`, `sau`, `precum` so `Acordul de la X și Protocolul de la Y` cleanly emits two refs (avoids the over-greedy name-capture failure mode noted in the v0.3.0 ship-notes). Production count: treaty grew 4,378 → 10,051 (+5,673; Acordul 4,921 / Protocolul 2,255 / Tratatul 1,596 / Convenția 1,009 / Carta 270).

3. **Best-effort `subject` extraction** — populates the previously-null `subject` field on `bill`, `law`, and `parliamentary_resolution` from the surrounding sentence. New helper `_extract_subject(text, ref_start, ref_end)` scans a 200-char post-cite window for one of five connector phrases (longest-first to favour specificity): `pentru aprobarea/modificarea/completarea X`, `cu privire la X`, `referitoare la X`, `asupra X`, `privind X`. Capture terminates on punctuation (`.,;:\n`) or a clause-boundary conjunction (`și`, `iar`, `dar`, `sau`, `precum`) — same boundary set as the broadened treaty. The earliest connector wins (the closest descriptor is the most likely subject); if multiple connectors fire, the leftmost one is kept. Cap at 200 chars; trailing punctuation/em-dash stripped; null when no connector fires. The schema's `BillReference` had no `subject` slot before v1.10.0 — added as `["string", "null"]`, optional. `LawReference.subject` and `ParliamentaryResolutionReference.subject` were already declared but always null through v0.4.0. Production sweep fill rates: bill 50.5% (36,530 / 72,360), law 76.8% (72,567 / 94,477), parliamentary_resolution 88.9% (550 / 619). Bill's lower rate is structural — bill cites in agenda items frequently appear without a connector phrase (the title is on a separate line), so the post-cite window has no connector to match. Trade-off chosen: stay conservative on the regex (don't speculate / hallucinate). Older docs (pre-2008) often surface mojibake characters in the captured subject (`Ordonanþei` instead of `Ordonanței`) — that's a pre-existing OCR artefact in the source MDs, not a Tier-1 issue.

The schema bump and `references.py` v0.5.0 cascade through the version-aware idempotency gate: every existing sidecar has its `schema_version` and/or `extractor_versions.references` mismatch the new code, triggering automatic re-extraction on the next `monitorul-ii extract` run. Plenary extractor versions also bumped (`plenary_stenogram` 0.2.2 → 0.2.3, `plenary_joint_session` 0.2.2 → 0.2.3) for explicit attribution of the body-shape change (the new `subject` field appears inside `agenda_items[].primary_references[]` and `activities[].references_mentioned[]`).

### What's deferred to v0.2+

- **`received_in_document` cross-document linker.** **Shipped (v0.1.x)** — see "Cross-document linker" below.
- **`issuing_body_normalized`** — currently null. The institutional-bodies registry (CSAT, SRI, SIE, BNR, ICR, Avocatul Poporului, Consiliul Legislativ, SRTv, SRR, ANCOM, ANRE, Curtea de Conturi, etc. — small enum, ~20 entries) is the canonical normalisation source. Once that registry exists, a one-script backfill on every report sidecar populates the slot.
- **Institution-specific outline detection.** The current heading extractor pulls every `## **...**` heading minus a skip-list. CSAT reports use `CAPITOLUL I/II/...` outlining; SRI uses `OBIECTIVELE PRIORITARE`; ANCOM uses numbered `N.M.K.L` decimal sections. v0.2 could add per-issuer outline parsers that classify each heading as `chapter` / `section` / `appendix` etc. — but the discovery loop hasn't surfaced a query that needs it yet.

## Cross-reference linkers

A separate post-extract stage that fills back-pointer / anchor-pointer fields no per-type extractor can populate at single-doc time. Three passes ship as of v0.2.0 of `linker.py` + v0.1.0 of `cross_reference_linker.py`:

1. **report→session** (cross-doc, linker.py v0.1.0+) — fills `report_facsimile.body.report.received_at.received_in_document` with the `mo://YYYY/PART/ISSUE` document_id of the joint-session (or single-chamber) stenogram that received the report.
2. **vote-pair** (cross-doc, linker.py v0.2.0+) — pairs a deferred vote (`outcome=deferred`) in stenogram N with its resolving vote in a later stenogram M. Forward link: `vote.defers_to = "<doc_id of M>"` on the deferring vote. Back-link: `vote.resolves = ["<doc_id of N>", ...]` on the resolver (a vote can resolve multiple prior deferrals when the chair batches several into one final-vote round).
3. **xref / cross-reference** (intra-doc, cross_reference_linker.py v0.1.0+) — resolves bare `art. N` unknown references in plenary sidecars to the most-recent preceding non-unknown reference IN THE SAME REFERENCE LIST. Anchor pointer: `unknown.resolved_to.char_offsets = <anchor's char_offsets in the same list>`.

Code: `src/monitorul_ii/extraction/linker.py` (passes 1+2) and `src/monitorul_ii/extraction/cross_reference_linker.py` (pass 3). CLI surface: `monitorul-ii link <paths> [--force] [--dry-run] [--report-only | --vote-only | --xref-only]`.

### Why a separate subcommand instead of inline-in-extract

Extract is single-pass per-MD. Linking needs the global picture: build an index of all stenogram sidecars, then resolve cross-doc references. Forcing extract to know about other sidecars at extract-time would break two contracts: (a) per-MD parallelism becomes harder (each worker would need to read the full sidecar set), and (b) the dispatcher's "single source of truth for body content" guarantee gets muddied. Splitting into `extract` (writes body content from MD) + `link` (writes cross-doc back-pointers) keeps each pass small and re-runnable.

The natural workflow is **extract first, then link**:

```
$ uv run monitorul-ii extract pdfs/        # writes all sidecars; *.defers_to=null, *.received_in_document=null
$ uv run monitorul-ii link pdfs/           # both passes
```

### Pass 1 — report→session: indexing strategy

`build_session_index(sidecars)` walks the input list once, reading each sidecar's `document_type` + `metadata.session_date` (with `metadata.published` fallback). Two document types act as receiving sessions: `plenary_joint_session` (priority 0, the dominant case — every R-suffix doc observed in the corpus is received in joint session) and `plenary_stenogram` (priority 1, single-chamber receptions for completeness, since the schema's `received_at.session_kind` enum allows `camera` / `senat`). When both share a date, joint wins.

The index is a flat `dict[date_str, document_id]`. Date collision within the same priority falls back to first-seen — defensible for 2013-2025 corpus; if multi-session days become a query problem, a future bump can promote the value to a `dict[date, list[document_id]]` and let the caller pick.

`link_report(path, *, session_index, force, write)` reads a single report_facsimile sidecar, looks up its `received_at.session_date` in the index, writes the matched document_id into `received_in_document`. Pre-write schema validation; atomic write via `.part` rename. Self-link prevention is a defensive guard (the index excludes report_facsimile, but if the corpus ever changes shape, we don't write a self-pointer).

`link_all(sidecars, *, force, write)` is the iterator entry point: two-pass over the input list (build index, then yield one `LinkResult` per report). Non-report sidecars are silently filtered.

### Pass 2 — vote-pair: matching algorithm (v0.2.0)

The hard problem is "did vote X in doc N resolve in vote Y of doc M?". The matching key is derived per-vote from the parent agenda item:

1. **Bill cite** — first entry in `agenda_items[].primary_references[]` of `type=bill`. Format: `f"bill:{number}/{year}"`. PL-x / L is the canonical bill identifier in Romanian parliamentary procedure: the same legislative initiative carries the same cite from first reading through final vote, so two docs discussing the same bill always share the same PL-x.
2. **Motion title hash** — for motion-class votes (parent agenda has a `motion` ref in primary_references[]) WITH a quoted title at least 12 chars long, use `f"motion:{motion_kind}:{title-hash}"`. Bare `Moțiunea simplă` without a quoted title is too generic and stays unlinked.

There is intentionally **no fallback to other ref types or agenda-title hashes**. Three iterations of v0.2.0 progressively tightened the keyspace based on 5,551-doc smoke spot-checks:

1. **Iteration 1** — included an agenda-title-hash fallback. 2,007 / 3,886 distinct keys were title-hashes; spot-check found 9/10 pairs were false positives across generic procedural items (`Ședința`, `Aprobarea ordinii de zi`, `Informare cu privire la inițiativele legislative`) whose titles repeat verbatim every session. **1,181 pairs written, mostly noise.**
2. **Iteration 2** — dropped the title-hash fallback, kept law / oug / og / parliamentary_resolution / chamber_resolution cites alongside bills. Spot-check found 6/10 pairs from `law:47/1992` (Constitutional Court procedural law, cited every time the Senate considers a CCR referral) collided unrelated weekly procedural notes; `law:286/2009` (Criminal Code) and `law:95/2006` (Health Code) collided independent amendment debates that happened to share the underlying law cite. **191 pairs written, still ~60% noise.**
3. **Iteration 3 (shipped)** — restricted to `bill` and `motion(quoted-title)` keys only. Bill cites are unique per legislative initiative; law / oug / etc. are inherently shared across multiple unrelated bills. The bill key also includes the prefix (`bill:PL-x:N/Y` vs `bill:L:N/Y`) because PL-x (Camera-originated) and L (Senate-originated) share number-spaces only by coincidence — a flat `bill:N/Y` key would cross-collide them. **The linker's job is to be precise, not aggressive — votes that don't carry a `bill` ref stay unlinked.** False matches in the back-link sets would silently corrupt downstream queries on "what resolved this deferral", which is the whole point of the linker.

**Production smoke (5,551 docs, v0.2.0)**: 12,257 votes indexed across 2,089 distinct bill keys (2,087 bill + 2 motion). 1,692 deferred votes, 15 forward links written, 12 back-link sites (one of which receives multiple origins), 11 multi-deferral chains. 24 plenary sidecars touched, 0 schema errors. Coverage held p50=0.998, mean=0.932; the 12 pre-existing agenda errors stayed unchanged.

**Post-fix corpus smoke (after `agenda.py` v0.2.5 title-contamination clip)**: 10,717 votes indexed (down from 12,257 — fewer bill keys per agenda after the misattribution drop), 12 forward links + 10 back-link sites, 22 plenary sidecars touched, 0 schema errors. The 12 pre-existing extractor errors stayed at 12. Coverage held at p50=0.998 / mean=0.9311. **10/10 random pairs verified as legitimate same-bill cross-doc deferrals — FP rate 0% in the spot-check, well under the <10% acceptance bar.**

**Post-references-v0.6.0 smoke**: errors=0 (was 12). The 12 docs that previously produced `.rejected.json` files now extract cleanly under the OOR-year demotion guard; their `bill` / `law` / `oug` / `og` cites whose parsed year fell outside `[1990, 2100]` (or `[1900, 2100]` for law) are now demoted to `unknown.law-ish` / `unknown.court-ish` instead of failing schema validation. The newly-valid sidecars participate in the linker / backfill passes like any other plenary sidecar; the linker's `bill:N/Y` keying is unaffected by the demotion (a demoted reference doesn't carry a number/year, so it doesn't generate a key). Stale `.rejected.json` files are auto-removed by the dispatcher's atomic-write path on successful re-extract.

The coverage gap on procedural-item cross-doc deferrals (e.g., re-tabled CCR-referral notes, deferred ordinea-de-zi approvals) is acceptable because such cross-doc deferrals are rare and a downstream consumer can re-query by document_date proximity if the use case ever needs them.

### Known issues — read before iterating

The v0.2.0 production smoke surfaced several caveats. Item 1 below was the dominant FP driver; `agenda.py` v0.2.5 (title-contamination clip) addressed it upstream, and the post-fix smoke confirms the FP rate dropped to 0/10 in spot-check. Items 2–7 still apply.

1. **(RESOLVED in `agenda.py` v0.2.5; extended in v0.2.8)** Pre-fix spot-check on v0.2.0's smoke surfaced a ~40% false-positive rate driven by the deferring agenda's title not matching the bill cite that landed in its `primary_references[]` — e.g., an agenda titled `Declarații politice` carrying a `bill:L:124/2010` ref, or an `Informare privind distribuirea unor documente la casetele deputaților` carrying a `bill:PL-x:103/2010` ref. Root cause was the SUMAR parser's `_SUMAR_ITEM_RE` over-capturing across an item boundary when PyMuPDF's table rendering broke (table block ending mid-row + plain-text continuation). `_clip_contamination_tail` now detects three SUMAR-row-boundary patterns (page-list + next-ordinal, `--- ---` artifact, markdown bullet ordinal) and clips the captured `rest` at the earliest boundary. **v0.2.8** lifts the Tier 2 trailing `\s+` requirement to `(?:\s+|$)`: a corpus-wide regression sweep surfaced 186 over-2000-era titles (mojibake-heavy Senate stenograms) whose `--- ---` artifact sat at end-of-string after `_clean_sumar_title`'s `.strip()`, slipping past the original anchor. The fix recovers all 186 cases without changing Tier 1 / Tier 3 behavior. **Production effect**: total bill refs across agendas dropped 56,888 → 28,126 (-50.6%); procedural-prefix-with-bill-refs misattribution dropped 259 → 14 (-94.6%); linker FP rate ~40% → 0/10 in 10-pair spot-check; coverage and schema-error counts held. Downstream consumers can now treat `defers_to` / `resolves` with substantially more confidence, though the pair count is small (12 forward / 10 back) so absolute precision is fragile to small distribution shifts. See `extractors/plenary/agenda.py` § "Title contamination & primary-references attribution (v0.2.5)" for the design call (option (a) — title-only with the title cleaned at source — and the rejected alternatives).

2. **Multi-chain ratio remains high (8 of 12 forward links, ~67%).** Most cross-doc deferrals still point at *another* deferred vote rather than directly at a resolver. Either chains genuinely run 3–4 levels deep before resolving, or there's a subset of bills that get punted indefinitely and the resolver never lands within the 60-day window. Worth instrumenting per-chain depth (max chain length, mean chain length, chains-truncated-by-window count) in a follow-up smoke. If many chains exceed 60 days, consider widening the window OR explicitly marking "truncated chain — no resolver found in window" so consumers can distinguish "still pending" from "no resolver".

3. **Same-agenda multiple-amendment votes inflate the forward-link count.** When one agenda item has multiple deferred amendment votes (`act#24` and `act#38` both deferring), each gets its own forward link, even though they collectively represent one cross-doc relation. The headline count overstates distinct cross-doc bill relations. A future bump can de-dupe forward links per `(deferring_doc_id, agenda_index)` to surface a separate "distinct cross-doc bill relations" metric.

4. **Idempotency was unit-tested but not corpus-smoked.** `test_link_vote_idempotent_skips_when_already_linked` covers the synthetic case; the production smoke script clears prior linker output before each iteration to measure from scratch. A future regression run should explicitly invoke `monitorul-ii link` twice in a row on the corpus and assert that the second pass yields all-skip with `pairs_written=0` / `backlinks_written=0`.

5. **Spot-check seed is fixed (`random.seed(42)`).** Iterations sample the same 10 pairs each run — good for diff comparison across linker tweaks, blind to other parts of the distribution. A future audit should re-sample with a fresh seed and confirm the precision rate holds.

6. **(RESOLVED in `references.py` v0.6.0)** The 12 pre-existing extractor schema-validation errors that left their MDs without `*.extraction.json` files at all are now closed by the OOR-year demotion guard. All 12 plenary docs now produce valid sidecars and participate in the linker / backfill passes like any other.

7. **Pair count is at the low end of "dozens to hundreds".** 15 forward links is technically dozens but small. The bill-only key correctly trades coverage for precision; if a future use case needs higher recall on cross-doc relations (e.g., committee-report-to-final-vote chains, motion-to-resolution chains), add a *separate* keying strategy for that specific relation rather than re-loosening the bill-only key — looser keys silently corrupted the back-link sets in v0.2.0 iterations 1 and 2.

**Window** — a deferred vote in doc N at session date D only matches candidate resolvers in docs M with `D < session_date(M) ≤ D + 60 days` (`DEFERRAL_WINDOW_DAYS=60`). Most parliamentary deferrals resolve within 1–2 weeks; beyond 60 days the same key is more likely a different debate cycle (re-introduced bills after a recess, recurring committee reports).

**Earliest-resolver-wins** — `build_vote_index` groups votes by match key and sorts each group by `session_date` ascending. For a given deferred vote at index `i`, the immediate next entry within the window is the forward link target (whether it's another deferral or a resolver). The chain terminates at the first non-deferred vote.

**Multi-deferral chain** — when A defers to B and B defers to C and C resolves: the forward links are `A→B, B→C`; the back-link on C is `[A, B]` (chain reversal — every prior deferral that ultimately landed on C). `_build_pairs` walks each chain to its terminal non-deferred vote and accumulates origin doc_ids on the resolver's back-link entry.

**Same-day collisions** — within a single session date, votes are sorted by `document_id` to keep the iteration deterministic, but same-day cross-doc deferrals shouldn't happen (each session has at most one stenogram per chamber, and joint > single-chamber priority is enforced upstream by document classification). If they do occur, the linear scan still terminates correctly because the next-day entry takes precedence.

`link_vote(path, *, forward_links, back_links, force, write)` writes the pair onto one stenogram sidecar. Pre-write schema validation; atomic write. Idempotent: skips when the forward/back link is already populated; `force=True` overwrites stale entries (useful after a stenogram cohort re-extract that may have rewritten doc_ids for some receivers — though `document_id` is deterministic in practice).

`link_all_votes(sidecars, *, force, write)` is the iterator entry point: three-pass over the input list (build index → derive pairs → write to touched sidecars). Sidecars with no vote-pair updates are skipped silently (no result yielded).

### Schema impact (v1.11.0)

`VoteActivity.defers_to` (`["string", "null"]`) and `VoteActivity.resolves` (`anyOf [array of string, null]`) added to the strict body shape. Both are required-with-default — the extractor emits `defers_to: null` and `resolves: []` at extract time so the schema validates without linker output present. The linker mutates these fields in place. `additionalProperties: false` on `VoteActivity` means the schema bump was unavoidable — the user-facing v0.2.0 narrative ("just thread the linker into the existing slots") doesn't fit the actual schema state, so v1.11.0 records the additive expansion.

`DeferralActivity.defers_to` (a separate string|null on a separate `deferral` activity type, present since v1.0.0) is unchanged. That slot remains for in-doc batched-vote pointers (chair says "Aceasta rămâne pentru votul final" — the deferral activity points at the final-vote item within the same agenda); the v0.2.0 linker does NOT touch it.

### Versioning contract

`LINKER_VERSION = "0.2.0"` lives in `linker.py` but is **not** propagated into the sidecar's `extraction.extractor_versions` dict. The version-keying contract there uses exact-match (`_versions_current` returns False on any key mismatch); adding linker as a key would force extractor re-runs whenever the linker bumped. Instead, linker output lives entirely inside body content. Tradeoff: re-extracting a sidecar (extractor version bump → re-extract per the cache-invalidation contract) clobbers `received_in_document` / `defers_to` / `resolves`. Recovery: `monitorul-ii link` is fast (~1ms per doc for the report pass; the vote pass is bounded by index construction which is O(N) over the corpus) and re-runnable. Adding `defers_to: null` and `resolves: []` to every vote at extract time means the schema validates without linker output and no re-extract is needed when the linker writes pairs.

### Pass 3 — cross-reference / xref linker (v0.1.0)

The third pass is intra-document, sister to passes 1+2 in shape (atomic write, pre-write schema validation, idempotency, schema-version bump on write) but joining anchor-to-unknown over a single reference list rather than across documents.

#### Why it exists

`references.py` v0.4.0+ emits `UnknownReference` entries with `hint="law-ish"` for cite-shaped spans that don't classify into a strict variant. The dominant unclassified bucket on the 5,551-doc production corpus is bare `art. N` cross-references — phrases like `"art. 25 alin. (3)"` or `"articolul 14"` — that lack an issuer (no `Legea`, no `Codul`, no `OUG` next to them). On their own these are useless; in context they refer to articles of a law/code/bill cited elsewhere in the same parent (same agenda title or same speech body). The xref pass walks every plenary sidecar, finds every art-N unknown, locates its anchor, and writes a pointer.

#### Design call: same-list scoping (load-bearing)

Each reference list (`agenda_items[].primary_references` / `agenda_items[].activities[].references_mentioned`) is its own coordinate system. The references parsers in `references.py` (`parse_primary_references`, `parse_mentioned_references`) are called by the per-type extractors WITHOUT a `base_offset` argument — that's the contract — so their output offsets index into the LOCAL parent string (an agenda title, a speech text), NOT the body-global text.

This made the first two design iterations of the linker fail: they tried sentence/paragraph/activity-span scoping using body-global offsets read from the sidecar, then computed between-text against the MD body. The anchor's stored offset of, say, `[78, 95]` was a position in the agenda title (where the law cite is), not in the body — so the linker compared nonsensical body slices and emitted thousands of garbage resolutions. Spot-check precision was ~20%.

The fix is to scope anchor lookup to the SAME LIST as the unknown. By construction, every ref in a given `primary_references` array shares the same coordinate system (the agenda title's text), and every ref in a given `references_mentioned` array shares the speech text's coordinates. Comparison is well-defined without touching the MD body.

The cost is recall: cross-list cases (an unknown art-N in a speech whose owning bill cite lives in the parent agenda's `primary_references[]`, NOT in the same speech's `references_mentioned[]`) stay unresolved. That's the dominant remaining unresolved bucket on the corpus (estimated >50% of the 81.5% unresolved fraction). A v0.2 future-work candidate is to walk each agenda_item, build a per-agenda anchor pool from its `primary_references[]` PLUS any speech-emitted bill cites, and resolve speech-level art-N unknowns into that pool. That's a structural join, not a regex change.

#### Anchor priority

Within a list, when multiple eligible non-unknown anchors precede the unknown:

1. **Most-recent-preceding wins** — the anchor with the largest `end` offset that's ≤ unknown's `start` offset.
2. **Tie-break at equal end offsets** — `code` > `law` > `bill` > `oug` > `og` > `regulation` > `constitution` > `chamber_resolution` > `parliamentary_resolution` > `treaty` > `court_decision` > `eu_doc` > `motion`. Codes (Codul muncii, Codul fiscal) are conventionally cited once and then referenced repeatedly via bare `art. N`; named laws follow the same pattern but get re-cited inline more often. Ties are rare — most refs end at distinct offsets.

#### Output shape

The unknown keeps its existing `type=unknown` shape and gains a `resolved_to: { char_offsets: [start, end] }` pointer at the anchor. Reasons against graduating the unknown into a fully-typed reference (e.g. promoting `art. N` near `Legea 95/2006` into a typed law-with-articles ref):

- The article number isn't on the original strict variant's schema, so graduation would require its own schema extension (article list on every variant).
- A stale or wrong link shouldn't corrupt the strict-variant pool — keeping unknowns as unknowns preserves the "uncommitted" semantics that downstream consumers can read.
- Same-list scoping means downstream consumers can recover the typed anchor cheaply: `unknown.resolved_to.char_offsets` → linear scan over the SAME `primary_references` / `references_mentioned` list for the ref with matching offsets.

The pointer's coordinates are LOCAL to the parent string, NOT body-global. Downstream consumers must look up the anchor inside the same list as the unknown.

#### Schema impact (v1.12.0)

`UnknownReference.resolved_to: anyOf [RefOffset, null]` added (additive). New `RefOffset` $def is a minimal pointer: `{ char_offsets: CharRange }`, no other fields. The xref linker bumps the sidecar's `schema_version` to 1.12.0 on every successful write — the new `resolved_to` field requires it, and bumping forward is safe because 1.12.0 is fully backwards-compatible with 1.11.0 (no fields removed, no required fields added). Existing 1.11.0 sidecars on disk are upgraded as the linker walks them; sidecars not touched by the linker stay at 1.11.0 until something else writes (typically the next `extract --force` after an extractor version bump).

`VoteActivity.defers_to` (`["string", "null"]`) and `VoteActivity.resolves` (`anyOf [array of string, null]`) are preserved from v1.11.0 — populated by the cross-doc linker, untouched by the xref pass.

#### Idempotency + force semantics

Same skip-on-populated rule as the cross-doc linker: a re-run with `force=False` skips every unknown that already carries a `resolved_to` value. `--force` does two things: (a) overwrite an existing `resolved_to` when the linker now finds a different anchor, AND (b) CLEAR a stale `resolved_to` when the new run finds NO anchor under the current rules. Without (b), a tightening of the linker rules (the v0.1.0 ship-day path was: tighten boundary detection, then tighten scope) leaves stale links alone and the only way to clean them is a manual sidecar rewrite. With (b), `--force` is the canonical "re-resolve under current scope" command.

#### Production sweep (5,551-doc corpus, v0.1.0)

| Metric                             | Value           |
|-----------------------------------|-----------------|
| Sidecars touched (linked + cleared) | 3,629 / 5,551 (65.4%)  |
| art-N unknowns total               | 110,453         |
| Resolved (resolved_to populated)   | 20,433 (18.5%)  |
| Unresolved (no same-list anchor)   | 90,020 (81.5%)  |
| Errors                             | 0               |
| Spot-check precision (random 20)   | ~85% (17/20)    |
| Idempotency on second run          | resolved=0      |

The 81.5% unresolved fraction is dominated by cross-list cases — speech-level art-N unknowns whose owning bill cite lives in the parent agenda's `primary_references[]`, not in the same speech's `references_mentioned`. Resolving those needs the agenda-aggregation join sketched above (v0.2 future work). The remainder is true no-anchor cases — SUMAR-area citations whose law cite never appeared in the same parent string.

#### Known caveats

1. **No cross-list resolution (v0.2 future).** Most semantic art-N references span agenda title → speech text or speech → speech across activities. v0.1's same-list-only is the precision floor; recall improves with agenda-aggregation.
2. **No anchor-relevance check.** Within a list, the linker picks the closest preceding anchor without checking whether the speaker is actually citing it. A multi-topic speech with multiple law cites will sometimes mis-anchor (~15% spot-check error rate is concentrated here).
3. **No forward-reference handling.** `art. 14 al legii care va fi adoptată` is common in committee debates — the law cite appears AFTER the article reference. The linker requires preceding anchors and stays null on these.
4. **Local-not-global offsets.** The `resolved_to.char_offsets` is in the SAME coordinate system as the unknown's offsets — the parent string, not the body. Downstream consumers must look up the anchor inside the SAME `primary_references` / `references_mentioned` list as the unknown. This is a feature for query simplicity (no body-side bookkeeping) and a footgun for naive "join unknown.resolved_to to body[start:end]".

#### Versioning contract

`XREF_LINKER_VERSION = "0.1.0"` lives in `cross_reference_linker.py` but is NOT propagated into the sidecar's `extraction.extractor_versions` dict — same Q11 conservative-by-design contract as the cross-doc linker. Re-extracting a sidecar clobbers `resolved_to`; re-running `monitorul-ii link --xref-only` recovers (the linker is fast — single walk per sidecar, no MD body access).

### CLI surface

`monitorul-ii link <paths> [--force] [--dry-run] [--report-only | --vote-only | --xref-only] [--bucket NAME | --no-upload]`. Path arguments are files or directories (non-recursive glob for `*.extraction.json`). Default runs all three passes; `--report-only`, `--vote-only`, and `--xref-only` are mutually exclusive selectors for a single pass. Output is one line per processed sidecar to stdout — `ok    <name>  -> mo://YYYY/PART/N` for report-pass linked, `ok    <name>  forward=N back=M` for vote-pass linked, `ok    <name>  resolved=N unresolved=M already=K` for xref-pass linked, `skip  <name>  (reason)` for skipped, `ERROR <name>  (reason)` for validation failures. Trailing summary on stdout (`linked=N skipped=M errors=K | s3 ...`); the xref pass also prints a final `[xref-pass] resolved=X unresolved=Y (Z% resolution rate)` line on stderr. Skip-reasons histogram on stderr when any skips occurred.

S3 mirror runs after each successful link when env vars are set: re-uploads the modified sidecar with `Content-Type: application/json`, overwriting the bucket copy. `--dry-run` skips both writes and uploads — useful for sanity-checking before a corpus-wide run.

## Registry-driven backfills (v0.1.0)

The fourth pipeline stage. Where `extract` produces typed records and `link` resolves cross-document references, **`backfill`** joins curated registries (small lookup tables maintained in tree at `src/monitorul_ii/registries/`) against the schema's `*_normalized` slots. The schema reserves these slots for canonical-id values that turn raw text into a key the downstream consumer can index by — the body that "Consiliului Suprem de Apărare a Țării" refers to in 2010 and "CSAT" refers to in 2024 are the same constitutional body, and querying by `issuing_body_normalized = 'csat'` recovers both.

### Architecture (sister to the linker)

`monitorul_ii.registries.<name>.json` is the data; `monitorul_ii.registries.__init__` exposes a `normalize_<kind>(raw) -> (id, matched_via)` function per registry; `monitorul_ii.extraction.backfills.<kind>_*` is the per-pass writer; `cli.py:cmd_backfill` is the orchestrator. Same atomic write + pre-write schema validation contract as the linker — backfills can never corrupt a sidecar; on schema failure the pre-write dict is rejected and the on-disk file is untouched.

There is intentionally **no fuzzy / Levenshtein tier**. Multiple bodies share long prefixes (`Agenția Națională ...`, `Consiliul Național ...`, `Autoritatea Națională ...`); a fuzzy tier would silently merge them. Each match returns the resolving tier as `matched_via` so corpus telemetry surfaces which tiers fired and an audit can sort matches by suspiciousness (prefix < token_set < diacritic < case < exact).

Each registry validates its entry shape at first load (id + canonical_name required; aliases optional list) and asserts uniqueness on `id`. `INSTITUTIONAL_BODIES_REGISTRY_VERSION` (and future siblings) are exposed as module-level constants for telemetry and surfaced in CLI output where it matters.

### Versioning contract

Backfill versions are NOT propagated into `extraction.extractor_versions`. Same Q11 conservative-by-design contract as the linker: an exact-match version key would force a full extractor re-run every time a registry file's `version` bumped. Backfill output lives entirely inside body content (`*_normalized` named fields), so the regression on a registry version bump is "the corpus has stale canonical ids until you re-run backfill" — a fast, idempotent operation. Re-extracting a sidecar (e.g., extractor v0.2.x → v0.2.y) clobbers all `*_normalized` fields back to their extractor defaults (typically null); re-running `monitorul-ii backfill` after re-extract is the recovery path.

The matcher tries tiers in order:
1. **exact** — input equals canonical_name OR an alias (case-sensitive).
2. **case** — case-insensitive equality after `.casefold()`.
3. **diacritic** — case + diacritic-stripped equality. Stripping uses `unicodedata.normalize('NFKD')` and filters combining marks; a cedilla map collapses pre-2010 `ţ`/`ş` into modern comma `ț`/`ș`; mojibake replacement chars `�` are stripped (without recovery — the underlying letter is gone, so the test in `tests/test_registries.py::test_normalize_replacement_chars_do_not_crash_or_misclassify` documents that mojibake'd inputs fall through to `(None, None)` rather than guessing).
4. **token_set** — orderless intersection of tokenised+folded form. Catches reorderings and minor inflections in long institutional names without admitting prefix collisions.
5. **prefix** (ministries only) — last-resort, longest-prefix match: cleaned input STARTS WITH a registered alias AND the next char is whitespace (or end-of-string). Recovers from the plenary extractor bleeding sentence prose into `addressed_to` (e.g. `Ministerul Justiției a fost să modifice legislația...`); without it, ~150 plenary records that carry a real ministry name plus a sentence continuation would stay unmatched. The token-boundary guard prevents partial-word collisions; the longest-first ordering guarantees `Ministerul Apărării Naționale` wins over the shorter `Ministerul Apărării` when both are registered. Disabled on the institutional registry (it isn't needed there and would be more dangerous because institutional names overlap less cleanly than ministry name variants).

### Pass 4.1 — institutional bodies → `report.issuing_body_normalized`

`src/monitorul_ii/registries/institutional_bodies.json` curates 30 entries covering the constitutional / autonomous bodies that file annual activity reports under R-suffix MOs:

CSAT, SRI, SIE, STS, SPP, BNR, ICR, Avocatul Poporului, Consiliul Legislativ, SRTv, SRR, ANCOM, ANRE, ANRM, ANCPI, Curtea de Conturi, Curtea Constituțională, ANI, ASF, ANSPDCP, CSM, ONPCSB, AGERPRES, ANAD, AEP, ICCJ, CCIR, CNA, CNSAS, CNCD.

For each: `id` (snake_case), `canonical_name` (full Romanian nominative), and `aliases[]` (acronym + Romanian genitive declension + common variants — `Consiliul ↔ Consiliului`, `Curtea ↔ Curții`, etc.). The genitive forms matter because Romanian text often refers to institutions in genitive case (`Raportul Consiliului Suprem ...`), and the `report_facsimile` extractor surfaces whatever case the source text used.

Production smoke on the 52 R-suffix corpus (2014-2024 cohort): **52/52 (100%)** of sidecars resolve to a registry id (was 34/52 = 65.4% in v0.1.0; the 18-doc gap was an extractor recovery problem, closed by `report_facsimile.py` v0.2.0's surface-form expansion + v0.2.1's reporting-period date-form fallback). All 52 resolutions land at the `exact` tier — the diacritic / token-set / prefix tiers stay defensive cover for variant raws that don't appear in this corpus. The 18 newly-recovered sidecars distribute across 5 ANCOM `pentru anul`, 4 ANRE (incl. 2 with `<br>` linebreak residue), 2 ASF, 1 SRTv `pentru anul`, and 6 AEP across the new pattern-2 (`Raportul privind activitatea X`) and broadened pattern-5 (`Raportul X (privind|asupra) <topic>` covering the AEP election-day forms `din 9 decembrie 2012`, `din iunie 2012`, `din 29 iulie 2012`). The 3 AEP-2014 election-day docs additionally gained reporting_period values via v0.2.1's date-form fallback.

Idempotent re-runs: 52 fills on the first run after the v0.2.x extractor bump, 0 fills + 52 skips on the second (all 52 "already filled with same canonical id"). Atomic write contract verified — no `.part` artefacts left on disk.

### CLI

`monitorul-ii backfill <paths> [--kind=issuing_body|all] [--force] [--dry-run] [--bucket NAME | --no-upload]`. Same path-resolution semantics as `link` (files or non-recursive directories of `*.extraction.json`). `--kind=all` runs every shipped pass — currently equivalent to `--kind=issuing_body` but forward-compatible with future ministry / sponsor / person passes. Unknown choices are rejected at argparse level. Output is one line per processed sidecar to stdout (`ok <name> -> <id> [matched_via] (raw=...)`, `skip <name> (reason)`, `ERROR <name>`). Trailing summary `filled=N skipped=M errors=K`; `matched_via` distribution + skip-reasons histogram on stderr.

### Pass 4.2 — ministries → `ministry_normalized` / `addressed_to_normalized`

`src/monitorul_ii/registries/ministries.json` curates 30 entries — one id per "ministry concept" (broad portfolio area: `health`, `education`, `transport`, `environment`, `finance`, `foreign_affairs`, `economy`, `justice`, `defense`, `internal_affairs`, etc.) plus the prime minister's office, two government secretariats, and four delegated portfolios. Aliases include every historical name observed in the corpus (e.g. `health`'s aliases include `Ministerul Sănătății Publice` from the 2007-2009 cabinets, plus the genitive form `Ministerului Sănătății`) and the Romanian genitive declension of the canonical form.

Time-window data (`active_from` / `active_to`) is intentionally NOT modelled in v0.1 — historical names collapse into the current ministry's id. This trades the ability to disambiguate "what was Ministerul Educației called in 2008?" for a much simpler registry. If a downstream consumer needs a time-window join, they can recover it from the MO publication date and the alias-matching record (the `active_from` / `active_to` infrastructure is forward-compatible — a future bump can add the field without a schema change).

The pass walks two slot families:
- `question_register.body.questions[].addressee.ministry_normalized` — written when the question's `addressee.ministry` raw resolves.
- `plenary_*.body.interpellations[].addressed_to_normalized` — written when the interpellation's `addressed_to` raw resolves.

Both join through `normalize_addressee`, which tries the ministry registry first and falls back to the institutional registry on miss (the schema's docstring explicitly lists intelligence services / CNSAS / ombudsman / central bank as valid addressees, and queries like "Curtea de Conturi" must resolve through the institutional fallback).

Production sweep on the full corpus (post `interpellations.py` v0.2.3 `addressed_to` rewrite):
- **qr addressees: 92.7% match rate** — 1851/1997 non-null raws resolve. matched_via histogram: `exact: 1828, prefix: 23`.
- **plenary interpellation addressees: 89.4% headline match rate** — 1849/2068 non-null raws resolve. matched_via histogram: `exact: 1247, case: 470, prefix: 131, token_set: 1`. Pre-v0.2.3 the headline rate was 25.0% (488/1953) because ~1500 of the raws were single-letter values (`m`, `p`, `S`, ...) emitted by the extractor's non-greedy regex; v0.2.3 captures `Ministerul X` / `ministrul X` (transformed to `Ministerul X` for registry match) / `(vice)?prim-ministru[lui]?` (returned as `Prim-ministrul`) / bare-person fallbacks against the first 800 chars of the questioner's turn body, lifting the headline 4× without any registry change. Spot-check on 10 random newly-filled plenary entries: 10/10 correctly normalised.

The `prefix` tier is now ~7% of plenary fills (down from 30% pre-fix, where it was load-bearing). It still catches sentence-bleed cases that escape the `,.;\n`-bounded greedy capture (e.g. when the agenda's `primary_references` extractor leaks long-form prose into the addressee field for legacy 2008-era docs). The token-boundary guard prevents matches like `Ministerul nostru` (no registered alias is bare `Ministerul`) from spuriously resolving.

Idempotent re-runs verified on the full corpus: third run yields `filled=0, errors=0, skipped=4510` (4185 of those are "no records" sidecars — committee_synthesis / report_facsimile / other types where the pass is a no-op).

### Pass 4.4 — `proposed_by` (Guvern attribution from OUG/OG signals)

Signal-driven, not registry-driven. There's no curated table of bill sponsors — the Tier 4 prompt scoped that registry at ~10K bills with per-bill metadata that's not present in the stenogram corpus. Instead, the pass attributes votes to the Government when the parent agenda's evidence makes that attribution unambiguous:

- The agenda's `primary_references[]` carries an `oug` (Government Emergency Ordinance) or `og` (Government Ordinance) ref — these are by definition government-issued, and any vote on a bill approving one inherits that proposer.
- The agenda title matches an OUG/OG cite pattern: `Ordonanța (de urgență)? a Guvernului`, `O.U.G.`, `O.G.` — covers cases where the ref-extractor missed the cite but the title still mentions it.

When either signal fires, the vote's `proposed_by` is populated with a canonical `Speaker` dict: `{raw: "Guvernul României", name: "Guvernul", role: "Guvern", title: null, party_group: null, person_id: null}`. The Speaker shape is forward-compatible with the future person registry — `person_id` stays null because the Government isn't a single named person.

The pass is intentionally precision-first. The Tier 4 prompt's 60% coverage target isn't reachable from agenda-title / ref-list signals alone on this corpus; per-bill metadata stripped from the stenogram by the time it reaches `extract` (the agenda title rarely contains "Inițiator: deputatul X"; the proposer info is recorded on parlament.ro per-bill but not in the MO publication). **Pre-`agenda.py`-v0.2.5 baseline**: 14.2% of plenary votes (7018/49556) attributed to Guvern; 692 of 4446 plenary sidecars touched. **Post-`agenda.py`-v0.2.5 re-baseline (current)**: 10.76% of plenary votes (5330/49556); 554 sidecars touched. The ~3.5pp drop reflects FP elimination — the previous figure was inflated by misattributed OUG/OG cites in contaminated agenda titles (declarations / informări / notă items absorbing bill cites from neighbouring items). 10/10 random Guvern votes spot-check as legitimate (real OUG/OG ref or title pattern). Idempotent re-runs verified — second pass yields `filled=0, skipped=4446, errors=0`.

The remaining 85% of votes (PL-x / L bills proposed by parliamentary groups, individual MPs, committees) need a parlament.ro per-bill metadata scrape to populate `proposed_by`. That work is deferred to a future tier — see § Future graduation candidates.

### Future passes (deferred from v0.1.0)

- **4.3 — Person registry → `Speaker.person_id`**: ~3000+ MPs across legislatures. **Blocked on data acquisition** — needs cdep.ro / senat.ro MP lists or a curated CSV; documented in § Future graduation candidates.

## Future graduation candidates

- **Tier 4.3 — Speaker person registry**. Schema slot `Speaker.person_id` stays null until an MP-list data source is wired in (cdep.ro / senat.ro JSON dumps or a curated CSV from a partner). The normalizer needs to handle Romanian name-order variation (family-first vs first-first), middle-initial drift, and diacritic variants. Smoke target ≥70% would leave the long tail (government officials, secretars de stat) unmatched by design — they're not MPs and won't appear in an MP registry.

- **Tier 4.4 long tail — bill-sponsor backfill from parlament.ro**. The shipped `proposed_by` pass attributes only the 14.2% of votes that carry an OUG/OG signal. The remaining 85% (PL-x / L bills proposed by parliamentary groups, individual MPs, or committees) need per-bill metadata scraped from `cdep.ro/pls?d=...&cam=...` and Senate equivalents. Path: regex first (cheap, in-corpus), then scrape only for unmatched bill cites; cache scraped responses under `data/scrapes/parlament_ro/<bill_id>.json` so re-runs hit disk; rate-limit ≥1s between requests; identify with `User-Agent: monitorul-ii backfill (contact@...)`. The schema's `Vote.proposed_by` is a `Speaker` dict — for non-Government proposers the populated form would be `{raw: "deputatul X (PNL)", name: "X", role: "deputat", party_group: "PNL", ...}`. Backfill version key would be a separate `BILL_SPONSOR_BACKFILL_VERSION`; the OUG/OG pass and the parlament.ro pass would coexist (Government attribution from the in-corpus signal, sponsor attribution from the scrape, both writing to the same slot — last-write-wins because the Speaker dicts collide unambiguously).

## Testing

Suite lives in `tests/`, mirrors `src/monitorul_ii/`, and ships ~130 unit tests that run in well under a second. Run with `uv run pytest`. The deliberate choices:

**Stubs at the I/O boundary, not below it.** Three boundaries, three stubs:

- `scraper.py` → `httpx.MockTransport`. `scrape_day` is exercised end-to-end with a fake transport that serves an index fragment for `get_mo.php` and `application/pdf` bytes for everything else. This gives us coverage of `fetch_index → parse_issues → download_pdf → DB upserts` without touching the network. The transport handler can also raise to simulate transient failures and test the `_with_retry` ladder.
- `converter.py` → `monkeypatch.setattr(converter, "convert_pdf", ...)`. `pymupdf4llm.to_markdown` is slow to import (loads onnxruntime) and slower to run. Tests for `convert_all`'s skip / force / parallel / error paths replace the leaf, exercise the orchestration, and assert events.
- `db.py` → `tmp_path`-backed SQLite. WAL mode survives the in-process tests just fine; no in-memory `:memory:` URI needed. The `db` fixture in `tests/conftest.py` wraps `DB(tmp_path / "audit.db")` in a context manager.

**No mocking library.** No `unittest.mock`, no `pytest-mock`, no fakes. `monkeypatch` for attribute swapping, `httpx.MockTransport` for HTTP, `tmp_path` for the filesystem. Two reasons: (1) the boundaries are narrow enough that hand-written stubs are clearer than `Mock(spec=...)` ceremony; (2) it keeps the dev dependency surface to pytest + ruff.

**Frozen `today`.** `scrape_day` and `should_fetch_index` both accept an explicit `today: date` parameter so tests don't have to monkeypatch `datetime.now`. `cli.py` is the only caller that passes `today=datetime.now(...).date()`.

**No real S3.** `boto3.client` is never instantiated by the suite — only `S3Config.from_env` (env-var permutations) and `_etag` (string handling) are tested. The `Uploader` class itself is thin enough that its surface is the boto3 calls; mocking those gains us nothing testing-wise. Live S3 round-trips, if ever needed, belong in a separate marker-gated integration suite.

**The contract for new features.** Every new feature ships with tests in the same change. The bar is laid out in `CLAUDE.md` ("Tests are mandatory…"); enforce it in code review.

## Rate-limiting

`scrape_day` sleeps `delay` seconds (default 0.5) **between successful downloads**, not before the first one and not when a file is skipped. This keeps re-runs over already-downloaded ranges fast while staying polite for fresh fetches.

The site has no `robots.txt` (the path returns a generic challenge page) and no published rate limit. 0.5 s/request is a conservative default for a government site; tune via `--delay 0` if scraping a small range you control.

## What is *not* here

- **No HTML parser dependency.** Regex is sufficient given the fragment shape; revisit if the site ever returns a richer payload.
- **No async / concurrency.** Sequential through the proxy — politeness against the site, simpler SQLite write path, no `--workers` flag. The DB makes resumes free, so wall-time isn't critical. Switch to `httpx.AsyncClient` only if we ever need to fan out across many days at once.
- **No mocking framework.** Tests are pytest + monkeypatch + `httpx.MockTransport`. We deliberately avoid `unittest.mock` / `pytest-mock`; the I/O boundaries are narrow enough that stubs are a few lines each and it keeps the dev dep list tiny.
- **No `attempts` cap.** Terminal classification is shape-of-error based (transient vs. permanent), not count-based. Permanent failures land in `status='gone'` directly; transient ones cycle through `failed` and auto-retry forever, which is fine because cyclic failures are visible in `last_error` and rare in practice.
- **No alembic / migrations framework.** Single `CREATE TABLE IF NOT EXISTS` block runs at startup; future schema changes pin an `ALTER TABLE` ladder to `PRAGMA user_version`.

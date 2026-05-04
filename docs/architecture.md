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
4. Bump `schema_version` in both the JSON file and `pipeline.py` if the body shape introduces new keys outside what v1.8.0 already documents.
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
- **`interpellations.py`** — `find_interpellation_block` scans for 6 transition phrases (`trecem la primirea răspunsurilor la interpelări`, `începem ora interpelărilor`, etc.); earliest match wins. Block ends at EOF (no observed case of agenda content following). Per-interpellation parser uses the same `## **<inner>:**` header pattern as activities; default `genre=interpelare` (more procedural / formal); flips to `întrebare` only when the questioner block strongly hints at oral-question form. `addressed_to` extracted from `adresat[ă] doamnei/domnului ROLE` or `Ministerului X` patterns; v0.1 leaves `addressed_to_normalized` null (deferred to ministry registry per schema § 8 line 231). `interpellation_number` reuses qr's `Nr. N(.NNN)?[A-Z]?` regex (last match wins). `response_deferred=True` when `(în scris)` notation present. `response` and `question_text` are null in v0.1 (responder-block parsing deferred).
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
| `references` | 0.1.0 (stub) | **0.2.0** | 6 strict variants (bill, law, oug, og, chamber_resolution, parliamentary_resolution) + `unknown` catch-all. 6 long-tail variants (motion, court_decision, constitution, regulation, eu_doc, treaty) deferred to v0.2+ |
| `speakers` | 0.1.0 | **0.2.0** | Adds shared primitives (HONORIFIC_RE, PARLIAMENTARY_TITLE_RE, extract_delivery_mode, parse_honorific_speaker) — used by plenary's per-form parsers; qr's `parse_questioner` is unchanged |
| `topics` | (new key) | **0.1.0** | 15 canonical primary topics aligned with parliamentary committees; title-scoped detection only; secondary topics deferred to v0.2 LLM pass |
| `plenary_stenogram` | (new key) | **0.1.0** | First per-type ship |
| `plenary_joint_session` | (new key) | **0.1.0** | First per-type ship |

The flat `_shared_helper_versions()` contract (Q11) means qr sidecars re-extract on first plenary run because their cached `extractor_versions` no longer matches (added `topics` key, bumped `references` and `speakers`). Acceptable cost (~2.5s for 53 qr docs) per the conservative-by-design version-keying contract — over-invalidate on helper-output-shape changes rather than risk stale sidecars when a static dependency declaration drifts.

### Schema deltas at v1.6.0

Strict body shapes for `plenary_stenogram` and `plenary_joint_session` replaced the v1.5.0 `PendingBody` placeholders. New `$defs`: `Reference` (oneOf 7 variants — bill, law, oug, og, chamber_resolution, parliamentary_resolution, unknown), `VoteCounts` (with `for: oneOf [int, "unanimous", null]`), `Topics`, `Attendance`, `ChairSegment`, `PlenarySession`, `PlenaryJointSession` (extends with `chambers_present`), `Activity` (oneOf 5 variants — speech, vote, procedural, narrator, deferral), `Interpellation`, `AgendaItem` (with `category` enum extended by `"other"`), `PlenaryStenogramBody`, `PlenaryJointSessionBody`. `committee_synthesis` and `report_facsimile` continue as `PendingBody` until their extractors land.

### What's deferred to v0.2+

Backfill-registry-dependent fields (always null in v0.1): `Speaker.person_id` (person registry), `QuestionAddressee.ministry_normalized` and `Interpellation.addressed_to_normalized` (ministry registry), `Vote.proposed_by` (bill-sponsor registry), `Vote.nominal_breakdown` (parlament.ro per-MP voting feed), `bill.subject` / `law.subject` / `parliamentary_resolution.subject` (best-effort context labels). Schema-modeled but stubbed: 6 long-tail reference variants, `topics.secondary` (LLM pass), per-topic `extraction` provenance block. Interpellation `response` and `question_text` parsing deferred. Cross-document `defers_to` / `resolves` linker for tying cross-session deferrals to their resolving final-vote document.

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

The 2 additional schema errors are pre-existing — the references parser correctly extracts `Legea nr. 19/1898` (an 1898 law citation in a 2001 stenogram), but the schema's `law.year: minimum 1990` rejects it. Same root cause as the 11 baseline errors. Loosening the year minimum (or routing pre-1990 citations to `unknown`) is a v0.2 concern; the 13 remaining errors are within the success-criterion budget (≤15).

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
| `2008-02-05_MO-PII-1c-2008.md` | Pre-pandemic narrative agenda + numbered roster, occasional `�` mojibake on PRE�EDINTE | 0.9997 |
| `2018-01-05_MO-PII-1c-2018.md` | 20-committee modern narrative-agenda baseline | 0.9998 |
| `2022-01-04_MO-PII-1c-2022.md` | Pandemic-era 17-committee, mixed format markers, `audiere candidat` purpose | 0.9997 |
| `2025-08-12_MO-PII-28c-2025.md` | Heavily tabular agenda + roster, 20 committees | 0.9998 |

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
| `committee_synthesis` | (new key) | **0.1.0** | First per-type ship |

The flat helper-version contract triggers re-extraction of qr + plenary sidecars on first committee_synthesis run because the cached `extractor_versions` dict on those sidecars now lacks the `committee_synthesis` key (and the comparison is exact-match per Q11 conservative-by-design contract). Acceptable cost.

### Schema deltas at v1.7.0

Strict body shape for `committee_synthesis` replaced the v1.6.0 `PendingBody` placeholder. New `$defs`: `CommitteeSynthesisBody`, `CommitteePeriod`, `Committee`, `CommitteeMeeting`, `TimeWindow`, `JointCommittee`, `RosterEntry`, `CommitteeAgendaItem`, `CommitteeVoteSummary`. `report_facsimile` continues as `PendingBody` until its extractor lands.

### What's deferred to v0.2+

Best-effort fields that v0.1 frequently emits as null/[] and v0.2 may tighten:

- `roster[]` — modern tabular and pandemic-era prose roster parsing. Currently emits `[]` for all blocks; the fields are reserved in the schema. The structural challenge is that the roster format varies wildly (tabular `|Name|Prezent fizic|`, narrative `au fost prezenți: A, B, C`, per-day `La lucrările din DD au fost prezenți: 1. NAME, group, role.`) and proper extraction needs three sub-parsers with format detection.
- `joint_with[]` — `în comun cu Comisia X[, Y, Z] din [Camera Deputaților|Senat]` detection. Currently `[]`. Multi-committee comma-list parsing is straightforward but bumps against ambiguous Romanian conjunction syntax (`X, Y și Z`); deferred for grilling.
- `committee.kind` for `special_joint` / `inquiry_joint` — the joint-Camera+Senat permanent committees (Statutul Deputaților și Senatorilor, etc.). Currently classified as `permanent`; the joint-prefix regex is conservative.
- Tabular agenda parsing (2025+) — when the agenda is rendered as a table (`|Nr.|PL-x|Title|Scopul|Rezoluție|`), the numbered-narrative regex misses the rows entirely. Coverage stays high because the partition still claims the table, but `agenda_items` is empty for those committees on those docs (~10-15% of 2025 cohort).

## Extract pipeline — `report_facsimile`

v0.1 ships the fifth — and final — per-type extractor. Cohort: 52 docs across 2014-2024, all `R`-suffix MO Partea II issues containing annual / activity reports from constitutional bodies (CSAT, SRI, SIE, BNR, ANCOM, ANRE, Avocatul Poporului, Consiliul Legislativ, SRTv/SRR, Curtea de Conturi, etc.) reproduced verbatim. Single-file extractor at `src/monitorul_ii/extraction/extractors/report_facsimile.py` — under 350 LOC; the body shape is intentionally minimal (report metadata + heading outline + excerpt), the lightest of the five per-type extractors.

### Why "minimal body shape"

The spec is explicit: "the full text remains in the sidecar markdown." A `report_facsimile` document IS the report — it's not extracted as records of speeches / votes / agenda items because the report's internal structure varies by issuing institution (CSAT chapters, SRI capitole, ANCOM ordered sections — no shared shape). Forcing a structured record-array would either (a) over-fit one institution's outline, or (b) produce nulls and noise. The pragmatic choice: extract metadata + a heading outline so consumers can search-and-snippet, leave the body verbatim, defer institutional sub-shape extraction to specialised tools per institution.

This shapes the coverage strategy: a single record claim spans the entire `(RAPOARTE DE ACTIVITATE)` genre marker through the trailing footer (or EOF). The full report content gets claimed once as one record, no per-section invention. Combined with rf-specific boilerplate (universal page-running headers, NOTĂ disclaimer, SUMAR block), coverage clears 0.97 on every doc tested.

### Title harvesting + issuing-body discrimination

Title hunt order: SUMAR row first (cleanest single-line form), `## **RAPORT ...**` body heading second. The SUMAR-row regex captures four observed surface forms via one alternation:

```
Raportul X privind activitatea desfășurată în anul YYYY    (CSAT — preposition before X)
Raport privind activitatea desfășurată de X în anul YYYY   (SRI/SIE)
Raport asupra activității desfășurate de X în anul YYYY    (Consiliul Legislativ, ANCOM)
Raport de activitate al X pe anul YYYY                     (SRTv/SRR, ANRE)
```

`_extract_issuing_body` then discriminates: each form has a unique preposition pattern (`Raportul X privind`, `... desfășurată de X`, `... activității desfășurate de X`, `Raport de activitate al X`), and a 4-pattern walk picks the matching one. Each pattern stops at `în anul`/`pe anul` / `privind` to keep the body label tight (no trailing year).

Reporting period is straightforward: `în anul YYYY` / `pe anul YYYY` → Jan 1 – Dec 31 of YYYY. Multi-year `în perioada YYYY-YYYY` widens both endpoints (rare; only a couple of CSAT bi-annuals in the cohort).

### Reception session

Every R-suffix doc in the corpus is received in joint session — Parliament receives the report at a `ȘEDINȚE COMUNE ALE CAMEREI DEPUTAȚILOR ȘI SENATULUI` session. The body line `## **Ședința din ziua de DD month YYYY**` (cedilla-tolerant for older docs) gives the reception date; frontmatter `session_date` is the fallback. `session_kind` = `joint` when the joint header is present, else falls back to frontmatter `chamber`. `received_in_document` (back-link to the receiving stenogram's `mo://YYYY/PART/ISSUE` document_id) stays null in v0.1 — the cross-document linker is a v0.2 pass that joins receiving stenograms to received reports.

### Coverage targets and measurement

Per Q9: **discovery margin 0.85**, **test fixture floor 0.80**, **mean target 0.90 documented (ungated)**.

Four hand-picked fixtures span the cohort eras:

| Fixture | Layout | Coverage |
|---|---|---|
| `2014-01-20_MO-PII-1R-2014.md` | CSAT 2010, image-only PDF (page-residue body) | 0.970 |
| `2014-04-24_MO-PII-13R-2014.md` | SRI 2007, modern body with H2 chapter headings | 0.999 |
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

### What's deferred to v0.2+

- **`received_in_document` cross-document linker.** **Shipped (v0.1.x)** — see "Cross-document linker" below.
- **`issuing_body_normalized`** — currently null. The institutional-bodies registry (CSAT, SRI, SIE, BNR, ICR, Avocatul Poporului, Consiliul Legislativ, SRTv, SRR, ANCOM, ANRE, Curtea de Conturi, etc. — small enum, ~20 entries) is the canonical normalisation source. Once that registry exists, a one-script backfill on every report sidecar populates the slot.
- **Institution-specific outline detection.** The current heading extractor pulls every `## **...**` heading minus a skip-list. CSAT reports use `CAPITOLUL I/II/...` outlining; SRI uses `OBIECTIVELE PRIORITARE`; ANCOM uses numbered `N.M.K.L` decimal sections. v0.2 could add per-issuer outline parsers that classify each heading as `chapter` / `section` / `appendix` etc. — but the discovery loop hasn't surfaced a query that needs it yet.

## Cross-document linker

A separate post-extract pass that fills back-pointer fields no per-type extractor can populate at single-doc time. v0.1 ships exactly one slot: `report_facsimile.body.report.received_at.received_in_document` — the `mo://YYYY/PART/ISSUE` document_id of the joint-session (or single-chamber) stenogram that received the report.

Code: `src/monitorul_ii/extraction/linker.py`. CLI surface: `monitorul-ii link <paths>`.

### Why a separate subcommand instead of inline-in-extract

Extract is single-pass per-MD. Linking needs the global picture: build an index of all stenogram sidecars, then look up each report's `received_at.session_date`. Forcing extract to know about other sidecars at extract-time would break two contracts: (a) per-MD parallelism becomes harder (each worker would need to read the full sidecar set), and (b) the dispatcher's "single source of truth for body content" guarantee gets muddied. Splitting into `extract` (writes body content from MD) + `link` (writes cross-doc back-pointers) keeps each pass small and re-runnable.

The natural workflow is **extract first, then link**:

```
$ uv run monitorul-ii extract pdfs/        # writes all sidecars; received_in_document=null
$ uv run monitorul-ii link pdfs/           # fills back-pointers cross-document
```

### Indexing strategy

`build_session_index(sidecars)` walks the input list once, reading each sidecar's `document_type` + `metadata.session_date` (with `metadata.published` fallback). Two document types act as receiving sessions: `plenary_joint_session` (priority 0, the dominant case — every R-suffix doc observed in the corpus is received in joint session) and `plenary_stenogram` (priority 1, single-chamber receptions for completeness, since the schema's `received_at.session_kind` enum allows `camera` / `senat`). When both share a date, joint wins.

The index is a flat `dict[date_str, document_id]`. Date collision within the same priority falls back to first-seen — defensible for 2013-2025 corpus; if multi-session days become a query problem, v0.2 can promote the value to a `dict[date, list[document_id]]` and let the caller pick.

### Linking + idempotency

`link_report(path, *, session_index, force, write)` reads a single report_facsimile sidecar, looks up its `received_at.session_date` in the index, writes the matched document_id into `received_in_document`. Pre-write schema validation; atomic write via `.part` rename. Self-link prevention is a defensive guard (the index excludes report_facsimile, but if the corpus ever changes shape, we don't write a self-pointer).

Idempotent by default: already-linked sidecars yield `status="skip"`, `reason="already linked"`. `--force` re-links populated entries — useful after a stenogram cohort re-extract that may have rewritten `document_id` for some receiving sessions. (In practice that doesn't happen since `document_id` is a deterministic projection of `metadata`, but the contract preserves the option.)

`link_all(sidecars, *, force, write)` is the iterator entry point: two-pass over the input list (build index, then yield one `LinkResult` per report). Non-report sidecars are silently filtered.

### Versioning contract

`LINKER_VERSION = "0.1.0"` lives in `linker.py` but is **not** propagated into the sidecar's `extraction.extractor_versions` dict. The version-keying contract there uses exact-match (`_versions_current` returns False on any key mismatch); adding linker as a key would force extractor re-runs whenever the linker bumped. Instead, linker output lives entirely inside body content. Tradeoff: re-extracting a sidecar (extractor version bump → re-extract per the cache-invalidation contract) clobbers `received_in_document`. Recovery: `monitorul-ii link` is fast (~1ms per doc — pure dict lookup) and re-runnable.

### CLI surface

`monitorul-ii link <paths> [--force] [--dry-run] [--bucket NAME | --no-upload]`. Path arguments are files or directories (non-recursive glob for `*.extraction.json`). Output is one line per processed report sidecar to stdout — `ok    <name>  -> mo://YYYY/PART/N` for linked, `skip  <name>  (reason)` for skipped, `ERROR <name>  (reason)` for validation failures. Trailing summary on stdout (`linked=N skipped=M errors=K | s3 ...`). Skip-reasons histogram on stderr when any skips occurred.

S3 mirror runs after each successful link when env vars are set: re-uploads the modified sidecar with `Content-Type: application/json`, overwriting the bucket copy. `--dry-run` skips both writes and uploads — useful for sanity-checking before a corpus-wide run.

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

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

# different Partea (default is II)
uv run monitorul-ii 2026-04-29 --part IV

# bypass the proxy
uv run monitorul-ii 2026-04-29 --no-proxy

# bypass the S3 mirror even when env vars are set
uv run monitorul-ii 2026-04-29 --no-upload
```

PDFs land in `<out>/<YYYY-MM-DD>_MO-P<part>-<num>-<year>.pdf`. The date is baked into the filename so everything sorts chronologically. Re-runs skip files already on disk.

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

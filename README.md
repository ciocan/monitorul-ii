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
```

PDFs land in `<out>/<YYYY-MM-DD>_MO-P<part>-<num>-<year>.pdf`. The date is baked into the filename so everything sorts chronologically. Re-runs skip files already on disk.

## How it works

The site exposes one undocumented AJAX endpoint that returns the day's index:

- `POST https://monitoruloficial.ro/ramo_customs/emonitor/get_mo.php` with body `today=YYYY-MM-DD` returns an HTML fragment containing `<a href="/Monitorul-Oficial--P<part>--<num>--<year>.html">` links per Partea.
- Following any of those `.html` URLs returns the PDF binary directly (`Content-Type: application/pdf`).

Issue numbers can have suffixes (`358Bis`, `12c`). Empty days (weekends, holidays) return zero issues for Partea II.

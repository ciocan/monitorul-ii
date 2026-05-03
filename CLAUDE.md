# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Python 3.12 + `uv`. Single CLI (`monitorul-ii`) that scrapes Monitorul Oficial PDFs by date. Hatchling-backed package at `src/monitorul_ii/`.

## Layout

- `src/monitorul_ii/scraper.py` — pure functions: `fetch_index`, `parse_issues`, `download_pdf`, `scrape_day`. No CLI concerns.
- `src/monitorul_ii/cli.py` — argparse wrapper exposing `monitorul-ii` (entry point in `pyproject.toml`).
- `src/monitorul_ii/__main__.py` — also runnable via `python -m monitorul_ii`.

## How the scraper talks to the site

There is no documented API. Reverse-engineered from the e-monitor page:

- **Index endpoint**: `POST https://monitoruloficial.ro/ramo_customs/emonitor/get_mo.php` with form body `today=YYYY-MM-DD` and a `Referer: https://monitoruloficial.ro/e-monitor/` header. Returns an HTML fragment — one `<div class="card-body">` per Partea, with `<a href="/Monitorul-Oficial--P<part>--<num>--<year>.html">` links.
- **PDF**: GET that `.html` URL — the response body **is** the PDF (`Content-Type: application/pdf`). The `.html` extension is misleading.
- Issue numbers can carry suffixes (`358Bis`, `12c`) — the parser regex accepts `[0-9A-Za-z]+`.
- Empty days (weekends, no Partea II that day) return zero issues; not an error.

## Commands

- Install / sync deps: `uv sync`
- Run the CLI: `uv run monitorul-ii <YYYY-MM-DD> [--until YYYY-MM-DD] [--out DIR] [--part II] [--delay 0.5]`
- Lint: `uv run ruff check`
- Format: `uv run ruff format`

PDFs land directly in `<out>/<YYYY-MM-DD>_MO-P<part>-<num>-<year>.pdf` (no per-day subdirectory — the date is in the filename so everything sorts chronologically in one folder). Re-runs skip files already on disk; partial downloads write to a `.part` file and are renamed atomically on success.

(A `.ruff_cache` is present; no committed config, so ruff defaults apply.)

**After each code change, run `uv run ruff format` and `uv run ruff check --fix` before reporting the task complete.**

**If a code change breaks tests, fix the tests in the same change — don't leave a red suite.** When tests fail because the production code's contract changed (renamed paths, refactored APIs, removed helpers), update the tests to match the new contract; don't revert the code or skip the tests. Only treat a test failure as a real bug to fix in production code when the test is asserting still-intended behavior.

**After meaningful feature changes, update `README.md` (user-facing) and `CLAUDE.md` (this file).**, and `docs/architecture.md` (deep dives).** "Meaningful" = a new CLI flag, a new behavior or default, a new module, a new external dependency, or anything a future user/agent would otherwise have to read the diff to discover.
**Keep CLAUDE.md scannable** — push detailed mechanics into `docs/architecture.md` and link from here.

## uv-on-snap quirk

`uv` installed via snap buffers stdout when there is no tty, so `uv run <cmd>` may appear silent in non-interactive shells. Pipe through `cat` (e.g. `uv run monitorul-ii --help | cat`) or invoke the venv binary directly (`.venv/bin/monitorul-ii ...`) when you need to see output.

## Release flow

Releases are automated via [release-please](https://github.com/googleapis/release-please) (`.github/workflows/release-please.yml`), triggered on every push to `main`.

- Tags include the component name (`include-component-in-tag: true`)
- Pre-1.0: minor bumps for features (`bump-minor-pre-major: true`)
- Version source of truth for release-please is `.release-please-manifest.json`, **not** `pyproject.toml` — keep them in sync if bumping manually
- Commits must follow Conventional Commits for release-please to pick them up (`feat:`, `fix:`, `chore:`, etc.)

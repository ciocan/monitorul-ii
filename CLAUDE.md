# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Freshly initialized Python 3.12 project. The only code is a hello-world `src/main.py`. No dependencies, no tests, no real architecture yet — when adding features, expect to also be establishing the project's structure and tooling conventions for the first time.

## Commands

The project uses `uv` (implied by `.python-version` + bare `pyproject.toml` without a build system); there is no lockfile yet.

- Run the entry point: `uv run python src/main.py`
- Add a dependency: `uv add <pkg>` (this will also create `uv.lock` and populate `[project].dependencies`)
- Sync env from `pyproject.toml`: `uv sync`

- Install / sync deps: `uv sync`
- Run a CLI: `uv run clipper-<cmd> <…>` (preferred — no install needed) or `.venv/bin/clipper-<cmd> <…>`
- Lint: `uv run ruff check`
- Format: `uv run ruff format`
- Tests: `uv run pytest` (unit) / `uv run pytest -m integration` (slow, requires ffmpeg + sample data in `data/`)

(A `.ruff_cache` is present; no committed config, so ruff defaults apply.)

**After each code change, run `uv run ruff format` and `uv run ruff check --fix` before reporting the task complete.** (For changes under `web/`, the equivalent is `bun run format && bun run lint`.)

**If a code change breaks tests, fix the tests in the same change — don't leave a red suite.** Run `uv run pytest` after non-trivial Python edits. When tests fail because the production code's contract changed (renamed paths, refactored APIs, removed helpers), update the tests to match the new contract; don't revert the code or skip the tests. Only treat a test failure as a real bug to fix in production code when the test is asserting still-intended behavior.

**After meaningful feature changes, update `README.md` (user-facing), `CLAUDE.md` (this file), and `docs/architecture.md` (deep dives).** "Meaningful" = a new CLI command or flag, a new behavior or default, a new module / page / route, a schema change, a new external dependency, or anything a future user/agent would otherwise have to read the diff to discover. Trivial bug fixes and pure refactors don't need a doc update. **Keep CLAUDE.md scannable** — push detailed mechanics into `docs/architecture.md` and link from here.

## Release flow

Releases are automated via [release-please](https://github.com/googleapis/release-please) (`.github/workflows/release-please.yml`), triggered on every push to `main`.

- Tags include the component name (`include-component-in-tag: true`)
- Pre-1.0: minor bumps for features (`bump-minor-pre-major: true`)
- Version source of truth for release-please is `.release-please-manifest.json`, **not** `pyproject.toml` — keep them in sync if bumping manually
- Commits must follow Conventional Commits for release-please to pick them up (`feat:`, `fix:`, `chore:`, etc.)

"""End-to-end catch-up runner for the Monitorul Oficial pipeline.

Runs `fetch → convert → extract → link → backfill → embed → index` in
sequence with per-stage pre/post checks, then writes a JSON indexing
report to disk and prints a human-readable summary on stderr.

The pipeline stages are idempotent — already-processed days/files are
skipped — so re-running the script is safe and resumes from wherever the
last run stopped.

# Usage

    uv run python tools/catchup.py
    uv run python tools/catchup.py --from 2026-04-15 --until 2026-05-08
    uv run python tools/catchup.py --include-cleanup
    uv run python tools/catchup.py --skip embed --skip index
    uv run python tools/catchup.py --report data/run-1.json

By default, `--from` is auto-detected from `data/monitorul.db` (the day
after the latest `status='ok'` row), `--until` is today's date, and the
optional `--include-cleanup` runs the registry-repair pass that applies
recent matcher/persons-registry fixes corpus-wide. Stages can be skipped
individually with `--skip <name>`. On failure, the script stops at the
failed stage and writes a partial report; pass `--continue-on-error` to
soldier on.

# Pre-flight

Reads `.env` (auto-loaded on import via python-dotenv per the CLI
contract). Probes the embed service via `GET /healthz` before the
`embed` stage and the ES cluster via `GET /` before the `index` stage —
both produce `pre_check.ok=false` entries in the report when unreachable
and skip the stage rather than half-running it.

# Report shape

```json
{
  "run_id": "20260508T173045",
  "date_range": {"from": "2026-04-15", "until": "2026-05-08"},
  "stages": [
    {"name": "fetch", "status": "ok", "duration_s": 42.3,
     "pre_check": {...}, "post_check": {"new_pdfs": 12, ...},
     "subprocess": {"exit_code": 0, "stderr_tail": "..."}},
    ...
  ],
  "summary": {"stages_ok": 7, "stages_failed": 0, "total_duration_s": 1234,
              "es_doc_count_in_range": 42, "es_latest_published": "..."}
}
```
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# `.env` autoload — match the CLI's contract so a fresh shell that just
# sourced PROXY_URL / S3_* / ES_* / EMBED_URL via dotenv has those
# available to subprocess'd `monitorul-ii` calls.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover — dotenv is a runtime dep
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
PDFS_DIR = REPO_ROOT / "pdfs"
DEFAULT_DB = REPO_ROOT / "data" / "monitorul.db"
ANALYZE_MAX_WORDS = 6000

# Ordered pipeline stages. Each entry's `command_args` is appended to
# `["uv", "run", "monitorul-ii", <subcommand>]` at run time. `pre` and
# `post` are method names looked up on `Runner`.
# Per-stage glob pattern over `pdfs/` — only files whose date prefix
# falls in [from, until] are passed to the subcommand. `fetch` is the
# only stage that doesn't take file paths (it takes the date range
# directly). The cleanup pass intentionally stays corpus-wide.
STAGE_INPUT_PATTERN: dict[str, str] = {
    "convert": "*.pdf",
    "extract": "*.md",
    "link": "*.extraction.json",
    "backfill": "*.extraction.json",
    "embed": "*.extraction.json",
    "analyze": "*.extraction.json",
    "index": "*.extraction.json",
}

STAGES: tuple[dict[str, Any], ...] = (
    {
        "name": "fetch",
        "subcommand": "fetch",
        "pre": "_pre_fetch",
        "post": "_post_fetch",
    },
    {
        "name": "convert",
        "subcommand": "convert",
        "pre": "_pre_convert",
        "post": "_post_convert",
    },
    {
        "name": "extract",
        "subcommand": "extract",
        "pre": "_pre_extract",
        "post": "_post_extract",
    },
    {
        "name": "link",
        "subcommand": "link",
        "pre": "_pre_link",
        "post": "_post_link",
    },
    {
        "name": "backfill",
        "subcommand": "backfill",
        "pre": "_pre_backfill",
        "post": "_post_backfill",
    },
    {
        "name": "embed",
        "subcommand": "embed",
        "pre": "_pre_embed",
        "post": "_post_embed",
    },
    {
        "name": "analyze",
        "subcommand": "analyze",
        "pre": "_pre_analyze",
        "post": "_post_analyze",
    },
    {
        "name": "index",
        "subcommand": "index",
        "pre": "_pre_index",
        "post": "_post_index",
    },
)

STAGE_NAMES = tuple(s["name"] for s in STAGES)


@dataclass
class StageResult:
    name: str
    status: str  # "ok" | "fail" | "skipped"
    duration_s: float
    pre_check: dict[str, Any] = field(default_factory=dict)
    post_check: dict[str, Any] = field(default_factory=dict)
    subprocess: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None  # populated on skipped/fail


def _today() -> date:
    return date.today()


def _repo_relative(p: Path) -> str:
    """Render `p` relative to REPO_ROOT for stderr messages, falling
    back to the absolute path when `p` lives outside the repo (e.g.
    cron job pointed `--pdfs` at a sibling directory, or a unit test
    using `tmp_path`). The bare `Path.relative_to(REPO_ROOT)` raises
    ValueError on a non-prefix relationship — fail-loud-in-prod is
    wrong for a stderr cosmetic.
    """
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


def _detect_date_range(db_path: Path) -> tuple[date, date]:
    """Auto-detect (from, until) for the catch-up run.

    `until` is today. `from` is the day after the latest `status='ok'`
    row in the SQLite `days` table (so we resume the day after the last
    successful index fetch). Falls back to (today, today) when the DB is
    missing or empty — that's a single-day run, which is harmless.
    """
    until = _today()
    if not db_path.is_file():
        return (until, until)
    try:
        with sqlite3.connect(db_path) as con:
            row = con.execute("SELECT MAX(date) FROM days WHERE status='ok'").fetchone()
    except sqlite3.OperationalError:
        return (until, until)
    if not row or not row[0]:
        return (until, until)
    last = date.fromisoformat(row[0])
    return (last + timedelta(days=1), until)


def _filter_stages(
    skip: list[str] | None, only: list[str] | None
) -> list[dict[str, Any]]:
    """Apply --skip / --only filters to the stage list. --only wins."""
    if only:
        sel = set(only)
        unknown = sel - set(STAGE_NAMES)
        if unknown:
            raise ValueError(f"unknown stage(s): {sorted(unknown)}")
        return [s for s in STAGES if s["name"] in sel]
    skip_set = set(skip or [])
    unknown = skip_set - set(STAGE_NAMES)
    if unknown:
        raise ValueError(f"unknown stage(s): {sorted(unknown)}")
    return [s for s in STAGES if s["name"] not in skip_set]


def _summarise_cmd(cmd: list[str], *, max_paths: int = 3) -> str:
    """Render the subprocess command line in a compact form.

    Long arg lists (50+ file paths once we start scoping by date range)
    would dominate the catchup runner's stderr output and bury the
    actual progress from the subcommand. Show the first `max_paths`
    paths plus an `… (+N more)` tail when paths are present.

    Distinguishes "path-shaped" args (no leading `--`, basename in the
    pdfs/data tree) from flag args (`-j`, `--from`, `--until`, etc.) so
    we keep the flags but elide the bulk of the file list.
    """
    head: list[str] = []
    paths: list[str] = []
    flags_after: list[str] = []
    seen_flag = False
    for tok in cmd:
        if tok.startswith("-") or seen_flag:
            seen_flag = True
            flags_after.append(tok)
            continue
        head.append(tok)
    # Walk back: paths are the trailing positional args, separated by
    # the program prefix. The simplest split: anything that isn't part
    # of the `["uv", "run", "monitorul-ii", "<subcommand>"]` prefix and
    # isn't a flag is a path candidate.
    prefix_len = 4  # uv / run / monitorul-ii / <subcommand>
    if len(head) > prefix_len:
        paths = head[prefix_len:]
        head = head[:prefix_len]
    if not paths:
        return " ".join(head + flags_after)
    if len(paths) <= max_paths:
        shown_paths = paths
        suffix = ""
    else:
        shown_paths = paths[:max_paths]
        suffix = f" … (+{len(paths) - max_paths} more)"
    return (
        " ".join(head + shown_paths)
        + suffix
        + (" " + " ".join(flags_after) if flags_after else "")
    )


def _http_probe(url: str, *, timeout: float = 5.0) -> tuple[bool, str]:
    """GET `url`, return (ok, message). Used for embed-service + ES probes."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
        return (200 <= status < 400, f"HTTP {status}")
    except urllib.error.HTTPError as exc:
        return (False, f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return (False, f"{type(exc).__name__}: {exc}")


def _es_verify_certs() -> bool:
    """Mirror `ESConfig.from_env()`'s `ES_VERIFY_CERTS` parsing.

    Opt-out, defaults to True; accepts `1/0`, `true/false`, `yes/no`,
    `on/off`. Anything else falls back to True (the safe default — we
    don't want a typo in the env var to silently disable cert checks
    here when the rest of the CLI does check).
    """
    raw = os.environ.get("ES_VERIFY_CERTS")
    if raw is None or raw.strip() == "":
        return True
    norm = raw.strip().lower()
    if norm in ("0", "false", "no", "off"):
        return False
    return True


def _es_probe(url: str, api_key: str, *, timeout: float = 3.0) -> tuple[bool, str]:
    """GET `url` with the API key, honouring `ES_VERIFY_CERTS`.

    Self-signed dev clusters set `ES_VERIFY_CERTS=0` so the probe must
    skip cert verification — otherwise the pre-flight reports a false
    failure and the operator dismisses the section as unreliable.
    """
    import ssl

    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"ApiKey {api_key}")
    ctx: ssl.SSLContext | None = None
    if not _es_verify_certs():
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return (200 <= resp.status < 400, f"HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        return (False, f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return (False, f"{type(exc).__name__}: {exc}")


class Runner:
    def __init__(
        self,
        *,
        date_from: date,
        date_until: date,
        workers: int,
        db_path: Path,
        pdfs_dir: Path,
        embed_url: str,
        es_url: str | None,
        es_api_key: str | None,
        include_cleanup: bool,
        continue_on_error: bool,
        include_mapping_bump: bool = False,
        analyze_provider: str = "openrouter",
        progress_stream=sys.stderr,
    ) -> None:
        self.date_from = date_from
        self.date_until = date_until
        self.workers = workers
        self.db_path = db_path
        self.pdfs_dir = pdfs_dir
        self.embed_url = embed_url.rstrip("/")
        self.es_url = es_url.rstrip("/") if es_url else None
        self.es_api_key = es_api_key
        self.include_cleanup = include_cleanup
        self.include_mapping_bump = include_mapping_bump
        # Provider for the discourse-analysis stage. The CLI flag mirrors
        # `monitorul-ii analyze --provider`; "openrouter" reads
        # OPENROUTER_API_KEY / OPENROUTER_URL, "google" reads
        # GOOGLE_AI_STUDIO_API_KEY / GOOGLE_AI_STUDIO_API_URL. Both
        # produce the same `<basename>.discourse.flash-lite.v0_1.json`
        # output (model class is `flash-lite` regardless of provider),
        # so downstream enrichment loader / denormaliser are agnostic.
        if analyze_provider not in ("openrouter", "google"):
            raise ValueError(
                f"unsupported analyze_provider: {analyze_provider!r} "
                "(expected 'openrouter' or 'google')"
            )
        self.analyze_provider = analyze_provider
        self.continue_on_error = continue_on_error
        self.progress = progress_stream

        # Snapshot pre-run filesystem counts so the post-checks can
        # report deltas instead of absolute counts.
        self._pre_pdf_count = self._count_pdfs()
        self._pre_md_count = self._count_glob("*.md")
        self._pre_sidecar_count = self._count_glob("*.extraction.json")
        self._pre_embedding_count = self._count_glob("*.embedding.bge-m3.v0_1.json")
        self._pre_discourse_count = self._count_glob("*.discourse.flash-lite.v0_1.json")

    # ----- filesystem helpers ---------------------------------------------

    def _count_glob(self, pattern: str) -> int:
        if not self.pdfs_dir.is_dir():
            return 0
        return sum(1 for _ in self.pdfs_dir.glob(pattern))

    def _count_pdfs(self) -> int:
        return self._count_glob("*.pdf")

    def _files_in_range(self, pattern: str) -> list[Path]:
        """Return sorted paths matching `pattern` whose filename starts
        with a date in [from, until].

        The CLI subcommands all accept multiple file paths as positional
        args, so the catchup runner passes only the date-scoped files
        instead of the whole `pdfs/` directory. This avoids the noise of
        the CLI walking the full corpus and printing a `skip` line for
        every already-processed file outside the catchup window.
        """
        if not self.pdfs_dir.is_dir():
            return []
        out: list[Path] = []
        for p in self.pdfs_dir.glob(pattern):
            try:
                d = date.fromisoformat(p.name[:10])
            except ValueError:
                continue
            if self.date_from <= d <= self.date_until:
                out.append(p)
        return sorted(out)

    def _count_in_range(self, pattern: str) -> int:
        """Count files whose filename starts with a date in [from, until]."""
        return len(self._files_in_range(pattern))

    # ----- pre-checks -----------------------------------------------------

    def _pre_fetch(self) -> dict[str, Any]:
        ok = self.db_path.parent.is_dir() or self.db_path == DEFAULT_DB
        return {"ok": True, "db_path_writable": ok, "out_dir": str(self.pdfs_dir)}

    def _pre_convert(self) -> dict[str, Any]:
        n = self._count_in_range("*.pdf")
        # Convert needs at least one PDF in the date range to operate on;
        # otherwise the subcommand would refuse to run with zero paths.
        return {"ok": n > 0, "pdfs_in_range": n}

    def _pre_extract(self) -> dict[str, Any]:
        n = self._count_in_range("*.md")
        return {"ok": n > 0, "mds_in_range": n}

    def _pre_link(self) -> dict[str, Any]:
        n = self._count_in_range("*.extraction.json")
        return {"ok": n > 0, "sidecars_in_range": n}

    def _pre_backfill(self) -> dict[str, Any]:
        n = self._count_in_range("*.extraction.json")
        return {"ok": n > 0, "sidecars_in_range": n}

    def _pre_embed(self) -> dict[str, Any]:
        n = self._count_in_range("*.extraction.json")
        if n == 0:
            return {"ok": False, "sidecars_in_range": 0}
        url = f"{self.embed_url}/healthz"
        ok, msg = _http_probe(url)
        return {
            "ok": ok,
            "sidecars_in_range": n,
            "embed_url": self.embed_url,
            "healthz": msg,
        }

    def _pre_analyze(self) -> dict[str, Any]:
        """Pre-check the discourse-analysis stage: sidecars in range +
        the right provider's API key available. Doesn't probe the LLM
        provider itself (the analyze CLI does that on its own startup
        via the provider's models endpoint).

        Provider choice (`self.analyze_provider`) decides which env var
        is required: `OPENROUTER_API_KEY` for OpenRouter (the default
        gateway path; OpenAI-compatible API; pricing has a markup over
        the upstream model rate) or `GOOGLE_AI_STUDIO_API_KEY` for
        Google AI Studio (direct to Google's `generativelanguage.googleapis.com`
        endpoint; ~25% cheaper at Flash-Lite rates per the constants in
        `discourse.py`). Both providers route to the same Gemini Flash-
        Lite model under the hood; output files are identical.
        """
        n = self._count_in_range("*.extraction.json")
        if n == 0:
            return {"ok": False, "sidecars_in_range": 0}
        if self.analyze_provider == "google":
            env_var = "GOOGLE_AI_STUDIO_API_KEY"
        else:
            env_var = "OPENROUTER_API_KEY"
        api_key_present = bool(os.environ.get(env_var))
        return {
            "ok": api_key_present,
            "sidecars_in_range": n,
            "provider": self.analyze_provider,
            "api_key_env": env_var,
            "api_key_present": api_key_present,
        }

    def _pre_index(self) -> dict[str, Any]:
        n = self._count_in_range("*.extraction.json")
        if n == 0:
            return {"ok": False, "sidecars_in_range": 0}
        if not self.es_url or not self.es_api_key:
            return {"ok": False, "reason": "ES_URL or ES_API_KEY missing"}
        ok, msg = _es_probe(self.es_url, self.es_api_key, timeout=5.0)
        return {"ok": ok, "sidecars_in_range": n, "es_url": self.es_url, "probe": msg}

    # ----- post-checks ----------------------------------------------------

    def _post_fetch(self) -> dict[str, Any]:
        new_pdfs = self._count_pdfs() - self._pre_pdf_count
        # Read DB summary so the report shows status splits per day.
        per_status: dict[str, int] = {}
        latest_ok: str | None = None
        if self.db_path.is_file():
            try:
                with sqlite3.connect(self.db_path) as con:
                    rows = con.execute(
                        "SELECT status, COUNT(*) FROM days WHERE date >= ? AND date <= ? GROUP BY status",
                        (self.date_from.isoformat(), self.date_until.isoformat()),
                    ).fetchall()
                    per_status = {r[0]: r[1] for r in rows}
                    latest_ok = con.execute(
                        "SELECT MAX(date) FROM days WHERE status='ok'"
                    ).fetchone()[0]
            except sqlite3.OperationalError:
                pass
        return {
            "new_pdfs": new_pdfs,
            "total_pdfs": self._count_pdfs(),
            "days_by_status": per_status,
            "latest_ok_date": latest_ok,
        }

    def _post_convert(self) -> dict[str, Any]:
        new_mds = self._count_glob("*.md") - self._pre_md_count
        # Pair check: every PDF in range should have a sibling .md
        unpaired = []
        for pdf in self.pdfs_dir.glob("*.pdf"):
            try:
                d = date.fromisoformat(pdf.name[:10])
            except ValueError:
                continue
            if not (self.date_from <= d <= self.date_until):
                continue
            if not pdf.with_suffix(".md").is_file():
                unpaired.append(pdf.name)
        return {
            "new_mds": new_mds,
            "unpaired_pdfs_in_range": unpaired[:5],
            "unpaired_count": len(unpaired),
        }

    def _post_extract(self) -> dict[str, Any]:
        new_sidecars = self._count_glob("*.extraction.json") - self._pre_sidecar_count
        rejected = self._count_in_range("*.rejected.json")
        return {"new_sidecars": new_sidecars, "rejected_in_range": rejected}

    def _post_link(self) -> dict[str, Any]:
        """Spot-check: sample a recent plenary sidecar and count the
        cross-doc deferral links + intra-doc art-N resolutions. This is
        a signal-of-life indicator, not exhaustive verification.
        """
        sample_paths = sorted(self.pdfs_dir.glob("*.extraction.json"))[-5:]
        forward_links = backlinks = xref_resolved = 0
        sampled = 0
        for p in sample_paths:
            try:
                sc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if sc.get("document_type") not in (
                "plenary_stenogram",
                "plenary_joint_session",
            ):
                continue
            sampled += 1
            for ai in sc.get("body", {}).get("agenda_items") or []:
                for a in ai.get("activities") or []:
                    if a.get("type") == "vote":
                        if a.get("defers_to"):
                            forward_links += 1
                        if a.get("resolves"):
                            backlinks += 1
                for ref in ai.get("primary_references") or []:
                    if ref.get("kind") == "unknown" and ref.get("resolved_to"):
                        xref_resolved += 1
        return {
            "sampled_plenary_sidecars": sampled,
            "vote_defers_to_count": forward_links,
            "vote_resolves_count": backlinks,
            "xref_resolved_count": xref_resolved,
        }

    def _post_backfill(self) -> dict[str, Any]:
        sample_paths = sorted(self.pdfs_dir.glob("*.extraction.json"))[-5:]
        with_pid = without_pid = 0
        sampled = 0
        for p in sample_paths:
            try:
                sc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            sampled += 1

            def walk(node: Any) -> None:
                nonlocal with_pid, without_pid
                if isinstance(node, dict):
                    keys = set(node.keys())
                    if {
                        "raw",
                        "name",
                        "person_id",
                        "title",
                        "role",
                        "party_group",
                    } <= keys:
                        if node.get("person_id"):
                            with_pid += 1
                        else:
                            without_pid += 1
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)

            walk(sc.get("body") or {})
        total = with_pid + without_pid
        rate = round(with_pid / total, 4) if total else 0.0
        return {
            "sampled_sidecars": sampled,
            "speakers_with_person_id": with_pid,
            "speakers_unresolved": without_pid,
            "resolution_rate": rate,
        }

    def _post_embed(self) -> dict[str, Any]:
        new_embeds = (
            self._count_glob("*.embedding.bge-m3.v0_1.json") - self._pre_embedding_count
        )
        # Embedding files in range / sidecar files in range coverage
        embedded = self._count_in_range("*.embedding.bge-m3.v0_1.json")
        sidecars = self._count_in_range("*.extraction.json")
        return {
            "new_embedding_files": new_embeds,
            "embedded_in_range": embedded,
            "sidecars_in_range": sidecars,
            "coverage_pct": round(embedded / sidecars, 4) if sidecars else 0.0,
        }

    def _post_analyze(self) -> dict[str, Any]:
        """Count fresh discourse files + report coverage in range."""
        new_discourse = (
            self._count_glob("*.discourse.flash-lite.v0_1.json")
            - self._pre_discourse_count
        )
        analyzed = self._count_in_range("*.discourse.flash-lite.v0_1.json")
        sidecars = self._count_in_range("*.extraction.json")
        return {
            "new_discourse_files": new_discourse,
            "analyzed_in_range": analyzed,
            "sidecars_in_range": sidecars,
            "coverage_pct": round(analyzed / sidecars, 4) if sidecars else 0.0,
        }

    def _post_index(self) -> dict[str, Any]:
        """Query ES for the doc count + latest published date in range.

        Falls back gracefully if ES isn't reachable from the post-check.
        """
        if not self.es_url or not self.es_api_key:
            return {"ok": False, "reason": "ES_URL or ES_API_KEY missing"}
        body = json.dumps(
            {
                "query": {
                    "range": {
                        "published": {
                            "gte": self.date_from.isoformat(),
                            "lte": self.date_until.isoformat(),
                        }
                    }
                },
                "size": 1,
                "sort": [{"published": "desc"}],
                "_source": ["published", "document_id"],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.es_url}/mo-documents/_search?track_total_hits=true",
            data=body,
            method="POST",
        )
        req.add_header("Authorization", f"ApiKey {self.es_api_key}")
        req.add_header("Content-Type", "application/json")
        ctx = None
        if not _es_verify_certs():
            import ssl

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=10.0, context=ctx) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        total = payload.get("hits", {}).get("total", {}).get("value", 0)
        hits = payload.get("hits", {}).get("hits", []) or []
        latest = hits[0]["_source"].get("published") if hits else None
        latest_doc_id = hits[0]["_source"].get("document_id") if hits else None
        return {
            "ok": True,
            "es_doc_count_in_range": total,
            "es_latest_published": latest,
            "es_latest_document_id": latest_doc_id,
        }

    # ----- subprocess driver ----------------------------------------------

    def _stage_args(self, name: str) -> list[str]:
        """Per-stage CLI args (the `monitorul-ii` subcommand args).

        For every non-fetch stage, the args are the explicit list of
        in-range files (PDFs / MDs / sidecars depending on stage). The
        CLI subcommands accept multiple file paths as positional args,
        so we pass only what's in `[from, until]` instead of the whole
        `pdfs/` directory. Workers flag is appended where supported.
        """
        if name == "fetch":
            return [
                self.date_from.isoformat(),
                "--until",
                self.date_until.isoformat(),
            ]
        pattern = STAGE_INPUT_PATTERN.get(name)
        if pattern is None:
            raise ValueError(f"unknown stage {name!r}")
        paths = self._files_in_range(pattern)
        args = [str(p) for p in paths]
        if name in ("convert", "backfill", "index", "analyze"):
            args += ["-j", str(self.workers)]
        if name == "analyze":
            args += ["--max-words", str(ANALYZE_MAX_WORDS)]
        if name == "analyze" and self.analyze_provider != "openrouter":
            # Forward the provider choice; only emit when it differs from
            # the analyze CLI's own default so the daily-cron command
            # line stays unchanged on the dominant openrouter path.
            args += ["--provider", self.analyze_provider]
        return args

    def _run_subprocess(self, name: str, subcommand: str) -> dict[str, Any]:
        cmd = ["uv", "run", "monitorul-ii", subcommand] + self._stage_args(name)
        self._progress(f"  $ {_summarise_cmd(cmd)}")
        # Don't capture: inherit the parent terminal's stdout+stderr so
        # the subcommand's rich progress bars (when stderr.isatty()) and
        # heartbeat lines (when piped) reach the user as they happen.
        # Capturing would silence the progress entirely until the stage
        # finished, which made the script feel like it had hung.
        proc = subprocess.run(cmd, cwd=REPO_ROOT, check=False)
        return {"exit_code": proc.returncode}

    # ----- driver ---------------------------------------------------------

    def _progress(self, msg: str) -> None:
        print(msg, file=self.progress, flush=True)

    def run_preflight(self) -> dict[str, Any]:
        """Print + return the pre-flight check results.

        Runs BEFORE any pipeline stage so the operator sees env / service
        / DB state up front, instead of staring at a silent terminal until
        the first stage finishes. None of the checks here gate stages —
        the per-stage `_pre_*` methods do that. This is just visibility.
        """
        self._progress("\n[pre-flight]")
        report: dict[str, Any] = {}

        # Working dir + sidecar dir
        report["working_dir"] = str(REPO_ROOT)
        self._progress(f"  ✓ working dir: {REPO_ROOT}")
        if self.pdfs_dir.is_dir():
            n = self._count_pdfs()
            report["pdfs_dir"] = {"path": str(self.pdfs_dir), "pdf_count": n}
            self._progress(
                f"  ✓ pdfs dir:    {_repo_relative(self.pdfs_dir)} ({n} PDFs)"
            )
        else:
            report["pdfs_dir"] = {"path": str(self.pdfs_dir), "pdf_count": 0}
            self._progress(
                f"  ⚠ pdfs dir:    {self.pdfs_dir} does not exist (will be created)"
            )

        # SQLite DB
        if self.db_path.is_file():
            try:
                with sqlite3.connect(self.db_path) as con:
                    rows = con.execute("SELECT COUNT(*) FROM days").fetchone()
                    latest = con.execute(
                        "SELECT MAX(date) FROM days WHERE status='ok'"
                    ).fetchone()
                report["db"] = {
                    "path": str(self.db_path),
                    "days_indexed": rows[0],
                    "latest_ok_date": latest[0],
                }
                self._progress(
                    f"  ✓ db:          {_repo_relative(self.db_path)} "
                    f"({rows[0]} days, latest ok={latest[0] or 'none'})"
                )
            except sqlite3.OperationalError as exc:
                report["db"] = {"path": str(self.db_path), "error": str(exc)}
                self._progress(f"  ⚠ db:          {self.db_path} unreadable: {exc}")
        else:
            report["db"] = {"path": str(self.db_path), "missing": True}
            self._progress(
                f"  ⚠ db:          {self.db_path} does not exist (will be created on first fetch)"
            )

        # Env vars (presence only — not values, to avoid logging secrets).
        # `EMBED_URL` is omitted from the missing-warning list because the
        # runner falls back to `http://127.0.0.1:8000` when it's unset, and
        # the actual reachability is reported separately by the embed probe
        # below. Same idea for ES_VERIFY_CERTS — it's an optional opt-out
        # whose absence is the safe default.
        required_keys = (
            "PROXY_URL",
            "S3_ENDPOINT",
            "S3_BUCKET",
            "S3_ACCESS_KEY_ID",
            "S3_SECRET_ACCESS_KEY",
            "ES_URL",
            "ES_API_KEY",
            # Discourse-analysis provider's API key. Tracks the operator's
            # `--analyze-provider` choice so a Google AI Studio run flags
            # the right env var as required and treats OPENROUTER_API_KEY
            # as optional (and vice versa).
            (
                "GOOGLE_AI_STUDIO_API_KEY"
                if self.analyze_provider == "google"
                else "OPENROUTER_API_KEY"
            ),
        )
        optional_keys = (
            "EMBED_URL",
            "OPENROUTER_URL",
            "GOOGLE_AI_STUDIO_API_URL",
            "ES_VERIFY_CERTS",
            # The non-selected provider's key is informational — present
            # if the operator has both available, missing-but-fine if not.
            (
                "OPENROUTER_API_KEY"
                if self.analyze_provider == "google"
                else "GOOGLE_AI_STUDIO_API_KEY"
            ),
        )
        env_present: list[str] = []
        env_missing: list[str] = []
        for key in required_keys:
            (env_present if os.environ.get(key) else env_missing).append(key)
        for key in optional_keys:
            if os.environ.get(key):
                env_present.append(key)
        report["env"] = {"present": env_present, "missing": env_missing}
        self._progress(f"  ✓ env set:     {', '.join(env_present) or '(none)'}")
        if env_missing:
            self._progress(f"  ⚠ env unset:   {', '.join(env_missing)}")

        # Embed service
        ok, msg = _http_probe(f"{self.embed_url}/healthz", timeout=2.0)
        report["embed"] = {"url": self.embed_url, "ok": ok, "probe": msg}
        marker = "✓" if ok else "⚠"
        suffix = "" if ok else "  (embed stage will be skipped)"
        self._progress(
            f"  {marker} embed:       {msg} ({self.embed_url}/healthz){suffix}"
        )

        # ES cluster (honours ES_VERIFY_CERTS)
        if self.es_url and self.es_api_key:
            es_ok, es_msg = _es_probe(self.es_url, self.es_api_key, timeout=3.0)
            verify_note = "" if _es_verify_certs() else "  [cert verify off]"
            report["es"] = {
                "url": self.es_url,
                "ok": es_ok,
                "probe": es_msg,
                "verify_certs": _es_verify_certs(),
            }
            marker = "✓" if es_ok else "⚠"
            suffix = "" if es_ok else "  (index stage will be skipped)"
            self._progress(
                f"  {marker} es:          {es_msg} ({self.es_url}){verify_note}{suffix}"
            )
        else:
            report["es"] = {"ok": False, "reason": "ES_URL or ES_API_KEY missing"}
            self._progress(
                "  ⚠ es:          ES_URL or ES_API_KEY missing — index stage will be skipped"
            )

        return report

    def run(self, stages: list[dict[str, Any]]) -> list[StageResult]:
        results: list[StageResult] = []

        # Mapping bump runs FIRST so the mo-* mapping is in place when
        # the index stage writes new docs from this catch-up window
        # (otherwise dynamic mapping would auto-detect rough types
        # without our analyzer / dense_vector overrides). The matching
        # post-step (index --force) runs after the pipeline to backfill
        # existing docs that the incremental index would have skipped.
        if self.include_mapping_bump:
            results.append(self._run_mapping_bump_pre())
            if results[-1].status != "ok" and not self.continue_on_error:
                return results

        for stage in stages:
            name = stage["name"]
            self._progress(f"\n=== {name} ===")
            t0 = time.monotonic()

            pre = getattr(self, stage["pre"])()
            if not pre.get("ok", True):
                results.append(
                    StageResult(
                        name=name,
                        status="skipped",
                        duration_s=round(time.monotonic() - t0, 2),
                        pre_check=pre,
                        reason=f"pre-check failed: {pre}",
                    )
                )
                self._progress(f"  pre-check failed; skipping. ({pre})")
                if not self.continue_on_error:
                    break
                continue

            sub = self._run_subprocess(name, stage["subcommand"])
            if sub["exit_code"] != 0:
                results.append(
                    StageResult(
                        name=name,
                        status="fail",
                        duration_s=round(time.monotonic() - t0, 2),
                        pre_check=pre,
                        subprocess=sub,
                        reason=f"subprocess exit {sub['exit_code']}",
                    )
                )
                self._progress(
                    f"  FAIL: exit {sub['exit_code']}\n  stderr tail:\n{sub['stderr_tail']}"
                )
                if not self.continue_on_error:
                    break
                continue

            post = getattr(self, stage["post"])()
            results.append(
                StageResult(
                    name=name,
                    status="ok",
                    duration_s=round(time.monotonic() - t0, 2),
                    pre_check=pre,
                    post_check=post,
                    subprocess=sub,
                )
            )
            self._progress(f"  ok ({results[-1].duration_s}s)  {post}")

        if self.include_mapping_bump:
            results.append(self._run_mapping_bump_post())
        if self.include_cleanup:
            results.append(self._run_cleanup())
        return results

    def _run_cleanup(self) -> StageResult:
        """Optional corpus-wide cleanup pass: re-run persons backfill
        with --force (clears stale fills under the current matcher) and
        re-index everything with --force --include-persons (refreshes
        mo-persons after registry changes).
        """
        self._progress(
            "\n=== cleanup (persons --force + index --force --include-persons) ==="
        )
        t0 = time.monotonic()
        cmds = [
            [
                "uv",
                "run",
                "monitorul-ii",
                "backfill",
                str(self.pdfs_dir),
                "--kind=persons",
                "--force",
                "-j",
                str(self.workers),
            ],
            [
                "uv",
                "run",
                "monitorul-ii",
                "index",
                str(self.pdfs_dir),
                "--force",
                "--include-persons",
                "-j",
                str(self.workers),
            ],
        ]
        sub_results = []
        for cmd in cmds:
            self._progress(f"  $ {' '.join(cmd)}")
            # Inherit parent stdio so the subcommand's progress bars / heartbeat
            # lines show live; matches `_run_subprocess` semantics.
            proc = subprocess.run(cmd, cwd=REPO_ROOT, check=False)
            sub_results.append({"command": cmd[3], "exit_code": proc.returncode})
            if proc.returncode != 0 and not self.continue_on_error:
                break
        ok = all(r["exit_code"] == 0 for r in sub_results)
        return StageResult(
            name="cleanup",
            status="ok" if ok else "fail",
            duration_s=round(time.monotonic() - t0, 2),
            subprocess={"steps": sub_results},
            reason=None if ok else "one or more cleanup commands failed",
        )

    def _run_mapping_bump_pre(self) -> StageResult:
        """Push additive mapping diffs to the live cluster BEFORE the
        index stage runs. Idempotent — `monitorul-ii es-init
        --update-mappings` resolves each `<grain>` read alias to its
        live concrete index and calls `indices.put_mapping(properties=...)`
        with the full properties block; ES `put_mapping` only accepts
        added fields, so the call is a no-op when the mapping is already
        current.

        Returns a `mapping-bump-pre` StageResult so the report shows the
        operator that the bump ran. Honours `--continue-on-error`: a
        failure here without that flag aborts the whole catch-up because
        running the index stage against a stale mapping would write docs
        with auto-detected (wrong) field types.
        """
        self._progress("\n=== mapping-bump-pre (es-init --update-mappings) ===")
        t0 = time.monotonic()
        if not self.es_url or not self.es_api_key:
            return StageResult(
                name="mapping-bump-pre",
                status="skipped",
                duration_s=round(time.monotonic() - t0, 2),
                reason="ES_URL or ES_API_KEY missing",
            )
        cmd = ["uv", "run", "monitorul-ii", "es-init", "--update-mappings"]
        self._progress(f"  $ {' '.join(cmd)}")
        proc = subprocess.run(cmd, cwd=REPO_ROOT, check=False)
        ok = proc.returncode == 0
        return StageResult(
            name="mapping-bump-pre",
            status="ok" if ok else "fail",
            duration_s=round(time.monotonic() - t0, 2),
            subprocess={"command": "es-init", "exit_code": proc.returncode},
            reason=None if ok else f"es-init --update-mappings exit {proc.returncode}",
        )

    def _run_mapping_bump_post(self) -> StageResult:
        """Force-reindex the corpus AFTER the regular pipeline so docs
        that the incremental index would have skipped (idempotency triple
        match) get reprojected through the new denormalizer and pick up
        the new mapping fields. ES does NOT retroactively re-analyze
        existing docs on a put_mapping call — the document body has to
        be written again. Timing: ~7 min for the full 5552-doc corpus on
        the operator's standard `-j 16`.
        """
        self._progress("\n=== mapping-bump-post (index --force) ===")
        t0 = time.monotonic()
        if not self.es_url or not self.es_api_key:
            return StageResult(
                name="mapping-bump-post",
                status="skipped",
                duration_s=round(time.monotonic() - t0, 2),
                reason="ES_URL or ES_API_KEY missing",
            )
        cmd = [
            "uv",
            "run",
            "monitorul-ii",
            "index",
            str(self.pdfs_dir),
            "--force",
            "-j",
            str(self.workers),
        ]
        self._progress(f"  $ {' '.join(cmd)}")
        proc = subprocess.run(cmd, cwd=REPO_ROOT, check=False)
        ok = proc.returncode == 0
        return StageResult(
            name="mapping-bump-post",
            status="ok" if ok else "fail",
            duration_s=round(time.monotonic() - t0, 2),
            subprocess={"command": "index --force", "exit_code": proc.returncode},
            reason=None if ok else f"index --force exit {proc.returncode}",
        )


def build_report(
    *,
    date_from: date,
    date_until: date,
    results: list[StageResult],
    cli_args: dict[str, Any],
    preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    total = round(sum(r.duration_s for r in results), 2)
    ok = sum(1 for r in results if r.status == "ok")
    failed = sum(1 for r in results if r.status == "fail")
    skipped = sum(1 for r in results if r.status == "skipped")
    es_metrics: dict[str, Any] = {}
    for r in results:
        if r.name == "index" and r.status == "ok":
            es_metrics = {
                "es_doc_count_in_range": r.post_check.get("es_doc_count_in_range"),
                "es_latest_published": r.post_check.get("es_latest_published"),
                "es_latest_document_id": r.post_check.get("es_latest_document_id"),
            }
            break
    return {
        "run_id": datetime.now().strftime("%Y%m%dT%H%M%S"),
        "date_range": {
            "from": date_from.isoformat(),
            "until": date_until.isoformat(),
        },
        "cli_args": cli_args,
        "preflight": preflight or {},
        "stages": [_stage_to_dict(r) for r in results],
        "summary": {
            "stages_ok": ok,
            "stages_failed": failed,
            "stages_skipped": skipped,
            "total_duration_s": total,
            **es_metrics,
        },
    }


def _stage_to_dict(s: StageResult) -> dict[str, Any]:
    d = {
        "name": s.name,
        "status": s.status,
        "duration_s": s.duration_s,
        "pre_check": s.pre_check,
        "post_check": s.post_check,
        "subprocess": s.subprocess,
    }
    if s.reason is not None:
        d["reason"] = s.reason
    return d


def print_summary(report: dict[str, Any], stream=sys.stderr) -> None:
    """Render the report as a compact stderr summary."""
    print("\n" + "=" * 60, file=stream)
    print("CATCH-UP REPORT", file=stream)
    print("=" * 60, file=stream)
    print(f"run_id:        {report['run_id']}", file=stream)
    print(
        f"date range:    {report['date_range']['from']} → {report['date_range']['until']}",
        file=stream,
    )
    s = report["summary"]
    print(
        f"stages:        {s['stages_ok']} ok, {s['stages_failed']} failed, {s['stages_skipped']} skipped",
        file=stream,
    )
    print(f"total time:    {s['total_duration_s']}s", file=stream)
    if "es_doc_count_in_range" in s:
        print(
            f"es docs:       {s.get('es_doc_count_in_range')} in range; latest published {s.get('es_latest_published')}",
            file=stream,
        )
    print("\nper-stage:", file=stream)
    for st in report["stages"]:
        marker = {"ok": "✓", "fail": "✗", "skipped": "·"}.get(st["status"], "?")
        line = f"  {marker} {st['name']:10s} {st['duration_s']:>7.2f}s"
        # One key metric per stage for the summary line.
        post = st.get("post_check") or {}
        for key in (
            "new_pdfs",
            "new_mds",
            "new_sidecars",
            "rejected_in_range",
            "vote_defers_to_count",
            "resolution_rate",
            "new_embedding_files",
            "new_discourse_files",
            "es_doc_count_in_range",
        ):
            if key in post:
                line += f"  {key}={post[key]}"
        if st.get("reason"):
            line += f"  reason={st['reason']!r}"
        print(line, file=stream)
    print("=" * 60, file=stream)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="catchup",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--from",
        dest="date_from",
        type=date.fromisoformat,
        default=None,
        help="Start of date range (YYYY-MM-DD). Default: day after the latest "
        "status='ok' row in data/monitorul.db.",
    )
    p.add_argument(
        "--until",
        dest="date_until",
        type=date.fromisoformat,
        default=None,
        help="End of date range (YYYY-MM-DD, inclusive). Default: today.",
    )
    p.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"SQLite path (default: {DEFAULT_DB.relative_to(REPO_ROOT)}).",
    )
    p.add_argument(
        "--pdfs",
        type=Path,
        default=PDFS_DIR,
        help=f"Sidecar directory (default: {PDFS_DIR.relative_to(REPO_ROOT)}).",
    )
    p.add_argument(
        "-j",
        "--workers",
        type=int,
        default=8,
        help="Workers for convert/backfill/index/embed (default: 8).",
    )
    p.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write JSON report to this path (default: data/catchup-reports/catchup-report-<run_id>.json).",
    )
    p.add_argument(
        "--skip",
        action="append",
        choices=STAGE_NAMES,
        default=[],
        help="Skip a stage (repeatable).",
    )
    p.add_argument(
        "--only",
        action="append",
        choices=STAGE_NAMES,
        default=[],
        help="Run only the named stage(s) (repeatable). Wins over --skip.",
    )
    p.add_argument(
        "--include-cleanup",
        action="store_true",
        help="Run the corpus-wide persons-backfill + index --force pass after "
        "the catch-up. Use after a matcher / persons.json registry change.",
    )
    p.add_argument(
        "--analyze-provider",
        choices=("openrouter", "google"),
        default="openrouter",
        help="LLM provider for the discourse-analysis stage. `openrouter` "
        "(default) uses the OpenAI-compatible gateway; reads "
        "OPENROUTER_API_KEY (+ optional OPENROUTER_URL). `google` calls "
        "the Google AI Studio API directly; reads "
        "GOOGLE_AI_STUDIO_API_KEY (+ optional GOOGLE_AI_STUDIO_API_URL). "
        "Both route to the same Gemini Flash-Lite model and produce "
        "identical `<basename>.discourse.flash-lite.v0_1.json` outputs; "
        "Google is ~25%% cheaper at Flash-Lite rates (no gateway markup) "
        "but lacks OpenRouter's per-call cost-reporting metadata. The "
        "pre-check looks at the matching env var; the analyze stage "
        "forwards `--provider <name>` to `monitorul-ii analyze`.",
    )
    p.add_argument(
        "--include-mapping-bump",
        action="store_true",
        help="Push mo-* mapping diffs to ES BEFORE the index stage and "
        "force-reindex the corpus AFTER it. Use after deploying a "
        "mapping/denormalizer change so the new fields populate on every "
        "doc, not just new ones from this catch-up window. The pre-step "
        "calls `monitorul-ii es-init --update-mappings` (idempotent, "
        "additive only); the post-step calls `monitorul-ii index pdfs/ "
        "--force -j <workers>` (~7 min for the full 5552-doc corpus on "
        "-j 16). Both stages skip cleanly when ES_URL/ES_API_KEY are "
        "missing — same gate as the regular index stage.",
    )
    p.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Don't stop on stage failure. Default: stop and write a partial report.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    auto_detected = args.date_from is None or args.date_until is None
    if auto_detected:
        auto_from, auto_until = _detect_date_range(args.db)
        args.date_from = args.date_from or auto_from
        args.date_until = args.date_until or auto_until

    if args.date_from > args.date_until:
        # Two interpretations of from > until:
        #  - User passed explicit dates that don't make sense → exit 2.
        #  - Auto-detect produced an empty range because the DB's latest
        #    status='ok' is today (or later) → already caught up; exit 0
        #    with a friendly message instead of an obscure error.
        if auto_detected:
            latest = args.date_from - timedelta(days=1)
            print(
                f"\n  Already caught up. Latest status='ok' in {args.db}: {latest}.\n"
                "  Pass --from YYYY-MM-DD to re-run for an earlier range, or\n"
                "  --until tomorrow to pick up an in-progress day.\n",
                file=sys.stderr,
            )
            return 0
        print(f"--from {args.date_from} > --until {args.date_until}", file=sys.stderr)
        return 2

    try:
        stages = _filter_stages(args.skip, args.only)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    runner = Runner(
        date_from=args.date_from,
        date_until=args.date_until,
        workers=args.workers,
        db_path=args.db,
        pdfs_dir=args.pdfs,
        embed_url=os.environ.get("EMBED_URL", "http://127.0.0.1:8000"),
        es_url=os.environ.get("ES_URL"),
        es_api_key=os.environ.get("ES_API_KEY"),
        include_cleanup=args.include_cleanup,
        include_mapping_bump=args.include_mapping_bump,
        analyze_provider=args.analyze_provider,
        continue_on_error=args.continue_on_error,
    )

    span_days = (args.date_until - args.date_from).days + 1
    stage_names = " → ".join(s["name"] for s in stages)
    print("\n" + "=" * 60, file=sys.stderr)
    print("MONITORUL CATCHUP", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(
        f"date range:    {args.date_from} → {args.date_until}  ({span_days} day(s))",
        file=sys.stderr,
    )
    print(f"stages:        {stage_names}", file=sys.stderr)
    if args.analyze_provider != "openrouter":
        # Only mention provider when it's been switched off the default
        # so the dominant openrouter run keeps a quiet header.
        env_var = (
            "GOOGLE_AI_STUDIO_API_KEY"
            if args.analyze_provider == "google"
            else "OPENROUTER_API_KEY"
        )
        print(
            f"analyze:       provider={args.analyze_provider} (reads {env_var})",
            file=sys.stderr,
        )
    if args.include_mapping_bump:
        print(
            "mapping-bump:  ON (es-init --update-mappings before, index --force after)",
            file=sys.stderr,
        )
    if args.include_cleanup:
        print(
            "cleanup:       ON (persons-backfill --force + index --force)",
            file=sys.stderr,
        )

    preflight = runner.run_preflight()

    print("\n[stages]", file=sys.stderr)
    results = runner.run(stages)

    report = build_report(
        date_from=args.date_from,
        date_until=args.date_until,
        results=results,
        cli_args={
            "skip": args.skip,
            "only": args.only,
            "workers": args.workers,
            "include_cleanup": args.include_cleanup,
            "include_mapping_bump": args.include_mapping_bump,
            "analyze_provider": args.analyze_provider,
            "continue_on_error": args.continue_on_error,
        },
        preflight=preflight,
    )

    report_path = args.report
    if report_path is None:
        report_path = (
            REPO_ROOT
            / "data"
            / "catchup-reports"
            / f"catchup-report-{report['run_id']}.json"
        )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print_summary(report)
    print(f"\nwrote: {report_path}", file=sys.stderr)

    return 0 if report["summary"]["stages_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

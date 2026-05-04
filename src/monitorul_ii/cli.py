from __future__ import annotations

import os

# Disable onnxruntime / OpenMP intra-op threading before pymupdf4llm imports it.
# pymupdf-layout creates an ort.InferenceSession internally; if ORT auto-threads,
# our outer ThreadPoolExecutor (-j N) competes with inner threads for the same
# cores and throughput collapses (8 PDFs: 110 s default → 28.7 s with OMP=1 -j 8).
# `setdefault` keeps explicit user overrides intact.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("ORT_INTRA_OP_NUM_THREADS", "1")

import argparse  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from dataclasses import replace  # noqa: E402
from datetime import date, datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

from monitorul_ii.converter import (  # noqa: E402
    ConvertEvent,
    ConvertEventPayload,
    collect_pdfs,
    convert_all,
)
from monitorul_ii.db import DB  # noqa: E402
from monitorul_ii.scraper import (  # noqa: E402
    DayResult,
    FileEvent,
    FileEventPayload,
    _client,
    daterange,
    scrape_day,
)
from monitorul_ii.uploader import S3Config, Uploader  # noqa: E402

_FETCH_LABELS: dict[FileEvent, str] = {
    "skip": "skip ",
    "download": "ok   ",
    "error": "ERR  ",
}
_CONVERT_LABELS: dict[ConvertEvent, str] = {
    "skip": "skip ",
    "convert": "ok   ",
    "error": "ERR  ",
}

_HEARTBEAT_EVERY = 100  # days, for `fetch`
_CONVERT_HEARTBEAT_EVERY = 50  # PDFs, for `convert` in pipes


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"invalid date {s!r}: expected YYYY-MM-DD"
        ) from e


def _add_s3_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip uploading to S3 even when S3_* env vars are set.",
    )
    p.add_argument(
        "--bucket",
        default=None,
        help="Override S3_BUCKET from env.",
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="monitorul-ii",
        description="Scrape Monitorul Oficial PDFs and convert them to markdown.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser(
        "fetch",
        help="Download PDFs for a date or date range.",
        description="Download Monitorul Oficial Partea a II-a PDFs for a date or date range.",
    )
    fetch.add_argument("date", type=_parse_date, help="Date (YYYY-MM-DD)")
    fetch.add_argument(
        "--until",
        type=_parse_date,
        default=None,
        help="End of date range (inclusive). If omitted, only `date` is fetched.",
    )
    fetch.add_argument(
        "--out",
        type=Path,
        default=Path("pdfs"),
        help="Output directory (default: ./pdfs). PDFs land directly here; the date is in the filename.",
    )
    fetch.add_argument(
        "--part",
        default="II",
        help="Roman-numeral Partea to fetch (default: II).",
    )
    fetch.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds to wait between PDF downloads (default: 0.5).",
    )
    fetch.add_argument(
        "--proxy",
        default=None,
        help="Proxy URL (e.g. http://user:pass@host:port). Overrides PROXY_URL from env.",
    )
    fetch.add_argument(
        "--no-proxy",
        action="store_true",
        help="Bypass any proxy configured via PROXY_URL or --proxy.",
    )
    _add_s3_args(fetch)
    fetch.add_argument(
        "--db",
        type=Path,
        default=Path("data/monitorul.db"),
        help="SQLite path for the audit log + resume gate (default: data/monitorul.db).",
    )
    fetch.add_argument(
        "--no-db",
        action="store_true",
        help="Disable the SQLite audit log entirely. Re-runs lose 'empty day' memory.",
    )
    fetch.add_argument(
        "--reverse",
        action="store_true",
        help="Walk the date range newest→oldest. A partial run leaves you with the most recent stretch.",
    )
    fetch.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch every day's index regardless of DB status.",
    )
    fetch.add_argument(
        "--rescrape-recent",
        type=int,
        default=0,
        metavar="N",
        help="Re-fetch the last N days regardless of DB status (default: 0). Today is always re-fetched.",
    )
    fetch.set_defaults(func=cmd_fetch)

    convert = sub.add_parser(
        "convert",
        help="Convert downloaded PDFs to markdown.",
        description=(
            "Convert one or more local PDFs to markdown next to the source "
            "(e.g. pdfs/<basename>.md). Optionally mirrors to the same S3 bucket."
        ),
    )
    convert.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="PDF files or directories containing PDFs (non-recursive).",
    )
    convert.add_argument(
        "--force",
        action="store_true",
        help="Re-convert PDFs that already have a non-empty .md alongside.",
    )
    convert.add_argument(
        "-j",
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        metavar="N",
        help="Parallel conversion threads (default: CPU count). Set to 1 for sequential.",
    )
    convert.add_argument(
        "--reverse",
        action="store_true",
        help="Process PDFs in reverse order (newest→oldest, since filenames are date-prefixed). A partial run leaves you with the most recent stretch.",
    )
    _add_s3_args(convert)
    convert.set_defaults(func=cmd_convert)

    return p


def _day_summary_line(
    r: DayResult, uploaded: int, in_bucket: int, upload_errors: int
) -> str | None:
    if not r.fetched_index and r.found == 0 and r.skipped == 0 and r.downloaded == 0:
        # Pure DB-cached skip — progress bar carries the day count, don't spam logs.
        return None
    line = (
        f"{r.day}: found={r.found} downloaded={r.downloaded} "
        f"skipped={r.skipped} errors={len(r.errors)}"
    )
    if not r.fetched_index:
        line += " (cached)"
    if uploaded or in_bucket or upload_errors:
        line += (
            f" | s3 uploaded={uploaded} in-bucket={in_bucket} errors={upload_errors}"
        )
    return line


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_bytes(b: int) -> str:
    if b >= 1_000_000_000:
        return f"{b / 1_000_000_000:.2f} GB"
    return f"{b / 1_000_000:.1f} MB"


def _progress_line(
    days_done: int,
    days_total: int,
    totals: dict[str, int],
    counters: dict[str, int],
    elapsed: float,
) -> str:
    rate = days_done / elapsed if elapsed > 0 else 0.0
    eta = (days_total - days_done) / rate if rate > 0 else 0.0
    pct = days_done / days_total * 100 if days_total else 0.0
    return (
        f"{days_done:,}/{days_total:,} ({pct:.1f}%) | "
        f"found={totals['found']:,} downloaded={totals['downloaded']:,} "
        f"({_fmt_bytes(counters['download_bytes'])}) "
        f"failed={totals['failed']:,} | "
        f"s3 uploaded={counters['uploaded']:,} "
        f"in-bucket={counters['in_bucket']:,} "
        f"errors={counters['upload_errors']:,} | "
        f"elapsed={_fmt_duration(elapsed)} ETA={_fmt_duration(eta)}"
    )


class _ProgressReporter:
    """Live tty bar (rich) when stderr is a terminal, periodic heartbeat otherwise."""

    def __init__(
        self,
        days_total: int,
        totals: dict[str, int],
        counters: dict[str, int],
    ) -> None:
        self.days_total = days_total
        self.totals = totals
        self.counters = counters
        self.start = time.monotonic()
        self.days_done = 0
        self._tty = sys.stderr.isatty()
        self._progress = None
        self._task = None
        if self._tty:
            from rich.console import Console
            from rich.progress import (
                BarColumn,
                MofNCompleteColumn,
                Progress,
                TextColumn,
                TimeElapsedColumn,
                TimeRemainingColumn,
            )

            self._progress = Progress(
                BarColumn(bar_width=None),
                TextColumn("[progress.percentage]{task.percentage:>5.1f}%"),
                MofNCompleteColumn(),
                TextColumn("·"),
                TextColumn("[cyan]{task.description}"),
                TextColumn("·"),
                TimeElapsedColumn(),
                TextColumn("ETA"),
                TimeRemainingColumn(),
                console=Console(stderr=True),
                transient=False,
            )
            self._task = self._progress.add_task(self._desc(), total=days_total)

    def _desc(self) -> str:
        t, c = self.totals, self.counters
        return (
            f"found={t['found']:,} dl={t['downloaded']:,} ({_fmt_bytes(c['download_bytes'])})"
            f" fail={t['failed']:,}"
            f" · s3 up={c['uploaded']:,} have={c['in_bucket']:,} err={c['upload_errors']:,}"
        )

    def __enter__(self) -> "_ProgressReporter":
        if self._progress is not None:
            self._progress.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._progress is not None:
            self._progress.stop()

    def print(self, line: str, *, err: bool = False) -> None:
        if self._progress is not None:
            self._progress.console.print(line, highlight=False)
            self._progress.update(self._task, description=self._desc())
        else:
            print(line, file=sys.stderr if err else sys.stdout)

    def advance(self) -> None:
        self.days_done += 1
        if self._progress is not None:
            self._progress.update(self._task, advance=1, description=self._desc())
            return
        if self.days_done % _HEARTBEAT_EVERY == 0 and self.days_done < self.days_total:
            elapsed = time.monotonic() - self.start
            print(
                "progress: "
                + _progress_line(
                    self.days_done, self.days_total, self.totals, self.counters, elapsed
                ),
                file=sys.stderr,
            )


class _ConvertProgressReporter:
    """Live `rich` bar for `convert` when stderr is a tty; heartbeat otherwise.

    Mirrors `_ProgressReporter`'s shape but speaks the convert vocabulary
    (converted/skipped/errors + the same s3 trio).
    """

    def __init__(self, total: int, counters: dict[str, int]) -> None:
        self.total = total
        self.counters = counters
        self.start = time.monotonic()
        self.done = 0
        self._tty = sys.stderr.isatty()
        self._progress = None
        self._task = None
        if self._tty and total > 0:
            from rich.console import Console
            from rich.progress import (
                BarColumn,
                MofNCompleteColumn,
                Progress,
                TextColumn,
                TimeElapsedColumn,
                TimeRemainingColumn,
            )

            self._progress = Progress(
                BarColumn(bar_width=None),
                TextColumn("[progress.percentage]{task.percentage:>5.1f}%"),
                MofNCompleteColumn(),
                TextColumn("·"),
                TextColumn("[cyan]{task.description}"),
                TextColumn("·"),
                TimeElapsedColumn(),
                TextColumn("ETA"),
                TimeRemainingColumn(),
                console=Console(stderr=True),
                transient=False,
            )
            self._task = self._progress.add_task(self._desc(), total=total)

    def _desc(self) -> str:
        c = self.counters
        s = f"ok={c['converted']:,} skip={c['skipped']:,} err={c['errors']:,}"
        if c["uploaded"] or c["in_bucket"] or c["upload_errors"]:
            s += (
                f" · s3 up={c['uploaded']:,} have={c['in_bucket']:,}"
                f" err={c['upload_errors']:,}"
            )
        return s

    def __enter__(self) -> "_ConvertProgressReporter":
        if self._progress is not None:
            self._progress.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._progress is not None:
            self._progress.stop()

    def print(self, line: str, *, err: bool = False) -> None:
        if self._progress is not None:
            self._progress.console.print(line, highlight=False)
            self._progress.update(self._task, description=self._desc())
        else:
            print(line, file=sys.stderr if err else sys.stdout)

    def advance(self) -> None:
        self.done += 1
        if self._progress is not None:
            self._progress.update(self._task, advance=1, description=self._desc())
            return
        if self.done % _CONVERT_HEARTBEAT_EVERY == 0 and self.done < self.total:
            elapsed = time.monotonic() - self.start
            rate = self.done / elapsed if elapsed > 0 else 0.0
            eta = (self.total - self.done) / rate if rate > 0 else 0.0
            pct = self.done / self.total * 100 if self.total else 0.0
            print(
                f"progress: {self.done:,}/{self.total:,} ({pct:.1f}%) | "
                f"{self._desc()} | "
                f"elapsed={_fmt_duration(elapsed)} ETA={_fmt_duration(eta)}",
                file=sys.stderr,
            )


def _resolve_uploader(args: argparse.Namespace) -> Uploader | None:
    if args.no_upload:
        return None
    cfg = S3Config.from_env()
    if cfg is None:
        return None
    if args.bucket:
        cfg = replace(cfg, bucket=args.bucket)
    up = Uploader(cfg)
    try:
        up.validate()
    except Exception as exc:
        print(
            f"s3: cannot reach bucket {cfg.bucket!r} at {cfg.endpoint}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    print(f"uploading to s3://{cfg.bucket} at {cfg.endpoint}", file=sys.stderr)
    return up


def cmd_fetch(args: argparse.Namespace) -> int:
    end = args.until or args.date
    if end < args.date:
        print("error: --until must be >= date", file=sys.stderr)
        return 2

    proxy: str | None = None
    if not args.no_proxy:
        proxy = args.proxy or os.environ.get("PROXY_URL") or None
    if proxy:
        print(f"using proxy {_redact_proxy(proxy)}", file=sys.stderr)

    uploader = _resolve_uploader(args)

    db: DB | None = None
    if not args.no_db:
        db = DB(args.db)
        print(f"db: {args.db}", file=sys.stderr)

    today = datetime.now(timezone.utc).date()
    counters = {
        "uploaded": 0,
        "in_bucket": 0,
        "upload_errors": 0,
        "download_bytes": 0,
    }
    days_total = (end - args.date).days + 1
    totals = {"found": 0, "downloaded": 0, "failed": 0}
    total_errors = 0
    reporter = _ProgressReporter(days_total, totals, counters)

    def on_event(p: FileEventPayload) -> None:
        label = _FETCH_LABELS[p.kind]
        if p.kind == "error":
            reporter.print(f"  {label} {p.path.name}  ({p.detail})", err=True)
        else:
            reporter.print(f"  {label} {p.path.name}")

        if p.kind == "download" and p.size_bytes:
            counters["download_bytes"] += p.size_bytes

        if uploader is None or p.kind == "error":
            return
        if not (p.path.exists() and p.path.stat().st_size > 0):
            return
        if (
            db is not None
            and db.issue_status(p.day, p.issue.part, p.issue.number, p.issue.year)
            == "uploaded"
        ):
            counters["in_bucket"] += 1
            return
        try:
            result = uploader.upload_if_missing(p.path)
            if result.uploaded:
                counters["uploaded"] += 1
                reporter.print(f"  s3+   {p.path.name}")
            else:
                counters["in_bucket"] += 1
                reporter.print(f"  s3=   {p.path.name}")
            if db is not None:
                db.record_issue_uploaded(
                    p.day,
                    p.issue.part,
                    p.issue.number,
                    p.issue.year,
                    s3_etag=result.etag,
                )
        except Exception as exc:
            counters["upload_errors"] += 1
            reporter.print(f"  s3!   {p.path.name}  ({exc})", err=True)

    try:
        with reporter, _client(proxy=proxy) as client:
            for day in daterange(args.date, end, reverse=args.reverse):
                day_uploaded_before = counters["uploaded"]
                day_inbucket_before = counters["in_bucket"]
                day_uperr_before = counters["upload_errors"]
                r = scrape_day(
                    client,
                    day,
                    args.out,
                    part=args.part,
                    delay=args.delay,
                    on_event=on_event,
                    db=db,
                    today=today,
                    force=args.force,
                    rescrape_recent_days=args.rescrape_recent,
                )
                summary = _day_summary_line(
                    r,
                    counters["uploaded"] - day_uploaded_before,
                    counters["in_bucket"] - day_inbucket_before,
                    counters["upload_errors"] - day_uperr_before,
                )
                if summary is not None:
                    reporter.print(summary)
                total_errors += len(r.errors)
                totals["found"] += r.found
                totals["downloaded"] += r.downloaded
                totals["failed"] += len(r.errors)
                reporter.advance()
    except KeyboardInterrupt:
        print(
            "\ninterrupted: "
            + _progress_line(
                reporter.days_done,
                days_total,
                totals,
                counters,
                time.monotonic() - reporter.start,
            ),
            file=sys.stderr,
        )
        return 130
    finally:
        if db is not None:
            db.close()

    total_errors += counters["upload_errors"]
    return 1 if total_errors else 0


def _convert_summary_line(counters: dict[str, int], *, prefix: str = "") -> str:
    line = (
        f"{prefix}converted={counters['converted']} "
        f"skipped={counters['skipped']} errors={counters['errors']}"
    )
    if counters["uploaded"] or counters["in_bucket"] or counters["upload_errors"]:
        line += (
            f" | s3 uploaded={counters['uploaded']} "
            f"in-bucket={counters['in_bucket']} "
            f"errors={counters['upload_errors']}"
        )
    return line


def cmd_convert(args: argparse.Namespace) -> int:
    pdfs = collect_pdfs(list(args.paths), reverse=args.reverse)
    if not pdfs:
        print("no PDFs found", file=sys.stderr)
        return 0

    uploader = _resolve_uploader(args)
    counters = {
        "converted": 0,
        "skipped": 0,
        "errors": 0,
        "uploaded": 0,
        "in_bucket": 0,
        "upload_errors": 0,
    }

    with _ConvertProgressReporter(len(pdfs), counters) as report:

        def on_event(p: ConvertEventPayload) -> None:
            if p.kind == "skip":
                counters["skipped"] += 1
            elif p.kind == "convert":
                counters["converted"] += 1
            else:
                counters["errors"] += 1

            label = _CONVERT_LABELS[p.kind]
            if p.kind == "error":
                report.print(f"  {label} {p.md_path.name}  ({p.detail})", err=True)
            else:
                report.print(f"  {label} {p.md_path.name}")

            if uploader is not None and p.kind != "error":
                if p.md_path.exists() and p.md_path.stat().st_size > 0:
                    try:
                        result = uploader.upload_if_missing(
                            p.md_path, content_type="text/markdown"
                        )
                        if result.uploaded:
                            counters["uploaded"] += 1
                            report.print(f"  s3+   {p.md_path.name}")
                        else:
                            counters["in_bucket"] += 1
                            report.print(f"  s3=   {p.md_path.name}")
                    except Exception as exc:
                        counters["upload_errors"] += 1
                        report.print(f"  s3!   {p.md_path.name}  ({exc})", err=True)

            report.advance()

        try:
            convert_all(pdfs, force=args.force, workers=args.workers, on_event=on_event)
        except KeyboardInterrupt:
            report.print(
                _convert_summary_line(counters, prefix="interrupted: "),
                err=True,
            )
            return 130

    print(_convert_summary_line(counters))
    return 1 if counters["errors"] or counters["upload_errors"] else 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


def _redact_proxy(url: str) -> str:
    """Mask any password embedded in the proxy URL before logging."""
    from urllib.parse import urlparse, urlunparse

    p = urlparse(url)
    if p.password:
        netloc = f"{p.username}:***@{p.hostname}"
        if p.port:
            netloc += f":{p.port}"
        return urlunparse(p._replace(netloc=netloc))
    return url


if __name__ == "__main__":
    raise SystemExit(main())

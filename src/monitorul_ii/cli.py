from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from monitorul_ii.converter import (
    ConvertEvent,
    ConvertEventPayload,
    collect_pdfs,
    convert_all,
)
from monitorul_ii.db import DB
from monitorul_ii.scraper import (
    DayResult,
    FileEvent,
    FileEventPayload,
    _client,
    daterange,
    scrape_day,
)
from monitorul_ii.uploader import S3Config, Uploader

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

_HEARTBEAT_EVERY = 100  # days


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
    _add_s3_args(convert)
    convert.set_defaults(func=cmd_convert)

    return p


def _print_day_summary(
    r: DayResult, uploaded: int, in_bucket: int, upload_errors: int
) -> None:
    if not r.fetched_index and r.found == 0 and r.skipped == 0 and r.downloaded == 0:
        # Pure DB-cached skip — heartbeat carries the progress, don't spam stdout.
        return
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
    print(line)


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


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
        f"failed={totals['failed']:,} | "
        f"s3 uploaded={counters['uploaded']:,} "
        f"in-bucket={counters['in_bucket']:,} "
        f"errors={counters['upload_errors']:,} | "
        f"elapsed={_fmt_duration(elapsed)} ETA={_fmt_duration(eta)}"
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
    counters = {"uploaded": 0, "in_bucket": 0, "upload_errors": 0}

    def on_event(p: FileEventPayload) -> None:
        label = _FETCH_LABELS[p.kind]
        if p.kind == "error":
            print(f"  {label} {p.path.name}  ({p.detail})", file=sys.stderr)
        else:
            print(f"  {label} {p.path.name}")

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
                print(f"  s3+   {p.path.name}")
            else:
                counters["in_bucket"] += 1
                print(f"  s3=   {p.path.name}")
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
            print(f"  s3!   {p.path.name}  ({exc})", file=sys.stderr)

    days_total = (end - args.date).days + 1
    start_time = time.monotonic()
    days_done = 0
    totals = {"found": 0, "downloaded": 0, "failed": 0}
    total_errors = 0

    try:
        with _client(proxy=proxy) as client:
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
                _print_day_summary(
                    r,
                    counters["uploaded"] - day_uploaded_before,
                    counters["in_bucket"] - day_inbucket_before,
                    counters["upload_errors"] - day_uperr_before,
                )
                total_errors += len(r.errors)
                totals["found"] += r.found
                totals["downloaded"] += r.downloaded
                totals["failed"] += len(r.errors)
                days_done += 1

                if days_done % _HEARTBEAT_EVERY == 0 and days_done < days_total:
                    print(
                        "progress: "
                        + _progress_line(
                            days_done,
                            days_total,
                            totals,
                            counters,
                            time.monotonic() - start_time,
                        ),
                        file=sys.stderr,
                    )
    except KeyboardInterrupt:
        print(
            "\ninterrupted: "
            + _progress_line(
                days_done,
                days_total,
                totals,
                counters,
                time.monotonic() - start_time,
            ),
            file=sys.stderr,
        )
        return 130
    finally:
        if db is not None:
            db.close()

    total_errors += counters["upload_errors"]
    return 1 if total_errors else 0


def cmd_convert(args: argparse.Namespace) -> int:
    pdfs = collect_pdfs(list(args.paths))
    if not pdfs:
        print("no PDFs found", file=sys.stderr)
        return 0

    uploader = _resolve_uploader(args)
    counters = {"uploaded": 0, "in_bucket": 0, "upload_errors": 0}

    def on_event(p: ConvertEventPayload) -> None:
        label = _CONVERT_LABELS[p.kind]
        if p.kind == "error":
            print(f"  {label} {p.md_path.name}  ({p.detail})", file=sys.stderr)
        else:
            print(f"  {label} {p.md_path.name}")

        if uploader is None or p.kind == "error":
            return
        if not (p.md_path.exists() and p.md_path.stat().st_size > 0):
            return
        try:
            result = uploader.upload_if_missing(p.md_path, content_type="text/markdown")
            if result.uploaded:
                counters["uploaded"] += 1
                print(f"  s3+   {p.md_path.name}")
            else:
                counters["in_bucket"] += 1
                print(f"  s3=   {p.md_path.name}")
        except Exception as exc:
            counters["upload_errors"] += 1
            print(f"  s3!   {p.md_path.name}  ({exc})", file=sys.stderr)

    try:
        summary = convert_all(pdfs, force=args.force, on_event=on_event)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    line = (
        f"converted={summary.converted} skipped={summary.skipped} "
        f"errors={len(summary.errors)}"
    )
    if counters["uploaded"] or counters["in_bucket"] or counters["upload_errors"]:
        line += (
            f" | s3 uploaded={counters['uploaded']} "
            f"in-bucket={counters['in_bucket']} "
            f"errors={counters['upload_errors']}"
        )
    print(line)
    return 1 if summary.errors or counters["upload_errors"] else 0


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

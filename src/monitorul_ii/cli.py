from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

from monitorul_ii.scraper import DayResult, FileEvent, _client, daterange, scrape_day
from monitorul_ii.uploader import S3Config, Uploader

_LABELS: dict[FileEvent, str] = {
    "skip": "skip ",
    "download": "ok   ",
    "error": "ERR  ",
}


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"invalid date {s!r}: expected YYYY-MM-DD"
        ) from e


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="monitorul-ii",
        description="Download Monitorul Oficial Partea a II-a PDFs for a date or date range.",
    )
    p.add_argument("date", type=_parse_date, help="Date (YYYY-MM-DD)")
    p.add_argument(
        "--until",
        type=_parse_date,
        default=None,
        help="End of date range (inclusive). If omitted, only `date` is fetched.",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("pdfs"),
        help="Output directory (default: ./pdfs). PDFs land directly here; the date is in the filename.",
    )
    p.add_argument(
        "--part",
        default="II",
        help="Roman-numeral Partea to fetch (default: II).",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds to wait between PDF downloads (default: 0.5).",
    )
    p.add_argument(
        "--proxy",
        default=None,
        help="Proxy URL (e.g. http://user:pass@host:port). Overrides PROXY_URL from env.",
    )
    p.add_argument(
        "--no-proxy",
        action="store_true",
        help="Bypass any proxy configured via PROXY_URL or --proxy.",
    )
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
    return p


def _print_summary(
    r: DayResult, uploaded: int, in_bucket: int, upload_errors: int
) -> None:
    line = (
        f"{r.day}: found={r.found} downloaded={r.downloaded} "
        f"skipped={r.skipped} errors={len(r.errors)}"
    )
    if uploaded or in_bucket or upload_errors:
        line += (
            f" | s3 uploaded={uploaded} in-bucket={in_bucket} errors={upload_errors}"
        )
    print(line)


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


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _build_parser().parse_args(argv)
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

    counters = {"uploaded": 0, "in_bucket": 0, "upload_errors": 0}

    def on_event(kind: FileEvent, path: Path, detail: str | None) -> None:
        label = _LABELS[kind]
        if kind == "error":
            print(f"  {label} {path.name}  ({detail})", file=sys.stderr)
        else:
            print(f"  {label} {path.name}")

        if uploader is None or kind == "error":
            return
        if not (path.exists() and path.stat().st_size > 0):
            return
        try:
            if uploader.upload_if_missing(path):
                counters["uploaded"] += 1
                print(f"  s3+   {path.name}")
            else:
                counters["in_bucket"] += 1
                print(f"  s3=   {path.name}")
        except Exception as exc:
            counters["upload_errors"] += 1
            print(f"  s3!   {path.name}  ({exc})", file=sys.stderr)

    total_errors = 0
    with _client(proxy=proxy) as client:
        for day in daterange(args.date, end):
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
            )
            _print_summary(
                r,
                counters["uploaded"] - day_uploaded_before,
                counters["in_bucket"] - day_inbucket_before,
                counters["upload_errors"] - day_uperr_before,
            )
            total_errors += len(r.errors)
    total_errors += counters["upload_errors"]
    return 1 if total_errors else 0


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

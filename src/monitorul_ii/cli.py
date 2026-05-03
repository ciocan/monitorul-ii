from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from monitorul_ii.scraper import DayResult, FileEvent, _client, daterange, scrape_day

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
    return p


def _print_summary(r: DayResult) -> None:
    line = (
        f"{r.day}: found={r.found} downloaded={r.downloaded} "
        f"skipped={r.skipped} errors={len(r.errors)}"
    )
    print(line)


def _on_event(kind: FileEvent, path: Path, detail: str | None) -> None:
    label = _LABELS[kind]
    if kind == "error":
        print(f"  {label} {path.name}  ({detail})", file=sys.stderr)
    else:
        print(f"  {label} {path.name}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    end = args.until or args.date
    if end < args.date:
        print("error: --until must be >= date", file=sys.stderr)
        return 2

    total_errors = 0
    with _client() as client:
        for day in daterange(args.date, end):
            r = scrape_day(
                client,
                day,
                args.out,
                part=args.part,
                delay=args.delay,
                on_event=_on_event,
            )
            _print_summary(r)
            total_errors += len(r.errors)
    return 1 if total_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

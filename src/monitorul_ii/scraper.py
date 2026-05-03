from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import httpx

FileEvent = Literal["skip", "download", "error"]
ProgressFn = Callable[[FileEvent, Path, str | None], None]

BASE_URL = "https://monitoruloficial.ro"
INDEX_ENDPOINT = f"{BASE_URL}/ramo_customs/emonitor/get_mo.php"
INDEX_REFERER = f"{BASE_URL}/e-monitor/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Issue numbers can be plain digits or have a letter suffix (e.g. "47", "12c", "358Bis").
_LINK_RE = re.compile(
    r'href="(?P<href>/Monitorul-Oficial--P(?P<part>[IVXM]+)--(?P<num>[0-9A-Za-z]+)--(?P<year>\d{4})\.html)"'
)


@dataclass(frozen=True)
class Issue:
    part: str  # e.g. "II"
    number: str  # e.g. "47" or "12c"
    year: int
    url: str  # absolute URL to the PDF

    def filename(self, pub_date: date) -> str:
        return f"{pub_date.isoformat()}_MO-P{self.part}-{self.number}-{self.year}.pdf"


def _client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        http2=False,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Referer": INDEX_REFERER},
        follow_redirects=True,
    )


def fetch_index(client: httpx.Client, day: date) -> str:
    """POST the date to the AJAX endpoint and return the raw HTML fragment."""
    resp = client.post(
        INDEX_ENDPOINT,
        data={"today": day.isoformat(), "rand": "0.123456789"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    resp.raise_for_status()
    return resp.text


def parse_issues(html: str, part: str = "II") -> list[Issue]:
    """Extract issue links of the requested Partea (default 'II') from the index HTML."""
    seen: set[tuple[str, str, int]] = set()
    issues: list[Issue] = []
    for m in _LINK_RE.finditer(html):
        if m.group("part") != part:
            continue
        key = (m.group("part"), m.group("num"), int(m.group("year")))
        if key in seen:
            continue
        seen.add(key)
        issues.append(
            Issue(
                part=m.group("part"),
                number=m.group("num"),
                year=int(m.group("year")),
                url=BASE_URL + m.group("href"),
            )
        )
    return issues


def download_pdf(client: httpx.Client, issue: Issue, out_path: Path) -> int:
    """Stream the PDF for an issue to out_path. Returns bytes written."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".part")
    written = 0
    with client.stream("GET", issue.url) as resp:
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "pdf" not in ctype.lower():
            raise RuntimeError(f"unexpected content-type {ctype!r} for {issue.url}")
        with tmp_path.open("wb") as f:
            for chunk in resp.iter_bytes(64 * 1024):
                f.write(chunk)
                written += len(chunk)
    tmp_path.replace(out_path)
    return written


def daterange(start: date, end: date) -> Iterator[date]:
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


@dataclass
class DayResult:
    day: date
    found: int
    downloaded: int
    skipped: int
    errors: list[str]


def scrape_day(
    client: httpx.Client,
    day: date,
    out_dir: Path,
    *,
    part: str = "II",
    delay: float = 0.5,
    on_event: ProgressFn | None = None,
) -> DayResult:
    """Fetch the index for `day` and download every Partea `part` PDF into out_dir.

    Files already on disk (size > 0) are skipped. `on_event(kind, path, detail)` fires
    once per issue with kind in {"skip", "download", "error"}.
    """
    html = fetch_index(client, day)
    issues = parse_issues(html, part=part)
    result = DayResult(day=day, found=len(issues), downloaded=0, skipped=0, errors=[])
    did_download = False
    for issue in issues:
        target = out_dir / issue.filename(day)
        if target.exists() and target.stat().st_size > 0:
            result.skipped += 1
            if on_event:
                on_event("skip", target, None)
            continue
        if did_download and delay > 0:
            time.sleep(delay)
        try:
            download_pdf(client, issue, target)
            result.downloaded += 1
            did_download = True
            if on_event:
                on_event("download", target, None)
        except (httpx.HTTPError, RuntimeError) as exc:
            msg = f"{issue.url}: {exc}"
            result.errors.append(msg)
            if on_event:
                on_event("error", target, msg)
    return result

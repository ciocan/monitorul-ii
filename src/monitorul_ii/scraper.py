from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeVar

import httpx

if TYPE_CHECKING:
    from monitorul_ii.db import DB

FileEvent = Literal["skip", "download", "error"]

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

_T = TypeVar("_T")


@dataclass(frozen=True)
class Issue:
    part: str  # e.g. "II"
    number: str  # e.g. "47" or "12c"
    year: int
    url: str  # absolute URL to the PDF

    def filename(self, pub_date: date) -> str:
        return f"{pub_date.isoformat()}_MO-P{self.part}-{self.number}-{self.year}.pdf"


@dataclass(frozen=True)
class FileEventPayload:
    kind: FileEvent
    issue: Issue
    day: date
    path: Path
    detail: str | None = None


ProgressFn = Callable[[FileEventPayload], None]


def _client(timeout: float = 30.0, proxy: str | None = None) -> httpx.Client:
    """Build the httpx client. If `proxy` is set, all requests route through it."""
    return httpx.Client(
        http2=False,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Referer": INDEX_REFERER},
        follow_redirects=True,
        proxy=proxy,
    )


def _is_transient(exc: BaseException) -> bool:
    """Decide whether a request error is worth retrying.

    Retry: transport errors, timeouts, 5xx, 429.
    Don't retry: 4xx-not-429 (URL is genuinely wrong), parse / content-type errors.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        sc = exc.response.status_code
        return sc == 429 or sc >= 500
    return isinstance(exc, httpx.HTTPError)


def _with_retry(
    fn: Callable[[], _T],
    *,
    attempts: int = 3,
    backoff: tuple[float, ...] = (1.0, 2.0, 4.0),
) -> _T:
    """Bounded retry with exponential-ish backoff for transient errors only."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if not _is_transient(exc) or i == attempts - 1:
                raise
            time.sleep(backoff[i])
    raise RuntimeError("unreachable")  # pragma: no cover


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


def download_pdf(client: httpx.Client, issue: Issue, out_path: Path) -> tuple[int, str]:
    """Stream the PDF for an issue to out_path. Returns (size_bytes, sha256_hex)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".part")
    written = 0
    h = hashlib.sha256()
    with client.stream("GET", issue.url) as resp:
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "pdf" not in ctype.lower():
            raise RuntimeError(f"unexpected content-type {ctype!r} for {issue.url}")
        with tmp_path.open("wb") as f:
            for chunk in resp.iter_bytes(64 * 1024):
                f.write(chunk)
                h.update(chunk)
                written += len(chunk)
    tmp_path.replace(out_path)
    return written, h.hexdigest()


def _hash_file(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(64 * 1024):
            h.update(chunk)
            size += len(chunk)
    return size, h.hexdigest()


def _file_mtime_iso(path: Path) -> str:
    ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def daterange(start: date, end: date, *, reverse: bool = False) -> Iterator[date]:
    if reverse:
        cur = end
        while cur >= start:
            yield cur
            cur -= timedelta(days=1)
    else:
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
    fetched_index: bool = True


def _issues_from_db(db: DB, day: date, part: str) -> list[Issue]:
    """For an 'ok' day we don't re-fetch, reconstruct Issues for non-terminal rows."""
    out: list[Issue] = []
    for row in db.issues_for_day(day, part):
        if row["status"] in ("downloaded", "uploaded"):
            continue
        out.append(
            Issue(
                part=row["part"],
                number=row["number"],
                year=int(row["year"]),
                url=row["url"],
            )
        )
    return out


def scrape_day(
    client: httpx.Client,
    day: date,
    out_dir: Path,
    *,
    part: str = "II",
    delay: float = 0.5,
    on_event: ProgressFn | None = None,
    db: DB | None = None,
    today: date | None = None,
    force: bool = False,
    rescrape_recent_days: int = 0,
) -> DayResult:
    """Fetch the index for `day` and download every Partea `part` PDF into out_dir.

    DB resume contract (when `db` is given):
      - If the day is already 'ok' and not within force / rescrape-recent, skip the
        index POST entirely. Non-terminal issues from a prior run are retried from
        the DB.
      - Otherwise fetch the index, parse, and UPSERT discovered issues.

    File skip contract (unchanged): a target file present and non-empty is treated
    as already-downloaded; we still hash it once into the DB so the audit log is
    complete.
    """
    today = today or datetime.now(timezone.utc).date()

    fetch = db is None or db.should_fetch_index(
        day, today, force=force, rescrape_recent_days=rescrape_recent_days
    )

    if fetch:
        try:
            html = _with_retry(lambda: fetch_index(client, day))
        except Exception as exc:
            if db is not None:
                db.record_day_failed(day, str(exc))
            return DayResult(
                day=day,
                found=0,
                downloaded=0,
                skipped=0,
                errors=[f"index: {exc}"],
                fetched_index=True,
            )
        issues = parse_issues(html, part=part)
        if db is not None:
            db.record_day_ok(day, len(issues))
            for issue in issues:
                db.record_issue_discovered(
                    day,
                    issue.part,
                    issue.number,
                    issue.year,
                    issue.url,
                    issue.filename(day),
                )
    else:
        assert db is not None  # fetch=False implies db is set
        issues = _issues_from_db(db, day, part)

    result = DayResult(
        day=day,
        found=len(issues),
        downloaded=0,
        skipped=0,
        errors=[],
        fetched_index=fetch,
    )
    did_download = False
    for issue in issues:
        target = out_dir / issue.filename(day)

        if target.exists() and target.stat().st_size > 0:
            result.skipped += 1
            if db is not None and not db.issue_has_hash(
                day, issue.part, issue.number, issue.year
            ):
                size, digest = _hash_file(target)
                db.record_issue_downloaded(
                    day,
                    issue.part,
                    issue.number,
                    issue.year,
                    size_bytes=size,
                    sha256=digest,
                    downloaded_at=_file_mtime_iso(target),
                    bump_attempts=False,
                )
            if on_event:
                on_event(
                    FileEventPayload(kind="skip", issue=issue, day=day, path=target)
                )
            continue

        if did_download and delay > 0:
            time.sleep(delay)
        try:
            size, digest = _with_retry(
                lambda i=issue, t=target: download_pdf(client, i, t)
            )
            result.downloaded += 1
            did_download = True
            if db is not None:
                db.record_issue_downloaded(
                    day,
                    issue.part,
                    issue.number,
                    issue.year,
                    size_bytes=size,
                    sha256=digest,
                )
            if on_event:
                on_event(
                    FileEventPayload(kind="download", issue=issue, day=day, path=target)
                )
        except (httpx.HTTPError, RuntimeError) as exc:
            msg = f"{issue.url}: {exc}"
            result.errors.append(msg)
            if db is not None:
                db.record_issue_failed(day, issue.part, issue.number, issue.year, msg)
            if on_event:
                on_event(
                    FileEventPayload(
                        kind="error",
                        issue=issue,
                        day=day,
                        path=target,
                        detail=msg,
                    )
                )
    return result

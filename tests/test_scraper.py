from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import httpx
import pytest

from monitorul_ii.db import DB
from monitorul_ii.scraper import (
    BASE_URL,
    Issue,
    _is_transient,
    _with_retry,
    daterange,
    download_pdf,
    parse_issues,
    scrape_day,
)


# --- Issue.filename ---------------------------------------------------------


def test_issue_filename_format():
    issue = Issue(part="II", number="47", year=2026, url=f"{BASE_URL}/x.html")
    assert issue.filename(date(2026, 4, 29)) == "2026-04-29_MO-PII-47-2026.pdf"


def test_issue_filename_letter_suffix():
    issue = Issue(part="II", number="358Bis", year=2026, url=f"{BASE_URL}/x.html")
    assert issue.filename(date(2026, 4, 29)) == "2026-04-29_MO-PII-358Bis-2026.pdf"


# --- parse_issues -----------------------------------------------------------


_INDEX_FRAGMENT = """
<div class="card-body">
  <a href="/Monitorul-Oficial--PII--47--2026.html">PII 47</a>
  <a href="/Monitorul-Oficial--PII--358Bis--2026.html">PII 358Bis</a>
  <a href="/Monitorul-Oficial--PII--12c--2026.html">PII 12c</a>
  <a href="/Monitorul-Oficial--PI--900--2026.html">PI 900</a>
  <a href="/Monitorul-Oficial--PIII--12--2026.html">PIII 12</a>
  <a href="/Monitorul-Oficial--PII--47--2026.html">duplicate</a>
</div>
"""


def test_parse_issues_default_part_ii():
    issues = parse_issues(_INDEX_FRAGMENT)
    assert [i.number for i in issues] == ["47", "358Bis", "12c"]


def test_parse_issues_filters_by_part():
    issues = parse_issues(_INDEX_FRAGMENT, part="I")
    assert len(issues) == 1
    assert issues[0].number == "900"
    assert issues[0].part == "I"


def test_parse_issues_dedupes_repeated_links():
    issues = parse_issues(_INDEX_FRAGMENT)
    assert len(issues) == 3
    nums = [i.number for i in issues]
    assert nums.count("47") == 1


def test_parse_issues_returns_absolute_url():
    issues = parse_issues(_INDEX_FRAGMENT)
    for i in issues:
        assert i.url.startswith(BASE_URL)


def test_parse_issues_empty_fragment_returns_empty_list():
    assert parse_issues("") == []
    assert parse_issues("<div>nothing here</div>") == []


def test_parse_issues_unknown_part_returns_empty():
    assert parse_issues(_INDEX_FRAGMENT, part="VII") == []


# --- daterange --------------------------------------------------------------


def test_daterange_forward_inclusive():
    out = list(daterange(date(2026, 1, 1), date(2026, 1, 3)))
    assert out == [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]


def test_daterange_reverse_inclusive():
    out = list(daterange(date(2026, 1, 1), date(2026, 1, 3), reverse=True))
    assert out == [date(2026, 1, 3), date(2026, 1, 2), date(2026, 1, 1)]


def test_daterange_single_day():
    d = date(2026, 4, 29)
    assert list(daterange(d, d)) == [d]
    assert list(daterange(d, d, reverse=True)) == [d]


def test_daterange_start_after_end_is_empty():
    assert list(daterange(date(2026, 1, 5), date(2026, 1, 1))) == []
    assert list(daterange(date(2026, 1, 5), date(2026, 1, 1), reverse=True)) == []


# --- _is_transient ----------------------------------------------------------


def _http_status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://x")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError("err", request=req, response=resp)


@pytest.mark.parametrize("code", [500, 502, 503, 504, 429])
def test_is_transient_retries_5xx_and_429(code):
    assert _is_transient(_http_status_error(code))


@pytest.mark.parametrize("code", [400, 401, 403, 404, 410])
def test_is_transient_does_not_retry_4xx(code):
    assert not _is_transient(_http_status_error(code))


def test_is_transient_retries_transport_errors():
    assert _is_transient(httpx.ConnectError("boom"))
    assert _is_transient(httpx.ReadTimeout("slow"))


def test_is_transient_does_not_retry_unrelated_exceptions():
    assert not _is_transient(RuntimeError("parse fail"))
    assert not _is_transient(ValueError("bad input"))


# --- _with_retry ------------------------------------------------------------


def test_with_retry_returns_first_success():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return "ok"

    assert _with_retry(fn) == "ok"
    assert calls["n"] == 1


def test_with_retry_recovers_after_transient(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return "ok"

    assert _with_retry(fn) == "ok"
    assert calls["n"] == 3


def test_with_retry_raises_after_exhausting_attempts(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    with pytest.raises(httpx.ConnectError):
        _with_retry(fn)
    assert calls["n"] == 3


def test_with_retry_does_not_retry_non_transient(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _http_status_error(404)

    with pytest.raises(httpx.HTTPStatusError):
        _with_retry(fn)
    assert calls["n"] == 1


def test_with_retry_uses_backoff_sequence(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    def fn():
        raise httpx.ConnectError("boom")

    with pytest.raises(httpx.ConnectError):
        _with_retry(fn, attempts=3, backoff=(1.0, 2.0, 4.0))
    # Three attempts → two sleeps between them (last failure raises).
    assert sleeps == [1.0, 2.0]


# --- download_pdf -----------------------------------------------------------


def _client_with_handler(handler) -> httpx.Client:
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def test_download_pdf_streams_to_file_and_returns_hash(tmp_path: Path):
    body = b"%PDF-1.4 fake-bytes-here-XYZ"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body, headers={"content-type": "application/pdf"}
        )

    issue = Issue(part="II", number="47", year=2026, url=f"{BASE_URL}/x.html")
    target = tmp_path / "out.pdf"
    with _client_with_handler(handler) as client:
        size, digest = download_pdf(client, issue, target)
    assert size == len(body)
    assert target.read_bytes() == body
    # Tmp .part file should have been renamed away.
    assert not target.with_suffix(".pdf.part").exists()
    # Sha256 of body, lowercase hex.
    import hashlib

    assert digest == hashlib.sha256(body).hexdigest()


def test_download_pdf_rejects_non_pdf_content_type(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html>not a pdf</html>",
            headers={"content-type": "text/html"},
        )

    issue = Issue(part="II", number="47", year=2026, url=f"{BASE_URL}/x.html")
    target = tmp_path / "out.pdf"
    with _client_with_handler(handler) as client:
        with pytest.raises(RuntimeError, match="unexpected content-type"):
            download_pdf(client, issue, target)
    assert not target.exists()


# --- scrape_day end-to-end (httpx MockTransport + DB) ----------------------


_PDF_BYTES = b"%PDF-1.4 minimal-fake"


def _success_handler(_pdf_count: int = 2):
    """Returns a handler serving an index of N PDFs and PDFs themselves."""
    fragment_links = "\n".join(
        f'<a href="/Monitorul-Oficial--PII--{i + 1}--2026.html">PII</a>'
        for i in range(_pdf_count)
    )
    fragment = f'<div class="card-body">{fragment_links}</div>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("get_mo.php"):
            return httpx.Response(200, text=fragment)
        return httpx.Response(
            200, content=_PDF_BYTES, headers={"content-type": "application/pdf"}
        )

    return handler


def test_scrape_day_downloads_and_records_in_db(tmp_path: Path, db: DB):
    client = _client_with_handler(_success_handler(2))
    out_dir = tmp_path / "pdfs"
    events = []

    result = scrape_day(
        client,
        date(2026, 4, 29),
        out_dir,
        on_event=events.append,
        db=db,
        today=date(2026, 5, 4),
        delay=0,
    )
    assert result.found == 2
    assert result.downloaded == 2
    assert result.skipped == 0
    assert result.errors == []
    assert result.fetched_index is True
    # Two PDFs landed on disk with the expected naming.
    files = sorted(p.name for p in out_dir.iterdir())
    assert files == [
        "2026-04-29_MO-PII-1-2026.pdf",
        "2026-04-29_MO-PII-2-2026.pdf",
    ]
    # DB has the day marked ok and both issues uploaded-state-pending.
    row = db.conn.execute(
        "SELECT status, issues_found FROM days WHERE date = ?", ("2026-04-29",)
    ).fetchone()
    assert row == ("ok", 2)
    statuses = [
        r[0]
        for r in db.conn.execute(
            "SELECT status FROM issues WHERE date = ? ORDER BY number", ("2026-04-29",)
        )
    ]
    assert statuses == ["downloaded", "downloaded"]


def test_scrape_day_skips_existing_file_and_lazy_hashes(tmp_path: Path, db: DB):
    client = _client_with_handler(_success_handler(1))
    out_dir = tmp_path / "pdfs"
    out_dir.mkdir()
    # Pre-stage the file as if a previous run dropped it.
    existing = out_dir / "2026-04-29_MO-PII-1-2026.pdf"
    existing.write_bytes(b"already-there")

    result = scrape_day(
        client,
        date(2026, 4, 29),
        out_dir,
        db=db,
        today=date(2026, 5, 4),
        delay=0,
    )
    assert result.skipped == 1
    assert result.downloaded == 0
    # Lazy hash got recorded.
    row = db.conn.execute(
        "SELECT sha256, size_bytes FROM issues WHERE date = ?",
        ("2026-04-29",),
    ).fetchone()
    assert row is not None
    sha, size = row
    assert size == len(b"already-there")
    assert sha is not None and len(sha) == 64


def test_scrape_day_uses_db_cache_on_resume(tmp_path: Path, db: DB):
    """Once a day is 'ok' in DB, the second run must not POST the index."""
    client = _client_with_handler(_success_handler(1))
    out_dir = tmp_path / "pdfs"
    today = date(2026, 5, 4)
    day = date(2026, 4, 29)

    # First run: populate DB + filesystem.
    scrape_day(client, day, out_dir, db=db, today=today, delay=0)

    # Second run with a transport that errors on any HTTP call. If the resume
    # gate works, no requests fire because the file already exists.
    def explode(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call HTTP on cached resume")

    cached_client = httpx.Client(transport=httpx.MockTransport(explode))
    with cached_client:
        result = scrape_day(cached_client, day, out_dir, db=db, today=today, delay=0)
    assert result.fetched_index is False
    assert result.found == 0  # _issues_from_db returns only non-terminal rows
    assert result.skipped == 0


def test_scrape_day_records_index_failure_and_returns_error(tmp_path: Path, db: DB):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="nope")

    client = _client_with_handler(handler)
    result = scrape_day(
        client,
        date(2026, 4, 29),
        tmp_path,
        db=db,
        today=date(2026, 5, 4),
        delay=0,
    )
    assert result.found == 0
    assert result.errors and "index" in result.errors[0]
    row = db.conn.execute(
        "SELECT status, last_error FROM days WHERE date = ?", ("2026-04-29",)
    ).fetchone()
    assert row[0] == "failed"
    assert row[1]


def test_scrape_day_emits_events_in_order(tmp_path: Path):
    client = _client_with_handler(_success_handler(2))
    events = []
    scrape_day(
        client,
        date(2026, 4, 29),
        tmp_path,
        on_event=events.append,
        today=date(2026, 5, 4),
        delay=0,
    )
    assert [e.kind for e in events] == ["download", "download"]
    assert all(e.size_bytes == len(_PDF_BYTES) for e in events)

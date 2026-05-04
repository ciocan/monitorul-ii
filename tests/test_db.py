from __future__ import annotations

from datetime import date, timedelta

from monitorul_ii.db import DB


TODAY = date(2026, 5, 4)
PAST = date(2026, 4, 29)
WAY_PAST = date(2025, 1, 1)


# --- should_fetch_index resume gate ----------------------------------------


def test_should_fetch_unknown_day(db: DB):
    """Days not in the table must be fetched."""
    assert db.should_fetch_index(PAST, TODAY, force=False, rescrape_recent_days=0)


def test_should_fetch_today_even_when_marked_ok(db: DB):
    """Publications can land throughout the day; today is always re-fetched."""
    db.record_day_ok(TODAY, issues_found=2)
    assert db.should_fetch_index(TODAY, TODAY, force=False, rescrape_recent_days=0)


def test_should_fetch_future_day(db: DB):
    future = TODAY + timedelta(days=1)
    assert db.should_fetch_index(future, TODAY, force=False, rescrape_recent_days=0)


def test_should_skip_ok_past_day(db: DB):
    db.record_day_ok(PAST, issues_found=3)
    assert not db.should_fetch_index(PAST, TODAY, force=False, rescrape_recent_days=0)


def test_should_skip_ok_past_day_even_when_empty(db: DB):
    """Zero-issues weekends still short-circuit on resume."""
    db.record_day_ok(PAST, issues_found=0)
    assert not db.should_fetch_index(PAST, TODAY, force=False, rescrape_recent_days=0)


def test_should_fetch_failed_day(db: DB):
    db.record_day_failed(PAST, "boom")
    assert db.should_fetch_index(PAST, TODAY, force=False, rescrape_recent_days=0)


def test_force_overrides_ok(db: DB):
    db.record_day_ok(PAST, issues_found=3)
    assert db.should_fetch_index(PAST, TODAY, force=True, rescrape_recent_days=0)


def test_rescrape_recent_window_overrides_ok(db: DB):
    """Days within the rescrape-recent window are always re-fetched."""
    recent = TODAY - timedelta(days=2)
    db.record_day_ok(recent, issues_found=1)
    assert db.should_fetch_index(recent, TODAY, force=False, rescrape_recent_days=3)


def test_rescrape_recent_does_not_extend_to_older_days(db: DB):
    older = TODAY - timedelta(days=30)
    db.record_day_ok(older, issues_found=1)
    assert not db.should_fetch_index(older, TODAY, force=False, rescrape_recent_days=3)


# --- record_day_* state transitions ----------------------------------------


def test_record_day_ok_inserts_then_updates(db: DB):
    db.record_day_ok(PAST, issues_found=2)
    db.record_day_ok(PAST, issues_found=5)  # second call updates issues_found
    row = db.conn.execute(
        "SELECT status, issues_found, last_error FROM days WHERE date = ?",
        (PAST.isoformat(),),
    ).fetchone()
    assert row == ("ok", 5, None)


def test_record_day_failed_then_ok_clears_error(db: DB):
    db.record_day_failed(PAST, "first error")
    db.record_day_ok(PAST, issues_found=1)
    row = db.conn.execute(
        "SELECT status, last_error FROM days WHERE date = ?", (PAST.isoformat(),)
    ).fetchone()
    assert row == ("ok", None)


def test_record_day_failed_then_failed_keeps_latest_error(db: DB):
    db.record_day_failed(PAST, "first")
    db.record_day_failed(PAST, "second")
    row = db.conn.execute(
        "SELECT status, last_error FROM days WHERE date = ?", (PAST.isoformat(),)
    ).fetchone()
    assert row == ("failed", "second")


# --- record_issue_* state transitions --------------------------------------


def _seed_day(db: DB, day: date = PAST) -> None:
    db.record_day_ok(day, issues_found=1)
    db.record_issue_discovered(
        day, "II", "47", 2026, f"https://x/y/{day}.html", f"{day}_MO-PII-47-2026.pdf"
    )


def test_record_issue_discovered_inserts_pending(db: DB):
    _seed_day(db)
    assert db.issue_status(PAST, "II", "47", 2026) == "pending"
    assert not db.issue_has_hash(PAST, "II", "47", 2026)


def test_record_issue_discovered_idempotent_keeps_status(db: DB):
    _seed_day(db)
    db.record_issue_downloaded(PAST, "II", "47", 2026, size_bytes=10, sha256="a" * 64)
    assert db.issue_status(PAST, "II", "47", 2026) == "downloaded"
    # A second discover (e.g. on rescrape) must not reset the status.
    db.record_issue_discovered(PAST, "II", "47", 2026, "https://x/new", "newname.pdf")
    assert db.issue_status(PAST, "II", "47", 2026) == "downloaded"


def test_record_issue_downloaded_records_hash_and_size(db: DB):
    _seed_day(db)
    db.record_issue_downloaded(PAST, "II", "47", 2026, size_bytes=512, sha256="b" * 64)
    row = db.conn.execute(
        "SELECT status, size_bytes, sha256, attempts FROM issues "
        "WHERE date=? AND part=? AND number=? AND year=?",
        (PAST.isoformat(), "II", "47", 2026),
    ).fetchone()
    assert row == ("downloaded", 512, "b" * 64, 1)


def test_record_issue_downloaded_no_bump_attempts_for_lazy_hash(db: DB):
    """Lazy hashing of pre-existing files must not bump the attempt counter."""
    _seed_day(db)
    db.record_issue_downloaded(
        PAST,
        "II",
        "47",
        2026,
        size_bytes=10,
        sha256="c" * 64,
        downloaded_at="2026-04-29T12:00:00Z",
        bump_attempts=False,
    )
    row = db.conn.execute(
        "SELECT downloaded_at, attempts FROM issues WHERE date=?",
        (PAST.isoformat(),),
    ).fetchone()
    assert row == ("2026-04-29T12:00:00Z", 0)


def test_record_issue_uploaded_sets_etag_and_status(db: DB):
    _seed_day(db)
    db.record_issue_downloaded(PAST, "II", "47", 2026, size_bytes=10, sha256="d" * 64)
    db.record_issue_uploaded(PAST, "II", "47", 2026, s3_etag="etag-1")
    row = db.conn.execute(
        "SELECT status, s3_etag, uploaded_at FROM issues WHERE date=?",
        (PAST.isoformat(),),
    ).fetchone()
    assert row[0] == "uploaded"
    assert row[1] == "etag-1"
    assert row[2] is not None


def test_record_issue_failed_bumps_attempts_and_keeps_error(db: DB):
    _seed_day(db)
    db.record_issue_failed(PAST, "II", "47", 2026, "boom-1")
    db.record_issue_failed(PAST, "II", "47", 2026, "boom-2")
    row = db.conn.execute(
        "SELECT status, attempts, last_error FROM issues WHERE date=?",
        (PAST.isoformat(),),
    ).fetchone()
    assert row == ("failed", 2, "boom-2")


# --- read helpers ----------------------------------------------------------


def test_issue_status_returns_none_for_missing(db: DB):
    assert db.issue_status(PAST, "II", "999", 2026) is None


def test_issue_has_hash_false_until_downloaded(db: DB):
    _seed_day(db)
    assert not db.issue_has_hash(PAST, "II", "47", 2026)
    db.record_issue_downloaded(PAST, "II", "47", 2026, size_bytes=1, sha256="e" * 64)
    assert db.issue_has_hash(PAST, "II", "47", 2026)


def test_issues_for_day_filters_by_part_and_orders(db: DB):
    db.record_day_ok(PAST, issues_found=3)
    db.record_issue_discovered(PAST, "II", "47", 2026, "u", "f1")
    db.record_issue_discovered(PAST, "II", "12", 2026, "u", "f2")
    db.record_issue_discovered(PAST, "I", "900", 2026, "u", "f3")
    rows = list(db.issues_for_day(PAST, "II"))
    nums = [r["number"] for r in rows]
    # Order is (year, number) ascending — string compare on number means
    # "12" < "47" lexicographically too, so this also covers the sort.
    assert nums == ["12", "47"]
    assert all(r["part"] == "II" for r in rows)


def test_issues_for_day_empty_when_no_match(db: DB):
    assert list(db.issues_for_day(PAST, "II")) == []


# --- bootstrap / lifecycle -------------------------------------------------


def test_db_bootstraps_schema_on_missing_file(tmp_path):
    path = tmp_path / "nested" / "audit.db"
    with DB(path) as d:
        # Smoke test — should_fetch_index requires both tables to exist.
        assert d.should_fetch_index(PAST, TODAY, force=False, rescrape_recent_days=0)
    assert path.exists()


def test_db_reopens_existing(tmp_path):
    path = tmp_path / "audit.db"
    with DB(path) as d:
        d.record_day_ok(WAY_PAST, issues_found=4)
    with DB(path) as d:
        row = d.conn.execute(
            "SELECT issues_found FROM days WHERE date = ?",
            (WAY_PAST.isoformat(),),
        ).fetchone()
        assert row == (4,)

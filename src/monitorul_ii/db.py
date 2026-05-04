from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

_DDL = """
CREATE TABLE IF NOT EXISTS days (
    date          TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    issues_found  INTEGER NOT NULL DEFAULT 0,
    attempted_at  TEXT NOT NULL,
    completed_at  TEXT,
    last_error    TEXT
);

CREATE TABLE IF NOT EXISTS issues (
    date          TEXT NOT NULL REFERENCES days(date),
    part          TEXT NOT NULL,
    number        TEXT NOT NULL,
    year          INTEGER NOT NULL,
    url           TEXT NOT NULL,
    filename      TEXT NOT NULL,
    status        TEXT NOT NULL,
    size_bytes    INTEGER,
    sha256        TEXT,
    s3_etag       TEXT,
    downloaded_at TEXT,
    uploaded_at   TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    PRIMARY KEY (date, part, number, year)
);

CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);
"""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mtime_iso(path: Path) -> str:
    ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


class DB:
    """SQLite-backed audit log + resume gate.

    See docs/architecture.md for the resume contract. In short: this DB decides
    whether to re-POST a day's index; the filesystem and S3 still gate per-PDF
    skips. Failure modes elsewhere (network, parse, upload) flow into rows here
    so re-runs can pick up the loose ends.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_DDL)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> DB:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---- resume gate ----

    def should_fetch_index(
        self,
        day: date,
        today: date,
        *,
        force: bool,
        rescrape_recent_days: int,
    ) -> bool:
        if force:
            return True
        if day >= today:
            return True
        if rescrape_recent_days > 0 and day >= today - timedelta(
            days=rescrape_recent_days
        ):
            return True
        row = self.conn.execute(
            "SELECT status FROM days WHERE date = ?", (day.isoformat(),)
        ).fetchone()
        if row is None:
            return True
        return row[0] != "ok"

    # ---- day rows ----

    def record_day_ok(self, day: date, issues_found: int) -> None:
        now = _utc_iso()
        self.conn.execute(
            """
            INSERT INTO days (date, status, issues_found, attempted_at, completed_at, last_error)
            VALUES (?, 'ok', ?, ?, ?, NULL)
            ON CONFLICT(date) DO UPDATE SET
                status='ok',
                issues_found=excluded.issues_found,
                attempted_at=excluded.attempted_at,
                completed_at=excluded.completed_at,
                last_error=NULL
            """,
            (day.isoformat(), issues_found, now, now),
        )

    def record_day_failed(self, day: date, error: str) -> None:
        now = _utc_iso()
        self.conn.execute(
            """
            INSERT INTO days (date, status, issues_found, attempted_at, completed_at, last_error)
            VALUES (?, 'failed', 0, ?, NULL, ?)
            ON CONFLICT(date) DO UPDATE SET
                status='failed',
                attempted_at=excluded.attempted_at,
                completed_at=NULL,
                last_error=excluded.last_error
            """,
            (day.isoformat(), now, error),
        )

    # ---- issue rows ----

    def record_issue_discovered(
        self,
        day: date,
        part: str,
        number: str,
        year: int,
        url: str,
        filename: str,
    ) -> None:
        """UPSERT — keeps the existing status (downloaded/uploaded) on conflict."""
        self.conn.execute(
            """
            INSERT INTO issues (date, part, number, year, url, filename, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            ON CONFLICT(date, part, number, year) DO UPDATE SET
                url=excluded.url,
                filename=excluded.filename
            """,
            (day.isoformat(), part, number, year, url, filename),
        )

    def record_issue_downloaded(
        self,
        day: date,
        part: str,
        number: str,
        year: int,
        *,
        size_bytes: int,
        sha256: str,
        downloaded_at: str | None = None,
        bump_attempts: bool = True,
    ) -> None:
        ts = downloaded_at or _utc_iso()
        attempts_clause = "attempts=attempts+1," if bump_attempts else ""
        self.conn.execute(
            f"""
            UPDATE issues
            SET status='downloaded',
                size_bytes=?,
                sha256=?,
                downloaded_at=?,
                {attempts_clause}
                last_error=NULL
            WHERE date=? AND part=? AND number=? AND year=?
            """,
            (size_bytes, sha256, ts, day.isoformat(), part, number, year),
        )

    def record_issue_uploaded(
        self,
        day: date,
        part: str,
        number: str,
        year: int,
        *,
        s3_etag: str | None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE issues
            SET status='uploaded',
                s3_etag=?,
                uploaded_at=?
            WHERE date=? AND part=? AND number=? AND year=?
            """,
            (s3_etag, _utc_iso(), day.isoformat(), part, number, year),
        )

    def record_issue_failed(
        self,
        day: date,
        part: str,
        number: str,
        year: int,
        error: str,
    ) -> None:
        self.conn.execute(
            """
            UPDATE issues
            SET status='failed',
                attempts=attempts+1,
                last_error=?
            WHERE date=? AND part=? AND number=? AND year=?
            """,
            (error, day.isoformat(), part, number, year),
        )

    def record_issue_gone(
        self,
        day: date,
        part: str,
        number: str,
        year: int,
        error: str,
    ) -> None:
        """Permanent failure — server says this isn't a PDF (content-type mismatch
        or 4xx-not-429). Terminal status; not auto-retried on resume."""
        self.conn.execute(
            """
            UPDATE issues
            SET status='gone',
                attempts=attempts+1,
                last_error=?
            WHERE date=? AND part=? AND number=? AND year=?
            """,
            (error, day.isoformat(), part, number, year),
        )

    def reset_gone(self) -> int:
        """Move every 'gone' issue back to 'pending' so a future run re-attempts it.
        Returns the number of rows touched."""
        cur = self.conn.execute(
            "UPDATE issues SET status='pending', last_error=NULL WHERE status='gone'"
        )
        return cur.rowcount

    # ---- read helpers ----

    def issue_status(self, day: date, part: str, number: str, year: int) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM issues WHERE date=? AND part=? AND number=? AND year=?",
            (day.isoformat(), part, number, year),
        ).fetchone()
        return row[0] if row else None

    def issue_has_hash(self, day: date, part: str, number: str, year: int) -> bool:
        row = self.conn.execute(
            "SELECT sha256 FROM issues WHERE date=? AND part=? AND number=? AND year=?",
            (day.isoformat(), part, number, year),
        ).fetchone()
        return bool(row and row[0])

    def issues_for_day(self, day: date, part: str) -> Iterable[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute(
            """
            SELECT date, part, number, year, url, filename, status
            FROM issues
            WHERE date=? AND part=?
            ORDER BY year, number
            """,
            (day.isoformat(), part),
        )
        rows = cur.fetchall()
        self.conn.row_factory = None
        return rows

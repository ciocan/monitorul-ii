from __future__ import annotations

import json
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

-- es_indexed: per-document indexer state (Q6 of elasticsearch-indexing.md).
-- The triple (sidecar_content_sha, enrichment_fingerprint, index_generation)
-- is the idempotency key — the indexer skips a document whose state row's
-- triple matches the current run's. `child_record_ids` is a JSON list of
-- every record_id projected to ES last time, used by the orphan-delete diff.
CREATE TABLE IF NOT EXISTS es_indexed (
    document_id            TEXT PRIMARY KEY,
    sidecar_content_sha    TEXT NOT NULL,
    enrichment_fingerprint TEXT NOT NULL,
    index_generation       TEXT NOT NULL,
    indexed_at             INTEGER NOT NULL,
    child_record_ids       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_es_indexed_generation ON es_indexed(index_generation);
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

    # ---- ES indexer state (Q6 of elasticsearch-indexing.md) ----

    def get_indexed_state(self, document_id: str) -> dict[str, object] | None:
        """Return the last-known indexer state for this document, or
        None when never indexed. The dict mirrors the row columns plus
        a deserialised `child_record_ids` list.
        """
        row = self.conn.execute(
            """
            SELECT document_id, sidecar_content_sha, enrichment_fingerprint,
                   index_generation, indexed_at, child_record_ids
            FROM es_indexed
            WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            child_ids = json.loads(row[5]) if row[5] else []
        except json.JSONDecodeError:
            child_ids = []
        return {
            "document_id": row[0],
            "sidecar_content_sha": row[1],
            "enrichment_fingerprint": row[2],
            "index_generation": row[3],
            "indexed_at": row[4],
            "child_record_ids": child_ids,
        }

    def set_indexed_state(
        self,
        document_id: str,
        *,
        sidecar_content_sha: str,
        enrichment_fingerprint: str,
        index_generation: str,
        child_record_ids: list[str],
        indexed_at: int | None = None,
    ) -> None:
        """Upsert the indexer state row for `document_id`.

        `indexed_at` defaults to now (epoch seconds, integer for compact
        SQLite storage). `child_record_ids` is JSON-serialised; the
        indexer relies on the round-trip via `get_indexed_state` for
        the orphan-delete diff.
        """
        if indexed_at is None:
            indexed_at = int(datetime.now(timezone.utc).timestamp())
        self.conn.execute(
            """
            INSERT INTO es_indexed (
                document_id, sidecar_content_sha, enrichment_fingerprint,
                index_generation, indexed_at, child_record_ids
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                sidecar_content_sha=excluded.sidecar_content_sha,
                enrichment_fingerprint=excluded.enrichment_fingerprint,
                index_generation=excluded.index_generation,
                indexed_at=excluded.indexed_at,
                child_record_ids=excluded.child_record_ids
            """,
            (
                document_id,
                sidecar_content_sha,
                enrichment_fingerprint,
                index_generation,
                indexed_at,
                json.dumps(child_record_ids, ensure_ascii=False),
            ),
        )

    def delete_indexed_state(self, document_id: str) -> None:
        """Drop the state row entirely — used when the operator wants
        to force an indexer re-run from scratch (the next pass will see
        no prior state and project + upsert without an idempotency
        short-circuit). Orphan-delete still runs against ES via the
        document_id query.
        """
        self.conn.execute(
            "DELETE FROM es_indexed WHERE document_id = ?",
            (document_id,),
        )

    def list_indexed_state(
        self, *, generation: str | None = None
    ) -> list[dict[str, object]]:
        """Return every indexer state row, optionally filtered by
        generation. Used by the blue-green helpers to iterate the
        documents that still need to ride into a new generation.
        """
        if generation is not None:
            cur = self.conn.execute(
                """
                SELECT document_id, sidecar_content_sha, enrichment_fingerprint,
                       index_generation, indexed_at, child_record_ids
                FROM es_indexed
                WHERE index_generation = ?
                ORDER BY document_id
                """,
                (generation,),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT document_id, sidecar_content_sha, enrichment_fingerprint,
                       index_generation, indexed_at, child_record_ids
                FROM es_indexed
                ORDER BY document_id
                """
            )
        rows = cur.fetchall()
        out: list[dict[str, object]] = []
        for row in rows:
            try:
                child_ids = json.loads(row[5]) if row[5] else []
            except json.JSONDecodeError:
                child_ids = []
            out.append(
                {
                    "document_id": row[0],
                    "sidecar_content_sha": row[1],
                    "enrichment_fingerprint": row[2],
                    "index_generation": row[3],
                    "indexed_at": row[4],
                    "child_record_ids": child_ids,
                }
            )
        return out

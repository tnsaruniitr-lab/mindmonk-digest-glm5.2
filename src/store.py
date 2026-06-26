"""Processed-video state store with two interchangeable backends.

- ``PostgresStore``: used in production when ``DATABASE_URL`` is set
  (e.g. on Railway). Survives container restarts.
- ``SQLiteStore``: local-dev fallback when no ``DATABASE_URL`` is present.

Both expose the identical ``Store`` protocol (``is_processed``, ``get``,
``list_recent``, ``mark_done``, ``mark_skipped``, ``mark_failed``, ``close``),
so ``pipeline.py`` never branches on backend.

``Store(db_path)`` is the factory: pick backend by presence of DATABASE_URL.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .models import ProcessedVideo


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #
def Store(db_path: Path | None = None):  # noqa: N802 - factory named like a class
    """Factory: return a Postgres-backed store if DATABASE_URL is set, else SQLite.

    Args:
        db_path: required for the SQLite fallback (local dev). Ignored when
            DATABASE_URL is present.
    """
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        return PostgresStore(database_url)
    if db_path is None:
        raise ValueError("db_path is required when DATABASE_URL is not set")
    return SQLiteStore(db_path)


# --------------------------------------------------------------------------- #
# Postgres backend
# --------------------------------------------------------------------------- #
_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_videos (
    video_id     TEXT PRIMARY KEY,
    channel_id   TEXT NOT NULL,
    status       TEXT NOT NULL,          -- "done" | "skipped" | "failed"
    processed_at TEXT NOT NULL,          -- ISO 8601
    summary      TEXT,
    note         TEXT
);
"""


class PostgresStore:
    """Postgres-backed store. One connection per process; autocommit.

    Railway injects ``DATABASE_URL`` from the linked Postgres service. We
    require SSL when the connection is over the public hostname and tolerate
    it otherwise (internal networking). psycopg3 is connection-pooled by the
    process; the scheduler runs one pipeline at a time, so a single connection
    guarded by a lock is sufficient.
    """

    def __init__(self, database_url: str):
        import psycopg  # imported lazily so local dev without psycopg works

        self._lock = threading.Lock()
        # Railway Postgres URLs support sslmode query; default to require for
        # safety unless the caller already specified sslmode.
        if "sslmode" not in database_url:
            database_url = database_url + (
                "&sslmode=require" if "?" in database_url else "?sslmode=require"
            )
        self._conn = psycopg.connect(database_url, autocommit=True)
        self._conn.execute(_PG_SCHEMA)
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS processed_videos_pkey "
            "ON processed_videos(video_id)"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def is_processed(self, video_id: str) -> bool:
        """True only if this video reached a terminal-SUCCESS state.

        ``failed`` rows are NOT treated as processed — they should be retried
        on subsequent cycles (e.g. after a bug fix or transient outage).
        """
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM processed_videos "
                    "WHERE video_id = %s AND status IN ('done', 'skipped') LIMIT 1",
                    (video_id,),
                )
                return cur.fetchone() is not None

    def reset_failed(self) -> int:
        """Delete all ``failed`` rows so they reprocess next cycle. Returns count."""
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute("DELETE FROM processed_videos WHERE status = 'failed'")
                return cur.rowcount

    def get(self, video_id: str) -> ProcessedVideo | None:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT video_id, channel_id, status, processed_at, "
                    "summary, note FROM processed_videos WHERE video_id = %s",
                    (video_id,),
                )
                row = cur.fetchone()
        return _pg_row_to_processed(row) if row else None

    def list_recent(self, limit: int = 50) -> list[ProcessedVideo]:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT video_id, channel_id, status, processed_at, summary, "
                    "note FROM processed_videos ORDER BY processed_at DESC LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()
        return [_pg_row_to_processed(r) for r in rows]

    def mark_done(
        self, video_id: str, channel_id: str, summary: str | None = None
    ) -> None:
        self._upsert(video_id, channel_id, "done", summary=summary)

    def mark_skipped(
        self, video_id: str, channel_id: str, note: str | None = None
    ) -> None:
        self._upsert(video_id, channel_id, "skipped", note=note)

    def mark_failed(
        self, video_id: str, channel_id: str, note: str | None = None
    ) -> None:
        self._upsert(video_id, channel_id, "failed", note=note)

    def _upsert(
        self,
        video_id: str,
        channel_id: str,
        status: str,
        summary: str | None = None,
        note: str | None = None,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO processed_videos
                        (video_id, channel_id, status, processed_at, summary, note)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (video_id) DO UPDATE SET
                        status       = EXCLUDED.status,
                        processed_at = EXCLUDED.processed_at,
                        summary      = EXCLUDED.summary,
                        note         = EXCLUDED.note
                    """,
                    (video_id, channel_id, status, now, summary, note),
                )


def _pg_row_to_processed(row) -> ProcessedVideo:
    return ProcessedVideo(
        video_id=row[0],
        channel_id=row[1],
        status=row[2],
        processed_at=datetime.fromisoformat(row[3]),
        summary=row[4],
        note=row[5],
    )


# --------------------------------------------------------------------------- #
# SQLite backend (local dev fallback)
# --------------------------------------------------------------------------- #
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_videos (
    video_id     TEXT PRIMARY KEY,
    channel_id   TEXT NOT NULL,
    status       TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    summary      TEXT,
    note         TEXT
);
"""


class SQLiteStore:
    """Local-dev SQLite store. Same interface as PostgresStore."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SQLITE_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def is_processed(self, video_id: str) -> bool:
        """True only for terminal-success states; ``failed`` is retryable."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM processed_videos "
                "WHERE video_id = ? AND status IN ('done', 'skipped') LIMIT 1",
                (video_id,),
            ).fetchone()
        return row is not None

    def reset_failed(self) -> int:
        """Delete all ``failed`` rows so they reprocess next cycle."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM processed_videos WHERE status = 'failed'"
            )
            return cur.rowcount

    def get(self, video_id: str) -> ProcessedVideo | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM processed_videos WHERE video_id = ?",
                (video_id,),
            ).fetchone()
        return _sqlite_row_to_processed(row) if row else None

    def list_recent(self, limit: int = 50) -> list[ProcessedVideo]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM processed_videos ORDER BY processed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_sqlite_row_to_processed(r) for r in rows]

    def mark_done(
        self, video_id: str, channel_id: str, summary: str | None = None
    ) -> None:
        self._upsert(video_id, channel_id, "done", summary=summary)

    def mark_skipped(
        self, video_id: str, channel_id: str, note: str | None = None
    ) -> None:
        self._upsert(video_id, channel_id, "skipped", note=note)

    def mark_failed(
        self, video_id: str, channel_id: str, note: str | None = None
    ) -> None:
        self._upsert(video_id, channel_id, "failed", note=note)

    def _upsert(
        self,
        video_id: str,
        channel_id: str,
        status: str,
        summary: str | None = None,
        note: str | None = None,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO processed_videos
                    (video_id, channel_id, status, processed_at, summary, note)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    status       = excluded.status,
                    processed_at = excluded.processed_at,
                    summary      = excluded.summary,
                    note         = excluded.note
                """,
                (video_id, channel_id, status, now, summary, note),
            )


def _sqlite_row_to_processed(row) -> ProcessedVideo:
    return ProcessedVideo(
        video_id=row["video_id"],
        channel_id=row["channel_id"],
        status=row["status"],
        processed_at=datetime.fromisoformat(row["processed_at"]),
        summary=row["summary"],
        note=row["note"],
    )

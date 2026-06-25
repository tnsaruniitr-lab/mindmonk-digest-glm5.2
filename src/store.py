"""SQLite-backed state store.

Tracks every video we've ever seen so processing is idempotent: a video is
processed once across restarts and poll cycles. Uses a single table with a
unique constraint on (video_id); ``mark_*`` methods upsert.

Concurrency: the scheduler runs one pipeline at a time, so we open a single
connection per process with ``check_same_thread=False`` for safety.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .models import ProcessedVideo

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_videos (
    video_id     TEXT PRIMARY KEY,
    channel_id   TEXT NOT NULL,
    status       TEXT NOT NULL,          -- "done" | "skipped" | "failed"
    processed_at TEXT NOT NULL,          -- ISO 8601
    summary      TEXT,
    note         TEXT
);
"""


class Store:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._lock = threading.Lock()
        # check_same_thread=False: scheduler/background threads may call in.
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def is_processed(self, video_id: str) -> bool:
        """True if this video already has a terminal-status row."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM processed_videos WHERE video_id = ? LIMIT 1",
                (video_id,),
            ).fetchone()
        return row is not None

    def get(self, video_id: str) -> ProcessedVideo | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM processed_videos WHERE video_id = ?",
                (video_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_processed(row)

    def list_recent(self, limit: int = 50) -> list[ProcessedVideo]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM processed_videos "
                "ORDER BY processed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_processed(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Mutations (idempotent upserts)
    # ------------------------------------------------------------------ #
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


def _row_to_processed(row: sqlite3.Row) -> ProcessedVideo:
    return ProcessedVideo(
        video_id=row["video_id"],
        channel_id=row["channel_id"],
        status=row["status"],
        processed_at=datetime.fromisoformat(row["processed_at"]),
        summary=row["summary"],
        note=row["note"],
    )

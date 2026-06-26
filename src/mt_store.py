"""Multi-tenant store — user-scoped access to the Phase 1 schema.

This is the new data layer for multi-user operation. Every method is scoped
by ``user_id`` (resolved from telegram_chat_id at the bot layer). Videos,
channels, transcripts, and summaries are global (shared); digests and
subscriptions are per-user.

Design:
  - get_or_create_user(chat_id) → user_id  (onboarding)
  - add_channel / list_channels / remove_channel are per-user (subscriptions)
  - digest_done / digest_skipped / is_digested / get_digest are per-user
  - transcript/summary caching is global (video-scoped) — Phase 3 uses these

The legacy Store (processed_videos) stays untouched; this runs alongside it
until the pipeline fully migrates.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class MultiTenantStore:
    """Postgres-backed, user-scoped store for the multi-tenant schema."""

    def __init__(self, database_url: str, schema: str = "public"):
        import psycopg

        # Normalize the URL for psycopg3 if needed.
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        # Require SSL only for remote hosts (Railway/production). Local dev
        # and CI localhost Postgres don't support SSL and would fail.
        is_local = any(
            h in database_url for h in ("localhost", "127.0.0.1", "::1", "0.0.0.0")
        )
        if not is_local and "sslmode" not in database_url:
            if "?" not in database_url:
                database_url += "?sslmode=require"
            else:
                database_url += "&sslmode=require"

        self._conn = psycopg.connect(database_url, autocommit=True)
        # Optionally isolate to a test schema (for integration tests).
        if schema != "public":
            with self._conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}, public")

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ #
    # Users
    # ------------------------------------------------------------------ #
    def get_or_create_user(
        self, telegram_chat_id: str, telegram_user_id: str, profile_yaml: str = ""
    ) -> int:
        """Return the user_id for a telegram chat, creating the user if new."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM users WHERE telegram_chat_id = %s",
                (telegram_chat_id,),
            )
            row = cur.fetchone()
            if row:
                return int(row[0])
            cur.execute(
                """
                INSERT INTO users (telegram_chat_id, telegram_user_id, profile_yaml)
                VALUES (%s, %s, %s) RETURNING id
                """,
                (telegram_chat_id, telegram_user_id, profile_yaml),
            )
            row = cur.fetchone()
            assert row is not None
            user_id = int(row[0])
            log.info("Created new user id=%s for chat_id=%s", user_id, telegram_chat_id)
            return user_id

    def get_user_profile(self, user_id: int) -> str:
        with self._conn.cursor() as cur:
            cur.execute("SELECT profile_yaml FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return row[0] if row else ""

    def set_user_profile(self, user_id: int, profile_yaml: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET profile_yaml = %s WHERE id = %s",
                (profile_yaml, user_id),
            )

    # ------------------------------------------------------------------ #
    # Channels + subscriptions (per-user)
    # ------------------------------------------------------------------ #
    def add_channel(self, user_id: int, name: str, url: str) -> bool:
        """Subscribe a user to a channel. Creates the channel if new.

        Returns True if a new subscription was created, False if already subscribed.
        """
        with self._conn.cursor() as cur:
            # Upsert the channel (global).
            cur.execute(
                """
                INSERT INTO channels (name, url)
                VALUES (%s, %s)
                ON CONFLICT (url) DO UPDATE SET name = EXCLUDED.name
                RETURNING id
                """,
                (name, url),
            )
            row = cur.fetchone()
            assert row is not None
            channel_id = int(row[0])
            # Subscribe (per-user). ON CONFLICT = already subscribed.
            cur.execute(
                """
                INSERT INTO subscriptions (user_id, channel_id)
                VALUES (%s, %s)
                ON CONFLICT (user_id, channel_id) DO NOTHING
                RETURNING user_id
                """,
                (user_id, channel_id),
            )
            created = cur.fetchone() is not None
            if created:
                log.info(
                    "User %s subscribed to channel %s (%s)", user_id, name, channel_id
                )
            return created

    def list_channels(self, user_id: int) -> list[dict[str, Any]]:
        """Return the channels a user is subscribed to."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.name, c.url
                FROM channels c
                JOIN subscriptions s ON s.channel_id = c.id
                WHERE s.user_id = %s
                ORDER BY c.name
                """,
                (user_id,),
            )
            return [{"id": r[0], "name": r[1], "url": r[2]} for r in cur.fetchall()]

    def remove_channel(self, user_id: int, index: int) -> dict[str, Any] | None:
        """Remove a subscription by its 0-based index in list_channels."""
        channels = self.list_channels(user_id)
        if index < 0 or index >= len(channels):
            return None
        removed = channels[index]
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM subscriptions WHERE user_id = %s AND channel_id = %s",
                (user_id, removed["id"]),
            )
        return removed

    # ------------------------------------------------------------------ #
    # Digests (per-user)
    # ------------------------------------------------------------------ #
    def is_digested(self, user_id: int, youtube_id: str) -> bool:
        """True if this user already has a terminal-status digest for the video."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM digests d
                JOIN videos v ON v.id = d.video_id
                WHERE d.user_id = %s AND v.youtube_id = %s
                  AND d.status IN ('done', 'skipped')
                LIMIT 1
                """,
                (user_id, youtube_id),
            )
            return cur.fetchone() is not None

    def get_digest(self, user_id: int, youtube_id: str) -> str | None:
        """Return a cached full_brief if the user has a done digest, else None."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.full_brief FROM digests d
                JOIN videos v ON v.id = d.video_id
                WHERE d.user_id = %s AND v.youtube_id = %s AND d.status = 'done'
                """,
                (user_id, youtube_id),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def get_or_create_video(self, youtube_id: str, title: str = "") -> int:
        """Upsert a video (global) and return its id."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO videos (youtube_id, title)
                VALUES (%s, %s)
                ON CONFLICT (youtube_id) DO UPDATE SET title = EXCLUDED.title
                RETURNING id
                """,
                (youtube_id, title),
            )
            row = cur.fetchone()
            assert row is not None
            return int(row[0])

    def mark_digest_done(
        self, user_id: int, video_id: int, full_brief: str, cost_usd: float = 0
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO digests (user_id, video_id, full_brief, status, delivered_at, cost_usd)
                VALUES (%s, %s, %s, 'done', now(), %s)
                ON CONFLICT (user_id, video_id) DO UPDATE SET
                    full_brief = EXCLUDED.full_brief,
                    status = EXCLUDED.status,
                    delivered_at = EXCLUDED.delivered_at
                """,
                (user_id, video_id, full_brief, cost_usd),
            )

    def mark_digest_skipped(self, user_id: int, video_id: int, note: str = "") -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO digests (user_id, video_id, status)
                VALUES (%s, %s, 'skipped')
                ON CONFLICT (user_id, video_id) DO UPDATE SET status = 'skipped'
                """,
                (user_id, video_id),
            )

    def mark_digest_failed(self, user_id: int, video_id: int, note: str = "") -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO digests (user_id, video_id, status)
                VALUES (%s, %s, 'failed')
                ON CONFLICT (user_id, video_id) DO UPDATE SET status = 'failed'
                """,
                (user_id, video_id),
            )

    def latest_digest(self, user_id: int) -> str | None:
        """Return the most recent done full_brief for a user."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT full_brief FROM digests
                WHERE user_id = %s AND status = 'done' AND full_brief != ''
                ORDER BY delivered_at DESC NULLS LAST LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def user_stats(self, user_id: int) -> dict[str, Any]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM subscriptions WHERE user_id = %s", (user_id,)
            )
            row = cur.fetchone()
            channels = row[0] if row else 0
            cur.execute(
                "SELECT status, count(*) FROM digests WHERE user_id = %s GROUP BY status",
                (user_id,),
            )
            status_counts = {r[0]: r[1] for r in cur.fetchall()}
        return {
            "channels": channels,
            "done": status_counts.get("done", 0),
            "skipped": status_counts.get("skipped", 0),
            "failed": status_counts.get("failed", 0),
        }

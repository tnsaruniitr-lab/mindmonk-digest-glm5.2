"""multi-tenant schema: users, channels, subscriptions, videos, transcripts, summaries, digests, usage

Revision ID: 38881c9c6cd5
Revises: ab61b1511739
Create Date: 2026-06-26 21:13:27.682130

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "38881c9c6cd5"
down_revision: Union[str, Sequence[str], None] = "ab61b1511739"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the multi-tenant schema.

    All tables use IF NOT EXISTS so this is safe to run on databases that
    already have the legacy `processed_videos` table. The legacy table is
    NOT dropped here — the backfill migration reads from it, then a later
    migration can drop it once backfill is verified.
    """
    # --- users ---
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id                BIGSERIAL PRIMARY KEY,
            telegram_chat_id  TEXT NOT NULL UNIQUE,
            telegram_user_id  TEXT NOT NULL UNIQUE,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            tier              TEXT NOT NULL DEFAULT 'free',
            llm_provider      TEXT NOT NULL DEFAULT '',
            llm_api_key_enc   TEXT NOT NULL DEFAULT '',
            profile_yaml      TEXT NOT NULL DEFAULT '',
            preferences_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
            usage_reset_at    TIMESTAMPTZ,
            deleted_at        TIMESTAMPTZ
        )
        """
    )

    # --- channels (global — one row per unique YouTube channel) ---
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
            id                BIGSERIAL PRIMARY KEY,
            youtube_handle    TEXT,
            name              TEXT NOT NULL,
            url               TEXT NOT NULL UNIQUE,
            last_polled_at    TIMESTAMPTZ,
            poll_error_count  INTEGER NOT NULL DEFAULT 0,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # --- subscriptions (many-to-many: users ↔ channels) ---
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            channel_id   BIGINT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            added_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, channel_id)
        )
        """
    )

    # --- videos (global — one row per unique YouTube video) ---
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            id                BIGSERIAL PRIMARY KEY,
            channel_id        BIGINT REFERENCES channels(id) ON DELETE SET NULL,
            youtube_id        TEXT NOT NULL UNIQUE,
            title             TEXT NOT NULL DEFAULT '',
            duration_s        REAL NOT NULL DEFAULT 0,
            published_at      TIMESTAMPTZ,
            transcript_status TEXT NOT NULL DEFAULT 'pending',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_videos_channel_id ON videos(channel_id)")

    # --- transcripts (GLOBAL cache — one per video, shared across users) ---
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS transcripts (
            video_id     BIGINT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
            text         TEXT NOT NULL DEFAULT '',
            source       TEXT NOT NULL DEFAULT '',    -- 'captions' | 'whisper'
            language     TEXT NOT NULL DEFAULT 'en',
            fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # --- summaries (GLOBAL cache — sections 1-3, one set per video) ---
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS summaries (
            video_id     BIGINT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
            content      TEXT NOT NULL DEFAULT '',    -- sections 1-3 (insights, patterns, grading)
            model        TEXT NOT NULL DEFAULT '',
            generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # --- digests (PER-USER — section 4 tailored + assembled full brief) ---
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS digests (
            id               BIGSERIAL PRIMARY KEY,
            user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            video_id         BIGINT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            tailored_section TEXT NOT NULL DEFAULT '',  -- section 4 (user-scoped)
            full_brief       TEXT NOT NULL DEFAULT '',  -- assembled 1-4
            status           TEXT NOT NULL DEFAULT 'pending', -- pending|done|skipped|failed
            delivered_at     TIMESTAMPTZ,
            tokens_used      INTEGER NOT NULL DEFAULT 0,
            cost_usd         REAL NOT NULL DEFAULT 0,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (user_id, video_id)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_digests_user_id ON digests(user_id)")

    # --- usage_ledger (per-user daily cost/usage tracking) ---
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_ledger (
            id                BIGSERIAL PRIMARY KEY,
            user_id           BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            date              DATE NOT NULL,
            videos_processed  INTEGER NOT NULL DEFAULT 0,
            tokens_in         BIGINT NOT NULL DEFAULT 0,
            tokens_out        BIGINT NOT NULL DEFAULT 0,
            cost_usd          REAL NOT NULL DEFAULT 0,
            UNIQUE (user_id, date)
        )
        """
    )


def downgrade() -> None:
    """Drop the multi-tenant tables (keeps legacy processed_videos)."""
    op.execute("DROP TABLE IF EXISTS usage_ledger")
    op.execute("DROP TABLE IF EXISTS digests")
    op.execute("DROP TABLE IF EXISTS summaries")
    op.execute("DROP TABLE IF EXISTS transcripts")
    op.execute("DROP TABLE IF EXISTS videos")
    op.execute("DROP TABLE IF EXISTS subscriptions")
    op.execute("DROP TABLE IF EXISTS channels")
    op.execute("DROP TABLE IF EXISTS users")

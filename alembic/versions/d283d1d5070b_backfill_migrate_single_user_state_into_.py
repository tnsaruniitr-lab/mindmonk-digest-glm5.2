"""backfill: migrate single-user state into multi-tenant tables

Revision ID: d283d1d5070b
Revises: 38881c9c6cd5
Create Date: 2026-06-26 21:14:06.445771

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d283d1d5070b"
down_revision: Union[str, Sequence[str], None] = "38881c9c6cd5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Backfill the existing single-user state into the multi-tenant tables.

    Creates the first user (the operator, identified by TELEGRAM_CHAT_ID env),
    migrates their channel (Diary of a CEO) into `channels` + `subscriptions`,
    and migrates their processed videos into `videos` + `digests`.

    Idempotent: uses ON CONFLICT DO NOTHING, so re-running is safe.

    NOTE: the operator's telegram_chat_id comes from the TELEGRAM_CHAT_ID env
    var at migration time. If it's unset, the backfill is skipped (run again
    with it set).
    """
    import os

    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    user_id_val = os.getenv("TELEGRAM_USER_ID", chat_id)  # fall back to chat_id
    profile = os.getenv("PROFILE_YAML", "")

    if not chat_id:
        print("TELEGRAM_CHAT_ID not set; skipping backfill (re-run with it set)")
        return

    # 1. Create the operator user (idempotent).
    op.execute(
        f"""
        INSERT INTO users (telegram_chat_id, telegram_user_id, profile_yaml, tier)
        VALUES ('{chat_id}', '{user_id_val}', '{profile.replace(chr(39), chr(39) + chr(39))}', 'admin')
        ON CONFLICT (telegram_chat_id) DO NOTHING
        """
    )

    # 2. Migrate the channel: DOAC (from CONFIG_YAML at deploy time).
    #    The channel url is the known production value.
    op.execute(
        """
        INSERT INTO channels (name, url)
        VALUES ('Diary of a CEO', 'https://www.youtube.com/@TheDiaryOfACEO/videos')
        ON CONFLICT (url) DO NOTHING
        """
    )

    # 3. Subscribe the operator to the channel.
    op.execute(
        f"""
        INSERT INTO subscriptions (user_id, channel_id)
        SELECT u.id, c.id FROM users u, channels c
        WHERE u.telegram_chat_id = '{chat_id}'
          AND c.url = 'https://www.youtube.com/@TheDiaryOfACEO/videos'
        ON CONFLICT (user_id, channel_id) DO NOTHING
        """
    )

    # 4. Migrate processed videos into videos + digests.
    #    Each legacy row becomes: a video (global) + a digest for the operator.
    op.execute(
        """
        INSERT INTO videos (youtube_id, title, transcript_status, created_at)
        SELECT video_id, video_id, 'done', now()
        FROM processed_videos
        ON CONFLICT (youtube_id) DO NOTHING
        """
    )
    # Fill in published_at / duration from legacy where we have them
    # (legacy schema didn't track these separately; best-effort).

    # 5. Create digests for the operator from legacy processed_videos.
    op.execute(
        f"""
        INSERT INTO digests (user_id, video_id, full_brief, status, delivered_at, created_at)
        SELECT
            u.id,
            v.id,
            COALESCE(pv.summary, ''),
            CASE WHEN pv.status = 'done' THEN 'done'
                 WHEN pv.status = 'skipped' THEN 'skipped'
                 ELSE 'failed' END,
            CASE WHEN pv.status = 'done' THEN pv.processed_at::timestamptz ELSE NULL END,
            pv.processed_at::timestamptz
        FROM processed_videos pv
        JOIN users u ON u.telegram_chat_id = '{chat_id}'
        JOIN videos v ON v.youtube_id = pv.video_id
        ON CONFLICT (user_id, video_id) DO NOTHING
        """
    )

    print(f"Backfill complete for operator chat_id={chat_id}")


def downgrade() -> None:
    """Backfill is not reversible (it's a data migration, not a schema change)."""
    # Removing the backfilled rows would lose user data. Intentionally a no-op.
    pass

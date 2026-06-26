"""initial schema: processed_videos

Revision ID: ab61b1511739
Revises:
Create Date: 2026-06-26 21:05:55.516978

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "ab61b1511739"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the processed_videos table (matches the current production schema).

    Note: production already has this table (created via CREATE TABLE IF NOT EXISTS).
    This migration uses IF NOT EXISTS so it's safe to run against the existing DB —
    it won't error or duplicate. Future migrations should be additive.
    """
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_videos (
            video_id     TEXT PRIMARY KEY,
            channel_id   TEXT NOT NULL,
            status       TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            summary      TEXT,
            note         TEXT
        )
        """
    )


def downgrade() -> None:
    """Drop the processed_videos table."""
    op.execute("DROP TABLE IF EXISTS processed_videos")

"""Core domain dataclasses shared across modules.

Multi-tenant (Phase 1): the central concept is now per-user digests rather
than a global processed-video list. Videos, transcripts, and summaries are
video-scoped (global); digests are user-scoped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Channel:
    """A YouTube channel (global, shared across users)."""

    id: int | None
    name: str
    url: str
    last_polled_at: datetime | None = None


@dataclass(frozen=True)
class Video:
    """A YouTube video (global, shared across users)."""

    id: int | None  # DB id; None before insertion
    youtube_id: str  # the 11-char YouTube video id
    title: str
    url: str
    duration_seconds: float
    channel: Channel | None = None
    upload_date: datetime | None = None

    @property
    def is_long_form(self, threshold_seconds: float = 1200) -> bool:
        """True if the video meets the long-form duration threshold."""
        return self.duration_seconds >= threshold_seconds


@dataclass
class Transcript:
    """A fetched transcript with its provenance (video-scoped, global cache)."""

    video: Video
    text: str
    language: str
    is_generated: bool = False
    segments: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Legacy compat (used by the old Store backends; being phased out)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LegacyChannel:
    """A registered YouTube channel (legacy, pre-multi-tenant)."""

    name: str
    url: str


@dataclass(frozen=True)
class LegacyVideo:
    """A candidate video discovered on a channel (legacy)."""

    video_id: str
    title: str
    url: str
    duration_seconds: float
    channel: LegacyChannel
    upload_date: datetime | None = None

    @property
    def is_long_form(self, threshold_seconds: float = 1200) -> bool:
        return self.duration_seconds >= threshold_seconds


@dataclass
class ProcessedVideo:
    """A row from the legacy processed_videos table (being phased out)."""

    video_id: str
    channel_id: str
    status: str
    processed_at: datetime
    summary: str | None = None
    note: str | None = None

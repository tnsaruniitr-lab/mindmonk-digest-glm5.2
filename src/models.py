"""Core domain dataclasses shared across modules.

Multi-tenant (Phase 1, transitional): the codebase currently runs on the
"legacy" single-user models (LegacyChannel, LegacyVideo, ProcessedVideo).
The new multi-tenant models (Channel, Video) exist for the Phase 1+ rewrite
of the Store/Pipeline. Transcript currently holds a LegacyVideo; this will
change to the new Video once the pipeline refactor (Phase 1.4-1.5) lands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


# --------------------------------------------------------------------------- #
# Legacy models — used by the running single-user code path (being phased out)
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
class Transcript:
    """A fetched transcript with its provenance.

    Currently holds a LegacyVideo (the running pipeline's shape). Will move
    to the new Video once the multi-tenant pipeline lands.
    """

    video: LegacyVideo
    text: str
    language: str
    is_generated: bool = False
    segments: list = field(default_factory=list)


@dataclass
class ProcessedVideo:
    """A row from the legacy processed_videos table (being phased out)."""

    video_id: str
    channel_id: str
    status: str
    processed_at: datetime
    summary: str | None = None
    note: str | None = None


# --------------------------------------------------------------------------- #
# New multi-tenant models (Phase 1+ — for the Store/Pipeline rewrite)
# --------------------------------------------------------------------------- #


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

    id: int | None
    youtube_id: str
    title: str
    url: str
    duration_seconds: float
    channel: Channel | None = None
    upload_date: datetime | None = None

    @property
    def is_long_form(self, threshold_seconds: float = 1200) -> bool:
        return self.duration_seconds >= threshold_seconds

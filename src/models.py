"""Core domain dataclasses shared across modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Channel:
    """A registered YouTube channel."""

    name: str
    url: str


@dataclass(frozen=True)
class Video:
    """A candidate video discovered on a channel."""

    video_id: str
    title: str
    url: str
    duration_seconds: float  # 0 if unknown
    channel: Channel
    upload_date: datetime | None = None  # may be unavailable

    @property
    def is_long_form(self, threshold_seconds: float = 1200) -> bool:
        """True if the video meets the long-form duration threshold."""
        return self.duration_seconds >= threshold_seconds


@dataclass
class Transcript:
    """A fetched transcript with its provenance."""

    video: Video
    text: str
    language: str  # actual language code of the returned text
    is_generated: bool = False  # auto-generated vs. manually authored
    segments: list[dict] = field(default_factory=list)


@dataclass
class ProcessedVideo:
    """A row from the processed-videos store."""

    video_id: str
    channel_id: str
    status: str  # "done" | "skipped" | "failed"
    processed_at: datetime
    summary: str | None = None
    note: str | None = None

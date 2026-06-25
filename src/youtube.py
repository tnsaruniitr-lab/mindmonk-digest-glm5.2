"""YouTube channel polling via yt-dlp.

Uses yt-dlp in flat-playlist mode to list a channel's recent uploads with
metadata (id, title, duration, upload date) WITHOUT downloading anything.
This avoids the need for a YouTube Data API key.

Exports:
    - poll_channel(channel, lookback_days) -> list[Video]
    - is_long_form(video, min_duration_seconds) -> bool
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from yt_dlp import YoutubeDL

from .models import Channel, Video

log = logging.getLogger(__name__)

# Flat-playlist extraction options: metadata only, no download.
_EXTRACT_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": "in_playlist",
    # Pull per-entry fields that flat mode can provide without extra requests.
    "playlist_items": "1-60",  # last ~60 uploads is plenty for each poll
}


def poll_channel(
    channel: Channel, lookback_days: int = 14
) -> list[Video]:
    """List recent uploads from a channel, newest entries first.

    Only videos uploaded within ``lookback_days`` are returned, which keeps
    the first run (with no state yet) from processing the entire back-catalog.
    Subsequent runs are also filtered by the store's dedup.

    Raises ``YouTubePollError`` if extraction fails outright so the caller can
    log and continue to the next channel.
    """
    opts = dict(_EXTRACT_OPTS)
    with YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(channel.url, download=False)
        except Exception as exc:  # yt-dlp raises various DownloadError types
            raise YouTubePollError(
                f"Failed to poll channel {channel.name!r} ({channel.url}): {exc}"
            ) from exc

    entries = _entries(info)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
    videos: list[Video] = []
    for entry in entries:
        video = _entry_to_video(entry, channel)
        if video is None:
            continue
        if video.upload_date and video.upload_date < cutoff:
            continue
        videos.append(video)
    log.info(
        "Polled %s: %d video(s) within %d day(s)",
        channel.name,
        len(videos),
        lookback_days,
    )
    return videos


def is_long_form(video: Video, min_duration_seconds: float) -> bool:
    """Apply the long-form duration filter.

    Treats unknown duration (0/None) conservatively by including the video,
    so it gets a chance to fetch a transcript rather than being silently
    dropped. Shorts and clips are rejected when duration is known.
    """
    if not video.duration_seconds or video.duration_seconds <= 0:
        return True  # unknown — let downstream decide
    return video.duration_seconds >= min_duration_seconds


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _entries(info: dict) -> list[dict]:
    """Pull the entry list from an extraction result (handles nested playlists)."""
    if not info:
        return []
    entries = info.get("entries")
    if entries is None:
        # Single video instead of a playlist — unusual for a channel URL.
        return [info] if info.get("id") else []
    # Some channel URLs resolve to a nested playlist (e.g. /videos tab).
    flat: list[dict] = []
    for entry in entries:
        if entry is None:
            continue
        if isinstance(entry, dict) and entry.get("_type") in (
            "playlist",
            "multi_video",
        ) and entry.get("entries"):
            flat.extend(e for e in entry["entries"] if e)
        else:
            flat.append(entry)
    return flat


def _entry_to_video(entry: dict, channel: Channel) -> Video | None:
    """Convert a yt-dlp flat entry into a Video, skipping unusable entries."""
    video_id = entry.get("id")
    title = entry.get("title") or "(untitled)"
    if not video_id:
        return None

    # Skip upcoming/premieres/live that have no real content yet.
    live_status = entry.get("live_status") or ""
    if live_status in ("is_upcoming", "is_live"):
        return None

    duration = float(entry.get("duration") or 0)
    upload_date = _parse_upload_date(entry.get("upload_date"))

    # yt-dlp sometimes puts just the id, or a full URL, in `url`.
    raw_url = entry.get("url") or ""
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        url = raw_url
    else:
        url = f"https://www.youtube.com/watch?v={video_id}"

    return Video(
        video_id=video_id,
        title=title,
        url=url,
        duration_seconds=duration,
        channel=channel,
        upload_date=upload_date,
    )


def _parse_upload_date(value: str | None) -> datetime | None:
    """yt-dlp returns upload_date as YYYYMMDD string."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class YouTubePollError(Exception):
    """Raised when channel polling fails."""

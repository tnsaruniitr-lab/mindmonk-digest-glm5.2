"""YouTube channel polling via yt-dlp.

Uses yt-dlp in flat-playlist mode to list a channel's recent uploads with
metadata (id, title, duration, upload date) WITHOUT downloading anything.
This avoids the need for a YouTube Data API key.

Exports:
    - poll_channel(channel, lookback_days) -> list[LegacyVideo]
    - is_long_form(video, min_duration_seconds) -> bool
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from yt_dlp import YoutubeDL

from .models import LegacyChannel, LegacyVideo

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


def _with_proxy(opts: dict, proxy: str | None = None) -> dict:
    """Return a copy of opts with the proxy applied if one is set."""
    if proxy:
        return {**opts, "proxy": proxy}
    return dict(opts)


def _looks_like_botwall(exc: Exception) -> bool:
    """True if an exception looks like YouTube's 'confirm you're not a bot' wall."""
    msg = str(exc).lower()
    return any(
        s in msg
        for s in (
            "sign in to confirm",
            "not a bot",
            "bot check",
            "captcha",
            "confirm your age",
            "429 too many requests",
        )
    )


def _extract_resilient(
    ydl_opts: dict, target: str, proxy: str | None = None
) -> dict[str, Any]:
    """Run yt-dlp extract_info with a bot-wall retry strategy.

    If the first attempt hits YouTube's bot wall, retry through a sequence
    of player clients (ios, web_safari, mweb, tv) which sometimes dodge it.
    If a proxy is set and the direct request was blocked, the proxy is
    already in ydl_opts; if no proxy was set, we still try client rotation.
    Raises the last error if all attempts fail.
    """
    if proxy:
        log.info("yt-dlp via proxy (%s) for %s", proxy.split("@")[-1], target)
    else:
        log.info("yt-dlp direct (no proxy) for %s", target)

    last_exc: Exception | None = None
    # First attempt: the supplied opts (which may already include proxy).
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info: dict[str, Any] = ydl.extract_info(target, download=False)
            return info
    except Exception as exc:
        if not _looks_like_botwall(exc):
            raise
        last_exc = exc
        log.warning("Bot wall hit on %s; rotating player clients...", target)

    # Retry with alternate player clients.
    base = {k: v for k, v in ydl_opts.items() if k != "extractor_args"}
    for client in ("ios", "web_safari", "mweb", "tv", "tv_embedded"):
        opts = _with_proxy(base, proxy)
        opts["extractor_args"] = {"youtube": {"player_client": [client]}}
        try:
            with YoutubeDL(opts) as ydl:
                recovered: dict[str, Any] = ydl.extract_info(target, download=False)
                log.info("Recovered via player_client=%s", client)
                return recovered
        except Exception as exc:
            last_exc = exc
            if not _looks_like_botwall(exc):
                raise
            log.warning("player_client=%s also blocked for %s", client, target)
    assert last_exc is not None
    raise last_exc


def poll_channel(
    channel: LegacyChannel, lookback_days: int = 14, proxy: str | None = None
) -> list[LegacyVideo]:
    """List recent uploads from a channel, newest entries first.

    Only videos uploaded within ``lookback_days`` are returned, which keeps
    the first run (with no state yet) from processing the entire back-catalog.
    Subsequent runs are also filtered by the store's dedup.

    Raises ``YouTubePollError`` if extraction fails outright so the caller can
    log and continue to the next channel.
    """
    opts = _with_proxy(_EXTRACT_OPTS, proxy)
    with YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(channel.url, download=False)
        except Exception as exc:  # yt-dlp raises various DownloadError types
            raise YouTubePollError(
                f"Failed to poll channel {channel.name!r} ({channel.url}): {exc}"
            ) from exc

    entries = _entries(info)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
    videos: list[LegacyVideo] = []
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


def is_long_form(video: LegacyVideo, min_duration_seconds: float) -> bool:
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
        if (
            isinstance(entry, dict)
            and entry.get("_type")
            in (
                "playlist",
                "multi_video",
            )
            and entry.get("entries")
        ):
            flat.extend(e for e in entry["entries"] if e)
        else:
            flat.append(entry)
    return flat


def _entry_to_video(entry: dict, channel: LegacyChannel) -> LegacyVideo | None:
    """Convert a yt-dlp flat entry into a LegacyVideo, skipping unusable entries."""
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

    return LegacyVideo(
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


class YouTubeResolveError(Exception):
    """Raised when an on-demand video/channel resolution fails."""


# --------------------------------------------------------------------------- #
# On-demand resolution (for /fetch and /channel bot commands)
# --------------------------------------------------------------------------- #
def get_video(url: str, proxy: str | None = None) -> LegacyVideo:
    """Resolve a single video URL to a LegacyVideo (with metadata, no download).

    Accepts youtu.be/<id>, watch?v=<id>, or embed/<id>. Used by /fetch.
    Retries with alternate player clients if YouTube's bot wall is hit.
    """
    opts = _with_proxy(
        {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        },
        proxy,
    )
    try:
        info = _extract_resilient(opts, url, proxy)
    except Exception as exc:  # noqa: BLE001
        raise YouTubeResolveError(f"Could not resolve video {url!r}: {exc}") from exc

    if not info or not info.get("id"):
        raise YouTubeResolveError(f"No video found at {url!r}")

    video_id = info["id"]
    title = info.get("title") or "(untitled)"
    if info.get("live_status") in ("is_upcoming", "is_live"):
        raise YouTubeResolveError("That video is live or upcoming — no transcript yet.")

    channel = LegacyChannel(
        name=info.get("channel") or info.get("uploader") or "YouTube",
        url=info.get("channel_url") or info.get("uploader_url") or "",
    )
    return LegacyVideo(
        video_id=video_id,
        title=title,
        url=f"https://www.youtube.com/watch?v={video_id}",
        duration_seconds=float(info.get("duration") or 0),
        channel=channel,
        upload_date=_parse_upload_date(info.get("upload_date")),
    )


def get_latest_video(channel_url: str, proxy: str | None = None) -> LegacyVideo:
    """Resolve a channel URL to its most recent uploaded video. Used by /channel.

    Polls the channel's uploads and returns the newest one (skipping
    live/upcoming). Accepts @handle, /channel/, /user/, /c/, or /videos URLs.
    """
    normalized = _ensure_videos_tab(channel_url)
    channel = LegacyChannel(name=_derive_channel_name(channel_url), url=normalized)
    videos = poll_channel(
        channel, lookback_days=365, proxy=proxy
    )  # broad; we want the latest
    if not videos:
        raise YouTubeResolveError(
            f"No uploaded videos found at {channel_url!r}. "
            "Check the URL — is it a channel with public uploads?"
        )
    # poll_channel returns newest-first already; but sort defensively by date.
    latest = max(
        videos,
        key=lambda v: v.upload_date or datetime.min.replace(tzinfo=timezone.utc),
    )
    return latest


# --------------------------------------------------------------------------- #
# URL normalization helpers
# --------------------------------------------------------------------------- #
def _ensure_videos_tab(url: str) -> str:
    """Make sure a channel URL points to its /videos tab for clean polling."""
    url = url.strip()
    if "youtube.com" not in url and "youtu.be" not in url:
        return url
    if url.endswith("/videos"):
        return url
    return url.rstrip("/") + "/videos"


def _derive_channel_name(url: str) -> str:
    """Best-effort friendly name from a channel URL."""
    import re

    m = re.search(r"@([\w.\-]+)", url)
    if m:
        return m.group(1).replace("-", " ").replace("_", " ").title()
    m = re.search(r"/(?:channel|user|c)/([^/?]+)", url)
    if m:
        return m.group(1).replace("-", " ").title()
    return "Channel"

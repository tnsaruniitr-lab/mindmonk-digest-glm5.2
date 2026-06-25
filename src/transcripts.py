"""Transcript fetching via youtube-transcript-api.

Targets the v1.x API where ``YouTubeTranscriptApi.fetch(video_id, languages)``
returns a ``FetchedTranscript`` directly, and ``.list(video_id)`` returns a
``TranscriptList`` for the manual→generated→translate fallback chain.

Strategy (first success wins):
  1. ``fetch()`` with preferred languages (handles manual + generated).
  2. ``list()`` → translate any translatable transcript to the preferred lang.
  3. Raise ``NoTranscriptError`` if nothing exists / captions disabled.
"""
from __future__ import annotations

import logging

from youtube_transcript_api import (
    NoTranscriptFound,
    TranscriptsDisabled,
    YouTubeTranscriptApi,
)

from .models import Transcript, Video

log = logging.getLogger(__name__)


def get_transcript(
    video: Video, languages: list[str] | None = None
) -> Transcript:
    """Fetch the best available transcript for a video.

    Args:
        video: the video to fetch for.
        languages: ordered preferred language codes, e.g. ["en", "en-US"].
    """
    languages = languages or ["en"]
    api = YouTubeTranscriptApi()

    # 1. Direct fetch — finds manual or generated in preferred languages.
    try:
        fetched = api.fetch(video.video_id, languages=languages)
        return _to_transcript(video, fetched, languages[0], is_generated=False)
    except NoTranscriptFound:
        log.info(
            "No caption in %s for %s; trying translation fallback",
            languages, video.video_id,
        )
    except TranscriptsDisabled:
        raise NoTranscriptError(video.video_id)

    # 2. Fallback: list all transcripts, translate any to the preferred lang.
    try:
        transcript_list = api.list(video.video_id)
    except (TranscriptsDisabled, NoTranscriptFound):
        raise NoTranscriptError(video.video_id)

    for tr in transcript_list:
        if tr.is_translatable:
            translated = tr.translate(languages[0]).fetch()
            return _to_transcript(
                video, translated, languages[0], is_generated=True
            )

    raise NoTranscriptError(video.video_id)


def _to_transcript(
    video: Video,
    fetched,
    language: str,
    is_generated: bool,
) -> Transcript:
    """Build a Transcript from a FetchedTranscript (list of snippets)."""
    snippets = list(fetched)
    text = "\n".join(
        getattr(s, "text", "").strip() for s in snippets if getattr(s, "text", "")
    )
    lang = getattr(fetched, "language", language) or language
    log.info(
        "Fetched transcript for %s (%d chars, %s)",
        video.video_id,
        len(text),
        "generated" if is_generated else "manual",
    )
    return Transcript(
        video=video,
        text=text,
        language=lang,
        is_generated=is_generated,
        segments=snippets,
    )


class NoTranscriptError(Exception):
    """No transcript is available for this video."""

    def __init__(self, video_id: str):
        super().__init__(f"No transcript available for video {video_id}")
        self.video_id = video_id

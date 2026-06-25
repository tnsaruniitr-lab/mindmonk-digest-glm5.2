"""Transcript fetching via youtube-transcript-api.

Tries the user's preferred languages in order, then falls back to any
available transcript (including auto-generated). Returns the plain text
concatenated from segments; segments are kept on the Transcript object
for future use (e.g. timestamps).
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

    Strategy (first success wins):
      1. Manual caption in any preferred language.
      2. Auto-generated caption in any preferred language.
      3. Any auto-generated caption, translated to the first preferred lang.
      4. Raise ``NoTranscriptError`` if nothing exists / captions disabled.

    Args:
        video: the video to fetch for.
        languages: ordered preferred language codes, e.g. ["en", "en-US"].
    """
    languages = languages or ["en"]
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video.video_id)
    except TranscriptsDisabled as exc:
        raise NoTranscriptError(video.video_id) from exc

    # 1 + 2: prefer manual, then generated, in the preferred languages.
    try:
        transcript_obj = transcript_list.find_manually_created_transcript(
            languages
        )
        is_generated = False
    except NoTranscriptFound:
        try:
            transcript_obj = transcript_list.find_generated_transcript(
                languages
            )
            is_generated = True
        except NoTranscriptFound:
            # 3: translate any available transcript to the preferred language.
            any_transcript = next(iter(transcript_list), None)
            if any_transcript is None or not any_transcript.is_translatable:
                raise NoTranscriptError(video.video_id)
            transcript_obj = any_transcript.translate(languages[0])
            is_generated = True

    fetched = transcript_obj.fetch()
    lang = getattr(transcript_obj, "language_code", languages[0])

    text = "\n".join(
        snippet.text.strip() for snippet in fetched if snippet.text.strip()
    )
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
        segments=fetched if isinstance(fetched, list) else [],
    )


class NoTranscriptError(Exception):
    """No transcript is available for this video."""

    def __init__(self, video_id: str):
        super().__init__(f"No transcript available for video {video_id}")
        self.video_id = video_id

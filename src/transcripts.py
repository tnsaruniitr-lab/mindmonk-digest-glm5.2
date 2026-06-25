"""Transcript fetching via yt-dlp.

We use yt-dlp (not youtube-transcript-api) because yt-dlp fetches captions
through YouTube's player API, which is far more robust against the IP-blocking
that hits the watch-page HTML scraping that youtube-transcript-api does. On
cloud hosts (Railway, AWS, etc.), youtube-transcript-api raises ``IpBlocked``;
yt-dlp succeeds because it uses different client endpoints (web/android/ios).

Strategy:
  1. yt-dlp with --write-subs --write-auto-subs --skip-download into a tempdir.
  2. Prefer manually-authored subs; fall back to auto-generated.
  3. Parse the resulting VTT/SRT/JSON3 file into plain text.
"""
from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

from yt_dlp import YoutubeDL

from .models import Transcript, Video

log = logging.getLogger(__name__)


def get_transcript(
    video: Video,
    languages: list[str] | None = None,
    groq_api_key: str | None = None,
    proxy: str | None = None,
) -> Transcript:
    """Fetch the best available transcript for a video.

    Cascade:
      1. yt-dlp caption extraction (free, instant) — preferred.
      2. If no captions AND groq_api_key is set → Groq Whisper transcription.
      3. Else raise NoTranscriptError.

    Args:
        video: the video to fetch for.
        languages: ordered preferred language codes, e.g. ["en"].
        groq_api_key: optional Groq key enabling the Whisper fallback.
    """
    languages = languages or ["en"]

    # 1. Try captions first.
    try:
        return _get_captions(video, languages, proxy)
    except NoTranscriptError:
        if not groq_api_key:
            raise
        log.info(
            "No captions for %s; falling back to Groq Whisper transcription",
            video.video_id,
        )

    # 2. Fallback: Groq Whisper (audio → text).
    from .transcribe import transcribe_via_groq, TranscribeError
    try:
        return transcribe_via_groq(video, groq_api_key, languages, proxy)
    except TranscribeError as exc:
        log.error("Groq transcription failed for %s: %s", video.video_id, exc)
        raise NoTranscriptError(video.video_id) from exc


def _get_captions(
    video: Video, languages: list[str], proxy: str | None = None
) -> Transcript:
    """Caption extraction via yt-dlp (the original method)."""
    sub_langs = ",".join(languages)

    with tempfile.TemporaryDirectory() as tmpdir:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "writesubtitles": True,        # manual captions
            "writeautomaticsub": True,     # auto-generated fallback
            "subtitleslangs": [sub_langs],
            "subtitlesformat": "vtt/srt/best",
            "outtmpl": str(Path(tmpdir) / "%(id)s"),
            # Use the android client — most reliable against IP blocks.
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        }
        if proxy:
            opts["proxy"] = proxy
        with YoutubeDL(opts) as ydl:
            try:
                ydl.download([video.video_id])
            except Exception as exc:  # noqa: BLE001
                raise NoTranscriptError(video.video_id) from exc

        # Find the written subtitle file. Prefer manual (lang.vtt) over
        # auto-generated (lang.*.vtt / has 'auto').
        files = sorted(Path(tmpdir).glob(f"{video.video_id}*.vtt")) + \
                sorted(Path(tmpdir).glob(f"{video.video_id}*.srt"))
        if not files:
            raise NoTranscriptError(video.video_id)

        # Manual subs are named "<id>.<lang>.vtt"; auto are "<id>.<lang>.<kind>.vtt".
        manual = [f for f in files if f.stem.count(".") == 1]
        is_generated = not manual
        sub_file = (manual[0] if manual else files[0])
        text, lang = _parse_subtitle(sub_file, languages[0])

    if not text.strip():
        raise NoTranscriptError(video.video_id)

    log.info(
        "Fetched transcript for %s (%d chars, %s) via yt-dlp",
        video.video_id,
        len(text),
        "generated" if is_generated else "manual",
    )
    return Transcript(
        video=video,
        text=text,
        language=lang,
        is_generated=is_generated,
        segments=[],
    )


# --------------------------------------------------------------------------- #
# Subtitle parsing
# --------------------------------------------------------------------------- #
def _parse_subtitle(path: Path, default_lang: str) -> tuple[str, str]:
    """Parse a VTT or SRT subtitle file into plain text + language code."""
    raw = path.read_text(encoding="utf-8", errors="ignore")
    # Language is the second dot-segment of the filename: <id>.<lang>.<kind?>
    stem_parts = path.stem.split(".")
    lang = stem_parts[1] if len(stem_parts) > 1 else default_lang

    if path.suffix == ".vtt":
        text = _parse_vtt(raw)
    else:
        text = _parse_srt(raw)
    return text, lang


def _parse_vtt(raw: str) -> str:
    """Extract plain text from WebVTT, dropping cues/timestamps/tags."""
    lines = []
    for block in raw.split("\n\n"):
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("WEBVTT") or line.startswith("NOTE"):
                continue
            # Skip cue identifiers and timestamp lines.
            if "-->" in line or re.fullmatch(r"[\d.a-zA-Z-]+", line):
                continue
            # Strip inline tags like <c.colorE5E5E5> ... </c>.
            clean = re.sub(r"<[^>]+>", "", line)
            if clean:
                lines.append(clean)
    # Deduplicate consecutive identical lines (VTT repeats per cue).
    deduped = []
    for ln in lines:
        if not deduped or deduped[-1] != ln:
            deduped.append(ln)
    return " ".join(deduped)


def _parse_srt(raw: str) -> str:
    """Extract plain text from SubRip (SRT)."""
    lines = []
    for block in raw.split("\n\n"):
        parts = block.splitlines()
        for line in parts[2:]:  # skip index + timestamp
            clean = re.sub(r"<[^>]+>", "", line.strip())
            if clean and "-->" not in clean:
                lines.append(clean)
    deduped = []
    for ln in lines:
        if not deduped or deduped[-1] != ln:
            deduped.append(ln)
    return " ".join(deduped)


class NoTranscriptError(Exception):
    """No transcript is available for this video."""

    def __init__(self, video_id: str):
        super().__init__(f"No transcript available for video {video_id}")
        self.video_id = video_id

"""Transcript waterfall with explicit per-step logging.

Primary path: audio download (via proxy) → OpenAI Whisper transcription.
Fallback: yt-dlp caption extraction (free, instant) — used only if Whisper
is unavailable (no key set) or the audio path fails.

Why Whisper-primary: it's the most reliable, universal path — works for every
video with playable audio, regardless of whether captions exist. Captions are
kept as a free fallback for the common case where they're available.

Each step logs its attempt and outcome explicitly so the waterfall is fully
observable in production logs.
"""

from __future__ import annotations

import logging
from pathlib import Path

from yt_dlp import YoutubeDL

from .models import LegacyVideo as Video, Transcript

log = logging.getLogger(__name__)


def get_transcript(
    video: Video,
    languages: list[str] | None = None,
    openai_api_key: str | None = None,
    proxy: str | None = None,
) -> Transcript:
    """Fetch a transcript via the waterfall. Logs each step.

    Waterfall:
      STEP 1 — OpenAI Whisper (audio download via proxy → whisper-1).
               Primary. Most reliable. ~$0.006/min. Needs openai_api_key.
      STEP 2 — yt-dlp captions (free, instant).
               Fallback if Whisper unavailable or fails.

    Raises NoTranscriptError if BOTH steps fail.
    """
    languages = languages or ["en"]
    log.info("=" * 60)
    log.info("TRANSCRIPT WATERFALL for %s (%s)", video.video_id, video.title[:50])
    log.info("=" * 60)

    # ------------------------------------------------------------------ #
    # STEP 1: OpenAI Whisper (primary)
    # ------------------------------------------------------------------ #
    if openai_api_key:
        log.info("[STEP 1/2] Attempting OpenAI Whisper (audio → whisper-1)")
        try:
            from .transcribe import transcribe_via_openai, TranscribeError

            transcript = transcribe_via_openai(video, openai_api_key, languages, proxy)
            log.info(
                "[STEP 1/2] ✅ SUCCESS via OpenAI Whisper: %d chars",
                len(transcript.text),
            )
            log.info("=" * 60)
            return transcript
        except TranscribeError as exc:
            log.warning("[STEP 1/2] ❌ Whisper failed: %s", str(exc)[:150])
            log.warning("[STEP 1/2] Falling through to STEP 2 (captions)")
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[STEP 1/2] ❌ Unexpected Whisper error: %s: %s",
                type(exc).__name__,
                str(exc)[:150],
            )
            log.warning("[STEP 1/2] Falling through to STEP 2 (captions)")
    else:
        log.info("[STEP 1/2] SKIP — OPENAI_TRANSCRIBE_KEY not set")

    # ------------------------------------------------------------------ #
    # STEP 2: yt-dlp captions (fallback)
    # ------------------------------------------------------------------ #
    log.info("[STEP 2/2] Attempting yt-dlp captions")
    try:
        transcript = _get_captions(video, languages, proxy)
        log.info("[STEP 2/2] ✅ SUCCESS via captions: %d chars", len(transcript.text))
        log.info("=" * 60)
        return transcript
    except NoTranscriptError:
        log.error("[STEP 2/2] ❌ No captions available either")
        log.error(
            "WATERFALL EXHAUSTED — both Whisper and captions failed for %s",
            video.video_id,
        )
        log.info("=" * 60)
        raise


def _get_captions(
    video: Video, languages: list[str], proxy: str | None = None
) -> Transcript:
    """Caption extraction via yt-dlp."""
    import tempfile

    sub_langs = ",".join(languages)
    with tempfile.TemporaryDirectory() as tmpdir:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": [sub_langs],
            "subtitlesformat": "vtt/srt/best",
            "outtmpl": str(Path(tmpdir) / "%(id)s"),
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        }
        if proxy:
            opts["proxy"] = proxy
        with YoutubeDL(opts) as ydl:
            try:
                ydl.download([video.video_id])
            except Exception as exc:  # noqa: BLE001
                raise NoTranscriptError(video.video_id) from exc

        files = sorted(Path(tmpdir).glob(f"{video.video_id}*.vtt")) + sorted(
            Path(tmpdir).glob(f"{video.video_id}*.srt")
        )
        if not files:
            raise NoTranscriptError(video.video_id)

        manual = [f for f in files if f.stem.count(".") == 1]
        is_generated = not manual
        sub_file = manual[0] if manual else files[0]
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
    stem_parts = path.stem.split(".")
    lang = stem_parts[1] if len(stem_parts) > 1 else default_lang

    if path.suffix == ".vtt":
        text = _parse_vtt(raw)
    else:
        text = _parse_srt(raw)
    return text, lang


def _parse_vtt(raw: str) -> str:
    import re

    lines = []
    for block in raw.split("\n\n"):
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("WEBVTT") or line.startswith("NOTE"):
                continue
            if "-->" in line or re.fullmatch(r"[\d.a-zA-Z-]+", line):
                continue
            clean = re.sub(r"<[^>]+>", "", line)
            if clean:
                lines.append(clean)
    deduped: list[str] = []
    for ln in lines:
        if not deduped or deduped[-1] != ln:
            deduped.append(ln)
    return " ".join(deduped)


def _parse_srt(raw: str) -> str:
    import re

    lines = []
    for block in raw.split("\n\n"):
        parts = block.splitlines()
        for line in parts[2:]:
            clean = re.sub(r"<[^>]+>", "", line.strip())
            if clean and "-->" not in clean:
                lines.append(clean)
    deduped: list[str] = []
    for ln in lines:
        if not deduped or deduped[-1] != ln:
            deduped.append(ln)
    return " ".join(deduped)


class NoTranscriptError(Exception):
    """No transcript is available for this video."""

    def __init__(self, video_id: str):
        super().__init__(f"No transcript available for video {video_id}")
        self.video_id = video_id

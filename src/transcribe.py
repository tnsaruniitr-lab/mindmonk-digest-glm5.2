"""Audio-based transcription fallback via Groq's Whisper API.

Used when yt-dlp caption extraction fails (no captions, IP-blocked, etc.).
This module downloads the video's audio with yt-dlp, splits it into ≤24MB
chunks with ffmpeg (Groq's hard limit is 25MB/file), transcribes each chunk
via Groq's Whisper large-v3 endpoint, and concatenates the text.

Flow:
    video → yt-dlp audio (mp3, 32kbps mono) → ffmpeg chunk → Groq × N → text

Cost: ~$0.17/hr of audio on Groq. A 2h podcast ≈ $0.34, split into ~2 chunks.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

import requests
from yt_dlp import YoutubeDL

from .models import Transcript, Video

log = logging.getLogger(__name__)

GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3"
# Groq limit is 25MB; keep chunks under with margin.
MAX_CHUNK_BYTES = 24 * 1024 * 1024  # 24 MiB
# 32kbps mono produces ~4MB per 15min — so ~90min per 24MB chunk.
AUDIO_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "format": "bestaudio/best",
    "postextractor_args": {"ffmpeg": []},
    "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    "noplaylist": True,
}


def transcribe_via_groq(
    video: Video,
    api_key: str,
    languages: list[str] | None = None,
) -> Transcript:
    """Download audio, transcribe via Groq Whisper, return a Transcript.

    Raises ``TranscribeError`` on any failure (download, chunk, API).
    """
    languages = languages or ["en"]
    if not api_key:
        raise TranscribeError("GROQ_API_KEY is not set.")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        # 1. Download audio as a single low-bitrate mp3.
        audio_path = _download_audio(video.video_id, tmpdir)
        size = audio_path.stat().st_size
        log.info(
            "Downloaded audio for %s: %.1f MB", video.video_id, size / 1e6
        )

        # 2. Split into chunks under Groq's limit (if needed).
        chunks = _chunk_audio(audio_path, tmpdir)
        log.info(
            "Transcribing %s in %d chunk(s)", video.video_id, len(chunks)
        )

        # 3. Transcribe each chunk via Groq.
        texts: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            text = _transcribe_file(chunk, api_key, languages[0])
            texts.append(text)
            log.info(
                "Chunk %d/%d done (%d chars)", i, len(chunks), len(text)
            )

    full_text = " ".join(t.strip() for t in texts if t.strip())
    if not full_text:
        raise TranscribeError("Groq returned empty transcript.")

    log.info(
        "Groq transcription complete for %s: %d chars",
        video.video_id,
        len(full_text),
    )
    return Transcript(
        video=video,
        text=full_text,
        language=languages[0],
        is_generated=True,
        segments=[],
    )


# --------------------------------------------------------------------------- #
def _download_audio(video_id: str, outdir: Path) -> Path:
    """Download audio as a low-bitrate mp3 (mono, 32kbps) to keep size down."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    outtmpl = str(outdir / "audio.%(ext)s")
    opts = dict(AUDIO_OPTS)
    opts["outtmpl"] = outtmpl
    # Re-encode to mp3 32k mono during download — smallest viable quality for STT.
    opts["postextractor_args"] = {"ffmpeg": ["-ac", "1", "-b:a", "32k"]}
    opts["postprocessors"] = [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "32",
    }]
    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001
        raise TranscribeError(f"Audio download failed: {exc}") from exc

    files = list(outdir.glob("audio.*"))
    if not files:
        raise TranscribeError("Audio download produced no file.")
    return files[0]


def _chunk_audio(audio_path: Path, outdir: Path) -> list[Path]:
    """Split audio into ≤24MB chunks. If already small, return as-is."""
    if audio_path.stat().st_size <= MAX_CHUNK_BYTES:
        return [audio_path]

    # Estimate chunk duration: total_duration * (24MB / total_size) * margin.
    duration = _probe_duration(audio_path)
    if not duration:
        raise TranscribeError(
            "Could not determine audio duration for chunking."
        )
    size = audio_path.stat().st_size
    # seconds per chunk, with 10% safety margin
    chunk_dur = duration * (MAX_CHUNK_BYTES / size) * 0.9
    n_chunks = max(1, int(duration // chunk_dur) + 1)

    chunks: list[Path] = []
    for i in range(n_chunks):
        start = i * chunk_dur
        out = outdir / f"chunk_{i:03d}.mp3"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(audio_path),
            "-ss", str(start), "-t", str(chunk_dur),
            "-acodec", "copy", str(out),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        chunks.append(out)
    return chunks


def _probe_duration(path: Path) -> float | None:
    """Get audio duration in seconds via ffprobe."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, text=True, check=True,
        )
        return float(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return None


def _transcribe_file(path: Path, api_key: str, language: str) -> str:
    """Send one audio file to Groq Whisper, return the transcript text."""
    headers = {"Authorization": f"Bearer {api_key}"}
    # Retry transient failures with backoff.
    for attempt in range(1, 4):
        try:
            with path.open("rb") as fh:
                resp = requests.post(
                    GROQ_STT_URL,
                    headers=headers,
                    files={"file": (path.name, fh, "audio/mpeg")},
                    data={
                        "model": GROQ_MODEL,
                        "response_format": "json",
                        "language": language,
                        "temperature": "0",
                    },
                    timeout=600,  # large chunks can take a while
                )
            if resp.status_code == 200:
                return resp.json().get("text", "")
            if resp.status_code == 429:
                wait = 5 * attempt
                log.warning("Groq 429, waiting %ds (attempt %d)", wait, attempt)
                time.sleep(wait)
                continue
            raise TranscribeError(
                f"Groq API error {resp.status_code}: {resp.text[:200]}"
            )
        except requests.RequestException as exc:
            if attempt == 3:
                raise TranscribeError(f"Groq request failed: {exc}") from exc
            time.sleep(3 * attempt)
    raise TranscribeError("Groq transcription failed after retries.")


class TranscribeError(Exception):
    """Raised when audio-based transcription fails."""

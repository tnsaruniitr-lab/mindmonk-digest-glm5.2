"""Pipeline orchestration: poll → filter → transcript → summarize → deliver.

One ``run_cycle`` polls all channels and processes every new long-form video.
Failures are isolated per video/channel — one bad transcript never stops the
rest of the cycle.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from config.settings import Settings
from .models import Channel, Video
from .store import Store
from . import transcripts, youtube
from .summarizer import LLMError, Summarizer
from .telegram import TelegramError, TelegramSender

log = logging.getLogger(__name__)


@dataclass
class CycleStats:
    polled: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return (
            f"polled={self.polled} processed={self.processed} "
            f"skipped={self.skipped} failed={self.failed}"
        )


class Pipeline:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.db_path)
        self.summarizer = None
        try:
            self.summarizer = Summarizer(settings.llm)
        except LLMError:
            # No/placeholder LLM key yet. Worker still starts and polls, but
            # skips summarization. See run_cycle() for the user notice.
            self._llm_configured = False
        else:
            self._llm_configured = True
        self.sender = TelegramSender(
            settings.telegram.bot_token, settings.telegram.chat_id
        )

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------ #
    def run_cycle(self) -> CycleStats:
        """Run one full poll+process cycle across all channels."""
        stats = CycleStats()
        if not self._llm_configured:
            log.warning(
                "LLM not configured (LLM_API_KEY unset). Skipping this cycle; "
                "the worker stays up and will process once the key is set."
            )
            self._safe_send(
                "⏳ Mindmonk worker is live and connected to Postgres, but "
                "LLM_API_KEY is not set yet. Add it (LLM_PROVIDER, LLM_API_KEY, "
                "LLM_MODEL) in Railway Variables and I'll start producing digests."
            )
            return stats
        for channel_cfg in self.settings.app.channels:
            channel = Channel(name=channel_cfg.name, url=channel_cfg.url)
            try:
                self._process_channel(channel, stats)
            except youtube.YouTubePollError as exc:
                log.error("Channel poll failed, skipping: %s", exc)
                stats.failed += 1
            except Exception:  # noqa: BLE001 - never let one channel kill the loop
                log.exception(
                    "Unexpected error processing channel %s", channel.name
                )
                stats.failed += 1
        log.info("Cycle complete: %s", stats)
        return stats

    # ------------------------------------------------------------------ #
    def _process_channel(self, channel: Channel, stats: CycleStats) -> None:
        videos = youtube.poll_channel(channel)
        stats.polled += len(videos)
        max_per_cycle = getattr(self.settings.app, "max_per_cycle", 3)
        processed_this_cycle = 0
        for video in videos:
            if self.store.is_processed(video.video_id):
                continue
            if not youtube.is_long_form(video, self.settings.app.min_duration_seconds):
                self.store.mark_skipped(
                    video.video_id,
                    channel.name,
                    note=f"duration {video.duration_seconds:.0f}s < threshold",
                )
                stats.skipped += 1
                log.info("Skipped (short-form): %s — %s", video.title, channel.name)
                continue
            # Cap processed videos per cycle to limit LLM cost + rate limits.
            # Remaining new videos wait for the next cycle.
            if processed_this_cycle >= max_per_cycle:
                log.info(
                    "Reached max_per_cycle=%d; deferring remaining new videos "
                    "to next cycle", max_per_cycle,
                )
                break
            processed_this_cycle += 1
            self._process_video(video, stats)

    def _process_video(self, video: Video, stats: CycleStats) -> None:
        channel_name = video.channel.name
        log.info("Processing: %s — %s", video.title, channel_name)

        # 1. Transcript.
        try:
            transcript = transcripts.get_transcript(
                video, self.settings.app.languages
            )
        except transcripts.NoTranscriptError:
            self.store.mark_skipped(
                video.video_id, channel_name, note="no transcript available"
            )
            stats.skipped += 1
            if self.settings.app.notify_on_no_transcript:
                self._safe_send(
                    f"⚠️ _No transcript available_\n*{video.title}*\n{video.url}"
                )
            log.info("Skipped (no transcript): %s", video.title)
            return
        except Exception as exc:  # noqa: BLE001
            self.store.mark_failed(video.video_id, channel_name, note=str(exc))
            stats.failed += 1
            log.exception("Transcript fetch failed: %s", video.title)
            return

        if not transcript.text.strip():
            self.store.mark_skipped(
                video.video_id, channel_name, note="empty transcript"
            )
            stats.skipped += 1
            log.info("Skipped (empty transcript): %s", video.title)
            return

        # 2. Summarize.
        try:
            brief = self.summarizer.summarize(transcript, self.settings.profile)
        except LLMError as exc:
            self.store.mark_failed(video.video_id, channel_name, note=str(exc))
            stats.failed += 1
            log.error("Summarization failed: %s — %s", video.title, exc)
            self._safe_send(
                f"❌ _Summary failed_\n*{video.title}*\n{video.url}\n`{exc}`"
            )
            return

        # 3. Deliver.
        try:
            self.sender.send(brief)
        except TelegramError as exc:
            # Brief is generated; store it so we don't re-summarize, but mark
            # failed so the operator knows delivery didn't happen.
            self.store.mark_failed(
                video.video_id, channel_name, note=f"delivery failed: {exc}"
            )
            stats.failed += 1
            log.error("Telegram delivery failed: %s — %s", video.title, exc)
            return

        self.store.mark_done(video.video_id, channel_name, summary=brief)
        stats.processed += 1
        log.info("Delivered: %s — %s", video.title, channel_name)

    # ------------------------------------------------------------------ #
    def _safe_send(self, text: str) -> None:
        """Send a notice, never raising (used for best-effort notifications)."""
        try:
            self.sender.send(text)
        except TelegramError:
            log.warning("Best-effort notification failed to send")

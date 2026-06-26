"""Interactive Telegram bot handler.

Runs alongside the scheduler in a background thread, long-polling Telegram for
commands and pasted URLs. Lets you manage channels from the chat:

  /help        — show commands
  /list        — list registered channels
  /add <url>   — register a channel (or just paste a youtube URL)
  /status      — show worker + DB stats
  /latest      — re-show the most recent digest
  /remove <id> — remove a channel by list index

Security: the bot only responds to TELEGRAM_CHAT_ID (you). All other senders
are ignored, so it's safe to leave public.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import requests

from config.settings import Settings
from .models import Channel

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"


class BotHandler:
    """Long-polling command handler. Run via .start(); stops on .stop()."""

    def __init__(
        self,
        settings: Settings,
        registry: "ChannelRegistry",
        on_status: Callable[[], str],
        on_latest: Callable[[], str | None],
        on_fetch: Callable[[str], str],
        on_channel: Callable[[str], str],
    ):
        self.settings = settings
        self.registry = registry
        self._on_status = on_status
        self._on_latest = on_latest
        self._on_fetch = on_fetch
        self._on_channel = on_channel
        self._token = settings.telegram.bot_token
        self._chat_id = settings.telegram.chat_id
        self._offset = 0  # Telegram update offset for ack
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="tg-bot", daemon=True
        )
        self._thread.start()
        log.info("Bot handler started (long-polling for commands)")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001 - never kill the listener
                log.exception("Bot poll error; retrying in 5s")
                self._stop.wait(5)

    def _poll_once(self) -> None:
        url = API_BASE.format(token=self._token, method="getUpdates")
        try:
            resp = requests.post(
                url,
                json={"offset": self._offset, "timeout": 25},
                timeout=30,
            )
        except requests.RequestException:
            return
        if resp.status_code != 200:
            return
        for update in resp.json().get("result", []):
            self._offset = update["update_id"] + 1
            self._handle_update(update)

    # ------------------------------------------------------------------ #
    def _handle_update(self, update: dict) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        # Only respond to the authorized owner.
        if chat_id != self._chat_id:
            log.warning(
                "Ignoring message from unauthorized chat %s: %r",
                chat_id, text[:60],
            )
            return
        if not text:
            return
        log.info("Received message: %r", text[:100])
        try:
            if text.startswith("/"):
                self._handle_command(text)
            elif "youtube.com" in text or "youtu.be" in text:
                # Pasted URL: route to /fetch if it's a video, /add if a channel.
                if _is_video_url(text):
                    log.info("Routed pasted URL to /fetch")
                    self._cmd_fetch(text)
                else:
                    log.info("Routed pasted URL to /add")
                    self._cmd_add(text)
            else:
                log.info("Non-command, non-URL message ignored: %r", text[:60])
        except Exception:  # noqa: BLE001
            log.exception("Error handling update")
            self.send("⚠️ Something went wrong handling that. Check logs.")

    # ------------------------------------------------------------------ #
    def _handle_command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]  # strip @botname suffix
        arg = parts[1].strip() if len(parts) > 1 else ""
        log.info("Dispatching command: %s arg=%r", cmd, arg[:60])
        dispatch = {
            "/help": lambda: self._cmd_help(),
            "/start": lambda: self._cmd_help(),
            "/list": lambda: self._cmd_list(),
            "/add": lambda: self._cmd_add(arg),
            "/status": lambda: self._cmd_status(),
            "/latest": lambda: self._cmd_latest(),
            "/remove": lambda: self._cmd_remove(arg),
            "/fetch": lambda: self._cmd_fetch(arg),
            "/channel": lambda: self._cmd_channel(arg),
        }
        handler = dispatch.get(cmd)
        if handler:
            handler()
        else:
            log.warning("Unknown command: %s", cmd)
            self.send(f"Unknown command: {cmd}\nType /help for the list.")

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    def _run_async(self, ack_msg: str, fn: Callable[[], str]) -> None:
        """Ack instantly, then run a slow fn in a thread and send its result.

        Used by /fetch and /channel which take ~30-60s to summarize.
        """
        log.info("Sending ack + starting async worker")
        self.send(ack_msg)

        def _worker() -> None:
            try:
                log.info("Async worker started: running pipeline")
                result = fn()
                log.info("Async worker done: %d chars to send", len(result))
                delivered = self.send(result)
                if delivered:
                    log.info("Async result delivered to Telegram")
                else:
                    log.error("Async result FAILED to deliver to Telegram (send returned False)")
            except Exception as exc:  # noqa: BLE001
                log.exception("Async command failed: %s", str(exc)[:200])
                self.send(f"❌ {exc}")
        threading.Thread(target=_worker, name="on-demand", daemon=True).start()

    def _cmd_fetch(self, arg: str) -> None:
        url = arg.strip()
        if not url or "youtu" not in url:
            self.send("Send a video URL, e.g.:\n/fetch https://youtu.be/VIDEO_ID")
            return
        self._run_async(
            f"⏳ Fetching transcript + summarizing… (≈30-60s)\n{url}",
            lambda: self._on_fetch(url),
        )

    def _cmd_channel(self, arg: str) -> None:
        url = arg.strip()
        if not url or "youtube.com" not in url:
            self.send("Send a channel URL, e.g.:\n/channel https://www.youtube.com/@handle")
            return
        self._run_async(
            f"⏳ Finding the latest video + summarizing… (≈30-60s)\n{url}",
            lambda: self._on_channel(url),
        )

    def _cmd_help(self) -> None:
        self.send(
            "*Mindmonk Digest* commands:\n"
            "/fetch <video url> — summarize one video now\n"
            "/channel <channel url> — summarize the channel's latest video\n"
            "/add <url> — register a YouTube channel to watch\n"
            "/list — show registered channels\n"
            "/remove <n> — remove channel #n from /list\n"
            "/status — worker + DB stats\n"
            "/latest — re-show the most recent digest\n"
            "/help — this message\n\n"
            "_Tip: paste a video link to /fetch it, or a channel link to /add it._"
        )

    def _cmd_list(self) -> None:
        channels = self.registry.list_channels()
        if not channels:
            self.send("No channels registered yet. Use /add <url>.")
            return
        lines = ["*Registered channels:*"]
        for i, ch in enumerate(channels, 1):
            lines.append(f"{i}. {ch.name}\n   {ch.url}")
        self.send("\n".join(lines))

    def _cmd_add(self, arg: str) -> None:
        url = arg.strip()
        if not url or "youtube.com" not in url:
            self.send("Send a YouTube URL, e.g.:\n/add https://www.youtube.com/@channel/videos")
            return
        name = self.registry.add_channel(url)
        self.send(f"✅ Added: *{name}*\n{url}\n\nIt'll be polled on the next cycle (within 30 min).")

    def _cmd_remove(self, arg: str) -> None:
        try:
            idx = int(arg) - 1
        except ValueError:
            self.send("Usage: /remove <n>  (n from /list)")
            return
        removed = self.registry.remove_channel(idx)
        if removed:
            self.send(f"🗑 Removed: *{removed.name}*")
        else:
            self.send("No channel at that index. Use /list to check.")

    def _cmd_status(self) -> None:
        self.send(self._on_status())

    def _cmd_latest(self) -> None:
        digest = self._on_latest()
        if digest:
            self.send(digest)
        else:
            self.send("No digests produced yet.")

    # ------------------------------------------------------------------ #
    def send(self, text: str) -> bool:
        """Send text to Telegram, splitting if over the 4096-char limit.

        Returns True if all chunks sent successfully, False on any failure.
        Uses the same splitter as the scheduled-digest path (telegram.py).
        """
        from .telegram import _split_message

        chunks = _split_message(text, 4000)  # safe margin under 4096
        log.info(
            "Sending Telegram message (%d chars → %d chunk%s): %s",
            len(text), len(chunks), "s" if len(chunks) > 1 else "",
            text[:80].replace("\n", " "),
        )
        all_ok = True
        for chunk in chunks:
            ok = self._send_chunk(chunk)
            if not ok:
                all_ok = False
        return all_ok

    def _send_chunk(self, text: str) -> bool:
        """Send one chunk. Returns True on success. Retries as plain text
        if Markdown parsing fails (common for long technical briefs)."""
        url = API_BASE.format(token=self._token, method="sendMessage")
        parse_mode = "Markdown"
        for attempt in range(1, 4):
            try:
                resp = requests.post(
                    url,
                    json={
                        "chat_id": self._chat_id,
                        "text": text,
                        "parse_mode": parse_mode,
                        "disable_web_page_preview": True,
                    },
                    timeout=15,
                )
            except requests.RequestException as exc:
                log.warning("Send attempt %d failed: %s", attempt, exc)
                if attempt < 3:
                    import time as _t; _t.sleep(2 * attempt)
                continue

            if resp.status_code == 200:
                return True

            body = resp.text
            # If Markdown parsing rejected it, retry the chunk as plain text.
            if resp.status_code == 400 and "parse" in body.lower() and parse_mode:
                log.warning("Markdown parse failed; retrying chunk as plain text: %s", body[:120])
                parse_mode = ""
                continue

            log.error("Telegram sendMessage failed (%d): %s", resp.status_code, body[:200])
            if attempt < 3:
                import time as _t; _t.sleep(2 * attempt)
        return False


class ChannelRegistry:
    """Manages the channel list, persisting changes back to the store.

    Channels live in CONFIG_YAML on Railway (no files in the image). To add/
    remove persistently, we re-serialize the config and write it back via the
    Railway CLI. In-memory changes take effect immediately for the running
    worker; persistence ensures they survive redeploys.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def list_channels(self) -> list[Channel]:
        return [
            Channel(name=c.name, url=c.url)
            for c in self.settings.app.channels
        ]

    def add_channel(self, url: str) -> str:
        name = _derive_name(url)
        new = {"name": name, "url": _normalize_url(url)}
        # Avoid duplicates by URL.
        existing_urls = {c.url for c in self.settings.app.channels}
        if new["url"] in existing_urls:
            return name  # already present
        self.settings.app.channels.append(
            __import__("config.settings", fromlist=["ChannelConfig"]).ChannelConfig(
                **new
            )
        )
        self._persist()
        return name

    def remove_channel(self, idx: int) -> Channel | None:
        channels = self.settings.app.channels
        if idx < 0 or idx >= len(channels):
            return None
        removed = channels.pop(idx)
        self._persist()
        return Channel(name=removed.name, url=removed.url)

    def _persist(self) -> None:
        """Re-serialize CONFIG_YAML and update the running config + Railway."""
        import os
        import yaml

        cfg = {
            "poll_interval_minutes": self.settings.app.poll_interval_minutes,
            "min_duration_seconds": self.settings.app.min_duration_seconds,
            "max_per_cycle": self.settings.app.max_per_cycle,
            "languages": self.settings.app.languages,
            "notify_on_no_transcript": self.settings.app.notify_on_no_transcript,
            "channels": [
                {"name": c.name, "url": c.url}
                for c in self.settings.app.channels
            ],
        }
        serialized = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
        os.environ["CONFIG_YAML"] = serialized  # keep running process in sync
        # Best-effort persist to Railway (CLI may not be present/linked in all envs).
        try:
            import subprocess

            subprocess.run(
                [
                    "railway", "variables",
                    "-s", "mindmonk-digest-glm5.2",
                    "-e", "production",
                    "-p", "1a984e53-d0a9-4682-b170-6352f344ecec",
                    "--set", f"CONFIG_YAML={serialized}",
                    "-y",
                ],
                check=False,
                capture_output=True,
                timeout=30,
            )
            log.info("Persisted channel list to Railway CONFIG_YAML")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not persist to Railway (non-fatal): %s", exc)


def _derive_name(url: str) -> str:
    """Best-effort friendly name from a YouTube URL."""
    import re

    # @handle
    m = re.search(r"@([\w.\-]+)", url)
    if m:
        handle = m.group(1)
        return handle.replace("-", " ").replace("_", " ").title()
    # /channel/UC... or /user/name
    m = re.search(r"/(?:channel|user|c)/([^/?]+)", url)
    if m:
        return m.group(1).replace("-", " ").title()
    return "YouTube Channel"


def _normalize_url(url: str) -> str:
    """Ensure the URL points to the /videos tab for clean polling."""
    url = url.strip()
    if url.endswith("/videos"):
        return url
    if "?" in url:
        url = url.split("?")[0]
    return url.rstrip("/") + "/videos"


def _is_video_url(url: str) -> bool:
    """True if a YouTube URL points to a single video (vs a channel).

    Video patterns: youtu.be/<id>, /watch?v=, /embed/, /shorts/, /live/.
    Channel patterns (@handle, /channel/, /user/, /c/, /videos) → False.
    """
    import re

    if "youtu.be/" in url:
        return True
    if re.search(r"youtube\.com/(watch|embed|shorts|live)(\?|/)", url):
        return True
    if "watch?v=" in url:
        return True
    return False

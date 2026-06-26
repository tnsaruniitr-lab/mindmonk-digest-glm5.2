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
from typing import Callable

import os

import requests

from config.settings import Settings
from .models import LegacyChannel as Channel
from .mt_store import MultiTenantStore

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"


class BotHandler:
    """Long-polling command handler. Run via .start(); stops on .stop().

    Multi-tenant (Phase 1): every message resolves a user_id from the chat_id
    via MultiTenantStore.get_or_create_user. Per-user operations (/add, /list,
    /fetch) are scoped to that user_id.
    """

    def __init__(
        self,
        settings: Settings,
        mt_store: MultiTenantStore,
        on_status: Callable[[int], str],
        on_latest: Callable[[int], str | None],
        on_fetch: Callable[[int, str], str],
        on_channel: Callable[[int, str], str],
    ):
        self.settings = settings
        self.mt_store = mt_store
        self._on_status = on_status
        self._on_latest = on_latest
        self._on_fetch = on_fetch
        self._on_channel = on_channel
        self._token = settings.telegram.bot_token
        # chat_id is no longer a single-owner restriction — multi-tenant.
        # Kept for the legacy operator-only mode (empty = accept all users).
        self._operator_chat_id = settings.telegram.chat_id
        self._offset = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="tg-bot", daemon=True)
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
            self.handle_update(update)

    # ------------------------------------------------------------------ #
    def handle_update(self, update: dict) -> None:
        """Process a single Telegram update (from webhook OR long-polling).

        This is the shared entry point. The webhook handler (web.py) calls
        this directly; the long-polling loop calls it after fetching updates.
        """
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        from_user = msg.get("from", {})
        telegram_user_id = str(from_user.get("id", chat_id))
        text = (msg.get("text") or "").strip()

        # Multi-tenant: auto-create the user. Accept ALL users (not just the
        # operator). The operator_chat_id is now just for admin-tier detection.
        if not chat_id:
            return
        try:
            user_id = self.mt_store.get_or_create_user(chat_id, telegram_user_id)
        except Exception:  # noqa: BLE001
            log.exception("Failed to resolve/create user for chat_id=%s", chat_id)
            return

        if not text:
            return
        log.info("Received message from user_id=%s: %r", user_id, text[:100])
        try:
            if text.startswith("/"):
                self._handle_command(user_id, chat_id, text)
            elif "youtube.com" in text or "youtu.be" in text:
                if _is_video_url(text):
                    log.info("Routed pasted URL to /fetch")
                    self._cmd_fetch(user_id, chat_id, text)
                else:
                    log.info("Routed pasted URL to /add")
                    self._cmd_add(user_id, chat_id, text)
            elif self._looks_like_profile(text):
                # Multi-line YAML-ish input → save as the user's profile.
                log.info("Saving profile for user_id=%s", user_id)
                self._save_profile(user_id, chat_id, text)
            else:
                log.info("Non-command, non-URL message ignored: %r", text[:60])
        except Exception:  # noqa: BLE001
            log.exception("Error handling update")
            self.send(chat_id, "⚠️ Something went wrong handling that. Check logs.")

    # ------------------------------------------------------------------ #
    def _handle_command(self, user_id: int, chat_id: str, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]  # strip @botname suffix
        arg = parts[1].strip() if len(parts) > 1 else ""
        log.info("Dispatching command: %s arg=%r (user_id=%s)", cmd, arg[:60], user_id)
        dispatch = {
            "/help": lambda: self._cmd_help(chat_id),
            "/start": lambda: self._cmd_start(user_id, chat_id),
            "/profile": lambda: self._cmd_profile(user_id, chat_id),
            "/list": lambda: self._cmd_list(user_id, chat_id),
            "/add": lambda: self._cmd_add(user_id, chat_id, arg),
            "/status": lambda: self._cmd_status(user_id, chat_id),
            "/latest": lambda: self._cmd_latest(user_id, chat_id),
            "/remove": lambda: self._cmd_remove(user_id, chat_id, arg),
            "/fetch": lambda: self._cmd_fetch(user_id, chat_id, arg),
            "/channel": lambda: self._cmd_channel(user_id, chat_id, arg),
        }
        handler = dispatch.get(cmd)
        if handler:
            handler()
        else:
            log.warning("Unknown command: %s", cmd)
            self.send(chat_id, f"Unknown command: {cmd}\nType /help for the list.")

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    def _run_async(self, chat_id: str, ack_msg: str, fn: Callable[[], str]) -> None:
        """Ack instantly, then run a slow fn in a thread and send its result."""
        log.info("Sending ack + starting async worker")
        self.send(chat_id, ack_msg)

        def _worker() -> None:
            try:
                log.info("Async worker started: running pipeline")
                result = fn()
                log.info("Async worker done: %d chars to send", len(result))
                delivered = self.send(chat_id, result)
                if delivered:
                    log.info("Async result delivered to Telegram")
                else:
                    log.error("Async result FAILED to deliver to Telegram")
            except Exception as exc:  # noqa: BLE001
                log.exception("Async command failed: %s", str(exc)[:200])
                self.send(chat_id, f"❌ {exc}")

        threading.Thread(target=_worker, name="on-demand", daemon=True).start()

    def _cmd_start(self, user_id: int, chat_id: str) -> None:
        """Onboarding: welcome + show help."""
        stats = self.mt_store.user_stats(user_id)
        self.send(
            chat_id,
            f"👋 *Welcome to Mindmonk!*\n\n"
            f"I watch YouTube channels and send you sharp, structured briefs "
            f"of new episodes.\n\n"
            f"*Your account:* {stats['channels']} channel(s) subscribed\n\n"
            f"*Get started:*\n"
            f"1. Set your profile so briefs are tailored to you — send "
            f"`/profile` to see/edit it\n"
            f"2. Add channels: `/add https://www.youtube.com/@channel`\n"
            f"3. Or summarize any video now: `/fetch <url>`\n\n"
            f"Type /help for all commands.",
        )

    def _cmd_profile(self, user_id: int, chat_id: str) -> None:
        """Show the user's profile (drives section 4 tailored learnings)."""
        profile = self.mt_store.get_user_profile(user_id) if self.mt_store else ""
        if profile.strip():
            self.send(
                chat_id,
                f"*Your profile* (drives tailored learnings):\n\n"
                f"```\n{profile}\n```\n\n"
                f"To update: send me your profile as YAML, e.g.:\n"
                f"```\nprofession: Engineer\n"
                f"goals:\n  - learn ML\n"
                f"interests:\n  - systems\n"
                f"current_focus: building things\n```",
            )
        else:
            self.send(
                chat_id,
                "*Your profile is empty.*\n\n"
                "Set it so briefs are tailored to you. Send your profile as YAML:\n"
                "```\nprofession: <your role>\n"
                "skill_level: <level>\n"
                "goals:\n  - <goal 1>\n"
                "interests:\n  - <interest 1>\n"
                "current_focus: <what you're working on>\n```",
            )

    def _looks_like_profile(self, text: str) -> bool:
        """Heuristic: multi-line text with a YAML-like 'key: value' line."""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) < 1:
            return False
        # At least one line looks like 'key: value' (profile fields).
        return any(":" in ln and not ln.startswith("http") for ln in lines[:3])

    def _save_profile(self, user_id: int, chat_id: str, text: str) -> None:
        """Validate and save a YAML profile for the user."""
        import yaml

        try:
            data = yaml.safe_load(text)
            if not isinstance(data, dict):
                raise ValueError("not a mapping")
            yaml.safe_dump(data)  # re-serialize to normalize
        except Exception as exc:  # noqa: BLE001
            self.send(
                chat_id,
                f"⚠️ Couldn't parse that as a profile. Make sure it's valid YAML.\n"
                f"Error: {exc}",
            )
            return
        if self.mt_store:
            self.mt_store.set_user_profile(user_id, text.strip())
        self.send(
            chat_id,
            "✅ *Profile saved!* Your digests will now be tailored to this.\n"
            "Send /profile anytime to view or update it.",
        )

    def _cmd_fetch(self, user_id: int, chat_id: str, arg: str) -> None:
        url = arg.strip()
        if not url or "youtu" not in url:
            self.send(
                chat_id, "Send a video URL, e.g.:\n/fetch https://youtu.be/VIDEO_ID"
            )
            return
        self._run_async(
            chat_id,
            f"⏳ Fetching transcript + summarizing… (≈30-60s)\n{url}",
            lambda: self._on_fetch(user_id, url),
        )

    def _cmd_channel(self, user_id: int, chat_id: str, arg: str) -> None:
        url = arg.strip()
        if not url or "youtube.com" not in url:
            self.send(
                chat_id,
                "Send a channel URL, e.g.:\n/channel https://www.youtube.com/@handle",
            )
            return
        self._run_async(
            chat_id,
            f"⏳ Finding the latest video + summarizing… (≈30-60s)\n{url}",
            lambda: self._on_channel(user_id, url),
        )

    def _cmd_help(self, chat_id: str) -> None:
        self.send(
            chat_id,
            "*Mindmonk Digest* commands:\n"
            "/fetch <video url> — summarize one video now\n"
            "/channel <channel url> — summarize a channel's latest video\n"
            "/profile — view/edit your profile (tailors briefs to you)\n"
            "/add <url> — register a YouTube channel to watch\n"
            "/list — show your registered channels\n"
            "/remove <n> — remove channel #n from /list\n"
            "/status — your account stats\n"
            "/latest — re-show your most recent digest\n"
            "/help — this message\n\n"
            "_Tip: paste a video link to /fetch it, or a channel link to /add it._",
        )

    def _cmd_list(self, user_id: int, chat_id: str) -> None:
        channels = self.mt_store.list_channels(user_id)
        if not channels:
            self.send(chat_id, "No channels registered yet. Use /add <url>.")
            return
        lines = ["*Your channels:*"]
        for i, ch in enumerate(channels, 1):
            lines.append(f"{i}. {ch['name']}\n   {ch['url']}")
        self.send(chat_id, "\n".join(lines))

    def _cmd_add(self, user_id: int, chat_id: str, arg: str) -> None:
        url = arg.strip()
        if not url or "youtube.com" not in url:
            self.send(
                chat_id,
                "Send a YouTube URL, e.g.:\n/add https://www.youtube.com/@channel/videos",
            )
            return
        name = _derive_name(url)
        normalized = _normalize_url(url)
        created = self.mt_store.add_channel(user_id, name, normalized)
        if created:
            self.send(
                chat_id,
                f"✅ Added: *{name}*\n{normalized}\n\nPolled on the next cycle.",
            )
        else:
            self.send(chat_id, f"You're already subscribed to *{name}*.")

    def _cmd_remove(self, user_id: int, chat_id: str, arg: str) -> None:
        try:
            idx = int(arg) - 1
        except ValueError:
            self.send(chat_id, "Usage: /remove <n>  (n from /list)")
            return
        removed = self.mt_store.remove_channel(user_id, idx)
        if removed:
            self.send(chat_id, f"🗑 Removed: *{removed['name']}*")
        else:
            self.send(chat_id, "No channel at that index. Use /list to check.")

    def _cmd_status(self, user_id: int, chat_id: str) -> None:
        stats = self.mt_store.user_stats(user_id)
        self.send(
            chat_id,
            f"*Your account*\n"
            f"Channels: {stats['channels']}\n"
            f"Digests: ✅{stats['done']} done · ⏭{stats['skipped']} skipped · ❌{stats['failed']} failed",
        )

    def _cmd_latest(self, user_id: int, chat_id: str) -> None:
        digest = self._on_latest(user_id)
        if digest:
            self.send(chat_id, digest)
        else:
            self.send(chat_id, "No digests produced yet.")

    # ------------------------------------------------------------------ #
    def send(self, chat_id: str, text: str) -> bool:
        """Send text to a specific chat, splitting if over the 4096-char limit.

        Returns True if all chunks sent successfully, False on any failure.
        Multi-tenant: chat_id is the recipient (not a hardcoded operator id).
        """
        from .telegram import _split_message

        chunks = _split_message(text, 4000)  # safe margin under 4096
        log.info(
            "Sending Telegram message to chat %s (%d chars → %d chunk%s): %s",
            chat_id,
            len(text),
            len(chunks),
            "s" if len(chunks) > 1 else "",
            text[:80].replace("\n", " "),
        )
        all_ok = True
        for chunk in chunks:
            ok = self._send_chunk(chat_id, chunk)
            if not ok:
                all_ok = False
        return all_ok

    def _send_chunk(self, chat_id: str, text: str) -> bool:
        """Send one chunk to a chat. Returns True on success. Retries as plain
        text if Markdown parsing fails (common for long technical briefs)."""
        url = API_BASE.format(token=self._token, method="sendMessage")
        parse_mode = "Markdown"
        for attempt in range(1, 4):
            try:
                resp = requests.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": parse_mode,
                        "disable_web_page_preview": True,
                    },
                    timeout=15,
                )
            except requests.RequestException as exc:
                log.warning("Send attempt %d failed: %s", attempt, exc)
                if attempt < 3:
                    import time as _t

                    _t.sleep(2 * attempt)
                continue

            if resp.status_code == 200:
                return True

            body = resp.text
            # If Markdown parsing rejected it, retry the chunk as plain text.
            if resp.status_code == 400 and "parse" in body.lower() and parse_mode:
                log.warning(
                    "Markdown parse failed; retrying chunk as plain text: %s",
                    body[:120],
                )
                parse_mode = ""
                continue

            log.error(
                "Telegram sendMessage failed (%d): %s", resp.status_code, body[:200]
            )
            if attempt < 3:
                import time as _t

                _t.sleep(2 * attempt)
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
        return [Channel(name=c.name, url=c.url) for c in self.settings.app.channels]

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
        import yaml

        cfg = {
            "poll_interval_minutes": self.settings.app.poll_interval_minutes,
            "min_duration_seconds": self.settings.app.min_duration_seconds,
            "max_per_cycle": self.settings.app.max_per_cycle,
            "languages": self.settings.app.languages,
            "notify_on_no_transcript": self.settings.app.notify_on_no_transcript,
            "channels": [
                {"name": c.name, "url": c.url} for c in self.settings.app.channels
            ],
        }
        serialized = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
        os.environ["CONFIG_YAML"] = serialized  # keep running process in sync
        # Best-effort persist to Railway (CLI may not be present/linked in all envs).
        # Project/service/env IDs come from env vars, not hardcoded.
        try:
            import subprocess

            project = os.getenv("RAILWAY_PROJECT_ID", "")
            service = os.getenv("RAILWAY_SERVICE_NAME", "mindmonk-digest-glm5.2")
            environment = os.getenv("RAILWAY_ENVIRONMENT", "production")
            if not project:
                log.warning(
                    "RAILWAY_PROJECT_ID not set; cannot persist channel list to Railway"
                )
                return
            subprocess.run(
                [
                    "railway",
                    "variables",
                    "-s",
                    service,
                    "-e",
                    environment,
                    "-p",
                    project,
                    "--set",
                    f"CONFIG_YAML={serialized}",
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

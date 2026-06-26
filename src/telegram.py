"""Telegram Bot API delivery (sendMessage only, no bot framework needed).

Uses the Bot HTTP API directly via ``requests``. Messages over Telegram's 4096-
char limit are split on section boundaries so the four-section brief arrives as
a clean sequence of messages.

Setup: create a bot via @BotFather, get the token, then find your chat id by
messaging @userinfobot (or your own bot and reading getUpdates).
"""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4096
# Keep messages a bit under the hard limit to leave room for any formatting.
SAFE_LEN = 4000


class TelegramError(Exception):
    """Raised when sending to Telegram fails after retries."""


class TelegramSender:
    def __init__(self, bot_token: str, chat_id: str):
        if not bot_token or bot_token.startswith("your-"):
            raise TelegramError(
                "TELEGRAM_BOT_TOKEN is not set. Edit .env (see .env.example)."
            )
        if not chat_id or chat_id.startswith("your-"):
            raise TelegramError(
                "TELEGRAM_CHAT_ID is not set. Edit .env (see .env.example)."
            )
        self._token = bot_token
        self._chat_id = chat_id

    def send(self, text: str) -> None:
        """Send ``text``, splitting into multiple messages if needed.

        Uses Markdown parse_mode. Splitting prefers section boundaries (lines
        starting with ``###`` or ``##``), then newlines, then hard-wraps.
        """
        for chunk in _split_message(text, SAFE_LEN):
            self._send_chunk(chunk)

    def _send_chunk(self, text: str) -> None:
        url = API_BASE.format(token=self._token, method="sendMessage")
        parse_mode = "Markdown"
        last_error: Exception | str = "unknown error"
        for attempt in range(1, 4):
            payload = {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            try:
                resp = requests.post(url, json=payload, timeout=30)
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(2 * attempt)
                continue

            if resp.status_code == 200:
                return

            body = resp.text
            # If Markdown parsing rejects the message, retry as plain text.
            if resp.status_code == 400 and "parse" in body.lower() and parse_mode:
                log.warning(
                    "Markdown parse failed; retrying chunk as plain text: %s",
                    body[:200],
                )
                parse_mode = ""
                last_error = (
                    f"Telegram sendMessage failed ({resp.status_code}): {body[:300]}"
                )
                continue

            last_error = (
                f"Telegram sendMessage failed ({resp.status_code}): {body[:300]}"
            )
            if attempt < 3:
                time.sleep(2 * attempt)

        raise TelegramError(f"Failed to send Telegram message: {last_error}")


# --------------------------------------------------------------------------- #
# Message splitting
# --------------------------------------------------------------------------- #
def _split_message(text: str, max_len: int) -> list[str]:
    """Split a long message into chunks <= max_len.

    Tries, in order: section headers (### / ##), blank-line paragraphs,
    newlines, then hard character splits. Never loses content.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        cut = _best_split_point(remaining, max_len)
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return [c for c in chunks if c]


def _best_split_point(text: str, max_len: int) -> int:
    """Find the best index <= max_len at which to split ``text``."""
    window = text[:max_len]

    # 1. Prefer splitting before a section header.
    for marker in ("\n### ", "\n## ", "\n# "):
        idx = window.rfind(marker)
        if idx > max_len // 4:  # avoid tiny leading chunks
            return idx

    # 2. Then a blank-line paragraph break.
    idx = window.rfind("\n\n")
    if idx > max_len // 4:
        return idx

    # 3. Then any newline.
    idx = window.rfind("\n")
    if idx > max_len // 4:
        return idx

    # 4. Last resort: hard split on a space, else exact max_len.
    idx = window.rfind(" ")
    return idx if idx > 0 else max_len

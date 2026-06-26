"""Minimal web server for the landing page + a debug fetch endpoint.

Serves the ``landing/`` directory on the port Railway assigns ($PORT).
Also exposes ``/debug/fetch?v=<url>`` and ``/debug/health`` which run the real
fetch path directly — useful for auditing without going through Telegram.

Uses only the stdlib ``http.server`` — no extra dependency.
"""

from __future__ import annotations

import logging
import os
import threading
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

# landing/ lives at the project root, two levels up from this file.
LANDING_DIR = Path(__file__).resolve().parent.parent / "landing"

# Set by main.py so the debug endpoint can call the real pipeline.
_PIPELINE = None


_BOT_HANDLER = None


def set_pipeline(pipeline) -> None:
    """Register the pipeline so /debug endpoints can use it."""
    global _PIPELINE
    _PIPELINE = pipeline


def set_bot_handler(bot_handler) -> None:
    """Register the bot handler so the webhook endpoint can dispatch updates."""
    global _BOT_HANDLER
    _BOT_HANDLER = bot_handler


class Handler(SimpleHTTPRequestHandler):
    """Serves static files + webhook + debug endpoints."""

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Debug endpoints (audit tooling — not linked anywhere public).
        if path == "/debug/health":
            return self._json({"status": "ok", "pipeline": _PIPELINE is not None})
        if path.startswith("/debug/fetch"):
            return self._handle_debug_fetch(parsed.query)

        # Default: serve static files from landing/.
        super().do_GET()

    def do_POST(self):  # noqa: N802
        """Handle Telegram webhook updates (POST /tg/{secret})."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Telegram webhook: POST /tg/{WEBHOOK_SECRET}
        if path.startswith("/tg/"):
            return self._handle_webhook(path)

        self.send_error(404)

    def _handle_webhook(self, path: str) -> None:
        """Dispatch a Telegram update to the bot handler.

        Validates the webhook secret in the path, then hands the update
        to BotHandler.handle_update (same path as long-polling).
        """
        import json
        import os

        secret = os.getenv("WEBHOOK_SECRET", "")
        provided = path.removeprefix("/tg/")
        if not secret or provided != secret:
            log.warning("Webhook rejected: bad secret (got %r)", provided[:20])
            return self._json({"error": "forbidden"}, 403)
        if _BOT_HANDLER is None:
            return self._json({"error": "bot not ready"}, 503)

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            update = json.loads(body)
        except Exception:  # noqa: BLE001
            log.exception("Failed to parse webhook body")
            return self._json({"error": "bad request"}, 400)

        # Dispatch in the caller's thread (Telegram expects a fast 200 OK;
        # the bot's handle_update is fast — slow work goes to async threads).
        try:
            _BOT_HANDLER.handle_update(update)
        except Exception:  # noqa: BLE001
            log.exception("Webhook dispatch error")
        return self._json({"ok": True})

    def _handle_debug_fetch(self, query: str) -> None:
        """Run the real /fetch path directly. Usage: /debug/fetch?v=<video-url>"""
        if _PIPELINE is None:
            return self._json({"error": "pipeline not registered"}, 500)
        params = urllib.parse.parse_qs(query)
        url = params.get("v", [None])[0]
        if not url:
            return self._json({"error": "missing ?v=<url>"}, 400)
        try:
            brief = _PIPELINE.fetch_video_by_url(url)
            return self._json({"ok": True, "chars": len(brief), "preview": brief[:300]})
        except Exception as exc:  # noqa: BLE001
            log.exception("debug fetch failed")
            return self._json({"ok": False, "error": str(exc)[:500]}, 500)

    def _json(self, data: dict, code: int = 200) -> None:
        import json

        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # quieter logs
        pass


def start_web_server(port: int | None = None) -> threading.Thread:
    """Start serving landing/ + debug endpoints in a daemon thread."""
    port = port or int(os.getenv("PORT", "8080"))
    if not LANDING_DIR.exists():
        log.warning("Landing dir not found at %s; web server not started", LANDING_DIR)
        return threading.Thread()

    handler = partial(Handler, directory=str(LANDING_DIR))

    def _serve() -> None:
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
            log.info("Web server on http://0.0.0.0:%d (dir: %s)", port, LANDING_DIR)
            httpd.serve_forever()
        except Exception:  # noqa: BLE001
            log.exception("Web server failed")

    thread = threading.Thread(target=_serve, name="web-server", daemon=True)
    thread.start()
    return thread


def register_telegram_webhook(bot_token: str, public_url: str, secret: str) -> bool:
    """Tell Telegram to send updates to our webhook URL.

    Called on startup when WEBHOOK_SECRET + a public domain are available.
    Falls back to long-polling if this isn't set (local dev).
    Returns True on success.
    """
    import requests

    webhook_url = f"{public_url.rstrip('/')}/tg/{secret}"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["message"]},
            timeout=15,
        )
        if resp.status_code == 200 and resp.json().get("ok"):
            log.info("Telegram webhook set: %s", webhook_url)
            return True
        log.error("setWebhook failed (%d): %s", resp.status_code, resp.text[:200])
    except requests.RequestException as exc:
        log.error("setWebhook request failed: %s", exc)
    return False

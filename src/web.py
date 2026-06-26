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


def set_pipeline(pipeline) -> None:
    """Register the pipeline so /debug endpoints can use it."""
    global _PIPELINE
    _PIPELINE = pipeline


class Handler(SimpleHTTPRequestHandler):
    """Serves static files + /debug endpoints."""

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

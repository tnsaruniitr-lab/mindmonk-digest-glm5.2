"""Minimal static-file web server for the landing page.

Serves the ``landing/`` directory on the port Railway assigns ($PORT).
Runs in a background thread alongside the scheduler and bot, so a single
Railway service exposes both the website (public URL) and the worker.

Uses only the stdlib ``http.server`` — no extra dependency. Enough for a
static marketing page; not designed for high-throughput production traffic.
"""
from __future__ import annotations

import logging
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

# landing/ lives at the project root, two levels up from this file.
LANDING_DIR = Path(__file__).resolve().parent.parent / "landing"


def start_web_server(port: int | None = None) -> threading.Thread:
    """Start serving landing/ in a daemon thread. Returns the thread."""
    port = port or int(os.getenv("PORT", "8080"))
    if not LANDING_DIR.exists():
        log.warning("Landing dir not found at %s; web server not started", LANDING_DIR)
        return threading.Thread()

    handler = partial(SimpleHTTPRequestHandler, directory=str(LANDING_DIR))

    def _serve() -> None:
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
            log.info("Landing page served at http://0.0.0.0:%d (dir: %s)", port, LANDING_DIR)
            httpd.serve_forever()
        except Exception:  # noqa: BLE001
            log.exception("Web server failed")

    thread = threading.Thread(target=_serve, name="web-server", daemon=True)
    thread.start()
    return thread

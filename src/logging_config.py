"""Structured logging configuration (structlog).

Called once at startup (main.py) to render all stdlib logging as structured
JSON. Existing modules keep using ``logging.getLogger(__name__)`` — no callsite
changes needed. The benefit: every log line becomes machine-parseable JSON
with timestamp, level, logger, and (future) context vars (job_id, user_id).

For human-readable local dev, set LOG_FORMAT=text in .env.
"""

from __future__ import annotations

import logging
import os
import sys


def configure_logging(log_level: str = "INFO") -> None:
    """Configure structured (JSON) logging for the whole process."""
    fmt = os.getenv("LOG_FORMAT", "json").lower()
    level = getattr(logging, log_level.upper(), logging.INFO)

    if fmt == "text":
        # Human-readable for local dev.
        _configure_text(level)
    else:
        # Structured JSON for production (Railway log drains, Loki, etc.).
        _configure_json(level)


def _configure_json(level: int) -> None:
    try:
        import structlog
    except ImportError:
        _configure_text(level)
        return

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging through structlog so existing log.info() calls
    # are also rendered as structured JSON.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=[
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.add_log_level,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def _configure_text(level: int) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

"""Entry point for Podcast Digest.

Run modes:
  - ``python main.py --once``   : run a single cycle and exit (cron-friendly).
  - ``python main.py``          : run as a daemon, polling on a schedule
                                  via APScheduler (default for systemd).

Configuration comes from .env (secrets, provider) and the YAML files
(config.yaml, profile.yaml). See .env.example / *.example.yaml.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from pydantic import ValidationError

from config.settings import load_settings
from src.pipeline import Pipeline
from src.summarizer import LLMError
from src.telegram import TelegramError


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Avoid duplicate handlers on repeated in-process init.
    if root.handlers:
        return
    fmt = logging.Formatter(LOG_FORMAT)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    file_handler = RotatingFileHandler(
        log_dir / "podcast-digest.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def _load_settings_or_exit():
    """Load settings, converting pydantic validation errors to ConfigError."""
    try:
        return load_settings()
    except ValidationError as exc:
        # Surface a readable, multi-line message instead of a raw traceback.
        raise ConfigError(f"invalid config/profile YAML:\n{exc}") from exc


def run_once() -> int:
    log = logging.getLogger("main")
    try:
        settings = _load_settings_or_exit()
        pipeline = Pipeline(settings)  # validates API keys here
    except (LLMError, TelegramError, ConfigError) as exc:
        log.error("Configuration error: %s", exc)
        return 2
    try:
        stats = pipeline.run_cycle()
        log.info("Run complete: %s", stats)
        return 0
    except Exception:  # noqa: BLE001
        log.exception("Fatal error in run_cycle")
        return 1
    finally:
        pipeline.close()


def run_scheduled() -> int:
    log = logging.getLogger("main")
    try:
        settings = _load_settings_or_exit()
        pipeline = Pipeline(settings)  # validates API keys here
    except (LLMError, TelegramError, ConfigError) as exc:
        log.error("Configuration error: %s", exc)
        return 2
    interval = settings.app.poll_interval_minutes
    log.info(
        "Starting scheduled mode; polling every %d minute(s)", interval
    )

    scheduler = BlockingScheduler()

    def job() -> None:
        try:
            stats = pipeline.run_cycle()
            log.info("Cycle complete: %s", stats)
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            log.exception("Error in scheduled cycle")

    # Run immediately on start, then on the interval.
    scheduler.add_job(job, "interval", minutes=interval, id="poll",
                      next_run_time=None, max_instances=1, coalesce=True)
    scheduler.add_job(job, "date", id="first-run")  # fire once now

    def _shutdown(signum, _frame):  # noqa: ANN001
        log.info("Received signal %s, shutting down", signum)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if pipeline is not None:
            pipeline.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Podcast Digest")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single poll+process cycle and exit",
    )
    args = parser.parse_args()

    setup_logging(Path("logs"))
    return run_once() if args.once else run_scheduled()


if __name__ == "__main__":
    raise SystemExit(main())

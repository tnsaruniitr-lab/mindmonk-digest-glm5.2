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
import os
import signal
from logging.handlers import RotatingFileHandler
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from pydantic import ValidationError

from config.settings import load_settings
from src.pipeline import Pipeline
from src.summarizer import LLMError
from src.telegram import TelegramError
from src.bot import BotHandler
from src.web import start_web_server
from src.logging_config import configure_logging


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


def setup_logging(log_dir: Path) -> None:
    """Configure structured logging (structlog JSON, or text for local dev).

    Also keeps a rotating file handler for local debugging.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(log_level="INFO")
    # Add a rotating file handler alongside (local debug; production uses stdout).
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        log_dir / "podcast-digest.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logging.getLogger().addHandler(file_handler)


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
    log.info("Starting scheduled mode; polling every %d minute(s)", interval)

    scheduler = BlockingScheduler()

    # Web server: serves the landing page on $PORT + debug endpoints.
    from src.web import set_pipeline

    set_pipeline(pipeline)
    start_web_server()

    # Interactive bot: multi-tenant (Phase 1). Every command resolves a user_id
    # from the chat_id via MultiTenantStore, then scopes operations per-user.
    from src.mt_store import MultiTenantStore

    mt_store = None
    db_url = os.getenv("DATABASE_URL", "").strip()
    if db_url:
        try:
            mt_store = MultiTenantStore(db_url)
            log.info("MultiTenantStore connected (multi-tenant mode)")
        except Exception as exc:  # noqa: BLE001
            log.warning("MultiTenantStore init failed (non-fatal): %s", exc)

    bot = BotHandler(
        settings=settings,
        mt_store=mt_store,
        on_status=lambda uid: (
            pipeline.status_report()
            if mt_store is None
            else _user_status(mt_store, uid)
        ),
        on_latest=lambda uid: (
            pipeline.latest_digest()
            if mt_store is None
            else mt_store.latest_digest(uid)
        ),
        on_fetch=lambda uid, url: (
            pipeline.fetch_video_for_user(uid, mt_store, url)
            if mt_store
            else pipeline.fetch_video_by_url(url)
        ),
        on_channel=lambda uid, url: (
            pipeline.fetch_latest_for_user(uid, mt_store, url)
            if mt_store
            else pipeline.fetch_latest_from_channel(url)
        ),
    )
    try:
        bot.start()
    except Exception as exc:  # noqa: BLE001
        log.warning("Bot handler did not start (non-fatal): %s", exc)

    def job() -> None:
        try:
            stats = pipeline.run_cycle()
            log.info("Cycle complete: %s", stats)
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            log.exception("Error in scheduled cycle")

    scheduler.add_job(
        job, "interval", minutes=interval, id="poll", max_instances=1, coalesce=True
    )
    scheduler.add_job(job, "date", id="first-run")  # fire once now

    def _shutdown(signum, _frame):  # noqa: ANN001
        log.info("Received signal %s, shutting down", signum)
        bot.stop()
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        bot.stop()
        if pipeline is not None:
            pipeline.close()
    return 0


def _user_status(mt_store, user_id: int) -> str:
    """Per-user status string for the /status command."""
    stats = mt_store.user_stats(user_id)
    return (
        f"*Your account*\n"
        f"Channels: {stats['channels']}\n"
        f"Digests: ✅{stats['done']} done · ⏭{stats['skipped']} skipped · "
        f"❌{stats['failed']} failed"
    )


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

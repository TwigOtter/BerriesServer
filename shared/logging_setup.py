"""
shared/logging_setup.py

One consistent logging configuration for every Berries service.

This fixes the two historical pain points that made journald hard to read:
  1. The log format omitted the logger name, so a line never said *what* was
     being invoked. Every line now carries its logger name
     (e.g. shared.retrieval, discord_bot.mention, berries.trace).
  2. Each service configured logging differently — discord_bot only attached
     handlers to its own "discord_bot" logger, silently dropping INFO logs
     from shared/*, while embed_api let httpx/huggingface_hub flood INFO
     with routine HTTP requests. Configuring the root logger once, with the
     noisy third-party libraries capped at WARNING, fixes both.

Usage (top of each service entry point, before other imports log anything):
    from shared.logging_setup import setup_logging
    log = setup_logging("discord_bot")
"""

import logging
import logging.handlers

from shared.config import LOGS_DIR

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Third-party loggers that narrate routine internals at INFO/DEBUG.
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "huggingface_hub",
    "urllib3",
    "filelock",
    "sentence_transformers",
    "chromadb",
    "anthropic",
)


def setup_logging(
    service: str,
    *,
    console_level: int = logging.INFO,
    file_logging: bool = True,
) -> logging.Logger:
    """
    Configure the root logger for `service` and return the service's logger.

    - Console/journald: `console_level` and above, from ALL loggers (shared/*
      included), each line prefixed with its logger name.
    - File: logs/<service>.log at DEBUG, rotating at 5 MB with 3 backups —
      the place to look when INFO wasn't enough.
    - Noisy third-party loggers are capped at WARNING; discord.py stays at
      INFO so connect/ready lines remain visible.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Drop handlers from any previous configuration (uvicorn workers, tests,
    # logging.basicConfig calls) so lines aren't duplicated.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(fmt)
    root.addHandler(console)

    if file_logging:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOGS_DIR / f"{service}.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.INFO)

    return logging.getLogger(service)

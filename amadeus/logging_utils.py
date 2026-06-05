import logging
import os
import sys
from typing import Any

LOGGER_NAME = "amadeus"


def str_to_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def fullwidth(text: str) -> str:
    """
    Converts text to full width.
    """
    result = []

    for char in text:
        code = ord(char)

        if 0x21 <= code <= 0x7E:
            result.append(chr(code + 0xFEE0))
        elif char == " ":
            result.append("\u3000")
        else:
            result.append(char)

    return "".join(result)


def get_log_level() -> int:
    """
    Reads AMADEUS_LOG_LEVEL from ENV.

    Valid values:
    DEBUG
    INFO
    WARNING
    ERROR
    CRITICAL
    """
    level_name = os.environ.get("AMADEUS_LOG_LEVEL", "INFO").upper()
    return getattr(logging, level_name, logging.INFO)


def get_discord_log_level() -> int:
    """
    Reads AMADEUS_DISCORD_LOG_LEVEL from ENV.
    """
    level_name = os.environ.get("AMADEUS_DISCORD_LOG_LEVEL", "WARNING").upper()
    return getattr(logging, level_name, logging.WARNING)


def get_logger(name: str | None = None) -> logging.Logger:
    """
    Gets the central Amadeus logger or a child logger.

    Examples:
    get_logger() -> amadeus
    get_logger("admin") -> amadeus.admin
    get_logger("verification") -> amadeus.verification
    """

    if name is None:
        return logging.getLogger(LOGGER_NAME)

    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def normalize_level(level: str | int) -> int:
    """
    Converts a string/int level into a logging level.
    """

    if isinstance(level, int):
        return level

    normalized = level.strip().upper()

    aliases = {
        "WARN": "WARNING",
        "EXCEPTION": "ERROR",
        "FATAL": "CRITICAL",
    }

    normalized = aliases.get(normalized, normalized)

    return getattr(logging, normalized, logging.INFO)


class AmadeusFormatter(logging.Formatter):
    """
    Formats Amadeus logs.

    If fullwidth_messages is True, only the actual message becomes full-width.
    Timestamp, level, and logger name stay normal.
    """

    def __init__(self, *, fullwidth_messages: bool = False):
        super().__init__(
            fmt="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
            datefmt="%m-%d %H:%M:%S",
        )
        self.fullwidth_messages = fullwidth_messages

    def format(self, record: logging.LogRecord) -> str:
        if not self.fullwidth_messages:
            return super().format(record)

        original_msg = record.msg
        original_args = record.args

        try:
            message = record.getMessage()
            record.msg = fullwidth(message)
            record.args = ()
            return super().format(record)
        finally:
            record.msg = original_msg
            record.args = original_args


def setup_logging() -> None:
    """
    Initializes centralized logging for Amadeus.

    ENV:
    AMADEUS_LOG_LEVEL=INFO
    AMADEUS_DISCORD_LOG_LEVEL=WARNING
    AMADEUS_FULLWIDTH_LOGS=false
    """

    log_level = get_log_level()
    fullwidth_logs = str_to_bool(os.environ.get("AMADEUS_FULLWIDTH_LOGS", "false"))

    logger = get_logger()
    logger.setLevel(log_level)
    logger.propagate = False

    # Prevent duplicate log lines if setup_logging() is called more than once.
    logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)

    handler.setFormatter(
        AmadeusFormatter(fullwidth_messages=fullwidth_logs)
    )

    logger.addHandler(handler)

    # Keep discord.py logs controlled separately.
    logging.getLogger("discord").setLevel(get_discord_log_level())


def log(
    message: str,
    level: str | int = logging.INFO,
    logger_name: str | None = None,
    *args: Any,
    exc_info: bool = False,
    **kwargs: Any,
) -> None:
    """
    Central Amadeus logging helper.

    Existing usage still works:
        log("Loaded Administrative cog")

    Optional levels:
        log("Debug details", level="debug")
        log("Careful", level="warning")
        log("Failed", level="error")
        log("Boom", level="exception")

    Supports normal logging args:
        log("Loaded cog: %s", "cogs.verification")

    Supports child loggers:
        log("Panel started", logger_name="admin")
        -> amadeus.admin
    """

    if isinstance(level, str) and level.strip().lower() == "exception":
        level_number = logging.ERROR
        exc_info = True
    else:
        level_number = normalize_level(level)

    logger = get_logger(logger_name)

    logger.log(
        level_number,
        message,
        *args,
        exc_info=exc_info,
        **kwargs,
    )
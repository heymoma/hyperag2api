"""Centralized logging configuration for the Hyperagent Local Proxy.

A single ``setup_logging()`` entry point configures the whole application so the
launcher, the FastAPI proxy server and uvicorn all share one consistent format
and one set of handlers. It is idempotent (safe to call more than once), honours
environment variables, and can optionally mirror everything to a rotating log
file under ``logs/``.

Environment variables
---------------------
LOG_LEVEL      Root log level: DEBUG | INFO | WARNING | ERROR (default: INFO).
LOG_TO_FILE    "1"/"true"/"yes" to enable file logging (default: off).
LOG_FILE       Explicit log file path. Implies file logging.
LOG_DIR        Directory for log files when LOG_FILE is not set (default: "logs").
LOG_NO_COLOR   "1"/"true"/"yes" to disable ANSI colours on the console.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from typing import Optional

# The single application logger name used across every module.
LOGGER_NAME = "hyperagent-proxy"

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Marker attached to the root logger so repeated calls do not stack handlers.
_CONFIGURED_FLAG = "_hyperagent_logging_configured"

_LEVEL_COLORS = {
    "DEBUG": "\033[36m",     # cyan
    "INFO": "\033[32m",      # green
    "WARNING": "\033[33m",   # yellow
    "ERROR": "\033[31m",     # red
    "CRITICAL": "\033[41m",  # red background
}
_RESET = "\033[0m"


def _env_flag(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class _ColorFormatter(logging.Formatter):
    """Formatter that colourises the level name for interactive terminals."""

    def __init__(self, *args, use_color: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        if self.use_color:
            color = _LEVEL_COLORS.get(record.levelname, "")
            if color:
                original = record.levelname
                record.levelname = f"{color}{original}{_RESET}"
                try:
                    return super().format(record)
                finally:
                    record.levelname = original
        return super().format(record)


def _resolve_level(level: Optional[str]) -> int:
    name = (level or os.environ.get("LOG_LEVEL", "INFO")).strip().upper()
    return getattr(logging, name, logging.INFO)


def _resolve_log_file(log_file: Optional[str]) -> Optional[str]:
    if log_file:
        return log_file
    if os.environ.get("LOG_FILE"):
        return os.environ["LOG_FILE"]
    if _env_flag("LOG_TO_FILE"):
        log_dir = os.environ.get("LOG_DIR", "logs")
        return os.path.join(log_dir, "hyperagent-proxy.log")
    return None


def setup_logging(
    level: Optional[str] = None,
    log_file: Optional[str] = None,
    *,
    force: bool = False,
) -> logging.Logger:
    """Configure application-wide logging and return the app logger.

    Attaches handlers to the *root* logger so that third-party loggers
    (uvicorn, httpx, ...) inherit the same output. Uvicorn's own loggers are
    reset to propagate here instead of installing their duplicate handlers.
    """
    root = logging.getLogger()

    if getattr(root, _CONFIGURED_FLAG, False) and not force:
        # Already configured: just make sure the level is up to date.
        root.setLevel(_resolve_level(level))
        return logging.getLogger(LOGGER_NAME)

    resolved_level = _resolve_level(level)
    root.setLevel(resolved_level)

    # Clear any pre-existing handlers (e.g. a stray basicConfig) so we own output.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    # --- Console handler (stderr) --------------------------------------------
    use_color = sys.stderr.isatty() and not _env_flag("LOG_NO_COLOR")
    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(resolved_level)
    console.setFormatter(
        _ColorFormatter(_LOG_FORMAT, datefmt=_DATE_FORMAT, use_color=use_color)
    )
    root.addHandler(console)

    # --- Optional rotating file handler --------------------------------------
    resolved_file = _resolve_log_file(log_file)
    if resolved_file:
        try:
            directory = os.path.dirname(os.path.abspath(resolved_file))
            if directory:
                os.makedirs(directory, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                resolved_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            file_handler.setLevel(resolved_level)
            # Never write ANSI colour codes into files.
            file_handler.setFormatter(
                logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
            )
            root.addHandler(file_handler)
        except Exception as exc:  # pragma: no cover - defensive
            root.warning("Could not initialise file logging at %s: %s", resolved_file, exc)

    # --- Integrate uvicorn / third-party loggers -----------------------------
    # Let them bubble up to the root handlers instead of configuring their own.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        lg = logging.getLogger(noisy)
        lg.handlers = []
        lg.propagate = True
        lg.setLevel(resolved_level)

    setattr(root, _CONFIGURED_FLAG, True)

    app_logger = logging.getLogger(LOGGER_NAME)
    app_logger.debug("Logging configured (level=%s, file=%s)", logging.getLevelName(resolved_level), resolved_file or "off")
    return app_logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return the shared application logger (or a child of it)."""
    if name:
        return logging.getLogger(f"{LOGGER_NAME}.{name}")
    return logging.getLogger(LOGGER_NAME)

"""Logging setup: console warnings+ for humans, full detail to file for post-mortems.

Log file: ~/.config/tmusic/tmusic.log (rotated, 5 x 1MB).
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_LEVEL_ENV = "TMUSIC_LOG"  # set to DEBUG/VERBOSE for troubleshooting


def log_file_path() -> Path:
    return config_dir() / "tmusic.log"


def config_dir() -> Path:  # local import to avoid cycle at module load
    from .config import config_dir as _cd

    return _cd()


def setup_logging(verbosity: int = 0) -> logging.Logger:
    root = logging.getLogger("tmusic")
    if root.handlers:  # already configured (e.g. tests)
        return root

    level = logging.DEBUG if verbosity >= 2 else (logging.INFO if verbosity == 1 else logging.WARNING)
    file_level = logging.DEBUG if verbosity >= 2 else logging.DEBUG

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(level)
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        log_file = log_file_path()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        fh.setLevel(file_level)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        # logging must never take the app down
        root.warning("could not open log file; continuing without file logging")

    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"tmusic.{name}")


def mute_console() -> None:
    """Silence console (stderr) logging — used by the curses TUI, which owns
    the terminal. File logging continues."""
    for h in logging.getLogger("tmusic").handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(
            h, RotatingFileHandler
        ):
            h.setLevel(logging.CRITICAL + 1)


def user_error_text(exc: BaseException) -> str:
    """Human-facing text for an exception: prefer TmusicError.user_message."""
    from .errors import TmusicError

    if isinstance(exc, TmusicError):
        return exc.description
    return f"Unexpected error: {exc}"


def log_exception(log: logging.Logger, exc: BaseException, context: str) -> None:
    log.exception("%s: %s", context, exc)
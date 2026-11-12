"""Logging for a process that usually has no console.

The tray and the daemon both run windowless (``pythonw.exe`` on Windows, a
systemd unit). An unhandled exception anywhere outside an explicit try/except would
otherwise vanish along with the process, leaving a bare exit code and nothing to debug —
so hook every path that can kill us and write it down first.
"""

from __future__ import annotations

import contextlib
import faulthandler
import logging
import os
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

_configured = False


def setup(name: str = "tintaview", level: int = logging.INFO) -> logging.Logger:
    """Configure rotating file logging plus crash hooks. Idempotent."""
    global _configured
    from .config import config_dir
    from .fsutil import PRIVATE_FILE, ensure_private_dir

    logger = logging.getLogger("tintaview")
    if _configured:
        return logger

    # Owner-only, the directory and the log: a log names the user's projects and
    # whatever an exception happened to quote.
    ensure_private_dir(config_dir())
    log_dir = config_dir() / "logs"
    try:
        ensure_private_dir(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)  # raises where the line above only tried
        path = log_dir / f"{name}.log"
        handler: logging.Handler = RotatingFileHandler(
            path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        if sys.platform != "win32":
            with contextlib.suppress(OSError):
                os.chmod(path, PRIVATE_FILE)
    except OSError:
        handler = logging.StreamHandler(sys.stderr)
        path = None

    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
    )
    logging.basicConfig(level=level, handlers=[handler], force=True)

    def _uncaught(exc_type, exc_value, exc_tb):
        logger.critical("UNCAUGHT EXCEPTION (main thread):\n%s",
                        "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _uncaught_thread(args):
        logger.critical("UNCAUGHT EXCEPTION (thread %s):\n%s", args.thread.name,
                        "".join(traceback.format_exception(
                            args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = _uncaught
    threading.excepthook = _uncaught_thread

    # Native-level faults (a Qt/PySide abort) bypass sys.excepthook entirely. The handle
    # is intentionally left open for the life of the process — faulthandler writes to it
    # from a signal handler, so it cannot be a context manager.
    if path is not None:
        with contextlib.suppress(OSError):
            faulthandler.enable(file=open(path, "a", buffering=1, encoding="utf-8"))  # noqa: SIM115

    _configured = True
    return logger


def log_path(name: str = "tintaview") -> Path:
    from .config import config_dir

    return config_dir() / "logs" / f"{name}.log"

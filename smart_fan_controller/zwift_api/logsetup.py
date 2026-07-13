"""The zwift_api helper's own logging (separate zwift_api_polling.log file).

The subprocess runs as a separate process, so it uses its own logger and
log file to avoid clashing with the main app's file writes. Logging is
driven by the ``global_settings.logging`` / ``log_directory`` fields of
settings.json – consistent with the main app's configuration.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

log = logging.getLogger("zwift_api_polling")

# The program's launch directory (frozen exe or the settings.json folder).
_base_dir: str = (
    os.path.dirname(os.path.abspath(sys.executable))
    if getattr(sys, "frozen", False)
    else os.getcwd()
)

# The resolved log directory (set by setup_logging)
_log_dir: str = _base_dir
# Handler buffering the early (pre-settings-load) logs
_early_mem_handler: Any = None


def _close_and_clear_handlers() -> None:
    """Detach AND close the logger's handlers.

    A plain handlers.clear() would leave the file handlers open: on
    Windows the forgotten open handle would also break the log rotation
    (renaming)."""
    for h in log.handlers[:]:
        log.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass


def set_base_dir(path: str) -> None:
    """Set the default (fallback) directory of the log files."""
    global _base_dir, _log_dir
    _base_dir = path
    _log_dir = path


def resolve_log_dir(log_directory: str | None) -> str:
    """Determine and validate the log directory (fallback: the base dir)."""
    if not log_directory:
        return _base_dir
    resolved = os.path.abspath(os.path.expanduser(log_directory))
    try:
        os.makedirs(resolved, exist_ok=True)
        test_file = os.path.join(resolved, ".log_write_test")
        with open(test_file, "w", encoding="utf-8") as fh:
            fh.write("test")
        os.remove(test_file)
        return resolved
    except OSError:
        log.warning(
            f"⚠️  log_directory nem elérhető / not accessible: '{resolved}', "
            f"alapértelmezett / default: '{_base_dir}'"
        )
        return _base_dir


def setup_early_logging() -> None:
    """Buffer the logs emitted BEFORE the settings load in memory.

    The 'logging' flag is only known after the settings load, so the
    early logs (e.g. validation warnings) are kept in memory and later
    replayed (flush_early_logging) or dropped (discard_early_logging)
    once the flag is known.
    """
    from logging.handlers import MemoryHandler

    global _early_mem_handler
    _close_and_clear_handlers()
    log.setLevel(logging.DEBUG)
    log.propagate = False
    mh = MemoryHandler(capacity=100000, flushLevel=logging.CRITICAL + 10)
    log.addHandler(mh)
    _early_mem_handler = mh


def setup_logging(
    log_directory: str | None = None,
    enabled: bool = True,
    debug: bool = False,
) -> None:
    """Configure logging: console + rotating file (zwift_api_polling.log).

    When ``enabled`` is False → NullHandler (total silence, no file).
    """
    from logging.handlers import RotatingFileHandler

    global _log_dir
    _close_and_clear_handlers()
    log.propagate = False

    if not enabled:
        log.addHandler(logging.NullHandler())
        return

    level = logging.DEBUG if debug else logging.INFO
    log.setLevel(level)

    # Console (clean format – message only)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(console)

    # Rotating file (timestamped)
    _log_dir = resolve_log_dir(log_directory)
    file_handler = RotatingFileHandler(
        os.path.join(_log_dir, "zwift_api_polling.log"),
        maxBytes=500 * 1024, backupCount=2, encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.addHandler(file_handler)


def flush_early_logging() -> None:
    """Replay the buffered early logs onto the configured handlers."""
    global _early_mem_handler
    if _early_mem_handler is not None:
        for record in _early_mem_handler.buffer:
            log.handle(record)
        _early_mem_handler.close()
        _early_mem_handler = None


def discard_early_logging() -> None:
    """Drop the buffered early logs (the logging: false case)."""
    global _early_mem_handler
    if _early_mem_handler is not None:
        _early_mem_handler.buffer.clear()
        _early_mem_handler.close()
        _early_mem_handler = None

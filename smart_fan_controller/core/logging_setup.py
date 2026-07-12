"""Logging infrastructure: console, rotating file, early buffering.

Ensures a consistent configuration of the two loggers (user_logger,
logger):
- Console: user_logger messages only, logger WARNING+ debug info
- File: both DEBUG+
- Headless mode: logging:false → NullHandler (silence)
- Early logging: buffering of logs emitted before settings are loaded
"""

import logging
import os
import sys
from logging.handlers import MemoryHandler, RotatingFileHandler

from smart_fan_controller.core.helpers import resolve_log_dir

__all__ = [
    "logger",
    "user_logger",
    "setup_logging",
    "setup_early_logging",
    "flush_early_logging",
    "discard_early_logging",
    "is_logging_enabled",
]

logger = logging.getLogger("zwift_fan_controller_new")
user_logger = logging.getLogger("user")


def _default_log_dir() -> str:
    """The default log directory: the directory of the
    ``zwift_fan_controller.py`` entry script (next to the exe when frozen).

    This module lives under ``smart_fan_controller/core/``, so the entry
    script (project root) is three levels up. Before the refactor logging
    lived in the entry script itself, putting the logs next to the script
    – this restores that behavior.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    # .../<root>/smart_fan_controller/core/logging_setup.py → <root>
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


# Module-level state
_log_dir: str = _default_log_dir()
_logging_enabled: bool = True
_early_mem_handlers: list[tuple[logging.Logger, MemoryHandler]] = []


def _close_and_clear_handlers(lg: logging.Logger) -> None:
    """Detach AND close the logger's handlers.

    A plain handlers.clear() would leave the file handlers open: the
    descriptors would leak, and on Windows the forgotten open handle
    makes log rotation (file rename) fail with WinError 32.
    close does not drain the MemoryHandler buffer (it has no target), so
    replaying the early logs is unaffected."""
    for h in lg.handlers[:]:
        lg.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass


def setup_logging(log_directory: str | None = None, logging_enabled: bool = True) -> None:
    """Logging configuration: console + rotating file (500 KB max).

    Two loggers:
      - ``user_logger``: user-facing messages (console + file). Clean
        format on the console (message only), timestamped in the file.
      - ``logger``: internal debug/info logs (always to file, console
        only at WARNING+).

    Log files go into ``log_directory`` (when valid), otherwise into the
    directory of the ``zwift_fan_controller.py`` entry script (next to
    the exe when frozen).

    When ``logging_enabled`` is False both loggers get a NullHandler
    (total silence – no file, no console). The startup summary still
    appears regardless (``print_startup_info`` switches to print()).

    Safe to call repeatedly: previous handlers are removed.

    Args:
        log_directory: Directory for the log files (None = default).
        logging_enabled: When False, all logging is disabled.
    """
    global _log_dir, _logging_enabled
    _logging_enabled = logging_enabled

    # Logging disabled → silence both loggers with a NullHandler
    if not logging_enabled:
        for name in ("user", "zwift_fan_controller_new"):
            lg = logging.getLogger(name)
            _close_and_clear_handlers(lg)
            lg.addHandler(logging.NullHandler())
            lg.propagate = False
        logging.getLogger("bleak").setLevel(logging.CRITICAL)
        logging.getLogger("openant").setLevel(logging.CRITICAL)
        return

    _log_dir = resolve_log_dir(log_directory, default_dir=_default_log_dir())
    log_file = os.path.join(_log_dir, "smart_fan_controller.log")

    file_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_file, maxBytes=500 * 1024, backupCount=2, encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_fmt)

    # ── user_logger: user-facing messages ──
    ul = logging.getLogger("user")
    _close_and_clear_handlers(ul)  # Close previous handlers (on re-invocation)
    ul.setLevel(logging.DEBUG)
    ul.propagate = False

    console_user = logging.StreamHandler(sys.stdout)
    console_user.setLevel(logging.DEBUG)
    console_user.setFormatter(logging.Formatter("%(message)s"))
    ul.addHandler(console_user)
    ul.addHandler(file_handler)

    # ── logger: internal logs ──
    il = logging.getLogger("zwift_fan_controller_new")
    _close_and_clear_handlers(il)
    il.setLevel(logging.DEBUG)
    il.propagate = False

    console_internal = logging.StreamHandler(sys.stderr)
    console_internal.setLevel(logging.WARNING)
    console_internal.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S",
    ))
    il.addHandler(console_internal)
    il.addHandler(file_handler)

    # Silence noisy third-party libraries
    logging.getLogger("bleak").setLevel(logging.CRITICAL)
    logging.getLogger("openant").setLevel(logging.CRITICAL)


def setup_early_logging() -> None:
    """Early logging: buffer the logs emitted BEFORE settings are loaded.

    Since the ``global_settings.logging`` flag is only known after the
    settings load, the early logs (e.g. config validation warnings) are
    kept in memory. Once the flag is known they are either replayed onto
    the real handlers (``flush_early_logging``) or dropped
    (``discard_early_logging``). This way ``logging: false`` creates no
    stray log file, while ``logging: true`` loses no early warnings.
    """
    global _early_mem_handlers
    _early_mem_handlers = []
    for name in ("user", "zwift_fan_controller_new"):
        lg = logging.getLogger(name)
        _close_and_clear_handlers(lg)
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        # Large capacity + high flushLevel → never flushes on its own
        mh = MemoryHandler(capacity=100000, flushLevel=logging.CRITICAL + 10)
        lg.addHandler(mh)
        _early_mem_handlers.append((lg, mh))
    logging.getLogger("bleak").setLevel(logging.CRITICAL)
    logging.getLogger("openant").setLevel(logging.CRITICAL)


def flush_early_logging() -> None:
    """Replay the buffered early logs onto the configured handlers."""
    global _early_mem_handlers
    for lg, mh in _early_mem_handlers:
        for record in mh.buffer:
            lg.handle(record)
        mh.close()
    _early_mem_handlers = []


def discard_early_logging() -> None:
    """Drop the buffered early logs (the logging: false case)."""
    global _early_mem_handlers
    for _lg, mh in _early_mem_handlers:
        mh.buffer.clear()
        mh.close()
    _early_mem_handlers = []


def is_logging_enabled() -> bool:
    """Return whether logging is enabled (set by setup_logging).

    Used by ``print_startup_info``: when logging is disabled it switches
    to ``print()`` so the startup summary still appears.
    """
    return _logging_enabled

"""Throttled console output – thread-safe, no Qt/BLE/IO dependencies.

ConsolePrinter manages the messages: the same message must not appear
too frequently. It keeps a timer per message key and only prints when
the interval has elapsed since the last print. A threading.Lock guards
the _last_times dict against concurrent access.
"""
from __future__ import annotations

import logging
import threading
import time

user_logger = logging.getLogger("user")


class ConsolePrinter:
    """Throttled console output – the same message must not repeat too often.

    Keeps a separate timer per message type (key). A message is only
    printed when at least interval seconds elapsed since its last print.

    Note: the method is named 'emit' so it does not shadow the built-in
    print().

    Attributes:
        _last_times: Time of the last print per message key.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_times: dict[str, float] = {}

    def emit(self, key: str, message: str, interval: float = 1.0) -> bool:
        """Print the message when the interval has elapsed.

        Args:
            key: Unique key identifying the message (e.g. "power_raw").
            message: The text to print.
            interval: Minimum seconds between two prints of the same key.

        Returns:
            True when the message was printed; False when throttled.
        """
        now = time.monotonic()
        with self._lock:
            if now - self._last_times.get(key, 0.0) >= interval:
                user_logger.info(message)
                self._last_times[key] = now
                return True
            return False

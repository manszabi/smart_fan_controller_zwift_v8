"""Rolling averaging – pure domain logic (no Qt/BLE/IO dependencies).

``_RollingAverager`` and its subclasses compute rolling averages from
incoming numeric samples. They rely only on the standard library
(``collections.deque``, ``logging``), so they are testable on their own.
"""
from __future__ import annotations

import logging
from collections import deque

# Internal debug logs go to the project's named logger (not the root),
# consistent with the other modules.
logger = logging.getLogger("zwift_fan_controller_new")


def compute_average(samples: "deque[float]") -> float | None:
    """Compute the arithmetic mean of the samples.

    Args:
        samples: Deque holding the samples.

    Returns:
        The average as float, or None when there are no samples.
    """
    if not samples:
        return None
    return sum(samples) / len(samples)


class _RollingAverager:
    """Computes a rolling average from incoming numeric samples.

    Expects buffer_rate_hz samples per second and keeps a window of
    buffer_seconds. The effective_minimum automatically adapts to the
    real buffer size, so an average is produced even when fewer samples
    arrive than minimum_samples.

    Attributes:
        buffer: Deque of samples (maxlen = buffer_seconds × buffer_rate_hz).
        minimum_samples: Desired minimum sample count for a valid average.
        effective_minimum: Actually applied minimum (cap: buffersize // 2).
        buffersize: Maximum size of the buffer.
    """

    def __init__(
        self,
        buffer_seconds: int,
        minimum_samples: int,
        buffer_rate_hz: int = 4,
        label: str = "adat",
    ) -> None:
        rate = max(1, int(buffer_rate_hz))
        self.buffersize = max(1, int(buffer_seconds) * rate)
        self.buffer: deque[float] = deque(maxlen=self.buffersize)
        self.minimum_samples = minimum_samples
        # Guard: effective_minimum never exceeds half of the buffer
        self.effective_minimum = min(self.minimum_samples, max(1, self.buffersize // 2))
        self._label = label
        # Running sum: O(1) averaging per sample instead of summing the
        # whole buffer each time. Exact for the integer samples the
        # processors feed in (no float drift).
        self._sum: float = 0.0

    def add_sample(self, value: float) -> float | None:
        """Add a sample and return the average once enough samples exist."""
        if len(self.buffer) == self.buffersize:
            self._sum -= self.buffer[0]  # the append below evicts this sample
        self.buffer.append(value)
        self._sum += value
        if len(self.buffer) < self.effective_minimum:
            logger.debug(
                "%s adatok gyűjtése: %d/%d (effective min)",
                self._label,
                len(self.buffer),
                self.effective_minimum,
            )
            return None
        return self._sum / len(self.buffer)

    def clear(self) -> None:
        """Clear all buffered samples."""
        self.buffer.clear()
        self._sum = 0.0


class PowerAverager(_RollingAverager):
    """Rolling average for power (watt) samples."""

    def __init__(
        self, buffer_seconds: int, minimum_samples: int, buffer_rate_hz: int = 4
    ) -> None:
        super().__init__(buffer_seconds, minimum_samples, buffer_rate_hz, label="Power")


class HRAverager(_RollingAverager):
    """Rolling average for heart-rate (bpm) samples."""

    def __init__(
        self, buffer_seconds: int, minimum_samples: int, buffer_rate_hz: int = 4
    ) -> None:
        super().__init__(buffer_seconds, minimum_samples, buffer_rate_hz, label="HR")

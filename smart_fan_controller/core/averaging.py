"""Rolling averaging – pure domain logic (no Qt/BLE/IO dependencies).

``_RollingAverager`` and its subclasses compute rolling averages from
incoming numeric samples. They rely only on the standard library
(``collections.deque``, ``logging``), so they are testable on their own.
"""
from __future__ import annotations

import logging
import time
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
    """Computes a rolling average over the last ``buffer_seconds`` of samples.

    The window is **time based**: a sample leaves the buffer once it is
    older than ``buffer_seconds``, regardless of how fast the data
    actually arrives. ``buffer_rate_hz`` (the expected samples per second)
    only sizes the capacity cap, ``buffer_seconds × buffer_rate_hz``.

    This matters because the sources run at very different rates. With a
    sample-count-only window the Zwift source (whose HTTPS API allows a
    poll every 3 s at best, i.e. ~0.33 Hz) filled its 10 s × 3 Hz = 30
    sample buffer with **90 seconds** of data, so the fan followed a
    minute-and-a-half average. The BLE/ANT+ sources (4 Hz, matching their
    configured rate) are unaffected: there 3 s × 4 Hz = 12 samples really
    is 3 seconds' worth, and both limits agree.

    ``effective_minimum`` samples are always kept, even when they are
    older than the window – otherwise a source slower than expected could
    never produce an average at all, leaving the fan without control.

    Attributes:
        buffer: Deque of samples (at most buffer_seconds × buffer_rate_hz).
        window_seconds: Length of the time window in seconds.
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
        self.window_seconds = float(max(1, int(buffer_seconds)))
        self.buffersize = max(1, int(buffer_seconds) * rate)
        self.buffer: deque[float] = deque()
        # Arrival time of each sample, in lockstep with buffer
        self._times: deque[float] = deque()
        self.minimum_samples = minimum_samples
        # Guard: effective_minimum never exceeds half of the buffer
        self.effective_minimum = min(self.minimum_samples, max(1, self.buffersize // 2))
        self._label = label
        # Running sum: O(1) averaging per sample instead of summing the
        # whole buffer each time. Exact for the integer samples the
        # processors feed in (no float drift).
        self._sum: float = 0.0

    def add_sample(self, value: float, now: float | None = None) -> float | None:
        """Add a sample and return the average once enough samples exist.

        Args:
            value: The new sample.
            now: Arrival time (monotonic). Only for tests – production
                code leaves it None, which reads the clock.
        """
        if now is None:
            now = time.monotonic()
        self.buffer.append(value)
        self._times.append(now)
        self._sum += value
        self._evict(now)
        if len(self.buffer) < self.effective_minimum:
            logger.debug(
                "%s adatok gyűjtése: %d/%d (effective min)",
                self._label,
                len(self.buffer),
                self.effective_minimum,
            )
            return None
        return self._sum / len(self.buffer)

    def _evict(self, now: float) -> None:
        """Drop samples over the capacity cap and outside the time window."""
        # 1. Capacity cap (memory guard)
        while len(self.buffer) > self.buffersize:
            self._drop_oldest()
        # 2. Time window – but never below effective_minimum, so a
        #    slower-than-expected source still yields an average
        while (
            len(self.buffer) > self.effective_minimum
            and (now - self._times[0]) > self.window_seconds
        ):
            self._drop_oldest()

    def _drop_oldest(self) -> None:
        self._sum -= self.buffer.popleft()
        self._times.popleft()

    def clear(self) -> None:
        """Clear all buffered samples."""
        self.buffer.clear()
        self._times.clear()
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

"""Gördülő átlagolás – tiszta domain-logika (nincs Qt/BLE/IO függőség).

A ``_RollingAverager`` és leszármazottai bejövő numerikus mintákból számítanak
gördülő átlagot. Csak a beépített könyvtárakra (``collections.deque``,
``logging``) támaszkodnak, ezért önállóan is tesztelhetők.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Optional


def compute_average(samples: "deque[float]") -> Optional[float]:
    """Kiszámítja a minták számtani átlagát.

    Args:
        samples: Mintákat tartalmazó deque.

    Returns:
        Az átlag float értéke, vagy None, ha nincs minta.
    """
    if not samples:
        return None
    return sum(samples) / len(samples)


class _RollingAverager:
    """Gördülő átlagot számít bejövő numerikus mintákból.

    buffer_rate_hz mintát vár másodpercenként, és buffer_seconds
    másodpercnyi ablakot tart. Az effective_minimum automatikusan
    alkalmazkodik a valódi buffer méretéhez, így akkor is
    számol átlagot, ha kevesebb adat érkezik, mint minimum_samples.

    Attribútumok:
        buffer: Mintákat tároló deque (maxlen = buffer_seconds × buffer_rate_hz).
        minimum_samples: Kívánt minimum mintaszám érvényes átlaghoz.
        effective_minimum: Ténylegesen alkalmazott minimum (max: buffersize // 2).
        buffersize: A buffer maximális mérete.
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
        # Védelem: effective_minimum soha nem nagyobb, mint a buffer fele
        self.effective_minimum = min(self.minimum_samples, max(1, self.buffersize // 2))
        self._label = label

    def add_sample(self, value: float) -> Optional[float]:
        """Új minta hozzáadása és az átlag visszaadása, ha elég minta van."""
        self.buffer.append(value)
        if len(self.buffer) < self.effective_minimum:
            logging.debug(
                "%s adatok gyűjtése: %d/%d (effective min)",
                self._label,
                len(self.buffer),
                self.effective_minimum,
            )
            return None
        return compute_average(self.buffer)

    def clear(self) -> None:
        """Törli az összes pufferelt mintát."""
        self.buffer.clear()


class PowerAverager(_RollingAverager):
    """Gördülő átlagszámítás teljesítmény (watt) mintákhoz."""

    def __init__(
        self, buffer_seconds: int, minimum_samples: int, buffer_rate_hz: int = 4
    ) -> None:
        super().__init__(buffer_seconds, minimum_samples, buffer_rate_hz, label="Power")


class HRAverager(_RollingAverager):
    """Gördülő átlagszámítás szívfrekvencia (bpm) mintákhoz."""

    def __init__(
        self, buffer_seconds: int, minimum_samples: int, buffer_rate_hz: int = 4
    ) -> None:
        super().__init__(buffer_seconds, minimum_samples, buffer_rate_hz, label="HR")

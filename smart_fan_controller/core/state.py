"""Szálbiztos state snapshot – asyncio és UI szálak közötti adatcsere.

Az UISnapshot a asyncio event loop és a PySide6 UI szál közötti szálbiztos
adatcserét támogatja. Az asyncio szál update()-tel frissít, a UI szál read()
-tel olvas. A threading.Lock garantálja a race condition-mentességet.

Nincs Qt/BLE/IO függőség, csak threading és typing.
"""
from __future__ import annotations

import dataclasses
import threading
from typing import Optional, Tuple


@dataclasses.dataclass
class UISnapshot:
    """Szálbiztos snapshot az asyncio loop és a PySide6 UI között.

    Az asyncio oldalon update() hívással frissítendő,
    a PySide6 oldalon read() hívással olvasható.
    A threading.Lock garantálja a race condition-mentességet.
    """

    zone: Optional[int] = None
    avg_power: Optional[float] = None
    avg_hr: Optional[float] = None
    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)

    def update(
        self,
        zone: Optional[int],
        avg_power: Optional[float],
        avg_hr: Optional[float],
    ) -> None:
        """Frissíti a snapshot értékeit (asyncio szálból hívandó)."""
        with self._lock:
            self.zone = zone
            self.avg_power = avg_power
            self.avg_hr = avg_hr

    def read(self) -> Tuple[Optional[int], Optional[float], Optional[float]]:
        """Visszaadja a snapshot értékeit (PySide6 szálból hívandó)."""
        with self._lock:
            return self.zone, self.avg_power, self.avg_hr

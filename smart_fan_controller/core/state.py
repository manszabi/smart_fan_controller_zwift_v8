"""Szálbiztos state snapshot – asyncio és UI szálak közötti adatcsere.

Ez a modul az asyncio event loop és a PySide6 UI szál közötti szálbiztos
adatcserét, valamint az asyncio task-ok közötti megosztott állapot kezelését
támogatja. Nincs Qt/BLE/IO függőség, csak asyncio, threading, és typing.
"""
from __future__ import annotations

import asyncio
import dataclasses
import threading
from typing import Optional, Tuple


class ControllerState:
    """A vezérlő megosztott állapota, asyncio.Lock-kal védve.

    Minden olyan mezőt tartalmaz, amelyet több asyncio korrutin is olvas
    vagy módosít. A lock biztosítja, hogy az olvasás-módosítás-írás
    műveletek atomikusak legyenek.

    Az ui_snapshot külön threading.Lock-kal védett, és kizárólag
    a PySide6 UI frissítéséhez használatos (szálbiztos olvasás).

    Attribútumok:
        current_zone: Az aktuálisan aktív ventilátor zóna (None = nincs döntés még).
        current_power_zone: A legutóbb kiszámított power zóna.
        current_hr_zone: A legutóbb kiszámított HR zóna.
        current_avg_power: A legutóbbi átlagolt teljesítmény (W).
        current_avg_hr: A legutóbbi átlagolt HR (bpm).
        last_power_time: Utolsó power adat érkezési ideje (monotonic).
        last_hr_time: Utolsó HR adat érkezési ideje (monotonic), vagy None.
        lock: asyncio.Lock a párhuzamos módosítások ellen.
        ui_snapshot: UISnapshot a PySide6 UI szálbiztos frissítéséhez.
    """

    def __init__(self) -> None:
        self.current_zone: Optional[int] = None
        self.current_power_zone: Optional[int] = None
        self.current_hr_zone: Optional[int] = None
        self.current_avg_power: Optional[float] = None
        self.current_avg_hr: Optional[float] = None
        self.last_power_time: Optional[float] = None
        self.last_hr_time: Optional[float] = None
        self.lock = asyncio.Lock()
        self.ui_snapshot = UISnapshot()

    def __repr__(self) -> str:
        return (
            f"ControllerState(zone={self.current_zone}, "
            f"power_zone={self.current_power_zone}, hr_zone={self.current_hr_zone}, "
            f"avg_power={self.current_avg_power}, avg_hr={self.current_avg_hr})"
        )


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

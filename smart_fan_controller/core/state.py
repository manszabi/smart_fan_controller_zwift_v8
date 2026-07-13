"""Thread-safe state snapshot – data exchange between asyncio and the UI.

This module supports thread-safe data exchange between the asyncio event
loop and the PySide6 UI thread, plus shared state between asyncio tasks.
No Qt/BLE/IO dependencies, only asyncio, threading and typing.
"""
from __future__ import annotations

import asyncio
import dataclasses
import threading


class ControllerState:
    """Shared controller state, guarded by an asyncio.Lock.

    Contains every field that multiple asyncio coroutines read or
    modify. The lock makes the read-modify-write operations atomic.

    ui_snapshot is guarded by its own threading.Lock and is used solely
    for updating the PySide6 UI (thread-safe reads).

    Attributes:
        current_zone: The currently active fan zone (None = no decision yet).
        current_power_zone: The most recently computed power zone.
        current_hr_zone: The most recently computed HR zone.
        current_avg_power: The latest averaged power (W).
        current_avg_hr: The latest averaged HR (bpm).
        last_power_time: Arrival time of the last power data (monotonic).
        last_hr_time: Arrival time of the last HR data (monotonic), or None.
        lock: asyncio.Lock against concurrent modification.
        ui_snapshot: UISnapshot for thread-safe PySide6 UI updates.
    """

    def __init__(self) -> None:
        self.current_zone: int | None = None
        self.current_power_zone: int | None = None
        self.current_hr_zone: int | None = None
        self.current_avg_power: float | None = None
        self.current_avg_hr: float | None = None
        self.last_power_time: float | None = None
        self.last_hr_time: float | None = None
        self.lock = asyncio.Lock()
        self.ui_snapshot = UISnapshot()

    def __repr__(self) -> str:
        return (
            f"ControllerState(zone={self.current_zone}, "
            f"power_zone={self.current_power_zone}, hr_zone={self.current_hr_zone}, "
            f"avg_power={self.current_avg_power}, avg_hr={self.current_avg_hr})"
        )


@dataclasses.dataclass(slots=True)
class UISnapshot:
    """Thread-safe snapshot between the asyncio loop and the PySide6 UI.

    Updated on the asyncio side via update(), read on the PySide6 side
    via read(). The threading.Lock guarantees freedom from races.
    """

    zone: int | None = None
    avg_power: float | None = None
    avg_hr: float | None = None
    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)

    def update(
        self,
        zone: int | None,
        avg_power: float | None,
        avg_hr: float | None,
    ) -> None:
        """Update the snapshot values (to be called from the asyncio thread)."""
        with self._lock:
            self.zone = zone
            self.avg_power = avg_power
            self.avg_hr = avg_hr

    def read(self) -> tuple[int | None, float | None, float | None]:
        """Return the snapshot values (to be called from the PySide6 thread)."""
        with self._lock:
            return self.zone, self.avg_power, self.avg_hr

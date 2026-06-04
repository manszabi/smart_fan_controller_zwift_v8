"""Input handler abstraktion – asyncio-based adatforrások.

Ez a csomag az Input handler-eket tartalmazza, amelyek szenzor-
adatokat (power, HR, stb.) fogadnak és asyncio Queue-kba helyezik.

Modulok:
- _ant.py: ANT+ power és HR adatforrás
- _ble.py: BLE ventilátor kimenet és szenzor bemenetek
- zwift_udp.py: Zwift UDP adatforrás
"""
from __future__ import annotations

from ._ant import ANTPlusInputHandler, _ANTPLUS_AVAILABLE
from ._ble import (
    BLECombinedSensor,
    BLEFanOutputController,
    BLEHRInputHandler,
    BLEPowerInputHandler,
    _BLESensorInputHandler,
    send_zone,
)
from .zwift_udp import ZwiftUDPInputHandler

__all__ = [
    # ANT+ handler
    "ANTPlusInputHandler",
    "_ANTPLUS_AVAILABLE",
    # BLE handlers
    "BLEFanOutputController",
    "_BLESensorInputHandler",
    "BLEPowerInputHandler",
    "BLEHRInputHandler",
    "BLECombinedSensor",
    "send_zone",
    # Zwift UDP handler
    "ZwiftUDPInputHandler",
]

"""Input handler abstractions – asyncio-based data sources.

This package contains the input handlers that receive sensor data
(power, HR, etc.) and place it into asyncio queues.

Modules:
- _ant.py: ANT+ power and HR data source
- _ble.py: BLE fan output and sensor inputs
- zwift_udp.py: Zwift UDP data source
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

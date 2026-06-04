"""Input handler abstraktion – asyncio-based adatforrások.

Ez a csomag az Input handler-eket tartalmazza, amelyek szenzor-
adatokat (power, HR, stb.) fogadnak és asyncio Queue-kba helyezik.
"""
from __future__ import annotations

from .zwift_udp import ZwiftUDPInputHandler

__all__ = [
    "ZwiftUDPInputHandler",
]

"""Tiszta domain-logika (zóna számítás, validáció, átlagolás, cooldown, UI).

Ez a csomag a mellékhatás-mentes (Qt/BLE/IO-független) magfüggvényeket és
-osztályokat gyűjti össze, amelyek korábban a fő alkalmazás moduljában éltek.
"""
from __future__ import annotations

from .averaging import (
    HRAverager,
    PowerAverager,
    _RollingAverager,
    compute_average,
)
from .cooldown import CooldownController
from .printers import ConsolePrinter
from .zones import (
    apply_zone_mode,
    calculate_hr_zones,
    calculate_power_zones,
    higher_wins,
    is_valid_hr,
    is_valid_power,
    zone_for_hr,
    zone_for_power,
)

__all__ = [
    # zones
    "calculate_power_zones",
    "calculate_hr_zones",
    "zone_for_power",
    "zone_for_hr",
    "is_valid_power",
    "is_valid_hr",
    "higher_wins",
    "apply_zone_mode",
    # averaging
    "compute_average",
    "_RollingAverager",
    "PowerAverager",
    "HRAverager",
    # cooldown
    "CooldownController",
    # printers
    "ConsolePrinter",
]

"""Tiszta zóna-logika – számítás, validáció, zóna-mód kombinálás.

Ez a modul mellékhatás-mentes (pure) függvényeket tartalmaz: nincs Qt, BLE,
ANT+ vagy I/O függőség. Csak a beépített könyvtárakra és a ``ZoneMode`` enumra
támaszkodik, ezért önmagában is könnyen tesztelhető.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

from ..config.schemas import ZoneMode


# ============================================================
# ZÓNA SZÁMÍTÁS
# ============================================================


def calculate_power_zones(
    ftp: int,
    min_watt: int,
    max_watt: int,
    z1_pct: int,
    z2_pct: int,
) -> Dict[int, Tuple[int, int]]:
    """Kiszámítja a teljesítmény zóna határokat.

    Args:
        ftp: Funkcionális küszöbteljesítmény (W).
        min_watt: Minimális érvényes pozitív teljesítmény (W).
        max_watt: Maximális érvényes teljesítmény (W).
        z1_pct: Z1 felső határ az FTP %-ában.
        z2_pct: Z2 felső határ az FTP %-ában.

    Returns:
        Dict formátum: {0: (0,0), 1: (1, z1_max), 2: (z1_max+1, z2_max), 3: (z2_max+1, max_watt)}
    """
    # max(1, ...) védi az érvénytelen z1_max=0 esetet (pl. nagyon alacsony FTP/százalék)
    z1_max = max(1, int(ftp * z1_pct / 100))
    z2_max = max(2, min(int(ftp * z2_pct / 100), max_watt))
    z1_max = min(z1_max, z2_max - 1)
    return {
        0: (0, 0),
        1: (1, z1_max),
        2: (z1_max + 1, z2_max),
        3: (z2_max + 1, max_watt),
    }


def calculate_hr_zones(
    max_hr: int,
    resting_hr: int,
    z1_pct: int,
    z2_pct: int,
) -> Dict[str, int]:
    """Kiszámítja a HR zóna határokat bpm-ben.

    Args:
        max_hr: Maximális szívfrekvencia (bpm).
        resting_hr: Pihenő szívfrekvencia (bpm); ez alatt Z0.
        z1_pct: Z1 felső határ a max_hr %-ában.
        z2_pct: Z2 felső határ a max_hr %-ában.

    Returns:
        Dict: {'resting': int, 'z1_max': int, 'z2_max': int}
    """
    return {
        "resting": resting_hr,
        "z1_max": int(max_hr * z1_pct / 100),
        "z2_max": int(max_hr * z2_pct / 100),
    }


def zone_for_power(power: float, zones: Dict[int, Tuple[int, int]]) -> int:
    """Meghatározza a teljesítmény zónát (0–3) az adott watt értékhez.

    Args:
        power: Teljesítmény wattban.
        zones: Zóna határok dict-je (calculate_power_zones kimenetele).

    Returns:
        Zóna szám (0–3).
    """
    if power <= 0:
        return 0
    # Védekezés üres vagy hibás zones dict ellen (ValueError elkerülése)
    positive_lows = [lo for lo, _hi in zones.values() if lo > 0]
    if not positive_lows:
        return 0
    min_lo = min(positive_lows)
    if power < min_lo:
        return 0
    for zone_num in sorted(zones):
        lo, hi = zones[zone_num]
        if lo <= power <= hi:
            return zone_num
    return 3  # csak max_watt felett érthető el


def zone_for_hr(hr: int, hr_zones: Dict[str, int]) -> int:
    """Meghatározza a HR zónát (0–3) az adott bpm értékhez.

    Args:
        hr: Szívfrekvencia bpm-ben.
        hr_zones: HR zóna határok dict-je (calculate_hr_zones kimenetele).

    Returns:
        Zóna szám (0–3).
    """
    if hr <= 0 or hr < hr_zones["resting"]:
        return 0
    if hr <= hr_zones["z1_max"]:
        return 1
    if hr <= hr_zones["z2_max"]:
        return 2
    return 3


# ============================================================
# BEMENETI ADAT VALIDÁCIÓ
# ============================================================


def is_valid_power(power: Any, min_watt: int, max_watt: int) -> bool:
    """Ellenőrzi, hogy az érték érvényes teljesítmény adat-e.

    Args:
        power: Az ellenőrizendő érték.
        min_watt: Minimális érvényes pozitív watt (0 és min_watt között elutasítva).
        max_watt: Maximális érvényes watt.

    Returns:
        True, ha érvényes teljesítmény adat.
    """
    if isinstance(power, bool):
        return False
    if not isinstance(power, (int, float)):
        return False
    if math.isnan(power) or math.isinf(power):
        return False
    if power < 0 or power > max_watt:
        return False
    if 0 < power < min_watt:
        return False
    return True


def is_valid_hr(hr: Any, valid_min_hr: int, valid_max_hr: int) -> bool:
    """Ellenőrzi, hogy az érték érvényes szívfrekvencia adat-e.

    Args:
        hr: Az ellenőrizendő érték.
        valid_min_hr: Minimális érvényes HR érték (bpm).
        valid_max_hr: Maximális érvényes HR érték (bpm).

    Returns:
        True, ha érvényes HR adat.
    """
    if isinstance(hr, bool):
        return False
    if not isinstance(hr, (int, float)):
        return False
    if math.isnan(hr) or math.isinf(hr):
        return False
    if hr < valid_min_hr or hr > valid_max_hr:
        return False
    return True


# ============================================================
# ZÓNA LOGIKA (higher_wins, zone_mode)
# ============================================================


def higher_wins(zone_a: int, zone_b: int) -> int:
    """A két zóna közül a nagyobbat adja vissza.

    Args:
        zone_a: Első zóna (0–3).
        zone_b: Második zóna (0–3).

    Returns:
        A nagyobb zóna szám.
    """
    return max(zone_a, zone_b)


def apply_zone_mode(
    power_zone: Optional[int],
    hr_zone: Optional[int],
    zone_mode: ZoneMode,
) -> Optional[int]:
    """A zone_mode alapján kombinálja a power és HR zónákat.

    Zóna módok:
        "power_only"  – csak a teljesítmény zóna dönt (HR figyelmen kívül)
        "hr_only"     – csak a HR zóna dönt (power figyelmen kívül)
        "higher_wins" – a kettő közül a nagyobb dönt

    Args:
        power_zone: Teljesítmény zóna (0–3), vagy None ha nem elérhető.
        hr_zone: HR zóna (0–3), vagy None ha nem elérhető.
        zone_mode: A kombinálási mód ("power_only", "hr_only", "higher_wins").

    Returns:
        A végső zóna szám (0–3), vagy None ha nincs elég adat.
    """
    if zone_mode == ZoneMode.POWER_ONLY:
        return power_zone
    if zone_mode == ZoneMode.HR_ONLY:
        return hr_zone
    # higher_wins: mindkét forrásból a nagyobb
    if power_zone is not None and hr_zone is not None:
        return higher_wins(power_zone, hr_zone)
    if power_zone is not None:
        return power_zone
    return hr_zone

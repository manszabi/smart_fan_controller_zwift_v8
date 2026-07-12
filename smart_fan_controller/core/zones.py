"""Pure zone logic – calculation, validation, zone-mode combination.

This module contains side-effect-free (pure) functions: no Qt, BLE,
ANT+ or I/O dependencies. It relies only on the standard library and the
``ZoneMode`` enum, so it is easily testable on its own.
"""
from __future__ import annotations

import math
from typing import Any

from ..config.schemas import ZoneMode


# ============================================================
# ZONE CALCULATION
# ============================================================


def calculate_power_zones(
    ftp: int,
    min_watt: int,
    max_watt: int,
    z1_pct: int,
    z2_pct: int,
) -> dict[int, tuple[int, int]]:
    """Compute the power zone boundaries.

    Args:
        ftp: Functional threshold power (W).
        min_watt: Minimum valid positive power (W).
        max_watt: Maximum valid power (W).
        z1_pct: Z1 upper bound as % of FTP.
        z2_pct: Z2 upper bound as % of FTP.

    Returns:
        Dict format: {0: (0,0), 1: (1, z1_max), 2: (z1_max+1, z2_max), 3: (z2_max+1, max_watt)}
    """
    # max(1, ...) guards the invalid z1_max=0 case (e.g. very low FTP/percent)
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
) -> dict[str, int]:
    """Compute the HR zone boundaries in bpm.

    Args:
        max_hr: Maximum heart rate (bpm).
        resting_hr: Resting heart rate (bpm); below it means Z0.
        z1_pct: Z1 upper bound as % of max_hr.
        z2_pct: Z2 upper bound as % of max_hr.

    Returns:
        Dict: {'resting': int, 'z1_max': int, 'z2_max': int}
    """
    return {
        "resting": resting_hr,
        "z1_max": int(max_hr * z1_pct / 100),
        "z2_max": int(max_hr * z2_pct / 100),
    }


def zone_for_power(power: float, zones: dict[int, tuple[int, int]]) -> int:
    """Determine the power zone (0–3) for the given watt value.

    Args:
        power: Power in watts.
        zones: Zone boundary dict (output of calculate_power_zones).

    Returns:
        Zone number (0–3).
    """
    if power <= 0:
        return 0
    # Guard against an empty or malformed zones dict (avoids ValueError)
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
    return 3  # only reachable above max_watt


def zone_for_hr(hr: int, hr_zones: dict[str, int]) -> int:
    """Determine the HR zone (0–3) for the given bpm value.

    Args:
        hr: Heart rate in bpm.
        hr_zones: HR zone boundary dict (output of calculate_hr_zones).

    Returns:
        Zone number (0–3).
    """
    if hr <= 0 or hr < hr_zones["resting"]:
        return 0
    if hr <= hr_zones["z1_max"]:
        return 1
    if hr <= hr_zones["z2_max"]:
        return 2
    return 3


# ============================================================
# INPUT DATA VALIDATION
# ============================================================


def is_valid_power(power: Any, min_watt: int, max_watt: int) -> bool:
    """Check whether the value is valid power data.

    Args:
        power: The value to check.
        min_watt: Minimum valid positive watt (values between 0 and
            min_watt are rejected).
        max_watt: Maximum valid watt.

    Returns:
        True when the value is valid power data.
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
    """Check whether the value is valid heart-rate data.

    Args:
        hr: The value to check.
        valid_min_hr: Minimum valid HR value (bpm).
        valid_max_hr: Maximum valid HR value (bpm).

    Returns:
        True when the value is valid HR data.
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
# ZONE LOGIC (higher_wins, zone_mode)
# ============================================================


def higher_wins(zone_a: int, zone_b: int) -> int:
    """Return the higher of the two zones.

    Args:
        zone_a: First zone (0–3).
        zone_b: Second zone (0–3).

    Returns:
        The higher zone number.
    """
    return max(zone_a, zone_b)


def apply_zone_mode(
    power_zone: int | None,
    hr_zone: int | None,
    zone_mode: ZoneMode,
) -> int | None:
    """Combine the power and HR zones based on zone_mode.

    Zone modes:
        "power_only"  – only the power zone decides (HR ignored)
        "hr_only"     – only the HR zone decides (power ignored)
        "higher_wins" – the higher of the two decides

    Args:
        power_zone: Power zone (0–3), or None when unavailable.
        hr_zone: HR zone (0–3), or None when unavailable.
        zone_mode: Combination mode ("power_only", "hr_only", "higher_wins").

    Returns:
        The final zone number (0–3), or None without enough data.
    """
    if zone_mode == ZoneMode.POWER_ONLY:
        return power_zone
    if zone_mode == ZoneMode.HR_ONLY:
        return hr_zone
    # higher_wins: the higher of the two sources
    if power_zone is not None and hr_zone is not None:
        return higher_wins(power_zone, hr_zone)
    if power_zone is not None:
        return power_zone
    return hr_zone

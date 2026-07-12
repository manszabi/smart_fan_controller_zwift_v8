"""Pure domain logic (zones, averaging, cooldown, state, helpers, asyncio).

This package collects the side-effect-free (Qt/BLE/IO-independent) core
functions and classes. asyncio.Lock usage enables thread-safe data
exchange between async coroutines.
"""
from __future__ import annotations

from .averaging import (
    HRAverager,
    PowerAverager,
    _RollingAverager,
    compute_average,
)
from .cooldown import CooldownController
from .helpers import generate_tone, resolve_log_dir
from .logging_setup import (
    logger,
    user_logger,
    setup_logging,
    setup_early_logging,
    flush_early_logging,
    discard_early_logging,
    is_logging_enabled,
)
from .printers import ConsolePrinter
from .state import ControllerState, UISnapshot
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
    # logging
    "logger",
    "user_logger",
    "setup_logging",
    "setup_early_logging",
    "flush_early_logging",
    "discard_early_logging",
    "is_logging_enabled",
    # printers
    "ConsolePrinter",
    # state
    "ControllerState",
    "UISnapshot",
    # helpers
    "resolve_log_dir",
    "generate_tone",
]

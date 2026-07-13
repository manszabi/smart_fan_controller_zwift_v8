#!/usr/bin/env python3
"""zwift_fan_controller.py – thin entry point.

Smart Fan Controller – modular, concurrent implementation (v8).

The actual code is organized into the ``smart_fan_controller`` package:
  - config      – type-safe settings models + loader (settings.json)
  - core        – pure domain logic (zones, averaging, cooldown, state, logging)
  - handlers    – input/output data handlers (ANT+, BLE, Zwift UDP)
  - processors  – async processing tasks (power/hr/zone/dropout)
  - ui          – PySide6 LCARS HUD
  - zwift_api   – Zwift HTTPS API polling helper process
  - controller  – FanController orchestrator
  - app         – entry point (asyncio loop + Qt HUD coordination)

This file preserves direct-run and PyInstaller entry-point
compatibility, and provides backwards-compatible re-exports for the
tests and existing code.
"""
from __future__ import annotations

# --- Backwards-compatible re-exports ---
# The tests and parts of the existing code import a few frequently used
# symbols from here (the main module). Re-exported from the submodules.
from smart_fan_controller.config import (
    BleConfig,
    DataSource,
    DatasourceConfig,
    GlobalSettingsConfig,
    HeartRateZonesConfig,
    HudConfig,
    PowerZonesConfig,
    ZoneMode,
)
from smart_fan_controller.core import (
    CooldownController,
    PowerAverager,
    apply_zone_mode,
    calculate_hr_zones,
    calculate_power_zones,
    compute_average,
    higher_wins,
    zone_for_hr,
    zone_for_power,
)
from smart_fan_controller.app import main, _PYSIDE6_AVAILABLE
from smart_fan_controller import __version__  # single source of the version

__all__ = [
    "main",
    "_PYSIDE6_AVAILABLE",
    # config
    "ZoneMode",
    "DataSource",
    "PowerZonesConfig",
    "GlobalSettingsConfig",
    "HeartRateZonesConfig",
    "BleConfig",
    "DatasourceConfig",
    "HudConfig",
    # core
    "calculate_power_zones",
    "calculate_hr_zones",
    "zone_for_power",
    "zone_for_hr",
    "higher_wins",
    "apply_zone_mode",
    "compute_average",
    "PowerAverager",
    "CooldownController",
]


if __name__ == "__main__":
    main()

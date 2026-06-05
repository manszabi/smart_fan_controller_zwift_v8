#!/usr/bin/env python3
"""swift_fan_controller_new_v8_PySide6.py – vékony belépő.

Smart Fan Controller – moduláris, párhuzamos implementáció (v8).

A tényleges kód a ``smart_fan_controller`` csomagba szerveződött:
  - config      – típusbiztos beállítás-modellek + betöltő (settings.json)
  - core        – tiszta domain-logika (zónák, átlagolás, cooldown, state, logging)
  - handlers    – be/kimeneti adatkezelők (ANT+, BLE, Zwift UDP)
  - processors  – async feldolgozó task-ok (power/hr/zone/dropout)
  - ui          – PySide6 LCARS HUD
  - zwift_api   – Zwift HTTPS API polling segédprocessz
  - controller  – FanController orchestrátor
  - app         – belépőpont (asyncio loop + Qt HUD összehangolása)

Ez a fájl megőrzi a közvetlen futtatás és a PyInstaller entry-point
kompatibilitását, és visszafelé kompatibilis re-exportokat biztosít a
tesztek és a meglévő kód számára.
"""
from __future__ import annotations

# --- Visszafelé kompatibilis re-exportok ---
# A tesztek és a meglévő kód egy része innen (a fő modulból) importál néhány
# gyakran használt szimbólumot. Ezeket a csomag almoduljaiból re-exportáljuk.
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

__version__ = "8.0.0"

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

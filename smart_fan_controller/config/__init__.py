"""Beállítás-kezelő al-package.

Egységes belépési pont a beállítás-modellekhez (``schemas``) és a betöltő /
származtatott logikához (``loader``).
"""
from __future__ import annotations

from .schemas import (
    DEFAULT_SETTINGS,
    VALID_DATA_SOURCES,
    VALID_ZONE_MODES,
    BleConfig,
    DataSource,
    DatasourceConfig,
    GlobalSettingsConfig,
    HeartRateZonesConfig,
    HudConfig,
    PowerZonesConfig,
    ZoneMode,
    ZwiftApiConfig,
)
from .loader import (
    get_effective_zone_mode,
    load_settings,
    save_hud_settings_only,
)

__all__ = [
    # Enumok
    "DataSource",
    "ZoneMode",
    "VALID_DATA_SOURCES",
    "VALID_ZONE_MODES",
    # Beállítás dataclass-ek
    "GlobalSettingsConfig",
    "PowerZonesConfig",
    "HeartRateZonesConfig",
    "BleConfig",
    "DatasourceConfig",
    "HudConfig",
    "ZwiftApiConfig",
    # Betöltés és lekérdezések
    "DEFAULT_SETTINGS",
    "load_settings",
    "get_effective_zone_mode",
    "save_hud_settings_only",
]

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
    _from_dict_int,
)
from .loader import (
    get_effective_zone_mode,
    load_settings,
    _resolve_buffer_settings,
    _save_default_settings,
    _settings_to_serializable,
)

__all__ = [
    "DEFAULT_SETTINGS",
    "VALID_DATA_SOURCES",
    "VALID_ZONE_MODES",
    "BleConfig",
    "DataSource",
    "DatasourceConfig",
    "GlobalSettingsConfig",
    "HeartRateZonesConfig",
    "HudConfig",
    "PowerZonesConfig",
    "ZoneMode",
    "get_effective_zone_mode",
    "load_settings",
]

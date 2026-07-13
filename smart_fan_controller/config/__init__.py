"""Settings sub-package.

Unified entry point for the settings models (``schemas``) and the
loading / derived logic (``loader``).
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
    # Enums
    "DataSource",
    "ZoneMode",
    "VALID_DATA_SOURCES",
    "VALID_ZONE_MODES",
    # Settings dataclasses
    "GlobalSettingsConfig",
    "PowerZonesConfig",
    "HeartRateZonesConfig",
    "BleConfig",
    "DatasourceConfig",
    "HudConfig",
    "ZwiftApiConfig",
    # Loading and queries
    "DEFAULT_SETTINGS",
    "load_settings",
    "get_effective_zone_mode",
    "save_hud_settings_only",
]

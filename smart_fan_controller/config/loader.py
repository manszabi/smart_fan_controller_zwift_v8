"""Beállítások betöltése, mentése és származtatott lekérdezések.

Ez a modul felel a ``settings.json`` beolvasásáért és validálásáért
(``load_settings``), az alapértelmezett fájl létrehozásáért, valamint néhány
származtatott segédfüggvényért (``get_effective_zone_mode``,
``_resolve_buffer_settings``), amelyek a betöltött beállítások dict-jén
dolgoznak.

A beállítás-modellek (dataclass-ek, enumok) a testvér ``schemas`` modulban
találhatók.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import logging
from typing import Any, Dict

from .schemas import (
    DEFAULT_SETTINGS,
    DataSource,
    DatasourceConfig,
    GlobalSettingsConfig,
    HeartRateZonesConfig,
    PowerZonesConfig,
    BleConfig,
    HudConfig,
    ZoneMode,
)

# A felhasználói üzeneteket a "user" nevű logger kezeli; lásd schemas.py.
user_logger = logging.getLogger("user")


# ============================================================
# BEÁLLÍTÁSOK BETÖLTÉSE
# ============================================================


def load_settings(settings_file: str = "settings.json") -> Dict[str, Any]:
    """Betölti és validálja a JSON beállítási fájlt.

    Alapértelmezett értékekből indul ki (DEFAULT_SETTINGS), majd felülírja
    az érvényes, fájlból betöltött értékekkel. Hibás mezőnél az alapértelmezett
    marad érvényben (figyelmeztetéssel).

    Ha a fájl nem létezik, automatikusan létrehozza az alapértelmezettekkel.

    Args:
        settings_file: A JSON beállítások fájl elérési útja.

    Returns:
        Validált beállítások dict-je.
    """
    settings = copy.deepcopy(DEFAULT_SETTINGS)

    try:
        with open(settings_file, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except FileNotFoundError:
        user_logger.warning(
            f"⚠ '{settings_file}' nem található, alapértelmezett beállítások használata."
        )
        _save_default_settings(settings_file, settings)
        return settings
    except (json.JSONDecodeError, OSError) as exc:
        user_logger.warning(f"⚠ '{settings_file}' beolvasási hiba: {exc}. Alapértelmezés használata.")
        return settings

    # --- Szekciók betöltése dataclass from_dict()-tel ---
    if isinstance(loaded.get("global_settings"), dict):
        settings["global_settings"] = GlobalSettingsConfig.from_dict(loaded["global_settings"])
    if isinstance(loaded.get("power_zones"), dict):
        settings["power_zones"] = PowerZonesConfig.from_dict(loaded["power_zones"])
    if isinstance(loaded.get("heart_rate_zones"), dict):
        settings["heart_rate_zones"] = HeartRateZonesConfig.from_dict(loaded["heart_rate_zones"])
    if isinstance(loaded.get("ble"), dict):
        settings["ble"] = BleConfig.from_dict(loaded["ble"])
    if isinstance(loaded.get("datasource"), dict):
        settings["datasource"] = DatasourceConfig.from_dict(loaded["datasource"])
    if isinstance(loaded.get("hud"), dict):
        settings["hud"] = HudConfig.from_dict(loaded["hud"])

    # --- Kereszt-validáció: zone_mode + null forrás ---
    try:
        ds_cfg: DatasourceConfig = settings["datasource"]
        hrz_cfg: HeartRateZonesConfig = settings["heart_rate_zones"]
        hr_on = hrz_cfg.enabled
        zm = hrz_cfg.zone_mode if hr_on else ZoneMode.POWER_ONLY
        ps = ds_cfg.power_source
        hs = ds_cfg.hr_source

        if zm == ZoneMode.HIGHER_WINS:
            if ps is None and hs is None:
                user_logger.warning(
                    "⚠ zone_mode 'higher_wins', de mindkét forrás null – "
                    "nincs adat a zóna meghatározásához!"
                )
            elif ps is None:
                user_logger.warning(
                    "⚠ zone_mode 'higher_wins', de power_source null – "
                    "csak HR alapján fog dönteni (mint hr_only)."
                )
            elif hs is None:
                user_logger.warning(
                    "⚠ zone_mode 'higher_wins', de hr_source null – "
                    "csak power alapján fog dönteni (mint power_only)."
                )
        elif zm == ZoneMode.POWER_ONLY and ps is None:
            user_logger.warning(
                "⚠ zone_mode 'power_only', de power_source null – "
                "nincs adat a zóna meghatározásához!"
            )
        elif zm == ZoneMode.HR_ONLY and hs is None:
            user_logger.warning(
                "⚠ zone_mode 'hr_only', de hr_source null – "
                "nincs adat a zóna meghatározásához!"
            )
    except Exception as exc:
        user_logger.warning(f"⚠ zone_mode/null forrás kereszt-validáció sikertelen: {exc}")

    return settings


def _settings_to_serializable(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Settings dict-et JSON-serializálható formára alakít (dataclass → dict)."""
    out = {}
    for k, v in settings.items():
        if dataclasses.is_dataclass(v) and not isinstance(v, type):
            out[k] = v.to_dict() if hasattr(v, "to_dict") else dataclasses.asdict(v)
        else:
            out[k] = v
    return out


def _save_default_settings(path: str, settings: Dict[str, Any]) -> None:
    """Létrehozza az alapértelmezett settings.json fájlt."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_settings_to_serializable(settings), f, indent=2, ensure_ascii=False)
        user_logger.info(f"✓ Alapértelmezett '{path}' létrehozva.")
    except OSError as exc:
        user_logger.warning(f"✗ Nem sikerült létrehozni a '{path}' fájlt: {exc}")


# ============================================================
# SZÁRMAZTATOTT LEKÉRDEZÉSEK
# ============================================================


def get_effective_zone_mode(settings: Dict[str, Any]) -> ZoneMode:
    """Meghatározza az effektív zóna módot a beállítások alapján.

    Ha a HR nincs engedélyezve (enabled=False), mindig POWER_ONLY-t ad vissza,
    függetlenül a zone_mode beállítástól.

    Args:
        settings: Betöltött beállítások dict-je.

    Returns:
        Az effektív ZoneMode.
    """
    hrz: HeartRateZonesConfig = settings["heart_rate_zones"]
    if not hrz.enabled:
        return ZoneMode.POWER_ONLY
    return hrz.zone_mode


def _resolve_buffer_settings(settings: Dict[str, Any], role: str) -> Dict[str, Any]:
    """
    Visszaadja a megfelelő buffer/dropout paramétereket a megadott szerephez.

    A role ("power" vagy "hr") alapján meghatározza az aktív adatforrást
    (datasource.power_source ill. datasource.hr_source mezőkből), majd
    visszaadja a forrás-specifikus buffer beállításokat.
    Fallback: globális buffer_seconds / minimum_samples / buffer_rate_hz / dropout_timeout.

    Args:
        settings: Betöltött beállítások dict-je.
        role:     "power" – a power_source alapján,
                  "hr"    – a hr_source alapján.
    Returns:
        Dict: buffer_seconds, minimum_samples, buffer_rate_hz, dropout_timeout
    """
    ds: DatasourceConfig = settings["datasource"]
    source = ds.power_source if role == "power" else ds.hr_source

    if source is None:
        # Null forrás: globális fallback értékek
        gs: GlobalSettingsConfig = settings["global_settings"]
        return {
            "buffer_seconds": gs.buffer_seconds,
            "minimum_samples": gs.minimum_samples,
            "buffer_rate_hz": gs.buffer_rate_hz,
            "dropout_timeout": gs.dropout_timeout,
        }

    if source == DataSource.BLE:
        prefix = "BLE"
    elif source == DataSource.ANTPLUS:
        prefix = "ANT"
    else:  # zwiftudp
        prefix = "zwiftUDP"

    gs: GlobalSettingsConfig = settings["global_settings"]
    return {
        "buffer_seconds": getattr(ds, f"{prefix}_buffer_seconds", gs.buffer_seconds),
        "minimum_samples": getattr(ds, f"{prefix}_minimum_samples", gs.minimum_samples),
        "buffer_rate_hz": getattr(ds, f"{prefix}_buffer_rate_hz", gs.buffer_rate_hz),
        "dropout_timeout": getattr(ds, f"{prefix}_dropout_timeout", gs.dropout_timeout),
    }

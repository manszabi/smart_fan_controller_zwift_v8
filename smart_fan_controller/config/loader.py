"""Beállítások betöltése, mentése és származtatott lekérdezések.

Ez a modul felel a ``settings.json`` beolvasásáért és validálásáért
(``load_settings``), valamint néhány származtatott segédfüggvényért
(``get_effective_zone_mode``, ``_resolve_buffer_settings``).

**Default settings:** a ``settings.default.json`` (verziókövetett sablonfájl)
tartalmazza az összes mező alapértelmezett értékét. Ha a felhasználó
``settings.json`` még nem létezik, a program automatikusan mássolja a
``settings.default.json``-t, így a felhasználó egyből a default-okból indulhat.

A beállítás-modellek (dataclass-ek, enumok) a testvér ``schemas`` modulban
találhatók.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import logging
import os
import shutil
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

    Logika:
      1. Ha ``settings_file`` nem létezik, de ``settings.default.json``
         (a szokásos helyén az aktuális könyvtárban) van, mássolja azt
         az ``settings_file`` helyére.
      2. Beolvassa az ``settings_file``-t, az értékeket validálja a dataclass-ek
         ``from_dict()`` metódusaival. Hibás mező → az alapértelmezett marad (warning).
      3. Ha még mindig nincs ``settings_file``, fallback a ``DEFAULT_SETTINGS``
         hardcoded dict-re.

    Args:
        settings_file: A JSON beállítások fájl elérési útja.

    Returns:
        Validált beállítások dict-je.
    """
    settings = copy.deepcopy(DEFAULT_SETTINGS)

    # Ha nincs settings.json, de van settings.default.json → másold
    _ensure_default_settings_file(settings_file)

    try:
        with open(settings_file, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except FileNotFoundError:
        user_logger.warning(
            f"⚠ '{settings_file}' nem található, alapértelmezett beállítások használata."
        )
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


# A versiókövetett default sablonfájl a config package-en belül van
# (package data) – a kódhoz tartozik, ami használja.
DEFAULT_SETTINGS_FILENAME = "settings.default.json"
DEFAULT_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), DEFAULT_SETTINGS_FILENAME)


def _ensure_default_settings_file(settings_path: str) -> None:
    """Ha ``settings_path`` nem létezik, de a ``settings.default.json`` sablon
    elérhető, mássolja azt ``settings_path`` helyére.

    A ``settings.default.json`` keresési sorrendje:
      1. A felhasználó aktuális munkakönyvtára (CWD) – ide tehet saját sablont.
      2. A config package data (``smart_fan_controller/config/settings.default.json``)
         – a verziókövetett, beépített alapértelmezés.

    Akkor hasznos, ha a felhasználó még nem készített ``settings.json`` fájlt,
    de szeretne egy érvényes alapértelmezésből indulni.
    """
    if os.path.exists(settings_path):
        # Már van settings.json → nincs mit csinálni
        return

    default_candidates = [
        os.path.join(os.getcwd(), DEFAULT_SETTINGS_FILENAME),  # CWD-beli felülíró sablon
        DEFAULT_SETTINGS_PATH,                                  # beépített package data
    ]

    for default_path in default_candidates:
        if os.path.exists(default_path) and os.path.abspath(default_path) != os.path.abspath(settings_path):
            try:
                shutil.copy2(default_path, settings_path)
                user_logger.info(
                    f"✓ '{default_path}' → '{settings_path}' másolva. "
                    f"Szerkeszd ezt a fájlt az igényeidnek megfelelően."
                )
                return
            except OSError as exc:
                user_logger.warning(f"⚠ Nem sikerült másolni '{default_path}' → '{settings_path}': {exc}")
                return

    # Ha nincs settings.default.json, nincs mit tennünk
    # (a fallback a DEFAULT_SETTINGS hardcoded dict lesz)


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

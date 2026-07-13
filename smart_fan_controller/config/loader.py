"""Settings loading, saving and derived queries.

This module is responsible for reading and validating ``settings.json``
(``load_settings``) plus a few derived helpers
(``get_effective_zone_mode``, ``_resolve_buffer_settings``).

**Default settings:** ``settings.default.json`` (a version-controlled
template file) contains the default value of every field. When the
user's ``settings.json`` does not exist yet, the program copies
``settings.default.json`` automatically, so the user starts from the
defaults right away.

The settings models (dataclasses, enums) live in the sibling ``schemas``
module.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import logging
import os
import shutil
from typing import Any

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
    ZwiftApiConfig,
)

# User-facing messages go through the logger named "user"; see schemas.py.
user_logger = logging.getLogger("user")

# Valid top-level sections of settings.json ("ble" is a deprecated alias)
_KNOWN_SECTIONS = frozenset((
    "global_settings", "power_zones", "heart_rate_zones",
    "ble_fan", "ble", "datasource", "hud", "zwift_api",
))


# ============================================================
# SETTINGS LOADING
# ============================================================


def load_settings(settings_file: str = "settings.json") -> dict[str, Any]:
    """Load and validate the JSON settings file.

    Logic:
      1. When ``settings_file`` does not exist but ``settings.default.json``
         (in its usual place in the current directory) does, copy it to
         ``settings_file``.
      2. Read ``settings_file`` and validate the values with the
         dataclasses' ``from_dict()`` methods. Invalid field → the
         default remains (with a warning).
      3. When there is still no ``settings_file``, fall back to the
         hardcoded ``DEFAULT_SETTINGS`` dict.

    Args:
        settings_file: Path of the JSON settings file.

    Returns:
        Dict of validated settings.
    """
    settings = copy.deepcopy(DEFAULT_SETTINGS)

    # No settings.json but settings.default.json exists → copy it
    _ensure_default_settings_file(settings_file)

    try:
        with open(settings_file, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except FileNotFoundError:
        user_logger.warning(
            f"⚠ '{settings_file}' nem található, alapértelmezett beállítások használata."
        )
        return settings
    except json.JSONDecodeError as exc:
        # Syntax error: the file is unreadable → full defaults. Before any
        # save the broken file is set aside as '.incorrect' so the manual
        # edits (made unreadable by a small typo) are not lost when the
        # program later overwrites settings.json with the default values.
        backup = _backup_incorrect_settings(settings_file)
        msg = f"⚠ '{settings_file}' JSON szintaxis hiba: {exc}. Alapértelmezés használata."
        if backup:
            msg += (
                f" A hibás fájl elmentve ide: '{backup}' – javítsd ki ott a hibát "
                f"(lásd a fenti sor/oszlop infót), majd nevezd vissza 'settings.json'-ra."
            )
        user_logger.warning(msg)
        return settings
    except OSError as exc:
        user_logger.warning(f"⚠ '{settings_file}' beolvasási hiba: {exc}. Alapértelmezés használata.")
        return settings

    # Valid JSON but not an object (e.g. a list or string at the top) →
    # treated the same as a syntax error: backup + full defaults.
    if not isinstance(loaded, dict):
        backup = _backup_incorrect_settings(settings_file)
        msg = (
            f"⚠ '{settings_file}' tartalma nem beállítás-objektum "
            f"(hanem {type(loaded).__name__}). Alapértelmezés használata."
        )
        if backup:
            msg += f" A fájl elmentve ide: '{backup}'."
        user_logger.warning(msg)
        return settings

    # Report mistyped / unknown section names (e.g. "power_zone" instead
    # of "power_zones" would be lost silently)
    unknown_sections = set(loaded) - _KNOWN_SECTIONS
    if unknown_sections:
        user_logger.warning(
            f"⚠ Ismeretlen szekció(k) a '{settings_file}' fájlban: "
            f"{', '.join(sorted(unknown_sections))} – figyelmen kívül hagyva."
        )

    # --- Load the sections via the dataclass from_dict() methods ---
    if isinstance(loaded.get("global_settings"), dict):
        settings["global_settings"] = GlobalSettingsConfig.from_dict(loaded["global_settings"])
    if isinstance(loaded.get("power_zones"), dict):
        settings["power_zones"] = PowerZonesConfig.from_dict(loaded["power_zones"])
    if isinstance(loaded.get("heart_rate_zones"), dict):
        settings["heart_rate_zones"] = HeartRateZonesConfig.from_dict(loaded["heart_rate_zones"])
    # "ble_fan": the BLE fan output section. Backwards compatibility: when
    # "ble_fan" is missing but the legacy "ble" key is present, use that
    # (with a deprecation warning) – old settings.json files keep working.
    if isinstance(loaded.get("ble_fan"), dict):
        settings["ble_fan"] = BleConfig.from_dict(loaded["ble_fan"])
    elif isinstance(loaded.get("ble"), dict):
        user_logger.warning(
            "⚠ A 'ble' szekció elavult – nevezd át 'ble_fan'-ra a settings.json-ban. "
            "Most még a régi 'ble' kulcsot használom."
        )
        settings["ble_fan"] = BleConfig.from_dict(loaded["ble"])
    if isinstance(loaded.get("datasource"), dict):
        settings["datasource"] = DatasourceConfig.from_dict(loaded["datasource"])
    if isinstance(loaded.get("hud"), dict):
        settings["hud"] = HudConfig.from_dict(loaded["hud"])
    if isinstance(loaded.get("zwift_api"), dict):
        settings["zwift_api"] = ZwiftApiConfig.from_dict(loaded["zwift_api"])

    # --- Cross-validation: zone_mode + null source ---
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


def _settings_to_serializable(settings: dict[str, Any]) -> dict[str, Any]:
    """Convert a settings dict to a JSON-serializable form (dataclass → dict)."""
    out = {}
    for k, v in settings.items():
        if dataclasses.is_dataclass(v) and not isinstance(v, type):
            out[k] = v.to_dict() if hasattr(v, "to_dict") else dataclasses.asdict(v)
        else:
            out[k] = v
    return out


# The version-controlled default template lives inside the config package
# (package data) – it belongs with the code that uses it.
DEFAULT_SETTINGS_FILENAME = "settings.default.json"
DEFAULT_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), DEFAULT_SETTINGS_FILENAME)


def _ensure_default_settings_file(settings_path: str) -> None:
    """When ``settings_path`` does not exist but the ``settings.default.json``
    template is available, copy it to ``settings_path``.

    Search order for ``settings.default.json``:
      1. The user's current working directory (CWD) – a custom template
         can be placed here.
      2. The config package data
         (``smart_fan_controller/config/settings.default.json``) – the
         version-controlled, built-in default.

    Useful when the user has not created a ``settings.json`` yet but
    wants to start from a valid default.
    """
    if os.path.exists(settings_path):
        # settings.json already exists → nothing to do
        return

    default_candidates = [
        os.path.join(os.getcwd(), DEFAULT_SETTINGS_FILENAME),  # CWD override template
        DEFAULT_SETTINGS_PATH,                                  # built-in package data
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

    # Without a settings.default.json there is nothing to do
    # (the fallback is the hardcoded DEFAULT_SETTINGS dict)


def _backup_incorrect_settings(settings_path: str) -> str | None:
    """Create a backup copy of the syntactically broken ``settings_path``.

    The copy is named ``<settings_path>.incorrect`` (e.g.
    ``settings.json.incorrect``). This preserves the user's manual edits
    even when a small typo (missing comma, bracket) made the file
    unreadable and the program would later overwrite the original
    ``settings.json`` with the defaults.

    An existing ``.incorrect`` file is overwritten (always keeps the most
    recent broken version).

    Returns:
        The backup path on success, ``None`` otherwise.
    """
    backup_path = settings_path + ".incorrect"
    try:
        shutil.copy2(settings_path, backup_path)
        return backup_path
    except OSError as exc:
        user_logger.warning(
            f"⚠ Nem sikerült biztonsági másolatot készíteni a hibás "
            f"'{settings_path}' fájlról: {exc}"
        )
        return None


def _write_json_atomic(path: str, data: dict[str, Any]) -> None:
    """Write JSON atomically: temp file + os.replace (atomic on Windows too).

    With a direct overwrite, dying mid-write (power loss, kill) would
    leave a truncated settings.json behind – all user settings lost.
    This way either the old or the new full content survives."""
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    finally:
        # Leave no stray temp file behind after a failed write
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def save_zwift_api_credentials(settings_file: str, username: str, password: str) -> bool:
    """Update only the username/password fields of the "zwift_api" section.

    Used by the zwift_api helper process when it prompted for the
    credentials interactively (the other fields – poll_interval,
    separate_window – and the other sections are left untouched). The
    "zwift_api" section is created when it does not exist yet.

    Args:
        settings_file: Path of the settings.json file.
        username: The Zwift username to save.
        password: The Zwift password to save (careful: plaintext).

    Returns:
        True on success, False on error.
    """
    try:
        with open(settings_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        user_logger.warning(f"⚠ Zwift API adatok mentési hiba (olvasás): {exc}")
        return False

    if not isinstance(data, dict):
        return False
    section = data.get("zwift_api")
    if not isinstance(section, dict):
        section = {}
    section["username"] = username
    section["password"] = password
    data["zwift_api"] = section

    try:
        _write_json_atomic(settings_file, data)
        return True
    except OSError as exc:
        user_logger.warning(f"⚠ Zwift API adatok mentési hiba (írás): {exc}")
        return False


def save_hud_settings_only(settings_file: str, hud_config: HudConfig) -> bool:
    """Update only the "hud" section of settings.json (no full overwrite).

    Ensures the HUD settings (opacity, sound_volume, window_geometry) are
    only persisted when ``save_hud_settings=True``. This way the user's
    manual edits (ftp, device_name, etc.) are never clobbered by a HUD
    position save.

    Args:
        settings_file: Path of the settings.json file.
        hud_config: The HudConfig object currently in use.

    Returns:
        True on success, False on error.
    """
    if not hud_config.save_hud_settings:
        # The user disabled HUD saving
        return False

    try:
        with open(settings_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        user_logger.warning(f"⚠ HUD beállítások mentési hiba (olvasás): {exc}")
        return False

    # Update only the "hud" section
    data["hud"] = hud_config.to_dict()

    try:
        _write_json_atomic(settings_file, data)
        return True
    except OSError as exc:
        user_logger.warning(f"⚠ HUD beállítások mentési hiba (írás): {exc}")
        return False


# ============================================================
# DERIVED QUERIES
# ============================================================


def get_effective_zone_mode(settings: dict[str, Any]) -> ZoneMode:
    """Determine the effective zone mode from the settings.

    When HR is disabled (enabled=False) it always returns POWER_ONLY,
    regardless of the zone_mode setting.

    Args:
        settings: Dict of loaded settings.

    Returns:
        The effective ZoneMode.
    """
    hrz: HeartRateZonesConfig = settings["heart_rate_zones"]
    if not hrz.enabled:
        return ZoneMode.POWER_ONLY
    return hrz.zone_mode


def _resolve_buffer_settings(settings: dict[str, Any], role: str) -> dict[str, Any]:
    """
    Return the buffer/dropout parameters appropriate for the given role.

    Based on the role ("power" or "hr") it determines the active data
    source (from datasource.power_source / datasource.hr_source) and
    returns the source-specific buffer settings.
    Fallback: global buffer_seconds / minimum_samples / buffer_rate_hz /
    dropout_timeout.

    Args:
        settings: Dict of loaded settings.
        role:     "power" – based on power_source,
                  "hr"    – based on hr_source.
    Returns:
        Dict: buffer_seconds, minimum_samples, buffer_rate_hz, dropout_timeout
    """
    ds: DatasourceConfig = settings["datasource"]
    source = ds.power_source if role == "power" else ds.hr_source

    if source is None:
        # Null source: global fallback values
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

#!/usr/bin/env python3
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownParameterType=false
# pyright: reportUnknownArgumentType=false
from __future__ import annotations

"""
swift_fan_controller_new_v8.py

Smart Fan Controller – moduláris, párhuzamos implementáció.

Minden fő funkció különálló aszinkron feladatban/szálban fut:
  - ANT+ bemenő adatkezelés (HR, power)        → ANTPlusInputHandler (daemon szál + asyncio bridge)
  - BLE ventilátor kimenő vezérlés              → BLEFanOutputController (asyncio korrutin)
  - BLE bemenő adatok (HR, power)               → BLEPowerInputHandler, BLEHRInputHandler (asyncio)
  - Zwift API polling bejövő adatkezelés          → ZwiftUDPInputHandler (asyncio DatagramProtocol)
  - Power átlag számítás                        → PowerAverager + power_processor_task
  - HR átlag számítás                           → HRAverager + hr_processor_task
  - higher_wins logika                          → apply_zone_mode() (tiszta függvény)
  - Cooldown logika                             → CooldownController (állapotgép)
  - Zona számítás                               → zone_for_power(), zone_for_hr() (tiszta függvények)
  - Zona elküldése                              → zone_controller_task + send_zone()
  - Konzolos kiírás                             → ConsolePrinter (throttle-olt)

Architektúra:
  - Egyetlen asyncio event loop a fő vezérlési logikához
  - Saját daemon szál az ANT+ számára (blokkoló könyvtár)
  - asyncio.Queue a komponensek közötti adatátvitelhez
  - asyncio.Event a zóna újraszámítás jelzéséhez
  - asyncio.Lock a megosztott állapot védelméhez
  - Tiszta (mellékhatás-mentes) függvények a logikához (jól tesztelhetők)

Verziószám: 8.0.0
"""

import abc
import asyncio
import copy
import dataclasses
import enum
import io
import json
import logging
import math
import platform as _platform
import signal
import threading
import time
import atexit
import struct
import subprocess
import sys
import os
import tempfile
import wave

# COM inicializálás threading modellje – APARTMENTTHREADED kell a Qt-nek (OLE/DnD),
# és ezt a pywinauto importálása ELŐTT kell beállítani, különben COM conflict lesz.
if not hasattr(sys, "coinit_flags"):
    sys.coinit_flags = 2  # type: ignore[attr-defined]  # COINIT_APARTMENTTHREADED

from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, cast

# --- Enum-ok a magic string-ek kiváltásához ---
# str öröklés: JSON-ból jövő string értékekkel is kompatibilis (==)
class DataSource(str, enum.Enum):
    ANTPLUS = "antplus"
    BLE = "ble"
    ZWIFTUDP = "zwiftudp"

class ZoneMode(str, enum.Enum):
    POWER_ONLY = "power_only"
    HR_ONLY = "hr_only"
    HIGHER_WINS = "higher_wins"

VALID_DATA_SOURCES: tuple[DataSource, ...] = tuple(DataSource)
VALID_ZONE_MODES: tuple[ZoneMode, ...] = tuple(ZoneMode)



Node: Any = None
ANTPLUS_NETWORK_KEY: Any = None  # type: ignore[reportConstantRedefinition]
PowerMeter: Any = None
PowerData: Any = None
HeartRate: Any = None
HeartRateData: Any = None

BleakClient: Any = None
BleakScanner: Any = None

# --- Külső könyvtárak (opcionális importok – a program importálható marad teszteléshez) ---
_ANTPLUS_AVAILABLE: bool = False
_BLEAK_AVAILABLE: bool = False
try:
    from openant.easy.node import Node  # type: ignore[import-untyped]
    from openant.devices import ANTPLUS_NETWORK_KEY  # type: ignore[import-untyped, assignment]
    from openant.devices.power_meter import PowerMeter, PowerData  # type: ignore[import-untyped]
    from openant.devices.heart_rate import HeartRate, HeartRateData  # type: ignore[import-untyped]

    _ANTPLUS_AVAILABLE = True  # type: ignore[misc]
except ImportError:
    pass

try:
    from bleak import BleakClient, BleakScanner  # type: ignore[import-untyped]

    _BLEAK_AVAILABLE = True  # type: ignore[misc]
except ImportError:
    pass

_PYWINAUTO_AVAILABLE: bool = False
try:
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Apply externally defined coinit_flags")
        from pywinauto import Application as WinAutoApp  # type: ignore[import-untyped]

    _PYWINAUTO_AVAILABLE = True  # type: ignore[misc]
except ImportError:
    WinAutoApp = None  # type: ignore[assignment]

_PYSIDE6_AVAILABLE: bool = False

try:
    from PySide6.QtWidgets import (  # type: ignore[import-untyped]
        QApplication, QWidget, QLabel, QHBoxLayout, QVBoxLayout,
        QSlider, QMenu, QFrame,
    )
    from PySide6.QtCore import Qt, QTimer, QPoint, QSize, QRectF, QUrl  # type: ignore[import-untyped]
    from PySide6.QtGui import (  # type: ignore[import-untyped]
        QColor, QPainter, QBrush, QFont, QFontDatabase,
        QPainterPath, QMouseEvent,
    )
    from PySide6.QtMultimedia import QSoundEffect  # type: ignore[import-untyped]

    _PYSIDE6_AVAILABLE = True  # type: ignore[misc]
except ImportError:
    QApplication: Any = None
    QWidget: Any = None
    QLabel: Any = None
    QHBoxLayout: Any = None
    QVBoxLayout: Any = None
    QSlider: Any = None
    QMenu: Any = None
    QFrame: Any = None
    Qt: Any = None
    QTimer: Any = None
    QPoint: Any = None
    QSize: Any = None
    QRectF: Any = None
    QUrl: Any = None
    QColor: Any = None
    QPainter: Any = None
    QBrush: Any = None
    QFont: Any = None
    QFontDatabase: Any = None
    QPainterPath: Any = None
    QMouseEvent: Any = None
    QSoundEffect: Any = None

if TYPE_CHECKING:
    from PySide6.QtWidgets import (
        QApplication, QWidget, QLabel, QHBoxLayout, QVBoxLayout,
        QSlider, QMenu, QFrame,
    )
    from PySide6.QtCore import Qt, QTimer, QPoint, QSize, QRectF, QUrl
    from PySide6.QtGui import (
        QColor, QPainter, QBrush, QFont, QFontDatabase,
        QPainterPath, QMouseEvent,
    )
    from PySide6.QtMultimedia import QSoundEffect

__version__ = "8.0.0"

logger = logging.getLogger("swift_fan_controller_new")
user_logger = logging.getLogger("user")


def _resolve_log_dir(log_directory: Optional[str]) -> str:
    """Log könyvtár meghatározása és validálása.

    Ha ``log_directory`` None, üres, vagy nem létezik / nem hozható létre,
    a program indítási könyvtárát (CWD) használja fallback-ként.

    Returns:
        Érvényes, írható könyvtár elérési útja.
    """
    default_dir = os.path.dirname(os.path.abspath(__file__))

    if not log_directory:
        return default_dir

    log_directory = os.path.expanduser(log_directory)
    log_directory = os.path.abspath(log_directory)

    try:
        os.makedirs(log_directory, exist_ok=True)
        # Írhatóság tesztelése
        test_file = os.path.join(log_directory, ".log_write_test")
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        return log_directory
    except OSError:
        # Nem sikerült létrehozni / írni – fallback
        user_logger.warning(
            f"⚠ log_directory nem elérhető: '{log_directory}', "
            f"alapértelmezett használata: '{default_dir}'"
        )
        return default_dir


# Modul-szintű változó: a feloldott log könyvtár (_setup_logging állítja be)
_log_dir: str = os.path.dirname(os.path.abspath(__file__))


def _setup_logging(log_directory: Optional[str] = None) -> None:
    """Logging konfiguráció: konzol + rotált fájl (500 KB max).

    Két logger:
      - ``user_logger``: Felhasználói üzenetek (konzolra + fájlba).
        Konzolra tiszta formátum (csak az üzenet), fájlba időbélyeggel.
      - ``logger``: Belső debug/info logok (fájlba mindig, konzolra WARNING+ felett).

    A log fájlok a ``log_directory``-ba kerülnek (ha érvényes), különben
    a program indítási könyvtárába.

    Többszöri hívás biztonságos: a korábbi handler-eket eltávolítja.

    Args:
        log_directory: Log fájlok könyvtára (None = alapértelmezett).
    """
    from logging.handlers import RotatingFileHandler

    global _log_dir
    _log_dir = _resolve_log_dir(log_directory)
    log_file = os.path.join(_log_dir, "smart_fan_controller.log")

    file_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_file, maxBytes=500 * 1024, backupCount=2, encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_fmt)

    # ── user_logger: felhasználói üzenetek ──
    ul = logging.getLogger("user")
    ul.handlers.clear()  # Korábbi handler-ek törlése (újrahívás esetén)
    ul.setLevel(logging.DEBUG)
    ul.propagate = False

    console_user = logging.StreamHandler(sys.stdout)
    console_user.setLevel(logging.DEBUG)
    console_user.setFormatter(logging.Formatter("%(message)s"))
    ul.addHandler(console_user)
    ul.addHandler(file_handler)

    # ── logger: belső logok ──
    il = logging.getLogger("swift_fan_controller_new")
    il.handlers.clear()
    il.setLevel(logging.DEBUG)
    il.propagate = False

    console_internal = logging.StreamHandler(sys.stderr)
    console_internal.setLevel(logging.WARNING)
    console_internal.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S",
    ))
    il.addHandler(console_internal)
    il.addHandler(file_handler)

    # Zajos külső könyvtárak elnémítása
    logging.getLogger("bleak").setLevel(logging.CRITICAL)
    logging.getLogger("openant").setLevel(logging.CRITICAL)


# ============================================================
# TÍPUSBIZTOS BEÁLLÍTÁS MODELLEK
# ============================================================


@dataclasses.dataclass
class PowerZonesConfig:
    """Teljesítmény zóna beállítások – típusbiztos, validált.

    Validáció a __post_init__-ben:
      - min_watt < max_watt (felcserélés ha szükséges)
      - z1_max_percent < z2_max_percent (rendezés ha szükséges)
    """

    ftp: int = 200
    min_watt: int = 0
    max_watt: int = 1000
    z1_max_percent: int = 60
    z2_max_percent: int = 89
    zero_power_immediate: bool = False

    def __post_init__(self) -> None:
        # min_watt / max_watt kereszt-validáció
        if self.min_watt > self.max_watt:
            user_logger.warning(
                f"⚠ Érvénytelen watt tartomány (min_watt={self.min_watt}, max_watt={self.max_watt}). "
                f"Feltételezett felcserélés, értékek megfordítva."
            )
            self.min_watt, self.max_watt = self.max_watt, self.min_watt
        elif self.min_watt == self.max_watt:
            user_logger.warning(
                f"⚠ min_watt és max_watt azonos értékű ({self.min_watt}). "
                f"max_watt {self.min_watt + 1}-re állítva."
            )
            self.max_watt = self.min_watt + 1

        # z1/z2 százalék kereszt-validáció
        if self.z1_max_percent >= self.z2_max_percent:
            low, high = min(self.z1_max_percent, self.z2_max_percent), max(self.z1_max_percent, self.z2_max_percent)
            if low == high:
                if low >= 100:
                    low, high = 99, 100
                else:
                    high = low + 1
            user_logger.warning(
                f"⚠ Érvénytelen power zóna százalékok (z1={self.z1_max_percent}, z2={self.z2_max_percent}). "
                f"Javítva: z1={low}, z2={high}."
            )
            self.z1_max_percent, self.z2_max_percent = low, high

    @classmethod
    def from_dict(cls, raw: dict[str, Any], defaults: "PowerZonesConfig | None" = None) -> "PowerZonesConfig":
        """Dict-ből (JSON) hoz létre validált PowerZonesConfig példányt.

        Érvénytelen értékeket figyelmen kívül hagyja (az alapértelmezés marad).

        Args:
            raw: A JSON-ból betöltött dict.
            defaults: Alapértelmezett értékek (None = osztály default-ok).
        """
        d = defaults or cls()
        ftp = d.ftp
        min_watt = d.min_watt
        max_watt = d.max_watt
        z1 = d.z1_max_percent
        z2 = d.z2_max_percent
        zpi = d.zero_power_immediate

        if "ftp" in raw:
            v = raw["ftp"]
            if isinstance(v, int) and not isinstance(v, bool) and 100 <= v <= 500:
                ftp = v
            else:
                user_logger.warning(f"⚠ Érvénytelen 'ftp' érték: {v} (100–500 közötti egész kell)")

        if "min_watt" in raw:
            v = raw["min_watt"]
            if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 9999:
                min_watt = v
            else:
                user_logger.warning(f"⚠ Érvénytelen 'min_watt' érték: {v} (0–9999 közötti egész kell)")

        if "max_watt" in raw:
            v = raw["max_watt"]
            if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 100000:
                max_watt = v
            else:
                user_logger.warning(f"⚠ Érvénytelen 'max_watt' érték: {v} (1–100000 közötti egész kell)")

        if "z1_max_percent" in raw:
            v = raw["z1_max_percent"]
            if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 100:
                z1 = v
            else:
                user_logger.warning(f"⚠ Érvénytelen 'z1_max_percent' érték: {v} (1–100 közötti egész kell)")

        if "z2_max_percent" in raw:
            v = raw["z2_max_percent"]
            if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 100:
                z2 = v
            else:
                user_logger.warning(f"⚠ Érvénytelen 'z2_max_percent' érték: {v} (1–100 közötti egész kell)")

        if "zero_power_immediate" in raw:
            v = raw["zero_power_immediate"]
            if isinstance(v, bool):
                zpi = v
            else:
                user_logger.warning(f"⚠ Érvénytelen 'zero_power_immediate' érték: {v} (true/false kell)")

        return cls(ftp=ftp, min_watt=min_watt, max_watt=max_watt,
                   z1_max_percent=z1, z2_max_percent=z2, zero_power_immediate=zpi)

    def to_dict(self) -> Dict[str, Any]:
        """Visszaadja dict formában (JSON serializáláshoz)."""
        return dataclasses.asdict(self)


@dataclasses.dataclass
class GlobalSettingsConfig:
    """Globális beállítások – típusbiztos."""

    cooldown_seconds: int = 120
    buffer_seconds: int = 3
    minimum_samples: int = 6
    buffer_rate_hz: int = 4
    dropout_timeout: int = 5
    log_directory: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GlobalSettingsConfig":
        d = cls()
        fields_range = {
            "cooldown_seconds": (0, 300),
            "buffer_seconds": (1, 10),
            "minimum_samples": (1, 1000),
            "buffer_rate_hz": (1, 60),
            "dropout_timeout": (1, 120),
        }
        kwargs: dict[str, Any] = {}
        for key, (lo, hi) in fields_range.items():
            if key in raw:
                v = raw[key]
                if isinstance(v, bool):
                    user_logger.warning(f"⚠ Érvénytelen '{key}' érték: {v!r} ({lo}–{hi} közötti egész kell)")
                elif isinstance(v, (int, float)) and lo <= v <= hi:
                    kwargs[key] = int(v)
                else:
                    user_logger.warning(f"⚠ Érvénytelen '{key}' érték: {v} ({lo}–{hi} közötti egész kell)")
        # log_directory: null vagy valid string
        if "log_directory" in raw:
            ld = raw["log_directory"]
            if ld is None:
                kwargs["log_directory"] = None
            elif isinstance(ld, str) and ld.strip():
                kwargs["log_directory"] = ld.strip()
        return cls(**{**dataclasses.asdict(d), **kwargs})


@dataclasses.dataclass
class HeartRateZonesConfig:
    """Szívfrekvencia zóna beállítások – típusbiztos, validált.

    Validáció a __post_init__-ben:
      - z1_max_percent < z2_max_percent
      - resting_hr < max_hr
      - valid_min_hr < valid_max_hr
    """

    enabled: bool = True
    max_hr: int = 185
    resting_hr: int = 60
    zone_mode: str = ZoneMode.HIGHER_WINS
    z1_max_percent: int = 70
    z2_max_percent: int = 80
    valid_min_hr: int = 30
    valid_max_hr: int = 220
    zero_hr_immediate: bool = False

    def __post_init__(self) -> None:
        # z1/z2 százalék kereszt-validáció
        if self.z1_max_percent >= self.z2_max_percent:
            low = min(self.z1_max_percent, self.z2_max_percent)
            high = max(self.z1_max_percent, self.z2_max_percent)
            if low == high:
                if low >= 100:
                    low, high = 99, 100
                else:
                    high = low + 1
            user_logger.warning(
                f"⚠ Érvénytelen HR zóna százalékok (z1={self.z1_max_percent}, z2={self.z2_max_percent}). "
                f"Értékek rendezése és legalább 1% különbség biztosítása."
            )
            self.z1_max_percent, self.z2_max_percent = low, high

        # resting_hr < max_hr
        if self.resting_hr >= self.max_hr:
            new_rest = max(30, self.max_hr - 1)
            user_logger.warning(
                f"⚠ Érvénytelen HR értékek (resting_hr={self.resting_hr}, max_hr={self.max_hr}). "
                f"resting_hr {new_rest}-re állítva."
            )
            self.resting_hr = new_rest

        # valid_min_hr < valid_max_hr
        if self.valid_min_hr >= self.valid_max_hr:
            user_logger.warning(
                f"⚠ valid_min_hr ({self.valid_min_hr}) >= valid_max_hr ({self.valid_max_hr}), "
                f"alapértelmezés visszaállítva."
            )
            defaults = HeartRateZonesConfig.__dataclass_fields__
            self.valid_min_hr = defaults["valid_min_hr"].default
            self.valid_max_hr = defaults["valid_max_hr"].default

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "HeartRateZonesConfig":
        d = cls()
        kwargs: dict[str, Any] = dataclasses.asdict(d)

        int_fields = {
            "max_hr": (100, 220),
            "resting_hr": (30, 100),
            "z1_max_percent": (1, 100),
            "z2_max_percent": (1, 100),
            "valid_min_hr": (30, 100),
            "valid_max_hr": (150, 300),
        }
        for key, (lo, hi) in int_fields.items():
            if key in raw:
                v = raw[key]
                if isinstance(v, bool):
                    user_logger.warning(f"⚠ Érvénytelen '{key}' érték: {v!r} ({lo}–{hi} közötti egész kell)")
                elif isinstance(v, (int, float)) and lo <= v <= hi:
                    kwargs[key] = int(v)
                else:
                    user_logger.warning(f"⚠ Érvénytelen '{key}' érték: {v} ({lo}–{hi} közötti egész kell)")

        for key in ("enabled", "zero_hr_immediate"):
            if key in raw:
                v = raw[key]
                if isinstance(v, bool):
                    kwargs[key] = v
                else:
                    user_logger.warning(f"⚠ Érvénytelen '{key}' érték: {v} (true/false kell)")

        if "zone_mode" in raw and raw["zone_mode"] in VALID_ZONE_MODES:
            kwargs["zone_mode"] = raw["zone_mode"]

        return cls(**kwargs)


@dataclasses.dataclass
class BleConfig:
    """BLE kimeneti (ventillátor) beállítások – típusbiztos."""

    device_name: Optional[str] = None
    scan_timeout: int = 10
    connection_timeout: int = 15
    reconnect_interval: int = 5
    max_retries: int = 10
    command_timeout: int = 3
    service_uuid: str = "0000ffe0-0000-1000-8000-00805f9b34fb"
    characteristic_uuid: str = "0000ffe1-0000-1000-8000-00805f9b34fb"
    pin_code: Optional[str] = "123456"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BleConfig":
        d = cls()
        kwargs: dict[str, Any] = dataclasses.asdict(d)

        # device_name
        if "device_name" in raw:
            dn = raw["device_name"]
            if dn is None or (isinstance(dn, str) and not dn.strip()):
                kwargs["device_name"] = None
            elif isinstance(dn, str) and dn.strip():
                kwargs["device_name"] = dn.strip()

        int_fields = {
            "scan_timeout": (1, 60),
            "connection_timeout": (1, 60),
            "reconnect_interval": (1, 60),
            "max_retries": (1, 100),
            "command_timeout": (1, 30),
        }
        for key, (lo, hi) in int_fields.items():
            if key in raw:
                v = raw[key]
                if isinstance(v, bool):
                    user_logger.warning(f"⚠ Érvénytelen '{key}' érték: {v!r} ({lo}–{hi} közötti egész kell)")
                elif isinstance(v, (int, float)) and lo <= v <= hi:
                    kwargs[key] = int(v)
                else:
                    user_logger.warning(f"⚠ Érvénytelen '{key}' érték: {v} ({lo}–{hi} közötti egész kell)")

        if isinstance(raw.get("service_uuid"), str) and raw["service_uuid"]:
            kwargs["service_uuid"] = raw["service_uuid"]
        if isinstance(raw.get("characteristic_uuid"), str) and raw["characteristic_uuid"]:
            kwargs["characteristic_uuid"] = raw["characteristic_uuid"]

        # pin_code
        if "pin_code" in raw:
            pc = raw["pin_code"]
            if pc is None:
                kwargs["pin_code"] = None
            elif isinstance(pc, int) and not isinstance(pc, bool) and 0 <= pc <= 999999:
                kwargs["pin_code"] = str(pc)
                if len(str(pc)) < 6:
                    user_logger.warning(
                        f"⚠ pin_code int-ként megadva ({pc}) → \"{str(pc)}\". "
                        f"Ha vezető nullákra van szükség, string-ként add meg: "
                        f"\"pin_code\": \"{pc:06d}\""
                    )
            elif isinstance(pc, str) and pc.isdigit() and 0 < len(pc) <= 20:
                kwargs["pin_code"] = pc
            else:
                user_logger.warning(f"⚠ Érvénytelen 'pin_code' érték: {pc}")

        return cls(**kwargs)


@dataclasses.dataclass
class DatasourceConfig:
    """Adatforrás beállítások – típusbiztos."""

    power_source: Optional[str] = DataSource.ZWIFTUDP
    hr_source: Optional[str] = DataSource.ZWIFTUDP
    BLE_buffer_seconds: int = 3
    BLE_minimum_samples: int = 6
    BLE_buffer_rate_hz: int = 4
    BLE_dropout_timeout: int = 5
    ANT_buffer_seconds: int = 3
    ANT_minimum_samples: int = 6
    ANT_buffer_rate_hz: int = 4
    ANT_dropout_timeout: int = 5
    zwiftUDP_buffer_seconds: int = 10
    zwiftUDP_minimum_samples: int = 2
    zwiftUDP_buffer_rate_hz: int = 3
    zwiftUDP_dropout_timeout: int = 15
    ant_power_device_id: int = 0
    ant_hr_device_id: int = 0
    ant_power_reconnect_interval: int = 5
    ant_power_max_retries: int = 10
    ant_hr_reconnect_interval: int = 5
    ant_hr_max_retries: int = 10
    ble_power_device_name: Optional[str] = None
    ble_power_scan_timeout: int = 10
    ble_power_reconnect_interval: int = 5
    ble_power_max_retries: int = 10
    ble_hr_device_name: Optional[str] = None
    ble_hr_scan_timeout: int = 10
    ble_hr_reconnect_interval: int = 5
    ble_hr_max_retries: int = 10
    zwift_udp_port: int = 7878
    zwift_udp_host: str = "127.0.0.1"
    zwift_auto_launch: bool = True
    zwift_launcher_path: Optional[str] = None

    def __post_init__(self) -> None:
        # minimum_samples <= buffer_seconds * buffer_rate_hz cross-validation
        for prefix in ("BLE", "ANT", "zwiftUDP"):
            bs = getattr(self, f"{prefix}_buffer_seconds")
            ms = getattr(self, f"{prefix}_minimum_samples")
            brz = getattr(self, f"{prefix}_buffer_rate_hz")
            if bs > 0 and brz > 0:
                max_samples = bs * brz
                if ms > max_samples:
                    user_logger.warning(
                        f"⚠ [{prefix}] Érvénytelen minimum_samples ({ms}) – "
                        f"nagyobb, mint buffer_seconds * buffer_rate_hz "
                        f"({bs} * {brz} = {max_samples}). "
                        f"{prefix}_minimum_samples {max_samples}-re állítva."
                    )
                    setattr(self, f"{prefix}_minimum_samples", max_samples)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DatasourceConfig":
        d = cls()
        kwargs: dict[str, Any] = dataclasses.asdict(d)

        # power_source / hr_source
        if raw.get("power_source") in VALID_DATA_SOURCES:
            kwargs["power_source"] = raw["power_source"]
        elif "power_source" in raw and raw["power_source"] is None:
            kwargs["power_source"] = None
        if raw.get("hr_source") in VALID_DATA_SOURCES:
            kwargs["hr_source"] = raw["hr_source"]
        elif "hr_source" in raw and raw["hr_source"] is None:
            kwargs["hr_source"] = None

        # ANT+ device IDs
        for key in ("ant_power_device_id", "ant_hr_device_id"):
            _from_dict_int(raw, kwargs, key, 0, 65535)
        for key in ("ant_power_reconnect_interval", "ant_hr_reconnect_interval"):
            _from_dict_int(raw, kwargs, key, 1, 60)
        for key in ("ant_power_max_retries", "ant_hr_max_retries"):
            _from_dict_int(raw, kwargs, key, 1, 100)

        # BLE sensor device names
        for key in ("ble_power_device_name", "ble_hr_device_name"):
            if key in raw and (raw[key] is None or isinstance(raw[key], str)):
                kwargs[key] = raw[key]
        for key in ("ble_power_scan_timeout", "ble_power_reconnect_interval",
                     "ble_hr_scan_timeout", "ble_hr_reconnect_interval"):
            _from_dict_int(raw, kwargs, key, 1, 60)
        for key in ("ble_power_max_retries", "ble_hr_max_retries"):
            _from_dict_int(raw, kwargs, key, 1, 100)

        # Zwift UDP
        if isinstance(raw.get("zwift_udp_host"), str) and raw["zwift_udp_host"]:
            kwargs["zwift_udp_host"] = raw["zwift_udp_host"]
        _from_dict_int(raw, kwargs, "zwift_udp_port", 1024, 65535)

        if "zwift_auto_launch" in raw and isinstance(raw["zwift_auto_launch"], bool):
            kwargs["zwift_auto_launch"] = raw["zwift_auto_launch"]
        if "zwift_launcher_path" in raw:
            lp = raw["zwift_launcher_path"]
            if lp is None or isinstance(lp, str):
                kwargs["zwift_launcher_path"] = lp

        # Per-source buffer settings
        for prefix in ("BLE", "ANT", "zwiftUDP"):
            _from_dict_int(raw, kwargs, f"{prefix}_buffer_seconds", 1, 60)
            _from_dict_int(raw, kwargs, f"{prefix}_minimum_samples", 1, 100)
            _from_dict_int(raw, kwargs, f"{prefix}_buffer_rate_hz", 1, 60)
            _from_dict_int(raw, kwargs, f"{prefix}_dropout_timeout", 1, 300)

        return cls(**kwargs)


@dataclasses.dataclass
class HudConfig:
    """HUD beállítások – típusbiztos."""

    sound_enabled: bool = True
    sound_volume: float = 0.5
    close_at_zwiftapp_exe: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "HudConfig":
        kwargs: dict[str, Any] = {}
        if "sound_enabled" in raw and isinstance(raw["sound_enabled"], bool):
            kwargs["sound_enabled"] = raw["sound_enabled"]
        if "sound_volume" in raw and isinstance(raw["sound_volume"], (int, float)):
            kwargs["sound_volume"] = float(raw["sound_volume"])
        # Support both old key "close_at_zwiftapp.exe" and new "close_at_zwiftapp_exe"
        for key in ("close_at_zwiftapp.exe", "close_at_zwiftapp_exe"):
            if key in raw and isinstance(raw[key], bool):
                kwargs["close_at_zwiftapp_exe"] = raw[key]
        return cls(**kwargs)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-kompatibilis dict (régi kulcsnévvel a kompatibilitásért)."""
        return {
            "sound_enabled": self.sound_enabled,
            "sound_volume": self.sound_volume,
            "close_at_zwiftapp.exe": self.close_at_zwiftapp_exe,
        }


def _from_dict_int(src: dict[str, Any], dst: dict[str, Any], key: str, lo: int, hi: int) -> None:
    """Helper: int mezőt olvas raw dict-ből dst dict-be validálva."""
    if key not in src:
        return
    v = src[key]
    if isinstance(v, bool):
        user_logger.warning(f"⚠ Érvénytelen '{key}' érték: {v!r} ({lo}–{hi} közötti egész kell)")
        return
    if isinstance(v, float) and not v.is_integer():
        user_logger.warning(f"⚠ Érvénytelen '{key}' érték: {v} (törtrész nem elfogadott, egész kell)")
        return
    if isinstance(v, (int, float)) and lo <= v <= hi:
        dst[key] = int(v)
    else:
        user_logger.warning(f"⚠ Érvénytelen '{key}' érték: {v} ({lo}–{hi} közötti egész kell)")


# ============================================================
# ALAPÉRTELMEZETT BEÁLLÍTÁSOK
# ============================================================

DEFAULT_SETTINGS: Dict[str, Any] = {
    "global_settings": GlobalSettingsConfig(),
    "power_zones": PowerZonesConfig(),
    "heart_rate_zones": HeartRateZonesConfig(),
    "ble": BleConfig(),
    "datasource": DatasourceConfig(),
    "hud": HudConfig(),
}


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
# TISZTA FÜGGVÉNYEK – ZÓNA SZÁMÍTÁS
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


def calculate_power_zones(
    ftp: int,
    min_watt: int,
    max_watt: int,
    z1_pct: int,
    z2_pct: int,
) -> Dict[int, Tuple[int, int]]:
    """Kiszámítja a teljesítmény zóna határokat.

    Args:
        ftp: Funkcionális küszöbteljesítmény (W).
        min_watt: Minimális érvényes pozitív teljesítmény (W).
        max_watt: Maximális érvényes teljesítmény (W).
        z1_pct: Z1 felső határ az FTP %-ában.
        z2_pct: Z2 felső határ az FTP %-ában.

    Returns:
        Dict formátum: {0: (0,0), 1: (1, z1_max), 2: (z1_max+1, z2_max), 3: (z2_max+1, max_watt)}
    """
    # max(1, ...) védi az érvénytelen z1_max=0 esetet (pl. nagyon alacsony FTP/százalék)
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
) -> Dict[str, int]:
    """Kiszámítja a HR zóna határokat bpm-ben.

    Args:
        max_hr: Maximális szívfrekvencia (bpm).
        resting_hr: Pihenő szívfrekvencia (bpm); ez alatt Z0.
        z1_pct: Z1 felső határ a max_hr %-ában.
        z2_pct: Z2 felső határ a max_hr %-ában.

    Returns:
        Dict: {'resting': int, 'z1_max': int, 'z2_max': int}
    """
    return {
        "resting": resting_hr,
        "z1_max": int(max_hr * z1_pct / 100),
        "z2_max": int(max_hr * z2_pct / 100),
    }


def zone_for_power(power: float, zones: Dict[int, Tuple[int, int]]) -> int:
    """Meghatározza a teljesítmény zónát (0–3) az adott watt értékhez.

    Args:
        power: Teljesítmény wattban.
        zones: Zóna határok dict-je (calculate_power_zones kimenetele).

    Returns:
        Zóna szám (0–3).
    """
    if power <= 0:
        return 0
    # Védekezés üres vagy hibás zones dict ellen (ValueError elkerülése)
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
    return 3  # csak max_watt felett érthető el


def zone_for_hr(hr: int, hr_zones: Dict[str, int]) -> int:
    """Meghatározza a HR zónát (0–3) az adott bpm értékhez.

    Args:
        hr: Szívfrekvencia bpm-ben.
        hr_zones: HR zóna határok dict-je (calculate_hr_zones kimenetele).

    Returns:
        Zóna szám (0–3).
    """
    if hr <= 0 or hr < hr_zones["resting"]:
        return 0
    if hr <= hr_zones["z1_max"]:
        return 1
    if hr <= hr_zones["z2_max"]:
        return 2
    return 3


def is_valid_power(power: Any, min_watt: int, max_watt: int) -> bool:
    """Ellenőrzi, hogy az érték érvényes teljesítmény adat-e.

    Args:
        power: Az ellenőrizendő érték.
        min_watt: Minimális érvényes pozitív watt (0 és min_watt között elutasítva).
        max_watt: Maximális érvényes watt.

    Returns:
        True, ha érvényes teljesítmény adat.
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
    """Ellenőrzi, hogy az érték érvényes szívfrekvencia adat-e.

    Args:
        hr: Az ellenőrizendő érték.
        valid_min_hr: Minimális érvényes HR érték (bpm).
        valid_max_hr: Maximális érvényes HR érték (bpm).

    Returns:
        True, ha érvényes HR adat.
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
# TISZTA FÜGGVÉNYEK – ÁTLAGSZÁMÍTÁS
# ============================================================


def compute_average(samples: deque[float]) -> Optional[float]:
    """Kiszámítja a minták számtani átlagát.

    Args:
        samples: Mintákat tartalmazó deque.

    Returns:
        Az átlag float értéke, vagy None, ha nincs minta.
    """
    if not samples:
        return None
    return sum(samples) / len(samples)


# ============================================================
# TISZTA FÜGGVÉNYEK – ZÓNA LOGIKA (higher_wins, zone_mode)
# ============================================================


def higher_wins(zone_a: int, zone_b: int) -> int:
    """A két zóna közül a nagyobbat adja vissza.

    Args:
        zone_a: Első zóna (0–3).
        zone_b: Második zóna (0–3).

    Returns:
        A nagyobb zóna szám.
    """
    return max(zone_a, zone_b)


def apply_zone_mode(
    power_zone: Optional[int],
    hr_zone: Optional[int],
    zone_mode: ZoneMode,
) -> Optional[int]:
    """A zone_mode alapján kombinálja a power és HR zónákat.

    Zóna módok:
        "power_only"  – csak a teljesítmény zóna dönt (HR figyelmen kívül)
        "hr_only"     – csak a HR zóna dönt (power figyelmen kívül)
        "higher_wins" – a kettő közül a nagyobb dönt

    Args:
        power_zone: Teljesítmény zóna (0–3), vagy None ha nem elérhető.
        hr_zone: HR zóna (0–3), vagy None ha nem elérhető.
        zone_mode: A kombinálási mód ("power_only", "hr_only", "higher_wins").

    Returns:
        A végső zóna szám (0–3), vagy None ha nincs elég adat.
    """
    if zone_mode == ZoneMode.POWER_ONLY:
        return power_zone
    if zone_mode == ZoneMode.HR_ONLY:
        return hr_zone
    # higher_wins: mindkét forrásból a nagyobb
    if power_zone is not None and hr_zone is not None:
        return higher_wins(power_zone, hr_zone)
    if power_zone is not None:
        return power_zone
    return hr_zone


# ============================================================
# COOLDOWN LOGIKA
# ============================================================


class CooldownController:
    """Cooldown logika kezelője zóna csökkentés esetén.

    Zóna csökkentésekor nem vált azonnal, hanem cooldown_seconds
    másodpercig vár. Zóna növelésekor azonnal vált, cooldown nélkül.

    Adaptív cooldown módosítások:
        - Nagy zónaesés (>= 2 szint) vagy 0W → cooldown felezés (gyorsabb leállás)
        - Pending zóna emelkedik → cooldown duplázás (lassabb emelkedés)

    Attribútumok:
        cooldown_seconds: A cooldown időtartama másodpercben.
        active: True, ha a cooldown timer fut.
        start_time: A cooldown indítási ideje (time.monotonic()).
        pending_zone: A cooldown lejárta után alkalmazandó zóna.
        can_halve: True, ha a cooldown felezés még elvégezhető.
        can_double: True, ha a cooldown duplázás még elvégezhető.
    """

    PRINT_INTERVAL = 10.0

    def __init__(self, cooldown_seconds: int) -> None:
        self._lock = threading.Lock()
        self.cooldown_seconds = cooldown_seconds
        self.active = False
        self.start_time = 0.0
        self.pending_zone: Optional[int] = None
        self.can_halve = True
        self.can_double = False
        self._last_print = 0.0

    def process(
        self,
        current_zone: Optional[int],
        new_zone: int,
        zero_immediate: bool,
    ) -> Optional[int]:
        """Feldolgozza az új zóna javaslatot és alkalmazza a cooldown logikát.

        Args:
            current_zone: Az aktuális zóna (None = még nincs döntés).
            new_zone: Az új javasolt zóna (0–3).
            zero_immediate: True, ha 0W esetén azonnali leállás szükséges.

        Returns:
            A küldendő zóna szintje, ha változás szükséges; None egyébként.
        """
        with self._lock:
            return self._process_locked(current_zone, new_zone, zero_immediate)

    def _process_locked(
        self,
        current_zone: Optional[int],
        new_zone: int,
        zero_immediate: bool,
    ) -> Optional[int]:
        """Belső process logika – lock alatt hívandó."""
        now = time.monotonic()

        # Első döntés – nincs előző zóna
        if current_zone is None:
            self._reset_locked()
            return new_zone

        # 0W azonnali leállás (zero_power_immediate=True)
        if new_zone == 0 and zero_immediate:
            if current_zone != 0:
                self._reset_locked()
                user_logger.info("✓ 0W detektálva: azonnali leállás (cooldown nélkül)")
                return 0
            return None

        # Aktív cooldown kezelése
        if self.active:
            return self._handle_active(current_zone, new_zone, now)

        # Nincs cooldown – normál zónaváltás logika
        if new_zone == current_zone:
            return None
        if new_zone > current_zone:
            return new_zone
        # cooldown_seconds == 0 → azonnali váltás, nincs cooldown
        if self.cooldown_seconds == 0:
            return new_zone
        # Zóna csökkentés → cooldown indul
        return self._start(current_zone, new_zone, now)

    def _start(self, current_zone: int, new_zone: int, now: float) -> Optional[int]:
        """Cooldown indítása zóna csökkentésnél."""
        self.active = True
        self.start_time = now
        self.pending_zone = new_zone
        self.can_halve = True
        self.can_double = False
        # _last_print beállítása megakadályozza az azonnali dupla kiírást
        # az első _handle_active hívásnál
        self._last_print = now
        user_logger.info(
            f"🕐 Cooldown indítva: {self.cooldown_seconds}s várakozás (cél: {new_zone})"
        )
        # Nagy zónaesés esetén azonnali felezés
        if new_zone == 0 or (current_zone - new_zone >= 2):
            self._halve(now)
        return None

    def _handle_active(
        self, current_zone: int, new_zone: int, now: float
    ) -> Optional[int]:
        """Aktív cooldown feldolgozása – lock alatt hívandó."""
        # Zóna emelkedés → cooldown törlése
        if new_zone >= current_zone:
            self._reset_locked()
            if new_zone > current_zone:
                user_logger.info(f"✓ Teljesítmény emelkedés: cooldown törölve → zóna: {new_zone}")
                return new_zone
            return None

        elapsed = now - self.start_time

        # Cooldown lejárt
        if elapsed >= self.cooldown_seconds:
            target = new_zone
            self._reset_locked()
            if target != current_zone:
                user_logger.info(f"✓ Cooldown lejárt! Zóna váltás: {current_zone} → {target}")
                return target
            user_logger.info("✓ Cooldown lejárt, nincs zónaváltás (már a célzónában)")
            return None

        remaining = self.cooldown_seconds - elapsed

        # Pending zóna frissítése + adaptív cooldown módosítás
        if new_zone != self.pending_zone:
            old_pending = self.pending_zone
            self.pending_zone = new_zone
            if old_pending is not None and new_zone > old_pending and self.can_double:
                self._double(now)
                remaining = self.cooldown_seconds - (now - self.start_time)
                user_logger.info(
                    f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó zóna: {new_zone})"
                )
            elif (new_zone == 0 or (current_zone - new_zone >= 2)) and self.can_halve:
                self._halve(now)
                remaining = self.cooldown_seconds - (now - self.start_time)
                user_logger.info(
                    f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó zóna: {new_zone})"
                )
            else:
                user_logger.info(
                    f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó zóna: {new_zone})"
                )
            self._last_print = now
        elif now - self._last_print >= self.PRINT_INTERVAL:
            user_logger.info(
                f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó: {self.pending_zone})"
            )
            self._last_print = now

        return None

    def _halve(self, now: float) -> None:
        """Felezi a maradék cooldown időt."""
        remaining = max(0.0, self.cooldown_seconds - (now - self.start_time))
        new_remaining = remaining / 2
        self.start_time = now - (self.cooldown_seconds - new_remaining)
        self.can_halve = False
        self.can_double = True
        user_logger.info(f"🕐 Cooldown felezve: {remaining:.0f}s → {new_remaining:.0f}s")

    def _double(self, now: float) -> None:
        """Duplázza a maradék cooldown időt."""
        remaining = max(0.0, self.cooldown_seconds - (now - self.start_time))
        new_remaining = min(remaining * 2, float(self.cooldown_seconds))
        self.start_time = now - (self.cooldown_seconds - new_remaining)
        self.can_double = False
        self.can_halve = True
        user_logger.info(f"🕐 Cooldown duplázva: {remaining:.0f}s → {new_remaining:.0f}s")

    def reset(self) -> None:
        """Törli a cooldown állapotát (publikus API, szálbiztos)."""
        with self._lock:
            self._reset_locked()

    def _reset_locked(self) -> None:
        """Törli a cooldown állapotát – lock alatt hívandó."""
        self.active = False
        self.pending_zone = None
        self.can_halve = True
        self.can_double = False

    def snapshot(self) -> Tuple[bool, float]:
        """Szálbiztos pillanatfelvétel a HUD számára.

        Returns:
            (active, remaining_seconds) tuple.
        """
        with self._lock:
            if not self.active:
                return False, 0.0
            remaining = max(0.0, self.cooldown_seconds - (time.monotonic() - self.start_time))
            return True, remaining

    def __repr__(self) -> str:
        active, remaining = self.snapshot()
        return (
            f"CooldownController(active={active}, remaining={remaining:.1f}s, "
            f"pending_zone={self.pending_zone}, cooldown={self.cooldown_seconds}s)"
        )


# ============================================================
# GÖRDÜLŐ ÁTLAGOLÁS – KÖZÖS BASE CLASS
# ============================================================


class _RollingAverager:
    """Gördülő átlagot számít bejövő numerikus mintákból.

    buffer_rate_hz mintát vár másodpercenként, és buffer_seconds
    másodpercnyi ablakot tart. Az effective_minimum automatikusan
    alkalmazkodik a valódi buffer méretéhez, így akkor is
    számol átlagot, ha kevesebb adat érkezik, mint minimum_samples.

    Attribútumok:
        buffer: Mintákat tároló deque (maxlen = buffer_seconds × buffer_rate_hz).
        minimum_samples: Kívánt minimum mintaszám érvényes átlaghoz.
        effective_minimum: Ténylegesen alkalmazott minimum (max: buffersize // 2).
        buffersize: A buffer maximális mérete.
    """

    def __init__(
        self,
        buffer_seconds: int,
        minimum_samples: int,
        buffer_rate_hz: int = 4,
        label: str = "adat",
    ) -> None:
        rate = max(1, int(buffer_rate_hz))
        self.buffersize = max(1, int(buffer_seconds) * rate)
        self.buffer: deque[float] = deque(maxlen=self.buffersize)
        self.minimum_samples = minimum_samples
        # Védelem: effective_minimum soha nem nagyobb, mint a buffer fele
        self.effective_minimum = min(self.minimum_samples, max(1, self.buffersize // 2))
        self._label = label

    def add_sample(self, value: float) -> Optional[float]:
        """Új minta hozzáadása és az átlag visszaadása, ha elég minta van."""
        self.buffer.append(value)
        if len(self.buffer) < self.effective_minimum:
            logging.debug(
                "%s adatok gyűjtése: %d/%d (effective min)",
                self._label,
                len(self.buffer),
                self.effective_minimum,
            )
            return None
        return compute_average(self.buffer)

    def clear(self) -> None:
        """Törli az összes pufferelt mintát."""
        self.buffer.clear()


class PowerAverager(_RollingAverager):
    """Gördülő átlagszámítás teljesítmény (watt) mintákhoz."""

    def __init__(
        self, buffer_seconds: int, minimum_samples: int, buffer_rate_hz: int = 4
    ) -> None:
        super().__init__(buffer_seconds, minimum_samples, buffer_rate_hz, label="Power")


class HRAverager(_RollingAverager):
    """Gördülő átlagszámítás szívfrekvencia (bpm) mintákhoz."""

    def __init__(
        self, buffer_seconds: int, minimum_samples: int, buffer_rate_hz: int = 4
    ) -> None:
        super().__init__(buffer_seconds, minimum_samples, buffer_rate_hz, label="HR")


# ============================================================
# KONZOLOS KIÍRÁS (throttle-olt)
# ============================================================


class ConsolePrinter:
    """Throttle-olt konzol kiírás – ugyanaz az üzenet nem jelenhet meg túl sűrűn.

    Minden üzenettípushoz (key) külön időzítőt tart. Az üzenet csak
    akkor kerül kiírásra, ha az utolsó kiírás óta legalább interval
    másodperc telt el.

    Megjegyzés: a metódus neve 'emit', hogy ne fedje el a beépített print()-et.

    Attribútumok:
        _last_times: Utolsó kiírás ideje üzenetkulcsonként.
    """

    def __init__(self) -> None:
        self._last_times: Dict[str, float] = {}

    def emit(self, key: str, message: str, interval: float = 1.0) -> bool:
        """Kiírja az üzenetet, ha az interval eltelt.

        Args:
            key: Egyedi kulcs az üzenet azonosításához (pl. "power_raw").
            message: A kiírandó szöveg.
            interval: Minimális másodpercek száma két azonos kulcsú kiírás között.

        Returns:
            True, ha az üzenet kiírásra kerül; False, ha throttle-olt.
        """
        now = time.monotonic()
        if now - self._last_times.get(key, 0.0) >= interval:
            user_logger.info(message)
            self._last_times[key] = now
            return True
        return False



# ============================================================
# UI SNAPSHOT – szálbiztos adatcsere asyncio ↔ PySide6 között
# ============================================================


@dataclasses.dataclass
class UISnapshot:
    """Szálbiztos snapshot az asyncio loop és a PySide6 UI között.

    Az asyncio oldalon update() hívással frissítendő,
    a PySide6 oldalon read() hívással olvasható.
    A threading.Lock garantálja a race condition-mentességet.
    """

    zone: Optional[int] = None
    avg_power: Optional[float] = None
    avg_hr: Optional[float] = None
    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)

    def update(
        self,
        zone: Optional[int],
        avg_power: Optional[float],
        avg_hr: Optional[float],
    ) -> None:
        """Frissíti a snapshot értékeit (asyncio szálból hívandó)."""
        with self._lock:
            self.zone = zone
            self.avg_power = avg_power
            self.avg_hr = avg_hr

    def read(self) -> Tuple[Optional[int], Optional[float], Optional[float]]:
        """Visszaadja a snapshot értékeit (PySide6 szálból hívandó)."""
        with self._lock:
            return self.zone, self.avg_power, self.avg_hr


# ============================================================
# MEGOSZTOTT ÁLLAPOT
# ============================================================


class ControllerState:
    """A vezérlő megosztott állapota, asyncio.Lock-kal védve.

    Minden olyan mezőt tartalmaz, amelyet több asyncio korrutin is olvas
    vagy módosít. A lock biztosítja, hogy az olvasás-módosítás-írás
    műveletek atomikusak legyenek.

    Az ui_snapshot külön threading.Lock-kal védett, és kizárólag
    a PySide6 UI frissítéséhez használatos (szálbiztos olvasás).

    Attribútumok:
        current_zone: Az aktuálisan aktív ventilátor zóna (None = nincs döntés még).
        current_power_zone: A legutóbb kiszámított power zóna.
        current_hr_zone: A legutóbb kiszámított HR zóna.
        current_avg_power: A legutóbbi átlagolt teljesítmény (W).
        current_avg_hr: A legutóbbi átlagolt HR (bpm).
        last_power_time: Utolsó power adat érkezési ideje (monotonic).
        last_hr_time: Utolsó HR adat érkezési ideje (monotonic), vagy None.
        lock: asyncio.Lock a párhuzamos módosítások ellen.
        ui_snapshot: UISnapshot a PySide6 UI szálbiztos frissítéséhez.
    """

    def __init__(self) -> None:
        self.current_zone: Optional[int] = None
        self.current_power_zone: Optional[int] = None
        self.current_hr_zone: Optional[int] = None
        self.current_avg_power: Optional[float] = None
        self.current_avg_hr: Optional[float] = None
        self.last_power_time: Optional[float] = None
        self.last_hr_time: Optional[float] = None
        self.lock = asyncio.Lock()
        self.ui_snapshot = UISnapshot()

    def __repr__(self) -> str:
        return (
            f"ControllerState(zone={self.current_zone}, "
            f"power_zone={self.current_power_zone}, hr_zone={self.current_hr_zone}, "
            f"avg_power={self.current_avg_power}, avg_hr={self.current_avg_hr})"
        )


# ============================================================
# ZÓNA ELKÜLDÉSE (helper)
# ============================================================


async def send_zone(zone: int, zone_queue: asyncio.Queue[int]) -> None:
    """Zóna parancsot küld a BLE fan kimenet queue-ba.

    Ha a queue teli (maxsize=1), a régi parancsot elveti és az újat
    teszi be, hogy mindig a legfrissebb zóna kerüljön küldésre.
    A get_nowait() után a queue garantáltan üres, ezért put_nowait()
    nem dobhat QueueFull-t.

    Args:
        zone: Ventilátor zóna szintje (0–3).
        zone_queue: A BLE fan output asyncio.Queue-ja.
    """
    try:
        zone_queue.get_nowait()
    except asyncio.QueueEmpty:
        pass
    # get_nowait() után garantáltan szabad hely van
    zone_queue.put_nowait(zone)


# ============================================================
# BLE ESZKÖZ KERESÉS ÉS LOGOLÁS (közös segédfüggvények)
# ============================================================

def _ble_log_path() -> str:
    """Visszaadja a ble_devices.log teljes útvonalát a konfigurált log könyvtárban."""
    return os.path.join(_log_dir, "ble_devices.log")


def _log_ble_devices_to_file(
    devices_info: List[Tuple[Optional[str], str, List[str]]],
    scan_context: str,
) -> None:
    """Talált BLE eszközöket ír a ble_devices.log fájlba (append módban).

    Csak olyan eszközöket ír a fájlba, amelyek address-e még nem szerepel benne.
    Ha a fájl nem létezik, létrehozza. Minden bejegyzés időbélyeggel ellátott.

    Args:
        devices_info: Lista (name, address, service_uuids) tuple-ökből.
        scan_context: A keresés kontextusa (pl. "BLE Fan", "BLE Power").
    """
    if not devices_info:
        return

    # Meglévő address-ek beolvasása a fájlból
    existing_addresses: set[str] = set()
    try:
        with open(_ble_log_path(), "r", encoding="utf-8") as f:
            for line in f:
                # Sorok formátuma: "  név | ADDRESS | UUIDs: ..."
                parts = line.split("|")
                if len(parts) >= 2:
                    existing_addresses.add(parts[1].strip())
    except FileNotFoundError:
        pass  # Még nem létezik a fájl, minden eszköz új
    except OSError as exc:
        logger.warning(f"Nem sikerült olvasni a {_ble_log_path()} fájlt: {exc}")

    # Csak az új eszközök szűrése
    new_devices = [
        (name, addr, uuids)
        for name, addr, uuids in devices_info
        if addr not in existing_addresses
    ]

    if not new_devices:
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(_ble_log_path(), "a", encoding="utf-8") as f:
            f.write(f"\n--- BLE Scan ({scan_context}) @ {timestamp} ---\n")
            for name, addr, uuids in new_devices:
                uuid_str = ", ".join(uuids[:5]) if uuids else "–"
                f.write(f"  {name or '(névtelen)':30s} | {addr} | UUIDs: {uuid_str}\n")
    except OSError as exc:
        logger.warning(f"Nem sikerült írni a {_ble_log_path()} fájlba: {exc}")


def _print_ble_devices(
    devices_info: List[Tuple[Optional[str], str, List[str]]],
    scan_context: str,
    matched_addr: Optional[str] = None,
) -> None:
    """Talált BLE eszközöket ír a konzolra.

    Args:
        devices_info: Lista (name, address, service_uuids) tuple-ökből.
        scan_context: A keresés kontextusa.
        matched_addr: Az automatikusan kiválasztott eszköz címe (◄ jelöléshez).
    """
    user_logger.info(f"\n📡 BLE Scan ({scan_context}): {len(devices_info)} eszköz található")
    for name, addr, uuids in devices_info:
        marker = " ◄ AUTO" if matched_addr and addr == matched_addr else ""
        icon = "📱" if name else "❓"
        uuid_str = ", ".join(uuids[:3]) if uuids else "–"
        user_logger.info(f"  {icon} {name or '(névtelen)':30s} | {addr} | {uuid_str}{marker}")
    if not devices_info:
        user_logger.info("  (nincs eszköz a közelben)")


async def _scan_ble_with_autodiscovery(
    scan_timeout: int,
    target_service_uuid: Optional[str],
    scan_context: str,
) -> Tuple[Optional[Any], List[Tuple[Optional[str], str, List[str]]]]:
    """BLE eszközöket keres, logolja, és opcionálisan keres egy megadott service UUID-val.

    Ha target_service_uuid megadva, az első olyan eszközt választja ki,
    amelyik hirdeti ezt az UUID-t.

    Args:
        scan_timeout: Keresési timeout másodpercben.
        target_service_uuid: Keresett service UUID (vagy None).
        scan_context: A keresés kontextusa (logoláshoz).

    Returns:
        (matched_device, devices_info) – matched_device az első egyezés (BLEDevice)
        vagy None, devices_info a teljes lista.
    """
    if not _BLEAK_AVAILABLE:
        return None, []

    devices_info: List[Tuple[Optional[str], str, List[str]]] = []
    matched: Optional[Any] = None

    try:
        # return_adv=True: dict[str, tuple[BLEDevice, AdvertisementData]]
        discovered: Any = await BleakScanner.discover(
            timeout=scan_timeout, return_adv=True
        )

        items: list[Any] = (
            list(cast(Any, discovered).values())
            if isinstance(discovered, dict)
            else list(discovered)
        )

        for item in items:
            device: Any = None
            uuids: List[str] = []
            t = cast(tuple[Any, ...], item)
            if isinstance(item, tuple) and len(t) == 2:
                device = t[0]
                adv_data: Any = t[1]
                uuids = (
                    list(adv_data.service_uuids)
                    if hasattr(adv_data, "service_uuids") and adv_data.service_uuids
                    else []
                )
            else:
                device = cast(Any, item)

            dev_name: Optional[str] = getattr(device, "name", None)
            dev_addr: str = getattr(device, "address", str(device))
            devices_info.append((dev_name, dev_addr, uuids))

            if target_service_uuid and matched is None:
                if any(u.lower() == target_service_uuid.lower() for u in uuids):
                    matched = device

    except TypeError:
        # Fallback régebbi Bleak verziókhoz (return_adv nem támogatott)
        devices: Any = await BleakScanner.discover(timeout=scan_timeout)
        devices_info = [
            (getattr(d, "name", None), getattr(d, "address", ""), [])
            for d in devices
        ]
        matched = None

    except Exception as exc:
        logger.error(f"BLE scan hiba ({scan_context}): {exc}")
        return None, []

    matched_addr: Optional[str] = getattr(matched, "address", None) if matched else None
    _print_ble_devices(devices_info, scan_context, matched_addr)
    _log_ble_devices_to_file(devices_info, scan_context)

    return matched, devices_info


# ============================================================
# BLE VENTILÁTOR KIMENET VEZÉRLŐ
# ============================================================


class BLEFanOutputController:
    """BLE alapú ventilátor kimenet vezérlő (LEVEL:N parancsok küldése).

    Asyncio korrutin alapú implementáció. A parancsokat egy
    asyncio.Queue-n keresztül fogadja, és a BLE GATT karakterisztikára
    írja ki az ESP32 vezérlőnek. PIN autentikáció is támogatott.

    Attribútumok:
        device_name: A keresett BLE eszköz neve.
        is_connected: True, ha a BLE kapcsolat aktív.
        last_sent: Az utoljára sikeresen elküldött zóna szint.
    """

    RETRY_RESET_SECONDS = 30
    DISCONNECT_TIMEOUT = 5.0

    def __init__(self, settings: Dict[str, Any]) -> None:
        ble: BleConfig = settings["ble"]
        self.device_name: Optional[str] = ble.device_name
        self.scan_timeout: int = ble.scan_timeout
        self.connection_timeout: int = ble.connection_timeout
        self.reconnect_interval: int = ble.reconnect_interval
        self.max_retries: int = ble.max_retries
        self.command_timeout: int = ble.command_timeout
        self.service_uuid: str = ble.service_uuid
        self.characteristic_uuid: str = ble.characteristic_uuid
        self.pin_code: Optional[str] = ble.pin_code

        self.is_connected: bool = False
        self.last_sent: Optional[int] = None
        self._client: Optional[Any] = None
        self._device_address: Optional[str] = None
        self._retry_count: int = 0
        self._retry_reset_time: Optional[float] = None
        self._auth_failed: bool = False
        self.last_sent_time: float = 0.0
        # Utolsó reconnect kísérlet ideje – non-blocking reconnect logikához
        self._last_reconnect_attempt: float = 0.0
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def auth_failed(self) -> bool:
        """True ha az authentikáció sikertelen volt (PIN hibás)."""
        return self._auth_failed

    def __repr__(self) -> str:
        return (
            f"BLEFanOutputController(device={self.device_name!r}, "
            f"connected={self.is_connected}, last_sent={self.last_sent}, "
            f"retries={self._retry_count}/{self.max_retries})"
        )

    async def run(self, zone_queue: asyncio.Queue[int]) -> None:
        """A BLE fan kimenet fő korrutinja – olvassa a zone_queue-t és küldi a parancsokat.

        Indításkor megpróbál csatlakozni a BLE eszközhöz, majd folyamatosan
        olvassa a zone_queue-t és elküldi a zóna parancsokat.

        Args:
            zone_queue: asyncio.Queue, amelyből a zóna parancsokat olvassa.
        """
        self._loop = asyncio.get_running_loop()
        if not _BLEAK_AVAILABLE:
            msg = "BLE Fan: bleak könyvtár nem elérhető – BLE kimenet letiltva!"
            logger.error(msg)
            return

        logger.info("BLE Fan Output korrutin elindítva")
        await self._initial_connect()

        while True:
            zone = await zone_queue.get()
            await self._send_zone(zone)

    async def _initial_connect(self) -> None:
        """Kezdeti BLE csatlakozás indításkor (hiba esetén folytatja)."""
        ok = await self._scan_and_connect()
        if not ok:
            logger.warning(
                "BLE Fan: kezdeti csatlakozás sikertelen, automatikus újrapróbálkozás parancs küldéskor."
            )

    async def _scan_and_connect(self) -> bool:
        """BLE eszköz keresése és csatlakozás.

        Ha device_name üres vagy None, automatikus felderítés indul:
        a service_uuid alapján keres megfelelő eszközt, az összes talált
        eszközt konzolra és ble_devices.log-ba írja.

        Returns:
            True, ha a csatlakozás sikeres.
        """
        if not _BLEAK_AVAILABLE:
            return False

        # --- Automatikus felderítés (nincs device_name beállítva) ---
        if not self.device_name:
            try:
                matched, _ = await _scan_ble_with_autodiscovery(
                    self.scan_timeout, self.service_uuid, "BLE Fan (auto)"
                )
                if matched is not None:
                    self._device_address = matched.address
                    user_logger.info(
                        f"✓ BLE Fan auto-csatlakozás: "
                        f"{matched.name or '(névtelen)'} ({matched.address})"
                    )
                    logger.info(
                        f"BLE Fan auto-felderítés: {matched.name} ({matched.address})"
                    )
                    return await self._connect()
                user_logger.warning(
                    f"⚠ BLE Fan: nem található eszköz a(z) {self.service_uuid} "
                    f"service UUID-val – újrapróbálkozás..."
                )
                return False
            except Exception as exc:
                logger.error(f"BLE Fan auto-felderítés hiba: {exc}")
                return False

        # --- Név alapú keresés (device_name beállítva) ---
        try:
            devices = await BleakScanner.discover(timeout=self.scan_timeout)
            for d in devices:
                if d.name == self.device_name:
                    self._device_address = d.address
                    logger.info(f"BLE Fan eszköz megtalálva: {d.name} ({d.address})")
                    return await self._connect()
                if d.name is None:
                    logger.debug(f"BLE eszköz név nélkül: {d.address}")

            logger.error(f"BLE Fan eszköz nem található: {self.device_name}")
            return False

        except Exception as exc:
            logger.error(f"BLE Fan keresési hiba: {exc}")
            return False

    async def _connect(self) -> bool:
        """Csatlakozás a korábban megtalált BLE eszközhöz.

        Returns:
            True, ha a csatlakozás sikeres.
        """
        if not _BLEAK_AVAILABLE:
            return False
        if not self._device_address:
            return False

        try:
            client = self._client
            if client and client.is_connected:
                return True

            client = BleakClient(
                self._device_address,
                timeout=self.connection_timeout,
                disconnected_callback=self._on_disconnect,
            )
            self._client = client

            await client.connect()

            if self.pin_code is not None:
                ok = await self._authenticate()
                if not ok:
                    return False

            self.is_connected = True
            self._retry_count = 0
            self._retry_reset_time = None
            self.last_sent = None
            logger.info(f"BLE Fan csatlakozva: {self._device_address}")
            return True

        except Exception as exc:
            logger.error(f"BLE Fan csatlakozási hiba: {exc}")
            self.is_connected = False
            self._client = None
            return False

    async def _authenticate(self) -> bool:
        """Alkalmazás szintű BLE PIN autentikáció.

        Returns:
            True, ha az autentikáció sikeres (vagy timeout esetén is folytatja).
        """
        client = self._client
        if client is None:
            logger.error("BLE AUTH hiba: nincs aktív BLE kliens")
            return False

        try:
            auth_event = asyncio.Event()
            auth_result: list[str] = [""]

            def _notify_cb(sender: Any, data: bytes) -> None:
                auth_result[0] = data.decode("utf-8", errors="replace").strip()
                auth_event.set()

            await client.start_notify(self.characteristic_uuid, _notify_cb)
            try:
                try:
                    await asyncio.wait_for(
                        client.write_gatt_char(
                            self.characteristic_uuid,
                            f"AUTH:{self.pin_code}".encode("utf-8"),
                        ),
                        timeout=self.command_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.error("BLE AUTH write timeout")
                    return False

                try:
                    await asyncio.wait_for(
                        auth_event.wait(),
                        timeout=self.command_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "BLE AUTH válasz timeout - folytatás autentikáció nélkül"
                    )
                    return True

                resp: str = auth_result[0]
                if not resp:
                    logger.error("BLE AUTH: üres válasz")
                    return False
                if resp == "AUTH_OK":
                    logger.info("BLE AUTH sikeres")
                    return True
                if resp in ("AUTH_FAIL", "AUTH_LOCKED"):
                    logger.error(
                        f"BLE AUTH sikertelen: {resp} - ellenorizd a pin_code erteket!"
                    )
                    user_logger.warning(f"✗ BLE PIN hiba ({resp}): helytelen pin_code! Javítsd a settings.json-ban.")
                    self._auth_failed = True
                    try:
                        await client.disconnect()
                    except Exception as exc:
                        logger.debug(f"BLE disconnect hiba PIN fail után: {exc}")
                    return False

                logger.warning(f"BLE AUTH ismeretlen válasz: {resp} - folytatás")
                return True

            finally:
                try:
                    await client.stop_notify(self.characteristic_uuid)
                except Exception as exc:
                    logger.debug(f"BLE stop_notify hiba: {exc}")

        except Exception as exc:
            logger.error(f"BLE AUTH hiba: {exc}")
            return False

    def _on_disconnect(self, client: Any) -> None:
        """Callback: BLE kapcsolat váratlan megszakadásakor.

        Bleak nem garantálja, hogy az asyncio event loop szálán hívja ezt,
        ezért loop.call_soon_threadsafe()-fel delegáljuk az állapotmódosítást.
        """
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._handle_disconnect)
        else:
            self._handle_disconnect()

    def _handle_disconnect(self) -> None:
        """Disconnect állapotmódosítás – az asyncio event loop-on hívandó."""
        logger.warning("BLE Fan kapcsolat megszakadt")
        self.is_connected = False
        self.last_sent = None
        # NEM nullázzuk self._client-et itt – az asyncio oldalon kezeljük

    async def _send_zone(self, zone: int) -> None:
        """Zóna parancs küldése BLE-n, szükség esetén újracsatlakozással.

        A reconnect non-blocking: ha az utolsó kísérlet óta még nem telt el
        reconnect_interval másodperc, a parancsot kihagyja (nem blokkolja
        a zone_queue olvasását).

        Args:
            zone: Ventilátor zóna szintje (0–3).
        """
        if self._auth_failed:
            logger.error(
                "BLE Fan: AUTH hiba, parancs elutasítva! Javítsd a pin_code-ot."
            )
            return

        if self.last_sent == zone and self.is_connected:
            return

        if not self.is_connected:
            now = time.monotonic()
            # Csak akkor próbálunk újra, ha elég idő telt el az utolsó kísérlet óta
            if now - self._last_reconnect_attempt < self.reconnect_interval:
                return
            self._last_reconnect_attempt = now
            ok = await self._reconnect_once()
            if not ok:
                return

        await self._write_level(zone)

    async def _reconnect_once(self) -> bool:
        """Egyetlen újracsatlakozási kísérlet, sleep nélkül.

        A sleep-mentes implementáció biztosítja, hogy a zone_queue olvasása
        ne blokkolódjon hosszú reconnect várakozás miatt.

        Returns:
            True, ha az újracsatlakozás sikeres.
        """
        now = time.monotonic()

        if self._retry_reset_time is not None:
            elapsed = now - self._retry_reset_time
            if elapsed >= self.RETRY_RESET_SECONDS:
                self._retry_count = 0
                self._retry_reset_time = None
            else:
                return False

        if self._retry_count >= self.max_retries:
            if self._retry_reset_time is None:
                self._retry_reset_time = now
                logger.warning(
                    f"BLE Fan: max újracsatlakozás elérve ({self.max_retries})! "
                    f"{self.RETRY_RESET_SECONDS}s múlva újrapróbálkozik..."
                )
            return False

        self._retry_count += 1
        logger.info(
            f"BLE Fan újracsatlakozás... ({self._retry_count}/{self.max_retries})"
        )

        if self._device_address:
            return await self._connect()
        return await self._scan_and_connect()

    async def _write_level(self, zone: int) -> None:
        """LEVEL:N parancs írása a BLE GATT karakterisztikára.

        Args:
            zone: Ventilátor zóna szintje (0–3).
        """
        client = self._client
        if client is None or not client.is_connected:
            self.is_connected = False
            self._client = None
            return

        try:
            msg = f"LEVEL:{zone}"
            await asyncio.wait_for(
                client.write_gatt_char(
                    self.characteristic_uuid,
                    msg.encode("utf-8"),
                ),
                timeout=self.command_timeout,
            )
            self.last_sent = zone
            self.last_sent_time = time.monotonic()
            logger.info(f"BLE Fan parancs elküldve: {msg}")

        except asyncio.TimeoutError:
            logger.error(f"BLE Fan parancs küldés timeout ({self.command_timeout}s)")
            self.is_connected = False
            try:
                await client.disconnect()
            except Exception as exc:
                logger.debug(f"BLE disconnect hiba timeout után: {exc}")
            self._client = None

        except Exception as exc:
            logger.error(f"BLE Fan küldési hiba: {exc}")
            self.is_connected = False
            try:
                await client.disconnect()
            except Exception as exc2:
                logger.debug(f"BLE disconnect hiba küldési hiba után: {exc2}")
            self._client = None

    async def disconnect(self) -> None:
        """Bontja a BLE kapcsolatot és felszabadítja a klienst."""
        client = self._client
        if client is not None:
            try:
                await asyncio.wait_for(
                    client.disconnect(),
                    timeout=self.DISCONNECT_TIMEOUT,
                )
            except Exception as exc:
                logger.debug(f"BLE disconnect hiba: {exc}")
            finally:
                self.is_connected = False
                self._client = None


# ============================================================
# ANT+ BEMENŐ ADATKEZELÉS
# ============================================================


def _ant_log_path() -> str:
    """Visszaadja az ant_devices.log teljes útvonalát a konfigurált log könyvtárban."""
    return os.path.join(_log_dir, "ant_devices.log")


def _log_ant_device_to_file(
    device_type: str,
    device_id: int,
    device_info: str,
) -> None:
    """Talált ANT+ eszközt ír az ant_devices.log fájlba (append módban).

    Csak akkor ír, ha az eszköz (device_type + device_id) még nem szerepel
    a fájlban. Ha a fájl nem létezik, létrehozza.

    Args:
        device_type: Az eszköz típusa (pl. "PowerMeter", "HeartRate").
        device_id: Az ANT+ device number.
        device_info: Egyéb információ az eszközről.
    """
    # Egyedi kulcs: "típus | device_id"
    entry_key = f"{device_type} | {device_id}"

    # Meglévő bejegyzések ellenőrzése
    existing_entries: set[str] = set()
    try:
        with open(_ant_log_path(), "r", encoding="utf-8") as f:
            for line in f:
                # Sorok formátuma: "  TÍPUS | DEVICE_ID | info"
                parts = line.split("|")
                if len(parts) >= 2:
                    existing_entries.add(f"{parts[0].strip()} | {parts[1].strip()}")
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning(f"Nem sikerült olvasni a {_ant_log_path()} fájlt: {exc}")

    if entry_key in existing_entries:
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(_ant_log_path(), "a", encoding="utf-8") as f:
            f.write(
                f"  {device_type:20s} | {device_id} | {device_info} "
                f"| @ {timestamp}\n"
            )
    except OSError as exc:
        logger.warning(f"Nem sikerült írni a {_ant_log_path()} fájlba: {exc}")


class ANTPlusInputHandler:
    """ANT+ power és HR adatforrás kezelője saját daemon szálban.

    Az openant könyvtár blokkoló API-t használ, ezért saját daemon szálban fut.
    Az érkező adatokat az asyncio event loop-ba hídalkotja
    (asyncio.run_coroutine_threadsafe) és az asyncio queue-kba teszi.

    Ha a settings-ben ant_power_device_id / ant_hr_device_id meg van adva
    (és nem 0), specifikus eszközhöz csatlakozik. Ha 0, az első elérhető
    (wildcard) eszközt használja.

    Attribútumok:
        power_queue: asyncio.Queue a power adatokhoz.
        hr_queue: asyncio.Queue a HR adatokhoz.
        loop: A fő asyncio event loop referenciája.
    """

    MAX_RETRY_COOLDOWN = 30
    WATCHDOG_TIMEOUT = 30  # Ha ennyi mp-ig nincs adat, a node-ot leállítjuk

    def __init__(
        self,
        settings: Dict[str, Any],
        power_queue: asyncio.Queue[float],
        hr_queue: asyncio.Queue[float],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.settings = settings
        self.ds: DatasourceConfig = settings["datasource"]
        self.hr_enabled: bool = settings["heart_rate_zones"].enabled
        self.power_queue = power_queue
        self.hr_queue = hr_queue
        self.loop = loop

        # ANT+ device ID-k (0 = wildcard / első elérhető)
        self._power_device_id: int = self.ds.ant_power_device_id
        self._hr_device_id: int = self.ds.ant_hr_device_id

        # Reconnect beállítások a settings-ből (a kettő közül a nagyobbat használja,
        # mert a power és HR egyetlen közös ANT+ szálban fut)
        self._reconnect_delay: int = max(
            self.ds.ant_power_reconnect_interval,
            self.ds.ant_hr_reconnect_interval,
        )
        self._max_retries: int = max(
            self.ds.ant_power_max_retries,
            self.ds.ant_hr_max_retries,
        )

        self._running = threading.Event()
        self._stop_event = threading.Event()  # watchdog leállító jelzés
        self._node: Optional[Any] = None
        self._devices: list[Any] = []
        self._lastdata: float = 0.0  # utolsó bármilyen adat ideje (thread loop használja)
        self._node_started: float = 0.0  # node.start() indulási ideje (watchdog-hoz)
        self.power_lastdata: float = 0.0
        self.hr_lastdata: float = 0.0
        self.power_connected: bool = False
        self.hr_connected: bool = False

    def start(self) -> threading.Thread:
        """Elindítja az ANT+ daemon szálat.

        Returns:
            A létrehozott daemon threading.Thread objektum.
        """
        self._running.set()
        self._stop_event.clear()  # watchdog újraindulásához
        t = threading.Thread(
            target=self._thread_loop, daemon=True, name="ANTPlus-Thread"
        )
        t.start()

        # Indulási log: milyen device ID-kkal indul
        power_src = self.ds.power_source
        hr_src = self.ds.hr_source
        if power_src == DataSource.ANTPLUS:
            pid = self._power_device_id
            mode = f"device_id={pid}" if pid else "wildcard (első elérhető)"
            user_logger.info(f"ANT+ Power keresés indítva – {mode}")
        if hr_src == DataSource.ANTPLUS and self.hr_enabled:
            hid = self._hr_device_id
            mode = f"device_id={hid}" if hid else "wildcard (első elérhető)"
            user_logger.info(f"ANT+ HR keresés indítva – {mode}")

        return t

    def stop(self) -> None:
        """Leállítja az ANT+ szálat és az ANT+ node-ot."""
        self._running.clear()
        self._stop_event.set()  # watchdog szál felébresztése és leállítása
        self._stop_node()

    def _put_power(self, power: float) -> None:
        """Power értéket tesz az asyncio queue-ba (thread-safe)."""
        try:
            asyncio.run_coroutine_threadsafe(self.power_queue.put(power), self.loop)
        except RuntimeError:
            pass  # Loop már leállt – shutdown közben normális

    def _put_hr(self, hr: int) -> None:
        """HR értéket tesz az asyncio queue-ba (thread-safe)."""
        try:
            asyncio.run_coroutine_threadsafe(self.hr_queue.put(hr), self.loop)
        except RuntimeError:
            pass  # Loop már leállt – shutdown közben normális

    def _on_any_broadcast(self, data: Any) -> None:
        """Watchdog heartbeat: minden beérkező ANT+ broadcast frissíti az időbélyeget.

        Az openant on_update callbackje minden adatcsomagnál hívódik,
        függetlenül attól, hogy az event count változott-e (tehát akkor is,
        ha a power meter 0W-ot küld mert a felhasználó nem teker).
        Ez biztosítja, hogy a watchdog ne detektáljon false positive-ot.
        """
        self._lastdata = time.monotonic()

    def _on_data(self, page: Any, page_name: str, data: Any) -> None:
        """ANT+ adatcsomag callback – power és HR adatokat irányít a queue-kba.

        Csak akkor hívódik, ha ÚJ mérési adat érkezett (event count változott).
        A watchdog heartbeat-et az _on_any_broadcast kezeli külön.
        """
        if not _ANTPLUS_AVAILABLE:
            return
        now = time.monotonic()
        if isinstance(data, PowerData):
            self.power_lastdata = now
            self._put_power(data.instantaneous_power)
        elif isinstance(data, HeartRateData):
            self.hr_lastdata = now
            self._put_hr(data.heart_rate)

    def _make_on_found(
        self, sensor_label: str, device_type_str: str, device_ref: Any
    ) -> Any:
        """Létrehoz egy on_found callbacket az adott szenzorhoz.

        Az openant on_found() paraméter nélkül hívódik (staticmethod).
        A device_ref az openant device objektum referenciája, amelyen
        a device_id attribútum elérhető (wildcard esetén az openant
        automatikusan beállítja az első talált eszköz ID-jára).

        Args:
            sensor_label: Log prefix (pl. "ANT+ Power").
            device_type_str: Logfájl eszköz típus (pl. "PowerMeter").
            device_ref: Az openant device objektum referenciája.

        Returns:
            Paraméter nélküli callback függvény.
        """
        def _on_found() -> None:
            if "Power" in sensor_label:
                self.power_connected = True
            else:
                self.hr_connected = True
            dev_id = getattr(device_ref, "device_id", 0)
            dev_name = getattr(device_ref, "name", "")
            info = dev_name or sensor_label
            logger.info(f"{sensor_label} eszköz megtalálva: id={dev_id} ({info})")
            user_logger.info(f"\u2713 {sensor_label} csatlakozva: id={dev_id} ({info})")
            _log_ant_device_to_file(device_type_str, dev_id, info)
        return _on_found

    def _init_node(self) -> None:
        """Inicializálja az ANT+ node-ot és regisztrálja az eszközöket.

        Ha ant_power_device_id / ant_hr_device_id meg van adva (nem 0),
        specifikus eszközhöz csatlakozik. Ha 0, wildcard mód (első elérhető).
        """
        if not _ANTPLUS_AVAILABLE:
            raise RuntimeError("openant könyvtár nem elérhető")
        node = Node()
        assert node is not None  # Pylance: Node() mindig valid objektumot ad
        node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)
        self._node = node
        self._devices = []

        if self.ds.power_source == DataSource.ANTPLUS:
            pid = self._power_device_id
            meter = PowerMeter(self._node, device_id=pid)
            meter.on_found = self._make_on_found("ANT+ Power", "PowerMeter", meter)
            meter.on_device_data = self._on_data
            meter.on_update = self._on_any_broadcast
            self._devices.append(meter)

        if self.ds.hr_source == DataSource.ANTPLUS and self.hr_enabled:
            hid = self._hr_device_id
            hr_monitor = HeartRate(self._node, device_id=hid)
            hr_monitor.on_found = self._make_on_found("ANT+ HR", "HeartRate", hr_monitor)
            hr_monitor.on_device_data = self._on_data
            hr_monitor.on_update = self._on_any_broadcast
            self._devices.append(hr_monitor)

    def _stop_node(self) -> None:
        """Leállítja és felszabadítja az ANT+ node-ot.

        A connected flag-eket visszaállítja False-ra, mert az openant
        on_lost callbackje nem létezik.
        """
        self.power_connected = False
        self.hr_connected = False
        try:
            for d in self._devices:
                try:
                    d.close_channel()
                except Exception as exc:
                    logger.debug(f"ANT+ csatorna bezárási hiba: {exc}")
            if self._node:
                self._node.stop()
                self._node = None
            self._devices = []
        except Exception as exc:
            logger.debug(f"ANT+ cleanup hiba: {exc}")

    def _watchdog(self) -> None:
        """Watchdog szál: ha az ANT+ node fut, de sokáig nem jön adat, leállítja.

        Az openant Node.start() blokkoló hívás, és USB megszakadás esetén
        NEM tér vissza (a belső _main loop üres queue-ból olvas örökké).
        Ez a watchdog detektálja a helyzetet és kívülről hívja a node.stop()-ot,
        ami lehetővé teszi a _thread_loop retry logikájának lefutását.
        """
        # _running.is_set() == True normális működésnél, ezért NEM használható
        # wait(timeout)-ra (azonnal visszatérne). Helyette _stop_event-et
        # használunk, ami CSAK leálláskor lesz set.
        stop_event = self._stop_event
        while not stop_event.wait(timeout=5):
            node = self._node
            if node is None:
                continue

            now = time.monotonic()
            started = self._node_started
            last = self._lastdata

            # Ha a node fut és volt már sikeres adat, de azóta WATCHDOG_TIMEOUT
            # ideje nem jött semmi → valószínűleg USB megszakadás
            if last > 0 and (now - last) > self.WATCHDOG_TIMEOUT:
                user_logger.warning(
                    f"⚠ ANT+ {self.WATCHDOG_TIMEOUT}s óta nincs adat – "
                    f"kapcsolat megszakadt, újracsatlakozás..."
                )
                self._lastdata = 0.0  # Megakadályozza az ismételt triggerelést
                try:
                    node.stop()
                except Exception as exc:
                    logger.debug(f"ANT+ watchdog node.stop() hiba: {exc}")
            # Ha a node elindul, de WATCHDOG_TIMEOUT * 2 ideje nem jött semmi adat
            # (pl. rossz device_id, vagy az eszköz soha nem volt hatótávolságban)
            elif last == 0.0 and started > 0 and (now - started) > self.WATCHDOG_TIMEOUT * 2:
                user_logger.warning(
                    f"⚠ ANT+ {self.WATCHDOG_TIMEOUT * 2}s óta nem található eszköz, "
                    f"újrapróbálkozás..."
                )
                self._node_started = 0.0  # Megakadályozza az ismételt triggerelést
                try:
                    node.stop()
                except Exception as exc:
                    logger.debug(f"ANT+ watchdog node.stop() hiba: {exc}")

    def _thread_loop(self) -> None:
        """Az ANT+ szál fő ciklusa – újracsatlakozási logikával.

        Egy watchdog szálat is indít, ami figyeli, hogy jön-e adat. Ha az
        USB ANT+ stick megszakad, az openant Node.start() nem tér vissza
        magától – a watchdog kívülről hívja a node.stop()-ot, ami feloldja
        a blokkolást.
        """
        # Watchdog szál indítása
        watchdog = threading.Thread(
            target=self._watchdog, daemon=True, name="ANTPlus-Watchdog"
        )
        watchdog.start()

        retry_count = 0
        while self._running.is_set():
            try:
                self._init_node()
                self._lastdata = 0.0
                self._node_started = time.monotonic()
                if self._node is None:
                    raise RuntimeError("Node inicializálás sikertelen")
                self._node.start()  # Blokkoló hívás – itt vár, amíg az ANT+ node fut

                if not self._running.is_set():
                    break

                # Ha volt sikeres adat, reseteljük a számolót
                if self._lastdata > 0:
                    retry_count = 0
                    user_logger.info("ANT+ kapcsolat megszakadt, újracsatlakozás...")
                else:
                    retry_count += 1
                    user_logger.warning(
                        f"⚠ ANT+ eszköz nem válaszol "
                        f"({retry_count}/{self._max_retries})"
                    )

            except Exception as exc:
                if not self._running.is_set():
                    break
                retry_count += 1
                user_logger.warning(
                    f"⚠ ANT+ hiba ({retry_count}/{self._max_retries}): {exc}"
                )

            if not self._running.is_set():
                break

            if retry_count >= self._max_retries:
                user_logger.warning(
                    f"⚠ ANT+ {self._max_retries} sikertelen próbálkozás, "
                    f"{self.MAX_RETRY_COOLDOWN}s várakozás az újraindítás előtt..."
                )
                time.sleep(self.MAX_RETRY_COOLDOWN)
                if not self._running.is_set():
                    break
                retry_count = 0
                user_logger.info("ANT+ keresés újraindítása...")

            self._stop_node()
            self._node_started = 0.0
            time.sleep(self._reconnect_delay)

        self._stop_node()
        user_logger.info("ANT+ leállítva")


# ============================================================
# BLE SZENZOR KÖZÖS ŐSOSZTÁLY (DRY)
# ============================================================


class _BLESensorInputHandler(abc.ABC):
    """Közös ősosztály BLE szenzor handlerekhez (Power, HR).

    Asyncio korrutin alapú implementáció. A scan, csatlakozás, notification
    subscribe és retry/reconnect logika itt van, az alosztályok csak a
    szenzor-specifikus konstansokat és az adat-parse-olást definiálják.

    Alosztályoknak felül kell írniuk:
        SERVICE_UUID: A BLE service UUID string.
        MEASUREMENT_UUID: A BLE measurement characteristic UUID string.
        _sensor_label: Rövid név logokhoz (pl. "BLE Power").
        _settings_prefix: Settings kulcs prefix (pl. "ble_power").
        _parse_notification(data): Nyers bájt → szám konverzió.

    Attribútumok:
        device_name: A keresett BLE eszköz neve (None = auto-discovery).
        is_connected: True, ha a BLE kapcsolat aktív.
        lastdata: Utolsó sikeres adat időbélyege (time.monotonic).
    """

    SERVICE_UUID: str
    MEASUREMENT_UUID: str
    _sensor_label: str
    _settings_prefix: str
    RETRY_RESET_SECONDS = 30

    def __init__(
        self, settings: Dict[str, Any], queue: asyncio.Queue[float]
    ) -> None:
        ds: DatasourceConfig = settings["datasource"]
        pfx = self._settings_prefix
        self.device_name: Optional[str] = getattr(ds, f"{pfx}_device_name")
        self.scan_timeout: int = getattr(ds, f"{pfx}_scan_timeout", 10)
        self.reconnect_interval: int = getattr(ds, f"{pfx}_reconnect_interval", 5)
        self.max_retries: int = getattr(ds, f"{pfx}_max_retries", 10)
        self._queue = queue
        self.is_connected = False
        self._retry_count = 0
        self.lastdata = 0.0

    @abc.abstractmethod
    def _parse_notification(self, data: bytes) -> Optional[float]:
        """Nyers BLE notification bájtokból kinyeri a mért értéket.

        Returns:
            A kinyert érték (float), vagy None ha az adat érvénytelen/túl rövid.
        """
        ...

    async def run(self) -> None:
        """A BLE szenzor fogadó fő korrutinja – újracsatlakozási logikával.

        Ha nincs device_name, automatikusan keres a SERVICE_UUID alapján
        hirdető eszközt, és folyamatosan próbálkozik, amíg talál egyet.
        """
        label = self._sensor_label
        if not _BLEAK_AVAILABLE:
            logger.error(f"{label}: bleak könyvtár nem elérhető!")
            return

        if self.device_name:
            logger.info(f"{label} fogadó elindítva: {self.device_name}")
        else:
            user_logger.info(f"\U0001f4e1 {label}: nincs eszköznév megadva, automatikus felderítés...")
            logger.info(f"{label} fogadó elindítva (auto-discovery mód)")

        while True:
            try:
                await self._scan_and_subscribe()
                # _scan_and_subscribe normálisan tért vissza (pl. a BLE eszköz
                # lekapcsolódott de a connect/subscribe sikeres volt).
                # Rövid várakozás az újracsatlakozás előtt, hogy ne legyen
                # gyors végtelen loop ha az eszköz ismételten megszakad.
                self._retry_count = 0
                self.is_connected = False
                logger.info(
                    f"{label} kapcsolat megszakadt, újracsatlakozás "
                    f"{self.reconnect_interval}s múlva..."
                )
                await asyncio.sleep(self.reconnect_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._retry_count += 1
                self.is_connected = False
                logger.warning(
                    f"{label} kapcsolat hiba "
                    f"({self._retry_count}/{self.max_retries}): {exc}"
                )
                if self._retry_count >= self.max_retries:
                    logger.warning(
                        f"{label}: max újracsatlakozás elérve, "
                        f"{self.RETRY_RESET_SECONDS}s várakozás..."
                    )
                    await asyncio.sleep(self.RETRY_RESET_SECONDS)
                    self._retry_count = 0
                else:
                    await asyncio.sleep(self.reconnect_interval)

    async def _scan_and_subscribe(self) -> None:
        """BLE eszköz keresése, csatlakozás, notification feliratkozás.

        Ha device_name megadva: név alapján keres.
        Ha device_name üres: auto-discovery a SERVICE_UUID alapján.
        """
        if not _BLEAK_AVAILABLE:
            return

        label = self._sensor_label
        addr = None

        if self.device_name:
            # --- Név alapú keresés ---
            logger.info(f"{label} keresés: {self.device_name}...")
            devices = await BleakScanner.discover(timeout=self.scan_timeout)
            for d in devices:
                if d.name == self.device_name:
                    addr = d.address
                    logger.info(f"{label} eszköz megtalálva: {d.name} ({d.address})")
                    break
                if d.name is None:
                    logger.debug(f"BLE eszköz név nélkül: {d.address}")
            if not addr:
                raise Exception(f"{label} eszköz nem található: {self.device_name}")
        else:
            # --- Automatikus felderítés service UUID alapján ---
            matched, _ = await _scan_ble_with_autodiscovery(
                self.scan_timeout,
                self.SERVICE_UUID,
                f"{label} (auto)",
            )
            if matched is None:
                raise Exception(
                    f"{label}: nem található szolgáltatás eszköz – "
                    "újrapróbálkozás..."
                )
            addr = matched.address
            user_logger.info(
                f"\u2713 {label} auto-csatlakozás: "
                f"{matched.name or '(névtelen)'} ({matched.address})"
            )

        async with BleakClient(addr) as client:
            self.is_connected = True
            self._retry_count = 0
            logger.info(f"{label} csatlakozva: {addr}")

            def _handler(sender: Any, data: bytes) -> None:
                try:
                    value = self._parse_notification(data)
                    if value is None:
                        return
                    self.lastdata = time.monotonic()
                    try:
                        self._queue.put_nowait(value)
                    except asyncio.QueueFull:
                        logger.debug(f"{label} queue teli, adat elvetve")
                except Exception as exc:
                    logger.warning(f"{label} notification hiba: {exc}")

            await client.start_notify(self.MEASUREMENT_UUID, _handler)
            while client.is_connected:
                await asyncio.sleep(1)
            # stop_notify felesleges bontott kapcsolaton – a context manager kezeli

        self.is_connected = False


# ============================================================
# BLE POWER BEMENŐ ADATKEZELÉS
# ============================================================


class BLEPowerInputHandler(_BLESensorInputHandler):
    """BLE Cycling Power Service (UUID: 0x1818) fogadó.

    Parse: flags (2 bájt LE) → instantaneous power (2 bájt LE, signed int16).
    """

    SERVICE_UUID = "00001818-0000-1000-8000-00805f9b34fb"
    MEASUREMENT_UUID = "00002a63-0000-1000-8000-00805f9b34fb"
    _sensor_label = "BLE Power"
    _settings_prefix = "ble_power"

    @property
    def power_lastdata(self) -> float:
        """Visszafelé kompatibilis alias a lastdata attribútumhoz."""
        return self.lastdata

    @power_lastdata.setter
    def power_lastdata(self, value: float) -> None:
        self.lastdata = value

    def _parse_notification(self, data: bytes) -> Optional[float]:
        if len(data) < 4:
            return None
        return float(int.from_bytes(data[2:4], byteorder="little", signed=True))


# ============================================================
# BLE HR BEMENŐ ADATKEZELÉS
# ============================================================


class BLEHRInputHandler(_BLESensorInputHandler):
    """BLE Heart Rate Service (UUID: 0x180D) fogadó.

    Parse: flags byte bit 0 → 0 = 8-bites HR, 1 = 16-bites HR.
    """

    SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
    MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
    _sensor_label = "BLE HR"
    _settings_prefix = "ble_hr"

    @property
    def hr_lastdata(self) -> float:
        """Visszafelé kompatibilis alias a lastdata attribútumhoz."""
        return self.lastdata

    @hr_lastdata.setter
    def hr_lastdata(self, value: float) -> None:
        self.lastdata = value

    def _parse_notification(self, data: bytes) -> Optional[float]:
        if len(data) < 2:
            return None
        flags = data[0]
        # bit 0: 0 = 8-bites HR, 1 = 16-bites HR
        if flags & 0x01:
            if len(data) < 3:
                return None
            return float(int.from_bytes(data[1:3], byteorder="little"))
        return float(data[1])


# ============================================================
# ZWIFT UDP BEMENŐ ADATKEZELÉS
# ============================================================


class ZwiftUDPInputHandler:
    """Zwift UDP adatforrás fogadó – asyncio DatagramProtocol alapú.

    A zwift_api_polling programból érkező JSON csomagokat fogadja UDP-n.
    Asyncio DatagramProtocol alapú implementáció, teljesen non-blocking.
    Érvényes power és HR értékeket az asyncio queue-kba teszi.

    JSON formátum:
        {"power": int, "heartrate": int}

    Attribútumok:
        process_power: True, ha a power adatokat kell feldolgozni.
        process_hr: True, ha a HR adatokat kell feldolgozni.
        last_packet_time: utolsó érvényes ZwiftUDP csomag ideje (monotonic).
    """

    def __init__(
        self,
        settings: Dict[str, Any],
        power_queue: asyncio.Queue[float],
        hr_queue: asyncio.Queue[float],
    ) -> None:
        ds: DatasourceConfig = settings["datasource"]
        self.settings = settings
        self.host: str = ds.zwift_udp_host
        self.port: int = ds.zwift_udp_port
        self.power_queue = power_queue
        self.hr_queue = hr_queue

        self.process_power: bool = ds.power_source == DataSource.ZWIFTUDP
        hr_enabled: bool = settings["heart_rate_zones"].enabled
        self.process_hr: bool = ds.hr_source == DataSource.ZWIFTUDP and hr_enabled

        self._transport: Any = None

        # HUD számára: utolsó érvényes csomag ideje
        self.last_packet_time: float = 0.0

    async def run(self) -> None:
        """A Zwift UDP fogadó fő korrutinja – asyncio DatagramProtocol-t indít."""
        loop = asyncio.get_running_loop()
        logger.info(f"Zwift UDP fogadó elindítva: {self.host}:{self.port}")

        handler = self

        class _Protocol(asyncio.DatagramProtocol):
            def connection_made(self, transport: Any) -> None:
                logger.info(f"Zwift UDP socket kötve: {handler.host}:{handler.port}")
                handler._transport = transport

            def datagram_received(self, data: bytes, addr: Any) -> None:
                handler._process_packet(data)

            def error_received(self, exc: Exception) -> None:
                logger.warning(f"Zwift UDP hiba: {exc}")

            def connection_lost(self, exc: Optional[Exception]) -> None:
                logger.info("Zwift UDP kapcsolat lezárva")

        try:
            transport, _ = await loop.create_datagram_endpoint(
                _Protocol,
                local_addr=(self.host, self.port),
            )
            try:
                while True:
                    await asyncio.sleep(3600)
            finally:
                transport.close()
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            logger.error(f"Zwift UDP bind hiba: {exc}")

    def _process_packet(self, raw: bytes) -> None:
        """JSON csomag feldolgozása – validáció és queue-ba helyezés.

        A power validációhoz a settings-ből olvassa a max_watt értéket,
        így konzisztens marad a power_processor_task szűrőjével.
        """
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if not isinstance(data, dict):
            return
        # Pylance szűkíti dict[str, Unknown]-ra; cast → dict[str, Any]
        pkt = cast(Dict[str, Any], data)

        valid_any = False

        if self.process_power and "power" in pkt:
            p: int | float = pkt["power"]
            min_watt = self.settings["power_zones"].min_watt
            max_watt = self.settings["power_zones"].max_watt
            if is_valid_power(p, min_watt, max_watt):
                try:
                    self.power_queue.put_nowait(round(p))
                    valid_any = True
                except asyncio.QueueFull:
                    logger.debug("Zwift UDP: power queue teli, adat elvetve")
            else:
                logger.debug(f"Zwift UDP: érvénytelen power: {p}")

        if self.process_hr and "heartrate" in pkt:
            hrz: HeartRateZonesConfig = self.settings["heart_rate_zones"]
            valid_min_hr: int = hrz.valid_min_hr
            valid_max_hr: int = hrz.valid_max_hr

            h: int | float = pkt["heartrate"]
            if is_valid_hr(h, valid_min_hr, valid_max_hr):
                try:
                    self.hr_queue.put_nowait(round(h))
                    valid_any = True
                except asyncio.QueueFull:
                    logger.debug("Zwift UDP: hr queue teli, adat elvetve")
            else:
                logger.debug(f"Zwift UDP: érvénytelen heartrate: {h}")

        # Ha bármilyen érvényes adatot elfogadtunk, frissítjük az időbélyeget
        if valid_any:
            self.last_packet_time = time.monotonic()


# ============================================================
# POWER FELDOLGOZÓ KORRUTIN
# ============================================================


async def power_processor_task(
    raw_power_queue: asyncio.Queue[float],
    state: ControllerState,
    zone_event: asyncio.Event,
    power_averager: PowerAverager,
    printer: ConsolePrinter,
    settings: Dict[str, Any],
    power_zones: Dict[int, Tuple[int, int]],
) -> None:
    """Teljesítmény adatok feldolgozása – validálás, átlagolás, állapot frissítés.

    Olvassa a raw_power_queue-t, validálja a beérkező watt értékeket,
    gördülő átlagot számít, meghatározza a zónát, frissíti a megosztott
    állapotot, majd jelzi a zone_event-tel, hogy zóna újraszámítás szükséges.

    Args:
        raw_power_queue: Nyers power adatok asyncio.Queue-ja.
        state: A megosztott vezérlő állapot.
        zone_event: asyncio.Event – beállítja, ha új átlag áll rendelkezésre.
        power_averager: PowerAverager példány.
        printer: ConsolePrinter a konzol kiíráshoz.
        settings: Betöltött beállítások dict-je.
        power_zones: Kiszámított power zóna határok.
    """
    min_watt = settings["power_zones"].min_watt
    max_watt = settings["power_zones"].max_watt
    zone_mode = get_effective_zone_mode(settings)

    logger.info("Power processor korrutin elindítva")

    while True:
        power = await raw_power_queue.get()

        if not is_valid_power(power, min_watt, max_watt):
            printer.emit("invalid_power", "⚠ FIGYELMEZTETÉS: Érvénytelen power adat!")
            continue

        power = int(power)
        now = time.monotonic()

        if zone_mode != ZoneMode.HIGHER_WINS:
            printer.emit("power_raw", f"⚡ Teljesítmény: {power} watt")

        avg_power = power_averager.add_sample(power)
        if avg_power is None:
            # Fix #39: Buffer feltöltés alatt is frissítjük a timestampet,
            # hogy a dropout checker ne jelezzen hamis kiesést
            async with state.lock:
                state.last_power_time = now
            continue

        avg_power = round(avg_power)
        new_power_zone = zone_for_power(avg_power, power_zones)

        if zone_mode == ZoneMode.HIGHER_WINS:
            printer.emit(
                "power_avg_hw",
                f"⚡ Átlag teljesítmény: {avg_power} watt | Power zóna: {new_power_zone} | Higher Wins!",
            )
        else:
            printer.emit(
                "power_avg",
                f"⚡ Átlag teljesítmény: {avg_power} watt | Power zóna: {new_power_zone}",
            )

        async with state.lock:
            state.last_power_time = now
            state.current_power_zone = new_power_zone
            state.current_avg_power = avg_power
            # Fix #1: UI snapshot frissítése lock alatt – konzisztens pillanatfelvétel
            state.ui_snapshot.update(
                state.current_zone,
                float(avg_power),
                state.current_avg_hr,
            )

        zone_event.set()  # Zone controller újraszámítást igényel


# ============================================================
# HR FELDOLGOZÓ KORRUTIN
# ============================================================


async def hr_processor_task(
    raw_hr_queue: asyncio.Queue[float],
    state: ControllerState,
    zone_event: asyncio.Event,
    hr_averager: HRAverager,
    printer: ConsolePrinter,
    settings: Dict[str, Any],
    hr_zones: Dict[str, int],
) -> None:
    """HR adatok feldolgozása – validálás, átlagolás, állapot frissítés.

    Olvassa a raw_hr_queue-t, validálja a bpm értékeket, gördülő átlagot
    számít, meghatározza a HR zónát, frissíti a megosztott állapotot, majd
    jelzi a zone_event-tel, hogy zóna újraszámítás szükséges.

    Frissíti a state.last_hr_time mezőt, amelyet a dropout checker
    hr_only és higher_wins módban figyelembe vesz.

    Args:
        raw_hr_queue: Nyers HR adatok asyncio.Queue-ja.
        state: A megosztott vezérlő állapot.
        zone_event: asyncio.Event – beállítja, ha új átlag áll rendelkezésre.
        hr_averager: HRAverager példány.
        printer: ConsolePrinter a konzol kiíráshoz.
        settings: Betöltött beállítások dict-je.
        hr_zones: Kiszámított HR zóna határok.
    """
    hrz: HeartRateZonesConfig = settings["heart_rate_zones"]
    zone_mode = get_effective_zone_mode(settings)
    valid_min_hr: int = hrz.valid_min_hr
    valid_max_hr: int = hrz.valid_max_hr

    logger.info("HR processor korrutin elindítva")

    while True:
        hr = await raw_hr_queue.get()

        try:
            hr = int(hr)
        except (TypeError, ValueError):
            continue
        if not is_valid_hr(hr, valid_min_hr, valid_max_hr):
            continue

        # Egyetlen now a ciklus elejéhez – konzisztens timestamp az egész iterációban
        now = time.monotonic()

        if not hr_enabled:
            printer.emit("hr_disabled", f"❤ Szívfrekvencia: {hr} bpm")
            async with state.lock:
                state.last_hr_time = now
            continue

        if zone_mode in (ZoneMode.HR_ONLY, ZoneMode.POWER_ONLY):
            printer.emit("hr_raw", f"❤ HR: {hr} bpm")

        avg_hr = hr_averager.add_sample(hr)

        if avg_hr is None:
            # Buffer feltöltés alatt is frissítjük a timestampet (dropout checker számára)
            async with state.lock:
                state.last_hr_time = now
            continue

        avg_hr = round(avg_hr)
        new_hr_zone = zone_for_hr(avg_hr, hr_zones)

        if zone_mode == ZoneMode.HR_ONLY:
            printer.emit(
                "hr_avg",
                f"❤ Átlag HR: {avg_hr} bpm | HR zóna: {new_hr_zone}",
            )
        elif zone_mode == ZoneMode.HIGHER_WINS:
            printer.emit(
                "hr_avg_hw",
                f"❤ Átlag HR: {avg_hr} bpm | HR zóna: {new_hr_zone} | Higher Wins!",
            )

        async with state.lock:
            state.last_hr_time = now
            state.current_hr_zone = new_hr_zone
            state.current_avg_hr = float(avg_hr)
            # Fix #1: UI snapshot frissítése lock alatt – konzisztens pillanatfelvétel
            state.ui_snapshot.update(
                state.current_zone,
                state.current_avg_power,
                float(avg_hr),
            )

        zone_event.set()  # Zone controller újraszámítást igényel


# ============================================================
# ZÓNA VEZÉRLŐ KORRUTIN
# ============================================================


async def zone_controller_task(
    state: ControllerState,
    zone_queue: asyncio.Queue[int],
    cooldown_ctrl: CooldownController,
    settings: Dict[str, Any],
    zone_event: asyncio.Event,
) -> None:
    """Zóna vezérlő – kombinálja a power és HR zónákat, alkalmazza a cooldownt.

    Megvárja a zone_event jelzést (amelyet a power és HR processorok állítanak be),
    majd a legfrissebb állapot alapján:
    1. Meghatározza a final zónát (apply_zone_mode / higher_wins)
    2. Alkalmazza a cooldown logikát (CooldownController)
    3. Ha szükséges, elküldi a zóna parancsot a BLE fan queue-ba

    Megjegyzés higher_wins módban: ha hr_zone None (az átlagoló még nem gyűjtött
    elég mintát), de hr_fresh True, az apply_zone_mode csak a power_zone-t
    használja – ez szándékos viselkedés.

    Args:
        state: A megosztott vezérlő állapot.
        zone_queue: BLE fan output asyncio.Queue-ja.
        cooldown_ctrl: CooldownController példány.
        settings: Betöltött beállítások dict-je.
        zone_event: asyncio.Event – jelzi, hogy új adat érkezett.
    """
    zone_mode = get_effective_zone_mode(settings)
    zero_power_immediate = settings["power_zones"].zero_power_immediate
    zero_hr_immediate = settings["heart_rate_zones"].zero_hr_immediate
    power_buf = _resolve_buffer_settings(settings, "power")
    hr_buf = _resolve_buffer_settings(settings, "hr")
    power_dropout_timeout = power_buf["dropout_timeout"]
    hr_dropout_timeout = hr_buf["dropout_timeout"]

    logger.info("Zóna vezérlő korrutin elindítva")

    while True:
        await zone_event.wait()
        zone_event.clear()
        # Állapot pillanatfelvétel (lock alatt)
        async with state.lock:
            power_zone = state.current_power_zone
            hr_zone = state.current_hr_zone
            current_zone = state.current_zone
            now = time.monotonic()
            last_power = state.last_power_time
            last_hr = state.last_hr_time

        # Frissesség ellenőrzése (dropout figyelembe vételéhez)
        # Fix #3: last_power_time most Optional – None = még nem érkezett adat
        power_fresh = (
            last_power is not None
            and (now - last_power) < power_dropout_timeout
        )
        hr_fresh = last_hr is not None and (now - last_hr) < hr_dropout_timeout

        # Zóna kombinálás a zone_mode alapján
        if zone_mode == ZoneMode.POWER_ONLY:
            final_zone = power_zone if power_fresh else None
        elif zone_mode == ZoneMode.HR_ONLY:
            final_zone = hr_zone if hr_fresh else None
        else:  # higher_wins
            p = power_zone if power_fresh else None
            h = hr_zone if hr_fresh else None
            final_zone = apply_zone_mode(p, h, zone_mode)

        if final_zone is None:
            continue  # Nincs elég friss adat a döntéshez

        # Azonnali leállás flag (zero_power_immediate / zero_hr_immediate)
        use_zero_immediate = (
            (zero_power_immediate and power_zone is not None and power_zone == 0 and power_fresh)
            or (zero_hr_immediate and hr_zone is not None and hr_zone == 0 and hr_fresh)
        )

        # Cooldown logika alkalmazása
        zone_to_send = cooldown_ctrl.process(current_zone, final_zone, use_zero_immediate)

        if zone_to_send is not None:
            async with state.lock:
                state.current_zone = zone_to_send
                # Fix #1: UI snapshot frissítése lock alatt – konzisztens pillanatfelvétel
                state.ui_snapshot.update(
                    zone_to_send,
                    state.current_avg_power,
                    state.current_avg_hr,
                )
            await send_zone(zone_to_send, zone_queue)
            user_logger.info(f"→ Zóna elküldve: LEVEL:{zone_to_send}")


# ============================================================
# DROPOUT ELLENŐRZŐ KORRUTIN
# ============================================================


async def dropout_checker_task(
    state: ControllerState,
    zonequeue: asyncio.Queue[int],
    settings: Dict[str, Any],
    poweraverager: PowerAverager,
    hraverager: HRAverager,
    power_dropout_timeout: float,
    hr_dropout_timeout: float,
    zone_mode: ZoneMode,
    cooldown_ctrl: CooldownController,
) -> None:
    """Adatforrás kiesés detektálása, Z0 küldése és pufferek ürítése.

    Args:
        state: A megosztott vezérlő állapot.
        zonequeue: BLE fan output asyncio.Queue-ja.
        settings: Betöltött beállítások dict-je.
        poweraverager: PowerAverager példány (ürítéshez).
        hraverager: HRAverager példány (ürítéshez).
        power_dropout_timeout: Power forrás timeout másodpercben.
        hr_dropout_timeout: HR forrás timeout másodpercben.
        zone_mode: Aktív zóna mód (paraméterként kapja, nem számolja újra).
        cooldown_ctrl: CooldownController példány (dropout-kor reseteléshez).
    """
    logger.info("Dropout checker korrutin elindítva")

    while True:
        await asyncio.sleep(1)
        now = time.monotonic()
        send_dropout = False

        # Fix #2: Egyetlen lock blokk az egész ellenőrzéshez
        async with state.lock:
            if state.current_zone is None or state.current_zone == 0:
                continue

            # Fix #3: last_power_time Optional – None = még nem érkezett adat
            power_fresh = (
                state.last_power_time is not None
                and (now - state.last_power_time) < power_dropout_timeout
            )
            hr_fresh = (
                state.last_hr_time is not None
                and (now - state.last_hr_time) < hr_dropout_timeout
            )

            label = "unknown"
            elapsed = 0.0
            stale = False

            if zone_mode == ZoneMode.POWER_ONLY:
                stale = not power_fresh
                elapsed = (
                    now - state.last_power_time
                    if state.last_power_time is not None
                    else float("inf")
                )
                label = "power"
            elif zone_mode == ZoneMode.HR_ONLY:
                elapsed = (
                    now - state.last_hr_time
                    if state.last_hr_time is not None
                    else float("inf")
                )
                # Fix #4: hr_only dropout akkor is triggerel, ha soha nem érkezett HR
                stale = not hr_fresh
                label = "HR"
            else:  # higher_wins
                stale = not power_fresh and not hr_fresh

                if stale:
                    elapsed = max(
                        (
                            now - state.last_power_time
                            if state.last_power_time is not None
                            else float("inf")
                        ),
                        (
                            now - state.last_hr_time
                            if state.last_hr_time is not None
                            else float("inf")
                        ),
                    )
                elif not power_fresh:
                    elapsed = (
                        now - state.last_power_time
                        if state.last_power_time is not None
                        else float("inf")
                    )
                elif not hr_fresh:
                    elapsed = (
                        now - state.last_hr_time
                        if state.last_hr_time is not None
                        else float("inf")
                    )
                else:
                    elapsed = 0.0
                label = "power+HR"

            if stale:
                user_logger.info(f"Adatforrás kiesett ({label}), {elapsed:.1f}s → LEVEL:0")
                if not power_fresh:
                    poweraverager.clear()
                    state.current_avg_power = None
                    state.current_power_zone = None
                if not hr_fresh:
                    hraverager.clear()
                    state.current_avg_hr = None
                    state.current_hr_zone = None
                state.current_zone = 0
                # Fix #28: Cooldown állapot resetelése dropout-kor
                cooldown_ctrl.reset()
                # Fix #40: UI snapshot frissítése – a HUD is lássa a dropout-ot
                state.ui_snapshot.update(0, state.current_avg_power, state.current_avg_hr)
                send_dropout = True

        if send_dropout:
            await send_zone(0, zonequeue)


class BLECombinedSensor:
    def __init__(self, power_handler: Optional[Any] = None, hr_handler: Optional[Any] = None):
        self.power_handler = power_handler
        self.hr_handler = hr_handler

    @property
    def power_lastdata(self):
        if self.power_handler:
            return getattr(self.power_handler, "power_lastdata", 0)
        return 0

    @property
    def hr_lastdata(self):
        if self.hr_handler:
            return getattr(self.hr_handler, "hr_lastdata", 0)
        return 0


# ============================================================
# TASK WRAPPER – kivétel logoláshoz
# ============================================================


async def _guarded_task(
    coro: Any,
    name: str,
    *,
    max_retries: int = 0,
    retry_delay: float = 5.0,
    coro_factory: Any = None,
) -> None:
    """Task wrapper: elkapja és logolja a váratlan kivételeket.

    CancelledError-t tovább engedi (normál leálláshoz szükséges).
    Minden más kivételt kritikus szinten logolja, hogy ne tűnjön el csendben.

    Ha max_retries > 0 és coro_factory adott, a task automatikusan újraindul
    exponenciális backoff-fal (retry_delay * 2^attempt, max 60s).

    Args:
        coro: Az indítandó korrutin (első futáshoz).
        name: A task neve (logoláshoz).
        max_retries: Max újrapróbálkozások száma (0 = nincs retry).
        retry_delay: Kezdő várakozás másodpercben újraindítás előtt.
        coro_factory: Paraméter nélküli callable, ami új korrutint ad vissza.
            Retry-hoz kötelező, mert egy korrutin csak egyszer await-elhető.
    """
    attempt = 0
    current_coro = coro
    while True:
        try:
            await current_coro
            return  # Normál befejezés
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempt += 1
            if coro_factory is not None and attempt <= max_retries:
                delay = min(retry_delay * (2 ** (attempt - 1)), 60.0)
                logger.warning(
                    f"Task '{name}' hiba ({attempt}/{max_retries}): {exc} "
                    f"→ újraindítás {delay:.0f}s múlva",
                    exc_info=True,
                )
                await asyncio.sleep(delay)
                current_coro = coro_factory()
            else:
                logger.error(
                    f"Task '{name}' váratlanul leállt: {exc}", exc_info=True
                )
                return


# ============================================================
# FAN CONTROLLER – FŐ ÖSSZEHANGOLÁS
# ============================================================


class FanController:
    """A Smart Fan Controller fő orchestrátora.

    Összefogja az összes komponenst, elindítja az asyncio task-okat
    és a szálakat, és gondoskodik a tiszta leállításról.

    Indítási sorrend:
        1. Beállítások betöltése
        2. Zóna határok kiszámítása
        3. Átlagolók, cooldown, printer létrehozása
        4. BLE fan output asyncio task indítása
        5. BLE power/HR input asyncio task-ok indítása (ha szükséges)
        6. Zwift UDP input asyncio task indítása (ha szükséges)
        7. ANT+ szál indítása (ha szükséges)
        8. Power/HR processor asyncio task-ok indítása
        9. Zone controller asyncio task indítása
        10. Dropout checker asyncio task indítása
        11. Főciklus: Ctrl+C / SIGTERM megvárása
        12. Leállítás: minden task és szál leállítása
    """

    def __init__(self, settings_file: str = "settings.json") -> None:
        self.settings_file = settings_file
        self.settings = load_settings(settings_file)
        self._antplus_handler: Optional[ANTPlusInputHandler] = None
        self._antplus_thread: Optional[threading.Thread] = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._running = True
        self._zwift_proc: Optional[subprocess.Popen[Any]] = None
        # Handler ref-ek (HUD és leállítás számára)
        self._ble_fan: Optional[BLEFanOutputController] = None
        self._ble_power: Optional[BLEPowerInputHandler] = None
        self._ble_hr: Optional[BLEHRInputHandler] = None
        self._zwift_udp: Optional[ZwiftUDPInputHandler] = None
        self._state: Optional[ControllerState] = None
        self._cooldown_ctrl: Optional[CooldownController] = None
        self._ble_sensor_handler: Optional[BLECombinedSensor] = None

    @property
    def state(self) -> "Optional[ControllerState]":
        """Aktuális vezérlő állapot (None ha még nem indult el a run())."""
        return self._state

    @property
    def ble_fan(self) -> "Optional[BLEFanOutputController]":
        """BLE ventilátor kimeneti vezérlő (None ha nincs)."""
        return self._ble_fan

    @property
    def cooldown_ctrl(self) -> "Optional[CooldownController]":
        """Hűtési időkorlát vezérlő (None ha még nem indult el a run())."""
        return self._cooldown_ctrl

    def __repr__(self) -> str:
        ds: DatasourceConfig = self.settings["datasource"]
        return (
            f"FanController(running={self._running}, "
            f"power_src={ds.power_source}, "
            f"hr_src={ds.hr_source}, "
            f"tasks={len(self._tasks)})"
        )

    # ----------------------------------------------------------
    # Zwift alkalmazás automatikus indítása
    # ----------------------------------------------------------

    @staticmethod
    def is_process_running(process_name: str) -> bool:
        """Ellenőrzi, hogy egy adott nevű Windows process fut-e.

        A ``tasklist`` parancsot használja, ``psutil`` nélkül.
        """
        if _platform.system() != "Windows":
            return False
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return process_name.lower() in result.stdout.lower()
        except (subprocess.TimeoutExpired, OSError):
            return False

    @staticmethod
    def _find_zwift_launcher() -> Optional[str]:
        """Megkeresi a ZwiftLauncher.exe útvonalát.

        Keresési sorrend:
          1. Windows Registry (Uninstall kulcsok)
          2. Ismert telepítési útvonalak
        """
        if _platform.system() != "Windows":
            return None

        # --- 1. Registry keresés ---
        try:
            import winreg

            uninstall_key = (
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
            )
            for root_key in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for view_flag in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                    try:
                        with winreg.OpenKey(
                            root_key, uninstall_key, 0, winreg.KEY_READ | view_flag
                        ) as key:
                            i = 0
                            while True:
                                try:
                                    subkey_name = winreg.EnumKey(key, i)
                                    i += 1
                                    with winreg.OpenKey(key, subkey_name) as subkey:
                                        try:
                                            display_name = winreg.QueryValueEx(
                                                subkey, "DisplayName"
                                            )[0]
                                        except OSError:
                                            continue
                                        if "zwift" not in str(display_name).lower():
                                            continue
                                        try:
                                            install_loc = winreg.QueryValueEx(
                                                subkey, "InstallLocation"
                                            )[0]
                                        except OSError:
                                            continue
                                        launcher = os.path.join(
                                            str(install_loc), "ZwiftLauncher.exe"
                                        )
                                        if os.path.isfile(launcher):
                                            return launcher
                                except OSError:
                                    break
                    except OSError:
                        continue
        except ImportError:
            pass  # winreg nem elérhető (nem Windows)

        # --- 2. Ismert útvonalak ---
        known_paths = [
            os.path.join(
                os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                "Zwift", "ZwiftLauncher.exe",
            ),
            os.path.join(
                os.environ.get("ProgramFiles", r"C:\Program Files"),
                "Zwift", "ZwiftLauncher.exe",
            ),
        ]
        for path in known_paths:
            if os.path.isfile(path):
                return path

        return None

    def _ensure_zwift_running(self) -> None:
        """Biztosítja, hogy a Zwift alkalmazás fut.

        Ha a ZwiftApp.exe nem fut:
          1. Megkeresi és elindítja a ZwiftLauncher.exe-t
          2. pywinauto segítségével megvárja a launcher ablakot
          3. Kezeli az esetleges frissítést (vár amíg a "Let's Go" gomb megjelenik)
          4. Rákattint a "Let's Go" gombra
          5. Megvárja amíg a ZwiftApp.exe elindul
        """
        ds: DatasourceConfig = self.settings["datasource"]
        if not ds.zwift_auto_launch:
            logger.info("Zwift auto-launch kikapcsolva a beállításokban.")
            return

        if _platform.system() != "Windows":
            logger.info("Zwift auto-launch csak Windows-on támogatott.")
            return

        # Már fut?
        if self.is_process_running("ZwiftApp.exe"):
            logger.info("ZwiftApp.exe már fut, auto-launch kihagyva.")
            return

        # Launcher útvonal meghatározása
        launcher_path: Optional[str] = ds.zwift_launcher_path
        if not launcher_path:
            launcher_path = self._find_zwift_launcher()
        if not launcher_path:
            logger.warning(
                "ZwiftLauncher.exe nem található! "
                "Állítsd be a 'zwift_launcher_path' értéket a settings.json-ben."
            )
            return

        if not os.path.isfile(launcher_path):
            logger.warning(f"ZwiftLauncher.exe nem található: {launcher_path}")
            return

        logger.info(f"Zwift indítása: {launcher_path}")
        user_logger.info(f"🚀 Zwift indítása: {launcher_path}")

        # Launcher indítása
        try:
            subprocess.Popen(
                [launcher_path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            logger.error(f"ZwiftLauncher.exe indítása sikertelen: {exc}")
            return

        # UI automatizáció (pywinauto)
        if not _PYWINAUTO_AVAILABLE:
            logger.warning(
                "pywinauto nincs telepítve – a 'Let's Go' gombra manuálisan kell "
                "kattintani. Telepítés: pip install pywinauto"
            )
            # Fallback: egyszerűen várunk a ZwiftApp.exe megjelenésére
            user_logger.info("⏳ Várakozás a ZwiftApp.exe indulására (kattints a 'Let's Go' gombra)...")
            for _ in range(180):  # max 6 perc
                time.sleep(2)
                if self.is_process_running("ZwiftApp.exe"):
                    logger.info("ZwiftApp.exe elindult.")
                    user_logger.info("✅ ZwiftApp.exe elindult!")
                    return
            logger.warning("ZwiftApp.exe nem indult el 6 perc alatt.")
            user_logger.warning("⚠️  ZwiftApp.exe nem indult el 6 perc alatt.")
            return

        # --- pywinauto automatizáció retry loop-pal ---
        # A launcher frissítés közben bezárhatja és újranyithatja az ablakot,
        # ezért retry loop-ot használunk ahelyett, hogy egyetlen connect + wait-re
        # támaszkodnánk.
        max_attempts = 10
        attempt_interval = 30  # másodperc próbálkozások között
        for attempt in range(1, max_attempts + 1):
            # Ha közben elindult a ZwiftApp.exe (pl. már be volt jelentkezve)
            if self.is_process_running("ZwiftApp.exe"):
                logger.info("ZwiftApp.exe elindult (frissítés/auto-login után).")
                user_logger.info("✅ ZwiftApp.exe elindult!")
                return

            # Ellenőrizzük, hogy a launcher process még fut-e
            if not self.is_process_running("ZwiftLauncher.exe"):
                logger.info(
                    f"ZwiftLauncher.exe nem fut (próba {attempt}/{max_attempts}). "
                    f"Lehet hogy újraindul frissítés után..."
                )
                if attempt < max_attempts:
                    time.sleep(attempt_interval)
                    continue
                else:
                    logger.warning("ZwiftLauncher.exe nem indult újra.")
                    break

            try:
                user_logger.info(
                    f"⏳ Zwift Launcher ablak keresése "
                    f"(próba {attempt}/{max_attempts})..."
                )
                app = WinAutoApp(backend="uia").connect(  # type: ignore[reportOptionalCall]
                    title="Zwift Launcher", timeout=30
                )
                window = app.top_window()  # type: ignore[reportOptionalCall]
                logger.info("Zwift Launcher ablak megtalálva.")

                # Debug: kilistázzuk az ablak összes child control-ját (beleértve webes tartalmat)
                try:
                    children = window.descendants()  # type: ignore[reportOptionalCall]
                    child_info = [
                        (c.window_text()[:50], c.friendly_class_name(), c.element_info.control_type)
                        for c in children
                    ]
                    logger.info(f"Zwift Launcher kontrollok ({len(child_info)} db): {child_info}")
                    user_logger.info(f"   🔍 Kontrollok ({len(child_info)} db):")
                    for text, cls, ctype in child_info:
                        if text.strip():
                            user_logger.info(f"      [{ctype}] {cls}: '{text}'")
                except Exception as debug_exc:
                    logger.debug(f"Kontroll lista lekérés sikertelen: {debug_exc}")

                # "LET'S GO" gomb keresése (regex: bármilyen aposztróf-típus)
                user_logger.info("⏳ Várakozás a 'LET'S GO' gombra (frissítés esetén ez eltarthat)...")
                button = window.child_window(  # type: ignore[reportOptionalCall]
                    title_re="LET.S GO", control_type="Button"
                )
                button.wait("visible", timeout=attempt_interval)  # type: ignore[reportOptionalCall]
                logger.info("'Let's Go' gomb megtalálva, kattintás...")
                button.click()  # type: ignore[reportOptionalCall]
                user_logger.info("✅ 'Let's Go' gomb megnyomva, várakozás a Zwift indulására...")
                break

            except Exception as exc:
                # Debug: kilistázzuk az összes látható ablak címét
                try:
                    from pywinauto import Desktop  # type: ignore[import-untyped]
                    desktop = Desktop(backend="uia")
                    windows = desktop.windows()
                    win_titles = [w.window_text() for w in windows if w.window_text()]
                    logger.info(f"Látható ablakok: {win_titles}")
                    user_logger.info(f"   🔍 Látható ablakok: {win_titles}")
                except Exception as debug_exc:
                    logger.debug(f"Ablak lista lekérés sikertelen: {debug_exc}")
                logger.info(
                    f"Launcher ablak/gomb nem elérhető (próba {attempt}/{max_attempts}): "
                    f"{exc}"
                )
                if attempt < max_attempts:
                    user_logger.info(
                        f"⏳ Újrapróbálkozás {attempt_interval}s múlva "
                        f"({attempt}/{max_attempts})..."
                    )
                    time.sleep(attempt_interval)
                else:
                    logger.warning(
                        f"Zwift Launcher UI automatizáció sikertelen {max_attempts} "
                        f"próba után: {exc}"
                    )
                    user_logger.warning(f"⚠️  Launcher automatizáció sikertelen: {exc}")
                    user_logger.info("    Kattints manuálisan a 'Let's Go' gombra!")

        # Várakozás a ZwiftApp.exe megjelenésére (akár manuális, akár auto kattintás után)
        if not self.is_process_running("ZwiftApp.exe"):
            user_logger.info("⏳ Várakozás a ZwiftApp.exe indulására...")
            for _ in range(120):  # max 4 perc
                time.sleep(2)
                if self.is_process_running("ZwiftApp.exe"):
                    logger.info("ZwiftApp.exe sikeresen elindult.")
                    user_logger.info("✅ ZwiftApp.exe elindult!")
                    return
            logger.warning("ZwiftApp.exe nem indult el 4 perc alatt.")
            user_logger.warning("⚠️  ZwiftApp.exe nem indult el 4 perc alatt.")

    def _start_zwift_subprocess(self, script_name: str) -> None:
        """Elindít egy Zwift subprocess-t (zwift_api_polling).

        Leállítja az esetlegesen még futó előző folyamatot, majd elindítja
        az újat. Az eredményt self._zwift_proc tartalmazza.
        """
        # Esetleges előző process leállítása
        if self._zwift_proc is not None and self._zwift_proc.poll() is None:
            try:
                self._zwift_proc.terminate()
                self._zwift_proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._zwift_proc.kill()
                self._zwift_proc.wait()  # zombie elkerülése
            except OSError:
                pass
            finally:
                self._zwift_proc = None

        try:
            if getattr(sys, "frozen", False):
                exe_dir = os.path.dirname(os.path.abspath(sys.executable))
                cmd = [os.path.join(exe_dir, f"{script_name}.exe")]
            else:
                monitor_script = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), f"{script_name}.py"
                )
                cmd = [sys.executable, monitor_script]

            if _platform.system() == "Windows":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 4  # SW_SHOWNOACTIVATE
                creation_flags = subprocess.CREATE_NEW_CONSOLE
            else:
                startupinfo = None
                creation_flags = 0

            popen_kwargs: Dict[str, Any] = dict(
                stdin=subprocess.DEVNULL,
            )
            if startupinfo is not None:
                popen_kwargs["startupinfo"] = startupinfo
                popen_kwargs["creationflags"] = creation_flags
            else:
                popen_kwargs["close_fds"] = True

            self._zwift_proc = subprocess.Popen(cmd, **popen_kwargs)
            logger.info(f"{script_name}.py elindítva (PID: {self._zwift_proc.pid})")

        except FileNotFoundError as exc:
            logger.error(f"{script_name}.py nem található: {exc}")
        except OSError as exc:
            logger.error(f"{script_name}.py indítása sikertelen: {exc}")
        except Exception as exc:
            logger.error(f"Váratlan hiba {script_name}.py indításakor: {exc}")

    def print_startup_info(self) -> None:
        """Kiírja az indítási konfigurációs összefoglalót."""
        s = self.settings
        ds: DatasourceConfig = s["datasource"]
        hrz: HeartRateZonesConfig = s["heart_rate_zones"]

        power_buf = _resolve_buffer_settings(s, "power")
        hr_buf = _resolve_buffer_settings(s, "hr")

        zone_mode = get_effective_zone_mode(s)

        user_logger.info("-" * 60)
        user_logger.info(f"  Smart Fan Controller v{__version__}  |  Power+HR → BLE Fan")
        user_logger.info("-" * 60)
        zt = s["power_zones"]
        user_logger.info(f"FTP: {zt.ftp}W | Érvényes tartomány: 0–{zt.max_watt}W")

        power_zones = calculate_power_zones(
            zt.ftp,
            zt.min_watt,
            zt.max_watt,
            zt.z1_max_percent,
            zt.z2_max_percent,
        )
        user_logger.info(f"Zóna határok: {power_zones}")

        if ds.power_source is not None:
            user_logger.info(
                f"💪 Power buffer ({ds.power_source.upper()}): "
                f"{power_buf['buffer_seconds']}s | "
                f"minta: {power_buf['minimum_samples']} | "
                f"rate: {power_buf['buffer_rate_hz']}Hz | "
                f"dropout: {power_buf['dropout_timeout']}s"
            )
        else:
            user_logger.info("💪 Power forrás: KIKAPCSOLVA (null)")
        if ds.hr_source is not None:
            user_logger.info(
                f"❤️  HR buffer    ({ds.hr_source.upper()}): "
                f"{hr_buf['buffer_seconds']}s | "
                f"minta: {hr_buf['minimum_samples']} | "
                f"rate: {hr_buf['buffer_rate_hz']}Hz | "
                f"dropout: {hr_buf['dropout_timeout']}s"
            )
        else:
            user_logger.info("❤️  HR forrás:    KIKAPCSOLVA (null)")

        user_logger.info(
            f"Cooldown: {s['global_settings'].cooldown_seconds}s  |  "
            f"0W azonnali: {'Igen' if s['power_zones'].zero_power_immediate else 'Nem'}  |  "
            f"0HR azonnali: {'Igen' if hrz.zero_hr_immediate else 'Nem'}"
        )
        ble_cfg: BleConfig = s["ble"]
        if ble_cfg.device_name:
            user_logger.info(f"BLE Fan: {ble_cfg.device_name}")
        else:
            user_logger.info("BLE Fan: (auto-discovery – service UUID alapján)")
        if ble_cfg.pin_code:
            user_logger.info(f"BLE PIN: {'*' * len(ble_cfg.pin_code)}")

        # BLE szenzor auto-discovery jelzés
        if ds.power_source == DataSource.BLE and not ds.ble_power_device_name:
            user_logger.info("BLE Power: (auto-discovery – Cycling Power Service)")
        if ds.hr_source == DataSource.BLE and not ds.ble_hr_device_name:
            user_logger.info("BLE HR: (auto-discovery – Heart Rate Service)")

        user_logger.info(f"Zónamód: {zone_mode}")
        user_logger.info("-" * 60)

    async def run(self) -> None:
        """A vezérlő fő asyncio korrutinja – elindít mindent és vár."""
        self._tasks = []
        s = self.settings
        ds: DatasourceConfig = s["datasource"]
        hrz_cfg: HeartRateZonesConfig = s["heart_rate_zones"]
        hr_enabled = hrz_cfg.enabled
        zone_mode = get_effective_zone_mode(s)

        # --- Zóna határok kiszámítása ---
        pz: PowerZonesConfig = s["power_zones"]
        power_zones = calculate_power_zones(
            pz.ftp, pz.min_watt, pz.max_watt, pz.z1_max_percent, pz.z2_max_percent,
        )
        hr_zones = (
            calculate_hr_zones(
                hrz_cfg.max_hr,
                hrz_cfg.resting_hr,
                hrz_cfg.z1_max_percent,
                hrz_cfg.z2_max_percent,
            )
            if hr_enabled
            else {"resting": 60, "z1_max": 130, "z2_max": 148}
        )

        # --- Zwift alkalmazás automatikus indítása (bármilyen adatforrás esetén) ---
        # to_thread: nem blokkolja az asyncio event loop-ot (signal kezelés, stb.)
        await asyncio.to_thread(self._ensure_zwift_running)

        # --- Komponensek létrehozása ---
        raw_power_queue: asyncio.Queue[float] = asyncio.Queue(maxsize=100)
        raw_hr_queue: asyncio.Queue[float] = asyncio.Queue(maxsize=100)
        zone_cmd_queue: asyncio.Queue[int] = asyncio.Queue(maxsize=1)
        zone_event = asyncio.Event()

        state = ControllerState()
        self._state = state
        power_buf = _resolve_buffer_settings(s, "power")
        hr_buf = _resolve_buffer_settings(s, "hr")

        power_averager = PowerAverager(
            power_buf["buffer_seconds"],
            power_buf["minimum_samples"],
            power_buf["buffer_rate_hz"],
        )
        hr_averager = HRAverager(
            hr_buf["buffer_seconds"],
            hr_buf["minimum_samples"],
            hr_buf["buffer_rate_hz"],
        )
        cooldown_ctrl = CooldownController(s["global_settings"].cooldown_seconds)
        self._cooldown_ctrl = cooldown_ctrl
        printer = ConsolePrinter()

        # --- BLE Fan Output ---
        ble_fan = BLEFanOutputController(s)
        self._ble_fan = ble_fan
        self._tasks.append(
            asyncio.create_task(
                _guarded_task(
                    ble_fan.run(zone_cmd_queue),
                    "BLEFanOutput",
                    max_retries=3,
                    retry_delay=5.0,
                    coro_factory=lambda: ble_fan.run(zone_cmd_queue),
                ),
                name="BLEFanOutput",
            )
        )

        # --- Bemeneti adatforrások ---
        power_source = ds.power_source
        hr_source = ds.hr_source

        if power_source == DataSource.BLE:
            ble_power = BLEPowerInputHandler(s, raw_power_queue)
            self._ble_power = ble_power
            self._tasks.append(
                asyncio.create_task(
                    _guarded_task(
                        ble_power.run(),
                        "BLEPowerInput",
                        max_retries=3,
                        retry_delay=5.0,
                        coro_factory=lambda: ble_power.run(),
                    ),
                    name="BLEPowerInput",
                )
            )

        if hr_source == DataSource.BLE and hr_enabled:
            ble_hr = BLEHRInputHandler(s, raw_hr_queue)
            self._ble_hr = ble_hr
            self._tasks.append(
                asyncio.create_task(
                    _guarded_task(
                        ble_hr.run(),
                        "BLEHRInput",
                        max_retries=3,
                        retry_delay=5.0,
                        coro_factory=lambda: ble_hr.run(),
                    ),
                    name="BLEHRInput",
                )
            )

        self._ble_sensor_handler = BLECombinedSensor(
            power_handler=self._ble_power, hr_handler=self._ble_hr
        )

        needs_zwift = (power_source == DataSource.ZWIFTUDP) or (
            hr_source == DataSource.ZWIFTUDP and hr_enabled
        )
        if needs_zwift:
            # Zwift API polling subprocess indítása
            self._start_zwift_subprocess("zwift_api_polling")

            # UDP handler a zwift_api_polling.py-tól érkező csomagok fogadásához
            zwiftudp = ZwiftUDPInputHandler(s, raw_power_queue, raw_hr_queue)
            self._zwift_udp = zwiftudp
            self._tasks.append(
                asyncio.create_task(
                    _guarded_task(
                        zwiftudp.run(),
                        "ZwiftUDPInput",
                        max_retries=3,
                        retry_delay=5.0,
                        coro_factory=lambda: zwiftudp.run(),
                    ),
                    name="ZwiftUDPInput",
                )
            )

        needs_antplus = (power_source == DataSource.ANTPLUS) or (
            hr_source == DataSource.ANTPLUS and hr_enabled
        )
        if needs_antplus:
            if _ANTPLUS_AVAILABLE:
                self._antplus_handler = ANTPlusInputHandler(
                    s, raw_power_queue, raw_hr_queue, asyncio.get_running_loop()
                )
                self._antplus_thread = self._antplus_handler.start()
            else:
                logger.warning(
                    "ANT+ forrás kérve, de az openant könyvtár nem elérhető!"
                )

        # --- Feldolgozó és vezérlő korrutinok ---
        if power_source is not None:
            self._tasks.append(
                asyncio.create_task(
                    _guarded_task(
                        power_processor_task(
                            raw_power_queue,
                            state,
                            zone_event,
                            power_averager,
                            printer,
                            s,
                            power_zones,
                        ),
                        "PowerProcessor",
                    ),
                    name="PowerProcessor",
                )
            )
        else:
            logger.info("Power processor kihagyva (power_source: null)")

        if hr_source is not None and hr_enabled:
            self._tasks.append(
                asyncio.create_task(
                    _guarded_task(
                        hr_processor_task(
                            raw_hr_queue,
                            state,
                            zone_event,
                            hr_averager,
                            printer,
                            s,
                            hr_zones,
                        ),
                        "HRProcessor",
                    ),
                    name="HRProcessor",
                )
            )
        else:
            logger.info("HR processor kihagyva (hr_source: %s, hr_enabled: %s)",
                        hr_source, hr_enabled)
        self._tasks.append(
            asyncio.create_task(
                _guarded_task(
                    zone_controller_task(
                        state,
                        zone_cmd_queue,
                        cooldown_ctrl,
                        s,
                        zone_event,
                    ),
                    "ZoneController",
                ),
                name="ZoneController",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                _guarded_task(
                    dropout_checker_task(
                        state,
                        zone_cmd_queue,
                        s,
                        power_averager,
                        hr_averager,
                        power_buf["dropout_timeout"],
                        hr_buf["dropout_timeout"],
                        zone_mode,
                        cooldown_ctrl,
                    ),
                    "DropoutChecker",
                ),
                name="DropoutChecker",
            )
        )

        user_logger.info("")
        user_logger.info("🚴 Figyelés elindítva... (Ctrl+C a leállításhoz)")
        user_logger.info("")

        try:
            if self._tasks:
                await asyncio.gather(*self._tasks)
            else:
                while self._running:
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            # Guard against None if setup crashed before ble_fan init
            if self._ble_fan is not None:  # type: ignore[redundant-expr]
                # Leállítás előtt LEVEL:0 küldése – ventilátor kikapcsolása
                try:
                    await self._ble_fan._write_level(0)
                    user_logger.info("✓ Ventilátor leállítva (LEVEL:0)")
                except Exception as exc:
                    logger.warning(f"LEVEL:0 küldése sikertelen leállításkor: {exc}")
                await self._ble_fan.disconnect()
                self._ble_fan = None
            # Fix #13: ANT+ leállítás a stop()-ban történik, nem duplikáljuk itt

    def stop(self) -> None:
        """Leállítja az összes task-ot és szálat.

        Megjegyzés: task.cancel() csak kérést küld az event loop-nak;
        a tényleges megszakítás az asyncio loop következő iterációján
        történik. A main() asyncio_thread.join(timeout=3.0) hívása
        elegendő időt biztosít a tiszta leálláshoz.
        """
        self._running = False
        for task in self._tasks:
            if not task.done():
                try:
                    task.cancel()
                except Exception as exc:
                    logger.debug(f"Task cancel hiba: {exc}")
        if self._antplus_handler:
            self._antplus_handler.stop()
        if self._antplus_thread and self._antplus_thread.is_alive():
            self._antplus_thread.join(timeout=5.0)
            if self._antplus_thread.is_alive():
                logger.warning("ANT+ szál nem állt le 5s alatt!")

        # Fix #17: Zwift UDP transport bezárása
        if self._zwift_udp is not None:
            t = getattr(self._zwift_udp, "_transport", None)
            if t is not None:
                try:
                    t.close()
                except Exception as exc:
                    logger.debug(f"Zwift UDP transport bezárási hiba: {exc}")

        # Zwift subprocess leállítása
        if self._zwift_proc is not None:
            if self._zwift_proc.poll() is None:  # csak ha még fut
                logger.info(f"zwift_api_polling.py leállítása (PID: {self._zwift_proc.pid})...")
                try:
                    self._zwift_proc.terminate()
                    self._zwift_proc.wait(timeout=5.0)
                    logger.info("zwift_api_polling.py leállítva")
                except subprocess.TimeoutExpired:
                    logger.warning("zwift_api_polling.py nem állt le 5s alatt, kill...")
                    self._zwift_proc.kill()
                except OSError as exc:
                    logger.error(f"zwift_api_polling.py leállítása sikertelen: {exc}")
                finally:
                    self._zwift_proc = None


# ============================================================
# HUD ABLAK (PySide6) – Star Trek LCARS stílus
# ============================================================


class LCARSHeaderWidget(QWidget):
    """LCARS fejléc widget – QPainter-rel rajzolt felső sáv."""

    def __init__(self, parent: QWidget, font_family: str, scale: float = 1.0) -> None:  # type: ignore[reportInvalidTypeForm]
        super().__init__(parent)
        self._font_family = font_family
        self._scale = scale
        self.setFixedHeight(50)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"background-color: {HUDWindow.BG};")

    def set_scale(self, s: float) -> None:
        self._scale = s
        h = max(30, int(50 * s))
        self.setFixedHeight(h)
        self.update()

    def paintEvent(self, event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        s = self._scale
        ch = self.height()
        bar_h = max(8, int(14 * s))
        sw = max(10, int(16 * s))
        R = max(14, int(26 * s))
        corner_r = max(12, int(18 * s))

        # Fő narancssárga sáv ívvel + lekerekített bal felső sarok
        path = QPainterPath()
        path.moveTo(corner_r, 0)
        path.lineTo(w - 6, 0)
        path.lineTo(w - 6, bar_h)
        for i in range(21):
            angle = math.radians(90 + (180 - 90) * i / 20)
            px = sw + R + R * math.cos(angle)
            py = bar_h + R - R * math.sin(angle)
            path.lineTo(px, py)
        path.lineTo(sw, ch)
        path.lineTo(0, ch)
        path.lineTo(0, corner_r)
        path.arcTo(QRectF(0, 0, 2 * corner_r, 2 * corner_r), 180, -90)
        path.closeSubpath()
        p.fillPath(path, QBrush(QColor(HUDWindow.LCARS_ORANGE)))

        # Bal felső sarok háttér kitöltés (ív mögött)
        bg_path = QPainterPath()
        bg_path.addRect(QRectF(0, 0, corner_r, corner_r))
        bg_path -= path
        p.fillPath(bg_path, QBrush(QColor(HUDWindow.BG)))

        # Cím szöveg
        title_size = max(8, int(12 * s))
        p.setFont(QFont(self._font_family, title_size, QFont.Weight.Bold))
        p.setPen(QColor(HUDWindow.LCARS_CYAN))
        p.drawText(QRectF(sw + R, bar_h, w - 6 - sw - R, ch - bar_h),
                    Qt.AlignmentFlag.AlignCenter, "SWIFT FAN CTRL")

        # Badge (magenta téglalap + verzió)
        badge_w = max(40, int(62 * s))
        p.fillRect(int(w - badge_w - 8), 1, badge_w, bar_h - 3,
                    QColor(HUDWindow.LCARS_MAGENTA))
        ver_size = max(6, int(7 * s))
        p.setFont(QFont(self._font_family, ver_size))
        p.setPen(QColor("#FFFFFF"))
        p.drawText(int(w - badge_w - 8), 1, badge_w, bar_h - 3,
                    Qt.AlignmentFlag.AlignCenter, f"v{__version__}")

        p.end()


class LCARSFooterWidget(QWidget):
    """LCARS lábléc widget – QPainter-rel rajzolt alsó sáv."""

    def __init__(self, parent: QWidget, font_family: str, scale: float = 1.0) -> None:  # type: ignore[reportInvalidTypeForm]
        super().__init__(parent)
        self._font_family = font_family
        self._scale = scale
        self.setFixedHeight(50)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"background-color: {HUDWindow.BG};")

    def set_scale(self, s: float) -> None:
        self._scale = s
        h = max(30, int(50 * s))
        self.setFixedHeight(h)
        self.update()

    def paintEvent(self, event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        s = self._scale
        fh = self.height()
        bar_h = max(8, int(14 * s))
        sw = max(10, int(16 * s))
        R = max(14, int(26 * s))
        bar_top = fh - bar_h
        corner_r = max(12, int(18 * s))

        # Fő kék sáv ívvel + lekerekített bal alsó sarok
        path = QPainterPath()
        path.moveTo(0, 0)
        path.lineTo(sw, 0)
        for i in range(21):
            angle = math.radians(180 + (270 - 180) * i / 20)
            px = sw + R + R * math.cos(angle)
            py = bar_top - R - R * math.sin(angle)
            path.lineTo(px, py)
        path.lineTo(w - 6, bar_top)
        path.lineTo(w - 6, fh)
        path.lineTo(corner_r, fh)
        path.arcTo(QRectF(0, fh - 2 * corner_r, 2 * corner_r, 2 * corner_r), 270, -90)
        path.lineTo(0, 0)
        path.closeSubpath()
        p.fillPath(path, QBrush(QColor(HUDWindow.LCARS_BLUE)))

        # Bal alsó sarok háttér kitöltés (ív mögött)
        bg_path = QPainterPath()
        bg_path.addRect(QRectF(0, fh - corner_r, corner_r, corner_r))
        bg_path -= path
        p.fillPath(bg_path, QBrush(QColor(HUDWindow.BG)))

        # Szegmensek
        seg_x = sw + R + 8
        seg_w = max(1, (w - 6 - int(seg_x)) // 3)
        p.fillRect(int(seg_x + seg_w + 4), bar_top, seg_w - 4, bar_h,
                    QColor(HUDWindow.LCARS_PURPLE))
        p.fillRect(int(seg_x + 2 * seg_w + 4), bar_top,
                    w - 6 - int(seg_x + 2 * seg_w + 4), bar_h,
                    QColor(HUDWindow.LCARS_TAN))

        # Footer szöveg
        footer_text_size = max(7, int(9 * s))
        p.setFont(QFont(self._font_family, footer_text_size))
        p.setPen(QColor(HUDWindow.LCARS_CYAN_DIM))
        p.drawText(int(sw + R), 0, int(w - 6 - sw - R), bar_top,
                    Qt.AlignmentFlag.AlignCenter, "STARFLEET CYCLING DIV")

        p.end()


class LCARSSidebarWidget(QWidget):
    """LCARS bal oldalsáv – színes szegmensek."""

    COLORS = ["#FF9900", "#FFCC66", "#5599FF", "#CC6699", "#9977CC", "#FFAA66"]

    def __init__(self, parent: QWidget, scale: float = 1.0) -> None:  # type: ignore[reportInvalidTypeForm]
        super().__init__(parent)
        self._scale = scale
        self.setFixedWidth(max(10, int(16 * scale)))
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"background-color: {HUDWindow.BG};")

    def set_scale(self, s: float) -> None:
        self._scale = s
        self.setFixedWidth(max(10, int(16 * s)))
        self.update()

    def paintEvent(self, event: Any) -> None:
        p = QPainter(self)
        sw = self.width()
        h = self.height()
        if h < 10:
            p.end()
            return
        n = len(self.COLORS)
        seg_h = max(10, h // n)
        gap = max(1, int(2 * self._scale))
        for i, c in enumerate(self.COLORS):
            y = i * seg_h
            bottom = h if i == n - 1 else y + seg_h
            p.fillRect(0, y + gap, sw, bottom - gap - y - gap, QColor(c))
        p.end()


# ────────────────────────────────────────────────────────────────────────────
#  Star Trek LCARS hangeffektek – WAV generátor és lejátszó
# ────────────────────────────────────────────────────────────────────────────


def _generate_tone(
    frequencies: List[Tuple[float, float, float]],
    sample_rate: int = 22050,
    volume: float = 0.4,
) -> bytes:
    """Szinuszhullám-alapú WAV generálás memóriában.

    Args:
        frequencies: lista (freq_hz, duration_sec, amplitude_mult) tuple-ökből.
                     Több elem esetén egymás után fűzi a hangokat.
        sample_rate: mintavételezési ráta
        volume: hangerő szorzó (0.0–1.0)
    """
    samples: list[int] = []
    for freq, duration, amp in frequencies:
        n_samples = int(sample_rate * duration)
        for i in range(n_samples):
            t = i / sample_rate
            # Hullámforma + fade in/out a kattanás elkerülésére
            fade_samples = min(200, n_samples // 4)
            fade = 1.0
            if fade_samples > 0:
                if i < fade_samples:
                    fade = i / fade_samples
                elif i > n_samples - fade_samples:
                    fade = (n_samples - i) / fade_samples
            val = math.sin(2 * math.pi * freq * t) * volume * amp * fade
            samples.append(int(val * 32767))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buf.getvalue()


class LCARSSoundManager:
    """Star Trek LCARS hangeffektek kezelője – QSoundEffect alapú lejátszás."""

    # Hang definíciók: (frekvencia_hz, időtartam_sec, amplitúdó)
    _SOUND_DEFS: Dict[str, List[Tuple[float, float, float]]] = {
        # Zónaváltás hangok – jellegzetes LCARS csippanások
        "zone_up": [(880, 0.08, 1.0), (1320, 0.12, 0.8)],       # felfelé lépés
        "zone_down": [(1320, 0.08, 0.8), (880, 0.12, 1.0)],     # lefelé lépés
        "zone_standby": [(440, 0.15, 0.5), (330, 0.2, 0.4)],    # standby-ba lépés
        # Szenzor események
        "sensor_dropout": [                                        # vészjelzés – hármas csipogás
            (1760, 0.06, 1.0), (0, 0.04, 0.0),
            (1760, 0.06, 1.0), (0, 0.04, 0.0),
            (1760, 0.06, 1.0),
        ],
        "sensor_reconnect": [                                      # visszacsatlakozás – emelkedő
            (660, 0.08, 0.7), (880, 0.08, 0.8), (1100, 0.12, 1.0),
        ],
        # Zwift
        "zwift_connect": [                                         # comm channel nyitás
            (440, 0.06, 0.6), (660, 0.06, 0.7), (880, 0.06, 0.8),
            (1100, 0.15, 1.0),
        ],
        "zwift_disconnect": [                                      # comm channel zárás
            (1100, 0.06, 0.8), (880, 0.06, 0.7), (660, 0.06, 0.6),
            (440, 0.15, 0.5),
        ],
        # Fan sebesség – rövid visszajelzés
        "fan_tx": [(1047, 0.05, 0.5), (1319, 0.07, 0.6)],       # parancs elküldve
        # HUD indítás – tricorder kinyitás hangeffekt
        "hud_startup": [
            (1200, 0.06, 0.3), (1500, 0.06, 0.4), (1800, 0.06, 0.5),
            (2200, 0.08, 0.6), (2600, 0.10, 0.7), (3000, 0.08, 0.8),
            (2400, 0.06, 0.5), (2800, 0.06, 0.6), (3200, 0.12, 0.9),
            (2000, 0.15, 0.4),
        ],
        # HUD bezárás – tricorder becsukás hangeffekt (fordított söprés lefelé)
        "hud_shutdown": [
            (2000, 0.06, 0.4), (3200, 0.06, 0.6), (2800, 0.06, 0.5),
            (2400, 0.08, 0.7), (3000, 0.08, 0.8), (2600, 0.06, 0.6),
            (2200, 0.06, 0.5), (1800, 0.06, 0.5), (1500, 0.06, 0.4),
            (1200, 0.10, 0.3), (800, 0.15, 0.2),
        ],
    }

    def __init__(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="lcars_snd_")
        self._effects: Dict[str, Any] = {}
        self._enabled = True
        self._volume = 0.5
        self._cleaned_up = False
        self._generate_all()
        import atexit
        atexit.register(self.cleanup)

    def _generate_all(self) -> None:
        """Összes hangeffekt generálása és QSoundEffect létrehozása."""
        if QSoundEffect is None:
            logger.info("QSoundEffect nem elérhető – hangeffektek kikapcsolva")
            return
        for name, tones in self._SOUND_DEFS.items():
            try:
                wav_data = _generate_tone(tones)
                wav_path = os.path.join(self._temp_dir, f"{name}.wav")
                with open(wav_path, "wb") as f:
                    f.write(wav_data)
                effect = QSoundEffect()
                effect.setSource(QUrl.fromLocalFile(wav_path))
                effect.setVolume(self._volume)
                self._effects[name] = effect
            except Exception as exc:
                logger.warning(f"LCARS hang generálás sikertelen ({name}): {exc}")

    def play(self, name: str) -> None:
        """Hangeffekt lejátszása név alapján."""
        if not self._enabled:
            return
        effect = self._effects.get(name)
        if effect is not None:
            effect.play()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def volume(self) -> float:
        return self._volume

    def sound_duration_ms(self, name: str) -> int:
        """Adott hangeffekt időtartama milliszekundumban (0, ha nem töltődött be)."""
        if name not in self._effects:
            return 0
        tones = self._SOUND_DEFS.get(name, [])
        return sum(int(d * 1000) for _, d, _ in tones)

    def set_enabled(self, enabled: bool) -> None:
        """Hangeffektek be/kikapcsolása."""
        self._enabled = enabled

    def set_volume(self, volume: float) -> None:
        """Összes hangeffekt hangerő beállítása (0.0–1.0)."""
        self._volume = volume
        for effect in self._effects.values():
            effect.setVolume(volume)

    def cleanup(self) -> None:
        """Összes effect leállítása és temp fájlok törlése."""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        for effect in self._effects.values():
            effect.stop()
        self._effects.clear()
        import shutil
        try:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        except Exception as exc:
            logger.debug(f"Temp dir törlési hiba: {exc}")


class HUDWindow(QWidget):
    """Lebegő, átlátszó HUD ablak – Star Trek LCARS stílusú megjelenítés (PySide6)."""

    # ─── LCARS SZÍN PALETTA ───
    BG = "#000a14"
    PANEL_BG = "#001020"
    LCARS_ORANGE = "#FF9900"
    LCARS_GOLD = "#FFCC66"
    LCARS_BLUE = "#5599FF"
    LCARS_CYAN = "#00CCFF"
    LCARS_CYAN_DIM = "#006688"
    LCARS_RED = "#FF3333"
    LCARS_MAGENTA = "#CC6699"
    LCARS_TAN = "#FFAA66"
    LCARS_PURPLE = "#9977CC"
    TEXT_BRIGHT = "#DDEEFF"
    TEXT_DIM = "#556688"
    BORDER_GLOW = "#003355"
    ZONE_COLORS = {
        0: "#556688",
        1: "#00CCFF",
        2: "#FF9900",
        3: "#FF3333",
    }
    _VAL_BG = "#001828"

    UPDATE_INTERVAL_MS = 500

    def __init__(self, controller: "FanController", app: "QApplication") -> None:  # type: ignore[reportInvalidTypeForm]
        super().__init__()
        self._base_width = 340
        self._base_height = 460
        self._scale = 1.0
        self._ctrl = controller
        self._app = app
        self._drag_pos: Optional[QPoint] = None  # type: ignore[reportInvalidTypeForm]
        self._resize_active = False
        self._resize_start_pos = QPoint()
        self._resize_start_size = QSize()

        # Referencia listák a skálázható label-ekhez
        self._row_key_labels: list[QLabel] = []  # type: ignore[reportInvalidTypeForm]
        self._status_key_labels: list[QLabel] = []  # type: ignore[reportInvalidTypeForm]

        # Flash effekt: előző értékek és flash számlálók
        self._prev_power: Optional[float] = None
        self._prev_hr: Optional[float] = None
        self._flash_power: int = 0  # hátralévő flash ciklusok
        self._flash_hr: int = 0
        self._flash_ble_tick: int = 0  # folyamatos villogás számláló

        # ───────── LCARS HANGEFFEKTEK ─────────
        self._sound = LCARSSoundManager()
        hud_cfg: HudConfig = self._ctrl.settings["hud"]
        self._sound.set_enabled(hud_cfg.sound_enabled)
        self._sound.set_volume(hud_cfg.sound_volume)
        self._prev_zone: Optional[int] = None
        self._prev_ble_status: Optional[str] = None
        self._prev_ant_status: Optional[str] = None
        self._prev_ble_sens_status: Optional[str] = None
        self._prev_zwift_status: Optional[str] = None
        self._prev_last_sent_time: float = 0.0

        # ───────── ZWIFT PROCESS MONITOR ─────────
        self._zwift_was_running = False
        self._zwift_seen = False           # True ha egyszer már láttuk futni
        self._zwift_check_counter = 0
        self._ZWIFT_CHECK_INTERVAL = 20    # minden 20. _update hívás = ~10s
        self._zwift_check_running = False  # race condition védelem
        self._zwift_grace_start: float = time.time()
        self._ZWIFT_GRACE_PERIOD: float = 300.0  # 5 perc várakozás indulásra

        # ───────── ABLAK BEÁLLÍTÁS ─────────
        self.setWindowTitle("LCARS Fan HUD")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setWindowOpacity(0.92)
        self.setGeometry(20, 20, self._base_width, self._base_height)
        self.setMinimumSize(220, 350)
        self.setStyleSheet(f"background-color: {self.BG};")

        # ───────── FONT ─────────
        self._try_load_lcars_font()
        self._font_family = self._detect_best_font()

        # ───────── LAYOUT ─────────
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Header
        self._header = LCARSHeaderWidget(self, self._font_family, self._scale)
        main_layout.addWidget(self._header)

        # Body (sidebar + content)
        body = QWidget(self)
        body.setStyleSheet(f"background-color: {self.BG};")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self._sidebar = LCARSSidebarWidget(body, self._scale)
        body_layout.addWidget(self._sidebar)

        # Content panel
        content = QWidget(body)
        content.setStyleSheet(f"background-color: {self.PANEL_BG};")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(6, 8, 6, 0)
        content_layout.setSpacing(0)
        body_layout.addWidget(content, 1)

        # ───────── ZÓNA KIJELZŐ ─────────
        self._lbl_zone_label = QLabel("FAN ZONE")
        self._lbl_zone_label.setStyleSheet(
            f"background-color: {self.LCARS_CYAN}; color: #000a14; "
            f"font-family: '{self._font_family}'; font-size: 12pt; font-weight: bold; "
            f"padding: 2px 4px; border-radius: 4px;"
        )
        content_layout.addWidget(self._lbl_zone_label)

        self._lbl_zone = QLabel("\u2013 \u2013 \u2013")
        self._lbl_zone.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_zone.setStyleSheet(
            f"background-color: {self._VAL_BG}; color: {self.LCARS_CYAN}; "
            f"font-family: '{self._font_family}'; font-size: 19pt; font-weight: bold; "
            f"padding: 3px 6px; border-radius: 4px;"
        )
        content_layout.addWidget(self._lbl_zone)

        # ───────── ÁLLAPOT CSÍK (tiles) ─────────
        tile_frame = QWidget(content)
        tile_frame.setStyleSheet(f"background-color: {self.PANEL_BG};")
        tile_layout = QHBoxLayout(tile_frame)
        tile_layout.setContentsMargins(0, 0, 0, 4)
        tile_layout.setSpacing(2)

        self._tile_zero_imm = self._make_tile(tile_layout, "ZRO IMM")
        self._tile_zero_hr_imm = self._make_tile(tile_layout, "ZHR IMM")
        self._tile_higher_wins = self._make_tile(tile_layout, "HI WINS")
        self._tile_ant = self._make_tile(tile_layout, "ANT+")
        self._tile_ble = self._make_tile(tile_layout, "BLE")
        self._tile_cooldown = self._make_tile(tile_layout, "COOL")
        content_layout.addWidget(tile_frame)

        # ───────── TELEMETRIA SOROK ─────────
        self._lbl_power = self._make_row(content_layout, "POWER", "\u2013 \u2013 \u2013",
                                          self.LCARS_GOLD, self.LCARS_TAN)
        self._lbl_hr = self._make_row(content_layout, "HEART RATE", "\u2013 \u2013 \u2013",
                                       self.LCARS_RED, self.LCARS_ORANGE)

        # ───────── SZEPARÁTOR ─────────
        sep = QFrame(content)
        sep.setFixedHeight(2)
        sep.setStyleSheet(f"background-color: {self.BORDER_GLOW}; margin: 6px 10px;")
        content_layout.addWidget(sep)

        # ───────── RENDSZER STÁTUSZ ─────────
        self._lbl_ble = self._make_status_row(content_layout, "BLE FAN", "OFFLINE",
                                               self.LCARS_BLUE)
        self._lbl_ble_sens = self._make_status_row(content_layout, "BLE SENS",
                                                     "\u2013 \u2013 \u2013", self.LCARS_BLUE)
        self._lbl_ant = self._make_status_row(content_layout, "ANT+",
                                               "\u2013 \u2013 \u2013", self.LCARS_PURPLE)
        self._lbl_zwift_udp = self._make_status_row(content_layout, "ZWIFT",
                                                      "\u2013 \u2013 \u2013", self.LCARS_PURPLE)

        # ───────── SZEPARÁTOR 2 ─────────
        sep2 = QFrame(content)
        sep2.setFixedHeight(2)
        sep2.setStyleSheet(f"background-color: {self.BORDER_GLOW}; margin: 6px 10px;")
        content_layout.addWidget(sep2)

        # ───────── RENDSZER INFO ─────────
        self._lbl_last_sent = self._make_status_row(content_layout, "LAST TX",
                                                      "\u2013 \u2013 \u2013", self.LCARS_TAN)
        self._lbl_cool = self._make_status_row(content_layout, "COOLDOWN",
                                                "\u2013 \u2013 \u2013", self.LCARS_TAN)

        # ───────── OPACITY SLIDER ─────────
        slider_widget = QWidget(content)
        slider_widget.setStyleSheet(f"background-color: {self.PANEL_BG};")
        slider_layout = QHBoxLayout(slider_widget)
        slider_layout.setContentsMargins(0, 6, 0, 4)
        slider_layout.setSpacing(4)

        self._opacity_label = QLabel("OPACITY")
        self._opacity_label.setStyleSheet(
            f"background-color: {self.LCARS_GOLD}; color: #000a14; "
            f"font-family: '{self._font_family}'; font-size: 9pt; font-weight: bold; "
            f"padding: 2px 4px; border-radius: 4px;"
        )
        slider_layout.addWidget(self._opacity_label)

        self._alpha_slider = QSlider(Qt.Orientation.Horizontal)
        self._alpha_slider.setRange(20, 100)
        self._alpha_slider.setValue(92)
        self._alpha_slider.setStyleSheet(
            f"QSlider::groove:horizontal {{"
            f"  background: #002244; height: 14px; border-radius: 2px;"
            f"}}"
            f"QSlider::handle:horizontal {{"
            f"  background: {self.LCARS_CYAN}; width: 16px; margin: -2px 0;"
            f"  border-radius: 3px;"
            f"}}"
        )
        self._alpha_slider.valueChanged.connect(self._on_alpha_change)
        slider_layout.addWidget(self._alpha_slider, 1)

        self._alpha_value = QLabel("92%")
        self._alpha_value.setStyleSheet(
            f"color: {self.LCARS_CYAN}; background-color: {self.PANEL_BG}; "
            f"font-family: '{self._font_family}'; font-size: 11pt; font-weight: bold;"
        )
        self._alpha_value.setFixedWidth(40)
        self._alpha_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        slider_layout.addWidget(self._alpha_value)

        content_layout.addWidget(slider_widget)
        content_layout.addStretch()

        main_layout.addWidget(body, 1)

        # Footer
        self._footer = LCARSFooterWidget(self, self._font_family, self._scale)
        main_layout.addWidget(self._footer)

        # ───────── KONTEXTUS MENÜ ─────────
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

        # ───────── TIMER ─────────
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update)
        self._timer.start(self.UPDATE_INTERVAL_MS)

    @property
    def sound(self) -> "LCARSSoundManager":
        return self._sound

    # ────────── FONT BETÖLTÉS ──────────

    def _try_load_lcars_font(self) -> None:
        """Antonio font betöltése a script melletti fonts/ mappából.

        Keresési sorrend:
          1. <script_dir>/fonts/Antonio-{Bold,Regular}.ttf
          2. <exe_dir>/fonts/...  (PyInstaller frozen)
        Ha a fontok nem találhatók, a program rendszer fontot használ fallback-ként.
        """
        if _platform.system() != "Windows":
            return
        try:
            if getattr(sys, "frozen", False):
                base_dir = os.path.dirname(os.path.abspath(sys.executable))
            else:
                base_dir = os.path.dirname(os.path.abspath(__file__))
            font_dir = os.path.join(base_dir, "fonts")

            loaded = 0
            for style in ("Bold", "Regular"):
                fpath = os.path.join(font_dir, f"Antonio-{style}.ttf")
                if os.path.exists(fpath):
                    QFontDatabase.addApplicationFont(fpath)
                    loaded += 1

            if loaded == 0:
                logger.info(
                    f"LCARS fontok nem találhatók a {font_dir} mappában – "
                    f"rendszer font használata. Lásd: fonts/README.txt"
                )
        except Exception as exc:
            logger.warning(f"LCARS font betöltés sikertelen (rendszer font használata): {exc}")

    def _detect_best_font(self) -> str:
        """Legjobb elérhető LCARS-stílusú font kiválasztása."""
        try:
            available = set(QFontDatabase.families())
        except Exception as exc:
            logger.debug(f"Font lista lekérés sikertelen: {exc}")
            return "Consolas"

        preferred = [
            "Antonio", "Michroma", "Century Gothic", "Eras Bold ITC",
            "Eras Medium ITC", "Bahnschrift", "Trebuchet MS", "Segoe UI", "Consolas",
        ]
        for f in preferred:
            if f in available:
                return f
        return "Consolas"

    # ────────── UI SEGÉDFÜGGVÉNYEK ──────────

    def _make_row(self, layout: "QVBoxLayout", label: str, value: str,  # type: ignore[reportInvalidTypeForm]
                  color: str, label_bg: str) -> "QLabel":  # type: ignore[reportInvalidTypeForm]
        """Telemetria sor LCARS színes label háttérrel."""
        row = QWidget()
        row.setStyleSheet(f"background-color: {self.PANEL_BG};")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 2, 0, 2)
        row_layout.setSpacing(2)

        key_lbl = QLabel(label)
        key_lbl.setFixedWidth(100)
        key_lbl.setStyleSheet(
            f"background-color: {label_bg}; color: #000a14; "
            f"font-family: '{self._font_family}'; font-size: 9pt; font-weight: bold; "
            f"padding: 3px 4px; border-radius: 4px;"
        )
        row_layout.addWidget(key_lbl)
        self._row_key_labels.append(key_lbl)

        val_lbl = QLabel(value)
        val_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        val_lbl.setStyleSheet(
            f"background-color: {self._VAL_BG}; color: {color}; "
            f"font-family: '{self._font_family}'; font-size: 14pt; font-weight: bold; "
            f"padding: 3px 6px; border-radius: 4px;"
        )
        row_layout.addWidget(val_lbl, 1)

        layout.addWidget(row)
        return val_lbl

    def _make_tile(self, layout: "QHBoxLayout", text: str) -> "QLabel":  # type: ignore[reportInvalidTypeForm]
        """Állapot csík tile."""
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(
            f"background-color: {self.TEXT_DIM}; color: #000a14; "
            f"font-family: '{self._font_family}'; font-size: 9pt; font-weight: bold; "
            f"padding: 2px 5px; border-radius: 4px;"
        )
        layout.addWidget(lbl, 1)
        return lbl

    def _make_status_row(self, layout: "QVBoxLayout", label: str, value: str,  # type: ignore[reportInvalidTypeForm]
                         label_bg: str) -> "QLabel":  # type: ignore[reportInvalidTypeForm]
        """Státusz sor LCARS színes label háttérrel."""
        row = QWidget()
        row.setStyleSheet(f"background-color: {self.PANEL_BG};")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 2, 0, 2)
        row_layout.setSpacing(2)

        key_lbl = QLabel(label)
        key_lbl.setFixedWidth(100)
        key_lbl.setStyleSheet(
            f"background-color: {label_bg}; color: #000a14; "
            f"font-family: '{self._font_family}'; font-size: 9pt; font-weight: bold; "
            f"padding: 2px 4px; border-radius: 4px;"
        )
        row_layout.addWidget(key_lbl)
        self._status_key_labels.append(key_lbl)

        val_lbl = QLabel(value)
        val_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        val_lbl.setStyleSheet(
            f"background-color: {self._VAL_BG}; color: {self.TEXT_DIM}; "
            f"font-family: '{self._font_family}'; font-size: 11pt; "
            f"padding: 2px 6px; border-radius: 4px;"
        )
        row_layout.addWidget(val_lbl, 1)

        layout.addWidget(row)
        return val_lbl

    # ────────── DRAG / RESIZE ──────────

    def mousePressEvent(self, event: "QMouseEvent") -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            if (self.width() - pos.x() < 20) and (self.height() - pos.y() < 20):
                self._resize_active = True
                self._resize_start_pos = event.globalPosition().toPoint()
                self._resize_start_size = self.size()
            else:
                self._drag_pos = (
                    event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: "QMouseEvent") -> None:  # type: ignore[override]
        if self._resize_active:
            delta = event.globalPosition().toPoint() - self._resize_start_pos
            new_w = max(220, self._resize_start_size.width() + delta.x())
            new_h = max(350, self._resize_start_size.height() + delta.y())
            self.resize(new_w, new_h)
            self._scale = new_w / self._base_width
            self._apply_scale()
        elif self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: "QMouseEvent") -> None:  # type: ignore[override]
        self._drag_pos = None
        self._resize_active = False
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        super().keyPressEvent(event)

    # ────────── OPACITY ──────────

    def _set_alpha_from_menu(self, percent: int) -> None:
        self.setWindowOpacity(percent / 100.0)
        self._alpha_slider.setValue(percent)
        self._alpha_value.setText(f"{percent}%")

    def _on_alpha_change(self, value: int) -> None:
        self.setWindowOpacity(value / 100.0)
        self._alpha_value.setText(f"{value}%")

    # ────────── KONTEXTUS MENÜ ──────────

    def _show_menu(self, pos: "QPoint") -> None:  # type: ignore[reportInvalidTypeForm]
        menu_ss = (
            f"QMenu {{ background-color: #001828; color: {self.LCARS_CYAN}; "
            f"font-family: '{self._font_family}'; font-size: 10pt; }}"
            f"QMenu::item:selected {{ background-color: {self.LCARS_BLUE}; "
            f"color: white; }}"
        )
        menu = QMenu(self)
        menu.setStyleSheet(menu_ss)
        menu.addAction("Bezárás", self.close)

        menu.addSeparator()
        menu.addAction("Opacity: 50%", lambda: self._set_alpha_from_menu(50))
        menu.addAction("Opacity: 85%", lambda: self._set_alpha_from_menu(85))
        menu.addAction("Opacity: 100%", lambda: self._set_alpha_from_menu(100))

        # ─── LCARS HANG BEÁLLÍTÁSOK ───
        menu.addSeparator()
        sound_enabled = self._sound.enabled
        toggle_label = "🔊 Hang: KI" if sound_enabled else "🔇 Hang: BE"
        menu.addAction(toggle_label, self._toggle_sound)

        vol_menu = menu.addMenu("🔉 Hangerő")
        vol_menu.setStyleSheet(menu_ss)
        for pct in (25, 50, 75, 100):
            v = pct / 100.0
            current = round(self._sound.volume * 100)
            marker = " ◄" if pct == current else ""
            vol_menu.addAction(
                f"{pct}%{marker}", lambda _v=v: self._set_sound_volume(_v)
            )

        menu.exec(self.mapToGlobal(pos))

    def _toggle_sound(self) -> None:
        """Hangeffektek be/kikapcsolása és mentés settings.json-ba."""
        new_state = not self._sound.enabled
        self._sound.set_enabled(new_state)
        self._save_hud_setting("sound_enabled", new_state)

    def _set_sound_volume(self, volume: float) -> None:
        """Hangerő beállítása és mentés settings.json-ba."""
        self._sound.set_volume(volume)
        self._save_hud_setting("sound_volume", round(volume, 2))

    def _save_hud_setting(self, key: str, value: Any) -> None:
        """Egy HUD beállítás mentése a settings.json fájlba."""
        settings = self._ctrl.settings
        hud_cfg: HudConfig = settings["hud"]
        # Map old key names to dataclass attribute names
        attr = key.replace(".", "_") if "." in key else key
        if hasattr(hud_cfg, attr):
            setattr(hud_cfg, attr, value)
        try:
            with open(self._ctrl.settings_file, "w", encoding="utf-8") as f:
                json.dump(_settings_to_serializable(settings), f, indent=2, ensure_ascii=False)
            logger.info(f"HUD beállítás mentve: hud.{key} = {value}")
        except OSError as exc:
            logger.warning(f"Settings mentési hiba: {exc}")

    # ────────── LABEL FRISSÍTÉS SEGÉD ──────────

    import re as _re
    _RE_COLOR = _re.compile(r"(?<!-)color:\s*[^;]+;")
    _RE_BG_COLOR = _re.compile(r"background-color:\s*[^;]+;")

    @staticmethod
    def _update_label(lbl: "QLabel", text: str, color: str) -> None:  # type: ignore[reportInvalidTypeForm]
        """Label szöveg és szín frissítése stylesheet-tel."""
        current = lbl.styleSheet()
        new_ss = HUDWindow._RE_COLOR.sub(f"color: {color};", current, count=1)
        lbl.setStyleSheet(new_ss)
        lbl.setText(text)

    @staticmethod
    def _update_tile_bg(tile: "QLabel", bg: str) -> None:  # type: ignore[reportInvalidTypeForm]
        """Tile háttérszín frissítése."""
        current = tile.styleSheet()
        new_ss = HUDWindow._RE_BG_COLOR.sub(f"background-color: {bg};", current, count=1)
        tile.setStyleSheet(new_ss)

    @staticmethod
    def _lighten(color_hex: str, factor: float = 0.5) -> str:
        """Szín világosítása – factor=0 eredeti, factor=1 fehér."""
        r = int(color_hex[1:3], 16)
        g = int(color_hex[3:5], 16)
        b = int(color_hex[5:7], 16)
        r = int(r + (255 - r) * factor)
        g = int(g + (255 - g) * factor)
        b = int(b + (255 - b) * factor)
        return f"#{r:02X}{g:02X}{b:02X}"

    # ────────── FRISSÍTÉS (500 ms) ──────────

    def _update(self) -> None:
        try:
            state = self._ctrl.state
            ble_fan = self._ctrl.ble_fan
            cool = self._ctrl.cooldown_ctrl

            if state is not None:
                zone, power, hr = state.ui_snapshot.read()

                zone_color = (
                    self.ZONE_COLORS.get(zone, self.LCARS_CYAN)
                    if zone is not None else self.TEXT_DIM
                )
                zone_names = {0: "STANDBY", 1: "ZONE 1", 2: "ZONE 2", 3: "ZONE 3"}
                zone_txt = (
                    zone_names.get(zone, "\u2013 \u2013 \u2013")
                    if zone is not None else "\u2013 \u2013 \u2013"
                )

                self._update_label(self._lbl_zone, zone_txt, zone_color)

                # Zónaváltás hang
                if zone is not None and zone != self._prev_zone and self._prev_zone is not None:
                    if zone == 0:
                        self._sound.play("zone_standby")
                    elif zone > self._prev_zone:
                        self._sound.play("zone_up")
                    else:
                        self._sound.play("zone_down")
                self._prev_zone = zone

                # Power – flash ha változott
                if power is not None and power != self._prev_power:
                    self._flash_power = 2  # 2 ciklus = ~1s villanás
                self._prev_power = power

                if self._flash_power > 0:
                    self._flash_power -= 1
                    power_color = self._lighten(self.LCARS_GOLD) if self._flash_power % 2 == 1 else self.LCARS_GOLD
                else:
                    power_color = self.LCARS_GOLD if power is not None else self.TEXT_DIM

                self._update_label(
                    self._lbl_power,
                    "\u2013 \u2013 \u2013" if power is None else f"{power:.0f} W",
                    power_color,
                )

                # HR – flash ha változott
                if hr is not None and hr != self._prev_hr:
                    self._flash_hr = 2
                self._prev_hr = hr

                if self._flash_hr > 0:
                    self._flash_hr -= 1
                    hr_color = self._lighten(self.LCARS_RED) if self._flash_hr % 2 == 1 else self.LCARS_RED
                else:
                    hr_color = self.LCARS_RED if hr is not None else self.TEXT_DIM

                self._update_label(
                    self._lbl_hr,
                    "\u2013 \u2013 \u2013" if hr is None else f"{hr:.0f} BPM",
                    hr_color,
                )

            # BLE fan – villogás OFFLINE/PIN FAIL állapotoknál
            self._flash_ble_tick = 1 - self._flash_ble_tick
            flash_white = self._flash_ble_tick == 0
            ble_status = "DISABLED"
            if ble_fan is not None:
                if ble_fan.auth_failed:
                    c = self._lighten(self.LCARS_GOLD) if flash_white else self.LCARS_GOLD
                    self._update_label(self._lbl_ble, "PIN FAIL", c)
                    ble_status = "PIN FAIL"
                elif ble_fan.is_connected:
                    self._update_label(self._lbl_ble, "ONLINE", self.LCARS_CYAN)
                    ble_status = "ONLINE"
                else:
                    c = self._lighten(self.LCARS_RED) if flash_white else self.LCARS_RED
                    self._update_label(self._lbl_ble, "OFFLINE", c)
                    ble_status = "OFFLINE"
            else:
                c = self._lighten(self.TEXT_DIM) if flash_white else self.TEXT_DIM
                self._update_label(self._lbl_ble, "DISABLED", c)

            # BLE fan hangeffekt
            if self._prev_ble_status is not None and ble_status != self._prev_ble_status:
                if ble_status == "ONLINE":
                    self._sound.play("sensor_reconnect")
                elif ble_status in ("OFFLINE", "PIN FAIL"):
                    self._sound.play("sensor_dropout")
            self._prev_ble_status = ble_status

            # BLE szenzorok
            ds: DatasourceConfig = self._ctrl.settings["datasource"]
            power_ble = ds.power_source == DataSource.BLE
            hr_ble = ds.hr_source == DataSource.BLE

            if not power_ble and not hr_ble:
                c = self._lighten(self.TEXT_DIM) if flash_white else self.TEXT_DIM
                self._update_label(self._lbl_ble_sens, "\u2013 \u2013 \u2013", c)
            else:
                ble = getattr(self._ctrl, "_ble_sensor_handler", None)
                if ble is not None:
                    now = time.monotonic()
                    power_ok = (
                        power_ble
                        and (ble.power_lastdata > 0)
                        and (now - ble.power_lastdata < 10)
                    )
                    hr_ok = (
                        hr_ble
                        and (ble.hr_lastdata > 0)
                        and (now - ble.hr_lastdata < 10)
                    )
                    p_s = "OK" if power_ok else ("--" if not power_ble else "FAIL")
                    h_s = "OK" if hr_ok else ("--" if not hr_ble else "FAIL")

                    ble_states: list[bool] = []
                    if power_ble:
                        ble_states.append(power_ok)
                    if hr_ble:
                        ble_states.append(hr_ok)

                    if any(s is False for s in ble_states):
                        row_color = self._lighten(self.LCARS_RED) if flash_white else self.LCARS_RED
                    elif all(s is True for s in ble_states):
                        row_color = self.LCARS_CYAN
                    else:
                        row_color = self.LCARS_GOLD

                    self._update_label(
                        self._lbl_ble_sens, f"P:{p_s}  HR:{h_s}", row_color
                    )
                else:
                    self._update_label(self._lbl_ble_sens, "STANDBY", self.LCARS_GOLD)

            # ANT+
            power_ant = ds.power_source == DataSource.ANTPLUS
            hr_ant = ds.hr_source == DataSource.ANTPLUS
            ant = getattr(self._ctrl, "_antplus_handler", None)

            if not power_ant and not hr_ant:
                c = self._lighten(self.TEXT_DIM) if flash_white else self.TEXT_DIM
                self._update_label(self._lbl_ant, "\u2013 \u2013 \u2013", c)
            elif ant is not None:
                now = time.monotonic()
                power_ok = (
                    power_ant
                    and (ant.power_lastdata > 0)
                    and (now - ant.power_lastdata < 10)
                )
                hr_ok = (
                    hr_ant
                    and (ant.hr_lastdata > 0)
                    and (now - ant.hr_lastdata < 10)
                )
                p_s = "OK" if power_ok else ("--" if not power_ant else "FAIL")
                h_s = "OK" if hr_ok else ("--" if not hr_ant else "FAIL")

                ant_states: list[bool] = []
                if power_ant:
                    ant_states.append(power_ok)
                if hr_ant:
                    ant_states.append(hr_ok)

                if any(s is False for s in ant_states):
                    row_color = self._lighten(self.LCARS_RED) if flash_white else self.LCARS_RED
                elif all(s is True for s in ant_states):
                    row_color = self.LCARS_CYAN
                else:
                    row_color = self.LCARS_GOLD

                self._update_label(self._lbl_ant, f"P:{p_s}  HR:{h_s}", row_color)
                # ANT+ hangeffekt
                ant_status = "FAIL" if any(s is False for s in ant_states) else "OK"
                if self._prev_ant_status is not None and ant_status != self._prev_ant_status:
                    if ant_status == "OK":
                        self._sound.play("sensor_reconnect")
                    else:
                        self._sound.play("sensor_dropout")
                self._prev_ant_status = ant_status
            else:
                c = self._lighten(self.TEXT_DIM) if flash_white else self.TEXT_DIM
                self._update_label(self._lbl_ant, "\u2013 \u2013 \u2013", c)

            # Zwift
            zwift = getattr(self._ctrl, "_zwift_udp", None)
            power_zwift = ds.power_source == DataSource.ZWIFTUDP
            hr_zwift = ds.hr_source == DataSource.ZWIFTUDP

            if zwift is not None and (power_zwift or hr_zwift):
                now = time.monotonic()
                ok = (
                    zwift.last_packet_time > 0
                    and (now - zwift.last_packet_time) < 5.0
                )
                zwift_status = "RECEIVING" if ok else "NO SIGNAL"
                if ok:
                    self._update_label(
                        self._lbl_zwift_udp, "RECEIVING", self.LCARS_CYAN
                    )
                else:
                    c = self._lighten(self.LCARS_RED) if flash_white else self.LCARS_RED
                    self._update_label(self._lbl_zwift_udp, "NO SIGNAL", c)

                # Zwift hangeffekt
                if self._prev_zwift_status is not None and zwift_status != self._prev_zwift_status:
                    if zwift_status == "RECEIVING":
                        self._sound.play("zwift_connect")
                    else:
                        self._sound.play("zwift_disconnect")
                self._prev_zwift_status = zwift_status
            else:
                c = self._lighten(self.TEXT_DIM) if flash_white else self.TEXT_DIM
                self._update_label(self._lbl_zwift_udp, "\u2013 \u2013 \u2013", c)

            # Last TX
            if ble_fan is not None and getattr(ble_fan, "last_sent_time", 0) > 0:
                cur_sent_time = ble_fan.last_sent_time
                ago = time.monotonic() - cur_sent_time
                self._update_label(self._lbl_last_sent, f"{ago:.0f}s AGO", self.LCARS_TAN)

                # Fan TX hangeffekt – csak ha új parancs ment ki
                if cur_sent_time != self._prev_last_sent_time and self._prev_last_sent_time > 0:
                    self._sound.play("fan_tx")
                self._prev_last_sent_time = cur_sent_time
            else:
                c = self._lighten(self.TEXT_DIM) if flash_white else self.TEXT_DIM
                self._update_label(self._lbl_last_sent, "\u2013 \u2013 \u2013", c)

            # Cooldown
            if cool is not None:
                active, remaining = cool.snapshot()
                if active:
                    self._update_label(
                        self._lbl_cool, f"{remaining:.0f}s", self.LCARS_GOLD
                    )
                else:
                    c = self._lighten(self.TEXT_DIM) if flash_white else self.TEXT_DIM
                    self._update_label(self._lbl_cool, "INACTIVE", c)
            else:
                c = self._lighten(self.TEXT_DIM) if flash_white else self.TEXT_DIM
                self._update_label(self._lbl_cool, "\u2013 \u2013 \u2013", c)

            # ── Állapot csík frissítése (aktív = villogó háttér) ──
            zpi = self._ctrl.settings["power_zones"].zero_power_immediate
            if zpi:
                bg = self._lighten(self.LCARS_CYAN) if flash_white else self.LCARS_CYAN
            else:
                bg = self.TEXT_DIM
            self._update_tile_bg(self._tile_zero_imm, bg)

            zhi = self._ctrl.settings["heart_rate_zones"].zero_hr_immediate
            if zhi:
                bg = self._lighten(self.LCARS_CYAN) if flash_white else self.LCARS_CYAN
            else:
                bg = self.TEXT_DIM
            self._update_tile_bg(self._tile_zero_hr_imm, bg)

            zone_mode_val = self._ctrl.settings["heart_rate_zones"].zone_mode
            hw = zone_mode_val == ZoneMode.HIGHER_WINS
            if hw:
                bg = self._lighten(self.LCARS_ORANGE) if flash_white else self.LCARS_ORANGE
            else:
                bg = self.TEXT_DIM
            self._update_tile_bg(self._tile_higher_wins, bg)

            if power_ant or hr_ant:
                bg = self._lighten(self.LCARS_PURPLE) if flash_white else self.LCARS_PURPLE
            else:
                bg = self.TEXT_DIM
            self._update_tile_bg(self._tile_ant, bg)

            if power_ble or hr_ble:
                bg = self._lighten(self.LCARS_BLUE) if flash_white else self.LCARS_BLUE
            else:
                bg = self.TEXT_DIM
            self._update_tile_bg(self._tile_ble, bg)

            if cool is not None:
                cd_active, _ = cool.snapshot()
                if cd_active:
                    bg = self._lighten(self.LCARS_GOLD) if flash_white else self.LCARS_GOLD
                else:
                    bg = self.TEXT_DIM
            else:
                bg = self.TEXT_DIM
            self._update_tile_bg(self._tile_cooldown, bg)

            # ── ZwiftApp.exe process figyelés (~10s-onként) ──
            if self._ctrl.settings["hud"].close_at_zwiftapp_exe:
                self._zwift_check_counter += 1
                if self._zwift_check_counter >= self._ZWIFT_CHECK_INTERVAL:
                    self._zwift_check_counter = 0
                    if not self._zwift_check_running:
                        self._zwift_check_running = True
                        threading.Thread(
                            target=self._check_zwift_process,
                            daemon=True,
                            name="ZwiftProcessCheck",
                        ).start()

        except Exception as exc:
            logger.warning(f"HUD _update hiba: {exc}")

    # ────────── ZWIFT PROCESS MONITOR ──────────

    def _check_zwift_process(self) -> None:
        """Háttérszálban ellenőrzi, hogy a ZwiftApp.exe fut-e."""
        try:
            running = self._ctrl.is_process_running("ZwiftApp.exe")
            should_close = False
            if running:
                if not self._zwift_seen:
                    self._zwift_seen = True
                    logger.info("ZwiftApp.exe észlelve / detected.")
            elif self._zwift_seen:
                # Zwift korábban futott, de most már nem → HUD bezárása
                logger.info("ZwiftApp.exe kilépett, HUD leállítása...")
                should_close = True
            elif time.time() - self._zwift_grace_start >= self._ZWIFT_GRACE_PERIOD:
                # Grace period lejárt, Zwift soha nem indult el → kilépés
                logger.info(
                    "ZwiftApp.exe nem indult el %.0f másodperc alatt, kilépés...",
                    self._ZWIFT_GRACE_PERIOD,
                )
                should_close = True
            self._zwift_was_running = running
            if should_close:
                # QTimer.singleShot háttérszálból NEM működik (nincs Qt event
                # loop). QMetaObject.invokeMethod thread-safe: a fő szál event
                # loop-jába ütemezi a close() hívást.
                from PySide6.QtCore import QMetaObject, Qt, Q_ARG
                QMetaObject.invokeMethod(
                    self, "close", Qt.ConnectionType.QueuedConnection,
                )
        finally:
            self._zwift_check_running = False

    # ────────── SKÁLÁZÁS ──────────

    def _apply_scale(self) -> None:
        s = self._scale

        self._header.set_scale(s)
        self._footer.set_scale(s)
        self._sidebar.set_scale(s)

    def cleanup_sound(self) -> None:
        """Publikus interfész a hangrendszer felszabadításához."""
        self._sound.cleanup()

    # ────────── RUN / CLOSE ──────────

    def run(self) -> None:
        self.show()
        self._sound.play("hud_startup")
        self._app.exec()

    def closeEvent(self, event: Any) -> None:
        if getattr(self, "_close_done", False):
            # Harmadik hívás: a hang lejátszódott, ténylegesen bezárjuk
            self._sound.cleanup()
            super().closeEvent(event)
            self._app.quit()
            return
        if getattr(self, "_closing", False):
            # Második hívás (pl. finally blokkból): még várjuk a hangot, ignoráljuk
            event.ignore()
            return
        self._closing = True
        event.ignore()
        self._timer.stop()
        self._sound.play("hud_shutdown")
        # Várunk, amíg a bezáró hang lejátszódik, majd ténylegesen bezárjuk
        duration_ms = self._sound.sound_duration_ms("hud_shutdown")

        def _finish_close() -> None:
            self._close_done = True
            self.close()

        QTimer.singleShot(duration_ms + 100, _finish_close)
# ============================================================
# MAIN
# ============================================================


def main() -> None:
    # Windows: SelectorEventLoop megbízhatóbb threaded asyncio-hoz
    # Python 3.16-tól ezek az API-k eltávolításra kerülnek
    if _platform.system() == "Windows" and sys.version_info < (3, 14):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # Kezdeti logging (alapértelmezett könyvtár) – a settings betöltés is logolhat
    _setup_logging()

    # PyInstaller frozen exe: settings.json az exe mellett keresendő
    if getattr(sys, 'frozen', False):
        _exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        _settings_path = os.path.join(_exe_dir, "settings.json")
    else:
        _script_dir = os.path.dirname(os.path.abspath(__file__))
        _settings_path = os.path.join(_script_dir, "settings.json")
    controller = FanController(_settings_path)

    # Logging újrakonfigurálása a betöltött log_directory alapján
    log_dir_setting = controller.settings["global_settings"].log_directory
    if log_dir_setting:
        _setup_logging(log_dir_setting)

    controller.print_startup_info()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    cleaned_up = False
    # Shutdown event: az asyncio loop-ot megbízhatóan leállítja
    shutdown_event = asyncio.Event()
    # Mutable konténer: a signal handler-nek kell a HUD referencia,
    # ami később jön létre. Lista azért, hogy nonlocal nélkül módosítható legyen.
    hud_ref: list[Any] = [None]

    def cleanup() -> None:
        nonlocal cleaned_up
        if cleaned_up:
            return
        cleaned_up = True
        controller.stop()

    def signal_handler(signum: int, frame: Any) -> None:
        user_logger.info(f"\nSignal {signum} fogadva, leállítás...")
        cleanup()
        # A HUD close()-on keresztül indítjuk a leállítást, hogy a
        # shutdown hang lejátszódhasson. A closeEvent timer-je a hang
        # végén hívja a tényleges close-t, ami kilép a Qt event loop-ból.
        hud = hud_ref[0]
        if hud is not None:
            try:
                from PySide6.QtCore import QMetaObject, Qt
                QMetaObject.invokeMethod(
                    hud, "close", Qt.ConnectionType.QueuedConnection,
                )
            except Exception as exc:
                # Fallback: ha az invokeMethod nem működik, quit() azonnal
                logger.debug(f"HUD invokeMethod hiba: {exc}")
                try:
                    from PySide6.QtWidgets import QApplication as _QApp
                    _qapp = _QApp.instance()
                    if _qapp is not None:
                        _qapp.quit()
                except Exception as exc2:
                    logger.debug(f"QApplication quit hiba: {exc2}")
        try:
            loop.call_soon_threadsafe(shutdown_event.set)
        except Exception as exc:
            logger.debug(f"shutdown_event.set hiba: {exc}")

    # SIGTERM: Unix-on megbízható, Windows-on a Popen.terminate() nem garantálja
    # SIGINT: Ctrl+C mindkét platformon működik
    signal.signal(signal.SIGTERM, signal_handler)
    try:
        signal.signal(signal.SIGINT, signal_handler)
    except (OSError, ValueError):
        pass  # Egyes környezetekben (pl. nem főszál) SIGINT nem regisztrálható

    atexit.register(cleanup)

    loop_ready = threading.Event()

    async def _run_until_shutdown() -> None:
        """Controller futtatása shutdown_event-ig."""
        controller_task = asyncio.create_task(controller.run())
        shutdown_task = asyncio.create_task(shutdown_event.wait())
        _done, pending = await asyncio.wait(
            [controller_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    def run_asyncio() -> None:
        asyncio.set_event_loop(loop)
        # Jelezzük, hogy az event loop fut és kész feladatokat fogadni
        loop.call_soon(loop_ready.set)
        try:
            loop.run_until_complete(_run_until_shutdown())
        except Exception as exc:
            # Fix #22: teljes traceback logolása
            logger.error(f"AsyncioThread hiba: {exc}", exc_info=True)
        finally:
            loop_ready.set()  # Ha hiba miatt kilép, ne blokkoljon örökre

    asyncio_thread = threading.Thread(
        target=run_asyncio, daemon=True, name="AsyncioThread"
    )
    asyncio_thread.start()

    # Megvárjuk, amíg az asyncio event loop ténylegesen elindul (max 5s)
    loop_ready.wait(timeout=5.0)

    # Fix #20: PySide6 opcionális – headless módban HUD nélkül fut
    if _PYSIDE6_AVAILABLE:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import qInstallMessageHandler, QtMsgType

        # A pywinauto már beállította a process DPI awareness-t, ezért a Qt
        # SetProcessDpiAwarenessContext() hívása "access denied"-del meghiúsul.
        # Ez ártalmatlan warning – elnyomjuk, hogy ne zavarjon a konzol outputban.
        def _qt_message_filter(mode: QtMsgType, _context: Any, message: str) -> None:
            if "SetProcessDpiAwarenessContext" in message:
                return  # elnyomjuk
            sys.stderr.write(message + "\n")

        qInstallMessageHandler(_qt_message_filter)
        app = QApplication(sys.argv)
        qInstallMessageHandler(None)  # type: ignore[arg-type]  # visszaállítjuk az alapértelmezett handler-t
        hud = HUDWindow(controller, app)
        hud_ref[0] = hud
        try:
            hud.run()
        except KeyboardInterrupt:
            user_logger.info("\nLeállítás (Ctrl+C)...")
        finally:
            # Ha a HUD még nem kezdte a bezárást, elindítjuk a shutdown hangot
            if not getattr(hud, "_closing", False):
                hud.close()  # closeEvent phase 1: hang indul, event.ignore()
                # Qt event loop már nem fut, manuálisan pumpáljuk az eseményeket
                # amíg a shutdown hang lejátszódik
                duration_ms = hud.sound.sound_duration_ms("hud_shutdown")
                if duration_ms > 0:
                    from PySide6.QtCore import QElapsedTimer
                    elapsed = QElapsedTimer()
                    elapsed.start()
                    while elapsed.elapsed() < duration_ms + 150:
                        app.processEvents()
            # Mindenképpen biztosítjuk a végleges bezárást
            hud._close_done = True
            hud.cleanup_sound()
            hud.close()
            hud_ref[0] = None
            cleanup()

            # Shutdown event jelzése → asyncio loop megbízhatóan leáll
            try:
                loop.call_soon_threadsafe(shutdown_event.set)
            except Exception as exc:
                logger.debug(f"shutdown_event.set hiba: {exc}")

            asyncio_thread.join(timeout=3.0)
            loop.close()
            user_logger.info("\nProgram leállítva.")
    else:
        logger.warning("PySide6 nem elérhető, HUD nélkül fut")
        user_logger.warning("⚠ PySide6 nem elérhető – HUD nélkül fut. Ctrl+C a leállításhoz.")
        try:
            asyncio_thread.join()
        except KeyboardInterrupt:
            user_logger.info("\nLeállítás (Ctrl+C)...")
        finally:
            cleanup()
            try:
                loop.call_soon_threadsafe(shutdown_event.set)
            except Exception as exc:
                logger.debug(f"shutdown_event.set hiba: {exc}")
            asyncio_thread.join(timeout=3.0)
            loop.close()
            user_logger.info("\nProgram leállítva.")


if __name__ == "__main__":
    main()

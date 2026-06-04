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

# --- Enum-ok és beállítás-modellek (kiszervezve a config al-package-be) ---
# A típusbiztos beállítás-modellek és az enumok a smart_fan_controller.config
# modulba kerültek. Itt újraexportáljuk őket a visszafelé kompatibilitásért
# (a tesztek és a kód többi része innen importál).
from smart_fan_controller.config import (
    DataSource,
    ZoneMode,
    VALID_DATA_SOURCES,
    VALID_ZONE_MODES,
)




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
    # Headless mód (PySide6 nincs telepítve, pl. Raspberry Pi terminál):
    # A HUD osztályok modul-szinten definiálódnak (class X(QWidget)), ezért a
    # QWidget-nek subclass-olhatónak kell lennie, különben az import elhasal
    # (TypeError: NoneType takes no arguments). A HUD soha nem példányosul
    # headless módban (main() a _PYSIDE6_AVAILABLE flag-et ellenőrzi), így egy
    # üres stub bázisosztály elegendő – a példányosítás egyértelmű hibát ad.
    class _HeadlessQtWidgetStub:
        """Üres QWidget-helyettesítő headless módhoz – nem példányosítható."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                "PySide6 nincs telepítve – a HUD nem használható headless módban."
            )

    QApplication: Any = None
    QWidget: Any = _HeadlessQtWidgetStub
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


# (A _resolve_log_dir a smart_fan_controller.core.helpers modulba kerül; fent re-exportálva.)


# Modul-szintű változó: a feloldott log könyvtár (_setup_logging állítja be)
_log_dir: str = os.path.dirname(os.path.abspath(__file__))
# Modul-szintű flag: a loggolás engedélyezve van-e (global_settings.logging)
_logging_enabled: bool = True


def _setup_logging(log_directory: Optional[str] = None, logging_enabled: bool = True) -> None:
    """Logging konfiguráció: konzol + rotált fájl (500 KB max).

    Két logger:
      - ``user_logger``: Felhasználói üzenetek (konzolra + fájlba).
        Konzolra tiszta formátum (csak az üzenet), fájlba időbélyeggel.
      - ``logger``: Belső debug/info logok (fájlba mindig, konzolra WARNING+ felett).

    A log fájlok a ``log_directory``-ba kerülnek (ha érvényes), különben
    a program indítási könyvtárába.

    Ha ``logging_enabled`` False, mindkét logger NullHandler-t kap (teljes
    némaság – se fájl, se konzol). A program-indítási összefoglaló ettől
    függetlenül megjelenik (``print_startup_info`` print()-re vált).

    Többszöri hívás biztonságos: a korábbi handler-eket eltávolítja.

    Args:
        log_directory: Log fájlok könyvtára (None = alapértelmezett).
        logging_enabled: Ha False, minden loggolás kikapcsol.
    """
    from logging.handlers import RotatingFileHandler

    global _log_dir, _logging_enabled
    _logging_enabled = logging_enabled

    # Loggolás kikapcsolva → mindkét logger elnémítása NullHandler-rel
    if not logging_enabled:
        for name in ("user", "swift_fan_controller_new"):
            lg = logging.getLogger(name)
            lg.handlers.clear()
            lg.addHandler(logging.NullHandler())
            lg.propagate = False
        logging.getLogger("bleak").setLevel(logging.CRITICAL)
        logging.getLogger("openant").setLevel(logging.CRITICAL)
        return

    _log_dir = resolve_log_dir(
        log_directory, default_dir=os.path.dirname(os.path.abspath(__file__))
    )
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


# Modul-szintű: a korai (settings betöltés előtti) logokat pufferelő handlerek
_early_mem_handlers: list = []


def _setup_early_logging() -> None:
    """Korai loggolás: a settings betöltése ELŐTTI logokat memóriába puffereli.

    Mivel a ``global_settings.logging`` flag csak a settings betöltése után
    ismert, a korai logokat (pl. config validációs warningok) memóriában
    tartjuk. A flag ismeretében később vagy visszajátsszuk a valódi
    handlerekre (``_flush_early_logging``), vagy eldobjuk
    (``_discard_early_logging``). Így ``logging: false`` esetén nem jön létre
    fölösleges log fájl, ``logging: true`` esetén pedig a korai warningok sem
    vesznek el.
    """
    from logging.handlers import MemoryHandler

    global _early_mem_handlers
    _early_mem_handlers = []
    for name in ("user", "swift_fan_controller_new"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        # Nagy kapacitás + magas flushLevel → nem ürül ki automatikusan
        mh = MemoryHandler(capacity=100000, flushLevel=logging.CRITICAL + 10)
        lg.addHandler(mh)
        _early_mem_handlers.append((lg, mh))
    logging.getLogger("bleak").setLevel(logging.CRITICAL)
    logging.getLogger("openant").setLevel(logging.CRITICAL)


def _flush_early_logging() -> None:
    """A pufferelt korai logokat visszajátssza a már beállított handlerekre."""
    global _early_mem_handlers
    for lg, mh in _early_mem_handlers:
        for record in mh.buffer:
            lg.handle(record)
        mh.close()
    _early_mem_handlers = []


def _discard_early_logging() -> None:
    """A pufferelt korai logokat eldobja (logging: false eset)."""
    global _early_mem_handlers
    for _lg, mh in _early_mem_handlers:
        mh.buffer.clear()
        mh.close()
    _early_mem_handlers = []


# ============================================================
# TÍPUSBIZTOS BEÁLLÍTÁS MODELLEK (kiszervezve: config al-package)
# ============================================================
# A beállítás dataclass-ok, a DEFAULT_SETTINGS, a betöltő/mentő és a
# származtatott lekérdező függvények a smart_fan_controller.config modulba
# kerültek. Itt újraexportáljuk őket a visszafelé kompatibilitásért, hogy a
# fő fájl és a tesztek továbbra is innen importálhassanak.
from smart_fan_controller.config import (
    PowerZonesConfig,
    GlobalSettingsConfig,
    HeartRateZonesConfig,
    BleConfig,
    DatasourceConfig,
    HudConfig,
    DEFAULT_SETTINGS,
    load_settings,
    get_effective_zone_mode,
)
from smart_fan_controller.config.schemas import _from_dict_int
from smart_fan_controller.config.loader import (
    _settings_to_serializable,
    _resolve_buffer_settings,
)
# A tiszta zóna-logika és átlagolás a smart_fan_controller.core csomagba került.
# Itt újraexportáljuk a visszafelé kompatibilitásért (tesztek + belső használat).
from smart_fan_controller.core import (
    calculate_power_zones,
    calculate_hr_zones,
    zone_for_power,
    zone_for_hr,
    is_valid_power,
    is_valid_hr,
    higher_wins,
    apply_zone_mode,
    compute_average,
    _RollingAverager,
    PowerAverager,
    HRAverager,
    CooldownController,
    ConsolePrinter,
    ControllerState,
    UISnapshot,
    resolve_log_dir,
    generate_tone,
)
from smart_fan_controller.handlers import (
    ANTPlusInputHandler,
    _ANTPLUS_AVAILABLE,
    BLECombinedSensor,
    BLEFanOutputController,
    BLEHRInputHandler,
    BLEPowerInputHandler,
    ZwiftUDPInputHandler,
    _BLESensorInputHandler,
    send_zone,
)
from smart_fan_controller.processors import (
    _guarded_task,
    dropout_checker_task,
    hr_processor_task,
    power_processor_task,
    zone_controller_task,
)


# ============================================================
# TISZTA ZÓNA-LOGIKA + ÁTLAGOLÁS
# ============================================================
# A zóna számítás, validáció, zóna-mód kombinálás és gördülő átlagolás
# a smart_fan_controller.core csomagba került (zones.py, averaging.py).
# A szimbólumok fent re-exportálva (calculate_power_zones, zone_for_power,
# is_valid_power, apply_zone_mode, compute_average, PowerAverager, stb.).


# ============================================================
# COOLDOWN LOGIKA
# ============================================================


# (A CooldownController a smart_fan_controller.core.cooldown modulba kerül; fent re-exportálva.)


# (A gördülő átlagoló osztályok – _RollingAverager, PowerAverager, HRAverager –
#  a smart_fan_controller.core.averaging modulba kerültek; fent re-exportálva.)


# ============================================================
# KONZOLOS KIÍRÁS (throttle-olt)
# ============================================================


# (A ConsolePrinter a smart_fan_controller.core.printers modulba kerül; fent re-exportálva.)



# ============================================================
# UI SNAPSHOT – szálbiztos adatcsere asyncio ↔ PySide6 között
# ============================================================


# (A UISnapshot a smart_fan_controller.core.state modulba kerül; fent re-exportálva.)


# ============================================================
# MEGOSZTOTT ÁLLAPOT
# ============================================================


# (A ControllerState a smart_fan_controller.core.state modulba kerül; fent re-exportálva.)


# ============================================================
# ZÓNA ELKÜLDÉSE (helper)
# ============================================================


# A send_zone(), BLE logolás és scan függvények a
# smart_fan_controller.handlers._ble modulba kerültek.
# A send_zone fent re-importálva; a _scan_ble_with_autodiscovery és a
# BLE logoló segédfüggvények csak a _ble modulon belül használatosak
# (a BLE handlerek hívják), ezért a fő modulba nincsenek re-importálva.


# BLEFanOutputController a smart_fan_controller.handlers._ble modulba került.
# Fent re-importálva a BLEFanOutputController név alatt.


# ============================================================
# ANT+ BEMENŐ ADATKEZELÉS
# ============================================================






# ============================================================
# BLE SZENZOR HANDLEREK (smart_fan_controller.handlers._ble modulba kerültek)
# ============================================================
# A _BLESensorInputHandler ősosztály és a BLEPowerInputHandler /
# BLEHRInputHandler alosztályok a smart_fan_controller.handlers._ble
# modulba kerültek. Fent re-importálva a smart_fan_controller.handlers
# csomagból (a _scan_ble_with_autodiscovery segédfüggvénnyel együtt).


# ============================================================
# ZWIFT UDP BEMENŐ ADATKEZELÉS
# ============================================================


# (A ZwiftUDPInputHandler a smart_fan_controller.handlers.zwift_udp modulba kerül; fent re-importálva.)


# ============================================================
# ASYNC PROCESSZOROK (smart_fan_controller.processors modulba kerültek)
# ============================================================
# Az 5 async processor task (power_processor_task, hr_processor_task,
# zone_controller_task, dropout_checker_task, _guarded_task) a
# smart_fan_controller.processors.processors modulba kerültek.
# Fent re-importálva a smart_fan_controller.processors csomagból.


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
        """Kiírja az indítási konfigurációs összefoglalót.

        Ha a loggolás ki van kapcsolva (``global_settings.logging`` false),
        ``print()``-tel ír, hogy a startup info akkor is megjelenjen.
        """
        # Loggolás kikapcsolva → print(); egyébként user_logger.info
        emit = user_logger.info if _logging_enabled else print

        s = self.settings
        ds: DatasourceConfig = s["datasource"]
        hrz: HeartRateZonesConfig = s["heart_rate_zones"]

        power_buf = _resolve_buffer_settings(s, "power")
        hr_buf = _resolve_buffer_settings(s, "hr")

        zone_mode = get_effective_zone_mode(s)

        emit("-" * 60)
        emit(f"  Smart Fan Controller v{__version__}  |  Power+HR → BLE Fan")
        emit("-" * 60)
        zt = s["power_zones"]
        emit(f"FTP: {zt.ftp}W | Érvényes tartomány: 0–{zt.max_watt}W")

        power_zones = calculate_power_zones(
            zt.ftp,
            zt.min_watt,
            zt.max_watt,
            zt.z1_max_percent,
            zt.z2_max_percent,
        )
        emit(f"Zóna határok: {power_zones}")

        if ds.power_source is not None:
            emit(
                f"💪 Power buffer ({ds.power_source.upper()}): "
                f"{power_buf['buffer_seconds']}s | "
                f"minta: {power_buf['minimum_samples']} | "
                f"rate: {power_buf['buffer_rate_hz']}Hz | "
                f"dropout: {power_buf['dropout_timeout']}s"
            )
        else:
            emit("💪 Power forrás: KIKAPCSOLVA (null)")
        if ds.hr_source is not None:
            emit(
                f"❤️  HR buffer    ({ds.hr_source.upper()}): "
                f"{hr_buf['buffer_seconds']}s | "
                f"minta: {hr_buf['minimum_samples']} | "
                f"rate: {hr_buf['buffer_rate_hz']}Hz | "
                f"dropout: {hr_buf['dropout_timeout']}s"
            )
        else:
            emit("❤️  HR forrás:    KIKAPCSOLVA (null)")

        emit(
            f"Cooldown: {s['global_settings'].cooldown_seconds}s  |  "
            f"0W azonnali: {'Igen' if s['power_zones'].zero_power_immediate else 'Nem'}  |  "
            f"0HR azonnali: {'Igen' if hrz.zero_hr_immediate else 'Nem'}"
        )
        ble_cfg: BleConfig = s["ble_fan"]
        if ble_cfg.device_name:
            emit(f"BLE Fan: {ble_cfg.device_name}")
        else:
            emit("BLE Fan: (auto-discovery – service UUID alapján)")
        if ble_cfg.pin_code:
            emit(f"BLE PIN: {'*' * len(ble_cfg.pin_code)}")

        # BLE szenzor auto-discovery jelzés
        if ds.power_source == DataSource.BLE and not ds.ble_power_device_name:
            emit("BLE Power: (auto-discovery – Cycling Power Service)")
        if ds.hr_source == DataSource.BLE and not ds.ble_hr_device_name:
            emit("BLE HR: (auto-discovery – Heart Rate Service)")

        emit(f"Zónamód: {zone_mode}")
        emit("-" * 60)

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
                try:
                    await self._ble_fan._write_raw("ROLLER:0")
                    user_logger.info("✓ Görgő leállítva (ROLLER:0)")
                except Exception as exc:
                    logger.warning(f"ROLLER:0 küldése sikertelen leállításkor: {exc}")
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


# (A _generate_tone a smart_fan_controller.core.helpers modulba kerül; fent re-exportálva.)


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
                wav_data = generate_tone(tones)
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
        hud_cfg: HudConfig = controller.settings["hud"]
        self._initial_opacity = max(20, min(100, hud_cfg.opacity))
        self.setWindowOpacity(self._initial_opacity / 100.0)
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
        self._alpha_slider.setValue(self._initial_opacity)
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

        self._alpha_value = QLabel(f"{self._initial_opacity}%")
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
        self._save_hud_setting("opacity", percent)

    def _on_alpha_change(self, value: int) -> None:
        self.setWindowOpacity(value / 100.0)
        self._alpha_value.setText(f"{value}%")
        self._save_hud_setting("opacity", value)

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
        """Egy HUD beállítás frissítése és mentése (csak ha save_hud_settings=True).

        Frissíti a HUD beállítást a memóriában, majd ha save_hud_settings engedélyezett,
        csak a "hud" szekciót menti a JSON-ba (nem az egész settings-et, így az egyéb
        szekciók kézi szerkesztéseit megőrzi).
        """
        settings = self._ctrl.settings
        hud_cfg: HudConfig = settings["hud"]
        # Map old key names to dataclass attribute names
        attr = key.replace(".", "_") if "." in key else key
        if hasattr(hud_cfg, attr):
            setattr(hud_cfg, attr, value)
            # Mentés: csak a "hud" szekciót frissítjük, és csak ha engedélyezett
            from smart_fan_controller.config.loader import save_hud_settings_only
            if save_hud_settings_only(self._ctrl.settings_file, hud_cfg):
                logger.info(f"HUD beállítás mentve: hud.{key} = {value}")
            elif hud_cfg.save_hud_settings:
                # save_hud_settings=True volt, de valamilyen hiba történt az íráskor
                logger.warning(f"HUD beállítás nem sikerült menteni: hud.{key} = {value}")
            # Ha save_hud_settings=False, nincs log üzenet (szándékos)

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

    # ────────── MONITOR GEOMETRIA ──────────

    def _current_screen_name(self) -> str:
        """Az ablak aktuális képernyőjének neve (vagy üres ha nem elérhető)."""
        screen = self.screen()
        if screen is not None:
            return screen.name()
        return ""

    def _restore_geometry(self) -> None:
        """Visszaállítja az ablak pozícióját/méretét az utoljára használt monitorhoz.

        Ha a mentett monitor nem létezik, az aktív (elsődleges) monitorra helyezi.
        """
        hud_cfg: HudConfig = self._ctrl.settings["hud"]
        geo_map = hud_cfg.window_geometry
        if not geo_map:
            return

        # Elérhető monitorok nevei
        available = {}
        for s in self._app.screens():
            available[s.name()] = s

        # Megpróbáljuk az utolsó használt monitort (a dict utolsó kulcsa)
        last_screen_name = list(geo_map.keys())[-1] if geo_map else ""
        if last_screen_name in available and last_screen_name in geo_map:
            rect = geo_map[last_screen_name]
            target_screen = available[last_screen_name]
        else:
            # Monitor nem létezik → aktív (elsődleges) monitor, ha van rá mentett geom
            primary = self._app.primaryScreen()
            if primary is None:
                return
            pname = primary.name()
            if pname in geo_map:
                rect = geo_map[pname]
            else:
                # Nincs semmilyen mentett geometria ehhez a monitorhoz
                return
            target_screen = primary

        # Validáljuk, hogy a pozíció a monitor területén belül van
        sg = target_screen.availableGeometry()
        x = max(sg.x(), min(rect["x"], sg.x() + sg.width() - 100))
        y = max(sg.y(), min(rect["y"], sg.y() + sg.height() - 100))
        w = max(self.minimumWidth(), min(rect["w"], sg.width()))
        h = max(self.minimumHeight(), min(rect["h"], sg.height()))
        self.setGeometry(x, y, w, h)
        self._scale = w / self._base_width

    def _save_geometry(self) -> None:
        """Elmenti az ablak pozícióját/méretét az aktuális monitorhoz."""
        screen_name = self._current_screen_name()
        if not screen_name:
            return
        geo = self.geometry()
        rect = {"x": geo.x(), "y": geo.y(), "w": geo.width(), "h": geo.height()}
        hud_cfg: HudConfig = self._ctrl.settings["hud"]
        hud_cfg.window_geometry[screen_name] = rect
        self._save_hud_setting("window_geometry", hud_cfg.window_geometry)

    # ────────── RUN / CLOSE ──────────

    def run(self) -> None:
        self._restore_geometry()
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
        self._save_geometry()
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

    # Korai logging: a settings betöltése előtti logokat memóriába puffereljük,
    # mert a logging flag még nem ismert. Így logging:false esetén nem jön létre
    # fölösleges log fájl, logging:true esetén a korai warningok sem vesznek el.
    _setup_early_logging()

    # PyInstaller frozen exe: settings.json az exe mellett keresendő
    if getattr(sys, 'frozen', False):
        _exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        _settings_path = os.path.join(_exe_dir, "settings.json")
    else:
        _script_dir = os.path.dirname(os.path.abspath(__file__))
        _settings_path = os.path.join(_script_dir, "settings.json")
    controller = FanController(_settings_path)

    # Logging konfigurálása a betöltött beállítások alapján
    _gs = controller.settings["global_settings"]
    if not _gs.logging:
        # Loggolás kikapcsolva → teljes némaság, korai logok eldobva
        _setup_logging(_gs.log_directory, logging_enabled=False)
        _discard_early_logging()
    else:
        _setup_logging(_gs.log_directory)
        # Korai (betöltés előtti) logok visszajátszása a valódi handlerekre
        _flush_early_logging()

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

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

HUDWindow: Any = None
try:
    from smart_fan_controller.ui import HUDWindow  # type: ignore[assignment]
except ImportError:
    pass



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

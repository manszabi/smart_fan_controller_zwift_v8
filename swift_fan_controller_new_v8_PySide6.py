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
from smart_fan_controller.controller import FanController
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
    logger,
    user_logger,
    setup_logging,
    setup_early_logging,
    flush_early_logging,
    discard_early_logging,
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
# A FanController osztály a smart_fan_controller.controller modulba került.
# Fent importálva (smart_fan_controller.controller).

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
    setup_early_logging()

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
        setup_logging(_gs.log_directory, logging_enabled=False)
        discard_early_logging()
    else:
        setup_logging(_gs.log_directory)
        # Korai (betöltés előtti) logok visszajátszása a valódi handlerekre
        flush_early_logging()

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

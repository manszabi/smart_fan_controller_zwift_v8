"""Alkalmazás belépőpont – asyncio event loop + PySide6 HUD összehangolása.

A ``main()`` indítja a teljes alkalmazást:
  1. Windows-specifikus asyncio event loop policy
  2. Korai logging (settings betöltés előtti pufferelés)
  3. Settings betöltés (frozen exe esetén az exe mellől, különben a script mellől)
  4. Logging véglegesítés a global_settings.logging szerint
  5. FanController asyncio futtatása külön daemon szálon
  6. PySide6 HUD a fő szálon (headless módban HUD nélkül)
  7. Signal-kezelés (SIGTERM/SIGINT) és tiszta leállítás

A domain-logika a smart_fan_controller almodulokban van; ez a modul csak
az életciklust és a szálak/Qt összehangolását végzi.
"""
from __future__ import annotations

import asyncio
import atexit
import os
import platform as _platform
import signal
import sys
import threading
import warnings
from typing import Any

# COM inicializálás threading modellje – APARTMENTTHREADED kell a Qt-nek (OLE/DnD),
# és ezt a pywinauto/PySide6 importálása ELŐTT kell beállítani, különben COM conflict lesz.
if not hasattr(sys, "coinit_flags"):
    sys.coinit_flags = 2  # type: ignore[attr-defined]  # COINIT_APARTMENTTHREADED

# A pywinauto importáláskor figyelmeztet, ha a coinit_flags-et kívülről állították be
# (lásd fent). Ez nálunk szándékos, ezért a (ártalmatlan) UserWarning-ot elnémítjuk.
# A coinit_flags beállítás MARAD érvényben – csak a warning szöveget rejtjük el.
warnings.filterwarnings(
    "ignore",
    message="Apply externally defined coinit_flags",
    category=UserWarning,
    module="pywinauto",
)

# A Qt multimédia FFmpeg-háttere induláskor egy info sort ír ki
# ("qt.multimedia.ffmpeg: Using Qt multimedia with FFmpeg ..."). Ezt a Qt logging
# szabályon keresztül némítjuk. A meglévő QT_LOGGING_RULES-t nem írjuk felül, hozzáfűzünk.
_qt_rule = "qt.multimedia.ffmpeg=false"
_existing_rules = os.environ.get("QT_LOGGING_RULES", "")
if _qt_rule not in _existing_rules:
    os.environ["QT_LOGGING_RULES"] = (
        f"{_existing_rules};{_qt_rule}" if _existing_rules else _qt_rule
    )

from smart_fan_controller.controller import FanController
from smart_fan_controller.core import (
    discard_early_logging,
    flush_early_logging,
    logger,
    setup_early_logging,
    setup_logging,
    user_logger,
)

__all__ = ["main", "_PYSIDE6_AVAILABLE"]

# PySide6 elérhetőség: a HUD opcionális (headless módban, pl. Raspberry Pi
# terminálon, az app HUD nélkül fut). A flag a main() ágválasztását vezérli.
_PYSIDE6_AVAILABLE: bool = False
try:
    from PySide6.QtWidgets import QApplication  # noqa: F401  # type: ignore[import-untyped]
    from PySide6.QtCore import QtMsgType  # noqa: F401  # type: ignore[import-untyped]

    _PYSIDE6_AVAILABLE = True
except ImportError:
    pass

# A HUD ablak a ui csomagban van; PySide6 nélkül az import elhasal (None marad).
HUDWindow: Any = None
try:
    from smart_fan_controller.ui import HUDWindow  # type: ignore[assignment]
except ImportError:
    pass


def main() -> None:
    # Korai logging: a settings betöltése előtti logokat memóriába puffereljük,
    # mert a logging flag még nem ismert. Így logging:false esetén nem jön létre
    # fölösleges log fájl, logging:true esetén a korai warningok sem vesznek el.
    setup_early_logging()

    # PyInstaller frozen exe: settings.json az exe mellett keresendő
    if getattr(sys, 'frozen', False):
        _exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        _settings_path = os.path.join(_exe_dir, "settings.json")
    else:
        # A fő belépő script könyvtára (a csomag szülője), nem ezé a modulé.
        _script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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

    # Windows: SelectorEventLoop megbízhatóbb threaded asyncio-hoz. Explicit
    # loop-példányosítás (nem event loop policy): a policy API deprecated
    # (Python 3.16-ban megszűnik), és a policy-s megoldás 3.14+ alatt már
    # nem is érvényesült.
    if _platform.system() == "Windows":
        loop: asyncio.AbstractEventLoop = asyncio.SelectorEventLoop()
    else:
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

    def _finish_shutdown() -> None:
        """Közös leállítási lépések (HUD-os és headless ág)."""
        cleanup()
        # Shutdown event jelzése → asyncio loop megbízhatóan leáll
        try:
            loop.call_soon_threadsafe(shutdown_event.set)
        except Exception as exc:
            logger.debug(f"shutdown_event.set hiba: {exc}")
        asyncio_thread.join(timeout=3.0)
        if asyncio_thread.is_alive():
            # Futó event loopot TILOS bezárni (RuntimeError-t dobna a
            # kilépési útvonalon); a daemon szál a processz végén leáll.
            logger.warning("AsyncioThread nem állt le 3s alatt – loop.close() kihagyva")
        else:
            loop.close()
        user_logger.info("\nProgram leállítva.")

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
            _finish_shutdown()
    else:
        logger.warning("PySide6 nem elérhető, HUD nélkül fut")
        user_logger.warning("⚠ PySide6 nem elérhető – HUD nélkül fut. Ctrl+C a leállításhoz.")
        try:
            asyncio_thread.join()
        except KeyboardInterrupt:
            user_logger.info("\nLeállítás (Ctrl+C)...")
        finally:
            _finish_shutdown()

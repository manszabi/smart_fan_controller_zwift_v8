"""Application entry point – coordinating the asyncio loop and the PySide6 HUD.

``main()`` starts the whole application:
  1. Windows-specific asyncio event loop policy
  2. Early logging (buffering before the settings load)
  3. Settings load (next to the exe when frozen, next to the script otherwise)
  4. Logging finalized per global_settings.logging
  5. FanController asyncio run on a separate daemon thread
  6. PySide6 HUD on the main thread (headless mode runs without a HUD)
  7. Signal handling (SIGTERM/SIGINT) and clean shutdown

The domain logic lives in the smart_fan_controller submodules; this
module only manages the lifecycle and thread/Qt coordination.
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

# COM init threading model – Qt needs APARTMENTTHREADED (OLE/DnD), and it
# must be set BEFORE importing pywinauto/PySide6, otherwise COM conflicts.
if not hasattr(sys, "coinit_flags"):
    sys.coinit_flags = 2  # type: ignore[attr-defined]  # COINIT_APARTMENTTHREADED

# pywinauto warns at import time when coinit_flags was set externally (see
# above). That is intentional here, so the (harmless) UserWarning is
# silenced. The coinit_flags setting REMAINS in effect – only the warning
# text is hidden.
warnings.filterwarnings(
    "ignore",
    message="Apply externally defined coinit_flags",
    category=UserWarning,
    module="pywinauto",
)

# The Qt multimedia FFmpeg backend prints an info line at startup
# ("qt.multimedia.ffmpeg: Using Qt multimedia with FFmpeg ..."). It is
# silenced via a Qt logging rule. The existing QT_LOGGING_RULES is not
# overwritten, we append to it.
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

# PySide6 availability: the HUD is optional (in headless mode, e.g. on a
# Raspberry Pi terminal, the app runs without a HUD). The flag drives the
# branch selection in main().
_PYSIDE6_AVAILABLE: bool = False
try:
    from PySide6.QtWidgets import QApplication  # noqa: F401  # type: ignore[import-untyped]
    from PySide6.QtCore import QtMsgType  # noqa: F401  # type: ignore[import-untyped]

    _PYSIDE6_AVAILABLE = True
except ImportError:
    pass

# The HUD window lives in the ui package; without PySide6 the import
# fails (stays None).
HUDWindow: Any = None
try:
    from smart_fan_controller.ui import HUDWindow  # type: ignore[assignment]
except ImportError:
    pass


def main() -> None:
    # Early logging: logs before the settings load are buffered in memory
    # because the logging flag is not known yet. logging:false then creates
    # no stray log file, while logging:true loses no early warnings.
    setup_early_logging()

    # PyInstaller frozen exe: settings.json is looked up next to the exe
    if getattr(sys, 'frozen', False):
        _exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        _settings_path = os.path.join(_exe_dir, "settings.json")
    else:
        # Directory of the main entry script (the package parent), not this module.
        _script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _settings_path = os.path.join(_script_dir, "settings.json")
    controller = FanController(_settings_path)

    # Configure logging based on the loaded settings
    _gs = controller.settings["global_settings"]
    if not _gs.logging:
        # Logging disabled → total silence, early logs dropped
        setup_logging(_gs.log_directory, logging_enabled=False)
        discard_early_logging()
    else:
        setup_logging(_gs.log_directory)
        # Replay the early (pre-load) logs onto the real handlers
        flush_early_logging()

    controller.print_startup_info()

    # Windows: SelectorEventLoop is more reliable for threaded asyncio.
    # Explicit loop instantiation (not an event loop policy): the policy
    # API is deprecated (removed in Python 3.16), and the policy-based
    # solution no longer took effect on 3.14+ anyway.
    if _platform.system() == "Windows":
        loop: asyncio.AbstractEventLoop = asyncio.SelectorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    cleaned_up = False
    # Shutdown event: stops the asyncio loop reliably
    shutdown_event = asyncio.Event()
    # Mutable container: the signal handler needs the HUD reference that
    # is created later. A list so it can be mutated without nonlocal.
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
        # Shutdown goes through HUD close() so the shutdown sound can
        # play. The closeEvent timer performs the real close at the end
        # of the sound, which exits the Qt event loop.
        hud = hud_ref[0]
        if hud is not None:
            try:
                from PySide6.QtCore import QMetaObject, Qt
                QMetaObject.invokeMethod(
                    hud, "close", Qt.ConnectionType.QueuedConnection,
                )
            except Exception as exc:
                # Fallback: when invokeMethod does not work, quit() immediately
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

    # SIGTERM: reliable on Unix; on Windows Popen.terminate() gives no guarantee
    # SIGINT: Ctrl+C works on both platforms
    signal.signal(signal.SIGTERM, signal_handler)
    try:
        signal.signal(signal.SIGINT, signal_handler)
    except (OSError, ValueError):
        pass  # In some environments (e.g. non-main thread) SIGINT cannot be registered

    atexit.register(cleanup)

    loop_ready = threading.Event()

    async def _run_until_shutdown() -> None:
        """Run the controller until shutdown_event."""
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
        # Signal that the event loop runs and is ready to accept work
        loop.call_soon(loop_ready.set)
        try:
            loop.run_until_complete(_run_until_shutdown())
        except Exception as exc:
            # Fix #22: log the full traceback
            logger.error(f"AsyncioThread hiba: {exc}", exc_info=True)
        finally:
            loop_ready.set()  # On an error exit, never block forever

    asyncio_thread = threading.Thread(
        target=run_asyncio, daemon=True, name="AsyncioThread"
    )
    asyncio_thread.start()

    # Wait until the asyncio event loop has actually started (max 5 s)
    loop_ready.wait(timeout=5.0)

    def _finish_shutdown() -> None:
        """Shared shutdown steps (HUD and headless branches)."""
        cleanup()
        # Signal the shutdown event → the asyncio loop stops reliably
        try:
            loop.call_soon_threadsafe(shutdown_event.set)
        except Exception as exc:
            logger.debug(f"shutdown_event.set hiba: {exc}")
        asyncio_thread.join(timeout=3.0)
        if asyncio_thread.is_alive():
            # A running event loop must NOT be closed (it would raise a
            # RuntimeError on the exit path); the daemon thread dies with
            # the process.
            logger.warning("AsyncioThread nem állt le 3s alatt – loop.close() kihagyva")
        else:
            loop.close()
        user_logger.info("\nProgram leállítva.")

    # Fix #20: PySide6 is optional – headless mode runs without a HUD
    if _PYSIDE6_AVAILABLE:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import qInstallMessageHandler, QtMsgType

        # pywinauto already set the process DPI awareness, so the Qt
        # SetProcessDpiAwarenessContext() call fails with "access denied".
        # A harmless warning – suppressed to keep the console output clean.
        def _qt_message_filter(mode: QtMsgType, _context: Any, message: str) -> None:
            if "SetProcessDpiAwarenessContext" in message:
                return  # suppress
            sys.stderr.write(message + "\n")

        qInstallMessageHandler(_qt_message_filter)
        app = QApplication(sys.argv)
        qInstallMessageHandler(None)  # type: ignore[arg-type]  # restore the default handler
        hud = HUDWindow(controller, app)
        hud_ref[0] = hud
        try:
            hud.run()
        except KeyboardInterrupt:
            user_logger.info("\nLeállítás (Ctrl+C)...")
        finally:
            # If the HUD has not begun closing yet, start the shutdown sound
            if not getattr(hud, "_closing", False):
                hud.close()  # closeEvent phase 1: sound starts, event.ignore()
                # The Qt event loop no longer runs; pump the events manually
                # while the shutdown sound plays
                duration_ms = hud.sound.sound_duration_ms("hud_shutdown")
                if duration_ms > 0:
                    from PySide6.QtCore import QElapsedTimer
                    elapsed = QElapsedTimer()
                    elapsed.start()
                    while elapsed.elapsed() < duration_ms + 150:
                        app.processEvents()
            # Guarantee the final close in every case
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

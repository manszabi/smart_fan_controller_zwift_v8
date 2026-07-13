"""Smart Fan Controller orchestrator – component wiring and lifecycle.

Responsibilities of FanController:
  1. Loading and validating the settings
  2. Starting the asyncio tasks and the ANT+ thread
  3. Managing the BLE fan, power input (BLE/ANT+/UDP) and HR input
  4. Auto-launching the Zwift application (on Windows)
  5. Graceful shutdown: task cancel, BLE disconnect, ANT+ stop, process kill
"""

import asyncio
import os
import platform as _platform
import subprocess
import sys
import threading
from typing import Any

from smart_fan_controller.config import (
    BleConfig,
    DataSource,
    DatasourceConfig,
    HeartRateZonesConfig,
    PowerZonesConfig,
    get_effective_zone_mode,
    load_settings,
)
from smart_fan_controller.config.loader import _resolve_buffer_settings
from smart_fan_controller.core import (
    CooldownController,
    ConsolePrinter,
    ControllerState,
    HRAverager,
    PowerAverager,
    calculate_hr_zones,
    calculate_power_zones,
    is_logging_enabled,
    logger,
    user_logger,
)
from smart_fan_controller.handlers import (
    ANTPlusInputHandler,
    BLECombinedSensor,
    BLEFanOutputController,
    BLEHRInputHandler,
    BLEPowerInputHandler,
    ZwiftUDPInputHandler,
    _ANTPLUS_AVAILABLE,
)
from smart_fan_controller.processors import (
    _guarded_task,
    dropout_checker_task,
    hr_processor_task,
    power_processor_task,
    zone_controller_task,
)

from smart_fan_controller import __version__  # single source of the version

__all__ = ["FanController"]


class FanController:
    """The main orchestrator of the Smart Fan Controller.

    Wires all the components together, starts the asyncio tasks and
    threads, and takes care of the clean shutdown.

    Startup order:
        1. Load the settings
        2. Compute the zone boundaries
        3. Create the averagers, cooldown, printer
        4. Start the BLE fan output asyncio task
        5. Start the BLE power/HR input asyncio tasks (when needed)
        6. Start the Zwift UDP input asyncio task (when needed)
        7. Start the ANT+ thread (when needed)
        8. Start the power/HR processor asyncio tasks
        9. Start the zone controller asyncio task
        10. Start the dropout checker asyncio task
        11. Main loop: wait for Ctrl+C / SIGTERM
        12. Shutdown: stop every task and thread
    """

    def __init__(self, settings_file: str = "settings.json") -> None:
        self.settings_file = settings_file
        self.settings = load_settings(settings_file)
        self._antplus_handler: ANTPlusInputHandler | None = None
        self._antplus_thread: threading.Thread | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._running = True
        self._zwift_proc: subprocess.Popen[Any] | None = None
        # Handler refs (for the HUD and the shutdown)
        self._ble_fan: BLEFanOutputController | None = None
        self._ble_power: BLEPowerInputHandler | None = None
        self._ble_hr: BLEHRInputHandler | None = None
        self._zwift_udp: ZwiftUDPInputHandler | None = None
        self._state: ControllerState | None = None
        self._cooldown_ctrl: CooldownController | None = None
        self._ble_sensor_handler: BLECombinedSensor | None = None
        # The event loop running under run() – stop() is called from another
        # thread (Qt main thread / signal handler), and Task.cancel() is not
        # thread-safe, so the cancels must be scheduled onto this loop.
        self._loop: asyncio.AbstractEventLoop | None = None
        # Shutdown signal for the blocking waits (running in to_thread):
        # executor threads are NOT daemon threads, exit would wait for them
        # – the wait(...) calls break immediately on this event.
        self._shutdown_evt = threading.Event()

    @property
    def state(self) -> ControllerState | None:
        """Current controller state (None before run() has started)."""
        return self._state

    @property
    def ble_fan(self) -> BLEFanOutputController | None:
        """BLE fan output controller (None when absent)."""
        return self._ble_fan

    @property
    def cooldown_ctrl(self) -> CooldownController | None:
        """Cooldown controller (None before run() has started)."""
        return self._cooldown_ctrl

    def __repr__(self) -> str:
        ds: DatasourceConfig = self.settings["datasource"]
        return (
            f"FanController(running={self._running}, "
            f"power_src={ds.power_source}, "
            f"hr_src={ds.hr_source}, "
            f"tasks={len(self._tasks)})"
        )

    @staticmethod
    def is_process_running(process_name: str) -> bool:
        """Check whether a Windows process with the given name is running.

        Uses the ``tasklist`` command, no ``psutil`` required.
        """
        if _platform.system() != "Windows":
            return False
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
                # No console flash even under windowed (pythonw/noconsole) runs
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return process_name.lower() in result.stdout.lower()
        except (subprocess.TimeoutExpired, OSError):
            return False

    @staticmethod
    def _find_zwift_launcher() -> str | None:
        """Locate the path of ZwiftLauncher.exe.

        Search order:
          1. Windows Registry (Uninstall keys)
          2. Known install paths
        """
        if _platform.system() != "Windows":
            return None

        # --- 1. Registry search ---
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
            pass  # winreg unavailable (not Windows)

        # --- 2. Known paths ---
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
        """Ensure that the Zwift application is running.

        When ZwiftApp.exe is not running:
          1. Locate and start ZwiftLauncher.exe
          2. Wait for the launcher window via pywinauto
          3. Handle a potential update (wait for the "Let's Go" button)
          4. Click the "Let's Go" button
          5. Wait until ZwiftApp.exe starts
        """
        try:
            from pywinauto import Application as WinAutoApp  # type: ignore[import-untyped]
            _PYWINAUTO_AVAILABLE = True
        except ImportError:
            WinAutoApp = None  # type: ignore[assignment]
            _PYWINAUTO_AVAILABLE = False

        ds: DatasourceConfig = self.settings["datasource"]
        if not ds.zwift_auto_launch:
            logger.info("Zwift auto-launch kikapcsolva a beállításokban.")
            return

        if _platform.system() != "Windows":
            logger.info("Zwift auto-launch csak Windows-on támogatott.")
            return

        # Already running?
        if self.is_process_running("ZwiftApp.exe"):
            logger.info("ZwiftApp.exe már fut, auto-launch kihagyva.")
            return

        # Determine the launcher path
        launcher_path: str | None = ds.zwift_launcher_path
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

        # Start the launcher
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

        # UI automation (pywinauto)
        if not _PYWINAUTO_AVAILABLE:
            logger.warning(
                "pywinauto nincs telepítve – a 'Let's Go' gombra manuálisan kell "
                "kattintani. Telepítés: pip install pywinauto"
            )
            # Fallback: simply wait for ZwiftApp.exe to appear
            user_logger.info("⏳ Várakozás a ZwiftApp.exe indulására (kattints a 'Let's Go' gombra)...")
            for _ in range(180):  # max 6 minutes
                if self._shutdown_evt.wait(2):
                    return  # shutdown requested – stop waiting
                if self.is_process_running("ZwiftApp.exe"):
                    logger.info("ZwiftApp.exe elindult.")
                    user_logger.info("✅ ZwiftApp.exe elindult!")
                    return
            logger.warning("ZwiftApp.exe nem indult el 6 perc alatt.")
            user_logger.warning("⚠️  ZwiftApp.exe nem indult el 6 perc alatt.")
            return

        # --- pywinauto automation with a retry loop ---
        # The launcher may close and reopen its window during an update, so
        # a retry loop is used instead of relying on a single connect + wait.
        max_attempts = 10
        attempt_interval = 30  # seconds between attempts
        for attempt in range(1, max_attempts + 1):
            # ZwiftApp.exe may have started meanwhile (e.g. already logged in)
            if self.is_process_running("ZwiftApp.exe"):
                logger.info("ZwiftApp.exe elindult (frissítés/auto-login után).")
                user_logger.info("✅ ZwiftApp.exe elindult!")
                return

            # Check whether the launcher process is still running
            if not self.is_process_running("ZwiftLauncher.exe"):
                logger.info(
                    f"ZwiftLauncher.exe nem fut (próba {attempt}/{max_attempts}). "
                    f"Lehet hogy újraindul frissítés után..."
                )
                if attempt < max_attempts:
                    if self._shutdown_evt.wait(attempt_interval):
                        return  # shutdown requested
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

                # Debug: list every child control of the window (incl. web content)
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

                # Find the "LET'S GO" button (regex: any apostrophe variant)
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
                # Debug: list the titles of every visible window
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
                    if self._shutdown_evt.wait(attempt_interval):
                        return  # shutdown requested
                else:
                    logger.warning(
                        f"Zwift Launcher UI automatizáció sikertelen {max_attempts} "
                        f"próba után: {exc}"
                    )
                    user_logger.warning(f"⚠️  Launcher automatizáció sikertelen: {exc}")
                    user_logger.info("    Kattints manuálisan a 'Let's Go' gombra!")

        # Wait for ZwiftApp.exe to appear (after a manual or auto click)
        if not self.is_process_running("ZwiftApp.exe"):
            user_logger.info("⏳ Várakozás a ZwiftApp.exe indulására...")
            for _ in range(120):  # max 4 minutes
                if self._shutdown_evt.wait(2):
                    return  # shutdown requested
                if self.is_process_running("ZwiftApp.exe"):
                    logger.info("ZwiftApp.exe sikeresen elindult.")
                    user_logger.info("✅ ZwiftApp.exe elindult!")
                    return
            logger.warning("ZwiftApp.exe nem indult el 4 perc alatt.")
            user_logger.warning("⚠️  ZwiftApp.exe nem indult el 4 perc alatt.")

    def _start_zwift_subprocess(self, script_name: str) -> None:
        """Start a Zwift subprocess (zwift_api_polling).

        Stops a possibly still-running previous process, then starts the
        new one. The result is kept in self._zwift_proc.
        """
        # Stop a possible previous process
        if self._zwift_proc is not None and self._zwift_proc.poll() is None:
            try:
                self._zwift_proc.terminate()
                self._zwift_proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._zwift_proc.kill()
                self._zwift_proc.wait()  # avoid a zombie
            except OSError:
                pass
            finally:
                self._zwift_proc = None

        # Pass the settings.json path – the subprocess reads the credentials
        # and settings from the "zwift_api" section.
        settings_arg = ["--settings", os.path.abspath(self.settings_file)]
        # Separate window (own console) per the zwift_api.separate_window flag.
        zwift_cfg = self.settings.get("zwift_api")
        separate_window = getattr(zwift_cfg, "separate_window", True)

        try:
            if getattr(sys, "frozen", False):
                exe_dir = os.path.dirname(os.path.abspath(sys.executable))
                cmd = [os.path.join(exe_dir, f"{script_name}.exe"), *settings_arg]
            else:
                # -m module run: independent of the script path (the package
                # is importable), instead of the thin shim (zwift_api_polling.py).
                cmd = [sys.executable, "-m", "smart_fan_controller.zwift_api", *settings_arg]

            if _platform.system() == "Windows" and separate_window:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 4  # SW_SHOWNOACTIVATE
                creation_flags = subprocess.CREATE_NEW_CONSOLE
            else:
                # No separate window: the subprocess runs in the background (or non-Windows).
                startupinfo = None
                creation_flags = 0

            popen_kwargs: dict[str, Any] = dict(
                stdin=subprocess.DEVNULL,
            )
            if startupinfo is not None:
                popen_kwargs["startupinfo"] = startupinfo
                popen_kwargs["creationflags"] = creation_flags
            else:
                popen_kwargs["close_fds"] = True

            self._zwift_proc = subprocess.Popen(cmd, **popen_kwargs)
            logger.info(f"{script_name} elindítva (PID: {self._zwift_proc.pid})")

        except FileNotFoundError as exc:
            logger.error(f"{script_name} nem található: {exc}")
        except OSError as exc:
            logger.error(f"{script_name} indítása sikertelen: {exc}")
        except Exception as exc:
            logger.error(f"Váratlan hiba {script_name} indításakor: {exc}")

    def print_startup_info(self) -> None:
        """Print the startup configuration summary.

        When logging is disabled (``global_settings.logging`` false) it
        writes via ``print()`` so the startup info still appears.
        """
        # Logging disabled → print(); user_logger.info otherwise
        emit = user_logger.info if is_logging_enabled() else print

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

        # BLE sensor auto-discovery notice
        if ds.power_source == DataSource.BLE and not ds.ble_power_device_name:
            emit("BLE Power: (auto-discovery – Cycling Power Service)")
        if ds.hr_source == DataSource.BLE and not ds.ble_hr_device_name:
            emit("BLE HR: (auto-discovery – Heart Rate Service)")

        emit(f"Zónamód: {zone_mode}")
        emit("-" * 60)

    async def run(self) -> None:
        """The controller's main asyncio coroutine – starts everything and waits."""
        self._loop = asyncio.get_running_loop()
        self._tasks = []
        s = self.settings
        ds: DatasourceConfig = s["datasource"]
        hrz_cfg: HeartRateZonesConfig = s["heart_rate_zones"]
        hr_enabled = hrz_cfg.enabled
        zone_mode = get_effective_zone_mode(s)

        # --- Compute the zone boundaries ---
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

        # --- Auto-launch the Zwift application (for any data source) ---
        # to_thread: does not block the asyncio event loop (signal handling, etc.)
        await asyncio.to_thread(self._ensure_zwift_running)

        # --- Create the components ---
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

        # --- Input data sources ---
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
            # Proactive warning into the visible (main app) log when the Zwift
            # credentials are missing: without them the subprocess cannot fetch
            # data, and its error in a separate window / background easily
            # escapes the user's attention. The subprocess itself exits cleanly.
            zcfg = s.get("zwift_api")
            has_cfg_cred = bool(getattr(zcfg, "username", "") and getattr(zcfg, "password", ""))
            has_env_cred = bool(os.environ.get("ZWIFT_USERNAME") and os.environ.get("ZWIFT_PASSWORD"))
            if not has_cfg_cred and not has_env_cred:
                user_logger.warning(
                    "⚠ Zwift API: nincs megadva username/password a settings.json "
                    "'zwift_api' szekciójában (és nincs ZWIFT_USERNAME/ZWIFT_PASSWORD "
                    "környezeti változó sem) – a Zwift adatlekérés nem fog elindulni."
                )

            # Start the Zwift API polling subprocess
            self._start_zwift_subprocess("zwift_api_polling")

            # UDP handler receiving the packets from zwift_api_polling.py
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

        # --- Processing and controller coroutines ---
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
                # Send LEVEL:0 before shutdown – turn the fan off
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

    def stop(self) -> None:
        """Stop every task and thread.

        Note: task.cancel() only sends a request to the event loop; the
        actual cancellation happens on the next iteration of the asyncio
        loop. The asyncio_thread.join(timeout=3.0) call in main() gives
        enough time for a clean shutdown.
        """
        self._running = False
        # Break the blocking waits running in to_thread (Zwift launch watch)
        # immediately – exit would otherwise wait for them
        self._shutdown_evt.set()
        loop = self._loop
        for task in self._tasks:
            if task.done():
                continue
            try:
                if loop is not None and loop.is_running():
                    # stop() is typically called from the Qt main thread while
                    # the tasks live on the asyncio thread's loop – Task.cancel()
                    # is not thread-safe, it must be scheduled onto the loop.
                    loop.call_soon_threadsafe(task.cancel)
                else:
                    task.cancel()
            except Exception as exc:
                logger.debug(f"Task cancel hiba: {exc}")
        if self._antplus_handler:
            self._antplus_handler.stop()
        if self._antplus_thread and self._antplus_thread.is_alive():
            self._antplus_thread.join(timeout=5.0)
            if self._antplus_thread.is_alive():
                logger.warning("ANT+ szál nem állt le 5s alatt!")

        # Fix #17: close the Zwift UDP transport
        if self._zwift_udp is not None:
            t = getattr(self._zwift_udp, "_transport", None)
            if t is not None:
                try:
                    t.close()
                except Exception as exc:
                    logger.debug(f"Zwift UDP transport bezárási hiba: {exc}")

        # Stop the Zwift subprocess
        if self._zwift_proc is not None:
            if self._zwift_proc.poll() is None:  # only when still running
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

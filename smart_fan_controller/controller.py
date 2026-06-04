"""Smart Fan Controller orchestrátora – komponensek szervezése és életciklus-kezelés.

A FanController feladata:
  1. Beállítások betöltése és validálása
  2. Asyncio task-ok és ANT+ szál indítása
  3. BLE fan, power input (BLE/ANT+/UDP), HR input (BLE/ANT+/UDP) kezelése
  4. Zwift alkalmazás automatikus indítása (Windows-on)
  5. Graceful shutdown: task cancel, BLE disconnect, ANT+ stop, process kill
"""

import asyncio
import logging
import os
import platform as _platform
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

from smart_fan_controller.config import (
    BleConfig,
    DataSource,
    DatasourceConfig,
    GlobalSettingsConfig,
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
    discard_early_logging,
    flush_early_logging,
    generate_tone,
    logger,
    setup_early_logging,
    setup_logging,
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
    send_zone,
)
from smart_fan_controller.processors import (
    _guarded_task,
    dropout_checker_task,
    hr_processor_task,
    power_processor_task,
    zone_controller_task,
)

__version__ = "8.0.0"
__all__ = ["FanController"]

# Global state for logging
_logging_enabled: bool = True


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

"""ANT+ Input handler – saját daemon szálban futó asyncio interfacing.

Az openant könyvtár blokkoló API-t használ, ezért saját daemon szálban fut.
Az érkező adatokat az asyncio event loop-ba hídalkotja (asyncio.run_coroutine_threadsafe)
és az asyncio queue-kba teszi.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

from smart_fan_controller.config.schemas import DataSource, DatasourceConfig
from smart_fan_controller.core.helpers import resolve_log_dir

logger = logging.getLogger("swift_fan_controller_new")
user_logger = logging.getLogger("user")

# Openant library imports – opcionális
Node: Any = None
ANTPLUS_NETWORK_KEY: Any = None
PowerMeter: Any = None
PowerData: Any = None
HeartRate: Any = None
HeartRateData: Any = None

_ANTPLUS_AVAILABLE: bool = False
try:
    from openant.easy.node import Node  # type: ignore[import-untyped]
    from openant.devices import ANTPLUS_NETWORK_KEY  # type: ignore[import-untyped, assignment]
    from openant.devices.power_meter import PowerMeter, PowerData  # type: ignore[import-untyped]
    from openant.devices.heart_rate import HeartRate, HeartRateData  # type: ignore[import-untyped]

    _ANTPLUS_AVAILABLE = True  # type: ignore[misc]
except ImportError:
    pass

__all__ = [
    "ANTPlusInputHandler",
    "_ANTPLUS_AVAILABLE",
]


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

    # Induláskor / újracsatlakozáskor a USB ANT+ stick libusb0 meghajtója pár száz
    # ms-ot igényelhet, amíg elérhetővé válik. Windows-on tipikus átmeneti hibák:
    # "could not claim interface (resource busy)" és "device not recognize command".
    # Ennyit várunk az első próbálkozás előtt, hogy a stick készen álljon.
    INITIAL_GRACE_DELAY = 0.5
    # Az első ennyi sikertelen kísérletet csak fejlesztői logba (info) írjuk, nem
    # riasztjuk a felhasználót – ezek tipikusan átmeneti USB-init hibák, amiket a
    # retry magától helyrehoz. E fölött már user-facing warning megy.
    QUIET_RETRIES = 3

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

        # Logging beállítások a settings-ből
        gs = settings["global_settings"]
        self._log_dir: str = resolve_log_dir(gs.log_directory)
        self._logging_enabled: bool = gs.logging

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
            user_logger.info(f"✓ {sensor_label} csatlakozva: id={dev_id} ({info})")
            self._log_ant_device_to_file(device_type_str, dev_id, info)
        return _on_found

    def _ant_log_path(self) -> str:
        """Visszaadja az ant_devices.log teljes útvonalát a konfigurált log könyvtárban."""
        return os.path.join(self._log_dir, "ant_devices.log")

    def _log_ant_device_to_file(
        self,
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
        # Loggolás kikapcsolva → nem írunk eszköz-log fájlt sem
        if not self._logging_enabled:
            return
        # Egyedi kulcs: "típus | device_id"
        entry_key = f"{device_type} | {device_id}"

        # Meglévő bejegyzések ellenőrzése
        existing_entries: set[str] = set()
        try:
            with open(self._ant_log_path(), "r", encoding="utf-8") as f:
                for line in f:
                    # Sorok formátuma: "  TÍPUS | DEVICE_ID | info"
                    parts = line.split("|")
                    if len(parts) >= 2:
                        existing_entries.add(f"{parts[0].strip()} | {parts[1].strip()}")
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(f"Nem sikerült olvasni a {self._ant_log_path()} fájlt: {exc}")

        if entry_key in existing_entries:
            return

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self._ant_log_path(), "a", encoding="utf-8") as f:
                f.write(
                    f"  {device_type:20s} | {device_id} | {device_info} "
                    f"| @ {timestamp}\n"
                )
        except OSError as exc:
            logger.warning(f"Nem sikerült írni a {self._ant_log_path()} fájlba: {exc}")

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

    def _log_retry(self, detail: str, retry_count: int) -> None:
        """Retry-üzenet logolása a próbálkozások számától függő szinten.

        Az első ``QUIET_RETRIES`` kísérlet csak a fejlesztői logba (info) kerül,
        mert ezek tipikusan átmeneti USB-init hibák (pl. libusb "resource busy"
        induláskor), amiket a retry-logika magától helyrehoz. E fölött már
        user-facing ``warning`` megy, hogy a tartós hiba látható maradjon.

        Args:
            detail: A hiba szöveges leírása (kivétel vagy állapot).
            retry_count: Az aktuális próbálkozás sorszáma.
        """
        if retry_count <= self.QUIET_RETRIES:
            logger.info(
                f"ANT+ átmeneti hiba ({retry_count}/{self._max_retries}), "
                f"újrapróbálkozás: {detail}"
            )
        else:
            user_logger.warning(
                f"⚠ ANT+ hiba ({retry_count}/{self._max_retries}): {detail}"
            )

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

        # Kezdeti grace-delay: esélyt adunk a USB ANT+ stick libusb0 meghajtójának
        # elérhetővé válni, mielőtt az első claim/open próbát megtennénk. Ez
        # kiküszöböli a tipikus induláskori "resource busy" / "device not recognize
        # command" átmeneti hibákat. (Megszakítható, ha közben leállítás jön.)
        if self.INITIAL_GRACE_DELAY > 0:
            self._stop_event.wait(timeout=self.INITIAL_GRACE_DELAY)

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
                    self._log_retry("eszköz nem válaszol", retry_count)

            except Exception as exc:
                if not self._running.is_set():
                    break
                retry_count += 1
                self._log_retry(str(exc), retry_count)

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

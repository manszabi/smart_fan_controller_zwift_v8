"""ANT+ input handler – asyncio interfacing running in its own daemon thread.

The openant library exposes a blocking API, so it runs in its own daemon
thread. Incoming data is bridged into the asyncio event loop
(loop.call_soon_threadsafe + put_nowait) and placed into the asyncio
queues.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any

from smart_fan_controller.config.schemas import DataSource, DatasourceConfig
from smart_fan_controller.core.helpers import resolve_log_dir

logger = logging.getLogger("zwift_fan_controller_new")
user_logger = logging.getLogger("user")

# openant library imports – optional
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
    """ANT+ power and HR data source handler in its own daemon thread.

    The openant library exposes a blocking API, so it runs in its own
    daemon thread. Incoming data is bridged into the asyncio event loop
    (loop.call_soon_threadsafe + put_nowait) and placed into the queues.

    When ant_power_device_id / ant_hr_device_id is set (non-zero) in the
    settings it connects to the specific device. When 0, the first
    available (wildcard) device is used.

    Attributes:
        power_queue: asyncio.Queue for the power data.
        hr_queue: asyncio.Queue for the HR data.
        loop: Reference to the main asyncio event loop.
    """

    MAX_RETRY_COOLDOWN = 30
    WATCHDOG_TIMEOUT = 30  # Stop the node after this many seconds without data

    # At startup / reconnect the USB ANT+ stick's libusb0 driver may need a
    # few hundred ms to become available. Typical transient errors on
    # Windows: "could not claim interface (resource busy)" and "device not
    # recognize command". Wait this long before the first attempt so the
    # stick is ready.
    INITIAL_GRACE_DELAY = 0.5
    # The first this-many failed attempts only go to the developer log
    # (info), the user is not alerted – these are typically transient USB
    # init errors the retry fixes on its own. Above it a user-facing
    # warning is emitted.
    QUIET_RETRIES = 3

    def __init__(
        self,
        settings: dict[str, Any],
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

        # ANT+ device IDs (0 = wildcard / first available)
        self._power_device_id: int = self.ds.ant_power_device_id
        self._hr_device_id: int = self.ds.ant_hr_device_id

        # Reconnect settings from the settings (uses the larger of the two
        # because power and HR share a single common ANT+ thread)
        self._reconnect_delay: int = max(
            self.ds.ant_power_reconnect_interval,
            self.ds.ant_hr_reconnect_interval,
        )
        self._max_retries: int = max(
            self.ds.ant_power_max_retries,
            self.ds.ant_hr_max_retries,
        )

        self._running = threading.Event()
        self._stop_event = threading.Event()  # watchdog stop signal
        self._node: Any | None = None
        self._devices: list[Any] = []
        # Lock guarding the swap of the node/devices refs: stop() from the
        # main thread and _thread_loop on its own thread may both tear the
        # node down.
        self._node_lock = threading.Lock()
        # Windows driver hint: printed only once
        self._usb_hint_shown = False
        self._lastdata: float = 0.0  # time of the last data of any kind (thread loop)
        self._node_started: float = 0.0  # node.start() launch time (for the watchdog)
        self.power_lastdata: float = 0.0
        self.hr_lastdata: float = 0.0
        self.power_connected: bool = False
        self.hr_connected: bool = False

        # Logging settings from the settings
        gs = settings["global_settings"]
        self._log_dir: str = resolve_log_dir(gs.log_directory)
        self._logging_enabled: bool = gs.logging

    def start(self) -> threading.Thread:
        """Start the ANT+ daemon thread.

        Returns:
            The created daemon threading.Thread object.
        """
        self._running.set()
        self._stop_event.clear()  # so the watchdog can restart
        t = threading.Thread(
            target=self._thread_loop, daemon=True, name="ANTPlus-Thread"
        )
        t.start()

        # Startup log: which device IDs it starts with
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
        """Stop the ANT+ thread and the ANT+ node."""
        self._running.clear()
        self._stop_event.set()  # wake and stop the watchdog thread
        self._stop_node()

    def _queue_put(self, queue: asyncio.Queue[float], value: float, label: str) -> None:
        """Put into the queue – runs on the asyncio event loop thread.

        put_nowait + drop on a full queue, consistent with the BLE/UDP
        handlers (only the freshest data matters)."""
        try:
            queue.put_nowait(value)
        except asyncio.QueueFull:
            logger.debug("ANT+ %s queue teli, adat elvetve", label)

    def _put_power(self, power: float) -> None:
        """Put a power value into the asyncio queue (thread-safe, non-blocking).

        call_soon_threadsafe: allocates no coroutine/Future per sample
        (unlike run_coroutine_threadsafe), and a queue error is not lost
        silently either."""
        try:
            self.loop.call_soon_threadsafe(
                self._queue_put, self.power_queue, float(power), "power"
            )
        except RuntimeError:
            pass  # The loop already stopped – normal during shutdown

    def _put_hr(self, hr: int) -> None:
        """Put an HR value into the asyncio queue (thread-safe, non-blocking)."""
        try:
            self.loop.call_soon_threadsafe(
                self._queue_put, self.hr_queue, float(hr), "HR"
            )
        except RuntimeError:
            pass  # The loop already stopped – normal during shutdown

    def _on_any_broadcast(self, data: Any) -> None:
        """Watchdog heartbeat: every incoming ANT+ broadcast refreshes the timestamp.

        The openant on_update callback fires for every data packet,
        regardless of whether the event count changed (so also when the
        power meter sends 0 W because the user is not pedaling).
        This keeps the watchdog free of false positives.
        """
        self._lastdata = time.monotonic()

    def _on_data(self, page: Any, page_name: str, data: Any) -> None:
        """ANT+ data packet callback – routes power and HR data to the queues.

        Only called when NEW measurement data arrived (event count
        changed). The watchdog heartbeat is handled separately by
        _on_any_broadcast.
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
        """Create an on_found callback for the given sensor.

        The openant on_found() is called without parameters (staticmethod).
        device_ref is the openant device object reference exposing the
        device_id attribute (in wildcard mode openant sets it to the ID of
        the first device found automatically).

        Args:
            sensor_label: Log prefix (e.g. "ANT+ Power").
            device_type_str: Log file device type (e.g. "PowerMeter").
            device_ref: Reference to the openant device object.

        Returns:
            A zero-argument callback function.
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
        """Return the full path of ant_devices.log in the configured log dir."""
        return os.path.join(self._log_dir, "ant_devices.log")

    def _log_ant_device_to_file(
        self,
        device_type: str,
        device_id: int,
        device_info: str,
    ) -> None:
        """Append a discovered ANT+ device to the ant_devices.log file.

        Only writes when the device (device_type + device_id) is not in
        the file yet. Creates the file when it does not exist.

        Args:
            device_type: Type of the device (e.g. "PowerMeter", "HeartRate").
            device_id: The ANT+ device number.
            device_info: Additional information about the device.
        """
        # Logging disabled → no device log file is written either
        if not self._logging_enabled:
            return
        # Unique key: "type | device_id"
        entry_key = f"{device_type} | {device_id}"

        # Check the existing entries
        existing_entries: set[str] = set()
        try:
            with open(self._ant_log_path(), "r", encoding="utf-8") as f:
                for line in f:
                    # Line format: "  TYPE | DEVICE_ID | info"
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
        """Initialize the ANT+ node and register the devices.

        When ant_power_device_id / ant_hr_device_id is set (non-zero) it
        connects to the specific device. When 0, wildcard mode (first
        available).
        """
        if not _ANTPLUS_AVAILABLE:
            raise RuntimeError("openant könyvtár nem elérhető")
        node = Node()
        node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)
        devices: list[Any] = []

        if self.ds.power_source == DataSource.ANTPLUS:
            pid = self._power_device_id
            meter = PowerMeter(node, device_id=pid)
            meter.on_found = self._make_on_found("ANT+ Power", "PowerMeter", meter)
            meter.on_device_data = self._on_data
            meter.on_update = self._on_any_broadcast
            devices.append(meter)

        if self.ds.hr_source == DataSource.ANTPLUS and self.hr_enabled:
            hid = self._hr_device_id
            hr_monitor = HeartRate(node, device_id=hid)
            hr_monitor.on_found = self._make_on_found("ANT+ HR", "HeartRate", hr_monitor)
            hr_monitor.on_device_data = self._on_data
            hr_monitor.on_update = self._on_any_broadcast
            devices.append(hr_monitor)

        # Publish only the fully built node (under the lock) so that
        # stop()/watchdog never sees a half-initialized state.
        with self._node_lock:
            self._node = node
            self._devices = devices

    def _stop_node(self) -> None:
        """Stop and release the ANT+ node (thread-safe, idempotent).

        The references are taken out atomically under the lock, so even
        when stop() (main thread) and _thread_loop (ANT thread) call it
        concurrently, only one of them tears the node down. The connected
        flags are reset to False because openant has no on_lost callback.
        """
        self.power_connected = False
        self.hr_connected = False
        with self._node_lock:
            node = self._node
            devices = self._devices
            self._node = None
            self._devices = []
        try:
            for d in devices:
                try:
                    d.close_channel()
                except Exception as exc:
                    logger.debug(f"ANT+ csatorna bezárási hiba: {exc}")
            if node:
                node.stop()
        except Exception as exc:
            logger.debug(f"ANT+ cleanup hiba: {exc}")

    def _log_retry(self, detail: str, retry_count: int) -> None:
        """Log a retry message at a level depending on the attempt count.

        The first ``QUIET_RETRIES`` attempts only go to the developer log
        (info), since these are typically transient USB init errors (e.g.
        libusb "resource busy" at startup) the retry logic fixes on its
        own. Above it a user-facing ``warning`` is emitted so a persistent
        failure stays visible.

        Args:
            detail: Textual description of the error (exception or state).
            retry_count: Sequence number of the current attempt.
        """
        # Windows: the most common PERSISTENT failure is the missing
        # libusb/WinUSB driver. For that we emit one targeted, actionable
        # message instead of repeating the raw error text. Note: the word
        # "libusb" can appear in TRANSIENT startup errors too (e.g.
        # claim/resource busy), so we only alert on it after the quiet
        # attempts (QUIET_RETRIES) are exhausted – "No backend available"
        # (the backend/DLL missing entirely) never heals itself, so that
        # one alerts immediately.
        low = detail.lower()
        if not self._usb_hint_shown and (
            "no backend available" in low
            or ("libusb" in low and retry_count > self.QUIET_RETRIES)
        ):
            self._usb_hint_shown = True
            user_logger.warning(
                "⚠ ANT+ USB stick nem érhető el – valószínűleg hiányzik a "
                "libusb/WinUSB meghajtó. Windows 11-en: telepíts WinUSB "
                "meghajtót az ANT+ stickre (pl. a Zadig eszközzel), majd "
                "húzd ki és dugd vissza a sticket."
            )

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
        """Watchdog thread: stops the ANT+ node when it runs without data for long.

        openant's Node.start() is a blocking call and does NOT return on a
        USB disconnect (its internal _main loop reads an empty queue
        forever). This watchdog detects the situation and calls
        node.stop() from the outside, letting the retry logic of
        _thread_loop run.
        """
        # _running.is_set() == True in normal operation, so it CANNOT be
        # used for wait(timeout) (it would return immediately). We use
        # _stop_event instead, which is only set at shutdown.
        stop_event = self._stop_event
        while not stop_event.wait(timeout=5):
            node = self._node
            if node is None:
                continue

            now = time.monotonic()
            started = self._node_started
            last = self._lastdata

            # The node runs and data has arrived before, but nothing for
            # WATCHDOG_TIMEOUT since → most likely a USB disconnect
            if last > 0 and (now - last) > self.WATCHDOG_TIMEOUT:
                user_logger.warning(
                    f"⚠ ANT+ {self.WATCHDOG_TIMEOUT}s óta nincs adat – "
                    f"kapcsolat megszakadt, újracsatlakozás..."
                )
                self._lastdata = 0.0  # Prevents repeated triggering
                try:
                    node.stop()
                except Exception as exc:
                    logger.debug(f"ANT+ watchdog node.stop() hiba: {exc}")
            # The node started but no data for WATCHDOG_TIMEOUT * 2 (e.g.
            # wrong device_id, or the device was never in range)
            elif last == 0.0 and started > 0 and (now - started) > self.WATCHDOG_TIMEOUT * 2:
                user_logger.warning(
                    f"⚠ ANT+ {self.WATCHDOG_TIMEOUT * 2}s óta nem található eszköz, "
                    f"újrapróbálkozás..."
                )
                self._node_started = 0.0  # Prevents repeated triggering
                try:
                    node.stop()
                except Exception as exc:
                    logger.debug(f"ANT+ watchdog node.stop() hiba: {exc}")

    def _thread_loop(self) -> None:
        """Main loop of the ANT+ thread – with reconnect logic.

        Also starts a watchdog thread that checks whether data arrives.
        When the USB ANT+ stick disconnects, openant's Node.start() does
        not return on its own – the watchdog calls node.stop() from the
        outside, which releases the block.
        """
        # Start the watchdog thread
        watchdog = threading.Thread(
            target=self._watchdog, daemon=True, name="ANTPlus-Watchdog"
        )
        watchdog.start()

        # Initial grace delay: give the USB ANT+ stick's libusb0 driver a
        # chance to become available before the first claim/open attempt.
        # This eliminates the typical startup "resource busy" / "device not
        # recognize command" transient errors. (Interruptible on shutdown.)
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
                self._node.start()  # Blocking call – waits here while the node runs

                if not self._running.is_set():
                    break

                # Reset the counter when data has arrived successfully
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
                # stop_event.wait: waits just as long but wakes immediately
                # on shutdown (time.sleep could hang the shutdown for 30 s)
                self._stop_event.wait(self.MAX_RETRY_COOLDOWN)
                if not self._running.is_set():
                    break
                retry_count = 0
                user_logger.info("ANT+ keresés újraindítása...")

            self._stop_node()
            self._node_started = 0.0
            self._stop_event.wait(self._reconnect_delay)

        self._stop_node()
        user_logger.info("ANT+ leállítva")

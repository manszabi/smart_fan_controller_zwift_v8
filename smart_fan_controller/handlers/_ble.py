"""BLE handler modules – fan output and sensor inputs.

This module contains every BLE handler class:
- BLEFanOutputController: BLE fan zone sending
- _BLESensorInputHandler: abstract base for sensor handlers
- BLEPowerInputHandler: BLE power metering
- BLEHRInputHandler: BLE heart rate
- BLECombinedSensor: power + HR aggregator
- Helper functions: scan, log, send_zone

Asyncio coroutine based implementation using the bleak library.
"""
from __future__ import annotations

import abc
import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any

logger = logging.getLogger("zwift_fan_controller_new")
user_logger = logging.getLogger("user")

# Check for the bleak library
try:
    from bleak import BleakClient, BleakScanner
    _BLEAK_AVAILABLE = True
except ImportError:
    _BLEAK_AVAILABLE = False

from smart_fan_controller.config.schemas import BleConfig, DatasourceConfig
from smart_fan_controller.core.helpers import resolve_log_dir


# ============================================================
# BLE DEVICE DISCOVERY AND LOGGING (shared helpers)
# ============================================================


def _ble_log_path(log_dir: str) -> str:
    """Return the full path of ble_devices.log in the configured log dir."""
    return os.path.join(log_dir, "ble_devices.log")


def _log_ble_devices_to_file(
    devices_info: list[tuple[str | None, str, list[str]]],
    scan_context: str,
    log_dir: str,
    logging_enabled: bool,
) -> None:
    """Append discovered BLE devices to the ble_devices.log file.

    Only devices whose address is not in the file yet are written.
    Creates the file when it does not exist. Every entry is timestamped.

    Args:
        devices_info: List of (name, address, service_uuids) tuples.
        scan_context: Context of the scan (e.g. "BLE Fan", "BLE Power").
        log_dir: Path of the log directory.
        logging_enabled: True when logging is enabled.
    """
    # Logging disabled → no device log file is written either
    if not logging_enabled:
        return
    if not devices_info:
        return

    # Read the existing addresses from the file
    existing_addresses: set[str] = set()
    ble_log = _ble_log_path(log_dir)
    try:
        with open(ble_log, "r", encoding="utf-8") as f:
            for line in f:
                # Line format: "  name | ADDRESS | UUIDs: ..."
                parts = line.split("|")
                if len(parts) >= 2:
                    existing_addresses.add(parts[1].strip())
    except FileNotFoundError:
        pass  # The file does not exist yet, every device is new
    except OSError as exc:
        logger.warning(f"Nem sikerült olvasni a {ble_log} fájlt: {exc}")

    # Filter for the new devices only
    new_devices = [
        (name, addr, uuids)
        for name, addr, uuids in devices_info
        if addr not in existing_addresses
    ]

    if not new_devices:
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(ble_log, "a", encoding="utf-8") as f:
            f.write(f"\n--- BLE Scan ({scan_context}) @ {timestamp} ---\n")
            for name, addr, uuids in new_devices:
                uuid_str = ", ".join(uuids[:5]) if uuids else "–"
                f.write(f"  {name or '(névtelen)':30s} | {addr} | UUIDs: {uuid_str}\n")
    except OSError as exc:
        logger.warning(f"Nem sikerült írni a {ble_log} fájlba: {exc}")


def _print_ble_devices(
    devices_info: list[tuple[str | None, str, list[str]]],
    scan_context: str,
    matched_addr: str | None = None,
) -> None:
    """Print the discovered BLE devices to the console.

    Args:
        devices_info: List of (name, address, service_uuids) tuples.
        scan_context: Context of the scan.
        matched_addr: Address of the auto-selected device (for the ◄ marker).
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
    target_service_uuid: str | None,
    scan_context: str,
    log_dir: str,
    logging_enabled: bool,
) -> tuple[Any, list[tuple[str | None, str, list[str]]]]:
    """Scan for BLE devices, log them, optionally match a given service UUID.

    When target_service_uuid is given, the first device advertising that
    UUID is selected.

    Args:
        scan_timeout: Scan timeout in seconds.
        target_service_uuid: Service UUID to look for (or None).
        scan_context: Context of the scan (for logging).
        log_dir: Path of the log directory.
        logging_enabled: True when logging is enabled.

    Returns:
        (matched_device, devices_info) – matched_device is the first match
        (BLEDevice) or None, devices_info is the full list.
    """
    if not _BLEAK_AVAILABLE:
        return None, []

    devices_info: list[tuple[str | None, str, list[str]]] = []
    matched: Any = None
    target_lower = target_service_uuid.lower() if target_service_uuid else None

    try:
        # return_adv=True: dict[str, tuple[BLEDevice, AdvertisementData]]
        # (guaranteed by bleak >= 0.21; the old list-based API is unsupported)
        discovered = await BleakScanner.discover(
            timeout=scan_timeout, return_adv=True
        )
        for device, adv_data in discovered.values():
            uuids = list(adv_data.service_uuids or [])
            devices_info.append((device.name, device.address, uuids))
            if target_lower and matched is None:
                if any(u.lower() == target_lower for u in uuids):
                    matched = device

    except Exception as exc:
        logger.error(f"BLE scan hiba ({scan_context}): {exc}")
        return None, []

    matched_addr: str | None = matched.address if matched else None
    _print_ble_devices(devices_info, scan_context, matched_addr)
    _log_ble_devices_to_file(devices_info, scan_context, log_dir, logging_enabled)

    return matched, devices_info


def _report_gatt_characteristics(
    client: Any,
    label: str,
    log_dir: str,
    logging_enabled: bool,
) -> None:
    """Print the connected device's GATT characteristic UUIDs to console and file.

    Unlike service UUIDs, characteristic UUIDs are NOT part of the BLE
    advertisement packet – they are only readable after connecting + GATT
    discovery. Call this function after a successful connection
    (``client.services`` is populated by then).

    Args:
        client: Connected BleakClient (with ``client.services``).
        label: Device/context label for the output (e.g. "BLE Fan").
        log_dir: Path of the log directory.
        logging_enabled: True when logging is enabled.
    """
    if not _BLEAK_AVAILABLE:
        return

    # Collect (service_uuid, char_uuid, [properties]) triples
    entries: list[tuple[str, str, list[str]]] = []
    try:
        services = getattr(client, "services", None)
        if services is None:
            return
        for service in services:
            svc_uuid = getattr(service, "uuid", "?")
            for char in getattr(service, "characteristics", []):
                char_uuid = getattr(char, "uuid", "?")
                props = list(getattr(char, "properties", []) or [])
                entries.append((svc_uuid, char_uuid, props))
    except Exception as exc:
        logger.warning(f"{label} GATT characteristic listázási hiba: {exc}")
        return

    if not entries:
        return

    # Console output
    user_logger.info(f"🔎 {label} characteristic UUID-k ({len(entries)} db):")
    for svc_uuid, char_uuid, props in entries:
        prop_str = ", ".join(props) if props else "–"
        user_logger.info(f"    {char_uuid}  [{prop_str}]  (service: {svc_uuid})")

    # File output (only when logging is enabled)
    if not logging_enabled:
        return
    ble_log = _ble_log_path(log_dir)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(ble_log, "a", encoding="utf-8") as f:
            f.write(f"\n--- GATT Characteristics ({label}) @ {timestamp} ---\n")
            for svc_uuid, char_uuid, props in entries:
                prop_str = ", ".join(props) if props else "–"
                f.write(f"  {char_uuid} | [{prop_str}] | service: {svc_uuid}\n")
    except OSError as exc:
        logger.warning(f"Nem sikerült írni a {ble_log} fájlba: {exc}")


async def send_zone(zone: int, zone_queue: asyncio.Queue[int]) -> None:
    """Send a zone command into the BLE fan output queue.

    When the queue is full (maxsize=1) the old command is discarded and
    the new one inserted, so the freshest zone is always the one sent.
    After get_nowait() the queue is guaranteed empty, so put_nowait()
    cannot raise QueueFull.

    Args:
        zone: Fan zone level (0–3).
        zone_queue: asyncio.Queue of the BLE fan output.
    """
    try:
        zone_queue.get_nowait()
    except asyncio.QueueEmpty:
        pass
    # After get_nowait() there is guaranteed free room
    zone_queue.put_nowait(zone)


# ============================================================
# BLE FAN OUTPUT CONTROLLER
# ============================================================


class BLEFanOutputController:
    """BLE based fan output controller (sends LEVEL:N commands).

    Asyncio coroutine based implementation. Commands are received via an
    asyncio.Queue and written to the BLE GATT characteristic of the ESP32
    controller. PIN authentication is supported.

    Reconnect: a timed background task (``_reconnect_loop``) attempts to
    reconnect every ``reconnect_interval`` while the connection is down –
    independent of the zone commands. Zone sending (``_send_zone``) never
    blocks on a reconnect: while the connection is down or a reconnect is
    in flight, it only records the requested zone, which the background
    loop sends out after a successful reconnect. ``_conn_lock``
    serializes the BLE operations, so concurrent zone sends and
    reconnects do not collide.

    Attributes:
        device_name: Name of the BLE device to look for.
        is_connected: True while the BLE connection is up.
        last_sent: The last successfully sent zone level.
    """

    RETRY_RESET_SECONDS = 30
    DISCONNECT_TIMEOUT = 5.0

    def __init__(self, settings: dict[str, Any]) -> None:
        ble: BleConfig = settings["ble_fan"]
        self.device_name: str | None = ble.device_name
        self.scan_timeout: int = ble.scan_timeout
        self.connection_timeout: int = ble.connection_timeout
        self.reconnect_interval: int = ble.reconnect_interval
        self.max_retries: int = ble.max_retries
        self.command_timeout: int = ble.command_timeout
        self.service_uuid: str = ble.service_uuid
        self.characteristic_uuid: str = ble.characteristic_uuid
        self.pin_code: str | None = ble.pin_code

        self.is_connected: bool = False
        self.last_sent: int | None = None
        self._client: Any | None = None
        self._device_address: str | None = None
        self._retry_count: int = 0
        self._retry_reset_time: float | None = None
        self._auth_failed: bool = False
        self.last_sent_time: float = 0.0
        # Most recently requested zone – the timed background reconnect
        # sends it after a successful reconnect (even without a new command).
        self._desired_zone: int | None = None
        # Lock serializing the BLE client operations (connect / write) so
        # the background reconnect and zone sending never run on the same
        # client concurrently. (Python ≥3.10: constructible without a loop.)
        self._conn_lock: asyncio.Lock = asyncio.Lock()
        # The timed background reconnect task (started/stopped by run()).
        self._reconnect_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        gs = settings["global_settings"]
        self._log_dir: str = resolve_log_dir(gs.log_directory)
        self._logging_enabled: bool = gs.logging

    @property
    def auth_failed(self) -> bool:
        """True when authentication failed (wrong PIN)."""
        return self._auth_failed

    def __repr__(self) -> str:
        return (
            f"BLEFanOutputController(device={self.device_name!r}, "
            f"connected={self.is_connected}, last_sent={self.last_sent}, "
            f"retries={self._retry_count}/{self.max_retries})"
        )

    async def run(self, zone_queue: asyncio.Queue[int]) -> None:
        """Main coroutine of the BLE fan output – reads zone_queue and sends commands.

        Attempts to connect to the BLE device at startup, then keeps
        reading zone_queue and sending the zone commands.

        Args:
            zone_queue: asyncio.Queue the zone commands are read from.
        """
        self._loop = asyncio.get_running_loop()
        if not _BLEAK_AVAILABLE:
            user_logger.warning("⚠ BLE Fan: bleak könyvtár nem elérhető – BLE kimenet letiltva!")
            return

        user_logger.info("BLE Fan kimenet elindítva")
        await self._initial_connect()

        # Timed background reconnect in its own task: attempts to reconnect
        # while the connection is down, independent of the zone commands.
        # A separate task, so it never blocks the zone_queue reads.
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())
        try:
            while True:
                zone = await zone_queue.get()
                await self._send_zone(zone)
        finally:
            task = self._reconnect_task
            self._reconnect_task = None
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _initial_connect(self) -> None:
        """Initial BLE connection at startup (continues on failure)."""
        ok = await self._scan_and_connect()
        if not ok:
            user_logger.warning(
                "⚠ BLE Fan: kezdeti csatlakozás sikertelen, "
                "automatikus háttér-újracsatlakozás folyamatban"
            )

    async def _scan_and_connect(self) -> bool:
        """Scan for the BLE device and connect.

        When device_name is empty or None, auto-discovery starts: a
        matching device is looked up by service_uuid, and every device
        found is written to the console and ble_devices.log.

        Returns:
            True when the connection succeeded.
        """
        if not _BLEAK_AVAILABLE:
            return False

        # --- Auto-discovery (no device_name configured) ---
        if not self.device_name:
            try:
                matched, _ = await _scan_ble_with_autodiscovery(
                    self.scan_timeout, self.service_uuid, "BLE Fan (auto)",
                    self._log_dir, self._logging_enabled
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

        # --- Name based lookup (device_name configured) ---
        # find_device_by_name: returns as soon as the device shows up
        # (discover() would always wait out the full scan_timeout)
        try:
            device = await BleakScanner.find_device_by_name(
                self.device_name, timeout=self.scan_timeout
            )
            if device is not None:
                self._device_address = device.address
                user_logger.info(
                    f"✓ BLE Fan eszköz megtalálva: {device.name} ({device.address})"
                )
                return await self._connect()

            user_logger.warning(f"⚠ BLE Fan eszköz nem található: '{self.device_name}'")
            return False

        except Exception as exc:
            user_logger.warning(f"⚠ BLE Fan keresési hiba: {exc}")
            return False

    async def _connect(self) -> bool:
        """Connect to the previously discovered BLE device.

        Returns:
            True when the connection succeeded.
        """
        if not _BLEAK_AVAILABLE:
            return False
        if not self._device_address:
            return False

        try:
            client = self._client
            if client and client.is_connected:
                # The client is already connected (e.g. from an earlier,
                # interrupted attempt) – sync the state flag too, otherwise
                # zone sending would skip forever.
                self.is_connected = True
                return True

            client = BleakClient(
                self._device_address,
                timeout=self.connection_timeout,
                disconnected_callback=self._on_disconnect,
            )
            self._client = client

            await client.connect()

            # Print the GATT characteristic UUIDs after connecting (the
            # only time they are readable)
            _report_gatt_characteristics(
                client, "BLE Fan", self._log_dir, self._logging_enabled
            )

            if self.pin_code is not None:
                ok = await self._authenticate()
                if not ok:
                    # The established link must not stay open: disconnect and
                    # release the client, otherwise the connection gets
                    # "stuck" (physically alive but seen as OFFLINE).
                    try:
                        await asyncio.wait_for(
                            client.disconnect(), timeout=self.DISCONNECT_TIMEOUT
                        )
                    except Exception as exc:
                        logger.debug(f"BLE disconnect hiba AUTH-bukás után: {exc}")
                    self._client = None
                    self.is_connected = False
                    return False

            self.is_connected = True
            self._retry_count = 0
            self._retry_reset_time = None
            self.last_sent = None
            user_logger.info(f"✓ BLE Fan csatlakozva: {self._device_address}")
            try:
                await self._write_raw("ROLLER:1")
                user_logger.info("✓ ROLLER:1 elküldve")
            except Exception as exc:
                logger.warning(f"ROLLER:1 küldési hiba: {exc}")
            return True

        except Exception as exc:
            user_logger.warning(f"⚠ BLE Fan csatlakozási hiba: {exc}")
            self.is_connected = False
            self._client = None
            return False

    async def _authenticate(self) -> bool:
        """Application-level BLE PIN authentication.

        Returns:
            True when authentication succeeded (also continues on timeout).
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
                except TimeoutError:
                    logger.error("BLE AUTH write timeout")
                    return False

                try:
                    await asyncio.wait_for(
                        auth_event.wait(),
                        timeout=self.command_timeout,
                    )
                except TimeoutError:
                    logger.warning(
                        "BLE AUTH válasz timeout - folytatás autentikáció nélkül"
                    )
                    return True

                resp: str = auth_result[0]
                if not resp:
                    logger.error("BLE AUTH: üres válasz")
                    return False
                if resp == "AUTH_OK":
                    user_logger.info("✓ BLE Fan PIN autentikáció sikeres")
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
        """Callback for an unexpected BLE disconnect.

        bleak does not guarantee calling this on the asyncio event loop
        thread, so the state change is delegated via
        loop.call_soon_threadsafe().
        """
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._handle_disconnect)
        else:
            self._handle_disconnect()

    def _handle_disconnect(self) -> None:
        """Disconnect state change – to be called on the asyncio event loop.

        Only a real (connected → dropped) transition alerts: intentional
        disconnects (shutdown, PIN failure, post-write-timeout cleanup)
        clear the is_connected flag first, so they stay silent."""
        if self.is_connected:
            user_logger.warning("⚠ BLE Fan kapcsolat megszakadt")
        self.is_connected = False
        self.last_sent = None

    async def _send_zone(self, zone: int) -> None:
        """Send a zone command over BLE – never blocks on a reconnect.

        Reconnecting is done by the timed background loop
        (``_reconnect_loop``), not this method. We only write when the
        connection is up and no BLE operation (reconnect) is in flight.
        While the connection is down the requested zone is recorded
        (``_desired_zone``); the background loop sends the latest
        requested zone automatically after a successful reconnect.

        Args:
            zone: Fan zone level (0–3).
        """
        self._desired_zone = zone

        if self._auth_failed:
            logger.error(
                "BLE Fan: AUTH hiba, parancs elutasítva! Javítsd a pin_code-ot."
            )
            return

        if self.last_sent == zone and self.is_connected:
            return

        # No connection → do not block; the background loop reconnects and
        # sends _desired_zone. When a reconnect is in flight (lock held),
        # skip as well – the fresh zone goes out at the end of the reconnect.
        if not self.is_connected or self._conn_lock.locked():
            return

        # The lock is free here → acquire completes immediately without an
        # await-yield (asyncio: acquiring an uncontended lock does not
        # yield control), so there is no race between the check above and
        # the acquire.
        async with self._conn_lock:
            if not self.is_connected:
                return
            await self._write_level(zone)

    async def _reconnect_once(self) -> bool:
        """A single reconnect attempt, without sleeping.

        The sleep-free implementation ensures that reading zone_queue is
        never blocked by a long reconnect wait.

        Returns:
            True when the reconnect succeeded.
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
                user_logger.warning(
                    f"⚠ BLE Fan: {self.max_retries} sikertelen próbálkozás, "
                    f"{self.RETRY_RESET_SECONDS}s várakozás..."
                )
            return False

        self._retry_count += 1
        user_logger.info(
            f"BLE Fan újracsatlakozás... ({self._retry_count}/{self.max_retries})"
        )

        if self._device_address:
            return await self._connect()
        return await self._scan_and_connect()

    async def _reconnect_loop(self) -> None:
        """Timed background reconnect – independent of the zone commands.

        Wakes every ``reconnect_interval`` and attempts a reconnect while
        the connection is down (and there is no AUTH failure).
        ``_conn_lock`` serializes the BLE operations, so a concurrent zone
        send (``_send_zone``) does not collide with the reconnect. After a
        successful reconnect the most recently requested zone
        (``_desired_zone``) is sent, putting the fan into the desired
        state even when no new command arrived meanwhile.

        ``_reconnect_once`` manages the ``max_retries`` counter and the
        subsequent ``RETRY_RESET_SECONDS`` wait; this loop only provides
        the timing. Runs in its own task, never blocking the zone_queue
        reads.
        """
        while True:
            await asyncio.sleep(self.reconnect_interval)

            if self.is_connected or self._auth_failed:
                continue

            async with self._conn_lock:
                # Re-check after acquiring the lock (a connection may have
                # been established meanwhile as a zone-send side effect).
                if self.is_connected or self._auth_failed:
                    continue
                ok = await self._reconnect_once()
                if ok and self._desired_zone is not None:
                    await self._write_level(self._desired_zone)

    async def _write_level(self, zone: int) -> None:
        """Write a LEVEL:N command to the BLE GATT characteristic.

        Args:
            zone: Fan zone level (0–3).
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

        except TimeoutError:
            user_logger.warning(f"⚠ BLE Fan parancs küldés timeout ({self.command_timeout}s)")
            self.is_connected = False
            try:
                await client.disconnect()
            except Exception as exc:
                logger.debug(f"BLE disconnect hiba timeout után: {exc}")
            self._client = None

        except Exception as exc:
            user_logger.warning(f"⚠ BLE Fan küldési hiba: {exc}")
            self.is_connected = False
            try:
                await client.disconnect()
            except Exception as exc2:
                logger.debug(f"BLE disconnect hiba küldési hiba után: {exc2}")
            self._client = None

    async def _write_raw(self, command: str) -> None:
        """Send an arbitrary command to the BLE GATT characteristic."""
        client = self._client
        if client is None or not client.is_connected:
            return
        try:
            await asyncio.wait_for(
                client.write_gatt_char(
                    self.characteristic_uuid,
                    command.encode("utf-8"),
                ),
                timeout=self.command_timeout,
            )
            logger.info(f"BLE Fan raw parancs elküldve: {command}")
        except Exception as exc:
            logger.warning(f"BLE Fan raw parancs küldési hiba ({command}): {exc}")

    async def disconnect(self) -> None:
        """Disconnect the BLE link and release the client."""
        client = self._client
        if client is not None:
            # Clear the flag BEFORE disconnecting: the disconnected_callback
            # then sees an intentional disconnect and does not alert.
            self.is_connected = False
            try:
                await asyncio.wait_for(
                    client.disconnect(),
                    timeout=self.DISCONNECT_TIMEOUT,
                )
            except Exception as exc:
                logger.debug(f"BLE disconnect hiba: {exc}")
            finally:
                self._client = None


# ============================================================
# SHARED BLE SENSOR BASE CLASS (DRY)
# ============================================================


class _BLESensorInputHandler(abc.ABC):
    """Shared base class for the BLE sensor handlers (Power, HR).

    Asyncio coroutine based implementation. The scan, connect,
    notification subscribe and retry/reconnect logic lives here; the
    subclasses only define the sensor-specific constants and the data
    parsing.

    Subclasses must override:
        SERVICE_UUID: The BLE service UUID string.
        MEASUREMENT_UUID: The BLE measurement characteristic UUID string.
        _sensor_label: Short name for the logs (e.g. "BLE Power").
        _settings_prefix: Settings key prefix (e.g. "ble_power").
        _parse_notification(data): raw bytes → number conversion.

    Attributes:
        device_name: Name of the BLE device (None = auto-discovery).
        is_connected: True while the BLE connection is up.
        lastdata: Timestamp of the last successful data (time.monotonic).
    """

    SERVICE_UUID: str
    MEASUREMENT_UUID: str
    _sensor_label: str
    _settings_prefix: str
    RETRY_RESET_SECONDS = 30

    def __init__(
        self, settings: dict[str, Any], queue: asyncio.Queue[float]
    ) -> None:
        ds: DatasourceConfig = settings["datasource"]
        pfx = self._settings_prefix
        self.device_name: str | None = getattr(ds, f"{pfx}_device_name")
        self.scan_timeout: int = getattr(ds, f"{pfx}_scan_timeout", 10)
        self.reconnect_interval: int = getattr(ds, f"{pfx}_reconnect_interval", 5)
        self.max_retries: int = getattr(ds, f"{pfx}_max_retries", 10)
        self._queue = queue
        self.is_connected = False
        self._retry_count = 0
        self.lastdata = 0.0
        gs = settings["global_settings"]
        self._log_dir: str = resolve_log_dir(gs.log_directory)
        self._logging_enabled: bool = gs.logging

    @abc.abstractmethod
    def _parse_notification(self, data: bytes) -> float | None:
        """Extract the measured value from the raw BLE notification bytes.

        Returns:
            The extracted value (float), or None when the data is
            invalid / too short.
        """
        ...

    async def run(self) -> None:
        """Main coroutine of the BLE sensor receiver – with reconnect logic.

        Without a device_name it looks for a device advertising the
        SERVICE_UUID automatically and keeps trying until one is found.
        """
        label = self._sensor_label
        if not _BLEAK_AVAILABLE:
            user_logger.warning(f"⚠ {label}: bleak könyvtár nem elérhető!")
            return

        if self.device_name:
            user_logger.info(f"{label} keresés indítva: {self.device_name}")
        else:
            user_logger.info(f"{label}: nincs eszköznév megadva, automatikus felderítés...")

        while True:
            try:
                await self._scan_and_subscribe()
                self._retry_count = 0
                self.is_connected = False
                user_logger.warning(
                    f"⚠ {label} kapcsolat megszakadt, újracsatlakozás "
                    f"{self.reconnect_interval}s múlva..."
                )
                await asyncio.sleep(self.reconnect_interval)
            except asyncio.CancelledError:
                # Cancellation must be re-raised (modern asyncio pattern);
                # the shutdown path (gather) handles it.
                self.is_connected = False
                raise
            except Exception as exc:
                self._retry_count += 1
                self.is_connected = False
                user_logger.warning(
                    f"⚠ {label} hiba "
                    f"({self._retry_count}/{self.max_retries}): {exc}"
                )
                if self._retry_count >= self.max_retries:
                    user_logger.warning(
                        f"⚠ {label}: {self.max_retries} sikertelen próbálkozás, "
                        f"{self.RETRY_RESET_SECONDS}s várakozás..."
                    )
                    await asyncio.sleep(self.RETRY_RESET_SECONDS)
                    self._retry_count = 0
                    user_logger.info(f"{label} keresés újraindítása...")
                else:
                    await asyncio.sleep(self.reconnect_interval)

    async def _scan_and_subscribe(self) -> None:
        """Scan for the BLE device, connect, subscribe to notifications.

        With a device_name: lookup by name.
        Without one: auto-discovery based on the SERVICE_UUID.
        """
        if not _BLEAK_AVAILABLE:
            return

        label = self._sensor_label
        addr = None

        if self.device_name:
            # --- Name based lookup ---
            # find_device_by_name: returns as soon as the device shows up
            # (discover() would wait out the full scan_timeout)
            logger.debug(f"{label} keresés: {self.device_name}...")
            device = await BleakScanner.find_device_by_name(
                self.device_name, timeout=self.scan_timeout
            )
            if device is None:
                raise RuntimeError(f"{label} eszköz nem található: '{self.device_name}'")
            addr = device.address
            user_logger.info(
                f"✓ {label} eszköz megtalálva: {device.name} ({device.address})"
            )
        else:
            # --- Auto-discovery based on the service UUID ---
            matched, _ = await _scan_ble_with_autodiscovery(
                self.scan_timeout,
                self.SERVICE_UUID,
                f"{label} (auto)",
                self._log_dir,
                self._logging_enabled,
            )
            if matched is None:
                raise RuntimeError(
                    f"{label}: nem található szolgáltatás eszköz – "
                    "újrapróbálkozás..."
                )
            addr = matched.address
            user_logger.info(
                f"✓ {label} auto-csatlakozás: "
                f"{matched.name or '(névtelen)'} ({matched.address})"
            )

        # Event-driven disconnect watch (official bleak pattern): the
        # disconnected_callback is scheduled on the event loop, so setting
        # the asyncio.Event directly is safe. No 1 Hz polling wakeups.
        disconnected = asyncio.Event()

        async with BleakClient(
            addr, disconnected_callback=lambda _c: disconnected.set()
        ) as client:
            self.is_connected = True
            self._retry_count = 0
            user_logger.info(f"✓ {label} csatlakozva: {addr}")

            # Print the GATT characteristic UUIDs after connecting (the
            # only time they are readable)
            _report_gatt_characteristics(
                client, label, self._log_dir, self._logging_enabled
            )

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
            # Safety net: re-check is_connected every 10 s in case a backend
            # ever fails to fire the disconnect callback.
            while client.is_connected:
                try:
                    await asyncio.wait_for(disconnected.wait(), timeout=10)
                    break
                except TimeoutError:
                    continue

        self.is_connected = False


# ============================================================
# BLE POWER INPUT HANDLING
# ============================================================


class BLEPowerInputHandler(_BLESensorInputHandler):
    """BLE Cycling Power Service (UUID: 0x1818) receiver.

    Parse: flags (2 bytes LE) → instantaneous power (2 bytes LE, signed int16).
    """

    SERVICE_UUID = "00001818-0000-1000-8000-00805f9b34fb"
    MEASUREMENT_UUID = "00002a63-0000-1000-8000-00805f9b34fb"
    _sensor_label = "BLE Power"
    _settings_prefix = "ble_power"

    @property
    def power_lastdata(self) -> float:
        """Backwards-compatible alias for the lastdata attribute."""
        return self.lastdata

    @power_lastdata.setter
    def power_lastdata(self, value: float) -> None:
        self.lastdata = value

    def _parse_notification(self, data: bytes) -> float | None:
        if len(data) < 4:
            return None
        return float(int.from_bytes(data[2:4], byteorder="little", signed=True))


# ============================================================
# BLE HR INPUT HANDLING
# ============================================================


class BLEHRInputHandler(_BLESensorInputHandler):
    """BLE Heart Rate Service (UUID: 0x180D) receiver.

    Parse: flags byte bit 0 → 0 = 8-bit HR, 1 = 16-bit HR.
    """

    SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
    MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
    _sensor_label = "BLE HR"
    _settings_prefix = "ble_hr"

    @property
    def hr_lastdata(self) -> float:
        """Backwards-compatible alias for the lastdata attribute."""
        return self.lastdata

    @hr_lastdata.setter
    def hr_lastdata(self, value: float) -> None:
        self.lastdata = value

    def _parse_notification(self, data: bytes) -> float | None:
        if len(data) < 2:
            return None
        flags = data[0]
        # bit 0: 0 = 8-bit HR, 1 = 16-bit HR
        if flags & 0x01:
            if len(data) < 3:
                return None
            return float(int.from_bytes(data[1:3], byteorder="little"))
        return float(data[1])


# ============================================================
# BLE COMBINED SENSOR
# ============================================================


class BLECombinedSensor:
    """Power and HR sensor aggregator."""

    def __init__(
        self,
        power_handler: Any | None = None,
        hr_handler: Any | None = None,
    ) -> None:
        self.power_handler = power_handler
        self.hr_handler = hr_handler

    @property
    def power_lastdata(self) -> float:
        """Backwards-compatible property."""
        if self.power_handler:
            return getattr(self.power_handler, "power_lastdata", 0)
        return 0

    @property
    def hr_lastdata(self) -> float:
        """Backwards-compatible property."""
        if self.hr_handler:
            return getattr(self.hr_handler, "hr_lastdata", 0)
        return 0

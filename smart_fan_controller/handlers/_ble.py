"""BLE kezelő modulok – ventilátor kimenet és szenzor bemenetek.

Ez a modul az összes BLE-kezelő osztályt tartalmazza:
- BLEFanOutputController: BLE ventilátor zóna küldés
- _BLESensorInputHandler: Abstract base szenzor kezelőkhöz
- BLEPowerInputHandler: BLE power métering
- BLEHRInputHandler: BLE szívfrekvencia
- BLECombinedSensor: Power + HR aggregátor
- Segédfüggvények: scan, log, send_zone

Asyncio korrutin alapú implementáció, bleak könyvtárral.
"""
from __future__ import annotations

import abc
import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, cast

logger = logging.getLogger("swift_fan_controller_new")
user_logger = logging.getLogger("user")

# Bleak könyvtár ellenőrzése
try:
    from bleak import BleakClient, BleakScanner
    _BLEAK_AVAILABLE = True
except ImportError:
    _BLEAK_AVAILABLE = False

from smart_fan_controller.config.schemas import BleConfig, DatasourceConfig
from smart_fan_controller.core.helpers import resolve_log_dir


# ============================================================
# BLE ESZKÖZ KERESÉS ÉS LOGOLÁS (közös segédfüggvények)
# ============================================================


def _ble_log_path(log_dir: str) -> str:
    """Visszaadja a ble_devices.log teljes útvonalát a konfigurált log könyvtárban."""
    return os.path.join(log_dir, "ble_devices.log")


def _log_ble_devices_to_file(
    devices_info: List[Tuple[Optional[str], str, List[str]]],
    scan_context: str,
    log_dir: str,
    logging_enabled: bool,
) -> None:
    """Talált BLE eszközöket ír a ble_devices.log fájlba (append módban).

    Csak olyan eszközöket ír a fájlba, amelyek address-e még nem szerepel benne.
    Ha a fájl nem létezik, létrehozza. Minden bejegyzés időbélyeggel ellátott.

    Args:
        devices_info: Lista (name, address, service_uuids) tuple-ökből.
        scan_context: A keresés kontextusa (pl. "BLE Fan", "BLE Power").
        log_dir: Log könyvtár elérési útja.
        logging_enabled: True, ha a logging engedélyezett.
    """
    # Loggolás kikapcsolva → nem írunk eszköz-log fájlt sem
    if not logging_enabled:
        return
    if not devices_info:
        return

    # Meglévő address-ek beolvasása a fájlból
    existing_addresses: set[str] = set()
    ble_log = _ble_log_path(log_dir)
    try:
        with open(ble_log, "r", encoding="utf-8") as f:
            for line in f:
                # Sorok formátuma: "  név | ADDRESS | UUIDs: ..."
                parts = line.split("|")
                if len(parts) >= 2:
                    existing_addresses.add(parts[1].strip())
    except FileNotFoundError:
        pass  # Még nem létezik a fájl, minden eszköz új
    except OSError as exc:
        logger.warning(f"Nem sikerült olvasni a {ble_log} fájlt: {exc}")

    # Csak az új eszközök szűrése
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
    devices_info: List[Tuple[Optional[str], str, List[str]]],
    scan_context: str,
    matched_addr: Optional[str] = None,
) -> None:
    """Talált BLE eszközöket ír a konzolra.

    Args:
        devices_info: Lista (name, address, service_uuids) tuple-ökből.
        scan_context: A keresés kontextusa.
        matched_addr: Az automatikusan kiválasztott eszköz címe (◄ jelöléshez).
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
    target_service_uuid: Optional[str],
    scan_context: str,
    log_dir: str,
    logging_enabled: bool,
) -> Tuple[Optional[Any], List[Tuple[Optional[str], str, List[str]]]]:
    """BLE eszközöket keres, logolja, és opcionálisan keres egy megadott service UUID-val.

    Ha target_service_uuid megadva, az első olyan eszközt választja ki,
    amelyik hirdeti ezt az UUID-t.

    Args:
        scan_timeout: Keresési timeout másodpercben.
        target_service_uuid: Keresett service UUID (vagy None).
        scan_context: A keresés kontextusa (logoláshoz).
        log_dir: Log könyvtár elérési útja.
        logging_enabled: True, ha a logging engedélyezett.

    Returns:
        (matched_device, devices_info) – matched_device az első egyezés (BLEDevice)
        vagy None, devices_info a teljes lista.
    """
    if not _BLEAK_AVAILABLE:
        return None, []

    devices_info: List[Tuple[Optional[str], str, List[str]]] = []
    matched: Optional[Any] = None

    try:
        # return_adv=True: dict[str, tuple[BLEDevice, AdvertisementData]]
        discovered: Any = await BleakScanner.discover(
            timeout=scan_timeout, return_adv=True
        )

        items: list[Any] = (
            list(cast(Any, discovered).values())
            if isinstance(discovered, dict)
            else list(discovered)
        )

        for item in items:
            device: Any = None
            uuids: List[str] = []
            t = cast(tuple[Any, ...], item)
            if isinstance(item, tuple) and len(t) == 2:
                device = t[0]
                adv_data: Any = t[1]
                uuids = (
                    list(adv_data.service_uuids)
                    if hasattr(adv_data, "service_uuids") and adv_data.service_uuids
                    else []
                )
            else:
                device = cast(Any, item)

            dev_name: Optional[str] = getattr(device, "name", None)
            dev_addr: str = getattr(device, "address", str(device))
            devices_info.append((dev_name, dev_addr, uuids))

            if target_service_uuid and matched is None:
                if any(u.lower() == target_service_uuid.lower() for u in uuids):
                    matched = device

    except TypeError:
        # Fallback régebbi Bleak verziókhoz (return_adv nem támogatott)
        devices: Any = await BleakScanner.discover(timeout=scan_timeout)
        devices_info = [
            (getattr(d, "name", None), getattr(d, "address", ""), [])
            for d in devices
        ]
        matched = None

    except Exception as exc:
        logger.error(f"BLE scan hiba ({scan_context}): {exc}")
        return None, []

    matched_addr: Optional[str] = getattr(matched, "address", None) if matched else None
    _print_ble_devices(devices_info, scan_context, matched_addr)
    _log_ble_devices_to_file(devices_info, scan_context, log_dir, logging_enabled)

    return matched, devices_info


async def send_zone(zone: int, zone_queue: asyncio.Queue[int]) -> None:
    """Zóna parancsot küld a BLE fan kimenet queue-ba.

    Ha a queue teli (maxsize=1), a régi parancsot elveti és az újat
    teszi be, hogy mindig a legfrissebb zóna kerüljön küldésre.
    A get_nowait() után a queue garantáltan üres, ezért put_nowait()
    nem dobhat QueueFull-t.

    Args:
        zone: Ventilátor zóna szintje (0–3).
        zone_queue: A BLE fan output asyncio.Queue-ja.
    """
    try:
        zone_queue.get_nowait()
    except asyncio.QueueEmpty:
        pass
    # get_nowait() után garantáltan szabad hely van
    zone_queue.put_nowait(zone)


# ============================================================
# BLE VENTILÁTOR KIMENET VEZÉRLŐ
# ============================================================


class BLEFanOutputController:
    """BLE alapú ventilátor kimenet vezérlő (LEVEL:N parancsok küldése).

    Asyncio korrutin alapú implementáció. A parancsokat egy
    asyncio.Queue-n keresztül fogadja, és a BLE GATT karakterisztikára
    írja ki az ESP32 vezérlőnek. PIN autentikáció is támogatott.

    Attribútumok:
        device_name: A keresett BLE eszköz neve.
        is_connected: True, ha a BLE kapcsolat aktív.
        last_sent: Az utoljára sikeresen elküldött zóna szint.
    """

    RETRY_RESET_SECONDS = 30
    DISCONNECT_TIMEOUT = 5.0

    def __init__(self, settings: Dict[str, Any]) -> None:
        ble: BleConfig = settings["ble_fan"]
        self.device_name: Optional[str] = ble.device_name
        self.scan_timeout: int = ble.scan_timeout
        self.connection_timeout: int = ble.connection_timeout
        self.reconnect_interval: int = ble.reconnect_interval
        self.max_retries: int = ble.max_retries
        self.command_timeout: int = ble.command_timeout
        self.service_uuid: str = ble.service_uuid
        self.characteristic_uuid: str = ble.characteristic_uuid
        self.pin_code: Optional[str] = ble.pin_code

        self.is_connected: bool = False
        self.last_sent: Optional[int] = None
        self._client: Optional[Any] = None
        self._device_address: Optional[str] = None
        self._retry_count: int = 0
        self._retry_reset_time: Optional[float] = None
        self._auth_failed: bool = False
        self.last_sent_time: float = 0.0
        # Utolsó reconnect kísérlet ideje – non-blocking reconnect logikához
        self._last_reconnect_attempt: float = 0.0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        gs = settings["global_settings"]
        self._log_dir: str = resolve_log_dir(gs.log_directory)
        self._logging_enabled: bool = gs.logging

    @property
    def auth_failed(self) -> bool:
        """True ha az authentikáció sikertelen volt (PIN hibás)."""
        return self._auth_failed

    def __repr__(self) -> str:
        return (
            f"BLEFanOutputController(device={self.device_name!r}, "
            f"connected={self.is_connected}, last_sent={self.last_sent}, "
            f"retries={self._retry_count}/{self.max_retries})"
        )

    async def run(self, zone_queue: asyncio.Queue[int]) -> None:
        """A BLE fan kimenet fő korrutinja – olvassa a zone_queue-t és küldi a parancsokat.

        Indításkor megpróbál csatlakozni a BLE eszközhöz, majd folyamatosan
        olvassa a zone_queue-t és elküldi a zóna parancsokat.

        Args:
            zone_queue: asyncio.Queue, amelyből a zóna parancsokat olvassa.
        """
        self._loop = asyncio.get_running_loop()
        if not _BLEAK_AVAILABLE:
            user_logger.warning("⚠ BLE Fan: bleak könyvtár nem elérhető – BLE kimenet letiltva!")
            return

        user_logger.info("BLE Fan kimenet elindítva")
        await self._initial_connect()

        while True:
            zone = await zone_queue.get()
            await self._send_zone(zone)

    async def _initial_connect(self) -> None:
        """Kezdeti BLE csatlakozás indításkor (hiba esetén folytatja)."""
        ok = await self._scan_and_connect()
        if not ok:
            user_logger.warning(
                "⚠ BLE Fan: kezdeti csatlakozás sikertelen, "
                "automatikus újrapróbálkozás parancs küldéskor"
            )

    async def _scan_and_connect(self) -> bool:
        """BLE eszköz keresése és csatlakozás.

        Ha device_name üres vagy None, automatikus felderítés indul:
        a service_uuid alapján keres megfelelő eszközt, az összes talált
        eszközt konzolra és ble_devices.log-ba írja.

        Returns:
            True, ha a csatlakozás sikeres.
        """
        if not _BLEAK_AVAILABLE:
            return False

        # --- Automatikus felderítés (nincs device_name beállítva) ---
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

        # --- Név alapú keresés (device_name beállítva) ---
        try:
            devices = await BleakScanner.discover(timeout=self.scan_timeout)
            for d in devices:
                if d.name == self.device_name:
                    self._device_address = d.address
                    user_logger.info(f"✓ BLE Fan eszköz megtalálva: {d.name} ({d.address})")
                    return await self._connect()
                if d.name is None:
                    logger.debug(f"BLE eszköz név nélkül: {d.address}")

            user_logger.warning(f"⚠ BLE Fan eszköz nem található: '{self.device_name}'")
            return False

        except Exception as exc:
            user_logger.warning(f"⚠ BLE Fan keresési hiba: {exc}")
            return False

    async def _connect(self) -> bool:
        """Csatlakozás a korábban megtalált BLE eszközhöz.

        Returns:
            True, ha a csatlakozás sikeres.
        """
        if not _BLEAK_AVAILABLE:
            return False
        if not self._device_address:
            return False

        try:
            client = self._client
            if client and client.is_connected:
                return True

            client = BleakClient(
                self._device_address,
                timeout=self.connection_timeout,
                disconnected_callback=self._on_disconnect,
            )
            self._client = client

            await client.connect()

            if self.pin_code is not None:
                ok = await self._authenticate()
                if not ok:
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
        """Alkalmazás szintű BLE PIN autentikáció.

        Returns:
            True, ha az autentikáció sikeres (vagy timeout esetén is folytatja).
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
                except asyncio.TimeoutError:
                    logger.error("BLE AUTH write timeout")
                    return False

                try:
                    await asyncio.wait_for(
                        auth_event.wait(),
                        timeout=self.command_timeout,
                    )
                except asyncio.TimeoutError:
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
        """Callback: BLE kapcsolat váratlan megszakadásakor.

        Bleak nem garantálja, hogy az asyncio event loop szálán hívja ezt,
        ezért loop.call_soon_threadsafe()-fel delegáljuk az állapotmódosítást.
        """
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._handle_disconnect)
        else:
            self._handle_disconnect()

    def _handle_disconnect(self) -> None:
        """Disconnect állapotmódosítás – az asyncio event loop-on hívandó."""
        user_logger.warning("⚠ BLE Fan kapcsolat megszakadt")
        self.is_connected = False
        self.last_sent = None

    async def _send_zone(self, zone: int) -> None:
        """Zóna parancs küldése BLE-n, szükség esetén újracsatlakozással.

        A reconnect non-blocking: ha az utolsó kísérlet óta még nem telt el
        reconnect_interval másodperc, a parancsot kihagyja (nem blokkolja
        a zone_queue olvasását).

        Args:
            zone: Ventilátor zóna szintje (0–3).
        """
        if self._auth_failed:
            logger.error(
                "BLE Fan: AUTH hiba, parancs elutasítva! Javítsd a pin_code-ot."
            )
            return

        if self.last_sent == zone and self.is_connected:
            return

        if not self.is_connected:
            now = time.monotonic()
            # Csak akkor próbálunk újra, ha elég idő telt el az utolsó kísérlet óta
            if now - self._last_reconnect_attempt < self.reconnect_interval:
                return
            self._last_reconnect_attempt = now
            ok = await self._reconnect_once()
            if not ok:
                return

        await self._write_level(zone)

    async def _reconnect_once(self) -> bool:
        """Egyetlen újracsatlakozási kísérlet, sleep nélkül.

        A sleep-mentes implementáció biztosítja, hogy a zone_queue olvasása
        ne blokkolódjon hosszú reconnect várakozás miatt.

        Returns:
            True, ha az újracsatlakozás sikeres.
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

    async def _write_level(self, zone: int) -> None:
        """LEVEL:N parancs írása a BLE GATT karakterisztikára.

        Args:
            zone: Ventilátor zóna szintje (0–3).
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

        except asyncio.TimeoutError:
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
        """Tetszőleges parancs küldése a BLE GATT karakterisztikára."""
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
        """Bontja a BLE kapcsolatot és felszabadítja a klienst."""
        client = self._client
        if client is not None:
            try:
                await asyncio.wait_for(
                    client.disconnect(),
                    timeout=self.DISCONNECT_TIMEOUT,
                )
            except Exception as exc:
                logger.debug(f"BLE disconnect hiba: {exc}")
            finally:
                self.is_connected = False
                self._client = None


# ============================================================
# BLE SZENZOR KÖZÖS ŐSOSZTÁLY (DRY)
# ============================================================


class _BLESensorInputHandler(abc.ABC):
    """Közös ősosztály BLE szenzor handlerekhez (Power, HR).

    Asyncio korrutin alapú implementáció. A scan, csatlakozás, notification
    subscribe és retry/reconnect logika itt van, az alosztályok csak a
    szenzor-specifikus konstansokat és az adat-parse-olást definiálják.

    Alosztályoknak felül kell írniuk:
        SERVICE_UUID: A BLE service UUID string.
        MEASUREMENT_UUID: A BLE measurement characteristic UUID string.
        _sensor_label: Rövid név logokhoz (pl. "BLE Power").
        _settings_prefix: Settings kulcs prefix (pl. "ble_power").
        _parse_notification(data): Nyers bájt → szám konverzió.

    Attribútumok:
        device_name: A keresett BLE eszköz neve (None = auto-discovery).
        is_connected: True, ha a BLE kapcsolat aktív.
        lastdata: Utolsó sikeres adat időbélyege (time.monotonic).
    """

    SERVICE_UUID: str
    MEASUREMENT_UUID: str
    _sensor_label: str
    _settings_prefix: str
    RETRY_RESET_SECONDS = 30

    def __init__(
        self, settings: Dict[str, Any], queue: asyncio.Queue[float]
    ) -> None:
        ds: DatasourceConfig = settings["datasource"]
        pfx = self._settings_prefix
        self.device_name: Optional[str] = getattr(ds, f"{pfx}_device_name")
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
    def _parse_notification(self, data: bytes) -> Optional[float]:
        """Nyers BLE notification bájtokból kinyeri a mért értéket.

        Returns:
            A kinyert érték (float), vagy None ha az adat érvénytelen/túl rövid.
        """
        ...

    async def run(self) -> None:
        """A BLE szenzor fogadó fő korrutinja – újracsatlakozási logikával.

        Ha nincs device_name, automatikusan keres a SERVICE_UUID alapján
        hirdető eszközt, és folyamatosan próbálkozik, amíg talál egyet.
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
                break
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
        """BLE eszköz keresése, csatlakozás, notification feliratkozás.

        Ha device_name megadva: név alapján keres.
        Ha device_name üres: auto-discovery a SERVICE_UUID alapján.
        """
        if not _BLEAK_AVAILABLE:
            return

        label = self._sensor_label
        addr = None

        if self.device_name:
            # --- Név alapú keresés ---
            logger.debug(f"{label} keresés: {self.device_name}...")
            devices = await BleakScanner.discover(timeout=self.scan_timeout)
            for d in devices:
                if d.name == self.device_name:
                    addr = d.address
                    user_logger.info(f"✓ {label} eszköz megtalálva: {d.name} ({d.address})")
                    break
                if d.name is None:
                    logger.debug(f"BLE eszköz név nélkül: {d.address}")
            if not addr:
                raise Exception(f"{label} eszköz nem található: '{self.device_name}'")
        else:
            # --- Automatikus felderítés service UUID alapján ---
            matched, _ = await _scan_ble_with_autodiscovery(
                self.scan_timeout,
                self.SERVICE_UUID,
                f"{label} (auto)",
                self._log_dir,
                self._logging_enabled,
            )
            if matched is None:
                raise Exception(
                    f"{label}: nem található szolgáltatás eszköz – "
                    "újrapróbálkozás..."
                )
            addr = matched.address
            user_logger.info(
                f"✓ {label} auto-csatlakozás: "
                f"{matched.name or '(névtelen)'} ({matched.address})"
            )

        async with BleakClient(addr) as client:
            self.is_connected = True
            self._retry_count = 0
            user_logger.info(f"✓ {label} csatlakozva: {addr}")

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
            while client.is_connected:
                await asyncio.sleep(1)

        self.is_connected = False


# ============================================================
# BLE POWER BEMENŐ ADATKEZELÉS
# ============================================================


class BLEPowerInputHandler(_BLESensorInputHandler):
    """BLE Cycling Power Service (UUID: 0x1818) fogadó.

    Parse: flags (2 bájt LE) → instantaneous power (2 bájt LE, signed int16).
    """

    SERVICE_UUID = "00001818-0000-1000-8000-00805f9b34fb"
    MEASUREMENT_UUID = "00002a63-0000-1000-8000-00805f9b34fb"
    _sensor_label = "BLE Power"
    _settings_prefix = "ble_power"

    @property
    def power_lastdata(self) -> float:
        """Visszafelé kompatibilis alias a lastdata attribútumhoz."""
        return self.lastdata

    @power_lastdata.setter
    def power_lastdata(self, value: float) -> None:
        self.lastdata = value

    def _parse_notification(self, data: bytes) -> Optional[float]:
        if len(data) < 4:
            return None
        return float(int.from_bytes(data[2:4], byteorder="little", signed=True))


# ============================================================
# BLE HR BEMENŐ ADATKEZELÉS
# ============================================================


class BLEHRInputHandler(_BLESensorInputHandler):
    """BLE Heart Rate Service (UUID: 0x180D) fogadó.

    Parse: flags byte bit 0 → 0 = 8-bites HR, 1 = 16-bites HR.
    """

    SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
    MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
    _sensor_label = "BLE HR"
    _settings_prefix = "ble_hr"

    @property
    def hr_lastdata(self) -> float:
        """Visszafelé kompatibilis alias a lastdata attribútumhoz."""
        return self.lastdata

    @hr_lastdata.setter
    def hr_lastdata(self, value: float) -> None:
        self.lastdata = value

    def _parse_notification(self, data: bytes) -> Optional[float]:
        if len(data) < 2:
            return None
        flags = data[0]
        # bit 0: 0 = 8-bites HR, 1 = 16-bites HR
        if flags & 0x01:
            if len(data) < 3:
                return None
            return float(int.from_bytes(data[1:3], byteorder="little"))
        return float(data[1])


# ============================================================
# BLE KOMBINÁLT SZENZOR
# ============================================================


class BLECombinedSensor:
    """Power és HR szenzor aggregátor."""

    def __init__(
        self,
        power_handler: Optional[Any] = None,
        hr_handler: Optional[Any] = None,
    ) -> None:
        self.power_handler = power_handler
        self.hr_handler = hr_handler

    @property
    def power_lastdata(self) -> float:
        """Visszafelé kompatibilis property."""
        if self.power_handler:
            return getattr(self.power_handler, "power_lastdata", 0)
        return 0

    @property
    def hr_lastdata(self) -> float:
        """Visszafelé kompatibilis property."""
        if self.hr_handler:
            return getattr(self.hr_handler, "hr_lastdata", 0)
        return 0

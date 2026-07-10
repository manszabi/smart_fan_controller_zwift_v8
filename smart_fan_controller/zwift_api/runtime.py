"""Futásidejű komponensek: adattároló, UDP broadcaster és a polling ciklus."""
from __future__ import annotations

import json
import logging
import platform as _platform
import socket
import struct
import subprocess
import threading
import time
from typing import Any

import requests

from .api import RateLimitError, ZwiftAPIClient, ZwiftAuth

log = logging.getLogger("zwift_api_polling")

BROADCAST_HOST = "127.0.0.1"
BROADCAST_PORT = 7878
DEFAULT_POLL_INTERVAL = 5.0  # seconds

# Back-off for rate-limit (429) responses
RATE_LIMIT_BACKOFF = 5.0  # seconds


class ZwiftDataStore:
    """Thread-safe store for the most recent Zwift rider data.

    Thread-safe store for the most recent Zwift rider data so
    the output dict is byte-for-byte compatible.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._power: int = 0
        self._heartrate: int = 0
        self._cadence: int = 0
        self._speed_kmh: float = 0.0
        self._rider_id: int = 0
        self._last_update: float = 0.0
        self._total_polls: int = 0

    def update(self, state: dict[str, Any]) -> None:
        """Store the latest values from an API response dict."""
        with self._lock:
            self._power = int(state.get("power", self._power))
            self._heartrate = int(state.get("heartrate", self._heartrate))
            self._cadence = int(state.get("cadence", self._cadence))
            speed_raw = state.get("speed", state.get("speed_kmh", 0))
            self._speed_kmh = round(float(speed_raw), 1)
            if state.get("riderId") or state.get("rider_id"):
                self._rider_id = int(
                    state.get("riderId") or state.get("rider_id", self._rider_id)
                )
            self._last_update = time.time()
            self._total_polls += 1

    def get_data(self) -> dict[str, Any]:
        """Return a dict that is structurally identical to ZwiftDataStore.get_data()."""
        with self._lock:
            return {
                "power": self._power,
                "heartrate": self._heartrate,
                "cadence": self._cadence,
                "speed_kmh": self._speed_kmh,
                "rider_id": self._rider_id,
                "last_update": self._last_update,
                "total_packets": self._total_polls,
                "timestamp": time.time(),
            }

    @property
    def total_polls(self) -> int:
        with self._lock:
            return self._total_polls


class UDPBroadcaster:
    """Sends JSON data via UDP to BROADCAST_HOST:BROADCAST_PORT."""

    def __init__(self, host: str = BROADCAST_HOST, port: int = BROADCAST_PORT):
        self._host = host
        self._port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if _platform.system() == "Windows":
            # Windows-egyediség: ha a cél-port nem figyel (a fő app még nem
            # indult el), az ICMP "port unreachable" a KÜLDŐ socketen
            # ConnectionResetError-t okoz a következő műveletnél. A
            # SIO_UDP_CONNRESET ioctl ezt a jelentést kapcsolja ki.
            try:
                SIO_UDP_CONNRESET = -1744830452  # 0x9800000C
                self._sock.ioctl(SIO_UDP_CONNRESET, struct.pack("I", 0))
            except (OSError, AttributeError, ValueError) as exc:
                log.debug(f"SIO_UDP_CONNRESET beállítás sikertelen: {exc}")

    def send(self, data: dict[str, Any]) -> None:
        """JSON-encode *data* and send it via UDP."""
        payload = json.dumps(data).encode("utf-8")
        self._sock.sendto(payload, (self._host, self._port))

    def log_console(self, data: dict[str, Any]) -> None:
        """Egy soros összefoglaló logolása (konzol + fájl)."""
        log.info(
            f"⚡ {data['power']:>4}W  "
            f"❤️  {data['heartrate']:>3}bpm  "
            f"🚴 {data['cadence']:>3}rpm  "
            f"🚀 {data['speed_kmh']:>5.1f}km/h  "
            f"📦 {data['total_packets']} polls"
        )

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def _is_zwift_running() -> bool:
    """Check if ZwiftApp.exe is running (Windows only, returns True on other OS)."""
    if _platform.system() != "Windows":
        return True
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ZwiftApp.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            # Ablakos (pythonw/noconsole) futtatásnál se villanjon konzol
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return "zwiftapp.exe" in result.stdout.lower()
    except (subprocess.TimeoutExpired, OSError):
        return True  # ha nem tudjuk ellenőrizni, ne lépjünk ki


def run_polling_loop(
    client: ZwiftAPIClient,
    auth: ZwiftAuth,
    store: ZwiftDataStore,
    broadcaster: UDPBroadcaster,
    stop_event: threading.Event,
    rider_id: int,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    debug: bool = False,
) -> None:
    """Main polling loop: fetch player state → store → broadcast."""
    world_id: int | None = None
    consecutive_errors: int = 0
    _ZWIFT_CHECK_INTERVAL = 10.0  # seconds between process checks
    _ZWIFT_GRACE_PERIOD = 300.0   # 5 perc várakozás a Zwift indulására
    _last_zwift_check: float = 0.0
    _zwift_seen: bool = False      # True ha egyszer már láttuk futni

    # Wait for ZwiftApp.exe to start (grace period)
    if _platform.system() == "Windows" and not _is_zwift_running():
        log.info(
            f"⏳ ZwiftApp.exe nem fut, várakozás max {_ZWIFT_GRACE_PERIOD:.0f}s / "
            f"ZwiftApp.exe not running, waiting up to {_ZWIFT_GRACE_PERIOD:.0f}s …"
        )
        grace_start = time.time()
        while not stop_event.is_set():
            if _is_zwift_running():
                log.info("✅ ZwiftApp.exe elindult / ZwiftApp.exe started!")
                _zwift_seen = True
                break
            if time.time() - grace_start >= _ZWIFT_GRACE_PERIOD:
                log.error(
                    "❌ ZwiftApp.exe nem indult el időben, kilépés / "
                    "ZwiftApp.exe did not start in time, exiting."
                )
                stop_event.set()
                return
            stop_event.wait(_ZWIFT_CHECK_INTERVAL)
    else:
        _zwift_seen = _is_zwift_running()

    while not stop_event.is_set():
        loop_start = time.time()

        # Periodic ZwiftApp.exe process check (only after we've seen it running)
        if _zwift_seen and loop_start - _last_zwift_check >= _ZWIFT_CHECK_INTERVAL:
            _last_zwift_check = loop_start
            if not _is_zwift_running():
                log.info(
                    "ZwiftApp.exe kilépett, program leállítása / "
                    "ZwiftApp.exe exited, stopping …"
                )
                stop_event.set()
                return

        try:
            auth.ensure_valid_token()

            # Discover world if we don't have it yet
            if world_id is None:
                world_id = client.get_active_world(rider_id)
                if world_id is None:
                    log.debug("Nem aktív a lovaglás / Rider not currently active")
                    _sleep_remainder(loop_start, poll_interval, stop_event)
                    continue

            state = client.get_player_state(world_id, rider_id)
            if state is None:
                log.debug(
                    f"Rider {rider_id} nem található ebben a világban / "
                    f"not found in world {world_id}"
                )
                world_id = None  # reset so we re-discover next iteration
                _sleep_remainder(loop_start, poll_interval, stop_event)
                continue

            store.update(state)
            data = store.get_data()
            try:
                broadcaster.send(data)
            except OSError as exc:
                # A konzol-összefoglalót akkor is kiírjuk, ha az UDP küldés
                # átmenetileg nem megy (pl. a fő app még nem figyel)
                log.debug(f"UDP küldés sikertelen: {exc}")
            broadcaster.log_console(data)
            consecutive_errors = 0

        except RateLimitError:
            log.warning(
                f"⚠️  Rate limit elérve, várakozás {RATE_LIMIT_BACKOFF}s / "
                f"Rate limited, backing off {RATE_LIMIT_BACKOFF}s"
            )
            stop_event.wait(RATE_LIMIT_BACKOFF)
            continue

        except requests.exceptions.ConnectionError as exc:
            consecutive_errors += 1
            log.warning(
                f"⚠️  Hálózati hiba (#{consecutive_errors}) / "
                f"Network error (#{consecutive_errors}): {exc}"
            )
            stop_event.wait(_backoff_seconds(consecutive_errors))
            continue

        except requests.exceptions.HTTPError as exc:
            consecutive_errors += 1
            log.warning(f"⚠️  HTTP hiba / HTTP error: {exc}")
            stop_event.wait(_backoff_seconds(consecutive_errors))
            continue

        except Exception as exc:  # noqa: BLE001
            consecutive_errors += 1
            log.warning(f"⚠️  Váratlan hiba / Unexpected error: {exc}")
            log.debug("Traceback:", exc_info=True)
            stop_event.wait(_backoff_seconds(consecutive_errors))
            continue

        _sleep_remainder(loop_start, poll_interval, stop_event)


def _sleep_remainder(loop_start: float, interval: float, stop_event: threading.Event) -> None:
    """Sleep for the remaining time in the polling interval."""
    elapsed = time.time() - loop_start
    remaining = interval - elapsed
    if remaining > 0:
        stop_event.wait(remaining)


def _backoff_seconds(consecutive_errors: int, cap: float = 30.0) -> float:
    """Exponenciális backoff sapkázott kitevővel.

    A kitevőt is sapkázni kell, nem csak az eredményt: a 2.0**N float hatvány
    nagy N-nél (~1024, azaz több órányi folyamatos hiba után) OverflowError-t
    dobna – éppen a hibakezelő ágban.
    """
    return min(cap, 2.0 ** min(consecutive_errors, 10))

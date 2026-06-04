"""Zwift UDP Input handler – asyncio DatagramProtocol alapú.

A zwift_api_polling programból érkező JSON csomagokat fogadja UDP-n.
Asyncio DatagramProtocol alapú implementáció, teljesen non-blocking.
Érvényes power és HR értékeket az asyncio queue-kba teszi.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional, cast

from smart_fan_controller.config.schemas import DataSource, HeartRateZonesConfig
from smart_fan_controller.core import is_valid_hr, is_valid_power

logger = logging.getLogger("swift_fan_controller_new")


class ZwiftUDPInputHandler:
    """Zwift UDP adatforrás fogadó – asyncio DatagramProtocol alapú.

    A zwift_api_polling programból érkező JSON csomagokat fogadja UDP-n.
    Asyncio DatagramProtocol alapú implementáció, teljesen non-blocking.
    Érvényes power és HR értékeket az asyncio queue-kba teszi.

    JSON formátum:
        {"power": int, "heartrate": int}

    Attribútumok:
        process_power: True, ha a power adatokat kell feldolgozni.
        process_hr: True, ha a HR adatokat kell feldolgozni.
        last_packet_time: utolsó érvényes ZwiftUDP csomag ideje (monotonic).
    """

    def __init__(
        self,
        settings: Dict[str, Any],
        power_queue: asyncio.Queue[float],
        hr_queue: asyncio.Queue[float],
    ) -> None:
        from smart_fan_controller.config.schemas import DatasourceConfig

        ds: DatasourceConfig = settings["datasource"]
        self.settings = settings
        self.host: str = ds.zwift_udp_host
        self.port: int = ds.zwift_udp_port
        self.power_queue = power_queue
        self.hr_queue = hr_queue

        self.process_power: bool = ds.power_source == DataSource.ZWIFTUDP
        hr_enabled: bool = settings["heart_rate_zones"].enabled
        self.process_hr: bool = ds.hr_source == DataSource.ZWIFTUDP and hr_enabled

        self._transport: Any = None

        # HUD számára: utolsó érvényes csomag ideje
        self.last_packet_time: float = 0.0

    async def run(self) -> None:
        """A Zwift UDP fogadó fő korrutinja – asyncio DatagramProtocol-t indít."""
        loop = asyncio.get_running_loop()
        logger.info(f"Zwift UDP fogadó elindítva: {self.host}:{self.port}")

        handler = self

        class _Protocol(asyncio.DatagramProtocol):
            def connection_made(self, transport: Any) -> None:
                logger.info(f"Zwift UDP socket kötve: {handler.host}:{handler.port}")
                handler._transport = transport

            def datagram_received(self, data: bytes, addr: Any) -> None:
                handler._process_packet(data)

            def error_received(self, exc: Exception) -> None:
                logger.warning(f"Zwift UDP hiba: {exc}")

            def connection_lost(self, exc: Optional[Exception]) -> None:
                logger.info("Zwift UDP kapcsolat lezárva")

        try:
            transport, _ = await loop.create_datagram_endpoint(
                _Protocol,
                local_addr=(self.host, self.port),
            )
            try:
                while True:
                    await asyncio.sleep(3600)
            finally:
                transport.close()
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            logger.error(f"Zwift UDP bind hiba: {exc}")

    def _process_packet(self, raw: bytes) -> None:
        """JSON csomag feldolgozása – validáció és queue-ba helyezés.

        A power validációhoz a settings-ből olvassa a max_watt értéket,
        így konzisztens marad a power_processor_task szűrőjével.
        """
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if not isinstance(data, dict):
            return
        # Pylance szűkíti dict[str, Unknown]-ra; cast → dict[str, Any]
        pkt = cast(Dict[str, Any], data)

        valid_any = False

        if self.process_power and "power" in pkt:
            p: int | float = pkt["power"]
            min_watt = self.settings["power_zones"].min_watt
            max_watt = self.settings["power_zones"].max_watt
            if is_valid_power(p, min_watt, max_watt):
                try:
                    self.power_queue.put_nowait(round(p))
                    valid_any = True
                except asyncio.QueueFull:
                    logger.debug("Zwift UDP: power queue teli, adat elvetve")
            else:
                logger.debug(f"Zwift UDP: érvénytelen power: {p}")

        if self.process_hr and "heartrate" in pkt:
            hrz: HeartRateZonesConfig = self.settings["heart_rate_zones"]
            valid_min_hr: int = hrz.valid_min_hr
            valid_max_hr: int = hrz.valid_max_hr

            h: int | float = pkt["heartrate"]
            if is_valid_hr(h, valid_min_hr, valid_max_hr):
                try:
                    self.hr_queue.put_nowait(round(h))
                    valid_any = True
                except asyncio.QueueFull:
                    logger.debug("Zwift UDP: hr queue teli, adat elvetve")
            else:
                logger.debug(f"Zwift UDP: érvénytelen heartrate: {h}")

        # Ha bármilyen érvényes adatot elfogadtunk, frissítjük az időbélyeget
        if valid_any:
            self.last_packet_time = time.monotonic()

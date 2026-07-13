"""Zwift UDP input handler – based on an asyncio DatagramProtocol.

Receives the JSON packets sent over UDP by the zwift_api_polling helper
process. Fully non-blocking; valid power and HR values are placed into
the asyncio queues.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, cast

from smart_fan_controller.config.schemas import DataSource, HeartRateZonesConfig
from smart_fan_controller.core import is_valid_hr, is_valid_power

logger = logging.getLogger("zwift_fan_controller_new")


class ZwiftUDPInputHandler:
    """Zwift UDP data source receiver – based on an asyncio DatagramProtocol.

    Receives the JSON packets sent over UDP by the zwift_api_polling
    helper process. Fully non-blocking; valid power and HR values are
    placed into the asyncio queues.

    JSON format:
        {"power": int, "heartrate": int}

    Attributes:
        process_power: True when power data should be processed.
        process_hr: True when HR data should be processed.
        last_packet_time: time of the last valid packet (monotonic).
    """

    def __init__(
        self,
        settings: dict[str, Any],
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

        # Validation bounds cached (settings never change at runtime;
        # consistent with the processors, which also read them at startup)
        self._min_watt: int = settings["power_zones"].min_watt
        self._max_watt: int = settings["power_zones"].max_watt
        hrz: HeartRateZonesConfig = settings["heart_rate_zones"]
        self._valid_min_hr: int = hrz.valid_min_hr
        self._valid_max_hr: int = hrz.valid_max_hr

        self._transport: Any = None

        # For the HUD: time of the last valid packet
        self.last_packet_time: float = 0.0
        # Per-metric timestamps – the HUD reads them for the P:OK/FAIL and
        # HR:OK/FAIL display consistent with the BLE/ANT handlers
        self.power_lastdata: float = 0.0
        self.hr_lastdata: float = 0.0

    async def run(self) -> None:
        """Main coroutine of the receiver – starts an asyncio DatagramProtocol."""
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

            def connection_lost(self, exc: Exception | None) -> None:
                logger.info("Zwift UDP kapcsolat lezárva")

        try:
            transport, _ = await loop.create_datagram_endpoint(
                _Protocol,
                local_addr=(self.host, self.port),
            )
            try:
                # An event that never fires: sleeps until task cancellation
                # (no pointless hourly wakeups like a sleep loop would have)
                await asyncio.Event().wait()
            finally:
                transport.close()
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            logger.error(f"Zwift UDP bind hiba: {exc}")

    def _process_packet(self, raw: bytes) -> None:
        """Process one JSON packet – validation and queueing.

        Power validation reads max_watt from the settings, staying
        consistent with the power_processor_task filter.
        """
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if not isinstance(data, dict):
            return
        # Pylance narrows to dict[str, Unknown]; cast → dict[str, Any]
        pkt = cast(dict[str, Any], data)

        valid_any = False

        if self.process_power and "power" in pkt:
            p: int | float = pkt["power"]
            if is_valid_power(p, self._min_watt, self._max_watt):
                try:
                    self.power_queue.put_nowait(round(p))
                    valid_any = True
                    self.power_lastdata = time.monotonic()
                except asyncio.QueueFull:
                    logger.debug("Zwift UDP: power queue teli, adat elvetve")
            else:
                logger.debug("Zwift UDP: érvénytelen power: %s", p)

        if self.process_hr and "heartrate" in pkt:
            h: int | float = pkt["heartrate"]
            if is_valid_hr(h, self._valid_min_hr, self._valid_max_hr):
                try:
                    self.hr_queue.put_nowait(round(h))
                    valid_any = True
                    self.hr_lastdata = time.monotonic()
                except asyncio.QueueFull:
                    logger.debug("Zwift UDP: hr queue teli, adat elvetve")
            else:
                logger.debug("Zwift UDP: érvénytelen heartrate: %s", h)

        # Refresh the timestamp whenever any valid data was accepted
        if valid_any:
            self.last_packet_time = time.monotonic()

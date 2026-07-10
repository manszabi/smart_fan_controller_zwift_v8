"""Async adatsík tesztek – processzorok, Zwift UDP fogadó, protobuf dekóder.

A teljes láncot valódi komponensekkel hajtja végig (hardver és hálózati
függőség nélkül): power minta → zóna parancs → dropout → LEVEL:0.
Az aszinkron forgatókönyveket ``asyncio.run()`` hajtja (a projekt nem
használ pytest-asyncio-t – konzisztensen a TestBleFanReconnect mintájával).
"""
from __future__ import annotations

import asyncio
import socket
import struct

from smart_fan_controller.config import DataSource, ZoneMode
from smart_fan_controller.config.schemas import (
    DatasourceConfig,
    GlobalSettingsConfig,
    HeartRateZonesConfig,
    PowerZonesConfig,
)
from smart_fan_controller.core import (
    ConsolePrinter,
    ControllerState,
    CooldownController,
    HRAverager,
    PowerAverager,
    calculate_power_zones,
)
from smart_fan_controller.processors import (
    _guarded_task,
    dropout_checker_task,
    power_processor_task,
    zone_controller_task,
)


def _pipeline_settings() -> dict:
    """Gyors tesztbeállítások: 1s-es bufferek/dropout, cooldown nélkül."""
    return {
        "power_zones": PowerZonesConfig(ftp=200, min_watt=0, max_watt=1000),
        "heart_rate_zones": HeartRateZonesConfig(enabled=False),
        "datasource": DatasourceConfig(
            power_source=DataSource.ZWIFTUDP, hr_source=None,
            zwiftUDP_buffer_seconds=1, zwiftUDP_minimum_samples=1,
            zwiftUDP_buffer_rate_hz=4, zwiftUDP_dropout_timeout=1,
        ),
        "global_settings": GlobalSettingsConfig(cooldown_seconds=0),
    }


class _Pipeline:
    """A power → zóna adatsík összeállítása (context manager taskokkal)."""

    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self.raw_power: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.zone_cmd: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.zone_event = asyncio.Event()
        self.state = ControllerState()
        self.cooldown = CooldownController(0)
        self.power_zones = calculate_power_zones(200, 0, 1000, 60, 89)
        self._tasks: list[asyncio.Task] = []

    async def __aenter__(self) -> "_Pipeline":
        avg = PowerAverager(1, 1, 4)
        self._tasks = [
            asyncio.create_task(power_processor_task(
                self.raw_power, self.state, self.zone_event, avg,
                ConsolePrinter(), self.settings, self.power_zones)),
            asyncio.create_task(zone_controller_task(
                self.state, self.zone_cmd, self.cooldown,
                self.settings, self.zone_event)),
            asyncio.create_task(dropout_checker_task(
                self.state, self.zone_cmd, self.settings,
                avg, HRAverager(1, 1, 4),
                1.0, 1.0, ZoneMode.POWER_ONLY, self.cooldown)),
        ]
        return self

    async def __aexit__(self, *exc) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass


class TestAsyncPipeline:
    """Power minta → zóna parancs → dropout lánc, valódi komponensekkel."""

    def test_power_sample_produces_zone_command(self):
        """215 W (Z3 tartomány) → LEVEL:3 parancs a fan queue-ban."""
        async def scenario():
            async with _Pipeline(_pipeline_settings()) as p:
                await p.raw_power.put(215.0)
                zone = await asyncio.wait_for(p.zone_cmd.get(), timeout=3)
                assert zone == 3

        asyncio.run(scenario())

    def test_zone_transition_without_cooldown(self):
        """Teljesítmény-esés cooldown nélkül azonnali zónaváltást ad."""
        async def scenario():
            async with _Pipeline(_pipeline_settings()) as p:
                await p.raw_power.put(215.0)
                assert await asyncio.wait_for(p.zone_cmd.get(), timeout=3) == 3
                for _ in range(6):        # a gördülő átlag leérjen 100 W-ra
                    await p.raw_power.put(100.0)
                    await asyncio.sleep(0.02)
                assert await asyncio.wait_for(p.zone_cmd.get(), timeout=3) == 1

        asyncio.run(scenario())

    def test_dropout_sends_level_zero(self):
        """Elapadó adat → a dropout checker LEVEL:0-t küld és nullázza a snapshotot."""
        async def scenario():
            async with _Pipeline(_pipeline_settings()) as p:
                await p.raw_power.put(215.0)
                assert await asyncio.wait_for(p.zone_cmd.get(), timeout=3) == 3
                # Nincs több adat → 1s-es dropout timeout után LEVEL:0
                zone = await asyncio.wait_for(p.zone_cmd.get(), timeout=5)
                assert zone == 0
                z, avg_p, _ = p.state.ui_snapshot.read()
                assert z == 0 and avg_p is None

        asyncio.run(scenario())

    def test_invalid_samples_do_not_crash_pipeline(self):
        """NaN / negatív érték eldobva; a lánc utána is működik."""
        async def scenario():
            async with _Pipeline(_pipeline_settings()) as p:
                await p.raw_power.put(float("nan"))
                await p.raw_power.put(-50.0)
                await p.raw_power.put(215.0)
                assert await asyncio.wait_for(p.zone_cmd.get(), timeout=3) == 3

        asyncio.run(scenario())

    def test_guarded_task_retries_with_factory(self):
        """A _guarded_task hibázó korrutint a factory-val újraindít."""
        attempts: list[int] = []

        async def flaky():
            attempts.append(1)
            if len(attempts) < 2:
                raise RuntimeError("szimulált hiba")

        async def scenario():
            await _guarded_task(flaky(), "Flaky", max_retries=3,
                                retry_delay=0.01, coro_factory=flaky)

        asyncio.run(scenario())
        assert len(attempts) == 2


class TestZwiftUdpReceiver:
    """A ZwiftUDPInputHandler fogadó vége – valódi UDP csomagokkal."""

    def test_receive_validate_and_survive_garbage(self):
        from smart_fan_controller.handlers.zwift_udp import ZwiftUDPInputHandler

        settings = {
            "datasource": DatasourceConfig(
                power_source=DataSource.ZWIFTUDP,
                hr_source=DataSource.ZWIFTUDP,
                zwift_udp_host="127.0.0.1",
                zwift_udp_port=1024,   # helyére az OS által adott port kerül
            ),
            "heart_rate_zones": HeartRateZonesConfig(enabled=True),
            "power_zones": PowerZonesConfig(min_watt=0, max_watt=1000),
        }

        async def scenario():
            pq: asyncio.Queue = asyncio.Queue(maxsize=100)
            hq: asyncio.Queue = asyncio.Queue(maxsize=100)
            handler = ZwiftUDPInputHandler(settings, pq, hq)
            handler.port = 0    # OS-választott port (párhuzamos futás-biztos)
            task = asyncio.create_task(handler.run())
            try:
                # Várjuk a bindet, majd olvassuk ki a tényleges portot
                for _ in range(100):
                    if handler._transport is not None:
                        break
                    await asyncio.sleep(0.01)
                assert handler._transport is not None
                port = handler._transport.get_extra_info("sockname")[1]

                tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    # Érvényes csomag
                    tx.sendto(b'{"power": 213, "heartrate": 147}',
                              ("127.0.0.1", port))
                    await asyncio.sleep(0.2)
                    assert pq.get_nowait() == 213
                    assert hq.get_nowait() == 147
                    assert handler.last_packet_time > 0

                    # Érvénytelen értékek + szemét: eldobva, nincs crash
                    t0 = handler.last_packet_time
                    tx.sendto(b'{"power": 99999, "heartrate": 5}',
                              ("127.0.0.1", port))
                    tx.sendto(b"\xff\xfe nem json", ("127.0.0.1", port))
                    tx.sendto(b"[1,2,3]", ("127.0.0.1", port))
                    await asyncio.sleep(0.2)
                    assert pq.empty() and hq.empty()
                    assert handler.last_packet_time == t0

                    # A fogadó túlélte: újabb érvényes csomag átmegy
                    tx.sendto(b'{"power": 150}', ("127.0.0.1", port))
                    await asyncio.sleep(0.2)
                    assert pq.get_nowait() == 150
                finally:
                    tx.close()
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(scenario())


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b7 = n & 0x7F
        n >>= 7
        out += bytes([b7 | (0x80 if n else 0)])
        if not n:
            return out


def _field(num: int, wire: int, payload: bytes) -> bytes:
    return _varint((num << 3) | wire) + payload


class TestProtobufDecoder:
    """A zwift_api minimál protobuf dekódere – szintetikus PlayerState blobbal."""

    def test_playerstate_extraction_and_units(self):
        from smart_fan_controller.zwift_api.decoder import _parse_protobuf_player_state

        blob = (
            _field(1, 0, _varint(12345))          # riderId
            + _field(6, 0, _varint(35_200_000))   # speed (mm/h) → 35.2 km/h
            + _field(9, 0, _varint(1_500_000))    # cadence (µHz) → 90 rpm
            + _field(11, 0, _varint(147))         # heartrate
            + _field(12, 0, _varint(213))         # power
            + _field(20, 2, _varint(3) + b"xyz")  # ismeretlen mező (kihagyandó)
            + _field(21, 5, struct.pack("<f", 1.5))
        )
        state = _parse_protobuf_player_state(blob)
        assert state == {
            "riderId": 12345, "power": 213, "heartrate": 147,
            "cadence": 90, "speed_kmh": 35.2,
        }

    def test_inactive_rider_returns_none(self):
        from smart_fan_controller.zwift_api.decoder import _parse_protobuf_player_state

        assert _parse_protobuf_player_state(_field(12, 0, _varint(213))) is None

    def test_garbage_resilience(self):
        from smart_fan_controller.zwift_api.decoder import (
            ProtobufDecoder, _parse_protobuf_player_state,
        )

        assert _parse_protobuf_player_state(b"") is None
        _parse_protobuf_player_state(b"\xff\xff\xff")     # csonka varint
        _parse_protobuf_player_state(b"\xff" * 20)        # varint-bomba
        list(ProtobufDecoder(_field(1, 3, b"")).fields())  # ismeretlen wire type


class TestBackoff:
    def test_backoff_capped_no_overflow(self):
        from smart_fan_controller.zwift_api.runtime import _backoff_seconds

        assert _backoff_seconds(1) == 2.0
        assert _backoff_seconds(4) == 16.0
        assert _backoff_seconds(5) == 30.0
        assert _backoff_seconds(10 ** 6) == 30.0   # nincs OverflowError

"""Async adatsík tesztek – processzorok, Zwift UDP fogadó, protobuf dekóder.

A teljes láncot valódi komponensekkel hajtja végig (hardver és hálózati
függőség nélkül): power minta → zóna parancs → dropout → LEVEL:0.
Az aszinkron forgatókönyveket ``asyncio.run()`` hajtja (a projekt nem
használ pytest-asyncio-t – konzisztensen a TestBleFanReconnect mintájával).
"""
from __future__ import annotations

import asyncio
import os
import socket
import struct
import time

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


class TestZwiftUdpRebind:
    """Foglalt port: a fogadó újrapróbálkozik, nem hal el véglegesen.

    Regresszió: a run() elnyelte az OSError-t és normálisan visszatért, amit
    a _guarded_task sikeres befejezésnek látott – így a Zwift adatforrás egy
    logsor után az egész munkamenetre halott maradt (HUD: örök ZWIFT P:FAIL).
    """

    def _handler(self, port):
        from smart_fan_controller.handlers.zwift_udp import ZwiftUDPInputHandler
        settings = {
            "datasource": DatasourceConfig(
                power_source=DataSource.ZWIFTUDP,
                hr_source=DataSource.ZWIFTUDP,
                zwift_udp_host="127.0.0.1",
                zwift_udp_port=port,
            ),
            "heart_rate_zones": HeartRateZonesConfig(enabled=True),
            "power_zones": PowerZonesConfig(min_watt=0, max_watt=1000),
        }
        return ZwiftUDPInputHandler(settings, asyncio.Queue(maxsize=10),
                                    asyncio.Queue(maxsize=10))

    def test_busy_port_retries_then_binds_when_freed(self):
        # A port elfoglalása egy másik sockettel
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", 0))
        port = blocker.getsockname()[1]

        async def scenario():
            handler = self._handler(port)
            handler.REBIND_DELAY = 0.05        # a teszt ne várjon 5s-et
            handler.REBIND_DELAY_MAX = 0.05
            task = asyncio.create_task(handler.run())
            try:
                await asyncio.sleep(0.2)
                # Foglalt port: még nincs transport, de a task ÉL (nem halt el)
                assert handler._transport is None
                assert not task.done()

                blocker.close()               # a port felszabadul
                for _ in range(100):
                    if handler._transport is not None:
                        break
                    await asyncio.sleep(0.02)
                assert handler._transport is not None, "nem kötött újra a felszabadult portra"
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                blocker.close()

        asyncio.run(scenario())


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


class TestStaleBufferAfterGap:
    """Forrás-szünet után a régi minták nem keveredhetnek az új átlagba."""

    @staticmethod
    def _settings(dropout: int = 1) -> dict:
        s = _pipeline_settings()
        s["datasource"] = DatasourceConfig(
            power_source=DataSource.ZWIFTUDP, hr_source=None,
            zwiftUDP_buffer_seconds=1, zwiftUDP_minimum_samples=2,
            zwiftUDP_buffer_rate_hz=4, zwiftUDP_dropout_timeout=dropout,
        )
        return s

    def test_averager_cleared_after_a_gap(self):
        """A dropout timeout-nál hosszabb szünet után ürül a buffer.

        Az átlagoló a ``effective_minimum`` mintát az időablakon túl is
        megtartja (hogy a lassú forrás is adjon átlagot). Egy valódi
        kiesés után ezek percekkel korábbi wattok – nélkülük a
        visszatéréskor számolt első átlag azokkal keveredett. A dropout
        checker csak nem-nulla zónában ürít, ezért az álló helyzet
        (0. zóna) esetét a processzornak kell kezelnie.
        """
        async def scenario():
            settings = self._settings(dropout=1)
            state = ControllerState()
            avg = PowerAverager(1, 2, 4)
            queue: asyncio.Queue = asyncio.Queue(maxsize=100)
            event = asyncio.Event()
            task = asyncio.create_task(power_processor_task(
                queue, state, event, avg, ConsolePrinter(), settings,
                calculate_power_zones(200, 0, 1000, 60, 89)))
            try:
                for _ in range(2):
                    await queue.put(300.0)
                await asyncio.wait_for(_avg_becomes(state, 300), timeout=3)

                # Szünet a dropout timeout fölött, majd új minta
                await asyncio.sleep(1.2)
                await queue.put(100.0)
                await asyncio.sleep(0.2)
                # Az első minta önmagában kevés (effective_minimum=2),
                # tehát a régi 300 W nem húzhatja fel az átlagot 200-ra
                assert state.current_avg_power == 300, "a régi minta beleszámított"

                await queue.put(100.0)
                await asyncio.wait_for(_avg_becomes(state, 100), timeout=3)
                assert list(avg.buffer) == [100.0, 100.0]
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(scenario())

    def test_continuous_stream_is_not_cleared(self):
        """Folyamatos adatfolyamnál a buffer nem ürül feleslegesen."""
        async def scenario():
            settings = self._settings(dropout=5)
            state = ControllerState()
            avg = PowerAverager(1, 2, 4)
            queue: asyncio.Queue = asyncio.Queue(maxsize=100)
            event = asyncio.Event()
            task = asyncio.create_task(power_processor_task(
                queue, state, event, avg, ConsolePrinter(), settings,
                calculate_power_zones(200, 0, 1000, 60, 89)))
            try:
                for _ in range(4):
                    await queue.put(200.0)
                    await asyncio.sleep(0.05)
                await asyncio.wait_for(_avg_becomes(state, 200), timeout=3)
                assert len(avg.buffer) >= 2
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(scenario())


class TestZoneDecisionAtomicity:
    """A zóna döntés olvasás→döntés→visszaírás egyetlen lock alatt fut."""

    def test_cooldown_is_evaluated_while_holding_the_state_lock(self):
        """A cooldown döntés közben a state lock végig fogva van.

        Elengedve a dropout checker közbeékelődhetett: LEVEL:0-ra vitte a
        zónát, majd ez a task a kiesés előtti zónát írta vissza fölé – a
        ventilátor elavult adat alapján tovább járt.
        """
        async def scenario():
            settings = _pipeline_settings()
            state = ControllerState()
            cooldown = CooldownController(0)
            observed: list[bool] = []
            real_process = cooldown.process

            def spy(*args, **kwargs):
                observed.append(state.lock.locked())
                return real_process(*args, **kwargs)

            cooldown.process = spy  # type: ignore[method-assign]
            zone_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
            event = asyncio.Event()
            task = asyncio.create_task(zone_controller_task(
                state, zone_queue, cooldown, settings, event))
            try:
                async with state.lock:
                    state.current_power_zone = 3
                    state.current_avg_power = 250
                    state.last_power_time = time.monotonic()
                event.set()
                assert await asyncio.wait_for(zone_queue.get(), timeout=3) == 3
                assert observed and all(observed), "a lock nem volt fogva"
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(scenario())


async def _avg_becomes(state, value: float, timeout: float = 3.0) -> None:
    """Vár, amíg a state átlagos teljesítménye eléri az adott értéket."""
    while state.current_avg_power != value:
        await asyncio.sleep(0.02)


class TestShutdownStopsTheFan:
    """Leállításkor a ventilátornak tényleg le kell állnia."""

    @staticmethod
    def _controller(tmpdir: str):
        import json as _json
        from smart_fan_controller.controller import FanController

        path = os.path.join(tmpdir, "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            _json.dump({
                "global_settings": {"logging": False},
                "datasource": {"power_source": None, "hr_source": None,
                               "zwift_auto_launch": False},
                "heart_rate_zones": {"enabled": False},
            }, f)
        return FanController(path)

    def test_level_zero_is_sent_on_cancellation(self):
        """A run() megszakításakor kimegy a LEVEL:0, a ROLLER:0 és a bontás.

        A leállítási lépések időkorlátainak összege korábban meghaladta
        azt az időt, amíg a main() az asyncio szálra várt – a daemon szál
        a folyamattal együtt elhalt, jellemzően még a LEVEL:0 előtt, így
        a ventilátor tovább járt az utolsó szinten.
        """
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp()
        try:
            ctrl = self._controller(tmp)
            calls: list[str] = []

            class _FakeFan:
                async def _write_level(self, zone):
                    calls.append(f"LEVEL:{zone}")

                async def _write_raw(self, cmd):
                    calls.append(cmd)

                async def disconnect(self):
                    calls.append("disconnect")

            async def scenario():
                ctrl._ble_fan = _FakeFan()          # type: ignore[assignment]
                task = asyncio.create_task(ctrl.run())
                await asyncio.sleep(0.3)
                ctrl._ble_fan = _FakeFan()          # run() overwrote it
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            asyncio.run(scenario())
            assert calls == ["LEVEL:0", "ROLLER:0", "disconnect"], calls
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_shutdown_is_time_bounded(self):
        """Beragadt BLE stack esetén sem húzódik el a leállítás."""
        from smart_fan_controller.controller import FanController

        class _StuckFan:
            async def _write_level(self, zone):
                await asyncio.sleep(60)

            async def _write_raw(self, cmd):
                await asyncio.sleep(60)

            async def disconnect(self):
                await asyncio.sleep(60)

        async def scenario():
            ctrl = FanController.__new__(FanController)
            ctrl._ble_fan = _StuckFan()             # type: ignore[assignment]
            ctrl.SHUTDOWN_FAN_TIMEOUT = 0.2         # type: ignore[misc]
            t0 = time.monotonic()
            await ctrl._shutdown_fan()
            elapsed = time.monotonic() - t0
            assert elapsed < 1.0, f"a leállítás {elapsed:.1f}s-ig tartott"
            assert ctrl._ble_fan is None

        asyncio.run(scenario())


class TestZeroImmediateIsScopedToTheActiveZoneMode:
    """A zero-immediate kapcsolók csak a döntő metrikára vonatkoznak.

    Kapuzás nélkül az IGNORÁLT forrás nulla értéke is törölte a
    cooldown-t: power_only módban a levett (resting alatti) pulzusmérő
    zero_hr_immediate-et váltott ki, hr_only módban pedig a gurulás
    közbeni 0 W – mindkét esetben a felhasználó által kikapcsolt
    azonnali leállást erőltetve rá a másik metrikára.
    """

    @staticmethod
    def _run(zone_mode: ZoneMode, *, zero_power: bool, zero_hr: bool,
             power_zone: int, hr_zone: int) -> bool:
        """Visszaadja, hogy a cooldown zero_immediate=True-val hívódott-e."""
        async def scenario() -> bool:
            settings = _pipeline_settings()
            settings["heart_rate_zones"] = HeartRateZonesConfig(
                enabled=True, zone_mode=zone_mode, zero_hr_immediate=zero_hr,
            )
            settings["power_zones"] = PowerZonesConfig(
                ftp=200, min_watt=0, max_watt=1000,
                zero_power_immediate=zero_power,
            )
            settings["datasource"] = DatasourceConfig(
                power_source=DataSource.ZWIFTUDP,
                hr_source=DataSource.ZWIFTUDP,
                zwiftUDP_buffer_seconds=1, zwiftUDP_minimum_samples=1,
                zwiftUDP_buffer_rate_hz=4, zwiftUDP_dropout_timeout=30,
            )
            state = ControllerState()
            # 120 s cooldown: azonnali leállás nélkül semmi nem megy ki
            cooldown = CooldownController(120)
            seen: list[bool] = []
            real_process = cooldown.process

            def spy(current_zone, new_zone, zero_immediate):
                seen.append(zero_immediate)
                return real_process(current_zone, new_zone, zero_immediate)

            cooldown.process = spy  # type: ignore[method-assign]
            zone_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
            event = asyncio.Event()
            task = asyncio.create_task(zone_controller_task(
                state, zone_queue, cooldown, settings, event))
            try:
                now = time.monotonic()
                async with state.lock:
                    # Már fut a ventilátor – innen esik vissza nullára
                    state.current_zone = 3
                    state.current_power_zone = power_zone
                    state.current_hr_zone = hr_zone
                    state.last_power_time = now
                    state.last_hr_time = now
                event.set()
                for _ in range(100):
                    if seen:
                        break
                    await asyncio.sleep(0.01)
                assert seen, "a cooldown nem lett meghívva"
                return seen[0]
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        return asyncio.run(scenario())

    def test_zero_hr_immediate_is_ignored_in_power_only_mode(self):
        assert self._run(
            ZoneMode.POWER_ONLY, zero_power=False, zero_hr=True,
            power_zone=0, hr_zone=0,
        ) is False

    def test_zero_power_immediate_is_ignored_in_hr_only_mode(self):
        assert self._run(
            ZoneMode.HR_ONLY, zero_power=True, zero_hr=False,
            power_zone=0, hr_zone=0,
        ) is False

    def test_zero_power_immediate_still_applies_in_power_only_mode(self):
        assert self._run(
            ZoneMode.POWER_ONLY, zero_power=True, zero_hr=False,
            power_zone=0, hr_zone=0,
        ) is True

    def test_zero_hr_immediate_still_applies_in_hr_only_mode(self):
        assert self._run(
            ZoneMode.HR_ONLY, zero_power=False, zero_hr=True,
            power_zone=0, hr_zone=0,
        ) is True

    def test_both_flags_apply_in_higher_wins_mode(self):
        assert self._run(
            ZoneMode.HIGHER_WINS, zero_power=True, zero_hr=False,
            power_zone=0, hr_zone=0,
        ) is True
        assert self._run(
            ZoneMode.HIGHER_WINS, zero_power=False, zero_hr=True,
            power_zone=0, hr_zone=0,
        ) is True

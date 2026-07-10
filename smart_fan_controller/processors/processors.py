"""Aszinkron processzorok – zóna-számítás, cooldown, dropout detektálás.

Modulok közötti adatáramlás:
- raw_power_queue, raw_hr_queue: Bemenő adatok
- power_processor_task, hr_processor_task: Feldolgozás és zóna számítás
- zone_event: Jelzés az új adat érkezéséről
- zone_controller_task: Zóna kombinálás, cooldown, BLE küldés
- dropout_checker_task: Adatforrás kiesés detektálása
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Coroutine
from typing import Any

from smart_fan_controller.config import ZoneMode, get_effective_zone_mode
from smart_fan_controller.config.loader import _resolve_buffer_settings
from smart_fan_controller.core import (
    CooldownController,
    ConsolePrinter,
    ControllerState,
    HRAverager,
    PowerAverager,
    apply_zone_mode,
    is_valid_hr,
    is_valid_power,
    zone_for_hr,
    zone_for_power,
)
from smart_fan_controller.handlers import send_zone

logger = logging.getLogger("zwift_fan_controller_new")
user_logger = logging.getLogger("user")


async def power_processor_task(
    raw_power_queue: asyncio.Queue[float],
    state: ControllerState,
    zone_event: asyncio.Event,
    power_averager: PowerAverager,
    printer: ConsolePrinter,
    settings: dict[str, Any],
    power_zones: dict[int, tuple[int, int]],
) -> None:
    """Teljesítmény adatok feldolgozása – validálás, átlagolás, állapot frissítés.

    Olvassa a raw_power_queue-t, validálja a beérkező watt értékeket,
    gördülő átlagot számít, meghatározza a zónát, frissíti a megosztott
    állapotot, majd jelzi a zone_event-tel, hogy zóna újraszámítás szükséges.

    Args:
        raw_power_queue: Nyers power adatok asyncio.Queue-ja.
        state: A megosztott vezérlő állapot.
        zone_event: asyncio.Event – beállítja, ha új átlag áll rendelkezésre.
        power_averager: PowerAverager példány.
        printer: ConsolePrinter a konzol kiíráshoz.
        settings: Betöltött beállítások dict-je.
        power_zones: Kiszámított power zóna határok.
    """
    min_watt = settings["power_zones"].min_watt
    max_watt = settings["power_zones"].max_watt
    zone_mode = get_effective_zone_mode(settings)

    logger.info("Power processor korrutin elindítva")

    while True:
        power = await raw_power_queue.get()

        if not is_valid_power(power, min_watt, max_watt):
            printer.emit("invalid_power", "⚠ FIGYELMEZTETÉS: Érvénytelen power adat!")
            continue

        power = int(power)
        now = time.monotonic()

        if zone_mode != ZoneMode.HIGHER_WINS:
            printer.emit("power_raw", f"⚡ Teljesítmény: {power} watt")

        avg_power = power_averager.add_sample(power)
        if avg_power is None:
            # Fix #39: Buffer feltöltés alatt is frissítjük a timestampet,
            # hogy a dropout checker ne jelezzen hamis kiesést
            async with state.lock:
                state.last_power_time = now
            continue

        avg_power = round(avg_power)
        new_power_zone = zone_for_power(avg_power, power_zones)

        if zone_mode == ZoneMode.HIGHER_WINS:
            printer.emit(
                "power_avg_hw",
                f"⚡ Átlag teljesítmény: {avg_power} watt | Power zóna: {new_power_zone} | Higher Wins!",
            )
        else:
            printer.emit(
                "power_avg",
                f"⚡ Átlag teljesítmény: {avg_power} watt | Power zóna: {new_power_zone}",
            )

        async with state.lock:
            state.last_power_time = now
            state.current_power_zone = new_power_zone
            state.current_avg_power = avg_power
            # Fix #1: UI snapshot frissítése lock alatt – konzisztens pillanatfelvétel
            state.ui_snapshot.update(
                state.current_zone,
                float(avg_power),
                state.current_avg_hr,
            )

        zone_event.set()  # Zone controller újraszámítást igényel


async def hr_processor_task(
    raw_hr_queue: asyncio.Queue[float],
    state: ControllerState,
    zone_event: asyncio.Event,
    hr_averager: HRAverager,
    printer: ConsolePrinter,
    settings: dict[str, Any],
    hr_zones: dict[str, int],
) -> None:
    """HR adatok feldolgozása – validálás, átlagolás, állapot frissítés.

    Olvassa a raw_hr_queue-t, validálja a bpm értékeket, gördülő átlagot
    számít, meghatározza a HR zónát, frissíti a megosztott állapotot, majd
    jelzi a zone_event-tel, hogy zóna újraszámítás szükséges.

    Frissíti a state.last_hr_time mezőt, amelyet a dropout checker
    hr_only és higher_wins módban figyelembe vesz.

    Args:
        raw_hr_queue: Nyers HR adatok asyncio.Queue-ja.
        state: A megosztott vezérlő állapot.
        zone_event: asyncio.Event – beállítja, ha új átlag áll rendelkezésre.
        hr_averager: HRAverager példány.
        printer: ConsolePrinter a konzol kiíráshoz.
        settings: Betöltött beállítások dict-je.
        hr_zones: Kiszámított HR zóna határok.
    """
    hrz = settings["heart_rate_zones"]
    zone_mode = get_effective_zone_mode(settings)
    valid_min_hr: int = hrz.valid_min_hr
    valid_max_hr: int = hrz.valid_max_hr
    hr_enabled: bool = settings["heart_rate_zones"].enabled
    logger.info("HR processor korrutin elindítva")

    while True:
        hr = await raw_hr_queue.get()

        try:
            hr = int(hr)
        except (TypeError, ValueError):
            continue
        if not is_valid_hr(hr, valid_min_hr, valid_max_hr):
            continue

        # Egyetlen now a ciklus elejéhez – konzisztens timestamp az egész iterációban
        now = time.monotonic()

        if not hr_enabled:
            printer.emit("hr_disabled", f"❤ Szívfrekvencia: {hr} bpm")
            async with state.lock:
                state.last_hr_time = now
            continue

        if zone_mode in (ZoneMode.HR_ONLY, ZoneMode.POWER_ONLY):
            printer.emit("hr_raw", f"❤ HR: {hr} bpm")

        avg_hr = hr_averager.add_sample(hr)

        if avg_hr is None:
            # Buffer feltöltés alatt is frissítjük a timestampet (dropout checker számára)
            async with state.lock:
                state.last_hr_time = now
            continue

        avg_hr = round(avg_hr)
        new_hr_zone = zone_for_hr(avg_hr, hr_zones)

        if zone_mode == ZoneMode.HR_ONLY:
            printer.emit(
                "hr_avg",
                f"❤ Átlag HR: {avg_hr} bpm | HR zóna: {new_hr_zone}",
            )
        elif zone_mode == ZoneMode.HIGHER_WINS:
            printer.emit(
                "hr_avg_hw",
                f"❤ Átlag HR: {avg_hr} bpm | HR zóna: {new_hr_zone} | Higher Wins!",
            )

        async with state.lock:
            state.last_hr_time = now
            state.current_hr_zone = new_hr_zone
            state.current_avg_hr = float(avg_hr)
            # Fix #1: UI snapshot frissítése lock alatt – konzisztens pillanatfelvétel
            state.ui_snapshot.update(
                state.current_zone,
                state.current_avg_power,
                float(avg_hr),
            )

        zone_event.set()  # Zone controller újraszámítást igényel


async def zone_controller_task(
    state: ControllerState,
    zone_queue: asyncio.Queue[int],
    cooldown_ctrl: CooldownController,
    settings: dict[str, Any],
    zone_event: asyncio.Event,
) -> None:
    """Zóna vezérlő – kombinálja a power és HR zónákat, alkalmazza a cooldownt.

    Megvárja a zone_event jelzést (amelyet a power és HR processorok állítanak be),
    majd a legfrissebb állapot alapján:
    1. Meghatározza a final zónát (apply_zone_mode / higher_wins)
    2. Alkalmazza a cooldown logikát (CooldownController)
    3. Ha szükséges, elküldi a zóna parancsot a BLE fan queue-ba

    Megjegyzés higher_wins módban: ha hr_zone None (az átlagoló még nem gyűjtött
    elég mintát), de hr_fresh True, az apply_zone_mode csak a power_zone-t
    használja – ez szándékos viselkedés.

    Args:
        state: A megosztott vezérlő állapot.
        zone_queue: BLE fan output asyncio.Queue-ja.
        cooldown_ctrl: CooldownController példány.
        settings: Betöltött beállítások dict-je.
        zone_event: asyncio.Event – jelzi, hogy új adat érkezett.
    """
    zone_mode = get_effective_zone_mode(settings)
    zero_power_immediate = settings["power_zones"].zero_power_immediate
    zero_hr_immediate = settings["heart_rate_zones"].zero_hr_immediate
    power_buf = _resolve_buffer_settings(settings, "power")
    hr_buf = _resolve_buffer_settings(settings, "hr")
    power_dropout_timeout = power_buf["dropout_timeout"]
    hr_dropout_timeout = hr_buf["dropout_timeout"]

    logger.info("Zóna vezérlő korrutin elindítva")

    while True:
        await zone_event.wait()
        zone_event.clear()
        # Állapot pillanatfelvétel (lock alatt)
        async with state.lock:
            power_zone = state.current_power_zone
            hr_zone = state.current_hr_zone
            current_zone = state.current_zone
            now = time.monotonic()
            last_power = state.last_power_time
            last_hr = state.last_hr_time

        # Frissesség ellenőrzése (dropout figyelembe vételéhez)
        # Fix #3: last_power_time most Optional – None = még nem érkezett adat
        power_fresh = (
            last_power is not None
            and (now - last_power) < power_dropout_timeout
        )
        hr_fresh = last_hr is not None and (now - last_hr) < hr_dropout_timeout

        # Zóna kombinálás a zone_mode alapján
        if zone_mode == ZoneMode.POWER_ONLY:
            final_zone = power_zone if power_fresh else None
        elif zone_mode == ZoneMode.HR_ONLY:
            final_zone = hr_zone if hr_fresh else None
        else:  # higher_wins
            p = power_zone if power_fresh else None
            h = hr_zone if hr_fresh else None
            final_zone = apply_zone_mode(p, h, zone_mode)

        if final_zone is None:
            continue  # Nincs elég friss adat a döntéshez

        # Azonnali leállás flag (zero_power_immediate / zero_hr_immediate)
        use_zero_immediate = (
            (zero_power_immediate and power_zone is not None and power_zone == 0 and power_fresh)
            or (zero_hr_immediate and hr_zone is not None and hr_zone == 0 and hr_fresh)
        )

        # Cooldown logika alkalmazása
        zone_to_send = cooldown_ctrl.process(current_zone, final_zone, use_zero_immediate)

        if zone_to_send is not None:
            async with state.lock:
                state.current_zone = zone_to_send
                # Fix #1: UI snapshot frissítése lock alatt – konzisztens pillanatfelvétel
                state.ui_snapshot.update(
                    zone_to_send,
                    state.current_avg_power,
                    state.current_avg_hr,
                )
            await send_zone(zone_to_send, zone_queue)
            user_logger.info(f"→ Zóna elküldve: LEVEL:{zone_to_send}")


async def dropout_checker_task(
    state: ControllerState,
    zonequeue: asyncio.Queue[int],
    settings: dict[str, Any],
    poweraverager: PowerAverager,
    hraverager: HRAverager,
    power_dropout_timeout: float,
    hr_dropout_timeout: float,
    zone_mode: ZoneMode,
    cooldown_ctrl: CooldownController,
) -> None:
    """Adatforrás kiesés detektálása, Z0 küldése és pufferek ürítése.

    Args:
        state: A megosztott vezérlő állapot.
        zonequeue: BLE fan output asyncio.Queue-ja.
        settings: Betöltött beállítások dict-je.
        poweraverager: PowerAverager példány (ürítéshez).
        hraverager: HRAverager példány (ürítéshez).
        power_dropout_timeout: Power forrás timeout másodpercben.
        hr_dropout_timeout: HR forrás timeout másodpercben.
        zone_mode: Aktív zóna mód (paraméterként kapja, nem számolja újra).
        cooldown_ctrl: CooldownController példány (dropout-kor reseteléshez).
    """
    logger.info("Dropout checker korrutin elindítva")

    while True:
        await asyncio.sleep(1)
        now = time.monotonic()
        send_dropout = False

        # Fix #2: Egyetlen lock blokk az egész ellenőrzéshez
        async with state.lock:
            if state.current_zone is None or state.current_zone == 0:
                continue

            # Fix #3: last_power_time Optional – None = még nem érkezett adat
            power_fresh = (
                state.last_power_time is not None
                and (now - state.last_power_time) < power_dropout_timeout
            )
            hr_fresh = (
                state.last_hr_time is not None
                and (now - state.last_hr_time) < hr_dropout_timeout
            )

            # Eltelt idő az utolsó adat óta (inf, ha még sosem érkezett)
            power_elapsed = (
                now - state.last_power_time
                if state.last_power_time is not None else math.inf
            )
            hr_elapsed = (
                now - state.last_hr_time
                if state.last_hr_time is not None else math.inf
            )

            if zone_mode == ZoneMode.POWER_ONLY:
                stale = not power_fresh
                elapsed = power_elapsed
                label = "power"
            elif zone_mode == ZoneMode.HR_ONLY:
                # Fix #4: hr_only dropout akkor is triggerel, ha soha nem érkezett HR
                stale = not hr_fresh
                elapsed = hr_elapsed
                label = "HR"
            else:  # higher_wins
                stale = not power_fresh and not hr_fresh
                if stale:
                    elapsed = max(power_elapsed, hr_elapsed)
                elif not power_fresh:
                    elapsed = power_elapsed
                elif not hr_fresh:
                    elapsed = hr_elapsed
                else:
                    elapsed = 0.0
                label = "power+HR"

            if stale:
                user_logger.info(f"Adatforrás kiesett ({label}), {elapsed:.1f}s → LEVEL:0")
                if not power_fresh:
                    poweraverager.clear()
                    state.current_avg_power = None
                    state.current_power_zone = None
                if not hr_fresh:
                    hraverager.clear()
                    state.current_avg_hr = None
                    state.current_hr_zone = None
                state.current_zone = 0
                # Fix #28: Cooldown állapot resetelése dropout-kor
                cooldown_ctrl.reset()
                # Fix #40: UI snapshot frissítése – a HUD is lássa a dropout-ot
                state.ui_snapshot.update(0, state.current_avg_power, state.current_avg_hr)
                send_dropout = True

        if send_dropout:
            await send_zone(0, zonequeue)


async def _guarded_task(
    coro: Coroutine[Any, Any, None],
    name: str,
    *,
    max_retries: int = 0,
    retry_delay: float = 5.0,
    coro_factory: Callable[[], Coroutine[Any, Any, None]] | None = None,
) -> None:
    """Task wrapper: elkapja és logolja a váratlan kivételeket.

    CancelledError-t tovább engedi (normál leálláshoz szükséges).
    Minden más kivételt kritikus szinten logolja, hogy ne tűnjön el csendben.

    Ha max_retries > 0 és coro_factory adott, a task automatikusan újraindul
    exponenciális backoff-fal (retry_delay * 2^attempt, max 60s).

    Args:
        coro: Az indítandó korrutin (első futáshoz).
        name: A task neve (logoláshoz).
        max_retries: Max újrapróbálkozások száma (0 = nincs retry).
        retry_delay: Kezdő várakozás másodpercben újraindítás előtt.
        coro_factory: Paraméter nélküli callable, ami új korrutint ad vissza.
            Retry-hoz kötelező, mert egy korrutin csak egyszer await-elhető.
    """
    attempt = 0
    current_coro = coro
    while True:
        try:
            await current_coro
            return  # Normál befejezés
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempt += 1
            if coro_factory is not None and attempt <= max_retries:
                delay = min(retry_delay * (2 ** (attempt - 1)), 60.0)
                logger.warning(
                    f"Task '{name}' hiba ({attempt}/{max_retries}): {exc} "
                    f"→ újraindítás {delay:.0f}s múlva",
                    exc_info=True,
                )
                await asyncio.sleep(delay)
                current_coro = coro_factory()
            else:
                logger.error(
                    f"Task '{name}' váratlanul leállt: {exc}", exc_info=True
                )
                return

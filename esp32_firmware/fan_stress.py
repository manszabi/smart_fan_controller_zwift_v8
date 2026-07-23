#!/usr/bin/env python3
"""
fan_stress.py — FanController "edző"/stressz-teszt BLE-n.

Folyamatosan váltogatja a ventilátor fokozatokat, mintha edzenéd az eszközt,
hogy gyorsabban kiderüljön a "30-40 perc után leáll" tünet oka. A relé ki/be
és fokozat-váltás a legdurvább terhelés (induktív tüske + BLE TX), pont ez
váltja ki a brownout-ot.

A VALÓS firmware-protokoll a fan ffe1 karakterisztikán (ugyanaz mint diag_client.py):
    AUTH:<pin>     -> AUTH_OK / AUTH_FAIL / AUTH_LOCKED / AUTH_REQUIRED
    ROLLER:1 / 0   -> rendszer be / ki (relék engedélyezése + roller)
    LEVEL:0..3     -> fokozat (0 = ki, 1/2/3 = ventilátor fokozatok)
    DIAG?          -> 0x02"DIAG_BEGIN" <chunkok> 0x04"DIAG_END" (napló)

FIGYELEM: a firmware-nek NINCS STATUS/heap lekérdezése, ezért valós idejű
heap-figyelés nincs. Helyette:
  - a BLE KAPCSOLAT MEGSZAKADÁSA jelzi a "leállást" (mikor, hány váltás után)
  - opcionálisan a DIAG naplót lekérdezzük futás közben (--check-interval),
    és figyeljük az új [boot]/[lowmem] sorokat (reset / kevés memória).

Használat:
    pip install bleak
    python3 fan_stress.py                          # végtelen, LEVEL 1,2,3, 3s/fokozat
    python3 fan_stress.py --dwell 2 --cycles 50
    python3 fan_stress.py --roller-toggle           # ROLLER ki/be is minden ciklusban (durvább)
    python3 fan_stress.py --duration 2700           # max 45 perc
    python3 fan_stress.py --check-interval 60        # percenként diag-napló ellenőrzés
    python3 fan_stress.py --reconnect --log stress.csv
    python3 fan_stress.py --address AA:BB:CC:DD:EE:FF --pin 123456
"""

import argparse
import asyncio
import time
from datetime import datetime

from bleak import BleakClient, BleakScanner

DEVICE_NAME = "FanController"
FFE1 = "0000ffe1-0000-1000-8000-00805f9b34fb"

BEGIN = b"\x02DIAG_BEGIN"
END = b"\x04DIAG_END"


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str, fh=None) -> None:
    line = f"[{ts()}] {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


class StressState:
    def __init__(self):
        self.switches = 0
        self.boot_lines = 0      # diag naplóból: hány [boot] (reset) sor
        self.lowmem_lines = 0    # diag naplóból: hány [lowmem] sor
        self.last_boot_lines = 0


async def find_address(args) -> str | None:
    if args.address:
        return args.address
    log(f"Keresés: '{DEVICE_NAME}' ...")
    dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=10.0)
    if dev is None:
        log("Nem található a FanController. Add meg --address-szel.")
        return None
    log(f"Megtalálva: {dev.address}")
    return dev.address


async def run_session(address: str, args, st: StressState, fh) -> str:
    """Egy kapcsolat-munkamenet. Visszaadja a véget okát:
    'done' | 'duration' | 'disconnect' | 'error'."""

    disconnected = asyncio.Event()
    auth_ok = False
    auth_evt = asyncio.Event()
    diag_done = asyncio.Event()
    diag_buf = bytearray()
    collecting = False

    def on_disconnect(_client):
        disconnected.set()

    def on_notify(_handle, data: bytearray):
        nonlocal auth_ok, collecting
        if data.startswith(b"AUTH_OK"):
            auth_ok = True
            auth_evt.set()
            return
        if data.startswith((b"AUTH_FAIL", b"AUTH_LOCKED", b"AUTH_REQUIRED")):
            auth_ok = False
            auth_evt.set()
            return
        if data == BEGIN:
            diag_buf.clear()
            collecting = True
            return
        if data == END:
            collecting = False
            diag_done.set()
            return
        if collecting:
            diag_buf.extend(data)

    client = BleakClient(address, disconnected_callback=on_disconnect)
    try:
        await client.connect()
    except Exception as e:
        log(f"Csatlakozás sikertelen: {e}", fh)
        return "error"

    async def send(cmd: bytes):
        await client.write_gatt_char(FFE1, cmd, response=True)

    async def fetch_diag() -> None:
        """DIAG? lekérés, és az új [boot]/[lowmem] sorok jelentése."""
        diag_done.clear()
        try:
            await send(b"DIAG?")
            await asyncio.wait_for(diag_done.wait(), timeout=8.0)
        except asyncio.TimeoutError:
            log("DIAG? timeout (lehet, hogy lefagyott).", fh)
            return
        text = diag_buf.decode("utf-8", "replace")
        boots = text.count("[boot]")
        lowmems = text.count("[lowmem]")
        if boots > st.last_boot_lines:
            log(f"*** ÚJ RESET a diag naplóban! [boot] sorok: "
                f"{st.last_boot_lines} -> {boots} ***", fh)
            # írjuk ki az utolsó boot sort
            for ln in text.splitlines():
                if "[boot]" in ln:
                    last_boot = ln
            log(f"    utolsó boot: {last_boot}", fh)
        if lowmems > st.lowmem_lines:
            log(f"*** ÚJ [lowmem] esemény! {st.lowmem_lines} -> {lowmems} ***", fh)
        st.boot_lines = boots
        st.last_boot_lines = boots
        st.lowmem_lines = lowmems

    try:
        await client.start_notify(FFE1, on_notify)

        # AUTH
        auth_evt.clear()
        await send(f"AUTH:{args.pin}".encode())
        try:
            await asyncio.wait_for(auth_evt.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log("AUTH timeout.", fh)
            return "error"
        if not auth_ok:
            log("AUTH sikertelen (rossz PIN / lezárva).", fh)
            return "error"
        log("AUTH OK — stressz indul.", fh)

        # kiindulási diag-állapot (hány reset volt eddig)
        if args.check_interval:
            await fetch_diag()
            log(f"Kiindulás: [boot]={st.boot_lines} [lowmem]={st.lowmem_lines}", fh)

        # rendszer bekapcsolása
        await send(b"ROLLER:1")
        log("-> ROLLER:1 (rendszer be)", fh)

        levels = [int(s) for s in args.levels.split(",") if s.strip() != ""]
        start = time.monotonic()
        last_check = time.monotonic()
        cycle = 0

        while not disconnected.is_set():
            if args.duration and (time.monotonic() - start) >= args.duration:
                log(f"Elérte a max futásidőt ({args.duration}s).", fh)
                return "duration"
            if args.cycles and cycle >= args.cycles:
                log(f"Elérte a ciklusszámot ({args.cycles}).", fh)
                return "done"

            for lvl in levels:
                if disconnected.is_set():
                    break
                await send(f"LEVEL:{lvl}".encode())
                st.switches += 1
                log(f"-> LEVEL:{lvl}  (#{st.switches})", fh)
                await asyncio.sleep(args.dwell)

                now = time.monotonic()
                if args.check_interval and (now - last_check) >= args.check_interval:
                    last_check = now
                    await fetch_diag()

            # opcionális ROLLER ki/be a ciklusok között (durvább induktív stressz)
            if args.roller_toggle and not disconnected.is_set():
                await send(b"ROLLER:0")
                log("-> ROLLER:0", fh)
                await asyncio.sleep(args.off_dwell)
                if not disconnected.is_set():
                    await send(b"ROLLER:1")
                    log("-> ROLLER:1", fh)

            cycle += 1

        return "disconnect"
    except Exception as e:
        log(f"Hiba a munkamenetben: {e}", fh)
        return "disconnect" if disconnected.is_set() else "error"
    finally:
        try:
            if client.is_connected:
                await send(b"LEVEL:0")
                await send(b"ROLLER:0")
                await client.stop_notify(FFE1)
        except Exception:
            pass
        try:
            await client.disconnect()
        except Exception:
            pass


async def main() -> int:
    ap = argparse.ArgumentParser(description="FanController stressz-teszt (fokozat-edzés) BLE-n")
    ap.add_argument("--pin", default="123456", help="BLE AUTH PIN (alapért: 123456)")
    ap.add_argument("--address", default=None, help="BLE MAC/UUID (ha nincs, név alapján keres)")
    ap.add_argument("--levels", default="0,1,2,3", help="fokozatok sorrendje (alapért: 0,1,2,3; 0 = ki)")
    ap.add_argument("--dwell", type=float, default=3.0, help="másodperc fokozatonként (alapért: 3)")
    ap.add_argument("--roller-toggle", action="store_true", help="ROLLER ki/be a ciklusok között (durvább stressz)")
    ap.add_argument("--off-dwell", type=float, default=1.0, help="OFF állapot hossza --roller-toggle esetén (alapért: 1)")
    ap.add_argument("--cycles", type=int, default=0, help="teljes ciklusok száma (0 = végtelen)")
    ap.add_argument("--duration", type=float, default=0, help="max futásidő mp-ben (0 = korlátlan)")
    ap.add_argument("--check-interval", type=float, default=0,
                    help="DIAG napló ellenőrzése N mp-enként reset/lowmem után (0 = ki)")
    ap.add_argument("--reconnect", action="store_true", help="kapcsolat-megszakadás után újracsatlakozás")
    ap.add_argument("--reconnect-wait", type=float, default=5.0, help="várakozás újracsatlakozás előtt (mp)")
    ap.add_argument("--log", default=None, help="napló fájl (append)")
    args = ap.parse_args()

    fh = open(args.log, "a", encoding="utf-8") if args.log else None
    st = StressState()
    run_start = time.monotonic()

    try:
        address = await find_address(args)
        if address is None:
            return 1

        while True:
            reason = await run_session(address, args, st, fh)

            if reason in ("done", "duration"):
                break

            if reason == "disconnect":
                elapsed = time.monotonic() - run_start
                log(f"!!! KAPCSOLAT MEGSZAKADT {elapsed:.0f}s ({elapsed/60:.1f} perc) után, "
                    f"{st.switches} fokozat-váltás közben — EZ a 'leállás' tünet. !!!", fh)
                if not args.reconnect:
                    break
                log(f"Újracsatlakozás {args.reconnect_wait}s múlva...", fh)
                await asyncio.sleep(args.reconnect_wait)
                continue

            if not args.reconnect:
                break
            await asyncio.sleep(args.reconnect_wait)

    finally:
        total = time.monotonic() - run_start
        log("=" * 50, fh)
        log("ÖSSZEGZÉS:", fh)
        log(f"  futásidő:         {total:.0f}s ({total/60:.1f} perc)", fh)
        log(f"  fokozat-váltások:  {st.switches}", fh)
        if args.check_interval:
            log(f"  diag [boot] sorok: {st.boot_lines} (reset-ek)", fh)
            log(f"  diag [lowmem]:     {st.lowmem_lines}", fh)
        log("Részletekért:  python3 diag_client.py", fh)
        log("=" * 50, fh)
        if fh:
            fh.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nMegszakítva (Ctrl+C).")
    finally:
        import sys
        if sys.platform == "win32":
            input("\nNyomj Entert az ablak bezárásához...")

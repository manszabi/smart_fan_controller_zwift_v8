#!/usr/bin/env python3
"""
diag_client.py — FanController diagnosztikai napló (/diag.log) lekérdezése BLE-n.

A firmware (v7.6.1+) a fan karakterisztikán (ffe1) szolgálja ki a diag parancsokat:
    AUTH:<pin>   -> "AUTH_OK" / "AUTH_FAIL" / "AUTH_LOCKED"
    DIAG?        -> 0x02"DIAG_BEGIN" <chunkok...> 0x04"DIAG_END"
    DIAGCLR      -> "DIAG_CLEARED"

A napló csak akkor tartalmaz bejegyzést, ha tényleg történt valami:
    [boot]   reason=BROWNOUT(11) heap=... min=...   -> hibás reset (pl. brownout)
    [lowmem] heap=... min=... t=...s                -> kevés szabad memória
    [sleep]  src=...                                 -> honnan indult a deep sleep
    [ota]    bad magic=0x.. size=...                 -> rossz/sérült firmware fájl (nem 0xE9)

Használat:
    pip install bleak
    python3 diag_client.py                  # PIN=123456, napló lekérése
    python3 diag_client.py --pin 123456
    python3 diag_client.py --address AA:BB:CC:DD:EE:FF
    python3 diag_client.py --clear          # lekérés után törli a naplót
"""

import argparse
import asyncio

from bleak import BleakClient, BleakScanner

DEVICE_NAME = "FanController"
FFE1 = "0000ffe1-0000-1000-8000-00805f9b34fb"

BEGIN = b"\x02DIAG_BEGIN"
END = b"\x04DIAG_END"


async def main() -> int:
    ap = argparse.ArgumentParser(description="FanController diag napló lekérdező")
    ap.add_argument("--pin", default="123456", help="BLE AUTH PIN (alapért: 123456)")
    ap.add_argument("--address", default=None, help="BLE MAC/UUID (ha nincs, név alapján keres)")
    ap.add_argument("--clear", action="store_true", help="napló törlése a lekérés után")
    ap.add_argument("--timeout", type=float, default=15.0, help="lekérés timeout (s)")
    args = ap.parse_args()

    address = args.address
    if address is None:
        print(f"Keresés: '{DEVICE_NAME}' ...")
        dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=10.0)
        if dev is None:
            print("Nem található a FanController. Add meg a --address kapcsolóval.")
            return 1
        address = dev.address
        print(f"Megtalálva: {address}")

    auth_evt = asyncio.Event()
    auth_ok = False
    done_evt = asyncio.Event()
    chunks = bytearray()
    collecting = False
    cleared_evt = asyncio.Event()

    def on_notify(_handle, data: bytearray):
        nonlocal auth_ok, collecting
        # AUTH válasz
        if data.startswith(b"AUTH_OK"):
            auth_ok = True
            auth_evt.set()
            return
        if data.startswith(b"AUTH_FAIL") or data.startswith(b"AUTH_LOCKED") or data.startswith(b"AUTH_REQUIRED"):
            auth_ok = False
            auth_evt.set()
            return
        if data.startswith(b"DIAG_CLEARED"):
            cleared_evt.set()
            return
        # Diag stream keretezés
        if data == BEGIN:
            chunks.clear()
            collecting = True
            return
        if data == END:
            collecting = False
            done_evt.set()
            return
        if collecting:
            chunks.extend(data)

    async with BleakClient(address) as client:
        await client.start_notify(FFE1, on_notify)

        # 1) AUTH
        await client.write_gatt_char(FFE1, f"AUTH:{args.pin}".encode(), response=True)
        try:
            await asyncio.wait_for(auth_evt.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            print("AUTH timeout (nincs válasz).")
            return 2
        if not auth_ok:
            print("AUTH sikertelen (rossz PIN vagy lezárva).")
            return 3

        # 2) DIAG? lekérés
        await client.write_gatt_char(FFE1, b"DIAG?", response=True)
        try:
            await asyncio.wait_for(done_evt.wait(), timeout=args.timeout)
        except asyncio.TimeoutError:
            print("DIAG lekérés timeout.")
            return 4

        text = chunks.decode("utf-8", "replace").strip()
        print("=" * 40)
        if text:
            print(text)
        else:
            print("(a napló üres — nem volt hibás reset / lowmem esemény)")
        print("=" * 40)

        # 3) opcionális törlés
        if args.clear:
            await client.write_gatt_char(FFE1, b"DIAGCLR", response=True)
            try:
                await asyncio.wait_for(cleared_evt.wait(), timeout=5.0)
                print("Napló törölve.")
            except asyncio.TimeoutError:
                print("Törlés visszaigazolás timeout.")

        await client.stop_notify(FFE1)
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


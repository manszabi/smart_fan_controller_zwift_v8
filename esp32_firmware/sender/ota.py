"""
BLE OTA kuldo (CRC-s, firmware >= 7.9.0) a FanController_OTA_debug-hoz.
Eredet: fbiego ESP32_BLE_OTA protokoll. Reszletek/valtozasok: sender/README.md.
Hasznalat: python ota.py "01:23:45:67:89:ab" "FanController_OTA_debug.ino.bin"
"""

from __future__ import print_function
from os import path
import asyncio
import math
import sys
import re
import zlib  # per-part CRC32 (zlib.crc32, egyezik a firmware crc32_zlib-jével)

from bleak import BleakClient, BleakScanner

header = """#####################################################################
    ------------------------BLE OTA update---------------------
    Arduino code @ https://github.com/fbiego/ESP32_BLE_OTA_Arduino
#####################################################################"""

UART_SERVICE_UUID = "fb1e4001-54ae-4a28-9f74-dfccb248601d"
UART_RX_CHAR_UUID = "fb1e4002-54ae-4a28-9f74-dfccb248601d"
UART_TX_CHAR_UUID = "fb1e4003-54ae-4a28-9f74-dfccb248601d"

PART = 16000
MTU = 100

DEBUG = False

ota_done = False
ota_success = False  # [FIX-20] a 0x0F eredmény "OTA done"-t jelzett-e (siker)
clt = None
fileBytes = None
total = 0

def get_bytes_from_file(filename):
    print("Reading from: ", filename)
    with open(filename, "rb") as f:
        return f.read()

async def scan_devices():
    print("Scanning for BLE devices (10s)...")
    devices = await BleakScanner.discover(timeout=10.0)
    if not devices:
        print("No BLE devices found.")
    else:
        print(f"Found {len(devices)} device(s):")
        for d in devices:
            print(f"  {d.address}  {d.name or '(no name)'}")
    print()

async def start_ota(ble_address: str, file_name: str):
    device = await BleakScanner.find_device_by_address(ble_address, timeout=20.0)
    disconnected_event = asyncio.Event()

    def handle_disconnect(_: BleakClient):
        print(": Device disconnected")
        disconnected_event.set()

    async def handle_rx(_: int, data: bytearray):
        if DEBUG:
            print(f"RX: {data.hex()}")

        if data[0] == 0xF1:
            nxt = int.from_bytes(bytearray([data[1], data[2]]), "big")
            await send_part(nxt, fileBytes, clt)
            printProgressBar(nxt + 1, total, prefix='Progress:', suffix='Complete', length=50)
            if nxt + 1 == total:
                await asyncio.sleep(0.5)

        if data[0] == 0xF2:
            print("Installing firmware")

        if data[0] == 0x0F:
            result = bytearray(data[1:])
            msg = str(result, 'utf-8')
            print("OTA result: ", msg)
            global ota_done, ota_success
            ota_success = ("OTA done" in msg) and ("FAILED" not in msg) and ("Not finished" not in msg)
            ota_done = True

    def printProgressBar(iteration, total, prefix='', suffix='', decimals=1, length=100, fill='█', printEnd="\r"):
        """
        Call in a loop to create terminal progress bar
        @params:
            iteration   - Required  : current iteration (Int)
            total       - Required  : total iterations (Int)
            prefix      - Optional  : prefix string (Str)
            suffix      - Optional  : suffix string (Str)
            decimals    - Optional  : positive number of decimals in percent complete (Int)
            length      - Optional  : character length of bar (Int)
            fill        - Optional  : bar fill character (Str)
            printEnd    - Optional  : end character (e.g. "\r", "\r\n") (Str)
        """
        percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
        filledLength = int(length * iteration // total)
        bar = fill * filledLength + '-' * (length - filledLength)
        print(f'\r{prefix} |{bar}| {percent}% {suffix}', end=printEnd)
        if iteration == total:
            print()

    async def send_part(position: int, data: bytearray, client: BleakClient):
        start = position * PART
        end_pos = (position + 1) * PART
        if len(data) < end_pos:
            end_pos = len(data)
        chunk_size = end_pos - start
        full_chunks = chunk_size // MTU
        for i in range(full_chunks):
            toSend = bytearray([0xFB, i])
            toSend += data[(position * PART) + (MTU * i):(position * PART) + (MTU * i) + MTU]
            await send_data(client, toSend, False)
        remainder = chunk_size % MTU
        if remainder != 0:
            toSend = bytearray([0xFB, full_chunks])
            toSend += data[(position * PART) + (MTU * full_chunks):(position * PART) + (MTU * full_chunks) + remainder]
            await send_data(client, toSend, False)
        part_data = data[start:end_pos]
        crc = zlib.crc32(part_data) & 0xFFFFFFFF
        update = bytearray([
            0xFC,
            (chunk_size >> 8) & 0xFF,
            chunk_size & 0xFF,
            (position >> 8) & 0xFF,
            position & 0xFF,
            (crc >> 24) & 0xFF,
            (crc >> 16) & 0xFF,
            (crc >> 8) & 0xFF,
            crc & 0xFF,
        ])
        await send_data(client, update, True)

    async def send_data(client: BleakClient, data: bytearray, response: bool):
        await client.write_gatt_char(UART_RX_CHAR_UUID, data, response)

    if not device:
        print("-----------Failed--------------")
        print(f"Device with address {ble_address} could not be found.")
        return

    async with BleakClient(device, disconnected_callback=handle_disconnect) as client:
        global fileBytes
        fileBytes = get_bytes_from_file(file_name)
        global clt
        clt = client
        fileParts = math.ceil(len(fileBytes) / PART)
        fileLen = len(fileBytes)
        global total
        total = fileParts

        await client.start_notify(UART_TX_CHAR_UUID, handle_rx)

        await asyncio.sleep(2.0)

        await send_data(client, bytearray([0xFD]), True)

        fileSize = bytearray([
            0xFE,
            (fileLen >> 24) & 0xFF,
            (fileLen >> 16) & 0xFF,
            (fileLen >> 8) & 0xFF,
            fileLen & 0xFF,
        ])
        await send_data(client, fileSize, False)
        otaInfo = bytearray([0xFF, fileParts >> 8, fileParts & 0xFF, MTU >> 8, MTU & 0xFF])
        await send_data(client, otaInfo, False)

        while not ota_done:
            await asyncio.sleep(1.0)

        if ota_success:
            print("Waiting for reboot/disconnect... ", end="", flush=True)
            try:
                await asyncio.wait_for(disconnected_event.wait(), timeout=20.0)
                print("\n-----------Complete--------------")
            except asyncio.TimeoutError:
                print("\n(timeout: nincs disconnect 20s alatt — ellenőrizd az eszközt)")
        else:
            print("-----------OTA FAILED — lásd a fenti 'OTA result'-ot--------------")

def isValidAddress(address):
    mac_regex = re.compile(r"^([0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}$")
    uuid_regex = re.compile(r"^[{]?[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}[}]?$")

    if address is None:
        return False

    return bool(mac_regex.match(address)) or bool(uuid_regex.match(address))

if __name__ == "__main__":
    print(header)
    try:
        if len(sys.argv) > 2:
            print("Trying to start OTA update")
            if isValidAddress(sys.argv[1]) and path.exists(sys.argv[2]):
                if DEBUG:
                    asyncio.run(scan_devices())
                asyncio.run(start_ota(sys.argv[1], sys.argv[2]))
            else:
                if not isValidAddress(sys.argv[1]):
                    print("Invalid Address: ", sys.argv[1])
                if not path.exists(sys.argv[2]):
                    print("File not found: ", sys.argv[2])
        else:
            print("Specify the device address and firmware file")
            print(">python ota.py \"01:23:45:67:89:ab\" \"firmware.bin\"")
    finally:
        try:
            input("\nNyomj ENTER-t az ablak bezárásához...")
        except (EOFError, KeyboardInterrupt):
            pass

#!/usr/bin/env python3
"""
OTA diagnostic utility for FanController.
Analyzes firmware.bin files and checks compatibility with partition tables.
"""

import sys
import struct
import os

def analyze_firmware(bin_path):
    """Analyze firmware.bin file header and basic info."""
    if not os.path.exists(bin_path):
        print(f"Error: {bin_path} not found")
        return False

    try:
        size = os.path.getsize(bin_path)
        print(f"Firmware file: {bin_path}")
        print(f"File size: {size} bytes (0x{size:X})")

        # Read magic number (first 4 bytes should be 0xE9 for ESP32)
        with open(bin_path, 'rb') as f:
            magic = f.read(1)

        if magic[0] == 0xE9:
            print("✓ Valid ESP32 app image magic (0xE9) — OTA-ra alkalmas")
        else:
            print(f"✗ HIBÁS magic byte: 0x{magic[0]:02X} (0xE9 kellene)")
            print("  → Ez okozza a 'Decryption error'-t az eszközön!")
            print("  → Rossz fájlt töltesz fel. A helyes az app '*.ino.bin'.")
            if magic[0] == 0x1F:
                print("  → 0x1F: ez egy GZIP tömörített fájl, nem nyers .bin.")
            return False

        return True
    except Exception as e:
        print(f"Error analyzing firmware: {e}")
        return False

def check_partition_table():
    """Check partition table against app partition size."""
    # A partíciós tábla a szkript mellett van – ne beégetett abszolút útvonalon
    PARTITION_FILE = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "partitions_custom.csv"
    )

    if not os.path.exists(PARTITION_FILE):
        print(f"Partition file not found: {PARTITION_FILE}")
        return

    print("\n=== Partition Table Analysis ===")
    with open(PARTITION_FILE, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith('#') or not line:
                continue
            parts = [p.strip() for p in line.split(',')]
            if len(parts) >= 5:
                name, typ, subtype, offset, size = parts[0], parts[1], parts[2], parts[3], parts[4]
                if typ == 'app':
                    offset_dec = int(offset, 16)
                    size_dec = int(size, 16)
                    print(f"{name:8} @ 0x{offset} (0x{size} = {size_dec:,} bytes)")

def main():
    print("=== FanController OTA Diagnostic ===\n")

    if len(sys.argv) > 1:
        firmware_path = sys.argv[1]
    else:
        # Try to find firmware in Arduino build directories
        print("Usage: python3 ota_diagnostic.py <firmware.bin>")
        print("\nOr provide the full path to the compiled firmware.bin from:")
        print("  ~/.arduino15/packages/esp32/hardware/esp32/*/tools/...")
        print("  or your Arduino project build directory")
        return

    analyze_firmware(firmware_path)
    check_partition_table()

if __name__ == "__main__":
    main()

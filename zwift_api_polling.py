"""zwift_api_polling.py – vékony belépő a Zwift API polling segédprocesszhez.

A tényleges implementáció a ``smart_fan_controller.zwift_api`` csomagba került
(decoder / api / runtime / logsetup / __main__). Ez a fájl megőrzi a közvetlen
futtatás és a PyInstaller entry-point kompatibilitását.

Konfiguráció: a settings.json ``zwift_api`` szekciója (a fő apppal közös fájl).
A fő app (FanController) a ``--settings <path>`` paraméterrel indítja.

Futtatás önállóan:
    python zwift_api_polling.py --settings settings.json
"""
from __future__ import annotations

import sys

from smart_fan_controller.zwift_api.__main__ import main

if __name__ == "__main__":
    sys.exit(main())

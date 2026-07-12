#!/usr/bin/env python3
"""LCARS hangeffekt WAV fájlok generálása a smart_fan_controller/sounds/ mappába.

A HUD hangjai fájl alapúak (smart_fan_controller/sounds/*.wav). Ez a szkript
a hangokat az eredeti, szintetizált tone-definíciókból állítja elő – a
kimenet bitre azonos a korábban futásidőben generált hangokkal.

Futtatás a projekt gyökeréből:
    python tools/generate_lcars_sounds.py

Egy meglévő fájlt csak a --force kapcsolóval ír felül, így a kézzel
lecserélt (egyedi) hangfájlok nem vesznek el véletlenül.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# A projekt gyökere kerüljön a sys.path-ra (bárhonnan futtatható legyen)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smart_fan_controller.core.helpers import generate_tone

# Hang definíciók: (frekvencia_hz, időtartam_sec, amplitúdó)
SOUND_DEFS: dict[str, list[tuple[float, float, float]]] = {
    # Zónaváltás hangok – jellegzetes LCARS csippanások
    "zone_up": [(880, 0.08, 1.0), (1320, 0.12, 0.8)],       # felfelé lépés
    "zone_down": [(1320, 0.08, 0.8), (880, 0.12, 1.0)],     # lefelé lépés
    "zone_standby": [(440, 0.15, 0.5), (330, 0.2, 0.4)],    # standby-ba lépés
    # Szenzor események
    "sensor_dropout": [                                        # vészjelzés – hármas csipogás
        (1760, 0.06, 1.0), (0, 0.04, 0.0),
        (1760, 0.06, 1.0), (0, 0.04, 0.0),
        (1760, 0.06, 1.0),
    ],
    "sensor_reconnect": [                                      # visszacsatlakozás – emelkedő
        (660, 0.08, 0.7), (880, 0.08, 0.8), (1100, 0.12, 1.0),
    ],
    # Zwift
    "zwift_connect": [                                         # comm channel nyitás
        (440, 0.06, 0.6), (660, 0.06, 0.7), (880, 0.06, 0.8),
        (1100, 0.15, 1.0),
    ],
    "zwift_disconnect": [                                      # comm channel zárás
        (1100, 0.06, 0.8), (880, 0.06, 0.7), (660, 0.06, 0.6),
        (440, 0.15, 0.5),
    ],
    # Fan sebesség – rövid visszajelzés
    "fan_tx": [(1047, 0.05, 0.5), (1319, 0.07, 0.6)],       # parancs elküldve
    # HUD indítás – tricorder kinyitás hangeffekt
    "hud_startup": [
        (1200, 0.06, 0.3), (1500, 0.06, 0.4), (1800, 0.06, 0.5),
        (2200, 0.08, 0.6), (2600, 0.10, 0.7), (3000, 0.08, 0.8),
        (2400, 0.06, 0.5), (2800, 0.06, 0.6), (3200, 0.12, 0.9),
        (2000, 0.15, 0.4),
    ],
    # HUD bezárás – tricorder becsukás hangeffekt (fordított söprés lefelé)
    "hud_shutdown": [
        (2000, 0.06, 0.4), (3200, 0.06, 0.6), (2800, 0.06, 0.5),
        (2400, 0.08, 0.7), (3000, 0.08, 0.8), (2600, 0.06, 0.6),
        (2200, 0.06, 0.5), (1800, 0.06, 0.5), (1500, 0.06, 0.4),
        (1200, 0.10, 0.3), (800, 0.15, 0.2),
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true",
        help="meglévő .wav fájlok felülírása is",
    )
    args = parser.parse_args()

    out_dir = (
        Path(__file__).resolve().parents[1] / "smart_fan_controller" / "sounds"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    written = skipped = 0
    for name, tones in SOUND_DEFS.items():
        path = out_dir / f"{name}.wav"
        if path.exists() and not args.force:
            print(f"  kihagyva (létezik): {path.name}")
            skipped += 1
            continue
        path.write_bytes(generate_tone(tones))
        dur = sum(d for _f, d, _a in tones)
        print(f"  generálva: {path.name} ({dur:.2f} s)")
        written += 1

    print(f"Kész: {written} fájl írva, {skipped} kihagyva → {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

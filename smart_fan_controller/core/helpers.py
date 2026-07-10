"""Segédfüggvények – tiszta logika, nincs Qt/BLE/IO függőség.

Path validáció, audio generálás, és egyéb utilitások az alkalmazás
számára. Ezek a függvények nincs mellékhatásuk és egymástól függetlenek.
"""
from __future__ import annotations

import io
import logging
import math
import os
import struct
import wave

user_logger = logging.getLogger("user")


def resolve_log_dir(
    log_directory: str | None, default_dir: str | None = None
) -> str:
    """Log könyvtár meghatározása és validálása.

    Ha ``log_directory`` None, üres, vagy nem létezik / nem hozható létre,
    a ``default_dir`` fallback-et használja. Ha ``default_dir`` None,
    az aktuális munkakönyvtárat (CWD) használja.

    A fő alkalmazás a saját modul-könyvtárát adja át ``default_dir``-ként,
    hogy a logok a script mellé kerüljenek (a refaktor előtti viselkedés),
    nem pedig az indítási munkakönyvtárba.

    Args:
        log_directory: Kérésezett log könyvtár elérési útja, vagy None.
        default_dir: Fallback könyvtár (None = aktuális munkakönyvtár).

    Returns:
        Érvényes, írható könyvtár elérési útja.
    """
    if default_dir is None:
        default_dir = os.getcwd()

    if not log_directory:
        return default_dir

    log_directory = os.path.expanduser(log_directory)
    log_directory = os.path.abspath(log_directory)

    try:
        os.makedirs(log_directory, exist_ok=True)
        # Írhatóság tesztelése
        test_file = os.path.join(log_directory, ".log_write_test")
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        return log_directory
    except OSError:
        # Nem sikerült létrehozni / írni – fallback
        user_logger.warning(
            f"⚠ log_directory nem elérhető: '{log_directory}', "
            f"alapértelmezett használata: '{default_dir}'"
        )
        return default_dir


def generate_tone(
    frequencies: list[tuple[float, float, float]],
    sample_rate: int = 22050,
    volume: float = 0.4,
) -> bytes:
    """Szinuszhullám-alapú WAV generálás memóriában.

    Args:
        frequencies: Lista (freq_hz, duration_sec, amplitude_mult) tuple-ökből.
                     Több elem esetén egymás után fűzi a hangokat.
        sample_rate: Mintavételezési ráta (Hz).
        volume: Hangerő szorzó (0.0–1.0).

    Returns:
        WAV audio adat byte-okban (memóriában, lejátszáshoz kész).

    Example:
        >>> wav_data = generate_tone([(440, 0.5, 1.0), (880, 0.5, 0.5)])
        >>> len(wav_data) > 0
        True
    """
    samples: list[int] = []
    for freq, duration, amp in frequencies:
        n_samples = int(sample_rate * duration)
        for i in range(n_samples):
            t = i / sample_rate
            # Fade in/out az audió kattanás elkerülésére
            fade_samples = min(200, n_samples // 4)
            fade = 1.0
            if fade_samples > 0:
                if i < fade_samples:
                    fade = i / fade_samples
                elif i > n_samples - fade_samples:
                    fade = (n_samples - i) / fade_samples
            val = math.sin(2 * math.pi * freq * t) * volume * amp * fade
            samples.append(int(val * 32767))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buf.getvalue()

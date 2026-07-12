"""Helper functions – pure logic, no Qt/BLE/IO dependencies.

Path validation, audio generation and other utilities for the
application. These functions are side-effect free and independent of
each other.
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
    """Determine and validate the log directory.

    When ``log_directory`` is None, empty, or cannot be created/written,
    the ``default_dir`` fallback is used. When ``default_dir`` is None,
    the current working directory (CWD) is used.

    The main application passes its own module directory as
    ``default_dir`` so the logs land next to the script (pre-refactor
    behavior) instead of the launch working directory.

    Args:
        log_directory: Requested log directory path, or None.
        default_dir: Fallback directory (None = current working dir).

    Returns:
        A valid, writable directory path.
    """
    if default_dir is None:
        default_dir = os.getcwd()

    if not log_directory:
        return default_dir

    log_directory = os.path.expanduser(log_directory)
    log_directory = os.path.abspath(log_directory)

    try:
        os.makedirs(log_directory, exist_ok=True)
        # Writability test
        test_file = os.path.join(log_directory, ".log_write_test")
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        return log_directory
    except OSError:
        # Could not create / write – fall back
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
    """Sine-wave based WAV generation in memory.

    Args:
        frequencies: List of (freq_hz, duration_sec, amplitude_mult)
                     tuples. Multiple items are concatenated in order.
        sample_rate: Sampling rate (Hz).
        volume: Volume multiplier (0.0–1.0).

    Returns:
        WAV audio data in bytes (in memory, ready for playback).

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
            # Fade in/out to avoid audio clicks
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

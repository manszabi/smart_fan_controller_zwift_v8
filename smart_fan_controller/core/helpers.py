"""Helper functions – pure logic, no Qt/BLE/IO dependencies.

Path validation, audio generation and other utilities for the
application. These functions are side-effect free and independent of
each other.
"""
from __future__ import annotations

import array
import io
import logging
import math
import os
import sys
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
        # Removing the probe is best effort: a failed cleanup (e.g. a
        # locking AV scanner) does not mean the directory is unwritable –
        # it must not send us to the fallback
        try:
            os.remove(test_file)
        except OSError:
            pass
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

    Samples are accumulated in an ``array("h")`` rather than a Python
    list: the list held a boxed int object per sample (~32 bytes each,
    against 2 bytes in the array), and ``struct.pack(f"<{n}h", *samples)``
    then had to unpack the whole thing onto the argument stack in one
    call. For a one-second effect that meant tens of thousands of
    arguments and about a megabyte of transient objects for 44 KB of
    audio.

    Values are clamped to the signed 16-bit range. ``volume * amp`` above
    1.0 used to overflow it and take the call down with a ``struct.error``
    mid-generation; clipping the peaks is the normal audio behavior and
    keeps the caller's tone definition usable.

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
    samples = array.array("h")
    sin = math.sin
    for freq, duration, amp in frequencies:
        n_samples = int(sample_rate * duration)
        # Loop invariants hoisted out: the fade length and the angular
        # frequency do not depend on the sample index. The multiplication
        # ORDER of the original expression is kept exactly as it was
        # (folding volume * amp together would shift the last ULP), so the
        # generated audio stays bit-for-bit identical to the shipped WAVs.
        fade_samples = min(200, n_samples // 4)
        omega = 2 * math.pi * freq
        for i in range(n_samples):
            t = i / sample_rate
            # Fade in/out to avoid audio clicks
            fade = 1.0
            if fade_samples > 0:
                if i < fade_samples:
                    fade = i / fade_samples
                elif i > n_samples - fade_samples:
                    fade = (n_samples - i) / fade_samples
            val = sin(omega * t) * volume * amp * fade
            samples.append(_clamp_int16(int(val * 32767)))

    if sys.byteorder == "big":
        # WAV PCM is little-endian; array.tobytes() uses the host order
        samples.byteswap()

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())
    return buf.getvalue()


def _clamp_int16(value: int) -> int:
    """Clamp to the signed 16-bit PCM range."""
    if value > 32767:
        return 32767
    if value < -32768:
        return -32768
    return value

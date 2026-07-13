"""File-based Star Trek LCARS sound effect playback for the HUD.

Sounds are loaded from the package ``sounds/`` directory
(``smart_fan_controller/sounds/<name>.wav``). A missing or broken file is
never fatal: the affected effect is muted and a log warning states the
exact path that is missing, so the file can be supplied later. The stock
sounds can be regenerated with ``tools/generate_lcars_sounds.py`` or
replaced with custom uncompressed PCM WAV files (the only format
QSoundEffect supports).
"""

from __future__ import annotations

import logging
import os
import sys
import wave

from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QSoundEffect

logger = logging.getLogger("zwift_fan_controller_new")


class LCARSSoundManager:
    """Loads and plays the LCARS sound effects via QSoundEffect."""

    # Expected effect file names (<name>.wav inside the sounds/ directory)
    SOUND_NAMES: tuple[str, ...] = (
        "zone_up",          # zone change upwards
        "zone_down",        # zone change downwards
        "zone_standby",     # entering standby
        "sensor_dropout",   # sensor signal lost
        "sensor_reconnect",  # sensor signal restored
        "zwift_connect",    # Zwift data arriving
        "zwift_disconnect",  # Zwift signal lost
        "fan_tx",           # fan command sent
        "hud_startup",      # HUD opening
        "hud_shutdown",     # HUD closing
    )

    def __init__(self) -> None:
        self._effects: dict[str, QSoundEffect] = {}
        self._durations_ms: dict[str, int] = {}
        self._enabled = True
        self._volume = 0.5
        self._cleaned_up = False
        self._load_all()

    @staticmethod
    def sounds_dir() -> str:
        """Directory of the sound files (source checkout and PyInstaller).

        Search order (same logic as the fonts/ directory):
          1. <package_dir>/sounds                     (smart_fan_controller/sounds)
          2. <exe_dir>/smart_fan_controller/sounds    (PyInstaller frozen)
        """
        if getattr(sys, "frozen", False):
            base_dir = os.path.join(
                os.path.dirname(os.path.abspath(sys.executable)),
                "smart_fan_controller",
            )
        else:
            # sound.py lives in smart_fan_controller/ui/ → package root is one up
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base_dir, "sounds")

    def _load_all(self) -> None:
        """Load every effect from the sounds/ directory.

        A missing file is not an error: the effect stays muted and the log
        states exactly which file to place where.
        """
        snd_dir = self.sounds_dir()
        missing: list[str] = []
        for name in self.SOUND_NAMES:
            wav_path = os.path.join(snd_dir, f"{name}.wav")
            if not os.path.isfile(wav_path):
                missing.append(wav_path)
                continue
            try:
                # Duration from the WAV header (needed to await the
                # shutdown sound before the window really closes)
                with wave.open(wav_path, "rb") as wf:
                    rate = wf.getframerate()
                    self._durations_ms[name] = (
                        int(wf.getnframes() * 1000 / rate) if rate > 0 else 0
                    )
                effect = QSoundEffect()
                effect.setSource(QUrl.fromLocalFile(wav_path))
                effect.setVolume(self._volume)
                self._effects[name] = effect
            except Exception as exc:
                logger.warning(
                    "LCARS hangfájl betöltése sikertelen: %s (%s) – "
                    "a(z) '%s' hangeffekt némítva.", wav_path, exc, name
                )
        for path in missing:
            logger.warning(
                "LCARS hangfájl hiányzik: %s – a hangeffekt némítva; "
                "pótolható a fájl elhelyezésével, vagy újragenerálható: "
                "python tools/generate_lcars_sounds.py", path
            )
        if missing and not self._effects:
            logger.warning(
                "Egyetlen LCARS hangfájl sem található a %s mappában – "
                "a HUD hang nélkül fut.", snd_dir
            )

    def play(self, name: str) -> None:
        """Play an effect by name (silent no-op for missing sounds)."""
        if not self._enabled:
            return
        effect = self._effects.get(name)
        if effect is not None:
            effect.play()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def volume(self) -> float:
        return self._volume

    def sound_duration_ms(self, name: str) -> int:
        """Duration of an effect in milliseconds (0 when not loaded)."""
        return self._durations_ms.get(name, 0) if name in self._effects else 0

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable all sound effects."""
        self._enabled = enabled

    def set_volume(self, volume: float) -> None:
        """Set the volume (0.0–1.0) of every effect."""
        self._volume = volume
        for effect in self._effects.values():
            effect.setVolume(volume)

    def cleanup(self) -> None:
        """Stop and release every effect."""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        for effect in self._effects.values():
            effect.stop()
        self._effects.clear()

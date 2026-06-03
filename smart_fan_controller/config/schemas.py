"""Típusbiztos beállítás modellek – enumok és dataclass-ek.

Ez a modul tartalmazza a ``settings.json`` szekcióit leképező, validált
dataclass-okat (``PowerZonesConfig``, ``GlobalSettingsConfig`` stb.), valamint
a hozzájuk tartozó enumokat (``DataSource``, ``ZoneMode``).

A betöltés/mentés logika a testvér ``loader`` modulban található.
"""
from __future__ import annotations

import dataclasses
import enum
from typing import Any, Dict, Optional

import logging

# A felhasználói üzeneteket a "user" nevű logger kezeli; a logging konfigurációt
# a fő alkalmazás állítja be (_setup_logging). Itt csak a már létező loggerre
# hivatkozunk – névmegegyezés miatt ugyanaz a példány.
user_logger = logging.getLogger("user")


# ============================================================
# ENUM-OK
# ============================================================

# --- Enum-ok a magic string-ek kiváltásához ---
# str öröklés: JSON-ból jövő string értékekkel is kompatibilis (==)
class DataSource(str, enum.Enum):
    ANTPLUS = "antplus"
    BLE = "ble"
    ZWIFTUDP = "zwiftudp"


class ZoneMode(str, enum.Enum):
    POWER_ONLY = "power_only"
    HR_ONLY = "hr_only"
    HIGHER_WINS = "higher_wins"


VALID_DATA_SOURCES: tuple[DataSource, ...] = tuple(DataSource)
VALID_ZONE_MODES: tuple[ZoneMode, ...] = tuple(ZoneMode)


# ============================================================
# TÍPUSBIZTOS BEÁLLÍTÁS MODELLEK
# ============================================================


@dataclasses.dataclass
class PowerZonesConfig:
    """Teljesítmény zóna beállítások – típusbiztos, validált.

    Validáció a __post_init__-ben:
      - min_watt < max_watt (felcserélés ha szükséges)
      - z1_max_percent < z2_max_percent (rendezés ha szükséges)
    """

    ftp: int = 200
    min_watt: int = 0
    max_watt: int = 1000
    z1_max_percent: int = 60
    z2_max_percent: int = 89
    zero_power_immediate: bool = False

    def __post_init__(self) -> None:
        # --- ftp tartomány-check: 0–1000 ---
        if self.ftp < 0 or self.ftp > 1000:
            user_logger.warning(
                f"⚠ Érvénytelen 'ftp' érték: {self.ftp} (0–1000 közötti kell). "
                f"Javítva: default {PowerZonesConfig.__dataclass_fields__['ftp'].default}-ra."
            )
            self.ftp = PowerZonesConfig.__dataclass_fields__['ftp'].default

        # --- min_watt tartomány-check: 0–1000 ---
        if self.min_watt < 0 or self.min_watt > 1000:
            user_logger.warning(
                f"⚠ Érvénytelen 'min_watt' érték: {self.min_watt} (0–1000 közötti kell). "
                f"Javítva: 0-ra."
            )
            self.min_watt = 0

        # --- max_watt tartomány-check: 0–1000 ---
        if self.max_watt < 0 or self.max_watt > 1000:
            user_logger.warning(
                f"⚠ Érvénytelen 'max_watt' érték: {self.max_watt} (0–1000 közötti kell). "
                f"Javítva: 1000-re."
            )
            self.max_watt = 1000

        # --- min_watt < max_watt logikai check ---
        if self.min_watt >= self.max_watt:
            default_min = PowerZonesConfig.__dataclass_fields__['min_watt'].default
            default_max = PowerZonesConfig.__dataclass_fields__['max_watt'].default
            user_logger.warning(
                f"⚠ Érvénytelen watt tartomány: min_watt ({self.min_watt}) >= max_watt ({self.max_watt}). "
                f"Alapértelmezésre állítva: min_watt={default_min}, max_watt={default_max}."
            )
            self.min_watt = default_min
            self.max_watt = default_max

        # --- z1_max_percent / z2_max_percent tartomány-check: 1–100 ---
        if self.z1_max_percent < 1 or self.z1_max_percent > 100:
            default_z1 = PowerZonesConfig.__dataclass_fields__['z1_max_percent'].default
            user_logger.warning(
                f"⚠ Érvénytelen 'z1_max_percent' érték: {self.z1_max_percent} (1–100 közötti kell). "
                f"Javítva: default {default_z1}-re."
            )
            self.z1_max_percent = default_z1

        if self.z2_max_percent < 1 or self.z2_max_percent > 100:
            default_z2 = PowerZonesConfig.__dataclass_fields__['z2_max_percent'].default
            user_logger.warning(
                f"⚠ Érvénytelen 'z2_max_percent' érték: {self.z2_max_percent} (1–100 közötti kell). "
                f"Javítva: default {default_z2}-re."
            )
            self.z2_max_percent = default_z2

        # --- z1_max_percent < z2_max_percent logikai check ---
        if self.z1_max_percent >= self.z2_max_percent:
            default_z1 = PowerZonesConfig.__dataclass_fields__['z1_max_percent'].default
            default_z2 = PowerZonesConfig.__dataclass_fields__['z2_max_percent'].default
            user_logger.warning(
                f"⚠ Érvénytelen zóna százalékok: z1_max_percent ({self.z1_max_percent}) >= z2_max_percent ({self.z2_max_percent}). "
                f"Alapértelmezésre állítva: z1_max_percent={default_z1}, z2_max_percent={default_z2}."
            )
            self.z1_max_percent = default_z1
            self.z2_max_percent = default_z2

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PowerZonesConfig":
        """Dict-ből (JSON) hoz létre validált PowerZonesConfig példányt.

        Érvénytelen értékeket figyelmen kívül hagyja (az alapértelmezés marad).

        Args:
            raw: A JSON-ból betöltött dict.
        """
        d = cls()
        ftp = d.ftp
        min_watt = d.min_watt
        max_watt = d.max_watt
        z1 = d.z1_max_percent
        z2 = d.z2_max_percent
        zpi = d.zero_power_immediate

        if "ftp" in raw:
            v = raw["ftp"]
            if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 1000:
                ftp = v
            else:
                user_logger.warning(f"⚠ Érvénytelen 'ftp' érték: {v!r} (0–1000 közötti egész kell, default: {d.ftp})")

        if "min_watt" in raw:
            v = raw["min_watt"]
            if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 1000:
                min_watt = v
            else:
                user_logger.warning(f"⚠ Érvénytelen 'min_watt' érték: {v!r} (0–1000 közötti egész kell, default: {d.min_watt})")

        if "max_watt" in raw:
            v = raw["max_watt"]
            if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 1000:
                max_watt = v
            else:
                user_logger.warning(f"⚠ Érvénytelen 'max_watt' érték: {v!r} (0–1000 közötti egész kell, default: {d.max_watt})")

        if "z1_max_percent" in raw:
            v = raw["z1_max_percent"]
            if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 100:
                z1 = v
            else:
                user_logger.warning(f"⚠ Érvénytelen 'z1_max_percent' érték: {v!r} (1–100 közötti egész kell)")

        if "z2_max_percent" in raw:
            v = raw["z2_max_percent"]
            if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 100:
                z2 = v
            else:
                user_logger.warning(f"⚠ Érvénytelen 'z2_max_percent' érték: {v!r} (1–100 közötti egész kell)")

        if "zero_power_immediate" in raw:
            v = raw["zero_power_immediate"]
            if isinstance(v, bool):
                zpi = v
            else:
                user_logger.warning(f"⚠ Érvénytelen 'zero_power_immediate' érték: {v!r} (true/false kell)")

        return cls(ftp=ftp, min_watt=min_watt, max_watt=max_watt,
                   z1_max_percent=z1, z2_max_percent=z2, zero_power_immediate=zpi)

    def to_dict(self) -> Dict[str, Any]:
        """Visszaadja dict formában (JSON serializáláshoz)."""
        return dataclasses.asdict(self)


@dataclasses.dataclass
class GlobalSettingsConfig:
    """Globális beállítások – típusbiztos, validált.

    Validáció a __post_init__-ben:
      - minimum_samples <= buffer_seconds * buffer_rate_hz (kereszt-validáció)
    """

    cooldown_seconds: int = 120
    buffer_seconds: int = 3
    minimum_samples: int = 6
    buffer_rate_hz: int = 4
    dropout_timeout: int = 5
    logging: bool = True
    log_directory: Optional[str] = None

    def __post_init__(self) -> None:
        # minimum_samples <= buffer_seconds * buffer_rate_hz cross-validation
        if self.buffer_seconds > 0 and self.buffer_rate_hz > 0:
            max_samples = self.buffer_seconds * self.buffer_rate_hz
            if self.minimum_samples > max_samples:
                user_logger.warning(
                    f"⚠ Érvénytelen minimum_samples ({self.minimum_samples}) – "
                    f"nagyobb, mint buffer_seconds * buffer_rate_hz "
                    f"({self.buffer_seconds} * {self.buffer_rate_hz} = {max_samples}). "
                    f"minimum_samples {max_samples}-re állítva."
                )
                self.minimum_samples = max_samples

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GlobalSettingsConfig":
        d = cls()
        kwargs: dict[str, Any] = dataclasses.asdict(d)

        # cooldown_seconds: 0–600 (0 = azonnali váltás, nincs cooldown)
        _from_dict_int(raw, kwargs, "cooldown_seconds", 0, 600)
        # Domain-alapú tartományok (fitness szenzor jellemzők szerint)
        _from_dict_int(raw, kwargs, "buffer_seconds", 1, 60)
        _from_dict_int(raw, kwargs, "minimum_samples", 1, 600)
        _from_dict_int(raw, kwargs, "buffer_rate_hz", 1, 60)
        # dropout_timeout: 1–300 (egységes a forrás-specifikus *_dropout_timeout-tal)
        _from_dict_int(raw, kwargs, "dropout_timeout", 1, 300)

        # logging: bool – globális loggolás be/ki
        _from_dict_bool(raw, kwargs, "logging")

        # log_directory: null vagy nem-üres string.
        # null, "null" string, vagy hiányzó kulcs → None (program könyvtár), csendben.
        if "log_directory" in raw:
            ld = raw["log_directory"]
            if ld is None:
                kwargs["log_directory"] = None
            elif isinstance(ld, str):
                stripped = ld.strip()
                if stripped.lower() == "null":
                    # "null" string → None (program könyvtár), csendben
                    kwargs["log_directory"] = None
                elif not stripped:
                    user_logger.warning(
                        "⚠ Üres 'log_directory' érték – alapértelmezett "
                        "(program könyvtár) használata."
                    )
                else:
                    kwargs["log_directory"] = stripped
            else:
                user_logger.warning(
                    f"⚠ Érvénytelen 'log_directory' érték: {ld!r} "
                    f"(string vagy null kell) – alapértelmezett "
                    f"(program könyvtár) használata."
                )

        return cls(**kwargs)


@dataclasses.dataclass
class HeartRateZonesConfig:
    """Szívfrekvencia zóna beállítások – típusbiztos, validált.

    Validáció a __post_init__-ben:
      - z1_max_percent < z2_max_percent (érvénytelen sorrend → default visszaállítás)
      - resting_hr < max_hr
      - valid_min_hr < valid_max_hr
    """

    enabled: bool = True
    max_hr: int = 185
    resting_hr: int = 60
    zone_mode: str = ZoneMode.HIGHER_WINS
    z1_max_percent: int = 70
    z2_max_percent: int = 80
    valid_min_hr: int = 30
    valid_max_hr: int = 220
    zero_hr_immediate: bool = False

    def __post_init__(self) -> None:
        # z1_max_percent < z2_max_percent logikai check
        # (PowerZonesConfig-gal konzisztens: érvénytelen sorrend → default visszaállítás)
        if self.z1_max_percent >= self.z2_max_percent:
            default_z1 = HeartRateZonesConfig.__dataclass_fields__["z1_max_percent"].default
            default_z2 = HeartRateZonesConfig.__dataclass_fields__["z2_max_percent"].default
            user_logger.warning(
                f"⚠ Érvénytelen HR zóna százalékok: z1_max_percent ({self.z1_max_percent}) >= z2_max_percent ({self.z2_max_percent}). "
                f"Alapértelmezésre állítva: z1_max_percent={default_z1}, z2_max_percent={default_z2}."
            )
            self.z1_max_percent = default_z1
            self.z2_max_percent = default_z2

        # resting_hr < max_hr
        if self.resting_hr >= self.max_hr:
            new_rest = max(30, self.max_hr - 1)
            user_logger.warning(
                f"⚠ Érvénytelen HR értékek (resting_hr={self.resting_hr}, max_hr={self.max_hr}). "
                f"resting_hr {new_rest}-re állítva."
            )
            self.resting_hr = new_rest

        # valid_min_hr < valid_max_hr
        if self.valid_min_hr >= self.valid_max_hr:
            user_logger.warning(
                f"⚠ valid_min_hr ({self.valid_min_hr}) >= valid_max_hr ({self.valid_max_hr}), "
                f"alapértelmezés visszaállítva."
            )
            defaults = HeartRateZonesConfig.__dataclass_fields__
            self.valid_min_hr = defaults["valid_min_hr"].default
            self.valid_max_hr = defaults["valid_max_hr"].default

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "HeartRateZonesConfig":
        d = cls()
        kwargs: dict[str, Any] = dataclasses.asdict(d)

        # Int fields with ranges
        _from_dict_int(raw, kwargs, "max_hr", 100, 220)
        _from_dict_int(raw, kwargs, "resting_hr", 30, 100)
        _from_dict_int(raw, kwargs, "z1_max_percent", 1, 100)
        _from_dict_int(raw, kwargs, "z2_max_percent", 1, 100)
        _from_dict_int(raw, kwargs, "valid_min_hr", 30, 100)
        _from_dict_int(raw, kwargs, "valid_max_hr", 150, 300)

        # Bool fields
        for key in ("enabled", "zero_hr_immediate"):
            _from_dict_bool(raw, kwargs, key)

        # Enum field
        if "zone_mode" in raw:
            v = raw["zone_mode"]
            if v in VALID_ZONE_MODES:
                kwargs["zone_mode"] = v
            else:
                valid = ", ".join(m.value for m in VALID_ZONE_MODES)
                user_logger.warning(
                    f"⚠ Érvénytelen 'zone_mode' érték: {v!r} ({valid} valamelyike kell, default: {kwargs['zone_mode']})"
                )

        return cls(**kwargs)


@dataclasses.dataclass
class BleConfig:
    """BLE kimeneti (ventilátor) beállítások – típusbiztos.

    Megjegyzés: ez a settings.json ``"ble_fan"`` szekciójához tartozik (a BLE
    ventilátor kimenet). Az osztály neve történeti okból maradt ``BleConfig``.
    A loader visszafelé kompatibilisen a régi ``"ble"`` kulcsot is elfogadja.
    """

    device_name: Optional[str] = None
    scan_timeout: int = 10
    connection_timeout: int = 15
    reconnect_interval: int = 5
    max_retries: int = 10
    command_timeout: int = 3
    service_uuid: str = "0000ffe0-0000-1000-8000-00805f9b34fb"
    characteristic_uuid: str = "0000ffe1-0000-1000-8000-00805f9b34fb"
    pin_code: Optional[str] = "123456"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BleConfig":
        d = cls()
        kwargs: dict[str, Any] = dataclasses.asdict(d)

        # device_name: null / "" / "null" / "none" → auto-discovery (csendes);
        # nem-üres string → használt név; bármi más típus → figyelmeztetés.
        _from_dict_nullable_str(raw, kwargs, "device_name")

        # Int fields with ranges
        _from_dict_int(raw, kwargs, "scan_timeout", 1, 60)
        _from_dict_int(raw, kwargs, "connection_timeout", 1, 60)
        _from_dict_int(raw, kwargs, "reconnect_interval", 1, 60)
        _from_dict_int(raw, kwargs, "max_retries", 1, 100)
        _from_dict_int(raw, kwargs, "command_timeout", 1, 30)

        # UUID-k: nem-üres string kell, különben figyelmeztetés + default marad.
        for uuid_key in ("service_uuid", "characteristic_uuid"):
            if uuid_key in raw:
                v = raw[uuid_key]
                if isinstance(v, str) and v.strip():
                    kwargs[uuid_key] = v.strip()
                else:
                    user_logger.warning(f"⚠ Érvénytelen '{uuid_key}' érték: {v!r} (nem-üres string kell)")

        # pin_code
        if "pin_code" in raw:
            pc = raw["pin_code"]
            if pc is None:
                kwargs["pin_code"] = None
            elif isinstance(pc, int) and not isinstance(pc, bool) and 0 <= pc <= 999999:
                kwargs["pin_code"] = str(pc)
                if len(str(pc)) < 6:
                    user_logger.warning(
                        f"⚠ pin_code int-ként megadva ({pc}) → \"{str(pc)}\". "
                        f"Ha vezető nullákra van szükség, string-ként add meg: "
                        f"\"pin_code\": \"{pc:06d}\""
                    )
            elif isinstance(pc, str) and pc.isdigit() and 0 < len(pc) <= 20:
                kwargs["pin_code"] = pc
            else:
                user_logger.warning(f"⚠ Érvénytelen 'pin_code' érték: {pc!r}")

        return cls(**kwargs)


@dataclasses.dataclass
class DatasourceConfig:
    """Adatforrás beállítások – típusbiztos."""

    power_source: Optional[str] = DataSource.ZWIFTUDP
    hr_source: Optional[str] = DataSource.ZWIFTUDP
    BLE_buffer_seconds: int = 3
    BLE_minimum_samples: int = 6
    BLE_buffer_rate_hz: int = 4
    BLE_dropout_timeout: int = 5
    ANT_buffer_seconds: int = 3
    ANT_minimum_samples: int = 6
    ANT_buffer_rate_hz: int = 4
    ANT_dropout_timeout: int = 5
    zwiftUDP_buffer_seconds: int = 10
    zwiftUDP_minimum_samples: int = 2
    zwiftUDP_buffer_rate_hz: int = 3
    zwiftUDP_dropout_timeout: int = 15
    ant_power_device_id: int = 0
    ant_hr_device_id: int = 0
    ant_power_reconnect_interval: int = 5
    ant_power_max_retries: int = 10
    ant_hr_reconnect_interval: int = 5
    ant_hr_max_retries: int = 10
    ble_power_device_name: Optional[str] = None
    ble_power_scan_timeout: int = 10
    ble_power_reconnect_interval: int = 5
    ble_power_max_retries: int = 10
    ble_hr_device_name: Optional[str] = None
    ble_hr_scan_timeout: int = 10
    ble_hr_reconnect_interval: int = 5
    ble_hr_max_retries: int = 10
    zwift_udp_port: int = 7878
    zwift_udp_host: str = "127.0.0.1"
    zwift_auto_launch: bool = True
    zwift_launcher_path: Optional[str] = None

    def __post_init__(self) -> None:
        # minimum_samples <= buffer_seconds * buffer_rate_hz cross-validation
        for prefix in ("BLE", "ANT", "zwiftUDP"):
            bs = getattr(self, f"{prefix}_buffer_seconds")
            ms = getattr(self, f"{prefix}_minimum_samples")
            brz = getattr(self, f"{prefix}_buffer_rate_hz")
            if bs > 0 and brz > 0:
                max_samples = bs * brz
                if ms > max_samples:
                    user_logger.warning(
                        f"⚠ [{prefix}] Érvénytelen minimum_samples ({ms}) – "
                        f"nagyobb, mint buffer_seconds * buffer_rate_hz "
                        f"({bs} * {brz} = {max_samples}). "
                        f"{prefix}_minimum_samples {max_samples}-re állítva."
                    )
                    setattr(self, f"{prefix}_minimum_samples", max_samples)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DatasourceConfig":
        d = cls()
        kwargs: dict[str, Any] = dataclasses.asdict(d)

        # power_source / hr_source: érvényes adatforrás (antplus/ble/zwiftudp)
        # vagy null (kikapcsolva). Bármi más → figyelmeztetés + default marad.
        for key in ("power_source", "hr_source"):
            if key not in raw:
                continue
            v = raw[key]
            if v is None:
                kwargs[key] = None
            elif v in VALID_DATA_SOURCES:
                kwargs[key] = v
            else:
                valid = ", ".join(src.value for src in VALID_DATA_SOURCES)
                user_logger.warning(
                    f"⚠ Érvénytelen '{key}' érték: {v!r} ({valid} vagy null kell)"
                )

        # ANT+ device IDs
        for key in ("ant_power_device_id", "ant_hr_device_id"):
            _from_dict_int(raw, kwargs, key, 0, 65535)
        for key in ("ant_power_reconnect_interval", "ant_hr_reconnect_interval"):
            _from_dict_int(raw, kwargs, key, 1, 60)
        for key in ("ant_power_max_retries", "ant_hr_max_retries"):
            _from_dict_int(raw, kwargs, key, 1, 100)

        # BLE sensor device names: null/""/"null"/"none" → auto-discovery (csendes);
        # nem-üres string → trimmed név; rossz típus → figyelmeztetés.
        for key in ("ble_power_device_name", "ble_hr_device_name"):
            _from_dict_nullable_str(raw, kwargs, key)
        for key in ("ble_power_scan_timeout", "ble_power_reconnect_interval",
                     "ble_hr_scan_timeout", "ble_hr_reconnect_interval"):
            _from_dict_int(raw, kwargs, key, 1, 60)
        for key in ("ble_power_max_retries", "ble_hr_max_retries"):
            _from_dict_int(raw, kwargs, key, 1, 100)

        # Zwift UDP host: nem-üres string kell (whitespace levágva).
        if "zwift_udp_host" in raw:
            v = raw["zwift_udp_host"]
            if isinstance(v, str) and v.strip():
                kwargs["zwift_udp_host"] = v.strip()
            else:
                user_logger.warning(f"⚠ Érvénytelen 'zwift_udp_host' érték: {v!r} (nem-üres string kell)")
        _from_dict_int(raw, kwargs, "zwift_udp_port", 1024, 65535)

        if "zwift_auto_launch" in raw:
            v = raw["zwift_auto_launch"]
            if isinstance(v, bool):
                kwargs["zwift_auto_launch"] = v
            else:
                user_logger.warning(f"⚠ Érvénytelen 'zwift_auto_launch' érték: {v!r} (true/false kell)")

        # zwift_launcher_path: null/""/"null"/"none"/whitespace → None (automatikus
        # keresés); nem-üres string → trimmed útvonal; rossz típus → figyelmeztetés.
        _from_dict_nullable_str(raw, kwargs, "zwift_launcher_path")

        # Per-source buffer settings
        for prefix in ("BLE", "ANT", "zwiftUDP"):
            _from_dict_int(raw, kwargs, f"{prefix}_buffer_seconds", 1, 60)
            _from_dict_int(raw, kwargs, f"{prefix}_minimum_samples", 1, 100)
            _from_dict_int(raw, kwargs, f"{prefix}_buffer_rate_hz", 1, 60)
            _from_dict_int(raw, kwargs, f"{prefix}_dropout_timeout", 1, 300)

        return cls(**kwargs)


@dataclasses.dataclass
class HudConfig:
    """HUD beállítások – típusbiztos."""

    save_hud_settings: bool = False
    sound_enabled: bool = True
    sound_volume: float = 0.5
    close_at_zwiftapp_exe: bool = True
    opacity: int = 92
    # Per-monitor ablak geometria: {"<screen_name>": {"x": .., "y": .., "w": .., "h": ..}}
    window_geometry: Dict[str, Dict[str, int]] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "HudConfig":
        kwargs: dict[str, Any] = {}
        _from_dict_bool(raw, kwargs, "save_hud_settings")
        _from_dict_bool(raw, kwargs, "sound_enabled")
        _from_dict_float(raw, kwargs, "sound_volume", 0.0, 1.0)
        # close_at_zwiftapp_exe: az új kulcs elsőbbséget élvez; a régi
        # "close_at_zwiftapp.exe" kulcsot visszafelé kompatibilisen elfogadjuk.
        if "close_at_zwiftapp_exe" in raw:
            _from_dict_bool(raw, kwargs, "close_at_zwiftapp_exe")
        elif "close_at_zwiftapp.exe" in raw:
            v = raw["close_at_zwiftapp.exe"]
            if isinstance(v, bool):
                kwargs["close_at_zwiftapp_exe"] = v
            else:
                user_logger.warning(f"⚠ Érvénytelen 'close_at_zwiftapp.exe' érték: {v!r} (true/false kell)")
        _from_dict_int(raw, kwargs, "opacity", 20, 100)
        if "window_geometry" in raw and isinstance(raw["window_geometry"], dict):
            geo: Dict[str, Dict[str, int]] = {}
            for screen_name, rect in raw["window_geometry"].items():
                if isinstance(rect, dict) and all(
                    k in rect and isinstance(rect[k], (int, float))
                    for k in ("x", "y", "w", "h")
                ):
                    geo[str(screen_name)] = {
                        "x": int(rect["x"]), "y": int(rect["y"]),
                        "w": int(rect["w"]), "h": int(rect["h"]),
                    }
            kwargs["window_geometry"] = geo
        return cls(**kwargs)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-kompatibilis dict (régi kulcsnévvel a kompatibilitásért)."""
        return {
            "save_hud_settings": self.save_hud_settings,
            "sound_enabled": self.sound_enabled,
            "sound_volume": self.sound_volume,
            "close_at_zwiftapp.exe": self.close_at_zwiftapp_exe,
            "opacity": self.opacity,
            "window_geometry": self.window_geometry,
        }


def _from_dict_int(src: dict[str, Any], dst: dict[str, Any], key: str, lo: int, hi: int) -> None:
    """Helper: int mezőt olvas raw dict-ből dst dict-be validálva."""
    if key not in src:
        return
    v = src[key]
    if isinstance(v, bool):
        user_logger.warning(f"⚠ Érvénytelen '{key}' érték: {v!r} ({lo}–{hi} közötti egész kell)")
        return
    if isinstance(v, float) and not v.is_integer():
        user_logger.warning(f"⚠ Érvénytelen '{key}' érték: {v!r} (törtrész nem elfogadott, egész kell)")
        return
    if isinstance(v, (int, float)) and lo <= v <= hi:
        dst[key] = int(v)
    else:
        user_logger.warning(f"⚠ Érvénytelen '{key}' érték: {v!r} ({lo}–{hi} közötti egész kell)")


def _from_dict_bool(src: dict[str, Any], dst: dict[str, Any], key: str) -> None:
    """Helper: bool mezőt olvas validálva. Rossz típus → figyelmeztetés, default marad."""
    if key not in src:
        return
    v = src[key]
    if isinstance(v, bool):
        dst[key] = v
    else:
        user_logger.warning(f"⚠ Érvénytelen '{key}' érték: {v!r} (true/false kell)")


def _from_dict_float(src: dict[str, Any], dst: dict[str, Any], key: str, lo: float, hi: float) -> None:
    """Helper: float mezőt olvas tartomány-validálva (bool kizárva)."""
    if key not in src:
        return
    v = src[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi):
        user_logger.warning(f"⚠ Érvénytelen '{key}' érték: {v!r} ({lo}–{hi} közötti szám kell)")
        return
    dst[key] = float(v)


def _from_dict_nullable_str(src: dict[str, Any], dst: dict[str, Any], key: str) -> None:
    """Helper: nullable string mező normalizálás (eszköznév, útvonal stb.).

    null / "" / "null" / "none" (kis-nagybetű érzéketlen) → None, csendben
    (az adott mezőnél ez auto-discovery / automatikus keresés jelentésű).
    Egyéb nem-üres string → trimmed érték. Rossz típus → figyelmeztetés,
    a default (None) marad.
    """
    if key not in src:
        return
    v = src[key]
    if v is None:
        dst[key] = None
    elif isinstance(v, str):
        s = v.strip()
        dst[key] = None if (not s or s.lower() in ("null", "none")) else s
    else:
        user_logger.warning(f"⚠ Érvénytelen '{key}' érték: {v!r} (string vagy null kell)")


# ============================================================
# ALAPÉRTELMEZETT BEÁLLÍTÁSOK
# ============================================================

DEFAULT_SETTINGS: Dict[str, Any] = {
    "global_settings": GlobalSettingsConfig(),
    "power_zones": PowerZonesConfig(),
    "heart_rate_zones": HeartRateZonesConfig(),
    "ble_fan": BleConfig(),
    "datasource": DatasourceConfig(),
    "hud": HudConfig(),
}

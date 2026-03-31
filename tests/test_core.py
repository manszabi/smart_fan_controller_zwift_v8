"""Unit tesztek a core logikához.

Futtatás: pytest tests/ -v
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import time

import pytest

from swift_fan_controller_new_v8_PySide6 import (
    ZoneMode,
    DataSource,
    calculate_hr_zones,
    calculate_power_zones,
    zone_for_power,
    zone_for_hr,
    higher_wins,
    apply_zone_mode,
    CooldownController,
    PowerZonesConfig,
    GlobalSettingsConfig,
    HeartRateZonesConfig,
    BleConfig,
    DatasourceConfig,
    HudConfig,
    _resolve_log_dir,
)


# ============================================================
# calculate_power_zones
# ============================================================

class TestCalculatePowerZones:
    """Teljesítmény zóna határok kiszámítása."""

    def test_default_ftp200(self):
        """FTP=200, z1=60%, z2=89% → Z1:1-120, Z2:121-178, Z3:179-1000."""
        zones = calculate_power_zones(ftp=200, min_watt=0, max_watt=1000, z1_pct=60, z2_pct=89)
        assert zones[0] == (0, 0)
        assert zones[1] == (1, 120)
        assert zones[2] == (121, 178)
        assert zones[3] == (179, 1000)

    def test_low_ftp(self):
        """Alacsony FTP – z1_max legalább 1."""
        zones = calculate_power_zones(ftp=100, min_watt=0, max_watt=500, z1_pct=1, z2_pct=5)
        assert zones[1][0] == 1
        assert zones[1][1] >= 1

    def test_high_ftp(self):
        """Magas FTP – normál zónahatárok."""
        zones = calculate_power_zones(ftp=400, min_watt=0, max_watt=2000, z1_pct=60, z2_pct=89)
        assert zones[1] == (1, 240)   # 400*0.6
        assert zones[2] == (241, 356)  # 400*0.89=356
        assert zones[3] == (357, 2000)

    def test_zones_contiguous(self):
        """Nincs rés és nincs átfedés a zónák között."""
        zones = calculate_power_zones(ftp=250, min_watt=0, max_watt=800, z1_pct=55, z2_pct=75)
        assert zones[1][0] == zones[0][1] + 1  # Z1 start = Z0 end + 1
        assert zones[2][0] == zones[1][1] + 1
        assert zones[3][0] == zones[2][1] + 1


# ============================================================
# zone_for_power
# ============================================================

class TestZoneForPower:
    """Teljesítmény → zóna szám konverzió."""

    @pytest.fixture()
    def zones(self):
        return calculate_power_zones(ftp=200, min_watt=0, max_watt=1000, z1_pct=60, z2_pct=89)

    def test_zero_watts(self, zones):
        assert zone_for_power(0, zones) == 0

    def test_negative_watts(self, zones):
        assert zone_for_power(-10, zones) == 0

    def test_zone1_low(self, zones):
        assert zone_for_power(1, zones) == 1

    def test_zone1_boundary(self, zones):
        assert zone_for_power(120, zones) == 1

    def test_zone2_low(self, zones):
        assert zone_for_power(121, zones) == 2

    def test_zone2_boundary(self, zones):
        assert zone_for_power(178, zones) == 2

    def test_zone3(self, zones):
        assert zone_for_power(179, zones) == 3

    def test_zone3_high(self, zones):
        assert zone_for_power(999, zones) == 3

    def test_above_max(self, zones):
        """max_watt felett is Z3."""
        assert zone_for_power(5000, zones) == 3

    def test_empty_zones(self):
        """Üres zones dict → Z0."""
        assert zone_for_power(100, {}) == 0


# ============================================================
# calculate_hr_zones / zone_for_hr
# ============================================================

class TestCalculateHrZones:
    def test_default(self):
        hr_z = calculate_hr_zones(max_hr=185, resting_hr=60, z1_pct=70, z2_pct=80)
        assert hr_z["resting"] == 60
        assert hr_z["z1_max"] == 129   # int(185*0.70)
        assert hr_z["z2_max"] == 148   # int(185*0.80)


class TestZoneForHr:
    @pytest.fixture()
    def hr_zones(self):
        return calculate_hr_zones(max_hr=185, resting_hr=60, z1_pct=70, z2_pct=80)

    def test_zero_hr(self, hr_zones):
        assert zone_for_hr(0, hr_zones) == 0

    def test_below_resting(self, hr_zones):
        assert zone_for_hr(50, hr_zones) == 0

    def test_at_resting(self, hr_zones):
        """resting_hr-nél Z1 (resting <= z1_max tartomány)."""
        assert zone_for_hr(60, hr_zones) == 1

    def test_zone1(self, hr_zones):
        assert zone_for_hr(100, hr_zones) == 1

    def test_zone1_boundary(self, hr_zones):
        assert zone_for_hr(129, hr_zones) == 1

    def test_zone2(self, hr_zones):
        assert zone_for_hr(130, hr_zones) == 2

    def test_zone2_boundary(self, hr_zones):
        assert zone_for_hr(148, hr_zones) == 2

    def test_zone3(self, hr_zones):
        assert zone_for_hr(149, hr_zones) == 3

    def test_zone3_high(self, hr_zones):
        assert zone_for_hr(200, hr_zones) == 3

    def test_negative_hr(self, hr_zones):
        assert zone_for_hr(-5, hr_zones) == 0


# ============================================================
# higher_wins / apply_zone_mode
# ============================================================

class TestHigherWins:
    def test_equal(self):
        assert higher_wins(2, 2) == 2

    def test_first_higher(self):
        assert higher_wins(3, 1) == 3

    def test_second_higher(self):
        assert higher_wins(1, 3) == 3

    def test_zero(self):
        assert higher_wins(0, 0) == 0


class TestApplyZoneMode:
    def test_power_only(self):
        assert apply_zone_mode(2, 3, ZoneMode.POWER_ONLY) == 2

    def test_power_only_none_hr(self):
        assert apply_zone_mode(2, None, ZoneMode.POWER_ONLY) == 2

    def test_hr_only(self):
        assert apply_zone_mode(2, 3, ZoneMode.HR_ONLY) == 3

    def test_hr_only_none_power(self):
        assert apply_zone_mode(None, 1, ZoneMode.HR_ONLY) == 1

    def test_higher_wins_both(self):
        assert apply_zone_mode(1, 3, ZoneMode.HIGHER_WINS) == 3

    def test_higher_wins_both_reversed(self):
        assert apply_zone_mode(3, 1, ZoneMode.HIGHER_WINS) == 3

    def test_higher_wins_only_power(self):
        assert apply_zone_mode(2, None, ZoneMode.HIGHER_WINS) == 2

    def test_higher_wins_only_hr(self):
        assert apply_zone_mode(None, 2, ZoneMode.HIGHER_WINS) == 2

    def test_higher_wins_both_none(self):
        assert apply_zone_mode(None, None, ZoneMode.HIGHER_WINS) is None

    def test_power_only_both_none(self):
        assert apply_zone_mode(None, None, ZoneMode.POWER_ONLY) is None


# ============================================================
# CooldownController
# ============================================================

class TestCooldownController:
    """Cooldown logika tesztek."""

    def test_first_zone_no_cooldown(self):
        """Első zóna beállítás – nincs cooldown."""
        cc = CooldownController(cooldown_seconds=60)
        result = cc.process(current_zone=None, new_zone=2, zero_immediate=False)
        assert result == 2
        assert not cc.active

    def test_zone_increase_immediate(self):
        """Zóna emelkedés → azonnali váltás, cooldown nélkül."""
        cc = CooldownController(cooldown_seconds=60)
        cc.process(None, 1, False)  # init
        result = cc.process(1, 3, False)
        assert result == 3
        assert not cc.active

    def test_zone_decrease_starts_cooldown(self):
        """Zóna csökkentés → cooldown indul, nem vált azonnal."""
        cc = CooldownController(cooldown_seconds=60)
        cc.process(None, 3, False)
        result = cc.process(3, 1, False)
        assert result is None  # nem vált
        assert cc.active
        assert cc.pending_zone == 1

    def test_same_zone_no_change(self):
        """Ugyanaz a zóna → None (nincs változás)."""
        cc = CooldownController(cooldown_seconds=60)
        cc.process(None, 2, False)
        result = cc.process(2, 2, False)
        assert result is None

    def test_zero_immediate(self):
        """zero_power_immediate=True, 0W → azonnali leállás."""
        cc = CooldownController(cooldown_seconds=120)
        cc.process(None, 3, False)
        result = cc.process(3, 0, True)
        assert result == 0
        assert not cc.active

    def test_zero_immediate_already_zero(self):
        """Már Z0-ban vagyunk, zero_immediate → None."""
        cc = CooldownController(cooldown_seconds=120)
        cc.process(None, 0, False)
        result = cc.process(0, 0, True)
        assert result is None

    def test_cooldown_zero_seconds_immediate(self):
        """cooldown_seconds=0 → azonnali zónacsökkentés, nincs várakozás."""
        cc = CooldownController(cooldown_seconds=0)
        cc.process(None, 3, False)
        result = cc.process(3, 1, False)
        assert result == 1

    def test_cooldown_expires(self):
        """Cooldown lejárta után alkalmazódik a várakozó zóna."""
        cc = CooldownController(cooldown_seconds=1)
        cc.process(None, 3, False)
        cc.process(3, 1, False)
        assert cc.active
        time.sleep(1.1)
        result = cc.process(3, 1, False)
        assert result == 1
        assert not cc.active

    def test_zone_increase_cancels_cooldown(self):
        """Cooldown alatt emelkedés → cooldown törlődik, azonnali váltás."""
        cc = CooldownController(cooldown_seconds=60)
        cc.process(None, 3, False)
        cc.process(3, 1, False)
        assert cc.active
        result = cc.process(3, 3, False)
        assert result is None
        assert not cc.active  # cooldown törölve

    def test_zone_increase_above_current_cancels_cooldown(self):
        """Cooldown alatt > current_zone → cooldown törlődik + új zóna."""
        cc = CooldownController(cooldown_seconds=60)
        cc.process(None, 2, False)
        cc.process(2, 0, False)
        assert cc.active
        result = cc.process(2, 3, False)
        assert result == 3
        assert not cc.active

    def test_snapshot_active(self):
        """Snapshot aktív cooldown-ról."""
        cc = CooldownController(cooldown_seconds=60)
        cc.process(None, 3, False)
        cc.process(3, 1, False)
        active, remaining = cc.snapshot()
        assert active is True
        assert 0 < remaining <= 60

    def test_snapshot_inactive(self):
        cc = CooldownController(cooldown_seconds=60)
        active, remaining = cc.snapshot()
        assert active is False
        assert remaining == 0.0

    def test_reset(self):
        cc = CooldownController(cooldown_seconds=60)
        cc.process(None, 3, False)
        cc.process(3, 1, False)
        assert cc.active
        cc.reset()
        assert not cc.active
        assert cc.pending_zone is None


# ============================================================
# PowerZonesConfig dataclass
# ============================================================

class TestPowerZonesConfig:
    def test_defaults(self):
        cfg = PowerZonesConfig()
        assert cfg.ftp == 200
        assert cfg.z1_max_percent == 60
        assert cfg.z2_max_percent == 89
        assert cfg.zero_power_immediate is False

    def test_from_dict_valid(self):
        cfg = PowerZonesConfig.from_dict({"ftp": 300, "z1_max_percent": 50, "z2_max_percent": 75})
        assert cfg.ftp == 300
        assert cfg.z1_max_percent == 50
        assert cfg.z2_max_percent == 75

    def test_from_dict_invalid_ftp_ignored(self):
        """Érvénytelen FTP → alapértelmezés marad."""
        cfg = PowerZonesConfig.from_dict({"ftp": 9999})
        assert cfg.ftp == 200  # default

    def test_from_dict_bool_ftp_ignored(self):
        cfg = PowerZonesConfig.from_dict({"ftp": True})
        assert cfg.ftp == 200

    def test_from_dict_string_ftp_ignored(self):
        cfg = PowerZonesConfig.from_dict({"ftp": "two hundred"})
        assert cfg.ftp == 200

    def test_post_init_swaps_min_max(self):
        """min_watt > max_watt → felcserélés."""
        cfg = PowerZonesConfig(ftp=200, min_watt=500, max_watt=100)
        assert cfg.min_watt == 100
        assert cfg.max_watt == 500

    def test_post_init_equal_min_max(self):
        """min_watt == max_watt → max_watt += 1."""
        cfg = PowerZonesConfig(ftp=200, min_watt=100, max_watt=100)
        assert cfg.max_watt == 101

    def test_post_init_z1_ge_z2(self):
        """z1 >= z2 → rendezés és legalább 1% különbség."""
        cfg = PowerZonesConfig(ftp=200, z1_max_percent=90, z2_max_percent=60)
        assert cfg.z1_max_percent < cfg.z2_max_percent

    def test_post_init_z1_eq_z2(self):
        cfg = PowerZonesConfig(ftp=200, z1_max_percent=80, z2_max_percent=80)
        assert cfg.z1_max_percent < cfg.z2_max_percent

    def test_to_dict(self):
        cfg = PowerZonesConfig()
        d = cfg.to_dict()
        assert d["ftp"] == 200
        assert isinstance(d, dict)

    def test_from_dict_empty(self):
        """Üres dict → összes default."""
        cfg = PowerZonesConfig.from_dict({})
        assert cfg == PowerZonesConfig()


# ============================================================
# GlobalSettingsConfig dataclass
# ============================================================

class TestGlobalSettingsConfig:
    def test_defaults(self):
        cfg = GlobalSettingsConfig()
        assert cfg.cooldown_seconds == 120
        assert cfg.log_directory is None

    def test_from_dict(self):
        cfg = GlobalSettingsConfig.from_dict({"cooldown_seconds": 60, "buffer_seconds": 5})
        assert cfg.cooldown_seconds == 60
        assert cfg.buffer_seconds == 5

    def test_from_dict_invalid_range(self):
        """Tartományon kívüli érték → default marad."""
        cfg = GlobalSettingsConfig.from_dict({"cooldown_seconds": 999})
        assert cfg.cooldown_seconds == 120  # default (0-300 range)

    def test_from_dict_bool_rejected(self):
        cfg = GlobalSettingsConfig.from_dict({"cooldown_seconds": True})
        assert cfg.cooldown_seconds == 120

    def test_from_dict_log_directory_null(self):
        cfg = GlobalSettingsConfig.from_dict({"log_directory": None})
        assert cfg.log_directory is None

    def test_from_dict_log_directory_string(self):
        cfg = GlobalSettingsConfig.from_dict({"log_directory": "/tmp/logs"})
        assert cfg.log_directory == "/tmp/logs"

    def test_from_dict_log_directory_empty_string(self):
        """Üres string → nincs mentve (marad None default)."""
        cfg = GlobalSettingsConfig.from_dict({"log_directory": "   "})
        assert cfg.log_directory is None


# ============================================================
# HeartRateZonesConfig dataclass
# ============================================================

class TestHeartRateZonesConfig:
    def test_defaults(self):
        cfg = HeartRateZonesConfig()
        assert cfg.max_hr == 185
        assert cfg.resting_hr == 60
        assert cfg.zone_mode == ZoneMode.HIGHER_WINS

    def test_from_dict(self):
        cfg = HeartRateZonesConfig.from_dict({
            "max_hr": 190, "resting_hr": 55, "zone_mode": "power_only"
        })
        assert cfg.max_hr == 190
        assert cfg.resting_hr == 55
        assert cfg.zone_mode == "power_only"

    def test_post_init_z1_ge_z2(self):
        cfg = HeartRateZonesConfig(z1_max_percent=90, z2_max_percent=70)
        assert cfg.z1_max_percent < cfg.z2_max_percent

    def test_post_init_resting_ge_max(self):
        """resting_hr >= max_hr → resting_hr korrigálva."""
        cfg = HeartRateZonesConfig(max_hr=150, resting_hr=160)
        assert cfg.resting_hr < cfg.max_hr

    def test_post_init_valid_min_ge_valid_max(self):
        """valid_min >= valid_max → default-ra állítva."""
        cfg = HeartRateZonesConfig(valid_min_hr=250, valid_max_hr=200)
        assert cfg.valid_min_hr == 30
        assert cfg.valid_max_hr == 220

    def test_from_dict_invalid_zone_mode(self):
        """Érvénytelen zone_mode → default marad."""
        cfg = HeartRateZonesConfig.from_dict({"zone_mode": "banana"})
        assert cfg.zone_mode == ZoneMode.HIGHER_WINS

    def test_from_dict_bool_fields(self):
        cfg = HeartRateZonesConfig.from_dict({
            "enabled": False, "zero_hr_immediate": True
        })
        assert cfg.enabled is False
        assert cfg.zero_hr_immediate is True


# ============================================================
# BleConfig dataclass
# ============================================================

class TestBleConfig:
    def test_defaults(self):
        cfg = BleConfig()
        assert cfg.device_name is None
        assert cfg.pin_code == "123456"
        assert cfg.scan_timeout == 10

    def test_from_dict_device_name(self):
        cfg = BleConfig.from_dict({"device_name": "MyESP32"})
        assert cfg.device_name == "MyESP32"

    def test_from_dict_device_name_null(self):
        cfg = BleConfig.from_dict({"device_name": None})
        assert cfg.device_name is None

    def test_from_dict_device_name_empty(self):
        cfg = BleConfig.from_dict({"device_name": "  "})
        assert cfg.device_name is None

    def test_from_dict_pin_code_int(self):
        """Int pin_code → string-re konvertálva."""
        cfg = BleConfig.from_dict({"pin_code": 123456})
        assert cfg.pin_code == "123456"

    def test_from_dict_pin_code_null(self):
        cfg = BleConfig.from_dict({"pin_code": None})
        assert cfg.pin_code is None

    def test_from_dict_pin_code_string(self):
        cfg = BleConfig.from_dict({"pin_code": "012345"})
        assert cfg.pin_code == "012345"

    def test_from_dict_int_ranges(self):
        cfg = BleConfig.from_dict({"scan_timeout": 30, "max_retries": 50})
        assert cfg.scan_timeout == 30
        assert cfg.max_retries == 50

    def test_from_dict_invalid_scan_timeout(self):
        cfg = BleConfig.from_dict({"scan_timeout": 999})
        assert cfg.scan_timeout == 10  # default


# ============================================================
# DatasourceConfig dataclass
# ============================================================

class TestDatasourceConfig:
    def test_defaults(self):
        cfg = DatasourceConfig()
        assert cfg.power_source == DataSource.ZWIFTUDP
        assert cfg.hr_source == DataSource.ZWIFTUDP
        assert cfg.zwift_udp_port == 7878

    def test_from_dict_sources(self):
        cfg = DatasourceConfig.from_dict({
            "power_source": "antplus", "hr_source": "ble"
        })
        assert cfg.power_source == "antplus"
        assert cfg.hr_source == "ble"

    def test_from_dict_null_source(self):
        cfg = DatasourceConfig.from_dict({"power_source": None})
        assert cfg.power_source is None

    def test_from_dict_invalid_source_ignored(self):
        cfg = DatasourceConfig.from_dict({"power_source": "banana"})
        assert cfg.power_source == DataSource.ZWIFTUDP  # default

    def test_post_init_min_samples_capped(self):
        """minimum_samples > buffer*rate → korrigálva."""
        cfg = DatasourceConfig(
            BLE_buffer_seconds=2,
            BLE_buffer_rate_hz=3,
            BLE_minimum_samples=99,
        )
        assert cfg.BLE_minimum_samples == 6  # 2*3

    def test_from_dict_zwift_settings(self):
        cfg = DatasourceConfig.from_dict({
            "zwift_udp_port": 9999,
            "zwift_udp_host": "192.168.1.1",
            "zwift_auto_launch": False,
        })
        assert cfg.zwift_udp_port == 9999
        assert cfg.zwift_udp_host == "192.168.1.1"
        assert cfg.zwift_auto_launch is False

    def test_from_dict_ant_device_ids(self):
        cfg = DatasourceConfig.from_dict({
            "ant_power_device_id": 12345,
            "ant_hr_device_id": 54321,
        })
        assert cfg.ant_power_device_id == 12345
        assert cfg.ant_hr_device_id == 54321


# ============================================================
# HudConfig dataclass
# ============================================================

class TestHudConfig:
    def test_defaults(self):
        cfg = HudConfig()
        assert cfg.sound_enabled is True
        assert cfg.sound_volume == 0.5
        assert cfg.close_at_zwiftapp_exe is True

    def test_from_dict_old_key(self):
        """A régi 'close_at_zwiftapp.exe' kulcs is elfogadott."""
        cfg = HudConfig.from_dict({"close_at_zwiftapp.exe": False})
        assert cfg.close_at_zwiftapp_exe is False

    def test_from_dict_new_key(self):
        cfg = HudConfig.from_dict({"close_at_zwiftapp_exe": False})
        assert cfg.close_at_zwiftapp_exe is False

    def test_to_dict_old_key(self):
        """to_dict() a régi kulcsnévvel adja vissza a kompatibilitásért."""
        d = HudConfig().to_dict()
        assert "close_at_zwiftapp.exe" in d
        assert d["close_at_zwiftapp.exe"] is True

    def test_from_dict_volume(self):
        cfg = HudConfig.from_dict({"sound_volume": 0.8})
        assert cfg.sound_volume == 0.8

    def test_from_dict_int_volume(self):
        """Int volume → float-ra konvertálva."""
        cfg = HudConfig.from_dict({"sound_volume": 1})
        assert cfg.sound_volume == 1.0
        assert isinstance(cfg.sound_volume, float)


# ============================================================
# _resolve_log_dir
# ============================================================

class TestResolveLogDir:
    """Log könyvtár feloldás és validálás."""

    @property
    def _default_dir(self) -> str:
        """Az elvárt default könyvtár (a fő modul helye)."""
        import swift_fan_controller_new_v8_PySide6 as mod
        return os.path.dirname(os.path.abspath(mod.__file__))

    def test_none_returns_default(self):
        assert _resolve_log_dir(None) == self._default_dir

    def test_empty_returns_default(self):
        assert _resolve_log_dir("") == self._default_dir

    def test_creates_new_directory(self):
        tmp = tempfile.mkdtemp()
        test_dir = os.path.join(tmp, "logs")
        try:
            result = _resolve_log_dir(test_dir)
            assert result == test_dir
            assert os.path.isdir(test_dir)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_creates_nested_directory(self):
        tmp = tempfile.mkdtemp()
        nested = os.path.join(tmp, "a", "b", "c")
        try:
            result = _resolve_log_dir(nested)
            assert result == nested
            assert os.path.isdir(nested)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_non_writable_fallback(self):
        """/proc alá nem tud írni → fallback."""
        result = _resolve_log_dir("/proc/1/fake_log_test")
        assert result == self._default_dir

    def test_tilde_expansion(self):
        home = os.path.expanduser("~")
        test_dir = os.path.join(home, ".smart_fan_test_tmp")
        try:
            result = _resolve_log_dir("~/.smart_fan_test_tmp")
            assert result == test_dir
            assert os.path.isdir(test_dir)
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)

    def test_existing_directory(self):
        """Már létező könyvtár → visszaadja."""
        tmp = tempfile.mkdtemp()
        try:
            result = _resolve_log_dir(tmp)
            assert result == tmp
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# Enums
# ============================================================

class TestEnums:
    def test_zone_mode_values(self):
        assert ZoneMode.POWER_ONLY == "power_only"
        assert ZoneMode.HR_ONLY == "hr_only"
        assert ZoneMode.HIGHER_WINS == "higher_wins"

    def test_data_source_values(self):
        assert DataSource.ANTPLUS == "antplus"
        assert DataSource.BLE == "ble"
        assert DataSource.ZWIFTUDP == "zwiftudp"

    def test_zone_mode_string_comparison(self):
        """str(enum) is hasonlítható raw string-gel (str öröklés)."""
        assert ZoneMode.HIGHER_WINS == "higher_wins"
        assert "higher_wins" == ZoneMode.HIGHER_WINS

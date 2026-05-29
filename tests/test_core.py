"""Unit tesztek a core logikához.

Futtatás: pytest tests/ -v
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from unittest.mock import patch

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
        """Cooldown lejárta után alkalmazódik a várakozó zóna.

        A 3→1 zónaesés (>=2 szint) auto-felezést triggerel: 60s → 30s.
        A mock 61s-t szimulál, tehát biztosan lejár.
        """
        clock = [1000.0]
        with patch("time.monotonic", side_effect=lambda: clock[0]):
            cc = CooldownController(cooldown_seconds=60)
            cc.process(None, 3, False)       # init
            cc.process(3, 1, False)          # cooldown indul + auto-felezés (30s)
            assert cc.active
            clock[0] = 1061.0                # 61s később → biztosan lejárt
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
        clock = [1000.0]
        with patch("time.monotonic", side_effect=lambda: clock[0]):
            cc = CooldownController(cooldown_seconds=60)
            cc.process(None, 3, False)
            cc.process(3, 1, False)  # cooldown indul, 3→1 (>=2 szint) → felezés: 30s
            clock[0] = 1010.0        # 10s eltelt → maradék ~20s
            active, remaining = cc.snapshot()
        assert active is True
        assert 19.0 <= remaining <= 21.0  # ~20s (30s felezett cooldown - 10s)

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

    # --- from_dict: min_watt / max_watt érvényes és érvénytelen ---

    def test_from_dict_min_max_watt_valid(self):
        """Érvényes min_watt/max_watt → átveszi."""
        cfg = PowerZonesConfig.from_dict({"min_watt": 50, "max_watt": 800})
        assert cfg.min_watt == 50
        assert cfg.max_watt == 800

    def test_from_dict_min_watt_out_of_range_ignored(self):
        """min_watt > 1000 → from_dict figyelmen kívül hagyja (default marad)."""
        cfg = PowerZonesConfig.from_dict({"min_watt": 5000})
        assert cfg.min_watt == 0  # default

    def test_from_dict_min_watt_negative_ignored(self):
        """Negatív min_watt → default marad."""
        cfg = PowerZonesConfig.from_dict({"min_watt": -10})
        assert cfg.min_watt == 0  # default

    def test_from_dict_max_watt_out_of_range_ignored(self):
        """max_watt > 1000 → default marad."""
        cfg = PowerZonesConfig.from_dict({"max_watt": 9999})
        assert cfg.max_watt == 1000  # default

    def test_from_dict_min_watt_bool_ignored(self):
        """Bool min_watt → default marad."""
        cfg = PowerZonesConfig.from_dict({"min_watt": True})
        assert cfg.min_watt == 0

    def test_from_dict_max_watt_string_ignored(self):
        """String max_watt → default marad."""
        cfg = PowerZonesConfig.from_dict({"max_watt": "ezer"})
        assert cfg.max_watt == 1000

    # --- from_dict: z1 / z2 érvénytelen ---

    def test_from_dict_z1_out_of_range_ignored(self):
        """z1_max_percent > 100 → default marad (60)."""
        cfg = PowerZonesConfig.from_dict({"z1_max_percent": 150})
        assert cfg.z1_max_percent == 60

    def test_from_dict_z1_zero_ignored(self):
        """z1_max_percent = 0 (tartományon kívül) → default marad (60)."""
        cfg = PowerZonesConfig.from_dict({"z1_max_percent": 0})
        assert cfg.z1_max_percent == 60

    def test_from_dict_z2_bool_ignored(self):
        """Bool z2_max_percent → default marad (89)."""
        cfg = PowerZonesConfig.from_dict({"z2_max_percent": True})
        assert cfg.z2_max_percent == 89

    def test_from_dict_logical_swap_corrected(self):
        """from_dict érvényes z1>z2 értékeket átvesz, majd __post_init__ defaultra állít."""
        cfg = PowerZonesConfig.from_dict({"z1_max_percent": 90, "z2_max_percent": 50})
        # mindkettő érvényes tartományban → from_dict átveszi,
        # de a logikai check (__post_init__) defaultra állítja
        assert cfg.z1_max_percent == 60
        assert cfg.z2_max_percent == 89

    def test_post_init_min_gt_max(self):
        """min_watt > max_watt → mindkettő alapértelmezésre áll."""
        cfg = PowerZonesConfig(ftp=200, min_watt=500, max_watt=100)
        assert cfg.min_watt == 0  # default
        assert cfg.max_watt == 1000  # default

    def test_post_init_min_eq_max(self):
        """min_watt == max_watt → mindkettő alapértelmezésre áll."""
        cfg = PowerZonesConfig(ftp=200, min_watt=100, max_watt=100)
        assert cfg.min_watt == 0  # default
        assert cfg.max_watt == 1000  # default

    def test_post_init_z1_ge_z2(self):
        """z1 >= z2 → mindkettő alapértelmezésre áll."""
        cfg = PowerZonesConfig(ftp=200, z1_max_percent=90, z2_max_percent=60)
        assert cfg.z1_max_percent == 60  # default
        assert cfg.z2_max_percent == 89  # default

    def test_post_init_z1_eq_z2(self):
        """z1 == z2 → mindkettő alapértelmezésre áll."""
        cfg = PowerZonesConfig(ftp=200, z1_max_percent=80, z2_max_percent=80)
        assert cfg.z1_max_percent == 60  # default
        assert cfg.z2_max_percent == 89  # default

    # --- __post_init__ tartomány-ellenőrzés (0–1000 watt) ---

    def test_post_init_ftp_negative(self):
        """Negatív ftp → alapértelmezés (200)."""
        cfg = PowerZonesConfig(ftp=-50)
        assert cfg.ftp == 200

    def test_post_init_ftp_too_high(self):
        """ftp > 1000 → alapértelmezés (200)."""
        cfg = PowerZonesConfig(ftp=2000)
        assert cfg.ftp == 200

    def test_post_init_min_watt_negative(self):
        """Negatív min_watt → 0-ra javítva."""
        cfg = PowerZonesConfig(min_watt=-10)
        assert cfg.min_watt == 0

    def test_post_init_min_watt_too_high(self):
        """min_watt > 1000 → 0-ra javítva (és így < max_watt)."""
        cfg = PowerZonesConfig(min_watt=2000)
        assert cfg.min_watt == 0

    def test_post_init_max_watt_negative(self):
        """Negatív max_watt → 1000-re javítva."""
        cfg = PowerZonesConfig(max_watt=-10)
        assert cfg.max_watt == 1000

    def test_post_init_max_watt_too_high(self):
        """max_watt > 1000 → 1000-re javítva."""
        cfg = PowerZonesConfig(max_watt=5000)
        assert cfg.max_watt == 1000

    def test_post_init_z1_zero(self):
        """z1_max_percent = 0 (tartományon kívül) → alapértelmezés (60)."""
        cfg = PowerZonesConfig(z1_max_percent=0)
        assert cfg.z1_max_percent == 60

    def test_post_init_z1_too_high(self):
        """z1_max_percent > 100 → alapértelmezés (60)."""
        cfg = PowerZonesConfig(z1_max_percent=150)
        assert cfg.z1_max_percent == 60

    def test_post_init_z2_too_high(self):
        """z2_max_percent > 100 → alapértelmezés (89)."""
        cfg = PowerZonesConfig(z2_max_percent=150)
        assert cfg.z2_max_percent == 89

    def test_to_dict(self):
        cfg = PowerZonesConfig()
        d = cfg.to_dict()
        assert d["ftp"] == 200
        assert isinstance(d, dict)

    def test_from_dict_empty(self):
        """Üres dict → összes default."""
        cfg = PowerZonesConfig.from_dict({})
        assert cfg == PowerZonesConfig()

    def test_from_dict_zero_power_immediate_valid_true(self):
        """zero_power_immediate = true → True."""
        cfg = PowerZonesConfig.from_dict({"zero_power_immediate": True})
        assert cfg.zero_power_immediate is True

    def test_from_dict_zero_power_immediate_valid_false(self):
        """zero_power_immediate = false → False."""
        cfg = PowerZonesConfig.from_dict({"zero_power_immediate": False})
        assert cfg.zero_power_immediate is False

    def test_from_dict_zero_power_immediate_string_typo_tue(self):
        """zero_power_immediate = 'tue' (typo) → False (warning logged)."""
        cfg = PowerZonesConfig.from_dict({"zero_power_immediate": "tue"})
        assert cfg.zero_power_immediate is False

    def test_from_dict_zero_power_immediate_string_typo_fales(self):
        """zero_power_immediate = 'fales' (typo) → False (warning logged)."""
        cfg = PowerZonesConfig.from_dict({"zero_power_immediate": "fales"})
        assert cfg.zero_power_immediate is False

    def test_from_dict_zero_power_immediate_string_true(self):
        """zero_power_immediate = 'true' (string, not bool) → False (warning logged)."""
        cfg = PowerZonesConfig.from_dict({"zero_power_immediate": "true"})
        assert cfg.zero_power_immediate is False

    def test_from_dict_zero_power_immediate_string_false(self):
        """zero_power_immediate = 'false' (string, not bool) → False (warning logged)."""
        cfg = PowerZonesConfig.from_dict({"zero_power_immediate": "false"})
        assert cfg.zero_power_immediate is False

    def test_from_dict_zero_power_immediate_integer_1(self):
        """zero_power_immediate = 1 (integer) → False (warning logged)."""
        cfg = PowerZonesConfig.from_dict({"zero_power_immediate": 1})
        assert cfg.zero_power_immediate is False

    def test_from_dict_zero_power_immediate_integer_0(self):
        """zero_power_immediate = 0 (integer) → False (warning logged)."""
        cfg = PowerZonesConfig.from_dict({"zero_power_immediate": 0})
        assert cfg.zero_power_immediate is False

    def test_from_dict_zero_power_immediate_none(self):
        """zero_power_immediate = None → False (warning logged)."""
        cfg = PowerZonesConfig.from_dict({"zero_power_immediate": None})
        assert cfg.zero_power_immediate is False

    def test_from_dict_zero_power_immediate_random_string(self):
        """zero_power_immediate = 'anything' → False (warning logged)."""
        cfg = PowerZonesConfig.from_dict({"zero_power_immediate": "anything"})
        assert cfg.zero_power_immediate is False


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
        assert cfg.opacity == 92
        assert cfg.window_geometry == {}

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

    def test_from_dict_opacity(self):
        cfg = HudConfig.from_dict({"opacity": 75})
        assert cfg.opacity == 75

    def test_from_dict_opacity_clamped(self):
        """Tartományon kívüli opacity → default marad."""
        cfg = HudConfig.from_dict({"opacity": 5})
        assert cfg.opacity == 92  # default, 5 < 20

    def test_from_dict_opacity_bool_ignored(self):
        cfg = HudConfig.from_dict({"opacity": True})
        assert cfg.opacity == 92

    def test_from_dict_window_geometry(self):
        geo = {"HDMI-1": {"x": 100, "y": 200, "w": 340, "h": 460}}
        cfg = HudConfig.from_dict({"window_geometry": geo})
        assert cfg.window_geometry == geo

    def test_from_dict_window_geometry_invalid_rect(self):
        """Hiányos rect → nem kerül be."""
        geo = {"HDMI-1": {"x": 100, "y": 200}}  # w, h hiányzik
        cfg = HudConfig.from_dict({"window_geometry": geo})
        assert cfg.window_geometry == {}

    def test_from_dict_window_geometry_multi_monitor(self):
        geo = {
            "HDMI-1": {"x": 0, "y": 0, "w": 340, "h": 460},
            "DP-2": {"x": 1920, "y": 100, "w": 400, "h": 500},
        }
        cfg = HudConfig.from_dict({"window_geometry": geo})
        assert len(cfg.window_geometry) == 2
        assert cfg.window_geometry["DP-2"]["x"] == 1920

    def test_to_dict_includes_opacity_and_geometry(self):
        cfg = HudConfig(opacity=80, window_geometry={"X": {"x": 1, "y": 2, "w": 3, "h": 4}})
        d = cfg.to_dict()
        assert d["opacity"] == 80
        assert d["window_geometry"]["X"]["w"] == 3

    def test_save_hud_settings_default_false(self):
        """save_hud_settings default értéke False."""
        cfg = HudConfig()
        assert cfg.save_hud_settings is False

    def test_from_dict_save_hud_settings(self):
        """save_hud_settings értékét from_dict-ből lehet beállítani."""
        cfg = HudConfig.from_dict({"save_hud_settings": True})
        assert cfg.save_hud_settings is True

    def test_to_dict_includes_save_hud_settings(self):
        """to_dict() tartalmazni kell a save_hud_settings értéket."""
        cfg = HudConfig(save_hud_settings=True)
        d = cfg.to_dict()
        assert d["save_hud_settings"] is True


# ============================================================
# HUD-csak mentés (save_hud_settings_only)
# ============================================================

class TestSaveHudSettingsOnly:
    """A save_hud_settings_only() csak a 'hud' szekciót frissíti."""

    def _import_loader(self):
        from smart_fan_controller.config import loader
        return loader

    def test_enabled_saves_only_hud_section(self, tmp_path):
        """save_hud_settings=True → csak a 'hud' szekciót frissíti."""
        loader = self._import_loader()
        import json

        target = tmp_path / "settings.json"
        original = {
            "power_zones": {"ftp": 285, "min_watt": 15},  # felhasználó szerkesztése
            "hud": {"save_hud_settings": True, "opacity": 92},
        }
        target.write_text(json.dumps(original), encoding="utf-8")

        # HUD frissítés
        from smart_fan_controller.config.schemas import HudConfig
        hud_cfg = HudConfig(save_hud_settings=True, opacity=75)
        result = loader.save_hud_settings_only(str(target), hud_cfg)

        assert result is True
        updated = json.loads(target.read_text(encoding="utf-8"))
        # power_zones megmaradt
        assert updated["power_zones"]["ftp"] == 285
        # hud frissítve
        assert updated["hud"]["opacity"] == 75

    def test_disabled_does_not_save(self, tmp_path):
        """save_hud_settings=False → nem ír a JSON-ba."""
        loader = self._import_loader()
        import json

        target = tmp_path / "settings.json"
        original = {
            "hud": {"save_hud_settings": False, "opacity": 92},
        }
        target.write_text(json.dumps(original), encoding="utf-8")

        from smart_fan_controller.config.schemas import HudConfig
        hud_cfg = HudConfig(save_hud_settings=False, opacity=75)
        result = loader.save_hud_settings_only(str(target), hud_cfg)

        assert result is False
        updated = json.loads(target.read_text(encoding="utf-8"))
        assert updated["hud"]["opacity"] == 92  # nem változott

    def test_preserves_other_sections_on_error(self, tmp_path):
        """Olvasási hiba → nem írja felül a fájlt, mas szekciók megmaradnak."""
        loader = self._import_loader()
        import json

        target = tmp_path / "settings.json"
        original = {"power_zones": {"ftp": 285}}
        target.write_text(json.dumps(original), encoding="utf-8")

        # A fájlba írható, de a hud szekció nincs benne
        from smart_fan_controller.config.schemas import HudConfig
        hud_cfg = HudConfig(save_hud_settings=True, opacity=75)
        result = loader.save_hud_settings_only(str(target), hud_cfg)

        assert result is True
        updated = json.loads(target.read_text(encoding="utf-8"))
        assert updated["power_zones"]["ftp"] == 285  # megmaradt
        assert updated["hud"]["opacity"] == 75  # hozzáadva

    def test_missing_file_error(self, tmp_path):
        """Nem létezik fájl → False."""
        loader = self._import_loader()
        target = tmp_path / "settings.json"

        from smart_fan_controller.config.schemas import HudConfig
        hud_cfg = HudConfig(save_hud_settings=True)
        result = loader.save_hud_settings_only(str(target), hud_cfg)

        assert result is False


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


# ============================================================
# Headless import (PySide6 nélkül)
# ============================================================

class TestHeadlessImport:
    """A modulnak importálhatónak kell lennie PySide6 nélkül is.

    A conftest stubokat injektál, ezért a PySide6-mentes utat külön
    alfolyamatban kell ellenőrizni, ahol sem a valódi PySide6, sem a
    conftest stubjai nincsenek a sys.modules-ban.
    """

    def _subprocess_import(self, block_pyside6: bool):
        import subprocess
        import sys
        import os

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # Tiszta import: ha block_pyside6, akkor a PySide6 import ImportError-t
        # dob (a sys.path-ról kitiltjuk a meta_path finder-rel).
        code = (
            "import sys\n"
            "if {block}:\n"
            "    class _Blocker:\n"
            "        # MetaPathFinder.find_spec protokoll (modern import rendszer)\n"
            "        def find_spec(self, name, path=None, target=None):\n"
            "            if name == 'PySide6' or name.startswith('PySide6.'):\n"
            "                raise ModuleNotFoundError(name)\n"
            "            return None\n"
            "    sys.meta_path.insert(0, _Blocker())\n"
            "import swift_fan_controller_new_v8_PySide6 as m\n"
            "assert m._PYSIDE6_AVAILABLE is (not {block})\n"
            "print('OK')\n"
        ).format(block=block_pyside6)
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root, capture_output=True, text=True,
        )

    def test_import_without_pyside6(self):
        """PySide6 nélkül a modul importja nem hasal el (headless mód)."""
        result = self._subprocess_import(block_pyside6=True)
        assert result.returncode == 0, (
            f"Headless import elhasalt:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout


# ============================================================
# Default settings betöltés / másolás (settings.default.json)
# ============================================================

class TestDefaultSettingsCopy:
    """A load_settings() / _ensure_default_settings_file() másolási logikája.

    Viselkedés:
      - Ha settings.json nincs, de settings.default.json elérhető → másol.
      - CWD-beli settings.default.json elsőbbséget élvez a package data-val szemben.
      - Meglévő settings.json-t nem ír felül.
      - Nem másol fájlt önmagára.
    """

    def _import_loader(self):
        from smart_fan_controller.config import loader
        return loader

    def test_copies_package_default_when_missing(self, tmp_path, monkeypatch):
        """Üres CWD: a beépített package data default másolódik settings.json-né."""
        loader = self._import_loader()
        monkeypatch.chdir(tmp_path)

        target = tmp_path / "settings.json"
        assert not target.exists()

        settings = loader.load_settings(str(target))

        assert target.exists(), "settings.json-t létre kellett volna hozni a package default-ból"
        # A package default ftp=200 (lásd schemas.PowerZonesConfig)
        assert settings["power_zones"].ftp == 200

    def test_cwd_default_takes_priority(self, tmp_path, monkeypatch):
        """A CWD-beli settings.default.json elsőbbséget élvez a package data-val szemben."""
        import json
        loader = self._import_loader()
        monkeypatch.chdir(tmp_path)

        # Saját sablon a CWD-ben, eltérő (de érvényes) ftp-vel
        custom = {"power_zones": {"ftp": 300}}
        (tmp_path / "settings.default.json").write_text(
            json.dumps(custom), encoding="utf-8"
        )

        target = tmp_path / "settings.json"
        settings = loader.load_settings(str(target))

        assert target.exists()
        assert settings["power_zones"].ftp == 300, "A CWD-beli sablonból kellett volna töltenie"

    def test_existing_settings_not_overwritten(self, tmp_path, monkeypatch):
        """Meglévő settings.json-t nem írja felül a default."""
        import json
        loader = self._import_loader()
        monkeypatch.chdir(tmp_path)

        target = tmp_path / "settings.json"
        target.write_text(json.dumps({"power_zones": {"ftp": 400}}), encoding="utf-8")

        settings = loader.load_settings(str(target))

        assert settings["power_zones"].ftp == 400, "A meglévő settings.json-t nem szabad felülírni"

    def test_no_copy_onto_self(self, tmp_path, monkeypatch):
        """_ensure_default_settings_file nem másolja a fájlt önmagára."""
        import json
        loader = self._import_loader()
        monkeypatch.chdir(tmp_path)

        # A settings_path maga a CWD-beli settings.default.json
        default_in_cwd = tmp_path / "settings.default.json"
        default_in_cwd.write_text(json.dumps({"power_zones": {"ftp": 250}}), encoding="utf-8")

        # Nem dobhat hibát, és nem ronthatja el a fájlt (self-copy guard)
        loader._ensure_default_settings_file(str(default_in_cwd))

        data = json.loads(default_in_cwd.read_text(encoding="utf-8"))
        assert data == {"power_zones": {"ftp": 250}}

    def test_missing_target_when_no_default_available(self, tmp_path, monkeypatch):
        """Ha sem CWD, sem (elérhetetlen) package default nincs, a hardcoded
        DEFAULT_SETTINGS fallback érvényesül és nem dob hibát."""
        loader = self._import_loader()
        monkeypatch.chdir(tmp_path)

        # A package data elérhetetlenné tétele: a DEFAULT_SETTINGS_PATH-t nem létezőre állítjuk
        monkeypatch.setattr(
            loader, "DEFAULT_SETTINGS_PATH", str(tmp_path / "nincs_ilyen.json")
        )

        target = tmp_path / "settings.json"
        settings = loader.load_settings(str(target))

        # Nincs sablon → nem jött létre fájl, de a hardcoded default visszajött
        assert not target.exists()
        assert settings["power_zones"].ftp == 200


# ============================================================
# Hibás JSON szintaxis → teljes default (ESET B)
# ============================================================

class TestMalformedJsonFallback:
    """Szintaktikailag hibás settings.json → teljes alapértelmezés.

    Ellentétben a mezőnkénti validációval (rossz ÉRTÉK egy mezőben, de
    érvényes JSON → csak az a mező áll defaultra), a hibás JSON SZINTAXIS
    az egész fájlt értelmezhetetlenné teszi, ezért minden szekció a hardcoded
    DEFAULT_SETTINGS-re esik vissza – a fájlban szereplő jó értékek is elvesznek.
    """

    def _import_loader(self):
        from smart_fan_controller.config import loader
        return loader

    def test_missing_comma_full_default(self, tmp_path):
        """Hiányzó vessző → az egész fájl eldobva, teljes default."""
        loader = self._import_loader()
        target = tmp_path / "settings.json"
        # 'ftp': 999 után HIÁNYZIK a vessző – ha mezőnként mentene, ftp=999 lenne
        target.write_text(
            '{\n  "power_zones": {\n    "ftp": 999\n    "min_watt": 10\n  }\n}',
            encoding="utf-8",
        )

        settings = loader.load_settings(str(target))

        # Nem 999, hanem a hardcoded default (200) → az egész fájl eldobva
        assert settings["power_zones"].ftp == 200
        assert settings["power_zones"].min_watt == 0

    def test_missing_closing_brace_full_default(self, tmp_path):
        """Hiányzó záró kapcsos zárójel → teljes default."""
        loader = self._import_loader()
        target = tmp_path / "settings.json"
        target.write_text('{"power_zones": {"ftp": 333}', encoding="utf-8")

        settings = loader.load_settings(str(target))

        assert settings["power_zones"].ftp == 200

    def test_unclosed_string_full_default(self, tmp_path):
        """Lezáratlan idézőjel → teljes default."""
        loader = self._import_loader()
        target = tmp_path / "settings.json"
        target.write_text('{"ble": {"device_name": "Ventilator}}', encoding="utf-8")

        settings = loader.load_settings(str(target))

        assert settings["power_zones"].ftp == 200
        assert settings["ble"].device_name is None

    def test_not_json_at_all_full_default(self, tmp_path):
        """Egyáltalán nem JSON (sima szöveg) → teljes default."""
        loader = self._import_loader()
        target = tmp_path / "settings.json"
        target.write_text("ez nem egy json fajl", encoding="utf-8")

        settings = loader.load_settings(str(target))

        assert settings["power_zones"].ftp == 200

    def test_empty_file_full_default(self, tmp_path):
        """Teljesen üres fájl → teljes default."""
        loader = self._import_loader()
        target = tmp_path / "settings.json"
        target.write_text("", encoding="utf-8")

        settings = loader.load_settings(str(target))

        assert settings["power_zones"].ftp == 200

    def test_trailing_comma_full_default(self, tmp_path):
        """Felesleges záró vessző (JSON-ban nem megengedett) → teljes default."""
        loader = self._import_loader()
        target = tmp_path / "settings.json"
        target.write_text('{"power_zones": {"ftp": 250,}}', encoding="utf-8")

        settings = loader.load_settings(str(target))

        assert settings["power_zones"].ftp == 200

    def test_good_values_lost_when_syntax_broken(self, tmp_path):
        """Megerősítés: a hibás szintaxis miatt a más szekciókban szereplő
        ÉRVÉNYES értékek is elvesznek (nem mentődnek mezőnként)."""
        loader = self._import_loader()
        target = tmp_path / "settings.json"
        # global_settings.cooldown_seconds=60 érvényes lenne, de a power_zones
        # blokkban hiányzó vessző miatt az egész fájl olvashatatlan
        target.write_text(
            '{\n  "global_settings": {"cooldown_seconds": 60},\n'
            '  "power_zones": {"ftp": 250 "min_watt": 5}\n}',
            encoding="utf-8",
        )

        settings = loader.load_settings(str(target))

        # A jó cooldown_seconds=60 is elveszett → default 120
        assert settings["global_settings"].cooldown_seconds == 120
        assert settings["power_zones"].ftp == 200

    def test_incorrect_backup_created(self, tmp_path):
        """Szintaxis-hiba → a hibás fájlról '.incorrect' másolat készül."""
        loader = self._import_loader()
        target = tmp_path / "settings.json"
        broken = '{"power_zones": {"ftp": 285 "min_watt": 15}}'
        target.write_text(broken, encoding="utf-8")

        loader.load_settings(str(target))

        backup = tmp_path / "settings.json.incorrect"
        assert backup.exists(), "A hibás fájlról '.incorrect' másolatot kell készíteni"
        # A másolat a felhasználó eredeti (hibás) tartalmát őrzi meg
        assert backup.read_text(encoding="utf-8") == broken

    def test_incorrect_backup_preserves_manual_edits(self, tmp_path):
        """A '.incorrect' másolat megőrzi a felhasználó kézi szerkesztéseit."""
        loader = self._import_loader()
        target = tmp_path / "settings.json"
        broken = (
            '{\n  "power_zones": {"ftp": 285, "max_watt": 950},\n'
            '  "heart_rate_zones": {"max_hr": 192}\n'  # ← hiányzó vessző
            '  "ble": {"device_name": "MyFan"}\n}'
        )
        target.write_text(broken, encoding="utf-8")

        loader.load_settings(str(target))

        backup = tmp_path / "settings.json.incorrect"
        assert backup.exists()
        # A teljes eredeti tartalom megvan, így a felhasználó kijavíthatja
        assert "285" in backup.read_text(encoding="utf-8")
        assert "MyFan" in backup.read_text(encoding="utf-8")

    def test_incorrect_backup_overwrites_previous(self, tmp_path):
        """Meglévő '.incorrect' másolatot felülír (mindig a legutóbbi hibás)."""
        loader = self._import_loader()
        target = tmp_path / "settings.json"
        backup = tmp_path / "settings.json.incorrect"
        backup.write_text("regi hibas tartalom", encoding="utf-8")

        new_broken = '{"power_zones": {"ftp": 999 bad}}'
        target.write_text(new_broken, encoding="utf-8")

        loader.load_settings(str(target))

        assert backup.read_text(encoding="utf-8") == new_broken

    def test_no_backup_when_valid_json(self, tmp_path):
        """Érvényes JSON esetén NEM készül '.incorrect' másolat."""
        loader = self._import_loader()
        target = tmp_path / "settings.json"
        target.write_text('{"power_zones": {"ftp": 250}}', encoding="utf-8")

        loader.load_settings(str(target))

        backup = tmp_path / "settings.json.incorrect"
        assert not backup.exists(), "Érvényes JSON esetén nem kell biztonsági másolat"


# ============================================================
# Az example sablonok tükrözik a default-ot
# ============================================================

class TestExampleFilesMirrorDefault:
    """A settings.example.json / .jsonc a settings.default.json-t tükrözi.

    Ezek a guard tesztek elkapják, ha a default megváltozik, de az example
    sablonok frissítését elfelejtik – így a dokumentációs sablonok soha nem
    csúsznak el a tényleges default-tól.
    """

    @staticmethod
    def _repo_root():
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    @staticmethod
    def _load_json(path):
        import json
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _strip_comments(obj):
        """Rekurzívan eltávolítja a "_comment*" kulcsokat (csak dokumentáció)."""
        if isinstance(obj, dict):
            return {
                k: TestExampleFilesMirrorDefault._strip_comments(v)
                for k, v in obj.items()
                if not k.startswith("_comment")
            }
        if isinstance(obj, list):
            return [TestExampleFilesMirrorDefault._strip_comments(v) for v in obj]
        return obj

    @staticmethod
    def _parse_jsonc(path):
        """Minimális JSONC → dict: sor-/blokk-kommentek és trailing commák eltávolítása."""
        import json
        import re
        raw = open(path, encoding="utf-8").read()
        raw = re.sub(r"(?m)//.*$", "", raw)            # // sorkommentek
        raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)  # /* */ blokkok
        raw = re.sub(r",(\s*[}\]])", r"\1", raw)         # trailing commák
        return json.loads(raw)

    def test_example_json_equals_default(self):
        """settings.example.json bájtra azonos a default sablonnal."""
        root = self._repo_root()
        default = self._load_json(
            os.path.join(root, "smart_fan_controller", "config", "settings.default.json")
        )
        example = self._load_json(os.path.join(root, "settings.example.json"))
        assert example == default, (
            "settings.example.json elcsúszott a default-tól – frissítsd "
            "(cp smart_fan_controller/config/settings.default.json settings.example.json)"
        )

    def test_example_jsonc_mirrors_default(self):
        """settings.example.jsonc értékei (kommentek nélkül) megegyeznek a default-tal."""
        root = self._repo_root()
        default = self._load_json(
            os.path.join(root, "smart_fan_controller", "config", "settings.default.json")
        )
        jsonc = self._strip_comments(
            self._parse_jsonc(os.path.join(root, "settings.example.jsonc"))
        )
        assert jsonc == default, (
            "settings.example.jsonc értékei elcsúsztak a default-tól – frissítsd "
            "az értékeket (a kommentek maradhatnak)"
        )

    def test_default_json_matches_dataclass_defaults(self):
        """A settings.default.json NYERS tartalma a dataclass default-okat tükrözi.

        Ez a guard elkapja, ha egy dataclass-hoz új mezőt adunk (vagy default-ot
        változtatunk), de a settings.default.json frissítését elfelejtjük – pl.
        ha a HudConfig kap egy új 'save_hud_settings' mezőt, ami kimaradna a sablonból.

        Fontos: a NYERS JSON-t hasonlítjuk (nem a load_settings eredményét), mert
        a from_dict() a hiányzó mezőket automatikusan default-ra töltené, így
        elfedné a fájl-szintű hiányt.
        """
        from smart_fan_controller.config.loader import _settings_to_serializable
        from smart_fan_controller.config.schemas import DEFAULT_SETTINGS

        root = self._repo_root()
        raw = self._load_json(
            os.path.join(root, "smart_fan_controller", "config", "settings.default.json")
        )
        # Amit a program a DEFAULT_SETTINGS-ből a fájlba írna (kulcsok + értékek)
        expected = _settings_to_serializable(DEFAULT_SETTINGS)

        assert raw == expected, (
            "A settings.default.json elcsúszott a dataclass default-októl "
            "(hiányzó/extra mező vagy eltérő érték). Frissítsd a "
            "settings.default.json-t (és a settings.example.json / .jsonc fájlokat)."
        )

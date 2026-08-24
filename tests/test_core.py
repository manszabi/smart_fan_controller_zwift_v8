"""Unit tesztek a core logikához.

Futtatás: pytest tests/ -v
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from unittest.mock import patch

import pytest

from smart_fan_controller.core import resolve_log_dir as _resolve_log_dir
from smart_fan_controller.handlers._ble import _log_ble_devices_to_file
from zwift_fan_controller import (
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
)
# A loggolás-infrastruktúra a smart_fan_controller.core.logging_setup modulba
# került. A régi alulvonásos neveket aliasként importáljuk a tesztek
# visszafelé kompatibilitásáért; a modulszintű állapotot a _logmod-on át érjük el.
from smart_fan_controller.core import (
    setup_logging as _setup_logging,
    setup_early_logging as _setup_early_logging,
    flush_early_logging as _flush_early_logging,
    discard_early_logging as _discard_early_logging,
)
from smart_fan_controller.core import logging_setup as _logmod
import logging as _logging
import json as _json
from smart_fan_controller.zwift_api import logsetup as _zaplog


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
        assert cfg.buffer_seconds == 3
        assert cfg.minimum_samples == 6
        assert cfg.buffer_rate_hz == 4
        assert cfg.dropout_timeout == 5
        assert cfg.logging is True
        assert cfg.log_directory is None

    # --- logging field tests ---
    def test_logging_default_true(self):
        assert GlobalSettingsConfig().logging is True

    def test_from_dict_logging_false(self):
        cfg = GlobalSettingsConfig.from_dict({"logging": False})
        assert cfg.logging is False

    def test_from_dict_logging_true(self):
        cfg = GlobalSettingsConfig.from_dict({"logging": True})
        assert cfg.logging is True

    def test_from_dict_logging_invalid_type(self):
        """logging: rossz típus (nem bool) → default (True) marad."""
        cfg = GlobalSettingsConfig.from_dict({"logging": "yes"})
        assert cfg.logging is True
        cfg = GlobalSettingsConfig.from_dict({"logging": 1})
        assert cfg.logging is True

    def test_from_dict(self):
        cfg = GlobalSettingsConfig.from_dict({
            "cooldown_seconds": 60,
            "buffer_seconds": 5,
            "minimum_samples": 10,
            "buffer_rate_hz": 2,
            "dropout_timeout": 10,
        })
        assert cfg.cooldown_seconds == 60
        assert cfg.buffer_seconds == 5
        assert cfg.minimum_samples == 10
        assert cfg.buffer_rate_hz == 2
        assert cfg.dropout_timeout == 10

    # --- cooldown_seconds field tests ---
    def test_cooldown_seconds_valid_boundaries(self):
        """cooldown_seconds: valid határértékek 0–600 (0 = azonnali váltás)."""
        cfg = GlobalSettingsConfig.from_dict({"cooldown_seconds": 0})
        assert cfg.cooldown_seconds == 0
        cfg = GlobalSettingsConfig.from_dict({"cooldown_seconds": 600})
        assert cfg.cooldown_seconds == 600

    def test_cooldown_seconds_invalid_range(self):
        """cooldown_seconds: -1 vagy 601 → default marad."""
        cfg = GlobalSettingsConfig.from_dict({"cooldown_seconds": -1})
        assert cfg.cooldown_seconds == 120
        cfg = GlobalSettingsConfig.from_dict({"cooldown_seconds": 601})
        assert cfg.cooldown_seconds == 120

    def test_cooldown_seconds_float_integer_accepted(self):
        """cooldown_seconds: 120.0 (egész float) → konvertálva 120-ra."""
        cfg = GlobalSettingsConfig.from_dict({"cooldown_seconds": 120.0})
        assert cfg.cooldown_seconds == 120

    def test_cooldown_seconds_float_fraction_rejected(self):
        """cooldown_seconds: 120.7 (törtrész) → default marad."""
        cfg = GlobalSettingsConfig.from_dict({"cooldown_seconds": 120.7})
        assert cfg.cooldown_seconds == 120

    def test_cooldown_seconds_bool_rejected(self):
        """cooldown_seconds: True → default marad."""
        cfg = GlobalSettingsConfig.from_dict({"cooldown_seconds": True})
        assert cfg.cooldown_seconds == 120

    # --- buffer_seconds field tests ---
    def test_buffer_seconds_valid_boundaries(self):
        """buffer_seconds: valid határértékek 1–60."""
        cfg = GlobalSettingsConfig.from_dict({"buffer_seconds": 1})
        assert cfg.buffer_seconds == 1
        cfg = GlobalSettingsConfig.from_dict({"buffer_seconds": 60})
        assert cfg.buffer_seconds == 60

    def test_buffer_seconds_invalid_range(self):
        """buffer_seconds: 0 vagy 61 → default marad."""
        cfg = GlobalSettingsConfig.from_dict({"buffer_seconds": 0})
        assert cfg.buffer_seconds == 3
        cfg = GlobalSettingsConfig.from_dict({"buffer_seconds": 61})
        assert cfg.buffer_seconds == 3

    def test_buffer_seconds_float_fraction_rejected(self):
        """buffer_seconds: 5.5 (törtrész) → default marad."""
        cfg = GlobalSettingsConfig.from_dict({"buffer_seconds": 5.5})
        assert cfg.buffer_seconds == 3

    # --- buffer_rate_hz field tests ---
    def test_buffer_rate_hz_valid_boundaries(self):
        """buffer_rate_hz: valid határértékek 1–60."""
        cfg = GlobalSettingsConfig.from_dict({"buffer_rate_hz": 1})
        assert cfg.buffer_rate_hz == 1
        cfg = GlobalSettingsConfig.from_dict({"buffer_rate_hz": 60})
        assert cfg.buffer_rate_hz == 60

    def test_buffer_rate_hz_invalid_range(self):
        """buffer_rate_hz: 0 vagy 61 → default marad."""
        cfg = GlobalSettingsConfig.from_dict({"buffer_rate_hz": 0})
        assert cfg.buffer_rate_hz == 4
        cfg = GlobalSettingsConfig.from_dict({"buffer_rate_hz": 61})
        assert cfg.buffer_rate_hz == 4

    def test_buffer_rate_hz_float_fraction_rejected(self):
        """buffer_rate_hz: 4.5 (törtrész) → default marad."""
        cfg = GlobalSettingsConfig.from_dict({"buffer_rate_hz": 4.5})
        assert cfg.buffer_rate_hz == 4

    # --- minimum_samples field tests ---
    def test_minimum_samples_valid_boundaries(self):
        """minimum_samples: valid határértékek 1–600 (cross-validation nélkül)."""
        cfg = GlobalSettingsConfig.from_dict({"minimum_samples": 1})
        assert cfg.minimum_samples == 1
        # 600 samples: buffer_seconds=10, buffer_rate_hz=60 → max=600 (cross-validation OK)
        cfg = GlobalSettingsConfig.from_dict({
            "minimum_samples": 600,
            "buffer_seconds": 10,
            "buffer_rate_hz": 60,
        })
        assert cfg.minimum_samples == 600

    def test_minimum_samples_invalid_range(self):
        """minimum_samples: 0 vagy 601 → default marad."""
        cfg = GlobalSettingsConfig.from_dict({"minimum_samples": 0})
        assert cfg.minimum_samples == 6
        cfg = GlobalSettingsConfig.from_dict({"minimum_samples": 601})
        assert cfg.minimum_samples == 6

    def test_minimum_samples_float_fraction_rejected(self):
        """minimum_samples: 6.5 (törtrész) → default marad."""
        cfg = GlobalSettingsConfig.from_dict({"minimum_samples": 6.5})
        assert cfg.minimum_samples == 6

    # --- dropout_timeout field tests ---
    def test_dropout_timeout_valid_boundaries(self):
        """dropout_timeout: valid határértékek 1–120."""
        cfg = GlobalSettingsConfig.from_dict({"dropout_timeout": 1})
        assert cfg.dropout_timeout == 1
        cfg = GlobalSettingsConfig.from_dict({"dropout_timeout": 120})
        assert cfg.dropout_timeout == 120

    def test_dropout_timeout_invalid_range(self):
        """dropout_timeout: 0 vagy 301 → default marad (érvényes: 1–300)."""
        cfg = GlobalSettingsConfig.from_dict({"dropout_timeout": 0})
        assert cfg.dropout_timeout == 5
        cfg = GlobalSettingsConfig.from_dict({"dropout_timeout": 301})
        assert cfg.dropout_timeout == 5

    def test_dropout_timeout_extended_range(self):
        """dropout_timeout: 300 (max, egységes a forrás-specifikussal) → elfogadva."""
        cfg = GlobalSettingsConfig.from_dict({"dropout_timeout": 300})
        assert cfg.dropout_timeout == 300

    def test_dropout_timeout_float_fraction_rejected(self):
        """dropout_timeout: 10.3 (törtrész) → default marad."""
        cfg = GlobalSettingsConfig.from_dict({"dropout_timeout": 10.3})
        assert cfg.dropout_timeout == 5

    # --- Cross-validation: minimum_samples <= buffer_seconds * buffer_rate_hz ---
    def test_post_init_minimum_samples_exceeds_max(self):
        """minimum_samples > buffer_seconds * buffer_rate_hz → minimum_samples korrigálva."""
        # buffer_seconds=2, buffer_rate_hz=3 → max 6, de minimum_samples=10 → 6-ra korrigálva
        cfg = GlobalSettingsConfig(
            buffer_seconds=2,
            buffer_rate_hz=3,
            minimum_samples=10,
        )
        assert cfg.minimum_samples == 6

    def test_post_init_minimum_samples_valid(self):
        """minimum_samples <= buffer_seconds * buffer_rate_hz → nincs korrekció."""
        cfg = GlobalSettingsConfig(
            buffer_seconds=3,
            buffer_rate_hz=4,
            minimum_samples=12,
        )
        assert cfg.minimum_samples == 12

    def test_post_init_minimum_samples_exact_boundary(self):
        """minimum_samples == buffer_seconds * buffer_rate_hz → valid."""
        cfg = GlobalSettingsConfig(
            buffer_seconds=2,
            buffer_rate_hz=5,
            minimum_samples=10,
        )
        assert cfg.minimum_samples == 10

    def test_post_init_from_dict_cross_validation(self):
        """from_dict + __post_init__: cross-validation is triggered."""
        cfg = GlobalSettingsConfig.from_dict({
            "buffer_seconds": 3,
            "buffer_rate_hz": 2,
            "minimum_samples": 100,  # max = 3*2=6, 100 > 6 → korrekció
        })
        assert cfg.minimum_samples == 6

    # --- log_directory field tests ---
    def test_from_dict_log_directory_null(self):
        cfg = GlobalSettingsConfig.from_dict({"log_directory": None})
        assert cfg.log_directory is None

    def test_from_dict_log_directory_string(self):
        cfg = GlobalSettingsConfig.from_dict({"log_directory": "/tmp/logs"})
        assert cfg.log_directory == "/tmp/logs"

    def test_from_dict_log_directory_null_string(self):
        """A "null" string (gyakori elgépelés) → None, csendben."""
        cfg = GlobalSettingsConfig.from_dict({"log_directory": "null"})
        assert cfg.log_directory is None

    def test_from_dict_log_directory_null_string_case_insensitive(self):
        """A "NULL" / " null " → None (case-insensitive, trimmelt)."""
        assert GlobalSettingsConfig.from_dict({"log_directory": "NULL"}).log_directory is None
        assert GlobalSettingsConfig.from_dict({"log_directory": " null "}).log_directory is None

    def test_from_dict_log_directory_null_string_silent(self, caplog):
        """A "null" string NEM ad figyelmeztetést (csendes default)."""
        import logging
        with caplog.at_level(logging.WARNING, logger="user"):
            GlobalSettingsConfig.from_dict({"log_directory": "null"})
        assert not any("log_directory" in r.message for r in caplog.records)

    def test_from_dict_log_directory_missing_silent(self, caplog):
        """Hiányzó kulcs → None, csendben (nincs figyelmeztetés)."""
        import logging
        with caplog.at_level(logging.WARNING, logger="user"):
            cfg = GlobalSettingsConfig.from_dict({})
        assert cfg.log_directory is None
        assert not any("log_directory" in r.message for r in caplog.records)

    def test_from_dict_log_directory_null_silent(self, caplog):
        """null (None) → None, csendben (nincs figyelmeztetés)."""
        import logging
        with caplog.at_level(logging.WARNING, logger="user"):
            GlobalSettingsConfig.from_dict({"log_directory": None})
        assert not any("log_directory" in r.message for r in caplog.records)

    def test_from_dict_log_directory_empty_string(self):
        """Üres string → None (default) + warning."""
        cfg = GlobalSettingsConfig.from_dict({"log_directory": "   "})
        assert cfg.log_directory is None

    def test_from_dict_log_directory_empty_string_warns(self, caplog):
        """Üres string → figyelmeztetés a logban."""
        import logging
        with caplog.at_level(logging.WARNING, logger="user"):
            GlobalSettingsConfig.from_dict({"log_directory": ""})
        assert any("log_directory" in r.message for r in caplog.records)

    def test_from_dict_log_directory_whitespace_stripped(self):
        """Whitespace-vel körülzárt string → trimmed."""
        cfg = GlobalSettingsConfig.from_dict({"log_directory": "  /var/log  "})
        assert cfg.log_directory == "/var/log"

    def test_from_dict_log_directory_wrong_type_int(self):
        """Rossz típus (int) → None (default)."""
        cfg = GlobalSettingsConfig.from_dict({"log_directory": 123})
        assert cfg.log_directory is None

    def test_from_dict_log_directory_wrong_type_bool(self):
        """Rossz típus (bool) → None (default)."""
        cfg = GlobalSettingsConfig.from_dict({"log_directory": True})
        assert cfg.log_directory is None

    def test_from_dict_log_directory_wrong_type_list(self):
        """Rossz típus (lista) → None (default)."""
        cfg = GlobalSettingsConfig.from_dict({"log_directory": ["/tmp"]})
        assert cfg.log_directory is None

    def test_from_dict_log_directory_wrong_type_warns(self, caplog):
        """Rossz típus → figyelmeztetés a logban."""
        import logging
        with caplog.at_level(logging.WARNING, logger="user"):
            GlobalSettingsConfig.from_dict({"log_directory": 123})
        assert any("log_directory" in r.message for r in caplog.records)


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
        """z1 >= z2 → default visszaállítás (Power-rel konzisztens)."""
        cfg = HeartRateZonesConfig(z1_max_percent=90, z2_max_percent=70)
        assert cfg.z1_max_percent == 70  # default
        assert cfg.z2_max_percent == 80  # default
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

    @pytest.mark.parametrize("bad", ["banana", "", "POWER_ONLY", 5, None, True])
    def test_from_dict_invalid_zone_mode_warns(self, bad, caplog):
        """Bármilyen el nem fogadott zone_mode (elírás/szám/üres) → figyelmeztetés + default."""
        import logging
        with caplog.at_level(logging.WARNING, logger="user"):
            cfg = HeartRateZonesConfig.from_dict({"zone_mode": bad})
        assert cfg.zone_mode == ZoneMode.HIGHER_WINS
        assert any("zone_mode" in r.message for r in caplog.records)

    def test_from_dict_bool_fields(self):
        cfg = HeartRateZonesConfig.from_dict({
            "enabled": False, "zero_hr_immediate": True
        })
        assert cfg.enabled is False
        assert cfg.zero_hr_immediate is True

    @pytest.mark.parametrize("field,bad", [
        ("enabled", 1), ("enabled", "true"), ("enabled", None),
        ("zero_hr_immediate", 0), ("zero_hr_immediate", "false"), ("zero_hr_immediate", []),
    ])
    def test_from_dict_bool_fields_invalid(self, field, bad, caplog):
        """Bool mezők hibás értékei (szám/string/null) → figyelmeztetés + default."""
        import logging
        with caplog.at_level(logging.WARNING, logger="user"):
            cfg = HeartRateZonesConfig.from_dict({field: bad})
        assert cfg.enabled is True  # enabled default True
        assert cfg.zero_hr_immediate is False  # zero_hr_immediate default False
        assert any(field in r.message for r in caplog.records)

    def test_from_dict_float_integer_accepted(self):
        """Float int fields: 190.0 (egész float) → konvertálva."""
        cfg = HeartRateZonesConfig.from_dict({"max_hr": 190.0})
        assert cfg.max_hr == 190

    def test_from_dict_float_fraction_rejected(self):
        """Float int fields: 190.5 (törtrész) → default marad."""
        cfg = HeartRateZonesConfig.from_dict({"max_hr": 190.5})
        assert cfg.max_hr == 185


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

    @pytest.mark.parametrize("val", ["null", "NULL", "  null  ", "none", "None", "NONE"])
    def test_from_dict_device_name_null_string(self, val, caplog):
        """"null"/"none" string (kis-nagybetű érzéketlen) → None, csendben."""
        import logging
        with caplog.at_level(logging.WARNING, logger="user"):
            cfg = BleConfig.from_dict({"device_name": val})
        assert cfg.device_name is None
        assert not any("device_name" in r.message for r in caplog.records)

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

    def test_from_dict_float_integer_accepted(self):
        """Float int fields: 30.0 (egész float) → konvertálva."""
        cfg = BleConfig.from_dict({"scan_timeout": 30.0})
        assert cfg.scan_timeout == 30

    def test_from_dict_float_fraction_rejected(self):
        """Float int fields: 30.5 (törtrész) → default marad."""
        cfg = BleConfig.from_dict({"scan_timeout": 30.5})
        assert cfg.scan_timeout == 10

    @pytest.mark.parametrize("bad", [123, 1.5, [], {"x": 1}, True])
    def test_from_dict_device_name_wrong_type_warns(self, bad, caplog):
        """device_name rossz típus (nem string/null) → figyelmeztetés + default None."""
        import logging
        with caplog.at_level(logging.WARNING, logger="user"):
            cfg = BleConfig.from_dict({"device_name": bad})
        assert cfg.device_name is None
        assert any("device_name" in r.message for r in caplog.records)

    @pytest.mark.parametrize("key", ["service_uuid", "characteristic_uuid"])
    @pytest.mark.parametrize("bad", ["", "   ", None, 123, []])
    def test_from_dict_uuid_invalid_warns(self, key, bad, caplog):
        """UUID üres/rossz típus → figyelmeztetés + default marad."""
        import logging
        default = getattr(BleConfig(), key)
        with caplog.at_level(logging.WARNING, logger="user"):
            cfg = BleConfig.from_dict({key: bad})
        assert getattr(cfg, key) == default
        assert any(key in r.message for r in caplog.records)

    def test_from_dict_uuid_stripped(self):
        """UUID körüli whitespace levágva."""
        cfg = BleConfig.from_dict({"service_uuid": "  abcd-1234  "})
        assert cfg.service_uuid == "abcd-1234"


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

    @pytest.mark.parametrize("key", ["power_source", "hr_source"])
    @pytest.mark.parametrize("bad", ["banana", 5, "", True])
    def test_from_dict_invalid_source_warns(self, key, bad, caplog):
        """Érvénytelen power/hr forrás → figyelmeztetés + default marad."""
        import logging
        with caplog.at_level(logging.WARNING, logger="user"):
            cfg = DatasourceConfig.from_dict({key: bad})
        assert getattr(cfg, key) == DataSource.ZWIFTUDP
        assert any(key in r.message for r in caplog.records)

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

    def test_from_dict_ble_device_names(self):
        cfg = DatasourceConfig.from_dict({
            "ble_power_device_name": "  PowerMeter  ",
            "ble_hr_device_name": "HRStrap",
        })
        assert cfg.ble_power_device_name == "PowerMeter"  # trimmed
        assert cfg.ble_hr_device_name == "HRStrap"

    @pytest.mark.parametrize("val", [None, "", "  ", "null", "NONE"])
    def test_from_dict_ble_device_name_null_like(self, val):
        """null/""/"null"/"none" → None (auto-discovery)."""
        cfg = DatasourceConfig.from_dict({"ble_power_device_name": val})
        assert cfg.ble_power_device_name is None

    def test_from_dict_ble_device_name_wrong_type_warns(self, caplog):
        """Rossz típus → figyelmeztetés + default None."""
        import logging
        with caplog.at_level(logging.WARNING, logger="user"):
            cfg = DatasourceConfig.from_dict({"ble_power_device_name": 123})
        assert cfg.ble_power_device_name is None
        assert any("ble_power_device_name" in r.message for r in caplog.records)

    @pytest.mark.parametrize("bad", ["", "   ", 5, None, True])
    def test_from_dict_zwift_udp_host_invalid_warns(self, bad, caplog):
        """zwift_udp_host üres/rossz típus → figyelmeztetés + default marad."""
        import logging
        with caplog.at_level(logging.WARNING, logger="user"):
            cfg = DatasourceConfig.from_dict({"zwift_udp_host": bad})
        assert cfg.zwift_udp_host == "127.0.0.1"
        assert any("zwift_udp_host" in r.message for r in caplog.records)

    def test_from_dict_zwift_udp_host_stripped(self):
        """zwift_udp_host körüli whitespace levágva."""
        cfg = DatasourceConfig.from_dict({"zwift_udp_host": "  10.0.0.5  "})
        assert cfg.zwift_udp_host == "10.0.0.5"

    @pytest.mark.parametrize("bad", ["yes", 1, 0, None, "true"])
    def test_from_dict_zwift_auto_launch_invalid_warns(self, bad, caplog):
        """zwift_auto_launch nem-bool → figyelmeztetés + default marad."""
        import logging
        with caplog.at_level(logging.WARNING, logger="user"):
            cfg = DatasourceConfig.from_dict({"zwift_auto_launch": bad})
        assert cfg.zwift_auto_launch is True
        assert any("zwift_auto_launch" in r.message for r in caplog.records)

    @pytest.mark.parametrize("val", [None, "", "   ", "null", "NULL", "none", "None"])
    def test_from_dict_zwift_launcher_path_none_like(self, val):
        """null/""/"null"/"none"/whitespace → None (automatikus keresés)."""
        cfg = DatasourceConfig.from_dict({"zwift_launcher_path": val})
        assert cfg.zwift_launcher_path is None

    def test_from_dict_zwift_launcher_path_valid(self):
        cfg = DatasourceConfig.from_dict({"zwift_launcher_path": "  C:/Zwift/ZwiftLauncher.exe  "})
        assert cfg.zwift_launcher_path == "C:/Zwift/ZwiftLauncher.exe"

    def test_from_dict_zwift_launcher_path_wrong_type_warns(self, caplog):
        """zwift_launcher_path rossz típus → figyelmeztetés + default None."""
        import logging
        with caplog.at_level(logging.WARNING, logger="user"):
            cfg = DatasourceConfig.from_dict({"zwift_launcher_path": 5})
        assert cfg.zwift_launcher_path is None
        assert any("zwift_launcher_path" in r.message for r in caplog.records)


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

    def test_to_dict_uses_current_key(self):
        """to_dict() az AKTUÁLIS kulcsnevet írja (nem a régi pontosat).

        Regresszió: a régi kulcsnév kiírása minden HUD-mentésnél átnevezte
        a felhasználó `close_at_zwiftapp_exe` kulcsát a legacy nevére –
        miközben a from_dict() épp az aláhúzásosat preferálja."""
        d = HudConfig().to_dict()
        assert "close_at_zwiftapp_exe" in d
        assert d["close_at_zwiftapp_exe"] is True
        assert "close_at_zwiftapp.exe" not in d

    def test_from_dict_still_reads_legacy_key(self):
        """A régi (pontos) kulcsnevet továbbra is beolvassa – régi settings.json."""
        cfg = HudConfig.from_dict({"close_at_zwiftapp.exe": False})
        assert cfg.close_at_zwiftapp_exe is False

    def test_to_dict_round_trip_keeps_value(self):
        """to_dict() → from_dict() körben a beállítás megmarad."""
        cfg = HudConfig(close_at_zwiftapp_exe=False)
        assert HudConfig.from_dict(cfg.to_dict()).close_at_zwiftapp_exe is False

    def test_from_dict_volume(self):
        cfg = HudConfig.from_dict({"sound_volume": 0.8})
        assert cfg.sound_volume == 0.8

    def test_from_dict_int_volume(self):
        """Int volume → float-ra konvertálva."""
        cfg = HudConfig.from_dict({"sound_volume": 1})
        assert cfg.sound_volume == 1.0
        assert isinstance(cfg.sound_volume, float)

    @pytest.mark.parametrize("val,expected", [(0.0, 0.0), (1.0, 1.0), (0.3, 0.3)])
    def test_from_dict_volume_boundary(self, val, expected):
        """sound_volume határértékek (0.0–1.0) elfogadva."""
        cfg = HudConfig.from_dict({"sound_volume": val})
        assert cfg.sound_volume == expected

    @pytest.mark.parametrize("bad", [5.0, -3.0, 1.5, -0.1, "loud", True])
    def test_from_dict_volume_invalid_warns(self, bad, caplog):
        """sound_volume tartományon kívül / rossz típus → figyelmeztetés + default."""
        import logging
        with caplog.at_level(logging.WARNING, logger="user"):
            cfg = HudConfig.from_dict({"sound_volume": bad})
        assert cfg.sound_volume == 0.5  # default
        assert any("sound_volume" in r.message for r in caplog.records)

    @pytest.mark.parametrize("key,default", [
        ("save_hud_settings", False), ("sound_enabled", True), ("close_at_zwiftapp_exe", True),
    ])
    @pytest.mark.parametrize("bad", ["yes", 1, 0, None])
    def test_from_dict_bool_invalid_warns(self, key, default, bad, caplog):
        """HUD bool mezők rossz típusa → figyelmeztetés + default marad."""
        import logging
        with caplog.at_level(logging.WARNING, logger="user"):
            cfg = HudConfig.from_dict({key: bad})
        assert getattr(cfg, key) is default
        assert any(key in r.message for r in caplog.records)

    def test_from_dict_legacy_key_invalid_warns(self, caplog):
        """A régi 'close_at_zwiftapp.exe' kulcs rossz típusa → figyelmeztetés."""
        import logging
        with caplog.at_level(logging.WARNING, logger="user"):
            cfg = HudConfig.from_dict({"close_at_zwiftapp.exe": "igen"})
        assert cfg.close_at_zwiftapp_exe is True  # default
        assert any("close_at_zwiftapp.exe" in r.message for r in caplog.records)

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

    def test_from_dict_opacity_float_integer_accepted(self):
        """Opacity: 75.0 (egész float) → konvertálva."""
        cfg = HudConfig.from_dict({"opacity": 75.0})
        assert cfg.opacity == 75

    def test_from_dict_opacity_float_fraction_rejected(self):
        """Opacity: 75.5 (törtrész) → default marad."""
        cfg = HudConfig.from_dict({"opacity": 75.5})
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

class TestBluetoothUnavailableHint:
    """Kikapcsolt Bluetooth: célzott, cselekvésre váltható üzenet.

    A bleak 2.0 külön kivételt dob (BleakBluetoothNotAvailableError), a
    régebbi csak általános hibát – mindkettőt felismerjük, és az ANT+
    libusb-tipphez hasonló egyszeri útmutatót adunk a nyers hibaszöveg
    helyett."""

    def _reset_hint(self, monkeypatch):
        import smart_fan_controller.handlers._ble as ble
        monkeypatch.setattr(ble, "_bt_hint_shown", False)
        return ble

    def test_message_text_recognized(self, monkeypatch, caplog):
        ble = self._reset_hint(monkeypatch)
        with caplog.at_level("WARNING", logger="user"):
            handled = ble._warn_if_bluetooth_unavailable(
                RuntimeError("No powered Bluetooth adapters found"), "BLE Fan"
            )
        assert handled is True
        assert any("Bluetooth" in rec.message for rec in caplog.records)

    def test_hint_shown_only_once(self, monkeypatch, caplog):
        ble = self._reset_hint(monkeypatch)
        with caplog.at_level("WARNING", logger="user"):
            ble._warn_if_bluetooth_unavailable(RuntimeError("bluetooth off"), "BLE Fan")
            ble._warn_if_bluetooth_unavailable(RuntimeError("bluetooth off"), "BLE HR")
        hints = [r for r in caplog.records if "kapcsold be" in r.message]
        assert len(hints) == 1

    def test_unrelated_error_not_swallowed(self, monkeypatch):
        ble = self._reset_hint(monkeypatch)
        assert ble._warn_if_bluetooth_unavailable(
            RuntimeError("write timeout"), "BLE Fan"
        ) is False


class TestAntNodeReleaseOnInitFailure:
    """Node-init hiba esetén a félkész ANT+ node is leáll.

    Regresszió: a Node() már lefoglalta az USB sticket, de ha a
    set_network_key()/eszköz-regisztráció elhasalt, a node sosem került a
    lock alá – így a _stop_node() már None-t talált, és a nyitott USB
    handle bennragadt. Ez maga okozza a következő próba "could not claim
    interface (resource busy)" hibáját: a hiba önmagát táplálta."""

    def _handler(self, monkeypatch, node_cls):
        import asyncio
        import smart_fan_controller.handlers._ant as ant
        from smart_fan_controller.config.schemas import DEFAULT_SETTINGS, DataSource
        import copy

        class _FakeDevice:
            def __init__(self, _node, device_id=0):
                self.device_id = device_id
                self.name = "fake"
                self.closed = False

            def close_channel(self):
                self.closed = True

        monkeypatch.setattr(ant, "_ANTPLUS_AVAILABLE", True)
        monkeypatch.setattr(ant, "Node", node_cls)
        monkeypatch.setattr(ant, "ANTPLUS_NETWORK_KEY", b"\x00" * 8)
        monkeypatch.setattr(ant, "PowerMeter", _FakeDevice)
        monkeypatch.setattr(ant, "HeartRate", _FakeDevice)

        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["datasource"].power_source = DataSource.ANTPLUS
        return ant.ANTPlusInputHandler(
            settings, asyncio.Queue(), asyncio.Queue(), asyncio.new_event_loop()
        )

    def test_failed_init_stops_the_half_built_node(self, monkeypatch):
        created, stopped = [], []

        class FailingNode:
            def __init__(self):
                created.append(self)

            def set_network_key(self, *_a):
                raise RuntimeError("could not claim interface (resource busy)")

            def stop(self):
                stopped.append(self)

        handler = self._handler(monkeypatch, FailingNode)
        for _ in range(3):
            with pytest.raises(RuntimeError):
                handler._init_node()
            handler._stop_node()   # amit a _thread_loop is hív a ciklus végén

        assert len(created) == 3
        assert len(stopped) == 3, "a hibás node-ok USB handle-je bennragadt"

    def test_successful_init_publishes_node(self, monkeypatch):
        class OkNode:
            def __init__(self):
                self.stopped = False

            def set_network_key(self, *_a):
                pass

            def stop(self):
                self.stopped = True

        handler = self._handler(monkeypatch, OkNode)
        handler._init_node()
        assert handler._node is not None       # sikeres init → publikálva
        node = handler._node
        handler._stop_node()
        assert node.stopped is True
        assert handler._node is None


class TestSaveHudSettingsOnlyRobustness:
    """A HUD-mentés nem dobhat kivételt hibás settings.json-re.

    Regresszió: a nem-objektum (pl. lista) tartalmú fájlnál a
    data["hud"] = ... TypeError-t dobott – és ez a HUD debounce-olt
    automata mentéséből, azaz a Qt eseményhurokból csapódott ki."""

    def test_non_object_settings_file_returns_false(self, tmp_path):
        import json
        from smart_fan_controller.config import loader
        from smart_fan_controller.config.schemas import HudConfig

        target = tmp_path / "settings.json"
        target.write_text(json.dumps(["nem", "objektum"]), encoding="utf-8")

        assert loader.save_hud_settings_only(
            str(target), HudConfig(save_hud_settings=True)
        ) is False
        # A fájl érintetlen marad
        assert json.loads(target.read_text(encoding="utf-8")) == ["nem", "objektum"]


class TestWrongTypedSectionWarns:
    """Rossz TÍPUSÚ szekció (pl. "power_zones": 42) figyelmeztetést kap.

    Az elgépelt szekció-NÉV eddig is figyelmeztetett, a rossz típusú érték
    viszont némán az alapértelmezésre esett vissza – a felhasználó teljes
    szekciója veszett el szó nélkül."""

    def test_non_dict_section_logs_warning(self, tmp_path, caplog):
        import json
        from smart_fan_controller.config.loader import load_settings

        target = tmp_path / "settings.json"
        target.write_text(json.dumps({"power_zones": 42}), encoding="utf-8")

        with caplog.at_level("WARNING", logger="user"):
            settings = load_settings(str(target))

        assert settings["power_zones"].ftp == 200        # default marad
        assert any("power_zones" in rec.message and "nem beállítás-objektum" in rec.message
                   for rec in caplog.records)


class TestResolveLogDir:
    """Log könyvtár feloldás és validálás."""

    @property
    def _default_dir(self) -> str:
        """A tiszta függvény default könyvtára (CWD, ha nincs default_dir)."""
        return os.getcwd()

    def test_none_returns_default(self):
        assert _resolve_log_dir(None) == self._default_dir

    def test_empty_returns_default(self):
        assert _resolve_log_dir("") == self._default_dir

    def test_explicit_default_dir(self):
        """default_dir átadása felülírja a CWD fallback-et."""
        tmp = tempfile.mkdtemp()
        try:
            assert _resolve_log_dir(None, default_dir=tmp) == tmp
            assert _resolve_log_dir("", default_dir=tmp) == tmp
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

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
        """Nem létrehozható útvonal (fájl alatti aldir) → fallback.

        Platformfüggetlen: Windowson a /proc/... létrehozható lenne
        (C:\\proc), ezért egy valódi fájl ALÁ próbálunk könyvtárat tenni,
        ami minden OS-en OSError."""
        fd, fpath = tempfile.mkstemp()
        os.close(fd)
        try:
            result = _resolve_log_dir(os.path.join(fpath, "sub"))
            assert result == self._default_dir
        finally:
            os.remove(fpath)

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
# Loggolás be/ki (logging flag) – _setup_logging viselkedés
# ============================================================

class TestLoggingToggle:
    """A global_settings.logging flag és a korai pufferelés viselkedése."""

    def _reset(self):
        """Loggerek semleges alaphelyzetbe (a tesztek közti szennyezés ellen).

        A handlerek törlése mellett a propagate-et is visszaállítja True-ra és
        a szintet NOTSET-re, hogy a később futó caplog-os tesztek (amik a
        propagáláson keresztül kapják el a 'user' logger üzeneteit) ne
        sérüljenek a futási sorrendtől függetlenül.
        """
        for name in ("user", "zwift_fan_controller_new"):
            lg = _logging.getLogger(name)
            lg.handlers.clear()
            lg.propagate = True
            lg.setLevel(_logging.NOTSET)
        _logmod._logging_enabled = True
        _logmod._early_mem_handlers = []

    def test_disabled_uses_nullhandler(self):
        """logging:false → mindkét logger NullHandler-t kap."""
        self._reset()
        tmp = tempfile.mkdtemp()
        try:
            _setup_logging(tmp, logging_enabled=False)
            assert _logmod._logging_enabled is False
            for name in ("user", "zwift_fan_controller_new"):
                handlers = _logging.getLogger(name).handlers
                assert len(handlers) == 1
                assert isinstance(handlers[0], _logging.NullHandler)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_disabled_no_log_file_created(self):
        """logging:false → nem jön létre smart_fan_controller.log."""
        self._reset()
        tmp = tempfile.mkdtemp()
        try:
            _setup_logging(tmp, logging_enabled=False)
            _logging.getLogger("user").warning("ne kerüljön fájlba")
            assert not os.path.exists(os.path.join(tmp, "smart_fan_controller.log"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_enabled_creates_log_file(self):
        """logging:true → létrejön a log fájl és tartalmazza az üzenetet."""
        self._reset()
        tmp = tempfile.mkdtemp()
        try:
            _setup_logging(tmp, logging_enabled=True)
            assert _logmod._logging_enabled is True
            _logging.getLogger("user").warning("EZ_BEKERÜL")
            logf = os.path.join(tmp, "smart_fan_controller.log")
            assert os.path.exists(logf)
            assert "EZ_BEKERÜL" in open(logf, encoding="utf-8").read()
        finally:
            self._reset()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_device_logs_gated_when_disabled(self):
        """logging:false → ble eszköz-fájl sem jön létre.

        A log_dir és a logging_enabled paraméterként megy a függvénynek,
        ezért nincs szükség modulszintű állapot beállítására.
        """
        self._reset()
        tmp = tempfile.mkdtemp()
        try:
            _log_ble_devices_to_file([("Fan", "AA:BB", ["uuid"])], "BLE Fan", tmp, False)
            assert not os.path.exists(os.path.join(tmp, "ble_devices.log"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_device_logs_written_when_enabled(self):
        """logging:true → ble eszköz-fájl létrejön."""
        self._reset()
        tmp = tempfile.mkdtemp()
        try:
            _log_ble_devices_to_file([("Fan", "AA:BB", ["uuid"])], "BLE Fan", tmp, True)
            _log_ble_devices_to_file([("Fan", "AA:BB", ["uuid"])], "BLE Fan", tmp, True)
            assert os.path.exists(os.path.join(tmp, "ble_devices.log"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_early_buffer_flush_replays_to_file(self):
        """logging:true → korai (betöltés előtti) logok visszajátszódnak a fájlba."""
        self._reset()
        tmp = tempfile.mkdtemp()
        try:
            _setup_early_logging()
            _logging.getLogger("user").warning("KORAI_WARNING")
            # még nincs fájl, csak pufferben
            assert not os.path.exists(os.path.join(tmp, "smart_fan_controller.log"))
            _setup_logging(tmp, logging_enabled=True)
            _flush_early_logging()
            logf = os.path.join(tmp, "smart_fan_controller.log")
            assert os.path.exists(logf)
            assert "KORAI_WARNING" in open(logf, encoding="utf-8").read()
        finally:
            self._reset()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_early_buffer_discard_when_disabled(self):
        """logging:false → korai logok eldobva, nincs fájl."""
        self._reset()
        tmp = tempfile.mkdtemp()
        try:
            _setup_early_logging()
            _logging.getLogger("user").warning("KORAI_WARNING")
            _setup_logging(tmp, logging_enabled=False)
            _discard_early_logging()
            assert not os.path.exists(os.path.join(tmp, "smart_fan_controller.log"))
            assert _logmod._early_mem_handlers == []
        finally:
            self._reset()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_early_buffer_is_bounded(self):
        """A korai puffer nem nő korlátlanul, és jelzi a veszteséget.

        A korábbi MemoryHandler(capacity=100000) nem volt korlátozható:
        target nélkül a flush() NEM üríti a puffert (a CPython csak
        ``if self.target`` esetén ürít), így a kapacitás elérése után
        minden további rekord egy hatástalan flush-t váltott ki, a lista
        pedig tovább nőtt – ameddig a pufferelés tart.
        """
        from smart_fan_controller.earlylog import EarlyLogBuffer

        self._reset()
        tmp = tempfile.mkdtemp()
        try:
            _setup_early_logging()
            bufs = [b for _lg, b in _logmod._early_mem_handlers]
            assert bufs and all(isinstance(b, EarlyLogBuffer) for b in bufs)
            # Kis kapacitásra állítva a határ gyorsan reprodukálható
            for b in bufs:
                b.capacity = 5

            user_log = _logging.getLogger("user")
            for i in range(40):
                user_log.warning("KORAI_%02d", i)

            user_buf = next(
                b for lg, b in _logmod._early_mem_handlers if lg.name == "user"
            )
            assert len(user_buf.records) == 5, "a kapacitás nem érvényesült"
            assert user_buf.dropped == 35

            _setup_logging(tmp, logging_enabled=True)
            _flush_early_logging()
            text = open(
                os.path.join(tmp, "smart_fan_controller.log"), encoding="utf-8"
            ).read()
            # A LEGKORÁBBI rekordok maradnak meg (az első hiba a leghasznosabb)
            assert "KORAI_00" in text
            assert "KORAI_04" in text
            assert "KORAI_05" not in text
            # …és a veszteség nem tűnik el csendben
            assert "35" in text
        finally:
            self._reset()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_early_buffer_detaches_before_replay(self):
        """Visszajátszáskor a puffer leválik: nem eteti vissza magát.

        A Logger.handle() minden csatolt handlernek átadja a rekordot –
        ha a puffer még a loggeren lóg, a visszajátszás újra beletolná
        ugyanazokat a rekordokat.
        """
        from smart_fan_controller.earlylog import EarlyLogBuffer

        self._reset()
        try:
            lg = _logging.getLogger("user")
            lg.handlers.clear()
            lg.setLevel(_logging.DEBUG)
            buf = EarlyLogBuffer()
            lg.addHandler(buf)
            lg.warning("EGYSZER")
            assert len(buf.records) == 1

            buf.replay(lg)
            assert buf not in lg.handlers
            assert buf.records == []
        finally:
            self._reset()


class TestEarlyLogBuffer:
    """A korlátozott korai log-puffer önmagában."""

    def test_records_are_kept_unformatted_until_replay(self):
        """A rekordok objektumként várakoznak – a formázás a cél handleré."""
        from smart_fan_controller.earlylog import EarlyLogBuffer

        buf = EarlyLogBuffer(capacity=10)
        rec = _logging.LogRecord(
            "t", _logging.WARNING, __file__, 1, "érték: %s", ("x",), None
        )
        buf.handle(rec)
        assert buf.records == [rec]
        assert buf.records[0].args == ("x",)
        buf.discard()

    def test_capacity_floor_is_one(self):
        """0 vagy negatív kapacitás sem hagyhat használhatatlan puffert."""
        from smart_fan_controller.earlylog import EarlyLogBuffer

        buf = EarlyLogBuffer(capacity=0)
        assert buf.capacity == 1
        buf.discard()

    def test_discard_drops_everything_and_closes(self):
        from smart_fan_controller.earlylog import EarlyLogBuffer

        buf = EarlyLogBuffer(capacity=2)
        for i in range(5):
            buf.handle(_logging.LogRecord(
                "t", _logging.INFO, __file__, 1, "m%d" % i, None, None
            ))
        assert (len(buf.records), buf.dropped) == (2, 3)
        buf.discard()
        assert (buf.records, buf.dropped) == ([], 0)


# ============================================================
# zwift_api – config (settings.json zwift_api szekció) + saját loggolás
# ============================================================

class TestZwiftApiConfig:
    """A zwift_api szekció betöltése és validációja (settings.json)."""

    def _load(self, tmp, zwift_api):
        p = os.path.join(tmp, "settings.json")
        with open(p, "w", encoding="utf-8") as fh:
            _json.dump({"zwift_api": zwift_api}, fh)
        from smart_fan_controller.config import load_settings as _ls
        return _ls(p)["zwift_api"]

    def test_defaults_when_section_missing(self):
        """Hiányzó zwift_api szekció → ZwiftApiConfig defaultok."""
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "settings.json")
            with open(p, "w", encoding="utf-8") as fh:
                _json.dump({}, fh)
            from smart_fan_controller.config import load_settings as _ls
            z = _ls(p)["zwift_api"]
            assert z.username == ""
            assert z.password == ""
            assert z.poll_interval == 3.0
            assert z.separate_window is True
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_valid_values_loaded(self):
        """Érvényes mezők betöltődnek."""
        tmp = tempfile.mkdtemp()
        try:
            z = self._load(tmp, {
                "username": "u@x.com", "password": "pw",
                "poll_interval": 4.5, "separate_window": False,
            })
            assert z.username == "u@x.com"
            assert z.password == "pw"
            assert z.poll_interval == 4.5
            assert z.separate_window is False
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_invalid_poll_interval_keeps_default(self):
        """Tartományon kívüli poll_interval → default (3.0) marad."""
        tmp = tempfile.mkdtemp()
        try:
            z = self._load(tmp, {"poll_interval": 999})
            assert z.poll_interval == 3.0
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_invalid_username_type_keeps_default(self):
        """Rossz username típus → default ('') marad."""
        tmp = tempfile.mkdtemp()
        try:
            z = self._load(tmp, {"username": 12345})
            assert z.username == ""
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_save_credentials_preserves_other_sections(self):
        """save_zwift_api_credentials csak a user/pass-t írja, a többit megőrzi."""
        from smart_fan_controller.config.loader import save_zwift_api_credentials
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "settings.json")
            with open(p, "w", encoding="utf-8") as fh:
                _json.dump({
                    "power_zones": {"ftp": 250},
                    "zwift_api": {"poll_interval": 5.0, "separate_window": False},
                }, fh)
            assert save_zwift_api_credentials(p, "me@x.com", "secret") is True
            data = _json.load(open(p, encoding="utf-8"))
            assert data["zwift_api"]["username"] == "me@x.com"
            assert data["zwift_api"]["password"] == "secret"
            # többi mező/szekció megőrizve
            assert data["zwift_api"]["poll_interval"] == 5.0
            assert data["zwift_api"]["separate_window"] is False
            assert data["power_zones"]["ftp"] == 250
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestZwiftPollIntervalFloor:
    """A poll_interval alsó határa 3s – a Zwift API ennél sűrűbben nem szolgál ki."""

    def test_below_floor_rejected(self, caplog):
        from smart_fan_controller.config.schemas import ZwiftApiConfig

        with caplog.at_level("WARNING", logger="user"):
            cfg = ZwiftApiConfig.from_dict({"poll_interval": 1.0})
        assert cfg.poll_interval == 3.0            # default marad
        assert any("poll_interval" in rec.message for rec in caplog.records)

    def test_floor_value_accepted(self):
        from smart_fan_controller.config.schemas import ZwiftApiConfig
        assert ZwiftApiConfig.from_dict({"poll_interval": 3.0}).poll_interval == 3.0
        assert ZwiftApiConfig.from_dict({"poll_interval": 8.5}).poll_interval == 8.5


class TestZwiftApiPollingLogging:
    """A zwift_api segédprocessz saját loggolása (zwift_api_polling.log)."""

    def _silence(self):
        """A zwift logger némítása a tesztek között."""
        _zaplog.log.handlers.clear()
        _zaplog.log.addHandler(_logging.NullHandler())

    def test_setup_logging_enabled_creates_file(self):
        """enabled=True → zwift_api_polling.log létrejön + tartalmazza az üzenetet."""
        tmp = tempfile.mkdtemp()
        try:
            _zaplog.setup_logging(tmp, enabled=True, debug=False)
            _zaplog.log.info("ZAP_TEST_SOR")
            logf = os.path.join(tmp, "zwift_api_polling.log")
            assert os.path.exists(logf)
            assert "ZAP_TEST_SOR" in open(logf, encoding="utf-8").read()
        finally:
            self._silence()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_setup_logging_disabled_nullhandler_no_file(self):
        """enabled=False → NullHandler, nincs log fájl."""
        tmp = tempfile.mkdtemp()
        try:
            _zaplog.setup_logging(tmp, enabled=False)
            _zaplog.log.info("NE_LEGYEN")
            handlers = _zaplog.log.handlers
            assert len(handlers) == 1
            assert isinstance(handlers[0], _logging.NullHandler)
            assert not os.path.exists(os.path.join(tmp, "zwift_api_polling.log"))
        finally:
            self._silence()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_early_buffer_flush_and_discard(self):
        """Korai puffer: flush (true) visszajátszik, discard (false) eldob."""
        # flush
        tmp = tempfile.mkdtemp()
        try:
            _zaplog.setup_early_logging()
            _zaplog.log.warning("KORAI_ZAP")
            assert not os.path.exists(os.path.join(tmp, "zwift_api_polling.log"))
            _zaplog.setup_logging(tmp, enabled=True)
            _zaplog.flush_early_logging()
            logf = os.path.join(tmp, "zwift_api_polling.log")
            assert "KORAI_ZAP" in open(logf, encoding="utf-8").read()
        finally:
            self._silence()
            shutil.rmtree(tmp, ignore_errors=True)
        # discard
        tmp2 = tempfile.mkdtemp()
        try:
            _zaplog.setup_early_logging()
            _zaplog.log.warning("ELDOBOTT")
            _zaplog.setup_logging(enabled=False)
            _zaplog.discard_early_logging()
            assert _zaplog._early_mem_handler is None
            assert not os.path.exists(os.path.join(tmp2, "zwift_api_polling.log"))
        finally:
            self._silence()
            shutil.rmtree(tmp2, ignore_errors=True)


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
# Core csomag (tiszta domain-logika) közvetlen importja
# ============================================================

class TestCorePackage:
    """A smart_fan_controller.core csomag önálló, PySide6/BLE nélkül használható."""

    def test_zones_direct_import(self):
        from smart_fan_controller.core import (
            calculate_power_zones, zone_for_power, apply_zone_mode,
        )
        zones = calculate_power_zones(ftp=200, min_watt=0, max_watt=1000, z1_pct=60, z2_pct=89)
        assert zone_for_power(0, zones) == 0
        assert zone_for_power(300, zones) == 3
        assert apply_zone_mode(2, 1, ZoneMode.HIGHER_WINS) == 2

    def test_averaging_direct_import(self):
        from smart_fan_controller.core import compute_average, PowerAverager
        from collections import deque
        assert compute_average(deque([2.0, 4.0])) == 3.0
        avg = PowerAverager(buffer_seconds=1, minimum_samples=1, buffer_rate_hz=4)
        assert avg.add_sample(100.0) == 100.0

    def test_main_module_reexports_core_objects(self):
        """A fő modul ugyanazokat az objektumokat exportálja, mint a core (identitás)."""
        import zwift_fan_controller as app
        from smart_fan_controller import core
        assert app.calculate_power_zones is core.calculate_power_zones
        assert app.PowerAverager is core.PowerAverager
        assert app.compute_average is core.compute_average


# ============================================================
# Gördülő átlag – futó összeg regressziós tesztek
# ============================================================

class TestRollingAveragerRunningSum:
    """A futó összegű (O(1)) átlagolás pontosan azt adja, amit a teljes
    buffer újraösszegzése adna – evikció (kieső minta) után is."""

    def test_average_exact_after_evictions(self):
        from smart_fan_controller.core import PowerAverager, compute_average
        # buffersize = 1s * 4Hz = 4 → az 5. mintától evikció történik
        avg = PowerAverager(buffer_seconds=1, minimum_samples=1, buffer_rate_hz=4)
        for value in (100, 150, 200, 250, 300, 0, 50, 400, 123, 7):
            result = avg.add_sample(float(value))
            assert result == compute_average(avg.buffer)

    def test_clear_resets_running_sum(self):
        from smart_fan_controller.core import HRAverager
        avg = HRAverager(buffer_seconds=1, minimum_samples=1, buffer_rate_hz=4)
        avg.add_sample(180.0)
        avg.add_sample(120.0)
        avg.clear()
        assert len(avg.buffer) == 0
        # A clear() utáni első minta átlaga csak az új mintát tükrözi
        assert avg.add_sample(60.0) == 60.0

    def test_long_run_no_drift_with_int_samples(self):
        from smart_fan_controller.core import PowerAverager, compute_average
        avg = PowerAverager(buffer_seconds=3, minimum_samples=1, buffer_rate_hz=4)
        for i in range(1000):
            result = avg.add_sample(float((i * 37) % 500))
            assert result == compute_average(avg.buffer)


# ============================================================
# Headless import (PySide6 nélkül)
# ============================================================

class TestRollingAveragerTimeWindow:
    """Az átlagolási ablak IDŐALAPÚ – a buffer_seconds valós másodperc.

    Regresszió: a puffer csak mintaszámra vágott (buffer_seconds ×
    buffer_rate_hz), így a beállított rátánál lassabb forrásnál az ablak
    sokkal hosszabb lett a beállítottnál. A Zwift HTTPS API 3 mp-nél
    sűrűbben nem kérdezhető le (~0.33 Hz), a 10s × 3Hz = 30 mintás puffer
    tehát 90 másodpercnyi adatot tartott – a ventilátor másfél perces
    átlagot követett."""

    def _feed(self, avg, real_rate_hz, seconds):
        """Mintaadás rögzített (szimulált) órával."""
        dt = 1.0 / real_rate_hz
        t = 0.0
        for i in range(int(seconds * real_rate_hz)):
            avg.add_sample(float(i % 300), now=t)
            t += dt
        return dt

    def test_slow_source_window_matches_buffer_seconds(self):
        from smart_fan_controller.core import PowerAverager

        # Zwift alapértelmezés, valós 0.33 Hz adatráta
        avg = PowerAverager(buffer_seconds=10, minimum_samples=2, buffer_rate_hz=3)
        dt = self._feed(avg, real_rate_hz=1 / 3, seconds=180)

        span = avg._times[-1] - avg._times[0] + dt
        assert span <= 10 + dt, f"az ablak {span:.0f}s, a beállított 10s helyett"
        assert len(avg.buffer) < avg.buffersize   # nem a mintaszám a korlát

    def test_fast_source_unchanged(self):
        """BLE/ANT+ (4 Hz, a beállított rátával egyező): változatlan viselkedés."""
        from smart_fan_controller.core import PowerAverager

        avg = PowerAverager(buffer_seconds=3, minimum_samples=6, buffer_rate_hz=4)
        dt = self._feed(avg, real_rate_hz=4, seconds=60)

        assert len(avg.buffer) == 12          # 3s × 4Hz, mint korábban
        assert avg._times[-1] - avg._times[0] + dt == pytest.approx(3.0, abs=0.3)

    def test_very_slow_source_still_averages(self):
        """A vártnál sokkal lassabb forrásnál sem marad átlag nélkül.

        Az effective_minimum mintát mindig megtartjuk, különben egy nagyon
        lassú forrásnál az ablak kiürülne és a ventilátor vezérlés nélkül
        maradna."""
        from smart_fan_controller.core import PowerAverager

        avg = PowerAverager(buffer_seconds=10, minimum_samples=2, buffer_rate_hz=3)
        # 30 másodpercenként egy minta – jóval ritkább, mint az ablak
        assert avg.add_sample(100.0, now=0.0) is None      # még gyűjt
        assert avg.add_sample(200.0, now=30.0) == 150.0    # de átlagot ad
        assert avg.add_sample(300.0, now=60.0) == 250.0    # a legfrissebb kettő

    def test_capacity_cap_still_applies(self):
        """A mintaszám-plafon (memóriavédelem) megmarad."""
        from smart_fan_controller.core import PowerAverager

        avg = PowerAverager(buffer_seconds=1, minimum_samples=1, buffer_rate_hz=4)
        for i in range(50):
            avg.add_sample(float(i), now=0.0)   # azonos időbélyeg → csak a plafon vág
        assert len(avg.buffer) == 4


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
            "import zwift_fan_controller as m\n"
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
        target.write_text('{"ble_fan": {"device_name": "Ventilator}}', encoding="utf-8")

        settings = loader.load_settings(str(target))

        assert settings["power_zones"].ftp == 200
        assert settings["ble_fan"].device_name is None

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
            '  "ble_fan": {"device_name": "MyFan"}\n}'
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
# "ble" → "ble_fan" szekció átnevezés + visszafelé kompatibilitás
# ============================================================

class TestBleFanSectionRename:
    """A fan kimenet szekció kulcsa "ble_fan"; a régi "ble" deprecated, de működik."""

    def _import_loader(self):
        from smart_fan_controller.config import loader
        return loader

    def test_new_ble_fan_key_loaded(self, tmp_path):
        """Az új "ble_fan" kulcs betöltődik."""
        loader = self._import_loader()
        target = tmp_path / "settings.json"
        target.write_text('{"ble_fan": {"device_name": "MyFan"}}', encoding="utf-8")
        settings = loader.load_settings(str(target))
        assert settings["ble_fan"].device_name == "MyFan"

    def test_legacy_ble_key_still_works(self, tmp_path, caplog):
        """A régi "ble" kulcs még betöltődik – deprecation figyelmeztetéssel."""
        import logging
        loader = self._import_loader()
        target = tmp_path / "settings.json"
        target.write_text('{"ble": {"device_name": "OldFan"}}', encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="user"):
            settings = loader.load_settings(str(target))
        assert settings["ble_fan"].device_name == "OldFan"
        assert any("ble_fan" in r.message for r in caplog.records)

    def test_ble_fan_takes_precedence_over_legacy(self, tmp_path):
        """Ha mindkét kulcs jelen van, az új "ble_fan" nyer."""
        loader = self._import_loader()
        target = tmp_path / "settings.json"
        target.write_text(
            '{"ble": {"device_name": "OldFan"}, "ble_fan": {"device_name": "NewFan"}}',
            encoding="utf-8",
        )
        settings = loader.load_settings(str(target))
        assert settings["ble_fan"].device_name == "NewFan"


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


# ============================================================
# BLE Fan – időzített háttér-újracsatlakozás (regressziós tesztek)
# ============================================================


async def _wait_until(predicate, timeout=2.0, interval=0.01):
    """Vár, amíg ``predicate()`` igaz lesz, vagy lejár a timeout (AssertionError)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("A feltétel nem teljesült a megadott időn belül")


async def _cancel(task):
    """Task lemondása és bevárása (a CancelledError-t elnyeli)."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestBleFanReconnect:
    """A BLEFanOutputController időzített, nem-blokkoló háttér-újracsatlakozása.

    A BLE primitíveket (scan/connect/write) stubok helyettesítik, így a tesztek
    valódi hardver és bleak nélkül futnak. Az aszinkron forgatókönyveket
    ``asyncio.run()`` hajtja (a projekt nem használ pytest-asyncio-t).
    """

    @pytest.fixture(autouse=True)
    def _bleak_and_quiet(self):
        """A bleak-et "elérhetőnek" jelöli és elnémítja a loggereket a teszt idejére."""
        from smart_fan_controller.handlers import _ble

        prev_avail = _ble._BLEAK_AVAILABLE
        _ble._BLEAK_AVAILABLE = True
        loggers = [_logging.getLogger("user"), _logging.getLogger("zwift_fan_controller_new")]
        prev_levels = [lg.level for lg in loggers]
        for lg in loggers:
            lg.setLevel(_logging.CRITICAL)
        try:
            yield
        finally:
            _ble._BLEAK_AVAILABLE = prev_avail
            for lg, lvl in zip(loggers, prev_levels):
                lg.setLevel(lvl)

    def _make_fan(self, connect_succeeds=True, reconnect_interval=0.05, **cfg):
        """Létrehoz egy BLEFanOutputController-t stubolt BLE primitívekkel.

        Returns:
            (ctl, state) – state["connect_calls"] és state["writes"] követhető.
        """
        from smart_fan_controller.handlers import _ble

        settings = {
            "ble_fan": BleConfig(**cfg),
            "global_settings": GlobalSettingsConfig(logging=False),
        }
        ctl = _ble.BLEFanOutputController(settings)
        # Gyors teszt: rövid reconnect intervallum (megkerüli a config min=1-et).
        ctl.reconnect_interval = reconnect_interval
        state = {"connect_calls": 0, "writes": []}

        async def fake_connect():
            state["connect_calls"] += 1
            if connect_succeeds:
                ctl.is_connected = True
                ctl.last_sent = None
                ctl._retry_count = 0
                return True
            ctl.is_connected = False
            return False

        # A _reconnect_once a _scan_and_connect-et hívja (nincs _device_address).
        ctl._scan_and_connect = fake_connect
        ctl._connect = fake_connect

        async def fake_write_level(zone):
            if not ctl.is_connected:
                return
            state["writes"].append(zone)
            ctl.last_sent = zone

        ctl._write_level = fake_write_level
        return ctl, state

    # ---------- alapviselkedés ----------

    def test_send_zone_writes_when_connected(self):
        """Kapcsolódott állapotban a _send_zone kiírja a zónát."""
        ctl, state = self._make_fan()

        async def scenario():
            ctl.is_connected = True
            await ctl._send_zone(2)
            assert state["writes"] == [2]
            assert ctl.last_sent == 2
            # Ugyanaz a zóna újra → nincs duplikált írás
            await ctl._send_zone(2)
            assert state["writes"] == [2]

        asyncio.run(scenario())

    # ---------- nem-blokkoló garanciák ----------

    def test_send_zone_non_blocking_when_disconnected(self):
        """Kapcsolat nélkül a _send_zone azonnal visszatér, csak elmenti a zónát."""
        ctl, state = self._make_fan()

        async def scenario():
            ctl.is_connected = False
            t0 = time.monotonic()
            await ctl._send_zone(3)
            dt = time.monotonic() - t0
            assert dt < 0.1, f"_send_zone blokkolt: {dt:.3f}s"
            assert ctl._desired_zone == 3
            assert state["writes"] == []  # nem írt, mert nincs kapcsolat

        asyncio.run(scenario())

    def test_send_zone_non_blocking_during_reconnect(self):
        """Folyamatban lévő reconnect (lock foglalt) alatt a _send_zone nem vár."""
        ctl, _state = self._make_fan()

        async def scenario():
            ctl.is_connected = False

            async def holder():
                async with ctl._conn_lock:
                    await asyncio.sleep(1.0)

            h = asyncio.create_task(holder())
            await asyncio.sleep(0.02)  # hagyjuk, hogy megszerezze a lockot
            assert ctl._conn_lock.locked()

            t0 = time.monotonic()
            await ctl._send_zone(2)
            dt = time.monotonic() - t0
            assert dt < 0.1, f"_send_zone várt a lockra: {dt:.3f}s"
            assert ctl._desired_zone == 2
            await _cancel(h)

        asyncio.run(scenario())

    # ---------- időzített háttér-újracsatlakozás ----------

    def test_background_reconnect_after_disconnect(self):
        """Váratlan bontás után a háttér-loop magától újracsatlakozik (parancs nélkül)."""
        ctl, state = self._make_fan()

        async def scenario():
            q: asyncio.Queue[int] = asyncio.Queue()
            runner = asyncio.create_task(ctl.run(q))
            await _wait_until(lambda: ctl.is_connected)  # kezdeti csatlakozás
            assert state["connect_calls"] == 1

            ctl._handle_disconnect()  # bleak disconnect callback szimulálása
            assert ctl.is_connected is False

            # Nem küldünk új zónát – a háttér-loopnak magától vissza kell jönnie
            await _wait_until(lambda: ctl.is_connected)
            assert state["connect_calls"] >= 2

            await _cancel(runner)

        asyncio.run(scenario())

    def test_desired_zone_flushed_after_reconnect(self):
        """Bontás alatt érkező zónaváltást a háttér-loop a reconnect után kiküldi."""
        ctl, state = self._make_fan()

        async def scenario():
            q: asyncio.Queue[int] = asyncio.Queue()
            runner = asyncio.create_task(ctl.run(q))
            await _wait_until(lambda: ctl.is_connected)

            await q.put(2)
            await _wait_until(lambda: state["writes"][-1:] == [2])

            ctl._handle_disconnect()
            await q.put(3)  # zónaváltás, amíg áll a kapcsolat

            # A háttér-loop újracsatlakozik és kiküldi a legfrissebb kért zónát (3)
            await _wait_until(lambda: ctl.is_connected)
            await _wait_until(lambda: state["writes"][-1] == 3)

            await _cancel(runner)

        asyncio.run(scenario())

    def test_auth_failed_blocks_background_reconnect(self):
        """AUTH hiba esetén a háttér-loop nem próbál újracsatlakozni."""
        ctl, state = self._make_fan()

        async def scenario():
            ctl.is_connected = False
            ctl._auth_failed = True
            loop_task = asyncio.create_task(ctl._reconnect_loop())
            # Több intervallumnyi idő – mégsem szabad csatlakoznia
            await asyncio.sleep(ctl.reconnect_interval * 4)
            assert state["connect_calls"] == 0
            assert ctl.is_connected is False
            await _cancel(loop_task)

        asyncio.run(scenario())

    # ---------- tiszta leállás ----------

    def test_reconnect_task_cancelled_on_run_exit(self):
        """A run() leállásakor a háttér-task tisztán megszűnik (cancel + await)."""
        ctl, _state = self._make_fan()

        async def scenario():
            q: asyncio.Queue[int] = asyncio.Queue()
            runner = asyncio.create_task(ctl.run(q))
            await _wait_until(lambda: ctl._reconnect_task is not None)
            rtask = ctl._reconnect_task

            await _cancel(runner)

            assert rtask.done()
            assert ctl._reconnect_task is None

        asyncio.run(scenario())


# ============================================================
# ÁTVILÁGÍTÁS: REGRESSZIÓS TESZTEK
# ============================================================


class TestAtomicSettingsWrite:
    """A settings.json atomikus írása – korrupció elleni védelem."""

    def test_temp_file_name_is_process_unique(self):
        """A temp fájl neve tartalmazza a PID-et (nem ütközik más processzel).

        Közös 'settings.json.tmp' esetén a főprogram HUD-mentése és a
        zwift_api_polling alfolyamat hitelesítő-mentése ugyanabba a fájlba
        írt, és az os.replace az összekeveredett, hibás tartalmat tette
        közzé a felhasználó settings.json-jaként.
        """
        from smart_fan_controller.config import loader as _loader

        captured: list[str] = []
        real_open = open

        def spy_open(path, *a, **kw):
            if str(path).endswith(".tmp"):
                captured.append(os.path.basename(str(path)))
            return real_open(path, *a, **kw)

        tmp = tempfile.mkdtemp()
        try:
            target = os.path.join(tmp, "settings.json")
            with patch("smart_fan_controller.config.loader.open", spy_open, create=True):
                _loader._write_json_atomic(target, {"a": 1})
            assert captured, "nem készült temp fájl"
            assert str(os.getpid()) in captured[0]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        """Sikeres írás után csak a végleges fájl marad a könyvtárban."""
        from smart_fan_controller.config.loader import _write_json_atomic

        tmp = tempfile.mkdtemp()
        try:
            target = os.path.join(tmp, "settings.json")
            _write_json_atomic(target, {"ékezet": "őű", "n": 1})
            _write_json_atomic(target, {"n": 2})
            assert os.listdir(tmp) == ["settings.json"]
            import json as _json
            assert _json.load(open(target, encoding="utf-8")) == {"n": 2}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_failed_write_keeps_the_previous_content(self):
        """Írás közbeni hiba esetén a régi tartalom sértetlen marad."""
        from smart_fan_controller.config import loader as _loader

        tmp = tempfile.mkdtemp()
        try:
            target = os.path.join(tmp, "settings.json")
            _loader._write_json_atomic(target, {"jo": 1})
            with patch("json.dump", side_effect=OSError("lemez tele")):
                with pytest.raises(OSError):
                    _loader._write_json_atomic(target, {"uj": 2})
            import json as _json
            assert _json.load(open(target, encoding="utf-8")) == {"jo": 1}
            assert os.listdir(tmp) == ["settings.json"]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestBleDeviceLogCaching:
    """A ble_devices.log nem olvasódik újra minden scan-nél és nem nő korlátlanul."""

    @staticmethod
    def _reset_caches():
        from smart_fan_controller.handlers import _ble
        _ble._ble_logged_addresses.clear()
        _ble._ble_log_full_warned.clear()
        _ble._ble_printed_addresses.clear()

    def test_file_is_parsed_once_per_process(self):
        """A második scan már nem nyitja meg olvasásra a log fájlt."""
        from smart_fan_controller.handlers import _ble

        self._reset_caches()
        tmp = tempfile.mkdtemp()
        try:
            _ble._log_ble_devices_to_file(
                [("Fan", "AA:01", ["u"])], "BLE Fan", tmp, True)
            reads: list[str] = []
            real_open = open

            def spy_open(path, mode="r", *a, **kw):
                if "r" in mode and str(path).endswith("ble_devices.log"):
                    reads.append(str(path))
                return real_open(path, mode, *a, **kw)

            with patch("smart_fan_controller.handlers._ble.open", spy_open, create=True):
                _ble._log_ble_devices_to_file(
                    [("Fan", "AA:01", ["u"]), ("Uj", "BB:02", ["u"])],
                    "BLE Fan", tmp, True)
            assert reads == [], "a gyorsítótár ellenére újraolvasta a fájlt"
            content = open(os.path.join(tmp, "ble_devices.log"), encoding="utf-8").read()
            assert content.count("AA:01") == 1   # nem duplikálódott
            assert "BB:02" in content            # az új eszköz bekerült
        finally:
            self._reset_caches()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_entry_cap_stops_unbounded_growth(self):
        """A bejegyzés-korlát felett már nem ír a fájlba (forgó BLE címek)."""
        from smart_fan_controller.handlers import _ble

        self._reset_caches()
        tmp = tempfile.mkdtemp()
        try:
            with patch.object(_ble, "_BLE_LOG_MAX_ENTRIES", 3):
                _ble._log_ble_devices_to_file(
                    [(f"d{i}", f"AA:{i}", []) for i in range(3)],
                    "BLE Fan", tmp, True)
                size_before = os.path.getsize(os.path.join(tmp, "ble_devices.log"))
                _ble._log_ble_devices_to_file(
                    [("uj", "ZZ:99", [])], "BLE Fan", tmp, True)
                assert os.path.getsize(os.path.join(tmp, "ble_devices.log")) == size_before
        finally:
            self._reset_caches()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cache_reloads_when_the_file_was_deleted(self):
        """A fájl törlése után az eszközök újra kiíródnak."""
        from smart_fan_controller.handlers import _ble

        self._reset_caches()
        tmp = tempfile.mkdtemp()
        try:
            log = os.path.join(tmp, "ble_devices.log")
            _ble._log_ble_devices_to_file([("Fan", "AA:01", [])], "BLE Fan", tmp, True)
            os.remove(log)
            _ble._log_ble_devices_to_file([("Fan", "AA:01", [])], "BLE Fan", tmp, True)
            assert "AA:01" in open(log, encoding="utf-8").read()
        finally:
            self._reset_caches()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_console_listing_is_not_repeated_for_known_devices(self, caplog):
        """Ismételt scan csak az ÚJ eszközöket listázza (konzol-elárasztás ellen)."""
        import logging as _logging
        from smart_fan_controller.handlers import _ble

        self._reset_caches()
        devices = [(f"dev{i}", f"AA:{i:02d}", []) for i in range(10)]
        try:
            with caplog.at_level(_logging.INFO, logger="user"):
                _ble._print_ble_devices(devices, "BLE Fan")
                first = len(caplog.records)
                caplog.clear()
                _ble._print_ble_devices(devices, "BLE Fan")
                second = len(caplog.records)
                caplog.clear()
                _ble._print_ble_devices(
                    devices + [("uj", "FF:FF", [])], "BLE Fan")
                third_text = "\n".join(r.getMessage() for r in caplog.records)
            assert first >= 11              # fejléc + 10 eszköz
            assert second == 1              # csak az összefoglaló sor
            assert "FF:FF" in third_text    # az új eszköz megjelenik
            assert "AA:00" not in third_text
        finally:
            self._reset_caches()


class TestGenerateToneRange:
    """A WAV generálás nem szállhat el a 16 bites tartomány túllépésén."""

    def test_loud_tone_is_clipped_not_crashing(self):
        """volume * amp > 1.0 → levágás, nem struct.error.

        A pack("<{n}h") korábban ValueError-ral (struct.error) állt le a
        generálás közepén, így egyetlen hangosabb hangdefiníció az egész
        hangkészlet legyártását megbuktatta.
        """
        from smart_fan_controller.core.helpers import generate_tone

        data = generate_tone([(440, 0.05, 1.5)], volume=1.0)
        assert data[:4] == b"RIFF"
        # A csúcsok pontosan a tartomány szélén állnak meg
        import wave as _wave
        import io as _io
        import array as _array

        with _wave.open(_io.BytesIO(data), "rb") as wf:
            frames = wf.readframes(wf.getnframes())
        samples = _array.array("h")
        samples.frombytes(frames)
        assert max(samples) == 32767
        assert min(samples) >= -32768

    def test_quiet_tone_is_untouched(self):
        """A normál (nem levágott) tartományban semmi nem változik."""
        from smart_fan_controller.core.helpers import generate_tone

        import wave as _wave
        import io as _io
        import array as _array

        data = generate_tone([(440, 0.05, 1.0)], volume=0.4)
        with _wave.open(_io.BytesIO(data), "rb") as wf:
            frames = wf.readframes(wf.getnframes())
        samples = _array.array("h")
        samples.frombytes(frames)
        assert max(samples) < 32767
        assert min(samples) > -32768

    def test_shipped_wavs_regenerate_bit_identically(self):
        """A becsomagolt hangfájlok bitre azonosan újragenerálhatók.

        Ez köti le a generate_tone számítási sorrendjét: egy ártalmatlannak
        tűnő átszervezés (pl. volume * amp összevonása) az utolsó ULP-t
        elmozdítva más bájtokat adna, és a repóban lévő WAV-ok
        észrevétlenül eltérnének a generátor kimenetétől.
        """
        import importlib.util
        import pathlib

        from smart_fan_controller.core.helpers import generate_tone

        root = pathlib.Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "_lcars_sound_defs", root / "tools" / "generate_lcars_sounds.py"
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        snd_dir = root / "smart_fan_controller" / "sounds"
        for name, tones in mod.SOUND_DEFS.items():
            wav = snd_dir / f"{name}.wav"
            assert wav.is_file(), f"hiányzó hangfájl: {wav}"
            assert wav.read_bytes() == generate_tone(tones), (
                f"a(z) {name}.wav nem egyezik a generátor kimenetével"
            )


class TestProcessWatch:
    """A folyamat-ellenőrző visszaesési (fallback) logikája."""

    @staticmethod
    def _fresh_module():
        """Friss procwatch modulpéldány (a lusta cache miatt)."""
        import importlib

        from smart_fan_controller import procwatch

        return importlib.reload(procwatch)

    def test_non_windows_reports_unknown(self, monkeypatch):
        """Nem Windows: None – a hívó dönti el, mit jelent."""
        pw = self._fresh_module()
        monkeypatch.setattr(pw, "_IS_WINDOWS", False)
        assert pw.process_running("ZwiftApp.exe") is None

    def test_callers_interpret_unknown_differently(self, monkeypatch):
        """A HUD 'nem fut'-ként, a Zwift poller 'ne lépj ki'-ként érti."""
        from smart_fan_controller.controller import FanController
        from smart_fan_controller.zwift_api import runtime

        monkeypatch.setattr(
            "smart_fan_controller.controller.process_running", lambda _n: None
        )
        monkeypatch.setattr(
            "smart_fan_controller.zwift_api.runtime.process_running",
            lambda _n: None,
        )
        assert FanController.is_process_running("ZwiftApp.exe") is False
        assert runtime._is_zwift_running() is True

    def test_toolhelp_failure_falls_back_to_tasklist(self, monkeypatch):
        """A Toolhelp32 hibája nem hiba: tasklist veszi át, egyszer."""
        pw = self._fresh_module()
        monkeypatch.setattr(pw, "_IS_WINDOWS", True)

        def _broken():
            raise OSError("kernel32 nem elérhető")

        calls: list[str] = []

        def _fake_tasklist(name: str):
            calls.append(name)
            return True

        monkeypatch.setattr(pw, "_build_toolhelp_reader", _broken)
        monkeypatch.setattr(pw, "_tasklist_running", _fake_tasklist)

        assert pw.process_running("ZwiftApp.exe") is True
        assert pw.process_running("ZwiftApp.exe") is True
        assert calls == ["ZwiftApp.exe", "ZwiftApp.exe"]

    def test_toolhelp_result_is_used_and_tasklist_skipped(self, monkeypatch):
        """Működő Toolhelp32 esetén nem indul segédfolyamat."""
        pw = self._fresh_module()
        monkeypatch.setattr(pw, "_IS_WINDOWS", True)

        seen: list[str] = []

        def _reader_factory():
            def _reader(target_lower: str) -> bool:
                seen.append(target_lower)
                return target_lower == "zwiftapp.exe"
            return _reader

        def _must_not_run(_name: str):
            raise AssertionError("a tasklist nem indulhatott volna el")

        monkeypatch.setattr(pw, "_build_toolhelp_reader", _reader_factory)
        monkeypatch.setattr(pw, "_tasklist_running", _must_not_run)

        assert pw.process_running("ZwiftApp.exe") is True
        assert pw.process_running("Other.exe") is False
        # A név kisbetűsen, pontos egyezésre megy (nem részstring-keresés)
        assert seen == ["zwiftapp.exe", "other.exe"]

    def test_reader_exception_disables_fast_path_once(self, monkeypatch):
        """Egy futásidejű Toolhelp32 hiba után végleg a tasklist marad."""
        pw = self._fresh_module()
        monkeypatch.setattr(pw, "_IS_WINDOWS", True)

        reader_calls: list[str] = []
        tasklist_calls: list[str] = []

        def _reader_factory():
            def _reader(target_lower: str) -> bool:
                reader_calls.append(target_lower)
                raise OSError(5, "hozzáférés megtagadva")
            return _reader

        monkeypatch.setattr(pw, "_build_toolhelp_reader", _reader_factory)
        monkeypatch.setattr(
            pw, "_tasklist_running",
            lambda name: (tasklist_calls.append(name), False)[1],
        )

        assert pw.process_running("ZwiftApp.exe") is False
        assert pw.process_running("ZwiftApp.exe") is False
        assert len(reader_calls) == 1, "a hibás gyors út újra lefutott"
        assert len(tasklist_calls) == 2

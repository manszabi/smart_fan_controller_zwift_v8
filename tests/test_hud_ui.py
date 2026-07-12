"""Automated UI tests for the LCARS HUD (offscreen Qt).

Exercises the real :class:`HUDWindow` against the same ``FakeController``
the manual ``hud_test`` harness uses, on the offscreen Qt platform – no
display, sensors or Zwift required. Skipped entirely when PySide6 is not
installed (the conftest Qt stubs cannot run real widget code).
"""
from __future__ import annotations

import os
import time

import pytest

from tests.conftest import REAL_PYSIDE6

if not REAL_PYSIDE6:
    pytest.skip("PySide6 nem elérhető – UI tesztek kihagyva",
                allow_module_level=True)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from hud_test.run_hud_test import FakeController  # noqa: E402
from smart_fan_controller.config.schemas import DataSource, ZoneMode  # noqa: E402
from smart_fan_controller.ui import theme  # noqa: E402
from smart_fan_controller.ui.sound import LCARSSoundManager  # noqa: E402
from smart_fan_controller.ui.window import HUDWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def hud(app):
    ctrl = FakeController()
    win = HUDWindow(ctrl, app)
    win._timer.stop()  # a teszt kézzel hívja az _update()-et
    yield win, ctrl
    win.sound.cleanup()
    win.deleteLater()
    app.processEvents()


def _feed(ctrl, zone, power, hr, *, fresh=True):
    """Snapshot + friss (vagy elavult) forrás-időbélyegek beállítása."""
    ctrl.state.ui_snapshot.update(zone, power, hr)
    now = time.monotonic() if fresh else 0.0
    ctrl._zwift_udp.power_lastdata = now
    ctrl._zwift_udp.hr_lastdata = now


# ─────────────────────────── import kompatibilitás ───────────────────────────


def test_hud_import_paths_are_compatible():
    """A régi (ui.hud) és az új (ui.window / ui) útvonal ugyanazt adja."""
    from smart_fan_controller.ui import HUDWindow as from_pkg
    from smart_fan_controller.ui.hud import HUDWindow as from_hud

    assert from_hud is from_pkg is HUDWindow


# ─────────────────────────────── zóna kijelzés ───────────────────────────────


def test_zone_display_updates(hud):
    win, ctrl = hud
    _feed(ctrl, 2, 150.0, 120.0)
    win._update()

    assert win._lbl_zone.text() == "ZONE 2"
    assert win._lbl_zone._hud_color == theme.ZONE_COLORS[2]
    assert win._zone_bar._zone == 2
    assert win._lbl_power.text() == "150 W"
    assert win._lbl_hr.text() == "120 BPM"


def test_zone_none_shows_placeholders(hud):
    win, ctrl = hud
    ctrl.state.ui_snapshot.update(None, None, None)
    win._update()

    assert win._lbl_zone.text() == "– – –"
    assert win._lbl_power.text() == "– – –"
    assert win._zone_bar._zone is None


# ───────────────────────────── szenzor státusz sorok ─────────────────────────


def test_zwift_row_ok_when_data_fresh(hud):
    win, ctrl = hud
    _feed(ctrl, 1, 100.0, 100.0)
    win._update()

    assert win._lbl_zwift_udp.text() == "P:OK  HR:OK"
    assert win._lbl_zwift_udp._hud_color == theme.LCARS_CYAN


def test_zwift_row_fail_on_stale_hr(hud):
    win, ctrl = hud
    _feed(ctrl, 1, 100.0, 100.0)
    ctrl._zwift_udp.hr_lastdata = 0.0  # sosem jött HR adat
    win._update()

    assert win._lbl_zwift_udp.text() == "P:OK  HR:FAIL"
    # Hibaállapot: piros (a villogás miatt a világosított piros is érvényes)
    assert win._lbl_zwift_udp._hud_color in (
        theme.LCARS_RED, theme.lighten(theme.LCARS_RED),
    )


def test_sensor_rows_follow_source_selection(hud):
    win, ctrl = hud
    ds = ctrl.settings["datasource"]
    ds.power_source = DataSource.ANTPLUS
    ds.hr_source = DataSource.ZWIFTUDP
    now = time.monotonic()
    ctrl._antplus_handler.power_lastdata = now
    ctrl._zwift_udp.hr_lastdata = now
    win._update()

    # ANT+ csak powerre van kiválasztva, Zwift csak pulzusra
    assert win._lbl_ant.text() == "P:OK  HR:--"
    assert win._lbl_zwift_udp.text() == "P:--  HR:OK"
    # BLE nincs kiválasztva → placeholder
    assert win._lbl_ble_sens.text() == "– – –"


# ─────────────────────────────── BLE fan állapotok ───────────────────────────


def test_ble_fan_states(hud):
    win, ctrl = hud

    ctrl.ble_fan = None
    win._update()
    assert win._lbl_ble.text() == "DISABLED"
    assert win._lbl_ble._hud_color == theme.TEXT_DIM  # nem villog

    fan = FakeController().ble_fan
    ctrl.ble_fan = fan

    fan.is_connected = True
    win._update()
    assert win._lbl_ble.text() == "ONLINE"

    fan.is_connected = False
    win._update()
    assert win._lbl_ble.text() == "OFFLINE"

    fan.auth_failed = True
    win._update()
    assert win._lbl_ble.text() == "PIN FAIL"


# ──────────────────────────────── meterek, tile-ok ───────────────────────────


def test_power_meter_fraction_and_zone_color(hud):
    win, ctrl = hud
    pz = ctrl.settings["power_zones"]  # ftp=200, z1=60%, z2=89% (defaultok)
    _feed(ctrl, 1, 100.0, 100.0)
    win._update()

    # 100 W <= z1 küszöb (120 W) → Z1 szín; kitöltés = 100 / (ftp*1.25)
    assert win._power_meter._color == theme.ZONE_COLORS[1]
    assert win._power_meter._fraction == pytest.approx(100 / (pz.ftp * 1.25))

    _feed(ctrl, 3, 300.0, 100.0)
    win._update()
    assert win._power_meter._color == theme.ZONE_COLORS[3]
    assert win._power_meter._fraction == 1.0  # plafonon


def test_higher_wins_tile_follows_zone_mode(hud):
    win, ctrl = hud
    hz = ctrl.settings["heart_rate_zones"]

    hz.zone_mode = ZoneMode.HIGHER_WINS
    win._update()
    assert win._tile_higher_wins.property("hudState") in ("on", "flash")

    hz.zone_mode = ZoneMode.POWER_ONLY
    win._update()
    assert win._tile_higher_wins.property("hudState") == "off"


# ─────────────────────────── geometria visszaállítás ─────────────────────────


def _primary(app):
    screen = app.primaryScreen()
    return screen.name(), screen.availableGeometry()


def test_restore_clamps_offscreen_position(app):
    """Létező monitor, de kilógó mentett pozíció → teljesen behúzva."""
    pname, sg = _primary(app)
    ctrl = FakeController()
    ctrl.settings["hud"].window_geometry = {
        pname: {"x": sg.x() + sg.width() - 50, "y": sg.y() + sg.height() - 50,
                "w": 300, "h": 420},
    }
    win = HUDWindow(ctrl, app)
    win._timer.stop()
    win._restore_geometry()
    assert sg.contains(win.geometry())
    win.deleteLater()


def test_restore_missing_monitor_uses_primary_entry(app):
    """Hiányzó monitor + primary mentés → a primary bejegyzés érvényesül."""
    pname, sg = _primary(app)
    ctrl = FakeController()
    ctrl.settings["hud"].window_geometry = {
        pname: {"x": sg.x() + 40, "y": sg.y() + 50, "w": 320, "h": 440},
        "\\\\.\\LECSATOLT_MONITOR": {"x": 5000, "y": 300, "w": 400, "h": 500},
    }
    win = HUDWindow(ctrl, app)
    win._timer.stop()
    win._restore_geometry()
    g = win.geometry()
    assert (g.x(), g.y(), g.width(), g.height()) == (sg.x() + 40, sg.y() + 50, 320, 440)
    win.deleteLater()


def test_restore_missing_monitor_centers_with_saved_size(app):
    """Hiányzó monitor, primary mentés nélkül → mentett méret, középre."""
    pname, sg = _primary(app)
    ctrl = FakeController()
    ctrl.settings["hud"].window_geometry = {
        "\\\\.\\LECSATOLT_MONITOR": {"x": 5000, "y": 300, "w": 400, "h": 500},
    }
    win = HUDWindow(ctrl, app)
    win._timer.stop()
    win._restore_geometry()
    g = win.geometry()
    assert (g.width(), g.height()) == (400, 500)
    assert g.x() == sg.x() + (sg.width() - 400) // 2
    assert sg.contains(g)
    win.deleteLater()


# ───────────────────────────────── skálázás ──────────────────────────────────


def test_resize_applies_scale(hud, app):
    win, _ctrl = hud
    base_header_h = win._header.height()
    # Rejtett ablaknak a Qt nem kézbesít resize eventet → meg kell jeleníteni
    win.show()
    app.processEvents()
    win.resize(510, 690)  # ~1.5x
    app.processEvents()
    assert win._scale == pytest.approx(1.5, abs=0.01)
    assert win._header.height() > base_header_h
    win.hide()


# ──────────────────────────────── hangrendszer ───────────────────────────────


def test_sound_manager_loads_all_stock_sounds(app):
    mgr = LCARSSoundManager()
    try:
        assert set(mgr._effects) == set(LCARSSoundManager.SOUND_NAMES)
        # Az időtartam a WAV fejlécből jön – a shutdown hangnak ~0.83 s
        assert mgr.sound_duration_ms("hud_shutdown") == pytest.approx(830, abs=20)
    finally:
        mgr.cleanup()


def test_sound_manager_tolerates_missing_files(app, tmp_path, monkeypatch, caplog):
    """Hiányzó hangfájl: nincs kivétel, a log a pontos útvonalat adja."""
    monkeypatch.setattr(
        LCARSSoundManager, "sounds_dir", staticmethod(lambda: str(tmp_path))
    )
    with caplog.at_level("WARNING", logger="zwift_fan_controller_new"):
        mgr = LCARSSoundManager()
    try:
        assert mgr._effects == {}
        mgr.play("zone_up")  # néma no-op, nem dobhat kivételt
        assert mgr.sound_duration_ms("hud_shutdown") == 0
        expected = os.path.join(str(tmp_path), "zone_up.wav")
        assert any(expected in rec.message for rec in caplog.records)
    finally:
        mgr.cleanup()


def test_opacity_change_updates_config_immediately(hud):
    win, ctrl = hud
    win._on_alpha_change(55)
    assert ctrl.settings["hud"].opacity == 55
    assert win._alpha_value.text() == "55%"

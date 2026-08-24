"""Automated UI tests for the LCARS HUD (offscreen Qt).

Exercises the real :class:`HUDWindow` against the same ``FakeController``
the manual ``hud_test`` harness uses, on the offscreen Qt platform – no
display, sensors or Zwift required. Skipped entirely when PySide6 is not
installed (the conftest Qt stubs cannot run real widget code).
"""
from __future__ import annotations

import math
import os
import time

import pytest

from tests.conftest import REAL_PYSIDE6

if not REAL_PYSIDE6:
    pytest.skip("PySide6 nem elérhető – UI tesztek kihagyva",
                allow_module_level=True)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLayout, QWidget  # noqa: E402

from hud_test.run_hud_test import FakeController  # noqa: E402
from smart_fan_controller.config.schemas import DataSource, ZoneMode  # noqa: E402
from smart_fan_controller.ui import theme  # noqa: E402
from smart_fan_controller.ui.sound import (  # noqa: E402
    _QT_MULTIMEDIA_AVAILABLE, LCARSSoundManager,
)
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


def test_tiles_follow_the_effective_zone_mode(hud):
    """HR letiltva → a vezérlő power_only, a csempéknek ezt kell mutatniuk.

    A HUD a nyers heart_rate_zones.zone_mode értéket olvasta, így a
    (default) higher_wins beállítás mellett HR nélküli gépen is villogott
    a HI WINS csempe, miközben a zóna kizárólag teljesítmény alapján dőlt
    el. A ZHR IMM csempe ugyanígy aktívnak látszott, holott HR nélkül a
    zero_hr_immediate soha nem léphet életbe.
    """
    win, ctrl = hud
    hz = ctrl.settings["heart_rate_zones"]
    hz.zone_mode = ZoneMode.HIGHER_WINS
    hz.zero_hr_immediate = True

    hz.enabled = False
    win._update()
    assert win._tile_higher_wins.property("hudState") == "off"
    assert win._tile_zero_hr_imm.property("hudState") == "off"

    hz.enabled = True
    win._update()
    assert win._tile_higher_wins.property("hudState") in ("on", "flash")
    assert win._tile_zero_hr_imm.property("hudState") in ("on", "flash")


def test_zero_power_tile_is_off_in_hr_only_mode(hud):
    """hr_only módban a 0W azonnali leállás nem érvényesül – ne is világítson."""
    win, ctrl = hud
    ctrl.settings["power_zones"].zero_power_immediate = True
    hz = ctrl.settings["heart_rate_zones"]
    hz.enabled = True

    hz.zone_mode = ZoneMode.HR_ONLY
    win._update()
    assert win._tile_zero_imm.property("hudState") == "off"

    hz.zone_mode = ZoneMode.POWER_ONLY
    win._update()
    assert win._tile_zero_imm.property("hudState") in ("on", "flash")


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
        pname: {"x": sg.x() + 40, "y": sg.y() + 50, "w": 600, "h": 700},
        "\\\\.\\LECSATOLT_MONITOR": {"x": 5000, "y": 300, "w": 400, "h": 500},
    }
    win = HUDWindow(ctrl, app)
    win._timer.stop()
    win._restore_geometry()
    g = win.geometry()
    assert (g.x(), g.y(), g.width(), g.height()) == (sg.x() + 40, sg.y() + 50, 600, 700)
    win.deleteLater()


def test_restore_clamps_size_up_to_readable_minimum(app):
    """A tartalom olvasható minimuma alá mentett méret felfelé kerekítődik.

    Régebbi verzió (vagy másik betűtípus) menthetett olyan kis ablakot,
    amiben a szövegek már ki lennének takarva – a visszaállítás ilyenkor
    a mért minimumra igazít."""
    pname, sg = _primary(app)
    ctrl = FakeController()
    ctrl.settings["hud"].window_geometry = {
        pname: {"x": sg.x() + 10, "y": sg.y() + 10, "w": 120, "h": 150},
    }
    win = HUDWindow(ctrl, app)
    win._timer.stop()
    win._restore_geometry()
    g = win.geometry()
    assert g.width() >= win.minimumWidth()
    assert g.height() >= win.minimumHeight()
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
    # ceil: a lefelé kerekítő létra egy fél pixeltől is fokot lépne vissza
    win.resize(math.ceil(win._base_width * 1.5), math.ceil(win._base_height * 1.5))
    app.processEvents()
    assert win._scale == pytest.approx(1.5, abs=0.01)
    assert win._header.height() > base_header_h
    win.hide()


def test_scale_snaps_to_the_calibrated_ladder(hud):
    """A skála a kalibrált létrára kerekítődik – LEFELÉ.

    A létrafokokat a _calibrate_sizing méri be; a köztes méretekre lefelé
    kerekítve garantált, hogy a tartalom befér (a maradék pixel az alsó
    kitöltésbe megy, nem egy levágott sorba)."""
    win, _ctrl = hud
    step = win.SCALE_STEP
    assert win._quantize_scale(1.5) == pytest.approx(1.5, abs=1e-9)
    assert win._quantize_scale(1.5 + step * 0.9) == pytest.approx(1.5, abs=1e-9)
    # a létra alja/teteje: sosem lépünk ki a bemért tartományból
    assert win._quantize_scale(0.01) == pytest.approx(win._min_scale, abs=1e-9)
    assert win._quantize_scale(99.0) == pytest.approx(win.SCALE_MAX, abs=1e-9)


def test_minimum_size_is_the_measured_readable_minimum(hud):
    """Az ablak minimuma a bemért olvasható minimum, nem az abszolút padló."""
    win, _ctrl = hud
    assert win.minimumWidth() == max(
        win.MIN_W, round(win._base_width * win._min_scale))
    assert win.minimumHeight() == max(
        win.MIN_H, round(win._base_height * win._min_scale))
    # A kalibrált alapméret elbírja a tartalmat 1.0 skálán
    assert win._fits(1.0)


def test_no_label_is_clipped_at_any_size(hud, app):
    """Regresszió: átméretezéskor egyetlen felirat/érték sem takaródhat ki.

    A ZONE / POWER / HEART RATE / STARFLEET sorok korábban levágódtak,
    mert a minimum a pillanatnyi ablakméretre volt korlátozva, a rácsok
    állandó (nem skálázódó) paddingje pedig kiszorította a szöveget."""
    win, _ctrl = hud
    win.show()
    app.processEvents()
    # a lehető legszélesebb értékekkel: később sem takarhat ki semmit
    for name, text in HUDWindow.WIDEST_TEXTS.items():
        getattr(win, name).setText(text)

    mw, mh = win.minimumWidth(), win.minimumHeight()
    sizes = [(mw, mh), (mw, mh + 200), (mw + 200, mh), (mw + 40, mh + 15),
             (mw * 2, mh * 2), (mw + 7, mh + 300), (mw + 300, mh + 7)]
    widgets = win.findChildren(QWidget)
    for w, h in sizes:
        win.resize(w, h)
        app.processEvents()
        win.layout().activate()
        for wid in widgets:
            if not wid.isVisible() or not wid.sizeHint().isValid():
                continue
            hint, size = wid.sizeHint(), wid.size()
            label = getattr(wid, "text", lambda: type(wid).__name__)()
            assert size.width() >= hint.width(), f"{label!r} levágva @ {w}x{h}"
            assert size.height() >= hint.height(), f"{label!r} levágva @ {w}x{h}"
    win.hide()


def test_panel_layouts_do_not_pin_their_minimum(hud):
    """A belső panelek minimuma nem ragadhat be a legnagyobb skálán mértre.

    Qt alap SetDefaultConstraint-je a layout minimumát a widget explicit
    minimumSize-ába írja, amit utána már csak emelni lehet – emiatt a
    kicsinyített ablakban a sorok nem tudtak összébb menni, csak a
    szövegük szorult ki."""
    win, _ctrl = hud
    for lay, _margins, _spacing in win._scalable_layouts:
        assert lay.sizeConstraint() == QLayout.SizeConstraint.SetNoConstraint


def test_box_metrics_follow_the_scale(hud, app):
    """A padding/margó/osztóvonal is skálázódik, nem csak a betűméret."""
    win, _ctrl = hud
    win.show()
    app.processEvents()
    win.resize(round(win._base_width * 2), round(win._base_height * 2))
    app.processEvents()
    big_px = win._px(4)
    big_css = win._lbl_zone._hud_css          # padding a stíluslapban
    big_sep = win._scalable_heights[0][0].height()   # osztóvonal vastagsága
    big_row = win._lbl_power.parentWidget().layout().contentsMargins().top()

    win.resize(win.minimumWidth(), win.minimumHeight())
    app.processEvents()
    assert win._px(4) < big_px
    assert win._lbl_zone._hud_css != big_css
    assert win._scalable_heights[0][0].height() < big_sep
    assert win._lbl_power.parentWidget().layout().contentsMargins().top() < big_row
    win.hide()


# ──────────────────────────────── hangrendszer ───────────────────────────────


_needs_multimedia = pytest.mark.skipif(
    not _QT_MULTIMEDIA_AVAILABLE,
    reason="PySide6.QtMultimedia nem tölthető be (pl. hiányzó audio "
           "kliens könyvtár) – a hangeffektek amúgy is némák",
)


@_needs_multimedia
def test_sound_manager_loads_all_stock_sounds(app):
    mgr = LCARSSoundManager()
    try:
        assert set(mgr._effects) == set(LCARSSoundManager.SOUND_NAMES)
        # Az időtartam a WAV fejlécből jön – a shutdown hangnak ~0.83 s
        assert mgr.sound_duration_ms("hud_shutdown") == pytest.approx(830, abs=20)
    finally:
        mgr.cleanup()


@_needs_multimedia
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


# ──────────────────── ZwiftApp.exe figyelés platformfüggése ────────────────────


def test_zwift_watch_disabled_where_the_process_list_is_unreadable(hud, monkeypatch):
    """Nem-Windows rendszeren a HUD NEM állítja le magát 5 perc után.

    A FanController.is_process_running() csak Windows-on tud választ adni,
    máshol mindig False – ezt szó szerint véve a türelmi idő lejárt, és a
    HUD (vele az egész alkalmazás) magától kilépett Linuxon/macOS-en.
    """
    win, ctrl = hud
    ctrl.settings["hud"].close_at_zwiftapp_exe = True

    started: list[object] = []

    class _RecordingThread:
        def __init__(self, *a, **kw):
            started.append(kw.get("name"))

        def start(self):
            pass

    # Rögzítő (nem dobó) dublőr: az _update() elnyelné a kivételt, így egy
    # assert-tel jelző szál-mock hamis zöldet adna
    monkeypatch.setattr(
        "smart_fan_controller.ui.window.threading.Thread", _RecordingThread)

    win._zwift_watch_supported = False
    # A türelmi idő már rég lejárt volna
    win._zwift_grace_start = time.monotonic() - 10_000
    for _ in range(win._ZWIFT_CHECK_INTERVAL * 2 + 2):
        win._update()          # nem indíthat ellenőrző szálat
    assert started == []
    assert not win._update_error_seen, "az _update hibára futott"


def test_zwift_watch_active_on_windows(hud, monkeypatch):
    """Windows-on a figyelés változatlanul elindítja az ellenőrző szálat."""
    win, ctrl = hud
    ctrl.settings["hud"].close_at_zwiftapp_exe = True
    win._zwift_watch_supported = True

    started: list[object] = []

    class _FakeThread:
        def __init__(self, *a, **kw):
            started.append(kw.get("name"))

        def start(self):
            pass

    monkeypatch.setattr(
        "smart_fan_controller.ui.window.threading.Thread", _FakeThread)
    for _ in range(win._ZWIFT_CHECK_INTERVAL):
        win._update()
    assert "ZwiftProcessCheck" in started


def test_grace_period_close_still_works_when_supported(hud, monkeypatch):
    """Windows-on a lejárt türelmi idő továbbra is bezárja a HUD-ot."""
    win, _ctrl = hud
    win._zwift_watch_supported = True
    win._zwift_seen = False
    win._zwift_grace_start = time.monotonic() - 10_000

    invoked: list[str] = []
    monkeypatch.setattr(
        "smart_fan_controller.ui.window.QMetaObject.invokeMethod",
        lambda obj, name, conn: invoked.append(name),
    )
    win._check_zwift_process()
    assert invoked == ["close"]


# ─────────────────────────── hang: opcionális modul ───────────────────────────


def test_hud_survives_a_missing_qtmultimedia(monkeypatch, caplog):
    """QtMultimedia nélkül a hang néma, de a HUD nem esik szét.

    A ui csomag importálja a sound modult; a QtMultimedia ImportError-ja
    korábban az EGÉSZ HUD-ot megbuktatta, és az alkalmazás headless módba
    esett egy hiányzó hang-backend miatt.
    """
    import logging
    from smart_fan_controller.ui import sound as _sound

    monkeypatch.setattr(_sound, "_QT_MULTIMEDIA_AVAILABLE", False)
    monkeypatch.setattr(_sound, "_QT_MULTIMEDIA_ERROR", "libpulse.so.0 hiányzik",
                        raising=False)
    with caplog.at_level(logging.WARNING, logger="zwift_fan_controller_new"):
        mgr = LCARSSoundManager()
    assert mgr._effects == {}
    assert mgr.sound_duration_ms("hud_shutdown") == 0
    mgr.play("hud_startup")     # néma no-op, nem dobhat
    mgr.set_volume(0.4)
    mgr.cleanup()
    assert any("QtMultimedia" in r.getMessage() for r in caplog.records)

#!/usr/bin/env python3
"""Standalone HUD test harness – fake telemetry with a control panel.

The HUD window is NOT a copy: the production ``HUDWindow`` class imported
from ``smart_fan_controller.ui.window`` runs against a simulated (fake)
controller. Any later change to the HUD therefore shows up here
automatically – there is nothing to keep in sync.

The control panel can adjust:
  - power / heart-rate values (sliders or AUTO simulation)
  - power / heart-rate data source independently (ANT+ / BLE / Zwift UDP)
  - per-source signal on/off (dropout / FAIL / NO SIGNAL testing)
  - HIGHER WINS zone mode on/off
  - BLE fan enable and connection state (DISABLED/OFFLINE/ONLINE)
  - ZPO IMM / ZHR IMM tiles, cooldown simulation

Run from the project root (or anywhere):
    python hud_test/run_hud_test.py
"""
from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

# Put the project root on sys.path so the smart_fan_controller package
# imports no matter where the script is launched from
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QCheckBox, QComboBox, QSlider,
    QVBoxLayout, QFormLayout, QGroupBox,
)

from smart_fan_controller.config.schemas import (
    DataSource, ZoneMode, DatasourceConfig, GlobalSettingsConfig,
    HeartRateZonesConfig, HudConfig, PowerZonesConfig,
)
from smart_fan_controller.core.state import ControllerState
from smart_fan_controller.core.zones import (
    apply_zone_mode, calculate_hr_zones, calculate_power_zones,
    zone_for_hr, zone_for_power,
)

UPDATE_INTERVAL_MS = 500

# Source order of the combos (displayed caption → DataSource)
SOURCE_OPTIONS: list[tuple[str, DataSource]] = [
    ("ANT+", DataSource.ANTPLUS),
    ("BLE", DataSource.BLE),
    ("Zwift UDP", DataSource.ZWIFTUDP),
]


# ============================================================
# FAKE COMPONENTS – the interfaces HUDWindow reads
# ============================================================


class FakeBleFan:
    """The BLE fan fields read by the HUD."""

    def __init__(self) -> None:
        self.auth_failed = False
        self.is_connected = True
        self.last_sent_time = 0.0


class FakeSensorHandler:
    """BLE/ANT+ sensor handler – the HUD reads the lastdata timestamps."""

    def __init__(self) -> None:
        self.power_lastdata = 0.0
        self.hr_lastdata = 0.0


class FakeZwiftUdp:
    """Zwift UDP handler – the HUD reads the per-metric timestamps."""

    def __init__(self) -> None:
        self.last_packet_time = 0.0
        self.power_lastdata = 0.0
        self.hr_lastdata = 0.0


class FakeCooldown:
    """Cooldown controller – snapshot() → (active, remaining seconds)."""

    def __init__(self) -> None:
        self._end: float = 0.0

    def start(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def stop(self) -> None:
        self._end = 0.0

    def snapshot(self) -> tuple[bool, float]:
        remaining = self._end - time.monotonic()
        if remaining > 0:
            return True, remaining
        return False, 0.0


class FakeController:
    """Fake implementation of the controller interface used by HUDWindow.

    Contains only the members the HUD actually reads: settings,
    settings_file, state, ble_fan, cooldown_ctrl, plus the
    _ble_sensor_handler / _antplus_handler / _zwift_udp handlers.
    """

    def __init__(self) -> None:
        hud = HudConfig()
        hud.save_hud_settings = False       # test: never write files
        hud.close_at_zwiftapp_exe = False   # test: do not watch ZwiftApp.exe
        ds = DatasourceConfig()
        ds.power_source = DataSource.ZWIFTUDP
        ds.hr_source = DataSource.ZWIFTUDP
        self.settings: dict[str, object] = {
            "hud": hud,
            "datasource": ds,
            "power_zones": PowerZonesConfig(),
            "heart_rate_zones": HeartRateZonesConfig(),
            "global_settings": GlobalSettingsConfig(),
        }
        self.settings_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "settings.test.json"
        )
        self.state = ControllerState()
        self.ble_fan: FakeBleFan | None = FakeBleFan()
        self.cooldown_ctrl = FakeCooldown()
        self._ble_sensor_handler = FakeSensorHandler()
        self._antplus_handler = FakeSensorHandler()
        self._zwift_udp = FakeZwiftUdp()

    @staticmethod
    def is_process_running(name: str) -> bool:
        return False


# ============================================================
# SIMULATION + CONTROL PANEL
# ============================================================


class HudTestPanel(QWidget):
    """Control panel for driving the fake telemetry and HUD states."""

    def __init__(self, ctrl: FakeController) -> None:
        super().__init__()
        self._ctrl = ctrl
        self._fan = ctrl.ble_fan or FakeBleFan()
        self._t0 = time.monotonic()
        self._prev_zone: int | None = None

        self.setWindowTitle("HUD teszt vezérlő")
        self.setMinimumWidth(320)
        self.setStyleSheet(
            "QWidget { background-color: #101820; color: #DDEEFF; }"
            "QGroupBox { border: 1px solid #334455; border-radius: 6px;"
            "  margin-top: 8px; padding-top: 4px; font-weight: bold; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
            "QComboBox, QSlider { background-color: #1A2634; }"
        )

        root = QVBoxLayout(self)

        # ── Telemetry ──
        g_tel = QGroupBox("Telemetria (fake adat)")
        f_tel = QFormLayout(g_tel)
        self.chk_auto = QCheckBox("AUTO szimuláció (hullámzó power/pulzus)")
        self.chk_auto.setChecked(True)
        f_tel.addRow(self.chk_auto)
        self.sld_power, self.lbl_power = self._make_slider(0, 500, 180, "W")
        f_tel.addRow("Power:", self._wrap(self.sld_power, self.lbl_power))
        self.sld_hr, self.lbl_hr = self._make_slider(0, 220, 140, "BPM")
        f_tel.addRow("Pulzus:", self._wrap(self.sld_hr, self.lbl_hr))
        root.addWidget(g_tel)

        # ── Data sources ──
        g_src = QGroupBox("Adatforrások")
        f_src = QFormLayout(g_src)
        self.cmb_power_src = self._make_source_combo(DataSource.ZWIFTUDP)
        f_src.addRow("Power forrás:", self.cmb_power_src)
        self.cmb_hr_src = self._make_source_combo(DataSource.ZWIFTUDP)
        f_src.addRow("Pulzus forrás:", self.cmb_hr_src)
        self.chk_power_signal = QCheckBox("Power jel aktív (ki = dropout/FAIL)")
        self.chk_power_signal.setChecked(True)
        f_src.addRow(self.chk_power_signal)
        self.chk_hr_signal = QCheckBox("Pulzus jel aktív (ki = dropout/FAIL)")
        self.chk_hr_signal.setChecked(True)
        f_src.addRow(self.chk_hr_signal)
        root.addWidget(g_src)

        # ── Zone mode ──
        g_zone = QGroupBox("Zóna mód")
        f_zone = QFormLayout(g_zone)
        self.chk_higher_wins = QCheckBox("HIGHER WINS (ki = power only)")
        self.chk_higher_wins.setChecked(True)
        f_zone.addRow(self.chk_higher_wins)
        self.chk_zero_pwr = QCheckBox("ZPO IMM (zero_power_immediate)")
        f_zone.addRow(self.chk_zero_pwr)
        self.chk_zero_hr = QCheckBox("ZHR IMM (zero_hr_immediate)")
        f_zone.addRow(self.chk_zero_hr)
        root.addWidget(g_zone)

        # ── BLE fan ──
        g_fan = QGroupBox("BLE ventilátor")
        f_fan = QFormLayout(g_fan)
        self.chk_fan_enabled = QCheckBox("Engedélyezve (ki = DISABLED)")
        self.chk_fan_enabled.setChecked(True)
        f_fan.addRow(self.chk_fan_enabled)
        self.chk_fan_connected = QCheckBox("Kapcsolódva (ki = OFFLINE)")
        self.chk_fan_connected.setChecked(True)
        f_fan.addRow(self.chk_fan_connected)
        self.chk_fan_pin_fail = QCheckBox("PIN hiba (PIN FAIL)")
        f_fan.addRow(self.chk_fan_pin_fail)
        root.addWidget(g_fan)

        # ── Misc ──
        g_misc = QGroupBox("Egyéb")
        f_misc = QFormLayout(g_misc)
        self.chk_cooldown = QCheckBox("Cooldown indítása (120 s)")
        f_misc.addRow(self.chk_cooldown)
        root.addWidget(g_misc)

        hint = QLabel(
            "A HUD az éles smart_fan_controller.ui.window.HUDWindow –\n"
            "minden HUD módosítás automatikusan itt is megjelenik."
        )
        hint.setStyleSheet("color: #667788; font-size: 9pt;")
        root.addWidget(hint)
        root.addStretch()

        self.chk_cooldown.toggled.connect(self._on_cooldown_toggle)

        # Simulation stepper – same cadence as the HUD refresh
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(UPDATE_INTERVAL_MS)
        self._tick()

    # ────────── UI helpers ──────────

    @staticmethod
    def _make_slider(lo: int, hi: int, val: int, unit: str) -> tuple[QSlider, QLabel]:
        sld = QSlider(Qt.Orientation.Horizontal)
        sld.setRange(lo, hi)
        sld.setValue(val)
        lbl = QLabel(f"{val} {unit}")
        lbl.setMinimumWidth(64)
        sld.valueChanged.connect(lambda v: lbl.setText(f"{v} {unit}"))
        return sld, lbl

    @staticmethod
    def _wrap(*widgets: QWidget) -> QWidget:
        from PySide6.QtWidgets import QHBoxLayout
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        for w in widgets:
            lay.addWidget(w)
        return box

    @staticmethod
    def _make_source_combo(initial: DataSource) -> QComboBox:
        cmb = QComboBox()
        for label, src in SOURCE_OPTIONS:
            cmb.addItem(label, src)
        cmb.setCurrentIndex(
            next(i for i, (_l, s) in enumerate(SOURCE_OPTIONS) if s == initial)
        )
        return cmb

    def _on_cooldown_toggle(self, checked: bool) -> None:
        if checked:
            self._ctrl.cooldown_ctrl.start(120)
        else:
            self._ctrl.cooldown_ctrl.stop()

    # ────────── Simulation step ──────────

    def _tick(self) -> None:
        ctrl = self._ctrl
        now = time.monotonic()
        t = now - self._t0

        # 1) Propagate the panel settings into the fake config
        ds: DatasourceConfig = ctrl.settings["datasource"]  # type: ignore[assignment]
        ds.power_source = self.cmb_power_src.currentData()
        ds.hr_source = self.cmb_hr_src.currentData()

        hz: HeartRateZonesConfig = ctrl.settings["heart_rate_zones"]  # type: ignore[assignment]
        hz.zone_mode = (
            ZoneMode.HIGHER_WINS if self.chk_higher_wins.isChecked()
            else ZoneMode.POWER_ONLY
        )
        pz: PowerZonesConfig = ctrl.settings["power_zones"]  # type: ignore[assignment]
        pz.zero_power_immediate = self.chk_zero_pwr.isChecked()
        hz.zero_hr_immediate = self.chk_zero_hr.isChecked()

        # 2) BLE fan state
        if self.chk_fan_enabled.isChecked():
            ctrl.ble_fan = self._fan
            self._fan.is_connected = self.chk_fan_connected.isChecked()
            self._fan.auth_failed = self.chk_fan_pin_fail.isChecked()
        else:
            ctrl.ble_fan = None

        # 3) Telemetry values (AUTO wave or sliders)
        if self.chk_auto.isChecked():
            # Slow "workout" wave – sweeps through every zone
            power = max(0.0, 160 + 140 * math.sin(t / 18.0))
            hr = 130 + 45 * math.sin(t / 23.0 + 1.2)
            self.sld_power.blockSignals(True)
            self.sld_power.setValue(int(power))
            self.sld_power.blockSignals(False)
            self.lbl_power.setText(f"{power:.0f} W")
            self.sld_hr.blockSignals(True)
            self.sld_hr.setValue(int(hr))
            self.sld_hr.blockSignals(False)
            self.lbl_hr.setText(f"{hr:.0f} BPM")
        else:
            power = float(self.sld_power.value())
            hr = float(self.sld_hr.value())

        power_ok = self.chk_power_signal.isChecked()
        hr_ok = self.chk_hr_signal.isChecked()

        # 4) "Live data" handler timestamps for the selected source
        if power_ok:
            if ds.power_source == DataSource.BLE:
                ctrl._ble_sensor_handler.power_lastdata = now
            elif ds.power_source == DataSource.ANTPLUS:
                ctrl._antplus_handler.power_lastdata = now
            elif ds.power_source == DataSource.ZWIFTUDP:
                ctrl._zwift_udp.power_lastdata = now
        if hr_ok:
            if ds.hr_source == DataSource.BLE:
                ctrl._ble_sensor_handler.hr_lastdata = now
            elif ds.hr_source == DataSource.ANTPLUS:
                ctrl._antplus_handler.hr_lastdata = now
            elif ds.hr_source == DataSource.ZWIFTUDP:
                ctrl._zwift_udp.hr_lastdata = now
        if (power_ok and ds.power_source == DataSource.ZWIFTUDP) or (
            hr_ok and ds.hr_source == DataSource.ZWIFTUDP
        ):
            ctrl._zwift_udp.last_packet_time = now

        # 5) Zone calculation with the PRODUCTION zone logic (core.zones)
        p_zones = calculate_power_zones(
            pz.ftp, pz.min_watt, pz.max_watt, pz.z1_max_percent, pz.z2_max_percent
        )
        h_zones = calculate_hr_zones(
            hz.max_hr, hz.resting_hr, hz.z1_max_percent, hz.z2_max_percent
        )
        power_zone = zone_for_power(power, p_zones) if power_ok else None
        hr_zone = zone_for_hr(int(hr), h_zones) if hr_ok else None
        zone = apply_zone_mode(power_zone, hr_zone, hz.zone_mode)

        # 6) Snapshot update – the HUD reads from here
        ctrl.state.ui_snapshot.update(
            zone,
            power if power_ok else None,
            hr if hr_ok else None,
        )

        # 7) "Send a command" to the fan on zone change (LAST TX / sound)
        if (
            zone is not None
            and zone != self._prev_zone
            and ctrl.ble_fan is not None
            and ctrl.ble_fan.is_connected
        ):
            ctrl.ble_fan.last_sent_time = now
        self._prev_zone = zone


def main() -> int:
    app = QApplication(sys.argv)

    # Import the HUD after the QApplication exists – a missing package
    # yields a usable traceback instead of a Qt error
    from smart_fan_controller.ui.window import HUDWindow

    ctrl = FakeController()
    panel = HudTestPanel(ctrl)
    hud = HUDWindow(ctrl, app)

    # Place the panel next to the HUD; closing either window quits
    hud.move(60, 60)
    panel.move(60 + hud.width() + 30, 60)
    panel.show()
    app.lastWindowClosed.connect(app.quit)

    hud._restore_geometry()  # no-op without saved geometry
    hud.show()
    hud.sound.play("hud_startup")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

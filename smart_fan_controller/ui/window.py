#!/usr/bin/env python3
# pyright: reportInvalidTypeForm=false
"""Main floating HUD window (PySide6) – Star Trek LCARS style.

The window is a frameless, always-on-top, translucent-background card:
the rounded base panel is painted here, the decorated bars and indicators
live in :mod:`smart_fan_controller.ui.widgets` and the sound effects in
:mod:`smart_fan_controller.ui.sound`.
"""

from __future__ import annotations

import logging
import os
import platform as _platform
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QTimer, QPoint, QSize, QRectF, QMetaObject
from PySide6.QtGui import (
    QFont, QFontDatabase, QMouseEvent, QPainter, QPainterPath, QPalette,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QHBoxLayout, QVBoxLayout,
    QSlider, QMenu, QFrame, QSizePolicy,
)

from smart_fan_controller.config import DataSource, ZoneMode
from smart_fan_controller.config.loader import (
    HudConfig, DatasourceConfig, save_hud_settings_only,
)
from smart_fan_controller.ui import theme
from smart_fan_controller.ui.sound import LCARSSoundManager
from smart_fan_controller.ui.theme import qcolor
from smart_fan_controller.ui.widgets import (
    LCARSFooterWidget, LCARSHeaderWidget, LCARSMeterWidget,
    LCARSSidebarWidget, LCARSZoneBarWidget,
)

if TYPE_CHECKING:
    # FanController lives in smart_fan_controller.controller. The type is
    # treated as Any here to avoid a circular import; the controller is a
    # loosely coupled, pass-through object.
    FanController = Any

logger = logging.getLogger("zwift_fan_controller_new")


class HUDWindow(QWidget):
    """Floating, translucent HUD window – Star Trek LCARS telemetry display."""

    # ─── LCARS palette – class attributes kept for backwards compatibility,
    #     the values live in smart_fan_controller.ui.theme ───
    BG = theme.BG
    PANEL_BG = theme.PANEL_BG
    LCARS_ORANGE = theme.LCARS_ORANGE
    LCARS_GOLD = theme.LCARS_GOLD
    LCARS_BLUE = theme.LCARS_BLUE
    LCARS_CYAN = theme.LCARS_CYAN
    LCARS_CYAN_DIM = theme.LCARS_CYAN_DIM
    LCARS_RED = theme.LCARS_RED
    LCARS_MAGENTA = theme.LCARS_MAGENTA
    LCARS_TAN = theme.LCARS_TAN
    LCARS_PURPLE = theme.LCARS_PURPLE
    TEXT_BRIGHT = theme.TEXT_BRIGHT
    TEXT_DIM = theme.TEXT_DIM
    BORDER_GLOW = theme.BORDER_GLOW
    ZONE_COLORS = theme.ZONE_COLORS
    ZONE_NAMES = theme.ZONE_NAMES
    _VAL_BG = theme.VAL_BG

    UPDATE_INTERVAL_MS = 500

    # Absolute minimum window size (applies during interactive resizing;
    # the content-based minimum is restored after the drag settles)
    MIN_W = 220
    MIN_H = 300

    def __init__(self, controller: "FanController", app: "QApplication") -> None:
        super().__init__()
        self._base_width = 340
        self._base_height = 460
        self._scale = 1.0
        self._ctrl = controller
        self._app = app
        self._drag_pos: QPoint | None = None
        self._resize_active = False
        self._resize_start_pos = QPoint()
        self._resize_start_size = QSize()

        # Scalable text labels: (label, base pt size, fixed width or None, bold)
        self._scalable_texts: list[tuple[QLabel, int, int | None, bool]] = []

        # Flash effect: previous values and flash counters
        self._prev_power: float | None = None
        self._prev_hr: float | None = None
        self._flash_power: int = 0  # remaining flash cycles
        self._flash_hr: int = 0
        self._flash_ble_tick: int = 0  # continuous blink counter

        # ───────── LCARS SOUND EFFECTS ─────────
        self._sound = LCARSSoundManager()
        hud_cfg: HudConfig = self._ctrl.settings["hud"]
        self._sound.set_enabled(hud_cfg.sound_enabled)
        self._sound.set_volume(hud_cfg.sound_volume)
        self._prev_zone: int | None = None
        self._prev_ble_status: str | None = None
        self._prev_ant_status: str | None = None
        self._prev_zwift_status: str | None = None
        self._prev_last_sent_time: float = 0.0

        # ───────── ZWIFT PROCESS MONITOR ─────────
        self._zwift_was_running = False
        self._zwift_seen = False           # True once we saw it running
        self._zwift_check_counter = 0
        self._ZWIFT_CHECK_INTERVAL = 20    # every 20th _update call ≈ 10 s
        self._zwift_check_running = False  # race-condition guard
        self._zwift_grace_start: float = time.time()
        self._ZWIFT_GRACE_PERIOD: float = 300.0  # wait 5 minutes for launch

        # "Live data" window for BLE/ANT sensors. Deliberately generous:
        # bike meters may go quiet while coasting / at 0 W – they should
        # not flash FAIL all the time.
        self._SENSOR_STALE_S: float = 10.0

        # ───────── WINDOW SETUP ─────────
        self.setWindowTitle("LCARS Fan HUD")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        # Translucent window background: the rounded base panel is painted
        # in paintEvent, so the corners are genuinely transparent (modern,
        # floating-card look)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._initial_opacity = max(20, min(100, hud_cfg.opacity))
        self.setWindowOpacity(self._initial_opacity / 100.0)
        self.setGeometry(20, 20, self._base_width, self._base_height)
        self.setMinimumSize(self.MIN_W, self.MIN_H)
        self.setStyleSheet("background-color: transparent;")

        # ───────── FONT ─────────
        self._try_load_lcars_font()
        self._font_family = self._detect_best_font()

        # ───────── LAYOUT ─────────
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Header
        self._header = LCARSHeaderWidget(self, self._font_family, self._scale)
        main_layout.addWidget(self._header)

        # Body (sidebar + content)
        body = QWidget(self)
        body.setStyleSheet("background-color: transparent;")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self._sidebar = LCARSSidebarWidget(body, self._scale)
        body_layout.addWidget(self._sidebar)

        # Content panel
        content = QWidget(body)
        content.setStyleSheet(f"background-color: {self.PANEL_BG};")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(6, 8, 6, 0)
        content_layout.setSpacing(0)
        body_layout.addWidget(content, 1)

        # ───────── ZONE DISPLAY ─────────
        self._lbl_zone_label = QLabel("FAN ZONE")
        self._lbl_zone_label.setStyleSheet(
            f"background-color: {self.LCARS_CYAN}; color: #000a14; "
            f"padding: 2px 4px; border-radius: 4px;"
        )
        self._lbl_zone_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._register_scalable(self._lbl_zone_label, 12)
        content_layout.addWidget(self._lbl_zone_label)

        self._lbl_zone = QLabel("– – –")
        self._lbl_zone.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Text color comes from QPalette (dynamic); the stylesheet only
        # carries the static part
        self._lbl_zone.setStyleSheet(
            f"background-color: {self._VAL_BG}; "
            f"padding: 3px 6px; border-radius: 4px;"
        )
        self._set_label_color(self._lbl_zone, self.LCARS_CYAN)
        self._lbl_zone.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._register_scalable(self._lbl_zone, 19)
        content_layout.addWidget(self._lbl_zone)

        # Zone segment bar below the zone display
        content_layout.addSpacing(3)
        self._zone_bar = LCARSZoneBarWidget(content, self._scale)
        content_layout.addWidget(self._zone_bar)
        content_layout.addSpacing(3)

        # ───────── STATUS STRIP (tiles) ─────────
        tile_frame = QWidget(content)
        tile_frame.setStyleSheet(f"background-color: {self.PANEL_BG};")
        tile_layout = QHBoxLayout(tile_frame)
        tile_layout.setContentsMargins(0, 0, 0, 4)
        tile_layout.setSpacing(2)

        self._tile_zero_imm = self._make_tile(tile_layout, "ZPO IMM", self.LCARS_CYAN)
        self._tile_zero_hr_imm = self._make_tile(tile_layout, "ZHR IMM", self.LCARS_CYAN)
        self._tile_higher_wins = self._make_tile(tile_layout, "HI WINS", self.LCARS_ORANGE)
        self._tile_ant = self._make_tile(tile_layout, "ANT+", self.LCARS_PURPLE)
        self._tile_ble = self._make_tile(tile_layout, "BLE", self.LCARS_BLUE)
        self._tile_cooldown = self._make_tile(tile_layout, "COOL", self.LCARS_GOLD)
        content_layout.addWidget(tile_frame)

        # ───────── TELEMETRY ROWS ─────────
        self._lbl_power = self._make_row(content_layout, "POWER", "– – –",
                                          self.LCARS_GOLD, self.LCARS_TAN)
        self._power_meter = LCARSMeterWidget(content, self._scale)
        content_layout.addWidget(self._power_meter)
        self._lbl_hr = self._make_row(content_layout, "HEART RATE", "– – –",
                                       self.LCARS_RED, self.LCARS_ORANGE)
        self._hr_meter = LCARSMeterWidget(content, self._scale)
        content_layout.addWidget(self._hr_meter)

        # ───────── SEPARATOR ─────────
        sep = QFrame(content)
        sep.setFixedHeight(2)
        sep.setStyleSheet(f"background-color: {self.BORDER_GLOW}; margin: 6px 10px;")
        content_layout.addWidget(sep)

        # ───────── SYSTEM STATUS ─────────
        self._lbl_ble = self._make_status_row(content_layout, "BLE FAN", "OFFLINE",
                                               self.LCARS_BLUE)
        self._lbl_ble_sens = self._make_status_row(content_layout, "BLE SEN.",
                                                     "– – –", self.LCARS_BLUE)
        self._lbl_ant = self._make_status_row(content_layout, "ANT+ SEN.",
                                               "– – –", self.LCARS_PURPLE)
        self._lbl_zwift_udp = self._make_status_row(content_layout, "ZWIFT",
                                                      "– – –", self.LCARS_PURPLE)

        # ───────── SEPARATOR 2 ─────────
        sep2 = QFrame(content)
        sep2.setFixedHeight(2)
        sep2.setStyleSheet(f"background-color: {self.BORDER_GLOW}; margin: 6px 10px;")
        content_layout.addWidget(sep2)

        # ───────── SYSTEM INFO ─────────
        self._lbl_last_sent = self._make_status_row(content_layout, "LAST TX",
                                                      "– – –", self.LCARS_TAN)
        self._lbl_cool = self._make_status_row(content_layout, "COOLDOWN",
                                                "– – –", self.LCARS_TAN)

        # ───────── OPACITY CONTROLS (into the footer's upper strip) ─────────
        self._opacity_label = QLabel("OPACITY")
        self._opacity_label.setStyleSheet(
            f"background-color: {self.LCARS_GOLD}; color: #000a14; "
            f"font-family: '{self._font_family}'; font-size: 9pt; font-weight: bold; "
            f"padding: 2px 4px; border-radius: 4px;"
        )

        self._alpha_slider = QSlider(Qt.Orientation.Horizontal)
        self._alpha_slider.setRange(20, 100)
        self._alpha_slider.setValue(self._initial_opacity)
        self._alpha_slider.setStyleSheet(
            f"QSlider::groove:horizontal {{"
            f"  background: #002244; height: 8px; border-radius: 4px;"
            f"}}"
            f"QSlider::sub-page:horizontal {{"
            f"  background: {self.LCARS_CYAN}; border-radius: 4px;"
            f"}}"
            f"QSlider::handle:horizontal {{"
            f"  background: #EAF6FF; width: 14px; margin: -3px 0;"
            f"  border-radius: 7px;"
            f"}}"
        )
        self._alpha_slider.valueChanged.connect(self._on_alpha_change)

        self._alpha_value = QLabel(f"{self._initial_opacity}%")
        self._alpha_value.setStyleSheet(
            f"color: {self.LCARS_CYAN}; background-color: transparent;"
        )
        self._alpha_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._register_scalable(self._alpha_value, 11, 40)

        # The STARFLEET caption takes the slider's former place
        self._footer_brand = QLabel("STARFLEET CYCLING DIV")
        self._footer_brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._footer_brand.setStyleSheet(
            f"color: {self.LCARS_CYAN_DIM}; background-color: {self.PANEL_BG}; "
            f"padding: 6px 0 4px 0;"
        )
        self._register_scalable(self._footer_brand, 9, bold=False)
        content_layout.addWidget(self._footer_brand)
        content_layout.addStretch()

        main_layout.addWidget(body, 1)

        # Footer – the opacity slider goes into its upper strip
        self._footer = LCARSFooterWidget(self, self._font_family, self._scale)
        self._footer.set_opacity_controls(
            self._opacity_label, self._alpha_slider, self._alpha_value
        )
        main_layout.addWidget(self._footer)

        # The content-based minimum size is recalculated with a debounce:
        # raising the minimum during a drag would block shrinking and make
        # the window jump around
        self._min_size_timer = QTimer(self)
        self._min_size_timer.setSingleShot(True)
        self._min_size_timer.setInterval(200)
        self._min_size_timer.timeout.connect(self._update_min_size)

        # Debounced automatic geometry save: the position survives even
        # when the program does not shut down cleanly
        self._geo_save_timer = QTimer(self)
        self._geo_save_timer.setSingleShot(True)
        self._geo_save_timer.setInterval(2500)
        self._geo_save_timer.timeout.connect(self._auto_save_geometry)

        # Debounced opacity save: no file write on every slider tick while
        # dragging, only once when the adjustment settles
        self._opacity_save_timer = QTimer(self)
        self._opacity_save_timer.setSingleShot(True)
        self._opacity_save_timer.setInterval(800)
        self._opacity_save_timer.timeout.connect(
            lambda: self._save_hud_setting("opacity", self._alpha_slider.value())
        )

        # The minimum window size follows the readable size of the content
        self._update_min_size()

        # ───────── CONTEXT MENU ─────────
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

        # ───────── TIMER ─────────
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update)
        self._timer.start(self.UPDATE_INTERVAL_MS)

        # From here on resizeEvent may apply the scale (the UI is complete)
        self._ui_ready = True

    @property
    def sound(self) -> "LCARSSoundManager":
        return self._sound

    # ────────── FONT LOADING ──────────

    def _try_load_lcars_font(self) -> None:
        """Load the Antonio font from the package fonts/ directory.

        Search order:
          1. <package_dir>/fonts/Antonio-{Bold,Regular}.ttf
          2. <exe_dir>/smart_fan_controller/fonts/...   (PyInstaller frozen)
        When the fonts are absent a system font is used as fallback.
        """
        if _platform.system() != "Windows":
            return
        try:
            if getattr(sys, "frozen", False):
                base_dir = os.path.join(
                    os.path.dirname(os.path.abspath(sys.executable)),
                    "smart_fan_controller",
                )
            else:
                # window.py lives in smart_fan_controller/ui/ → package root is one up
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            font_dir = os.path.join(base_dir, "fonts")

            loaded = 0
            for style in ("Bold", "Regular"):
                fpath = os.path.join(font_dir, f"Antonio-{style}.ttf")
                if os.path.exists(fpath):
                    QFontDatabase.addApplicationFont(fpath)
                    loaded += 1

            if loaded == 0:
                logger.info(
                    "LCARS fontok nem találhatók a %s mappában – "
                    "rendszer font használata. Lásd: fonts/README.txt", font_dir
                )
        except Exception as exc:
            logger.warning(
                "LCARS font betöltés sikertelen (rendszer font használata): %s", exc
            )

    def _detect_best_font(self) -> str:
        """Pick the best available LCARS-looking font."""
        try:
            available = set(QFontDatabase.families())
        except Exception as exc:
            logger.debug("Font lista lekérés sikertelen: %s", exc)
            return "Consolas"

        preferred = [
            "Antonio", "Michroma", "Century Gothic", "Eras Bold ITC",
            "Eras Medium ITC", "Bahnschrift", "Trebuchet MS", "Segoe UI", "Consolas",
        ]
        for f in preferred:
            if f in available:
                return f
        return "Consolas"

    # ────────── UI BUILD HELPERS ──────────

    def _make_row(self, layout: "QVBoxLayout", label: str, value: str,
                  color: str, label_bg: str) -> "QLabel":
        """Telemetry row with an LCARS colored label background."""
        row = QWidget()
        row.setStyleSheet(f"background-color: {self.PANEL_BG};")
        row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 2, 0, 2)
        row_layout.setSpacing(2)

        key_lbl = QLabel(label)
        key_lbl.setStyleSheet(
            f"background-color: {label_bg}; color: #000a14; "
            f"padding: 3px 4px; border-radius: 4px;"
        )
        row_layout.addWidget(key_lbl)
        self._register_scalable(key_lbl, 9, 100)

        val_lbl = QLabel(value)
        val_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        # Text color comes from QPalette (dynamic); stylesheet is static only
        val_lbl.setStyleSheet(
            f"background-color: {self._VAL_BG}; "
            f"padding: 3px 6px; border-radius: 4px;"
        )
        self._set_label_color(val_lbl, color)
        row_layout.addWidget(val_lbl, 1)
        self._register_scalable(val_lbl, 14)

        layout.addWidget(row)
        return val_lbl

    def _make_tile(self, layout: "QHBoxLayout", text: str, accent: str) -> "QLabel":
        """Status strip tile – its background is driven by the "hudState"
        dynamic property ("off"/"on"/"flash"); the colors are defined once
        in the stylesheet selectors."""
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setProperty("hudState", "off")
        # Off: dim outlined pill; on: accent fill
        lbl.setStyleSheet(
            f'QLabel {{ background-color: transparent; color: {self.TEXT_DIM}; '
            f'border: 1px solid {self.BORDER_GLOW}; '
            f'padding: 1px 4px; border-radius: 4px; }}'
            f'QLabel[hudState="on"] {{ background-color: {accent}; '
            f'color: #000a14; border-color: {accent}; }}'
            f'QLabel[hudState="flash"] {{ '
            f'background-color: {theme.lighten(accent)}; '
            f'color: #000a14; border-color: {theme.lighten(accent)}; }}'
        )
        # The caption must not get clipped (Minimum) nor squashed (Fixed)
        lbl.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self._register_scalable(lbl, 9)
        layout.addWidget(lbl, 1)
        return lbl

    def _make_status_row(self, layout: "QVBoxLayout", label: str, value: str,
                         label_bg: str) -> "QLabel":
        """Status row with an LCARS colored label background."""
        row = QWidget()
        row.setStyleSheet(f"background-color: {self.PANEL_BG};")
        row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 2, 0, 2)
        row_layout.setSpacing(2)

        key_lbl = QLabel(label)
        key_lbl.setStyleSheet(
            f"background-color: {label_bg}; color: #000a14; "
            f"padding: 2px 4px; border-radius: 4px;"
        )
        row_layout.addWidget(key_lbl)
        self._register_scalable(key_lbl, 9, 100)

        val_lbl = QLabel(value)
        val_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        # Text color comes from QPalette (dynamic); stylesheet is static only
        val_lbl.setStyleSheet(
            f"background-color: {self._VAL_BG}; "
            f"padding: 2px 6px; border-radius: 4px;"
        )
        self._set_label_color(val_lbl, self.TEXT_DIM)
        row_layout.addWidget(val_lbl, 1)
        self._register_scalable(val_lbl, 11, bold=False)

        layout.addWidget(row)
        return val_lbl

    # ────────── BASE PANEL (rounded card) ──────────

    def paintEvent(self, event: Any) -> None:
        """Paint the rounded base card – its radius equals the corner_r of
        the header/footer, so the LCARS sweeps follow the card contour
        exactly; the area outside the corners stays transparent."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = max(12, int(18 * self._scale))
        card = QPainterPath()
        card.addRoundedRect(QRectF(self.rect()), radius, radius)
        p.fillPath(card, theme.qbrush(self.BG))
        p.end()

    # ────────── DRAG / RESIZE ──────────

    def mousePressEvent(self, event: "QMouseEvent") -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            wh = self.windowHandle()
            # The resize corner grows with the scale so it stays easy to
            # grab on a large HUD as well
            grip = max(20, int(20 * self._scale))
            if (self.width() - pos.x() < grip) and (self.height() - pos.y() < grip):
                # For the duration of the drag allow the absolute minimum
                # size so the window can be shrunk in one motion; the
                # content-based minimum returns via the debounce timer
                self.setMinimumSize(self.MIN_W, self.MIN_H)
                self._min_size_timer.start()
                # Native (system) resize; manual fallback when the platform
                # does not support it
                if wh is None or not wh.startSystemResize(
                    Qt.Edge.RightEdge | Qt.Edge.BottomEdge
                ):
                    self._resize_active = True
                    self._resize_start_pos = event.globalPosition().toPoint()
                    self._resize_start_size = self.size()
            else:
                # Native window move (Windows snap etc.); manual fallback
                if wh is None or not wh.startSystemMove():
                    self._drag_pos = (
                        event.globalPosition().toPoint()
                        - self.frameGeometry().topLeft()
                    )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: "QMouseEvent") -> None:  # type: ignore[override]
        if self._resize_active:
            delta = event.globalPosition().toPoint() - self._resize_start_pos
            new_w = max(self.minimumWidth(),
                        self._resize_start_size.width() + delta.x())
            new_h = max(self.minimumHeight(),
                        self._resize_start_size.height() + delta.y())
            self.resize(new_w, new_h)
        elif self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: "QMouseEvent") -> None:  # type: ignore[override]
        self._drag_pos = None
        self._resize_active = False
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event: Any) -> None:
        """Recompute the scale on every resize – no mouseMoveEvent arrives
        during a native (startSystemResize) resize, hence it lives here.

        The scale is the minimum of the width AND height ratios, so the
        text still fits when only the height is reduced (rows do not
        collapse into each other)."""
        super().resizeEvent(event)
        if not getattr(self, "_ui_ready", False):
            return
        new_scale = min(self.width() / self._base_width,
                        self.height() / self._base_height)
        if abs(new_scale - self._scale) >= 0.001:
            self._scale = new_scale
            self._apply_scale()
        # The content-based minimum is only refreshed after the drag stops
        self._min_size_timer.start()
        self._geo_save_timer.start()

    def moveEvent(self, event: Any) -> None:
        """Schedule a debounced geometry save after a move."""
        super().moveEvent(event)
        if getattr(self, "_ui_ready", False):
            self._geo_save_timer.start()

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        super().keyPressEvent(event)

    # ────────── OPACITY ──────────

    def _set_alpha_from_menu(self, percent: int) -> None:
        # setValue emits valueChanged → _on_alpha_change performs the actual
        # change and the (debounced) save
        self._alpha_slider.setValue(percent)

    def _on_alpha_change(self, value: int) -> None:
        self.setWindowOpacity(value / 100.0)
        self._alpha_value.setText(f"{value}%")
        # Update in memory immediately; the file write is debounced
        hud_cfg: HudConfig = self._ctrl.settings["hud"]
        hud_cfg.opacity = value
        self._opacity_save_timer.start()

    # ────────── CONTEXT MENU ──────────

    def _show_menu(self, pos: "QPoint") -> None:
        menu_ss = (
            f"QMenu {{ background-color: #001828; color: {self.LCARS_CYAN}; "
            f"font-family: '{self._font_family}'; font-size: 10pt; }}"
            f"QMenu::item:selected {{ background-color: {self.LCARS_BLUE}; "
            f"color: white; }}"
        )
        menu = QMenu(self)
        menu.setStyleSheet(menu_ss)
        menu.addAction("Bezárás", self.close)

        menu.addSeparator()
        menu.addAction("Opacity: 50%", lambda: self._set_alpha_from_menu(50))
        menu.addAction("Opacity: 85%", lambda: self._set_alpha_from_menu(85))
        menu.addAction("Opacity: 100%", lambda: self._set_alpha_from_menu(100))

        # ─── LCARS SOUND SETTINGS ───
        menu.addSeparator()
        sound_enabled = self._sound.enabled
        toggle_label = "🔊 Hang: KI" if sound_enabled else "🔇 Hang: BE"
        menu.addAction(toggle_label, self._toggle_sound)

        vol_menu = menu.addMenu("🔉 Hangerő")
        vol_menu.setStyleSheet(menu_ss)
        current = round(self._sound.volume * 100)
        for pct in (25, 50, 75, 100):
            v = pct / 100.0
            marker = " ◄" if pct == current else ""
            vol_menu.addAction(
                f"{pct}%{marker}", lambda _v=v: self._set_sound_volume(_v)
            )

        menu.exec(self.mapToGlobal(pos))

    def _toggle_sound(self) -> None:
        """Toggle sound effects and persist to settings.json."""
        new_state = not self._sound.enabled
        self._sound.set_enabled(new_state)
        self._save_hud_setting("sound_enabled", new_state)

    def _set_sound_volume(self, volume: float) -> None:
        """Set the volume and persist to settings.json."""
        self._sound.set_volume(volume)
        self._save_hud_setting("sound_volume", round(volume, 2))

    def _save_hud_setting(self, key: str, value: Any) -> None:
        """Update one HUD setting and save it (only when save_hud_settings=True).

        Updates the in-memory HUD config, then – when saving is enabled –
        persists only the "hud" section of the JSON (not the whole settings,
        preserving manual edits in the other sections).
        """
        settings = self._ctrl.settings
        hud_cfg: HudConfig = settings["hud"]
        # Map old key names to dataclass attribute names
        attr = key.replace(".", "_") if "." in key else key
        if hasattr(hud_cfg, attr):
            setattr(hud_cfg, attr, value)
            if save_hud_settings_only(self._ctrl.settings_file, hud_cfg):
                logger.info("HUD beállítás mentve: hud.%s = %s", key, value)
            elif hud_cfg.save_hud_settings:
                # save_hud_settings was True but the write failed
                logger.warning("HUD beállítás nem sikerült menteni: hud.%s = %s",
                               key, value)
            # When save_hud_settings=False there is no log line (intentional)

    # ────────── LABEL UPDATE HELPERS ──────────

    @staticmethod
    def _set_label_color(lbl: "QLabel", color: str) -> None:
        """Set the label text color via QPalette – the stylesheet carries no
        color property, so the palette applies without a repolish."""
        pal = lbl.palette()
        pal.setColor(QPalette.ColorRole.WindowText, qcolor(color))
        lbl.setPalette(pal)
        lbl._hud_color = color

    @staticmethod
    def _update_label(lbl: "QLabel", text: str, color: str) -> None:
        """Update label text and color – only on actual change."""
        if getattr(lbl, "_hud_color", None) != color:
            HUDWindow._set_label_color(lbl, color)
        if getattr(lbl, "_hud_text", None) != text:
            lbl.setText(text)
            lbl._hud_text = text

    @staticmethod
    def _set_tile_state(tile: "QLabel", state: str) -> None:
        """Update a tile state ("off"/"on"/"flash") – the background comes
        from the property selectors of the tile's stylesheet; unpolish/polish
        applies the new rule after a property change."""
        if tile.property("hudState") == state:
            return
        tile.setProperty("hudState", state)
        style = tile.style()
        style.unpolish(tile)
        style.polish(tile)

    @staticmethod
    def _lighten(color_hex: str, factor: float = 0.35) -> str:
        """Backwards-compatible alias for :func:`theme.lighten`."""
        return theme.lighten(color_hex, factor)

    def _update_sensor_row(self, lbl: "QLabel", handler: Any, use_power: bool,
                           use_hr: bool, flash_white: bool, now: float) -> bool:
        """Update one sensor status row in the shared ``P:..  HR:..`` format.

        Works for the BLE, ANT+ and Zwift UDP handlers alike – all of them
        expose ``power_lastdata`` / ``hr_lastdata`` monotonic timestamps.
        A metric is OK when it is selected and its data is fresh; ``--``
        when not selected; FAIL (blinking red) when selected but stale.

        Returns True when every selected metric is alive (used by the
        callers to trigger the reconnect/dropout sounds).
        """
        power_ok = (
            use_power
            and (handler.power_lastdata > 0)
            and (now - handler.power_lastdata < self._SENSOR_STALE_S)
        )
        hr_ok = (
            use_hr
            and (handler.hr_lastdata > 0)
            and (now - handler.hr_lastdata < self._SENSOR_STALE_S)
        )
        p_s = "OK" if power_ok else ("--" if not use_power else "FAIL")
        h_s = "OK" if hr_ok else ("--" if not use_hr else "FAIL")

        states: list[bool] = []
        if use_power:
            states.append(power_ok)
        if use_hr:
            states.append(hr_ok)

        alive = all(states)
        if alive:
            row_color = self.LCARS_CYAN
        else:
            row_color = theme.lighten(self.LCARS_RED) if flash_white else self.LCARS_RED

        self._update_label(lbl, f"P:{p_s}  HR:{h_s}", row_color)
        return alive

    # ────────── UPDATE (every 500 ms) ──────────

    def _update(self) -> None:
        try:
            state = self._ctrl.state
            ble_fan = self._ctrl.ble_fan
            cool = self._ctrl.cooldown_ctrl
            settings = self._ctrl.settings
            now = time.monotonic()

            if state is not None:
                zone, power, hr = state.ui_snapshot.read()

                zone_color = (
                    self.ZONE_COLORS.get(zone, self.LCARS_CYAN)
                    if zone is not None else self.TEXT_DIM
                )
                zone_txt = (
                    self.ZONE_NAMES.get(zone, "– – –")
                    if zone is not None else "– – –"
                )

                self._update_label(self._lbl_zone, zone_txt, zone_color)
                self._zone_bar.set_zone(zone)

                # Zone change sound
                if zone is not None and zone != self._prev_zone and self._prev_zone is not None:
                    if zone == 0:
                        self._sound.play("zone_standby")
                    elif zone > self._prev_zone:
                        self._sound.play("zone_up")
                    else:
                        self._sound.play("zone_down")
                self._prev_zone = zone

                # Power – flash on change
                if power is not None and power != self._prev_power:
                    self._flash_power = 2  # 2 cycles ≈ 1 s flash
                self._prev_power = power

                if self._flash_power > 0:
                    self._flash_power -= 1
                    power_color = theme.lighten(self.LCARS_GOLD) if self._flash_power % 2 == 1 else self.LCARS_GOLD
                else:
                    power_color = self.LCARS_GOLD if power is not None else self.TEXT_DIM

                self._update_label(
                    self._lbl_power,
                    "– – –" if power is None else f"{power:.0f} W",
                    power_color,
                )

                # Power meter – fill relative to FTP, colored by the power
                # zone thresholds (independent of the combined zone)
                pz = settings["power_zones"]
                if power is not None and pz.ftp > 0:
                    z1_thr = pz.ftp * pz.z1_max_percent / 100.0
                    z2_thr = pz.ftp * pz.z2_max_percent / 100.0
                    if power <= z1_thr:
                        m_color = self.ZONE_COLORS[1]
                    elif power <= z2_thr:
                        m_color = self.ZONE_COLORS[2]
                    else:
                        m_color = self.ZONE_COLORS[3]
                    self._power_meter.set_value(power / (pz.ftp * 1.25), m_color)
                else:
                    self._power_meter.set_value(None, self.TEXT_DIM)

                # HR – flash on change
                if hr is not None and hr != self._prev_hr:
                    self._flash_hr = 2
                self._prev_hr = hr

                if self._flash_hr > 0:
                    self._flash_hr -= 1
                    hr_color = theme.lighten(self.LCARS_RED) if self._flash_hr % 2 == 1 else self.LCARS_RED
                else:
                    hr_color = self.LCARS_RED if hr is not None else self.TEXT_DIM

                self._update_label(
                    self._lbl_hr,
                    "– – –" if hr is None else f"{hr:.0f} BPM",
                    hr_color,
                )

                # HR meter – fill between resting and max heart rate,
                # colored by the HR zone thresholds (% of max_hr)
                hz = settings["heart_rate_zones"]
                if hr is not None and hz.max_hr > hz.resting_hr:
                    hr_frac = (hr - hz.resting_hr) / (hz.max_hr - hz.resting_hr)
                    if hr <= hz.max_hr * hz.z1_max_percent / 100.0:
                        m_color = self.ZONE_COLORS[1]
                    elif hr <= hz.max_hr * hz.z2_max_percent / 100.0:
                        m_color = self.ZONE_COLORS[2]
                    else:
                        m_color = self.ZONE_COLORS[3]
                    self._hr_meter.set_value(hr_frac, m_color)
                else:
                    self._hr_meter.set_value(None, self.TEXT_DIM)

            # BLE fan – blinking for the OFFLINE/PIN FAIL states.
            # Monotonic counter: the blinking rows blink out of PHASE (not
            # in sync) – period 4 ticks (~2 s), 50% duty cycle.
            self._flash_ble_tick += 1
            _ft = self._flash_ble_tick
            flash_white = (_ft + 0) % 4 < 2      # BLE FAN phase
            ble_status = "DISABLED"
            if ble_fan is not None:
                if ble_fan.auth_failed:
                    c = theme.lighten(self.LCARS_GOLD) if flash_white else self.LCARS_GOLD
                    self._update_label(self._lbl_ble, "PIN FAIL", c)
                    ble_status = "PIN FAIL"
                elif ble_fan.is_connected:
                    self._update_label(self._lbl_ble, "ONLINE", self.LCARS_CYAN)
                    ble_status = "ONLINE"
                else:
                    c = theme.lighten(self.LCARS_RED) if flash_white else self.LCARS_RED
                    self._update_label(self._lbl_ble, "OFFLINE", c)
                    ble_status = "OFFLINE"
            else:
                # Not an error state – calm, static dim display
                self._update_label(self._lbl_ble, "DISABLED", self.TEXT_DIM)

            # BLE fan sound effect
            if self._prev_ble_status is not None and ble_status != self._prev_ble_status:
                if ble_status == "ONLINE":
                    self._sound.play("sensor_reconnect")
                elif ble_status in ("OFFLINE", "PIN FAIL"):
                    self._sound.play("sensor_dropout")
            self._prev_ble_status = ble_status

            # ── Sensor status rows (shared helper, per-source sounds) ──
            ds: DatasourceConfig = settings["datasource"]

            # BLE sensors
            power_ble = ds.power_source == DataSource.BLE
            hr_ble = ds.hr_source == DataSource.BLE
            flash_white = (_ft + 1) % 4 < 2      # BLE SEN. phase
            if not power_ble and not hr_ble:
                self._update_label(self._lbl_ble_sens, "– – –", self.TEXT_DIM)
            else:
                ble = getattr(self._ctrl, "_ble_sensor_handler", None)
                if ble is None:
                    self._update_label(self._lbl_ble_sens, "STANDBY", self.LCARS_GOLD)
                else:
                    self._update_sensor_row(
                        self._lbl_ble_sens, ble, power_ble, hr_ble,
                        flash_white, now,
                    )

            # ANT+
            power_ant = ds.power_source == DataSource.ANTPLUS
            hr_ant = ds.hr_source == DataSource.ANTPLUS
            ant = getattr(self._ctrl, "_antplus_handler", None)
            flash_white = (_ft + 2) % 4 < 2      # ANT+ phase
            if (power_ant or hr_ant) and ant is not None:
                alive = self._update_sensor_row(
                    self._lbl_ant, ant, power_ant, hr_ant, flash_white, now,
                )
                # ANT+ sound effect on status transitions
                ant_status = "OK" if alive else "FAIL"
                if self._prev_ant_status is not None and ant_status != self._prev_ant_status:
                    if ant_status == "OK":
                        self._sound.play("sensor_reconnect")
                    else:
                        self._sound.play("sensor_dropout")
                self._prev_ant_status = ant_status
            else:
                self._update_label(self._lbl_ant, "– – –", self.TEXT_DIM)

            # Zwift UDP
            zwift = getattr(self._ctrl, "_zwift_udp", None)
            power_zwift = ds.power_source == DataSource.ZWIFTUDP
            hr_zwift = ds.hr_source == DataSource.ZWIFTUDP
            flash_white = (_ft + 3) % 4 < 2      # ZWIFT phase
            if (power_zwift or hr_zwift) and zwift is not None:
                alive = self._update_sensor_row(
                    self._lbl_zwift_udp, zwift, power_zwift, hr_zwift,
                    flash_white, now,
                )
                # Zwift sound effect – all selected metrics alive is
                # RECEIVING; losing any of them is NO SIGNAL
                zwift_status = "RECEIVING" if alive else "NO SIGNAL"
                if self._prev_zwift_status is not None and zwift_status != self._prev_zwift_status:
                    if zwift_status == "RECEIVING":
                        self._sound.play("zwift_connect")
                    else:
                        self._sound.play("zwift_disconnect")
                self._prev_zwift_status = zwift_status
            else:
                self._update_label(self._lbl_zwift_udp, "– – –", self.TEXT_DIM)

            # Last TX
            if ble_fan is not None and getattr(ble_fan, "last_sent_time", 0) > 0:
                cur_sent_time = ble_fan.last_sent_time
                ago = now - cur_sent_time
                self._update_label(self._lbl_last_sent, f"{ago:.0f}s AGO", self.LCARS_TAN)

                # Fan TX sound – only when a new command went out
                if cur_sent_time != self._prev_last_sent_time and self._prev_last_sent_time > 0:
                    self._sound.play("fan_tx")
                self._prev_last_sent_time = cur_sent_time
            else:
                self._update_label(self._lbl_last_sent, "– – –", self.TEXT_DIM)

            # Cooldown – the snapshot is reused by the tile update below
            cd_active = False
            if cool is not None:
                cd_active, remaining = cool.snapshot()
                if cd_active:
                    self._update_label(
                        self._lbl_cool, f"{remaining:.0f}s", self.LCARS_GOLD
                    )
                else:
                    self._update_label(self._lbl_cool, "INACTIVE", self.TEXT_DIM)
            else:
                self._update_label(self._lbl_cool, "– – –", self.TEXT_DIM)

            # ── Status strip update (active = blinking background) ──
            def _tile_state(active: bool, phase: int) -> str:
                if not active:
                    return "off"
                return "flash" if (_ft + phase) % 4 < 2 else "on"

            zpi = settings["power_zones"].zero_power_immediate
            self._set_tile_state(self._tile_zero_imm, _tile_state(zpi, 0))

            zhi = settings["heart_rate_zones"].zero_hr_immediate
            self._set_tile_state(self._tile_zero_hr_imm, _tile_state(zhi, 1))

            zone_mode_val = settings["heart_rate_zones"].zone_mode
            hw = zone_mode_val == ZoneMode.HIGHER_WINS
            self._set_tile_state(self._tile_higher_wins, _tile_state(hw, 2))

            self._set_tile_state(self._tile_ant,
                                 _tile_state(power_ant or hr_ant, 3))
            self._set_tile_state(self._tile_ble,
                                 _tile_state(power_ble or hr_ble, 0))
            self._set_tile_state(self._tile_cooldown,
                                 _tile_state(cool is not None and cd_active, 1))

            # ── ZwiftApp.exe process watch (about every 10 s) ──
            if settings["hud"].close_at_zwiftapp_exe:
                self._zwift_check_counter += 1
                if self._zwift_check_counter >= self._ZWIFT_CHECK_INTERVAL:
                    self._zwift_check_counter = 0
                    if not self._zwift_check_running:
                        self._zwift_check_running = True
                        threading.Thread(
                            target=self._check_zwift_process,
                            daemon=True,
                            name="ZwiftProcessCheck",
                        ).start()

        except Exception as exc:
            logger.warning("HUD _update hiba: %s", exc)

    # ────────── ZWIFT PROCESS MONITOR ──────────

    def _check_zwift_process(self) -> None:
        """Background-thread check whether ZwiftApp.exe is running."""
        try:
            running = self._ctrl.is_process_running("ZwiftApp.exe")
            should_close = False
            if running:
                if not self._zwift_seen:
                    self._zwift_seen = True
                    logger.info("ZwiftApp.exe észlelve / detected.")
            elif self._zwift_seen:
                # Zwift was running before but is gone now → close the HUD
                logger.info("ZwiftApp.exe kilépett, HUD leállítása...")
                should_close = True
            elif time.time() - self._zwift_grace_start >= self._ZWIFT_GRACE_PERIOD:
                # Grace period expired, Zwift never launched → exit
                logger.info(
                    "ZwiftApp.exe nem indult el %.0f másodperc alatt, kilépés...",
                    self._ZWIFT_GRACE_PERIOD,
                )
                should_close = True
            self._zwift_was_running = running
            if should_close:
                # QTimer.singleShot does NOT work from a background thread
                # (no Qt event loop there). QMetaObject.invokeMethod is
                # thread-safe: it queues close() onto the main event loop.
                QMetaObject.invokeMethod(
                    self, "close", Qt.ConnectionType.QueuedConnection,
                )
        finally:
            self._zwift_check_running = False

    # ────────── SCALING ──────────

    def _apply_scale(self) -> None:
        s = self._scale

        # Batch: individual widget updates must not trigger separate
        # repaints – a resize step causes a single repaint this way
        self.setUpdatesEnabled(False)
        try:
            self._header.set_scale(s)
            self._footer.set_scale(s)
            self._sidebar.set_scale(s)
            self._zone_bar.set_scale(s)
            self._power_meter.set_scale(s)
            self._hr_meter.set_scale(s)
            # Font size and fixed width of the text labels scale as well
            for lbl, base_pt, base_fw, bold in self._scalable_texts:
                self._apply_label_scale(lbl, base_pt, base_fw, bold)
        finally:
            self.setUpdatesEnabled(True)
        # The minimum size is NOT refreshed here: during a live resize a
        # growing minimum would push the window back (jumping); the
        # debounce timer calls _update_min_size once the drag has stopped

    def _register_scalable(self, lbl: "QLabel", base_pt: int,
                           base_fw: int | None = None, bold: bool = True) -> None:
        """Register a label for scaling (base pt size + optional fixed
        width) and apply the current scale immediately."""
        self._scalable_texts.append((lbl, base_pt, base_fw, bold))
        self._apply_label_scale(lbl, base_pt, base_fw, bold)

    def _apply_label_scale(self, lbl: "QLabel", base_pt: int,
                           base_fw: int | None, bold: bool) -> None:
        """Set the full font (family, size, weight) via setFont – the
        stylesheet has no font-* properties, so nothing overrides it. The
        fixed width scales too.

        The rounded point size / width only changes at the larger scale
        steps – when they equal the previous values the setFont /
        setFixedWidth calls are skipped (far fewer relayouts during a
        live resize)."""
        s = self._scale
        pt = max(6, int(base_pt * s))
        fw = None if base_fw is None else max(1, int(base_fw * s))
        key = (pt, fw, bold)
        if getattr(lbl, "_hud_font_key", None) == key:
            return
        lbl._hud_font_key = key
        f = QFont(self._font_family, pt)
        f.setBold(bold)
        lbl.setFont(f)
        if fw is not None:
            lbl.setFixedWidth(fw)

    def _update_min_size(self) -> None:
        """Compute the minimum window size from the natural (readable) size
        of the content, so rows/tiles cannot be squashed by mouse resizing.
        Runs only at rest (debounced), never during a live drag."""
        lay = self.layout()
        if lay is not None:
            lay.activate()
        hint = self.minimumSizeHint()
        self.setMinimumSize(max(self.MIN_W, hint.width()),
                            max(self.MIN_H, hint.height()))

    def cleanup_sound(self) -> None:
        """Public interface for releasing the sound system."""
        self._sound.cleanup()

    # ────────── MONITOR GEOMETRY ──────────

    def _current_screen_name(self) -> str:
        """Name of the window's current screen (or empty when unavailable)."""
        screen = self.screen()
        if screen is not None:
            return screen.name()
        return ""

    def _restore_geometry(self) -> None:
        """Restore the window position/size for the last used monitor.

        When the saved (last used) monitor is no longer connected:
          1. if the primary monitor also has a saved geometry, use that;
          2. otherwise keep the last saved SIZE and center the window on
             the primary monitor (the saved position would point into the
             missing monitor's area, meaningless there).
        Regardless of the monitor choice, the whole window ends up inside
        the screen's visible (available) area – it never starts off-screen.
        """
        hud_cfg: HudConfig = self._ctrl.settings["hud"]
        geo_map = hud_cfg.window_geometry
        if not geo_map:
            return

        # Names of the connected monitors
        available = {s.name(): s for s in self._app.screens()}

        # Try the last used monitor (the last key of the dict)
        last_screen_name = list(geo_map.keys())[-1]
        center_on_screen = False
        if last_screen_name in available:
            rect = geo_map[last_screen_name]
            target_screen = available[last_screen_name]
        else:
            # The saved monitor is not connected → primary monitor
            target_screen = self._app.primaryScreen()
            if target_screen is None:
                return
            pname = target_screen.name()
            if pname in geo_map:
                rect = geo_map[pname]
                logger.info(
                    "A mentett monitor (%s) nincs csatlakoztatva – az "
                    "elsődleges monitor (%s) mentett pozíciójának használata.",
                    last_screen_name, pname,
                )
            else:
                # No saved entry for the primary: keep the saved size,
                # center the position
                rect = geo_map[last_screen_name]
                center_on_screen = True
                logger.info(
                    "A mentett monitor (%s) nincs csatlakoztatva – a HUD a "
                    "mentett méretével az elsődleges monitor (%s) közepére "
                    "kerül.", last_screen_name, pname,
                )

        sg = target_screen.availableGeometry()
        # Clamp to the absolute floor (MIN_W/MIN_H), NOT the current
        # minimum: at startup the minimum is still computed for the 1.0
        # scale content, which would round a saved small size upwards
        w = max(self.MIN_W, min(rect["w"], sg.width()))
        h = max(self.MIN_H, min(rect["h"], sg.height()))
        if center_on_screen:
            x = sg.x() + (sg.width() - w) // 2
            y = sg.y() + (sg.height() - h) // 2
        else:
            # The WHOLE window goes inside the monitor's visible area
            x = max(sg.x(), min(rect["x"], sg.x() + sg.width() - w))
            y = max(sg.y(), min(rect["y"], sg.y() + sg.height() - h))
        # Qt would clamp setGeometry to the effective minimum – lower it to
        # the floor first; the correct (saved-scale) content minimum is
        # recomputed by the debounce timer started from resizeEvent
        self.setMinimumSize(self.MIN_W, self.MIN_H)
        # The scale is applied by resizeEvent as a result of setGeometry
        self.setGeometry(x, y, w, h)

    def _store_geometry_in_cfg(self) -> "HudConfig | None":
        """Write the current geometry into the hud config (no file write).

        The key is removed before re-insertion so it moves to the END of
        the dict: restore treats the last key as the last used monitor,
        and a plain update of an existing key would not move it back
        (Python dicts keep insertion order, not update order)."""
        screen_name = self._current_screen_name()
        if not screen_name:
            return None
        geo = self.geometry()
        rect = {"x": geo.x(), "y": geo.y(), "w": geo.width(), "h": geo.height()}
        hud_cfg: HudConfig = self._ctrl.settings["hud"]
        hud_cfg.window_geometry.pop(screen_name, None)
        hud_cfg.window_geometry[screen_name] = rect
        return hud_cfg

    def _save_geometry(self) -> None:
        """Persist the window position/size for the current monitor."""
        hud_cfg = self._store_geometry_in_cfg()
        if hud_cfg is not None:
            self._save_hud_setting("window_geometry", hud_cfg.window_geometry)

    def _auto_save_geometry(self) -> None:
        """Debounced automatic geometry save after a move/resize.

        Quiet (no log line per move); save_hud_settings_only checks the
        save_hud_settings flag itself. This way the position survives an
        unexpected shutdown (crash / power loss) as well."""
        if getattr(self, "_closing", False):
            return
        hud_cfg = self._store_geometry_in_cfg()
        if hud_cfg is not None:
            save_hud_settings_only(self._ctrl.settings_file, hud_cfg)

    # ────────── RUN / CLOSE ──────────

    def run(self) -> None:
        self._restore_geometry()
        self.show()
        self._sound.play("hud_startup")
        self._app.exec()

    def closeEvent(self, event: Any) -> None:
        if getattr(self, "_close_done", False):
            # Third call: the sound has played, close for real
            self._sound.cleanup()
            super().closeEvent(event)
            self._app.quit()
            return
        if getattr(self, "_closing", False):
            # Second call (e.g. from a finally block): still waiting for
            # the sound, ignore
            event.ignore()
            return
        self._closing = True
        self._save_geometry()
        event.ignore()
        self._timer.stop()
        self._sound.play("hud_shutdown")
        # Wait for the shutdown sound to finish, then close for real
        duration_ms = self._sound.sound_duration_ms("hud_shutdown")

        def _finish_close() -> None:
            self._close_done = True
            self.close()

        QTimer.singleShot(duration_ms + 100, _finish_close)

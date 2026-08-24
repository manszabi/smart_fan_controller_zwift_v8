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
import math
import os
import platform as _platform
import sys
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QTimer, QPoint, QSize, QRectF, QMetaObject
from PySide6.QtGui import (
    QFont, QFontDatabase, QMouseEvent, QPainter, QPainterPath, QPalette,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QHBoxLayout, QVBoxLayout, QLayout,
    QSlider, QMenu, QFrame, QSizePolicy, QSpacerItem,
)

from smart_fan_controller.config import (
    DataSource, ZoneMode, get_effective_zone_mode,
)
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

# Qt's "no maximum" sentinel (QWIDGETSIZE_MAX is not exported by PySide6)
QWIDGETSIZE_MAX = (1 << 24) - 1


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

    # Absolute floor for the window size – the effective minimum is the
    # measured readable minimum (see _calibrate_sizing), never less than this
    MIN_W = 220
    MIN_H = 300

    # The scale runs on a fixed ladder (…, 0.95, 1.00, 1.05, …) instead of
    # continuously. Font point sizes and rounded paddings are integers, so
    # the content grows in jumps anyway – on a fixed ladder those jumps
    # land on scales that _calibrate_sizing has actually measured, so no
    # in-between size can clip. A 5% step is visually seamless and it also
    # spares a relayout on most resize events.
    SCALE_STEP = 0.05
    SCALE_MAX = 3.0
    MIN_SCALE_FLOOR = 0.5

    # Widest text every value label can ever show. The readable minimum is
    # measured with these in place, so a later value change (a longer
    # string) can never end up clipped at the minimum window size.
    WIDEST_TEXTS: dict[str, str] = {
        "_lbl_zone": "STANDBY",
        "_lbl_power": "8888 W",
        "_lbl_hr": "888 BPM",
        "_lbl_ble": "DISABLED",
        "_lbl_ble_sens": "P:FAIL  HR:FAIL",
        "_lbl_ant": "P:FAIL  HR:FAIL",
        "_lbl_zwift_udp": "P:FAIL  HR:FAIL",
        "_lbl_last_sent": "8888s AGO",
        "_lbl_cool": "INACTIVE",
        "_alpha_value": "100%",
    }

    def __init__(self, controller: "FanController", app: "QApplication") -> None:
        super().__init__()
        self._base_width = 340
        self._base_height = 460
        self._scale = 1.0
        self._min_scale = 1.0
        self._ctrl = controller
        self._app = app
        self._drag_pos: QPoint | None = None
        self._resize_active = False
        self._resize_start_pos = QPoint()
        self._resize_start_size = QSize()

        # Scalable text labels: (label, base pt size, fixed width or None, bold)
        self._scalable_texts: list[tuple[QLabel, int, int | None, bool]] = []
        # Scalable box metrics – everything that occupies pixels has to
        # follow the scale, otherwise the constant padding/margin overhead
        # squeezes the text out of the rows on a shrinking window
        self._scalable_styles: list[tuple[QWidget, Callable[[], str]]] = []
        self._scalable_layouts: list[tuple[QLayout, tuple[int, int, int, int], int]] = []
        self._scalable_spacers: list[tuple[QSpacerItem, int]] = []
        self._scalable_heights: list[tuple[QWidget, int]] = []

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
        # Only the first _update failure is logged with a traceback
        self._update_error_seen = False

        # ───────── ZWIFT PROCESS MONITOR ─────────
        # The watch only works where the process list is actually
        # readable. FanController.is_process_running() is Windows-only and
        # answers False everywhere else – taken at face value that meant
        # "Zwift is not running", so on Linux/macOS the grace period
        # expired and the HUD shut the whole application down after five
        # minutes. Off-Windows the feature is simply disabled.
        self._zwift_watch_supported = _platform.system() == "Windows"
        self._zwift_seen = False           # True once we saw it running
        self._zwift_check_counter = 0
        self._ZWIFT_CHECK_INTERVAL = 20    # every 20th _update call ≈ 10 s
        self._zwift_check_running = False  # race-condition guard
        self._zwift_grace_start: float = time.monotonic()
        self._ZWIFT_GRACE_PERIOD: float = 300.0  # wait 5 minutes for launch
        if not self._zwift_watch_supported and hud_cfg.close_at_zwiftapp_exe:
            logger.info(
                "ZwiftApp.exe figyelés kihagyva: a folyamatlista csak "
                "Windows-on olvasható (hud.close_at_zwiftapp_exe hatástalan)."
            )

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
        self._register_layout(main_layout, (0, 0, 0, 0), 0)

        # Header
        self._header = LCARSHeaderWidget(self, self._font_family, self._scale)
        main_layout.addWidget(self._header)

        # Body (sidebar + content)
        body = QWidget(self)
        body.setStyleSheet("background-color: transparent;")
        body_layout = QHBoxLayout(body)
        self._register_layout(body_layout, (0, 0, 0, 0), 0)

        self._sidebar = LCARSSidebarWidget(body, self._scale)
        body_layout.addWidget(self._sidebar)

        # Content panel
        content = QWidget(body)
        content.setStyleSheet(f"background-color: {self.PANEL_BG};")
        content_layout = QVBoxLayout(content)
        self._register_layout(content_layout, (6, 8, 6, 0), 0)
        body_layout.addWidget(content, 1)

        # ───────── ZONE DISPLAY ─────────
        self._lbl_zone_label = QLabel("FAN ZONE")
        self._register_styled(self._lbl_zone_label, lambda: (
            f"background-color: {self.LCARS_CYAN}; color: #000a14; "
            f"padding: {self._px(2)}px {self._px(4)}px; "
            f"border-radius: {self._px(4)}px;"
        ))
        self._lbl_zone_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._register_scalable(self._lbl_zone_label, 12)
        content_layout.addWidget(self._lbl_zone_label)

        self._lbl_zone = QLabel("– – –")
        self._lbl_zone.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Text color comes from QPalette (dynamic); the stylesheet only
        # carries the static part
        self._set_label_color(self._lbl_zone, self.LCARS_CYAN)
        self._register_styled(self._lbl_zone, lambda: (
            f"background-color: {self._VAL_BG}; "
            f"padding: {self._px(3)}px {self._px(6)}px; "
            f"border-radius: {self._px(4)}px;"
        ))
        self._lbl_zone.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._register_scalable(self._lbl_zone, 19)
        content_layout.addWidget(self._lbl_zone)

        # Zone segment bar below the zone display
        self._add_spacing(content_layout, 3)
        self._zone_bar = LCARSZoneBarWidget(content, self._scale)
        content_layout.addWidget(self._zone_bar)
        self._add_spacing(content_layout, 3)

        # ───────── STATUS STRIP (tiles) ─────────
        tile_frame = QWidget(content)
        tile_frame.setStyleSheet(f"background-color: {self.PANEL_BG};")
        tile_layout = QHBoxLayout(tile_frame)
        self._register_layout(tile_layout, (0, 0, 0, 4), 2)

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
        self._make_separator(content, content_layout)

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
        self._make_separator(content, content_layout)

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
        self._register_styled(self._alpha_slider, self._slider_style)
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
        self._footer_brand.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._register_styled(self._footer_brand, lambda: (
            f"color: {self.LCARS_CYAN_DIM}; background-color: {self.PANEL_BG}; "
            f"padding: {self._px(6)}px 0 {self._px(4)}px 0;"
        ))
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

        # Calibrate the scale mapping to the real content and derive the
        # readable minimum window size from it
        self._calibrate_sizing()

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

        The fonts ship inside the package and QFontDatabase loads TTFs on
        every platform, so the loading is NOT Windows-gated: skipping it
        elsewhere left the bundled LCARS face unused on Linux/macOS and
        the HUD fell back to a generic system font for no reason.
        """
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
            "Eras Medium ITC", "Bahnschrift", "Trebuchet MS", "Segoe UI",
            # Cross-platform fallbacks before the Windows-only last resort
            "DejaVu Sans Condensed", "Liberation Sans Narrow", "Consolas",
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
        self._register_layout(row_layout, (0, 2, 0, 2), 2)

        key_lbl = QLabel(label)
        self._register_styled(key_lbl, lambda: (
            f"background-color: {label_bg}; color: #000a14; "
            f"padding: {self._px(3)}px {self._px(4)}px; "
            f"border-radius: {self._px(4)}px;"
        ))
        row_layout.addWidget(key_lbl)
        self._register_scalable(key_lbl, 9, 100)

        val_lbl = QLabel(value)
        val_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        # Text color comes from QPalette (dynamic); stylesheet is static only
        self._set_label_color(val_lbl, color)
        self._register_styled(val_lbl, lambda: (
            f"background-color: {self._VAL_BG}; "
            f"padding: {self._px(3)}px {self._px(6)}px; "
            f"border-radius: {self._px(4)}px;"
        ))
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
        self._register_styled(lbl, lambda: (
            f'QLabel {{ background-color: transparent; color: {self.TEXT_DIM}; '
            f'border: 1px solid {self.BORDER_GLOW}; '
            f'padding: {self._px(1)}px {self._px(4)}px; '
            f'border-radius: {self._px(4)}px; }}'
            f'QLabel[hudState="on"] {{ background-color: {accent}; '
            f'color: #000a14; border-color: {accent}; }}'
            f'QLabel[hudState="flash"] {{ '
            f'background-color: {theme.lighten(accent)}; '
            f'color: #000a14; border-color: {theme.lighten(accent)}; }}'
        ))
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
        self._register_layout(row_layout, (0, 2, 0, 2), 2)

        key_lbl = QLabel(label)
        self._register_styled(key_lbl, lambda: (
            f"background-color: {label_bg}; color: #000a14; "
            f"padding: {self._px(2)}px {self._px(4)}px; "
            f"border-radius: {self._px(4)}px;"
        ))
        row_layout.addWidget(key_lbl)
        self._register_scalable(key_lbl, 9, 100)

        val_lbl = QLabel(value)
        val_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        # Text color comes from QPalette (dynamic); stylesheet is static only
        self._set_label_color(val_lbl, self.TEXT_DIM)
        self._register_styled(val_lbl, lambda: (
            f"background-color: {self._VAL_BG}; "
            f"padding: {self._px(2)}px {self._px(6)}px; "
            f"border-radius: {self._px(4)}px;"
        ))
        row_layout.addWidget(val_lbl, 1)
        self._register_scalable(val_lbl, 11, bold=False)

        layout.addWidget(row)
        return val_lbl

    def _slider_style(self) -> str:
        """Opacity slider stylesheet at the current scale.

        The handle's width, height and radius all come from the same
        number: rounding them one by one could leave the radius larger
        than half the box, and Qt then drops the rounding altogether –
        the round knob would turn into a square at some scales."""
        groove = self._px(8)
        overhang = self._px(3)              # handle sticking out of the groove
        knob = groove + 2 * overhang        # handle box: square → round
        return (
            f"QSlider::groove:horizontal {{"
            f"  background: #002244; height: {groove}px;"
            f"  border-radius: {groove // 2}px;"
            f"}}"
            f"QSlider::sub-page:horizontal {{"
            f"  background: {self.LCARS_CYAN}; border-radius: {groove // 2}px;"
            f"}}"
            f"QSlider::handle:horizontal {{"
            f"  background: #EAF6FF; width: {knob}px;"
            f"  margin: -{overhang}px 0;"
            f"  border-radius: {knob // 2}px;"
            f"}}"
        )

    def _make_separator(self, parent: "QWidget", layout: "QVBoxLayout") -> "QFrame":
        """Thin LCARS divider line whose height follows the scale."""
        sep = QFrame(parent)
        sep.setStyleSheet(f"background-color: {self.BORDER_GLOW}; margin: 6px 10px;")
        self._register_height(sep, 2)
        layout.addWidget(sep)
        return sep

    # ────────── SCALABLE BOX METRICS ──────────

    def _px(self, base: int, floor: int = 1) -> int:
        """A base pixel value at the current scale (never below ``floor``)."""
        return max(floor, round(base * self._scale))

    def _register_styled(self, w: "QWidget", builder: "Callable[[], str]") -> None:
        """Register a widget whose stylesheet contains scale-dependent pixel
        values (padding, radius, …) and apply it right away."""
        self._scalable_styles.append((w, builder))
        self._apply_styled(w, builder)

    def _apply_styled(self, w: "QWidget", builder: "Callable[[], str]") -> None:
        """Re-generate the stylesheet – only assigned when the rounded pixel
        values actually changed (setStyleSheet forces a repolish)."""
        css = builder()
        if getattr(w, "_hud_css", None) == css:
            return
        w._hud_css = css
        w.setStyleSheet(css)
        # A repolish can drop the palette-driven text color – put it back
        color = getattr(w, "_hud_color", None)
        if color is not None:
            self._set_label_color(w, color)

    def _register_layout(self, lay: "QLayout",
                         margins: tuple[int, int, int, int], spacing: int) -> None:
        """Register a layout whose margins/spacing follow the scale.

        SetNoConstraint is essential here: with Qt's default
        (SetDefaultConstraint) the layout writes its own minimum into the
        panel's *explicit* minimumSize, and an explicit minimum is only
        ever raised, never lowered. Every panel would stay pinned to its
        largest-ever (biggest scale) minimum, so a shrinking window could
        not shrink the rows any more – it just squeezed the text out of
        them. Without the constraint the panels report their live layout
        minimum instead, which follows the scale in both directions."""
        lay.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._scalable_layouts.append((lay, margins, spacing))
        self._apply_layout_scale(lay, margins, spacing)

    def _apply_layout_scale(self, lay: "QLayout",
                            margins: tuple[int, int, int, int],
                            spacing: int) -> None:
        m = tuple(self._px(v, 0) for v in margins)
        sp = self._px(spacing, 0)
        if getattr(lay, "_hud_box_key", None) == (m, sp):
            return
        lay._hud_box_key = (m, sp)
        lay.setContentsMargins(*m)
        lay.setSpacing(sp)

    def _add_spacing(self, lay: "QVBoxLayout", base: int) -> None:
        """Fixed vertical gap that shrinks/grows with the scale."""
        item = QSpacerItem(0, base, QSizePolicy.Policy.Minimum,
                           QSizePolicy.Policy.Fixed)
        self._scalable_spacers.append((item, base))
        lay.addItem(item)
        self._apply_spacer_scale(item, base)

    def _apply_spacer_scale(self, item: "QSpacerItem", base: int) -> None:
        item.changeSize(0, self._px(base, 0), QSizePolicy.Policy.Minimum,
                        QSizePolicy.Policy.Fixed)

    def _register_height(self, w: "QWidget", base: int) -> None:
        """Register a widget with a scale-dependent fixed height."""
        self._scalable_heights.append((w, base))
        w.setFixedHeight(self._px(base))

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
                # The minimum size is constant (the measured readable
                # minimum), so the drag needs no temporary relaxation: the
                # window simply stops shrinking at the readable limit
                # instead of squeezing the text out of the rows
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
        new_scale = self._quantize_scale(
            min(self.width() / self._base_width,
                self.height() / self._base_height)
        )
        if abs(new_scale - self._scale) >= 0.001:
            self._set_scale(new_scale)
        self._geo_save_timer.start()

    def _quantize_scale(self, raw: float) -> float:
        """Snap a raw size ratio down onto the calibrated scale ladder.

        Rounding DOWN matters: the ladder step is the size the content was
        measured at, and the window is at least that big – so the content
        is guaranteed to fit, with the odd leftover pixel going into the
        bottom spacer instead of into a clipped row.
        """
        # round() before floor(): 1.5 / 0.05 is 29.999999999999996 in
        # binary floating point, which would drop a whole ladder step
        steps = math.floor(round(raw / self.SCALE_STEP, 6))
        return min(self.SCALE_MAX,
                   max(self._min_scale, round(steps * self.SCALE_STEP, 2)))

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

            # The tiles report what the controller ACTUALLY does, so they
            # use the effective zone mode – with heart_rate_zones.enabled
            # false the controller runs power_only whatever zone_mode says,
            # and the raw value lit up HIGHER WINS (the default) on a
            # power-only setup. The immediate-stop tiles follow the same
            # gating as zone_controller_task: a flag whose metric does not
            # decide the zone in the active mode has no effect.
            eff_mode = get_effective_zone_mode(settings)
            zpi = settings["power_zones"].zero_power_immediate and (
                eff_mode in (ZoneMode.POWER_ONLY, ZoneMode.HIGHER_WINS)
            )
            self._set_tile_state(self._tile_zero_imm, _tile_state(zpi, 0))

            zhi = settings["heart_rate_zones"].zero_hr_immediate and (
                eff_mode in (ZoneMode.HR_ONLY, ZoneMode.HIGHER_WINS)
            )
            self._set_tile_state(self._tile_zero_hr_imm, _tile_state(zhi, 1))

            hw = eff_mode == ZoneMode.HIGHER_WINS
            self._set_tile_state(self._tile_higher_wins, _tile_state(hw, 2))

            self._set_tile_state(self._tile_ant,
                                 _tile_state(power_ant or hr_ant, 3))
            self._set_tile_state(self._tile_ble,
                                 _tile_state(power_ble or hr_ble, 0))
            self._set_tile_state(self._tile_cooldown,
                                 _tile_state(cool is not None and cd_active, 1))

            # ── ZwiftApp.exe process watch (about every 10 s) ──
            if settings["hud"].close_at_zwiftapp_exe and self._zwift_watch_supported:
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
            # The timer fires twice a second, so an unhandled error would
            # repeat forever. The first occurrence carries the full
            # traceback (without it the cause was undiagnosable); the rest
            # are one-liners so a persistent fault cannot rotate the log
            # file out from under the useful entries.
            first = not self._update_error_seen
            self._update_error_seen = True
            logger.warning("HUD _update hiba: %s", exc, exc_info=first)

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
            elif time.monotonic() - self._zwift_grace_start >= self._ZWIFT_GRACE_PERIOD:
                # Grace period expired, Zwift never launched → exit
                logger.info(
                    "ZwiftApp.exe nem indult el %.0f másodperc alatt, kilépés...",
                    self._ZWIFT_GRACE_PERIOD,
                )
                should_close = True
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

    def _set_scale(self, s: float) -> None:
        """Set the scale and push it through the whole UI."""
        self._scale = s
        self._apply_scale()

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
            # …and so does every box metric (padding, margin, gap, divider):
            # a constant pixel overhead would eat the text out of the rows
            # as the window shrinks
            for w, builder in self._scalable_styles:
                self._apply_styled(w, builder)
            for lay, margins, spacing in self._scalable_layouts:
                self._apply_layout_scale(lay, margins, spacing)
            for item, base in self._scalable_spacers:
                self._apply_spacer_scale(item, base)
            for w, base in self._scalable_heights:
                w.setFixedHeight(self._px(base))
        finally:
            self.setUpdatesEnabled(True)
        # The minimum size is NOT touched here: it is a constant measured
        # once by _calibrate_sizing, so a resize can never feed back into
        # the minimum (that loop used to make the window ratchet upwards)

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
        live resize).

        A fixed width is never allowed below the label's own natural
        width: the base widths are calibrated for the LCARS font, and a
        wider fallback font would otherwise get its caption cut off. The
        natural width is measured on the widest text the label can ever
        show (``_hud_widest``), not on the current one – otherwise the
        box would be sized for "92%" and clip "100%"."""
        s = self._scale
        pt = max(6, int(base_pt * s))
        key = (pt, base_fw, bold)
        if getattr(lbl, "_hud_font_key", None) == key:
            return
        lbl._hud_font_key = key
        f = QFont(self._font_family, pt)
        f.setBold(bold)
        lbl.setFont(f)
        if base_fw is not None:
            widest = getattr(lbl, "_hud_widest", None)
            current = lbl.text()
            if widest is not None and widest != current:
                lbl.setText(widest)
            # setFixedWidth pins min = max; clear it so the natural
            # sizeHint is not just the previously pinned width
            lbl.setMinimumWidth(0)
            lbl.setMaximumWidth(QWIDGETSIZE_MAX)
            natural = lbl.sizeHint().width()
            if widest is not None and widest != current:
                lbl.setText(current)
            lbl.setFixedWidth(max(1, int(base_fw * s), natural))

    # ────────── SIZING CALIBRATION ──────────

    def _content_hint(self, s: float) -> "QSize":
        """The window's natural (nothing-clipped) size at the given scale.

        Qt refreshes the cached layout minimums from posted LayoutRequest
        events, i.e. only once the event loop runs – and on a not yet
        shown window it does not propagate them upwards at all. The
        measurement is synchronous, so the whole tree is invalidated by
        hand first: updateGeometry() drops the per-widget size caches and
        invalidate() the layout ones. Without it the nested layouts would
        answer with the minimum belonging to the previous scale.

        The child lists are collected once: _calibrate_sizing walks the
        whole scale ladder and calls this ~90 times, and findChildren
        walks the entire object tree on each call. Nothing creates or
        destroys widgets between those calls (_apply_scale only resizes
        existing ones), so two traversals replace ~180.
        """
        self._set_scale(s)
        widgets, layouts = self._layout_tree()
        for w in widgets:
            w.updateGeometry()
        self.updateGeometry()
        for lay in layouts:
            lay.invalidate()
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()
        return self.minimumSizeHint()

    def _layout_tree(self) -> "tuple[list[QWidget], list[QLayout]]":
        """The window's child widgets and layouts, collected once.

        Cached for the lifetime of the window: the tree is fully built
        before the first call (_calibrate_sizing runs at the end of
        __init__) and nothing adds or removes widgets afterwards.
        """
        cached = getattr(self, "_layout_tree_cache", None)
        if cached is None:
            cached = (self.findChildren(QWidget), self.findChildren(QLayout))
            self._layout_tree_cache = cached
        return cached

    def _fits(self, s: float) -> bool:
        """True when the content fits a ``base × s`` sized window.

        That is exactly the window the scale ``s`` belongs to: the resize
        handler derives the scale from ``min(w / base_w, h / base_h)``, so
        a window at scale ``s`` is at least ``base_w × s`` wide and
        ``base_h × s`` tall.

        The fixed-height LCARS bars are checked separately: their inner
        metrics (bar thickness, elbow radius) have readability floors of
        their own, so below a certain scale the footer would squeeze the
        opacity slider even though the window as a whole still fits.
        """
        hint = self._content_hint(s)
        if (hint.width() > round(self._base_width * s)
                or hint.height() > round(self._base_height * s)):
            return False
        for bar in (self._header, self._footer):
            need = bar.minimumSizeHint()
            if need.height() > 0 and bar.height() < need.height():
                return False
        return True

    def _calibrate_sizing(self) -> None:
        """Calibrate the scale mapping to the real content, then measure the
        smallest window size where no text gets clipped.

        Two steps, both done once (fonts and label texts are known by now):

        1. **Base size.** ``_base_*`` is the window size that scale 1.0
           belongs to, so the content's natural size must fit into it.
           The design values (340×460) hold for the LCARS font, but a
           fallback font can be taller/wider – the base is therefore
           grown until every ladder step from 1.0 up to SCALE_MAX fits.
           From then on the content needs at most ``base × s`` at every
           scale, i.e. the layout shrinks exactly as fast as the window.

        2. **Minimum size.** Below a certain scale the readability floors
           (6 pt font, 30 px header, 1 px padding…) stop following the
           scale, and from there the content would be clipped. The
           smallest still-fitting ladder step is searched downwards from
           1.0, and the minimum window size is ``base × that scale``.

        The measurement uses the widest text every value label can ever
        show (WIDEST_TEXTS), so a later value change cannot clip either.
        The resulting minimum depends only on the content – never on the
        current window size – hence resizing can never feed back into it.
        """
        saved_scale = self._scale
        saved_texts = {name: lbl.text()
                       for name, lbl in self._widest_text_labels()}
        for name, lbl in self._widest_text_labels():
            lbl._hud_widest = self.WIDEST_TEXTS[name]
        self.setUpdatesEnabled(False)
        try:
            for name, lbl in self._widest_text_labels():
                lbl.setText(self.WIDEST_TEXTS[name])

            # 1. base size – the largest demand over the ladder from 1.0 up
            bw, bh = self._base_width, self._base_height
            s = 1.0
            while s <= self.SCALE_MAX + 1e-9:
                hint = self._content_hint(s)
                bw = max(bw, math.ceil(hint.width() / s))
                bh = max(bh, math.ceil(hint.height() / s))
                s = round(s + self.SCALE_STEP, 2)
            self._base_width, self._base_height = bw, bh

            # 2. lowest ladder step that still fits
            s = 1.0
            while (round(s - self.SCALE_STEP, 2) >= self.MIN_SCALE_FLOOR
                   and self._fits(round(s - self.SCALE_STEP, 2))):
                s = round(s - self.SCALE_STEP, 2)
            self._min_scale = s
        finally:
            for name, lbl in self._widest_text_labels():
                lbl.setText(saved_texts[name])
            self._set_scale(saved_scale)
            self.setUpdatesEnabled(True)

        self.setMinimumSize(
            max(self.MIN_W, round(self._base_width * self._min_scale)),
            max(self.MIN_H, round(self._base_height * self._min_scale)),
        )
        logger.debug(
            "HUD méretezés kalibrálva: alap=%dx%d, min skála=%.2f, "
            "min méret=%dx%d", self._base_width, self._base_height,
            self._min_scale, self.minimumWidth(), self.minimumHeight(),
        )

    def _widest_text_labels(self) -> "list[tuple[str, QLabel]]":
        """The value labels measured with their widest possible text."""
        return [(name, getattr(self, name)) for name in self.WIDEST_TEXTS]

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
        # Clamp to the measured readable minimum: a geometry saved by an
        # older version (or on another font) may be smaller than what the
        # content needs – restoring it as-is would clip the texts
        w = max(self.minimumWidth(), min(rect["w"], sg.width()))
        h = max(self.minimumHeight(), min(rect["h"], sg.height()))
        if center_on_screen:
            x = sg.x() + (sg.width() - w) // 2
            y = sg.y() + (sg.height() - h) // 2
        else:
            # The WHOLE window goes inside the monitor's visible area
            x = max(sg.x(), min(rect["x"], sg.x() + sg.width() - w))
            y = max(sg.y(), min(rect["y"], sg.y() + sg.height() - h))
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

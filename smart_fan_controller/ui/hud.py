#!/usr/bin/env python3
# pyright: reportInvalidTypeForm=false
"""LCARS GUI components – Star Trek HUD interface for Smart Fan Controller.

This module contains the visual components for the LCARS-style HUD:
  - LCARSHeaderWidget: Top header with title and version badge
  - LCARSFooterWidget: Bottom footer with LCARS styling
  - LCARSSidebarWidget: Colored sidebar segments
  - LCARSSoundManager: Sound effect playback (LCARS beeps/tones)
  - HUDWindow: Main floating HUD window with telemetry display
"""

from __future__ import annotations

import atexit
import logging
import math
import os
import platform as _platform
import shutil
import sys
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer, QPoint, QSize, QRectF, QUrl
from PySide6.QtGui import (
    QColor, QPainter, QBrush, QFont, QFontDatabase,
    QPainterPath, QMouseEvent,
)
from PySide6.QtMultimedia import QSoundEffect
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QHBoxLayout, QVBoxLayout,
    QSlider, QMenu, QFrame,
)

from smart_fan_controller import __version__
from smart_fan_controller.config import DataSource, ZoneMode
from smart_fan_controller.config.loader import HudConfig, DatasourceConfig
from smart_fan_controller.core.helpers import generate_tone

if TYPE_CHECKING:
    # A FanController a smart_fan_controller.controller modulban él. A körkörös
    # import elkerülésére a típust itt Any-ként kezeljük; a controller egy
    # lazán kezelt, csak továbbadott objektum.
    FanController = Any

logger = logging.getLogger("swift_fan_controller_new")


class LCARSHeaderWidget(QWidget):
    """LCARS fejléc widget – QPainter-rel rajzolt felső sáv."""

    def __init__(self, parent: QWidget, font_family: str, scale: float = 1.0) -> None:  # type: ignore[reportInvalidTypeForm]
        super().__init__(parent)
        self._font_family = font_family
        self._scale = scale
        self.setFixedHeight(50)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"background-color: {HUDWindow.BG};")

    def set_scale(self, s: float) -> None:
        self._scale = s
        h = max(30, int(50 * s))
        self.setFixedHeight(h)
        self.update()

    def paintEvent(self, event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        s = self._scale
        ch = self.height()
        bar_h = max(8, int(14 * s))
        sw = max(10, int(16 * s))
        R = max(14, int(26 * s))
        corner_r = max(12, int(18 * s))

        # Fő narancssárga sáv ívvel + lekerekített bal felső sarok
        path = QPainterPath()
        path.moveTo(corner_r, 0)
        path.lineTo(w - 6, 0)
        path.lineTo(w - 6, bar_h)
        for i in range(21):
            angle = math.radians(90 + (180 - 90) * i / 20)
            px = sw + R + R * math.cos(angle)
            py = bar_h + R - R * math.sin(angle)
            path.lineTo(px, py)
        path.lineTo(sw, ch)
        path.lineTo(0, ch)
        path.lineTo(0, corner_r)
        path.arcTo(QRectF(0, 0, 2 * corner_r, 2 * corner_r), 180, -90)
        path.closeSubpath()
        p.fillPath(path, QBrush(QColor(HUDWindow.LCARS_ORANGE)))

        # Bal felső sarok háttér kitöltés (ív mögött)
        bg_path = QPainterPath()
        bg_path.addRect(QRectF(0, 0, corner_r, corner_r))
        bg_path -= path
        p.fillPath(bg_path, QBrush(QColor(HUDWindow.BG)))

        # Cím szöveg
        title_size = max(8, int(12 * s))
        p.setFont(QFont(self._font_family, title_size, QFont.Weight.Bold))
        p.setPen(QColor(HUDWindow.LCARS_CYAN))
        p.drawText(QRectF(sw + R, bar_h, w - 6 - sw - R, ch - bar_h),
                    Qt.AlignmentFlag.AlignCenter, "ZWIFT FAN CTRL")

        # Badge (magenta téglalap + verzió)
        badge_w = max(40, int(62 * s))
        p.fillRect(int(w - badge_w - 8), 1, badge_w, bar_h - 3,
                    QColor(HUDWindow.LCARS_MAGENTA))
        ver_size = max(6, int(7 * s))
        p.setFont(QFont(self._font_family, ver_size))
        p.setPen(QColor("#FFFFFF"))
        p.drawText(int(w - badge_w - 8), 1, badge_w, bar_h - 3,
                    Qt.AlignmentFlag.AlignCenter, f"v{__version__}")

        p.end()


class LCARSFooterWidget(QWidget):
    """LCARS lábléc widget – QPainter-rel rajzolt alsó sáv."""

    def __init__(self, parent: QWidget, font_family: str, scale: float = 1.0) -> None:  # type: ignore[reportInvalidTypeForm]
        super().__init__(parent)
        self._font_family = font_family
        self._scale = scale
        self.setFixedHeight(50)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"background-color: {HUDWindow.BG};")

    def set_scale(self, s: float) -> None:
        self._scale = s
        h = max(30, int(50 * s))
        self.setFixedHeight(h)
        self.update()

    def paintEvent(self, event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        s = self._scale
        fh = self.height()
        bar_h = max(8, int(14 * s))
        sw = max(10, int(16 * s))
        R = max(14, int(26 * s))
        bar_top = fh - bar_h
        corner_r = max(12, int(18 * s))

        # Fő kék sáv ívvel + lekerekített bal alsó sarok
        path = QPainterPath()
        path.moveTo(0, 0)
        path.lineTo(sw, 0)
        for i in range(21):
            angle = math.radians(180 + (270 - 180) * i / 20)
            px = sw + R + R * math.cos(angle)
            py = bar_top - R - R * math.sin(angle)
            path.lineTo(px, py)
        path.lineTo(w - 6, bar_top)
        path.lineTo(w - 6, fh)
        path.lineTo(corner_r, fh)
        path.arcTo(QRectF(0, fh - 2 * corner_r, 2 * corner_r, 2 * corner_r), 270, -90)
        path.lineTo(0, 0)
        path.closeSubpath()
        p.fillPath(path, QBrush(QColor(HUDWindow.LCARS_BLUE)))

        # Bal alsó sarok háttér kitöltés (ív mögött)
        bg_path = QPainterPath()
        bg_path.addRect(QRectF(0, fh - corner_r, corner_r, corner_r))
        bg_path -= path
        p.fillPath(bg_path, QBrush(QColor(HUDWindow.BG)))

        # Szegmensek
        seg_x = sw + R + 8
        seg_w = max(1, (w - 6 - int(seg_x)) // 3)
        p.fillRect(int(seg_x + seg_w + 4), bar_top, seg_w - 4, bar_h,
                    QColor(HUDWindow.LCARS_PURPLE))
        p.fillRect(int(seg_x + 2 * seg_w + 4), bar_top,
                    w - 6 - int(seg_x + 2 * seg_w + 4), bar_h,
                    QColor(HUDWindow.LCARS_TAN))

        # Footer szöveg
        footer_text_size = max(7, int(9 * s))
        p.setFont(QFont(self._font_family, footer_text_size))
        p.setPen(QColor(HUDWindow.LCARS_CYAN_DIM))
        p.drawText(int(sw + R), 0, int(w - 6 - sw - R), bar_top,
                    Qt.AlignmentFlag.AlignCenter, "STARFLEET CYCLING DIV")

        p.end()


class LCARSSidebarWidget(QWidget):
    """LCARS bal oldalsáv – színes szegmensek."""

    COLORS = ["#FF9900", "#FFCC66", "#5599FF", "#CC6699", "#9977CC", "#FFAA66"]

    def __init__(self, parent: QWidget, scale: float = 1.0) -> None:  # type: ignore[reportInvalidTypeForm]
        super().__init__(parent)
        self._scale = scale
        self.setFixedWidth(max(10, int(16 * scale)))
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"background-color: {HUDWindow.BG};")

    def set_scale(self, s: float) -> None:
        self._scale = s
        self.setFixedWidth(max(10, int(16 * s)))
        self.update()

    def paintEvent(self, event: Any) -> None:
        p = QPainter(self)
        sw = self.width()
        h = self.height()
        if h < 10:
            p.end()
            return
        n = len(self.COLORS)
        seg_h = max(10, h // n)
        gap = max(1, int(2 * self._scale))
        for i, c in enumerate(self.COLORS):
            y = i * seg_h
            bottom = h if i == n - 1 else y + seg_h
            p.fillRect(0, y + gap, sw, bottom - gap - y - gap, QColor(c))
        p.end()


# ────────────────────────────────────────────────────────────────────────────
#  Star Trek LCARS hangeffektek – WAV generátor és lejátszó
# ────────────────────────────────────────────────────────────────────────────


class LCARSSoundManager:
    """Star Trek LCARS hangeffektek kezelője – QSoundEffect alapú lejátszás."""

    # Hang definíciók: (frekvencia_hz, időtartam_sec, amplitúdó)
    _SOUND_DEFS: Dict[str, List[Tuple[float, float, float]]] = {
        # Zónaváltás hangok – jellegzetes LCARS csippanások
        "zone_up": [(880, 0.08, 1.0), (1320, 0.12, 0.8)],       # felfelé lépés
        "zone_down": [(1320, 0.08, 0.8), (880, 0.12, 1.0)],     # lefelé lépés
        "zone_standby": [(440, 0.15, 0.5), (330, 0.2, 0.4)],    # standby-ba lépés
        # Szenzor események
        "sensor_dropout": [                                        # vészjelzés – hármas csipogás
            (1760, 0.06, 1.0), (0, 0.04, 0.0),
            (1760, 0.06, 1.0), (0, 0.04, 0.0),
            (1760, 0.06, 1.0),
        ],
        "sensor_reconnect": [                                      # visszacsatlakozás – emelkedő
            (660, 0.08, 0.7), (880, 0.08, 0.8), (1100, 0.12, 1.0),
        ],
        # Zwift
        "zwift_connect": [                                         # comm channel nyitás
            (440, 0.06, 0.6), (660, 0.06, 0.7), (880, 0.06, 0.8),
            (1100, 0.15, 1.0),
        ],
        "zwift_disconnect": [                                      # comm channel zárás
            (1100, 0.06, 0.8), (880, 0.06, 0.7), (660, 0.06, 0.6),
            (440, 0.15, 0.5),
        ],
        # Fan sebesség – rövid visszajelzés
        "fan_tx": [(1047, 0.05, 0.5), (1319, 0.07, 0.6)],       # parancs elküldve
        # HUD indítás – tricorder kinyitás hangeffekt
        "hud_startup": [
            (1200, 0.06, 0.3), (1500, 0.06, 0.4), (1800, 0.06, 0.5),
            (2200, 0.08, 0.6), (2600, 0.10, 0.7), (3000, 0.08, 0.8),
            (2400, 0.06, 0.5), (2800, 0.06, 0.6), (3200, 0.12, 0.9),
            (2000, 0.15, 0.4),
        ],
        # HUD bezárás – tricorder becsukás hangeffekt (fordított söprés lefelé)
        "hud_shutdown": [
            (2000, 0.06, 0.4), (3200, 0.06, 0.6), (2800, 0.06, 0.5),
            (2400, 0.08, 0.7), (3000, 0.08, 0.8), (2600, 0.06, 0.6),
            (2200, 0.06, 0.5), (1800, 0.06, 0.5), (1500, 0.06, 0.4),
            (1200, 0.10, 0.3), (800, 0.15, 0.2),
        ],
    }

    def __init__(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="lcars_snd_")
        self._effects: Dict[str, Any] = {}
        self._enabled = True
        self._volume = 0.5
        self._cleaned_up = False
        self._generate_all()
        atexit.register(self.cleanup)

    def _generate_all(self) -> None:
        """Összes hangeffekt generálása és QSoundEffect létrehozása."""
        if QSoundEffect is None:
            logger.info("QSoundEffect nem elérhető – hangeffektek kikapcsolva")
            return
        for name, tones in self._SOUND_DEFS.items():
            try:
                wav_data = generate_tone(tones)
                wav_path = os.path.join(self._temp_dir, f"{name}.wav")
                with open(wav_path, "wb") as f:
                    f.write(wav_data)
                effect = QSoundEffect()
                effect.setSource(QUrl.fromLocalFile(wav_path))
                effect.setVolume(self._volume)
                self._effects[name] = effect
            except Exception as exc:
                logger.warning(f"LCARS hang generálás sikertelen ({name}): {exc}")

    def play(self, name: str) -> None:
        """Hangeffekt lejátszása név alapján."""
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
        """Adott hangeffekt időtartama milliszekundumban (0, ha nem töltődött be)."""
        if name not in self._effects:
            return 0
        tones = self._SOUND_DEFS.get(name, [])
        return sum(int(d * 1000) for _, d, _ in tones)

    def set_enabled(self, enabled: bool) -> None:
        """Hangeffektek be/kikapcsolása."""
        self._enabled = enabled

    def set_volume(self, volume: float) -> None:
        """Összes hangeffekt hangerő beállítása (0.0–1.0)."""
        self._volume = volume
        for effect in self._effects.values():
            effect.setVolume(volume)

    def cleanup(self) -> None:
        """Összes effect leállítása és temp fájlok törlése."""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        for effect in self._effects.values():
            effect.stop()
        self._effects.clear()
        try:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        except Exception as exc:
            logger.debug(f"Temp dir törlési hiba: {exc}")


class HUDWindow(QWidget):
    """Lebegő, átlátszó HUD ablak – Star Trek LCARS stílusú megjelenítés (PySide6)."""

    # ─── LCARS SZÍN PALETTA ───
    BG = "#000a14"
    PANEL_BG = "#001020"
    LCARS_ORANGE = "#FF9900"
    LCARS_GOLD = "#FFCC66"
    LCARS_BLUE = "#5599FF"
    LCARS_CYAN = "#00CCFF"
    LCARS_CYAN_DIM = "#006688"
    LCARS_RED = "#FF3333"
    LCARS_MAGENTA = "#CC6699"
    LCARS_TAN = "#FFAA66"
    LCARS_PURPLE = "#9977CC"
    TEXT_BRIGHT = "#DDEEFF"
    TEXT_DIM = "#556688"
    BORDER_GLOW = "#003355"
    ZONE_COLORS = {
        0: "#556688",
        1: "#00CCFF",
        2: "#FF9900",
        3: "#FF3333",
    }
    _VAL_BG = "#001828"

    UPDATE_INTERVAL_MS = 500

    def __init__(self, controller: "FanController", app: "QApplication") -> None:  # type: ignore[reportInvalidTypeForm]
        super().__init__()
        self._base_width = 340
        self._base_height = 460
        self._scale = 1.0
        self._ctrl = controller
        self._app = app
        self._drag_pos: Optional[QPoint] = None  # type: ignore[reportInvalidTypeForm]
        self._resize_active = False
        self._resize_start_pos = QPoint()
        self._resize_start_size = QSize()

        # Referencia listák a skálázható label-ekhez
        self._row_key_labels: list[QLabel] = []  # type: ignore[reportInvalidTypeForm]
        self._status_key_labels: list[QLabel] = []  # type: ignore[reportInvalidTypeForm]

        # Flash effekt: előző értékek és flash számlálók
        self._prev_power: Optional[float] = None
        self._prev_hr: Optional[float] = None
        self._flash_power: int = 0  # hátralévő flash ciklusok
        self._flash_hr: int = 0
        self._flash_ble_tick: int = 0  # folyamatos villogás számláló

        # ───────── LCARS HANGEFFEKTEK ─────────
        self._sound = LCARSSoundManager()
        hud_cfg: HudConfig = self._ctrl.settings["hud"]
        self._sound.set_enabled(hud_cfg.sound_enabled)
        self._sound.set_volume(hud_cfg.sound_volume)
        self._prev_zone: Optional[int] = None
        self._prev_ble_status: Optional[str] = None
        self._prev_ant_status: Optional[str] = None
        self._prev_ble_sens_status: Optional[str] = None
        self._prev_zwift_status: Optional[str] = None
        self._prev_last_sent_time: float = 0.0

        # ───────── ZWIFT PROCESS MONITOR ─────────
        self._zwift_was_running = False
        self._zwift_seen = False           # True ha egyszer már láttuk futni
        self._zwift_check_counter = 0
        self._ZWIFT_CHECK_INTERVAL = 20    # minden 20. _update hívás = ~10s
        self._zwift_check_running = False  # race condition védelem
        self._zwift_grace_start: float = time.time()
        self._ZWIFT_GRACE_PERIOD: float = 300.0  # 5 perc várakozás indulásra

        # ───────── ABLAK BEÁLLÍTÁS ─────────
        self.setWindowTitle("LCARS Fan HUD")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        hud_cfg: HudConfig = controller.settings["hud"]
        self._initial_opacity = max(20, min(100, hud_cfg.opacity))
        self.setWindowOpacity(self._initial_opacity / 100.0)
        self.setGeometry(20, 20, self._base_width, self._base_height)
        self.setMinimumSize(220, 350)
        self.setStyleSheet(f"background-color: {self.BG};")

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
        body.setStyleSheet(f"background-color: {self.BG};")
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

        # ───────── ZÓNA KIJELZŐ ─────────
        self._lbl_zone_label = QLabel("FAN ZONE")
        self._lbl_zone_label.setStyleSheet(
            f"background-color: {self.LCARS_CYAN}; color: #000a14; "
            f"font-family: '{self._font_family}'; font-size: 12pt; font-weight: bold; "
            f"padding: 2px 4px; border-radius: 4px;"
        )
        content_layout.addWidget(self._lbl_zone_label)

        self._lbl_zone = QLabel("– – –")
        self._lbl_zone.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_zone.setStyleSheet(
            f"background-color: {self._VAL_BG}; color: {self.LCARS_CYAN}; "
            f"font-family: '{self._font_family}'; font-size: 19pt; font-weight: bold; "
            f"padding: 3px 6px; border-radius: 4px;"
        )
        content_layout.addWidget(self._lbl_zone)

        # ───────── ÁLLAPOT CSÍK (tiles) ─────────
        tile_frame = QWidget(content)
        tile_frame.setStyleSheet(f"background-color: {self.PANEL_BG};")
        tile_layout = QHBoxLayout(tile_frame)
        tile_layout.setContentsMargins(0, 0, 0, 4)
        tile_layout.setSpacing(2)

        self._tile_zero_imm = self._make_tile(tile_layout, "ZRO IMM")
        self._tile_zero_hr_imm = self._make_tile(tile_layout, "ZHR IMM")
        self._tile_higher_wins = self._make_tile(tile_layout, "HI WINS")
        self._tile_ant = self._make_tile(tile_layout, "ANT+")
        self._tile_ble = self._make_tile(tile_layout, "BLE")
        self._tile_cooldown = self._make_tile(tile_layout, "COOL")
        content_layout.addWidget(tile_frame)

        # ───────── TELEMETRIA SOROK ─────────
        self._lbl_power = self._make_row(content_layout, "POWER", "– – –",
                                          self.LCARS_GOLD, self.LCARS_TAN)
        self._lbl_hr = self._make_row(content_layout, "HEART RATE", "– – –",
                                       self.LCARS_RED, self.LCARS_ORANGE)

        # ───────── SZEPARÁTOR ─────────
        sep = QFrame(content)
        sep.setFixedHeight(2)
        sep.setStyleSheet(f"background-color: {self.BORDER_GLOW}; margin: 6px 10px;")
        content_layout.addWidget(sep)

        # ───────── RENDSZER STÁTUSZ ─────────
        self._lbl_ble = self._make_status_row(content_layout, "BLE FAN", "OFFLINE",
                                               self.LCARS_BLUE)
        self._lbl_ble_sens = self._make_status_row(content_layout, "BLE SENS",
                                                     "– – –", self.LCARS_BLUE)
        self._lbl_ant = self._make_status_row(content_layout, "ANT+",
                                               "– – –", self.LCARS_PURPLE)
        self._lbl_zwift_udp = self._make_status_row(content_layout, "ZWIFT",
                                                      "– – –", self.LCARS_PURPLE)

        # ───────── SZEPARÁTOR 2 ─────────
        sep2 = QFrame(content)
        sep2.setFixedHeight(2)
        sep2.setStyleSheet(f"background-color: {self.BORDER_GLOW}; margin: 6px 10px;")
        content_layout.addWidget(sep2)

        # ───────── RENDSZER INFO ─────────
        self._lbl_last_sent = self._make_status_row(content_layout, "LAST TX",
                                                      "– – –", self.LCARS_TAN)
        self._lbl_cool = self._make_status_row(content_layout, "COOLDOWN",
                                                "– – –", self.LCARS_TAN)

        # ───────── OPACITY SLIDER ─────────
        slider_widget = QWidget(content)
        slider_widget.setStyleSheet(f"background-color: {self.PANEL_BG};")
        slider_layout = QHBoxLayout(slider_widget)
        slider_layout.setContentsMargins(0, 6, 0, 4)
        slider_layout.setSpacing(4)

        self._opacity_label = QLabel("OPACITY")
        self._opacity_label.setStyleSheet(
            f"background-color: {self.LCARS_GOLD}; color: #000a14; "
            f"font-family: '{self._font_family}'; font-size: 9pt; font-weight: bold; "
            f"padding: 2px 4px; border-radius: 4px;"
        )
        slider_layout.addWidget(self._opacity_label)

        self._alpha_slider = QSlider(Qt.Orientation.Horizontal)
        self._alpha_slider.setRange(20, 100)
        self._alpha_slider.setValue(self._initial_opacity)
        self._alpha_slider.setStyleSheet(
            f"QSlider::groove:horizontal {{"
            f"  background: #002244; height: 14px; border-radius: 2px;"
            f"}}"
            f"QSlider::handle:horizontal {{"
            f"  background: {self.LCARS_CYAN}; width: 16px; margin: -2px 0;"
            f"  border-radius: 3px;"
            f"}}"
        )
        self._alpha_slider.valueChanged.connect(self._on_alpha_change)
        slider_layout.addWidget(self._alpha_slider, 1)

        self._alpha_value = QLabel(f"{self._initial_opacity}%")
        self._alpha_value.setStyleSheet(
            f"color: {self.LCARS_CYAN}; background-color: {self.PANEL_BG}; "
            f"font-family: '{self._font_family}'; font-size: 11pt; font-weight: bold;"
        )
        self._alpha_value.setFixedWidth(40)
        self._alpha_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        slider_layout.addWidget(self._alpha_value)

        content_layout.addWidget(slider_widget)
        content_layout.addStretch()

        main_layout.addWidget(body, 1)

        # Footer
        self._footer = LCARSFooterWidget(self, self._font_family, self._scale)
        main_layout.addWidget(self._footer)

        # ───────── KONTEXTUS MENÜ ─────────
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

        # ───────── TIMER ─────────
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update)
        self._timer.start(self.UPDATE_INTERVAL_MS)

    @property
    def sound(self) -> "LCARSSoundManager":
        return self._sound

    # ────────── FONT BETÖLTÉS ──────────

    def _try_load_lcars_font(self) -> None:
        """Antonio font betöltése a smart_fan_controller package fonts/ mappájából.

        Keresési sorrend:
          1. <package_dir>/fonts/Antonio-{Bold,Regular}.ttf   (smart_fan_controller/fonts)
          2. <exe_dir>/smart_fan_controller/fonts/...         (PyInstaller frozen)
        Ha a fontok nem találhatók, a program rendszer fontot használ fallback-ként.
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
                # hud.py: smart_fan_controller/ui/hud.py → package gyökér két szinttel feljebb
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
                    f"LCARS fontok nem találhatók a {font_dir} mappában – "
                    f"rendszer font használata. Lásd: fonts/README.txt"
                )
        except Exception as exc:
            logger.warning(f"LCARS font betöltés sikertelen (rendszer font használata): {exc}")

    def _detect_best_font(self) -> str:
        """Legjobb elérhető LCARS-stílusú font kiválasztása."""
        try:
            available = set(QFontDatabase.families())
        except Exception as exc:
            logger.debug(f"Font lista lekérés sikertelen: {exc}")
            return "Consolas"

        preferred = [
            "Antonio", "Michroma", "Century Gothic", "Eras Bold ITC",
            "Eras Medium ITC", "Bahnschrift", "Trebuchet MS", "Segoe UI", "Consolas",
        ]
        for f in preferred:
            if f in available:
                return f
        return "Consolas"

    # ────────── UI SEGÉDFÜGGVÉNYEK ──────────

    def _make_row(self, layout: "QVBoxLayout", label: str, value: str,  # type: ignore[reportInvalidTypeForm]
                  color: str, label_bg: str) -> "QLabel":  # type: ignore[reportInvalidTypeForm]
        """Telemetria sor LCARS színes label háttérrel."""
        row = QWidget()
        row.setStyleSheet(f"background-color: {self.PANEL_BG};")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 2, 0, 2)
        row_layout.setSpacing(2)

        key_lbl = QLabel(label)
        key_lbl.setFixedWidth(100)
        key_lbl.setStyleSheet(
            f"background-color: {label_bg}; color: #000a14; "
            f"font-family: '{self._font_family}'; font-size: 9pt; font-weight: bold; "
            f"padding: 3px 4px; border-radius: 4px;"
        )
        row_layout.addWidget(key_lbl)
        self._row_key_labels.append(key_lbl)

        val_lbl = QLabel(value)
        val_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        val_lbl.setStyleSheet(
            f"background-color: {self._VAL_BG}; color: {color}; "
            f"font-family: '{self._font_family}'; font-size: 14pt; font-weight: bold; "
            f"padding: 3px 6px; border-radius: 4px;"
        )
        row_layout.addWidget(val_lbl, 1)

        layout.addWidget(row)
        return val_lbl

    def _make_tile(self, layout: "QHBoxLayout", text: str) -> "QLabel":  # type: ignore[reportInvalidTypeForm]
        """Állapot csík tile."""
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(
            f"background-color: {self.TEXT_DIM}; color: #000a14; "
            f"font-family: '{self._font_family}'; font-size: 9pt; font-weight: bold; "
            f"padding: 2px 5px; border-radius: 4px;"
        )
        layout.addWidget(lbl, 1)
        return lbl

    def _make_status_row(self, layout: "QVBoxLayout", label: str, value: str,  # type: ignore[reportInvalidTypeForm]
                         label_bg: str) -> "QLabel":  # type: ignore[reportInvalidTypeForm]
        """Státusz sor LCARS színes label háttérrel."""
        row = QWidget()
        row.setStyleSheet(f"background-color: {self.PANEL_BG};")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 2, 0, 2)
        row_layout.setSpacing(2)

        key_lbl = QLabel(label)
        key_lbl.setFixedWidth(100)
        key_lbl.setStyleSheet(
            f"background-color: {label_bg}; color: #000a14; "
            f"font-family: '{self._font_family}'; font-size: 9pt; font-weight: bold; "
            f"padding: 2px 4px; border-radius: 4px;"
        )
        row_layout.addWidget(key_lbl)
        self._status_key_labels.append(key_lbl)

        val_lbl = QLabel(value)
        val_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        val_lbl.setStyleSheet(
            f"background-color: {self._VAL_BG}; color: {self.TEXT_DIM}; "
            f"font-family: '{self._font_family}'; font-size: 11pt; "
            f"padding: 2px 6px; border-radius: 4px;"
        )
        row_layout.addWidget(val_lbl, 1)

        layout.addWidget(row)
        return val_lbl

    # ────────── DRAG / RESIZE ──────────

    def mousePressEvent(self, event: "QMouseEvent") -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            if (self.width() - pos.x() < 20) and (self.height() - pos.y() < 20):
                self._resize_active = True
                self._resize_start_pos = event.globalPosition().toPoint()
                self._resize_start_size = self.size()
            else:
                self._drag_pos = (
                    event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: "QMouseEvent") -> None:  # type: ignore[override]
        if self._resize_active:
            delta = event.globalPosition().toPoint() - self._resize_start_pos
            new_w = max(220, self._resize_start_size.width() + delta.x())
            new_h = max(350, self._resize_start_size.height() + delta.y())
            self.resize(new_w, new_h)
            self._scale = new_w / self._base_width
            self._apply_scale()
        elif self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: "QMouseEvent") -> None:  # type: ignore[override]
        self._drag_pos = None
        self._resize_active = False
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        super().keyPressEvent(event)

    # ────────── OPACITY ──────────

    def _set_alpha_from_menu(self, percent: int) -> None:
        self.setWindowOpacity(percent / 100.0)
        self._alpha_slider.setValue(percent)
        self._alpha_value.setText(f"{percent}%")
        self._save_hud_setting("opacity", percent)

    def _on_alpha_change(self, value: int) -> None:
        self.setWindowOpacity(value / 100.0)
        self._alpha_value.setText(f"{value}%")
        self._save_hud_setting("opacity", value)

    # ────────── KONTEXTUS MENÜ ──────────

    def _show_menu(self, pos: "QPoint") -> None:  # type: ignore[reportInvalidTypeForm]
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

        # ─── LCARS HANG BEÁLLÍTÁSOK ───
        menu.addSeparator()
        sound_enabled = self._sound.enabled
        toggle_label = "🔊 Hang: KI" if sound_enabled else "🔇 Hang: BE"
        menu.addAction(toggle_label, self._toggle_sound)

        vol_menu = menu.addMenu("🔉 Hangerő")
        vol_menu.setStyleSheet(menu_ss)
        for pct in (25, 50, 75, 100):
            v = pct / 100.0
            current = round(self._sound.volume * 100)
            marker = " ◄" if pct == current else ""
            vol_menu.addAction(
                f"{pct}%{marker}", lambda _v=v: self._set_sound_volume(_v)
            )

        menu.exec(self.mapToGlobal(pos))

    def _toggle_sound(self) -> None:
        """Hangeffektek be/kikapcsolása és mentés settings.json-ba."""
        new_state = not self._sound.enabled
        self._sound.set_enabled(new_state)
        self._save_hud_setting("sound_enabled", new_state)

    def _set_sound_volume(self, volume: float) -> None:
        """Hangerő beállítása és mentés settings.json-ba."""
        self._sound.set_volume(volume)
        self._save_hud_setting("sound_volume", round(volume, 2))

    def _save_hud_setting(self, key: str, value: Any) -> None:
        """Egy HUD beállítás frissítése és mentése (csak ha save_hud_settings=True).

        Frissíti a HUD beállítást a memóriában, majd ha save_hud_settings engedélyezett,
        csak a "hud" szekciót menti a JSON-ba (nem az egész settings-et, így az egyéb
        szekciók kézi szerkesztéseit megőrzi).
        """
        settings = self._ctrl.settings
        hud_cfg: HudConfig = settings["hud"]
        # Map old key names to dataclass attribute names
        attr = key.replace(".", "_") if "." in key else key
        if hasattr(hud_cfg, attr):
            setattr(hud_cfg, attr, value)
            # Mentés: csak a "hud" szekciót frissítjük, és csak ha engedélyezett
            from smart_fan_controller.config.loader import save_hud_settings_only
            if save_hud_settings_only(self._ctrl.settings_file, hud_cfg):
                logger.info(f"HUD beállítás mentve: hud.{key} = {value}")
            elif hud_cfg.save_hud_settings:
                # save_hud_settings=True volt, de valamilyen hiba történt az íráskor
                logger.warning(f"HUD beállítás nem sikerült menteni: hud.{key} = {value}")
            # Ha save_hud_settings=False, nincs log üzenet (szándékos)

    # ────────── LABEL FRISSÍTÉS SEGÉD ──────────

    import re as _re
    _RE_COLOR = _re.compile(r"(?<!-)color:\s*[^;]+;")
    _RE_BG_COLOR = _re.compile(r"background-color:\s*[^;]+;")

    @staticmethod
    def _update_label(lbl: "QLabel", text: str, color: str) -> None:  # type: ignore[reportInvalidTypeForm]
        """Label szöveg és szín frissítése stylesheet-tel."""
        current = lbl.styleSheet()
        new_ss = HUDWindow._RE_COLOR.sub(f"color: {color};", current, count=1)
        lbl.setStyleSheet(new_ss)
        lbl.setText(text)

    @staticmethod
    def _update_tile_bg(tile: "QLabel", bg: str) -> None:  # type: ignore[reportInvalidTypeForm]
        """Tile háttérszín frissítése."""
        current = tile.styleSheet()
        new_ss = HUDWindow._RE_BG_COLOR.sub(f"background-color: {bg};", current, count=1)
        tile.setStyleSheet(new_ss)

    @staticmethod
    def _lighten(color_hex: str, factor: float = 0.5) -> str:
        """Szín világosítása – factor=0 eredeti, factor=1 fehér."""
        r = int(color_hex[1:3], 16)
        g = int(color_hex[3:5], 16)
        b = int(color_hex[5:7], 16)
        r = int(r + (255 - r) * factor)
        g = int(g + (255 - g) * factor)
        b = int(b + (255 - b) * factor)
        return f"#{r:02X}{g:02X}{b:02X}"

    # ────────── FRISSÍTÉS (500 ms) ──────────

    def _update(self) -> None:
        try:
            state = self._ctrl.state
            ble_fan = self._ctrl.ble_fan
            cool = self._ctrl.cooldown_ctrl

            if state is not None:
                zone, power, hr = state.ui_snapshot.read()

                zone_color = (
                    self.ZONE_COLORS.get(zone, self.LCARS_CYAN)
                    if zone is not None else self.TEXT_DIM
                )
                zone_names = {0: "STANDBY", 1: "ZONE 1", 2: "ZONE 2", 3: "ZONE 3"}
                zone_txt = (
                    zone_names.get(zone, "– – –")
                    if zone is not None else "– – –"
                )

                self._update_label(self._lbl_zone, zone_txt, zone_color)

                # Zónaváltás hang
                if zone is not None and zone != self._prev_zone and self._prev_zone is not None:
                    if zone == 0:
                        self._sound.play("zone_standby")
                    elif zone > self._prev_zone:
                        self._sound.play("zone_up")
                    else:
                        self._sound.play("zone_down")
                self._prev_zone = zone

                # Power – flash ha változott
                if power is not None and power != self._prev_power:
                    self._flash_power = 2  # 2 ciklus = ~1s villanás
                self._prev_power = power

                if self._flash_power > 0:
                    self._flash_power -= 1
                    power_color = self._lighten(self.LCARS_GOLD) if self._flash_power % 2 == 1 else self.LCARS_GOLD
                else:
                    power_color = self.LCARS_GOLD if power is not None else self.TEXT_DIM

                self._update_label(
                    self._lbl_power,
                    "– – –" if power is None else f"{power:.0f} W",
                    power_color,
                )

                # HR – flash ha változott
                if hr is not None and hr != self._prev_hr:
                    self._flash_hr = 2
                self._prev_hr = hr

                if self._flash_hr > 0:
                    self._flash_hr -= 1
                    hr_color = self._lighten(self.LCARS_RED) if self._flash_hr % 2 == 1 else self.LCARS_RED
                else:
                    hr_color = self.LCARS_RED if hr is not None else self.TEXT_DIM

                self._update_label(
                    self._lbl_hr,
                    "– – –" if hr is None else f"{hr:.0f} BPM",
                    hr_color,
                )

            # BLE fan – villogás OFFLINE/PIN FAIL állapotoknál
            self._flash_ble_tick = 1 - self._flash_ble_tick
            flash_white = self._flash_ble_tick == 0
            ble_status = "DISABLED"
            if ble_fan is not None:
                if ble_fan.auth_failed:
                    c = self._lighten(self.LCARS_GOLD) if flash_white else self.LCARS_GOLD
                    self._update_label(self._lbl_ble, "PIN FAIL", c)
                    ble_status = "PIN FAIL"
                elif ble_fan.is_connected:
                    self._update_label(self._lbl_ble, "ONLINE", self.LCARS_CYAN)
                    ble_status = "ONLINE"
                else:
                    c = self._lighten(self.LCARS_RED) if flash_white else self.LCARS_RED
                    self._update_label(self._lbl_ble, "OFFLINE", c)
                    ble_status = "OFFLINE"
            else:
                c = self._lighten(self.TEXT_DIM) if flash_white else self.TEXT_DIM
                self._update_label(self._lbl_ble, "DISABLED", c)

            # BLE fan hangeffekt
            if self._prev_ble_status is not None and ble_status != self._prev_ble_status:
                if ble_status == "ONLINE":
                    self._sound.play("sensor_reconnect")
                elif ble_status in ("OFFLINE", "PIN FAIL"):
                    self._sound.play("sensor_dropout")
            self._prev_ble_status = ble_status

            # BLE szenzorok
            ds: DatasourceConfig = self._ctrl.settings["datasource"]
            power_ble = ds.power_source == DataSource.BLE
            hr_ble = ds.hr_source == DataSource.BLE

            if not power_ble and not hr_ble:
                c = self._lighten(self.TEXT_DIM) if flash_white else self.TEXT_DIM
                self._update_label(self._lbl_ble_sens, "– – –", c)
            else:
                ble = getattr(self._ctrl, "_ble_sensor_handler", None)
                if ble is not None:
                    now = time.monotonic()
                    power_ok = (
                        power_ble
                        and (ble.power_lastdata > 0)
                        and (now - ble.power_lastdata < 10)
                    )
                    hr_ok = (
                        hr_ble
                        and (ble.hr_lastdata > 0)
                        and (now - ble.hr_lastdata < 10)
                    )
                    p_s = "OK" if power_ok else ("--" if not power_ble else "FAIL")
                    h_s = "OK" if hr_ok else ("--" if not hr_ble else "FAIL")

                    ble_states: list[bool] = []
                    if power_ble:
                        ble_states.append(power_ok)
                    if hr_ble:
                        ble_states.append(hr_ok)

                    if any(s is False for s in ble_states):
                        row_color = self._lighten(self.LCARS_RED) if flash_white else self.LCARS_RED
                    elif all(s is True for s in ble_states):
                        row_color = self.LCARS_CYAN
                    else:
                        row_color = self.LCARS_GOLD

                    self._update_label(
                        self._lbl_ble_sens, f"P:{p_s}  HR:{h_s}", row_color
                    )
                else:
                    self._update_label(self._lbl_ble_sens, "STANDBY", self.LCARS_GOLD)

            # ANT+
            power_ant = ds.power_source == DataSource.ANTPLUS
            hr_ant = ds.hr_source == DataSource.ANTPLUS
            ant = getattr(self._ctrl, "_antplus_handler", None)

            if not power_ant and not hr_ant:
                c = self._lighten(self.TEXT_DIM) if flash_white else self.TEXT_DIM
                self._update_label(self._lbl_ant, "– – –", c)
            elif ant is not None:
                now = time.monotonic()
                power_ok = (
                    power_ant
                    and (ant.power_lastdata > 0)
                    and (now - ant.power_lastdata < 10)
                )
                hr_ok = (
                    hr_ant
                    and (ant.hr_lastdata > 0)
                    and (now - ant.hr_lastdata < 10)
                )
                p_s = "OK" if power_ok else ("--" if not power_ant else "FAIL")
                h_s = "OK" if hr_ok else ("--" if not hr_ant else "FAIL")

                ant_states: list[bool] = []
                if power_ant:
                    ant_states.append(power_ok)
                if hr_ant:
                    ant_states.append(hr_ok)

                if any(s is False for s in ant_states):
                    row_color = self._lighten(self.LCARS_RED) if flash_white else self.LCARS_RED
                elif all(s is True for s in ant_states):
                    row_color = self.LCARS_CYAN
                else:
                    row_color = self.LCARS_GOLD

                self._update_label(self._lbl_ant, f"P:{p_s}  HR:{h_s}", row_color)
                # ANT+ hangeffekt
                ant_status = "FAIL" if any(s is False for s in ant_states) else "OK"
                if self._prev_ant_status is not None and ant_status != self._prev_ant_status:
                    if ant_status == "OK":
                        self._sound.play("sensor_reconnect")
                    else:
                        self._sound.play("sensor_dropout")
                self._prev_ant_status = ant_status
            else:
                c = self._lighten(self.TEXT_DIM) if flash_white else self.TEXT_DIM
                self._update_label(self._lbl_ant, "– – –", c)

            # Zwift
            zwift = getattr(self._ctrl, "_zwift_udp", None)
            power_zwift = ds.power_source == DataSource.ZWIFTUDP
            hr_zwift = ds.hr_source == DataSource.ZWIFTUDP

            if zwift is not None and (power_zwift or hr_zwift):
                now = time.monotonic()
                ok = (
                    zwift.last_packet_time > 0
                    and (now - zwift.last_packet_time) < 5.0
                )
                zwift_status = "RECEIVING" if ok else "NO SIGNAL"
                if ok:
                    self._update_label(
                        self._lbl_zwift_udp, "RECEIVING", self.LCARS_CYAN
                    )
                else:
                    c = self._lighten(self.LCARS_RED) if flash_white else self.LCARS_RED
                    self._update_label(self._lbl_zwift_udp, "NO SIGNAL", c)

                # Zwift hangeffekt
                if self._prev_zwift_status is not None and zwift_status != self._prev_zwift_status:
                    if zwift_status == "RECEIVING":
                        self._sound.play("zwift_connect")
                    else:
                        self._sound.play("zwift_disconnect")
                self._prev_zwift_status = zwift_status
            else:
                c = self._lighten(self.TEXT_DIM) if flash_white else self.TEXT_DIM
                self._update_label(self._lbl_zwift_udp, "– – –", c)

            # Last TX
            if ble_fan is not None and getattr(ble_fan, "last_sent_time", 0) > 0:
                cur_sent_time = ble_fan.last_sent_time
                ago = time.monotonic() - cur_sent_time
                self._update_label(self._lbl_last_sent, f"{ago:.0f}s AGO", self.LCARS_TAN)

                # Fan TX hangeffekt – csak ha új parancs ment ki
                if cur_sent_time != self._prev_last_sent_time and self._prev_last_sent_time > 0:
                    self._sound.play("fan_tx")
                self._prev_last_sent_time = cur_sent_time
            else:
                c = self._lighten(self.TEXT_DIM) if flash_white else self.TEXT_DIM
                self._update_label(self._lbl_last_sent, "– – –", c)

            # Cooldown
            if cool is not None:
                active, remaining = cool.snapshot()
                if active:
                    self._update_label(
                        self._lbl_cool, f"{remaining:.0f}s", self.LCARS_GOLD
                    )
                else:
                    c = self._lighten(self.TEXT_DIM) if flash_white else self.TEXT_DIM
                    self._update_label(self._lbl_cool, "INACTIVE", c)
            else:
                c = self._lighten(self.TEXT_DIM) if flash_white else self.TEXT_DIM
                self._update_label(self._lbl_cool, "– – –", c)

            # ── Állapot csík frissítése (aktív = villogó háttér) ──
            zpi = self._ctrl.settings["power_zones"].zero_power_immediate
            if zpi:
                bg = self._lighten(self.LCARS_CYAN) if flash_white else self.LCARS_CYAN
            else:
                bg = self.TEXT_DIM
            self._update_tile_bg(self._tile_zero_imm, bg)

            zhi = self._ctrl.settings["heart_rate_zones"].zero_hr_immediate
            if zhi:
                bg = self._lighten(self.LCARS_CYAN) if flash_white else self.LCARS_CYAN
            else:
                bg = self.TEXT_DIM
            self._update_tile_bg(self._tile_zero_hr_imm, bg)

            zone_mode_val = self._ctrl.settings["heart_rate_zones"].zone_mode
            hw = zone_mode_val == ZoneMode.HIGHER_WINS
            if hw:
                bg = self._lighten(self.LCARS_ORANGE) if flash_white else self.LCARS_ORANGE
            else:
                bg = self.TEXT_DIM
            self._update_tile_bg(self._tile_higher_wins, bg)

            if power_ant or hr_ant:
                bg = self._lighten(self.LCARS_PURPLE) if flash_white else self.LCARS_PURPLE
            else:
                bg = self.TEXT_DIM
            self._update_tile_bg(self._tile_ant, bg)

            if power_ble or hr_ble:
                bg = self._lighten(self.LCARS_BLUE) if flash_white else self.LCARS_BLUE
            else:
                bg = self.TEXT_DIM
            self._update_tile_bg(self._tile_ble, bg)

            if cool is not None:
                cd_active, _ = cool.snapshot()
                if cd_active:
                    bg = self._lighten(self.LCARS_GOLD) if flash_white else self.LCARS_GOLD
                else:
                    bg = self.TEXT_DIM
            else:
                bg = self.TEXT_DIM
            self._update_tile_bg(self._tile_cooldown, bg)

            # ── ZwiftApp.exe process figyelés (~10s-onként) ──
            if self._ctrl.settings["hud"].close_at_zwiftapp_exe:
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
            logger.warning(f"HUD _update hiba: {exc}")

    # ────────── ZWIFT PROCESS MONITOR ──────────

    def _check_zwift_process(self) -> None:
        """Háttérszálban ellenőrzi, hogy a ZwiftApp.exe fut-e."""
        try:
            running = self._ctrl.is_process_running("ZwiftApp.exe")
            should_close = False
            if running:
                if not self._zwift_seen:
                    self._zwift_seen = True
                    logger.info("ZwiftApp.exe észlelve / detected.")
            elif self._zwift_seen:
                # Zwift korábban futott, de most már nem → HUD bezárása
                logger.info("ZwiftApp.exe kilépett, HUD leállítása...")
                should_close = True
            elif time.time() - self._zwift_grace_start >= self._ZWIFT_GRACE_PERIOD:
                # Grace period lejárt, Zwift soha nem indult el → kilépés
                logger.info(
                    "ZwiftApp.exe nem indult el %.0f másodperc alatt, kilépés...",
                    self._ZWIFT_GRACE_PERIOD,
                )
                should_close = True
            self._zwift_was_running = running
            if should_close:
                # QTimer.singleShot háttérszálból NEM működik (nincs Qt event
                # loop). QMetaObject.invokeMethod thread-safe: a fő szál event
                # loop-jába ütemezi a close() hívást.
                from PySide6.QtCore import QMetaObject
                QMetaObject.invokeMethod(
                    self, "close", Qt.ConnectionType.QueuedConnection,
                )
        finally:
            self._zwift_check_running = False

    # ────────── SKÁLÁZÁS ──────────

    def _apply_scale(self) -> None:
        s = self._scale

        self._header.set_scale(s)
        self._footer.set_scale(s)
        self._sidebar.set_scale(s)

    def cleanup_sound(self) -> None:
        """Publikus interfész a hangrendszer felszabadításához."""
        self._sound.cleanup()

    # ────────── MONITOR GEOMETRIA ──────────

    def _current_screen_name(self) -> str:
        """Az ablak aktuális képernyőjének neve (vagy üres ha nem elérhető)."""
        screen = self.screen()
        if screen is not None:
            return screen.name()
        return ""

    def _restore_geometry(self) -> None:
        """Visszaállítja az ablak pozícióját/méretét az utoljára használt monitorhoz.

        Ha a mentett monitor nem létezik, az aktív (elsődleges) monitorra helyezi.
        """
        hud_cfg: HudConfig = self._ctrl.settings["hud"]
        geo_map = hud_cfg.window_geometry
        if not geo_map:
            return

        # Elérhető monitorok nevei
        available = {}
        for s in self._app.screens():
            available[s.name()] = s

        # Megpróbáljuk az utolsó használt monitort (a dict utolsó kulcsa)
        last_screen_name = list(geo_map.keys())[-1] if geo_map else ""
        if last_screen_name in available and last_screen_name in geo_map:
            rect = geo_map[last_screen_name]
            target_screen = available[last_screen_name]
        else:
            # Monitor nem létezik → aktív (elsődleges) monitor, ha van rá mentett geom
            primary = self._app.primaryScreen()
            if primary is None:
                return
            pname = primary.name()
            if pname in geo_map:
                rect = geo_map[pname]
            else:
                # Nincs semmilyen mentett geometria ehhez a monitorhoz
                return
            target_screen = primary

        # Validáljuk, hogy a pozíció a monitor területén belül van
        sg = target_screen.availableGeometry()
        x = max(sg.x(), min(rect["x"], sg.x() + sg.width() - 100))
        y = max(sg.y(), min(rect["y"], sg.y() + sg.height() - 100))
        w = max(self.minimumWidth(), min(rect["w"], sg.width()))
        h = max(self.minimumHeight(), min(rect["h"], sg.height()))
        self.setGeometry(x, y, w, h)
        self._scale = w / self._base_width

    def _save_geometry(self) -> None:
        """Elmenti az ablak pozícióját/méretét az aktuális monitorhoz."""
        screen_name = self._current_screen_name()
        if not screen_name:
            return
        geo = self.geometry()
        rect = {"x": geo.x(), "y": geo.y(), "w": geo.width(), "h": geo.height()}
        hud_cfg: HudConfig = self._ctrl.settings["hud"]
        hud_cfg.window_geometry[screen_name] = rect
        self._save_hud_setting("window_geometry", hud_cfg.window_geometry)

    # ────────── RUN / CLOSE ──────────

    def run(self) -> None:
        self._restore_geometry()
        self.show()
        self._sound.play("hud_startup")
        self._app.exec()

    def closeEvent(self, event: Any) -> None:
        if getattr(self, "_close_done", False):
            # Harmadik hívás: a hang lejátszódott, ténylegesen bezárjuk
            self._sound.cleanup()
            super().closeEvent(event)
            self._app.quit()
            return
        if getattr(self, "_closing", False):
            # Második hívás (pl. finally blokkból): még várjuk a hangot, ignoráljuk
            event.ignore()
            return
        self._closing = True
        self._save_geometry()
        event.ignore()
        self._timer.stop()
        self._sound.play("hud_shutdown")
        # Várunk, amíg a bezáró hang lejátszódik, majd ténylegesen bezárjuk
        duration_ms = self._sound.sound_duration_ms("hud_shutdown")

        def _finish_close() -> None:
            self._close_done = True
            self.close()

        QTimer.singleShot(duration_ms + 100, _finish_close)

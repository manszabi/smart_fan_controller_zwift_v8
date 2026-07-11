#!/usr/bin/env python3
# pyright: reportInvalidTypeForm=false
"""LCARS GUI components – Star Trek HUD interface for Smart Fan Controller.

This module contains the visual components for the LCARS-style HUD:
  - LCARSHeaderWidget: Top header with title and version badge
  - LCARSFooterWidget: Bottom footer with LCARS styling
  - LCARSSidebarWidget: Colored sidebar segments
  - LCARSZoneBarWidget: Segmented zone indicator bar (STANDBY..ZONE 3)
  - LCARSMeterWidget: Thin rounded meter bar (power / heart-rate fill)
  - LCARSSoundManager: Sound effect playback (LCARS beeps/tones)
  - HUDWindow: Main floating HUD window with telemetry display
"""

from __future__ import annotations

import atexit
import logging
import os
import platform as _platform
import shutil
import sys
import tempfile
import threading
import time
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QTimer, QPoint, QSize, QRectF, QUrl, QMetaObject
from PySide6.QtGui import (
    QColor, QPainter, QBrush, QFont, QFontDatabase, QFontMetrics,
    QPainterPath, QMouseEvent, QPalette,
)
from PySide6.QtMultimedia import QSoundEffect
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QHBoxLayout, QVBoxLayout,
    QSlider, QMenu, QFrame, QSizePolicy,
)

from smart_fan_controller import __version__
from smart_fan_controller.config import DataSource, ZoneMode
from smart_fan_controller.config.loader import (
    HudConfig, DatasourceConfig, save_hud_settings_only,
)
from smart_fan_controller.core.helpers import generate_tone

if TYPE_CHECKING:
    # A FanController a smart_fan_controller.controller modulban él. A körkörös
    # import elkerülésére a típust itt Any-ként kezeljük; a controller egy
    # lazán kezelt, csak továbbadott objektum.
    FanController = Any

logger = logging.getLogger("zwift_fan_controller_new")


@lru_cache(maxsize=64)
def _qcolor(spec: str) -> QColor:
    """Cache-elt QColor – a visszaadott példányt tilos mutálni."""
    return QColor(spec)


@lru_cache(maxsize=64)
def _qbrush(spec: str) -> QBrush:
    """Cache-elt QBrush – a visszaadott példányt tilos mutálni."""
    return QBrush(QColor(spec))


class LCARSHeaderWidget(QWidget):
    """LCARS fejléc widget – QPainter-rel rajzolt felső sáv."""

    def __init__(self, parent: QWidget, font_family: str, scale: float = 1.0) -> None:
        super().__init__(parent)
        self._font_family = font_family
        self._scale = scale
        self._path_cache_key: tuple[int, float, int] | None = None
        self._path_cache: QPainterPath = QPainterPath()
        self.setFixedHeight(50)
        # Átlátszó háttér: a lekerekített alap-panelt a HUDWindow festi,
        # így az ablak sarkai valóban átlátszóak (lebegő kártya hatás)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background-color: transparent;")

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
        bar_h = max(18, int(25 * s))
        sw = max(10, int(20 * s))
        R = max(14, int(26 * s))
        corner_r = max(12, int(18 * s))

        cache_key = (w, s, ch)
        if self._path_cache_key != cache_key:
            # Fő narancssárga sáv ívvel + lekerekített bal ÉS jobb felső sarok
            # (a jobb felső ív az ablak lekerekítését követi, hogy a sáv ne
            # lógjon ki az átlátszó sarokba)
            rr = min(corner_r, bar_h)
            path = QPainterPath()
            path.moveTo(corner_r, 0)
            path.lineTo(w - 6 - rr, 0)
            path.arcTo(QRectF(w - 6 - 2 * rr, 0, 2 * rr, 2 * rr), 90, -90)
            path.lineTo(w - 6, bar_h)
            # Belső könyök ív – középpont (sw+R, bar_h+R), 90°→180°
            path.arcTo(QRectF(sw, bar_h, 2 * R, 2 * R), 90, 90)
            path.lineTo(sw, ch)
            path.lineTo(0, ch)
            path.lineTo(0, corner_r)
            path.arcTo(QRectF(0, 0, 2 * corner_r, 2 * corner_r), 180, -90)
            path.closeSubpath()

            self._path_cache = path
            self._path_cache_key = cache_key

        p.fillPath(self._path_cache, _qbrush(HUDWindow.LCARS_ORANGE))

        # Cím szöveg
        title_size = max(8, int(12 * s))
        p.setFont(QFont(self._font_family, title_size, QFont.Weight.Bold))
        p.setPen(_qcolor(HUDWindow.LCARS_CYAN))
        p.drawText(QRectF(sw + R, bar_h, w - 6 - sw - R, ch - bar_h),
                    Qt.AlignmentFlag.AlignCenter, "ZWIFT FAN CTRL")

        # Badge (lekerekített magenta pill + verzió)
        badge_w = max(40, int(62 * s))
        badge_rect = QRectF(w - badge_w - 8, 1, badge_w, bar_h - 3)
        badge_path = QPainterPath()
        badge_path.addRoundedRect(badge_rect, (bar_h - 3) / 2, (bar_h - 3) / 2)
        p.fillPath(badge_path, _qbrush(HUDWindow.LCARS_MAGENTA))
        ver_size = max(6, int(7 * s))
        p.setFont(QFont(self._font_family, ver_size))
        p.setPen(_qcolor("#FFFFFF"))
        p.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, f"v{__version__}")

        p.end()


class LCARSFooterWidget(QWidget):
    """LCARS lábléc widget – QPainter-rel rajzolt alsó sáv."""

    def __init__(self, parent: QWidget, font_family: str, scale: float = 1.0) -> None:
        super().__init__(parent)
        self._font_family = font_family
        self._scale = scale
        self._path_cache_key: tuple[int, float, int] | None = None
        self._path_cache: tuple[Any, ...] = ()
        self._opacity_tw_cache: tuple[int, int] | None = None  # (fontméret, szélesség)
        self.setFixedHeight(60)
        # Átlátszó háttér – a lekerekített alap-panelt a HUDWindow festi
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background-color: transparent;")
        # Az opacity vezérlők a felső, ívek közötti sávba kerülnek
        self._overlay = QHBoxLayout(self)
        self._overlay.setSpacing(4)
        self._update_overlay_margins()

    def _bar_metrics(self) -> tuple[int, int, int]:
        # Ugyanaz a vastagság/szélesség/sugár, mint a fejlécnél (tükörkép)
        s = self._scale
        sw = max(10, int(20 * s))
        R = max(14, int(26 * s))
        bar_h = max(18, int(25 * s))
        return sw, R, bar_h

    def _update_overlay_margins(self) -> None:
        # A csúszka a festett OPACITY doboz után kezdődik és a dobozzal
        # függőlegesen egy sávban (teteje/alja); jobbra az ívet követi.
        sw, R, bar_h = self._bar_metrics()
        box_top, box_bottom, box_right = self._opacity_box()
        fh = self.height()
        gap_s = max(6, int(8 * self._scale))
        self._overlay.setContentsMargins(
            box_right + gap_s, box_top, sw + R, fh - box_bottom
        )

    def _opacity_box(self) -> tuple[int, int, int]:
        """Az OPACITY doboz (box_top, box_bottom, box_right).
        Az ív KONCENTRIKUS a kék könyök ívével (azonos középpont, R-6 sugár),
        így a rés végig egyenletes 6px; a doboz teteje a könyök ív tetejével
        egy vonalban, bal széle a státuszsorok bal oldalával (sw+6)."""
        sw, R, bar_h = self._bar_metrics()
        fh = self.height()
        bar_top = fh - bar_h
        box_top = bar_top - R                    # a könyök ív tetejével egy vonalban
        box_bottom = bar_top - 6                 # egyenletes 6px rés a sáv fölött
        fs = max(7, int(9 * self._scale))
        if self._opacity_tw_cache is None or self._opacity_tw_cache[0] != fs:
            fm = QFontMetrics(QFont(self._font_family, fs, QFont.Weight.Bold))
            self._opacity_tw_cache = (fs, fm.horizontalAdvance("OPACITY"))
        text_w = self._opacity_tw_cache[1]
        pad = max(6, int(8 * self._scale))
        box_right = sw + R + text_w + 2 * pad    # a szöveg az íven túli flat részen
        return box_top, box_bottom, box_right

    def set_opacity_controls(self, label: QWidget, slider: QWidget,
                             value: QWidget) -> None:
        """Az opacity vezérlők beillesztése a footer felső sávjába.
        Az OPACITY feliratot a paintEvent rajzolja (íves dobozzal), ezért a
        címke widgetet elrejtjük."""
        label.hide()
        self._overlay.addWidget(slider, 1)
        self._overlay.addWidget(value)

    def set_scale(self, s: float) -> None:
        self._scale = s
        h = max(36, int(60 * s))
        self.setFixedHeight(h)
        self._update_overlay_margins()
        self.update()

    def paintEvent(self, event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        s = self._scale
        fh = self.height()
        bar_h = max(18, int(25 * s))
        sw = max(10, int(20 * s))
        R = max(14, int(26 * s))
        bar_top = fh - bar_h
        corner_r = max(12, int(18 * s))
        fs = max(7, int(9 * s))

        cache_key = (w, s, fh)
        if self._path_cache_key != cache_key:
            # Fő kék sáv ívvel + lekerekített bal alsó sarok
            path = QPainterPath()
            path.moveTo(0, 0)
            path.lineTo(sw, 0)
            # Belső könyök ív – középpont (sw+R, bar_top-R), 180°→270°
            path.arcTo(QRectF(sw, bar_top - 2 * R, 2 * R, 2 * R), 180, 90)
            path.lineTo(w - 6, bar_top)
            path.lineTo(w - 6, fh)
            path.lineTo(corner_r, fh)
            path.arcTo(QRectF(0, fh - 2 * corner_r, 2 * corner_r, 2 * corner_r),
                       270, -90)
            path.lineTo(0, 0)
            path.closeSubpath()

            # Szegmensek – középen a lila, tőle balra a kék alapsáv, jobbra a tan.
            # A tan közvetlenül a lila után kezdődik (nincs kék sliver).
            right_edge = w - 6                       # a sáv jobb széle
            flat_left = sw + R                       # a bal ív vége (lapos sáv kezdete)
            flat_right = right_edge - sw - R         # a jobb ív kezdete (lapos sáv vége)
            center = (flat_left + flat_right) / 2    # = right_edge / 2
            purple_w = max(1, int((flat_right - flat_left) / 3))
            purple_left = int(center - purple_w / 2)
            tan_left = purple_left + purple_w        # a tan rögtön a lila után

            # Fő tan (narancs) sáv ívvel + lekerekített jobb alsó sarok
            # – a bal alsó kék sarok pontos tükörképe (x' = right_edge - x)
            rpath = QPainterPath()
            rpath.moveTo(right_edge, 0)
            rpath.lineTo(right_edge - sw, 0)
            # Belső könyök ív – középpont (right_edge-sw-R, bar_top-R), 0°→-90°
            rpath.arcTo(QRectF(right_edge - sw - 2 * R, bar_top - 2 * R,
                               2 * R, 2 * R), 0, -90)
            rpath.lineTo(tan_left, bar_top)
            rpath.lineTo(tan_left, fh)
            rpath.lineTo(right_edge - corner_r, fh)
            rpath.arcTo(QRectF(right_edge - 2 * corner_r, fh - 2 * corner_r,
                                2 * corner_r, 2 * corner_r), 270, 90)
            rpath.closeSubpath()

            # OPACITY sárga doboz – a bal ív KONCENTRIKUS a kék könyök ívével
            # (azonos középpont, R-6 sugár), egyenletes 6px réssel; teteje a
            # könyök tetejével, bal széle a státuszsorok bal oldalával egy vonalban.
            box_top, box_bottom, box_right = self._opacity_box()
            gx = sw + 6                                 # bal (ív teteje) = státuszsorok bal széle
            rr = R - 6                                  # koncentrikus ív sugara
            box_r = max(3, int(4 * s))                  # jobb sarkok lekerekítése

            obox = QPainterPath()
            obox.moveTo(gx, box_top)                    # bal felső (az ív teteje)
            obox.lineTo(box_right - box_r, box_top)     # felső él
            obox.arcTo(QRectF(box_right - 2 * box_r, box_top,
                              2 * box_r, 2 * box_r), 90, -90)
            obox.lineTo(box_right, box_bottom - box_r)  # jobb él
            obox.arcTo(QRectF(box_right - 2 * box_r, box_bottom - 2 * box_r,
                              2 * box_r, 2 * box_r), 0, -90)
            obox.lineTo(sw + R, box_bottom)             # alsó él az ív aljáig
            # Koncentrikus ív (270°→180°) – középpont (sw+R, bar_top-R), rr sugár
            obox.arcTo(QRectF(sw + R - rr, bar_top - R - rr, 2 * rr, 2 * rr),
                       270, -90)
            obox.lineTo(gx, box_top)                    # zár
            obox.closeSubpath()

            self._path_cache = (
                path, rpath, obox,
                purple_left, purple_w, box_top, box_bottom, box_right,
            )
            self._path_cache_key = cache_key

        (path, rpath, obox,
         purple_left, purple_w, box_top, box_bottom, box_right) = self._path_cache

        p.fillPath(path, _qbrush(HUDWindow.LCARS_BLUE))
        p.fillRect(purple_left, bar_top, purple_w, bar_h,
                    _qcolor(HUDWindow.LCARS_PURPLE))
        p.fillPath(rpath, _qbrush(HUDWindow.LCARS_TAN))
        p.fillPath(obox, _qbrush(HUDWindow.LCARS_GOLD))

        # OPACITY felirat a doboz flat (íven túli) részén, középre
        p.setFont(QFont(self._font_family, fs, QFont.Weight.Bold))
        p.setPen(_qcolor("#000a14"))
        p.drawText(QRectF(sw + R, box_top, box_right - (sw + R), box_bottom - box_top),
                    Qt.AlignmentFlag.AlignCenter, "OPACITY")

        p.end()


class LCARSSidebarWidget(QWidget):
    """LCARS bal oldalsáv – színes szegmensek."""

    COLORS = ["#FF9900", "#FFCC66", "#5599FF", "#CC6699", "#9977CC", "#FFAA66"]

    def __init__(self, parent: QWidget, scale: float = 1.0) -> None:
        super().__init__(parent)
        self._scale = scale
        self.setFixedWidth(max(10, int(20 * scale)))
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background-color: transparent;")

    def set_scale(self, s: float) -> None:
        self._scale = s
        self.setFixedWidth(max(10, int(20 * s)))
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
        gap = max(1, int(1 * self._scale))
        for i, c in enumerate(self.COLORS):
            y = i * seg_h
            bottom = h if i == n - 1 else y + seg_h
            p.fillRect(0, y + gap, sw, bottom - gap - y - gap, _qcolor(c))
        p.end()


class LCARSZoneBarWidget(QWidget):
    """Szegmentált zóna sáv – a 4 zóna (STANDBY..ZONE 3) modern kijelzése.

    A szegmensek az aktuális zónáig világítanak a zóna színével
    (jelerősség-jelző stílus); zóna nélkül csak a halvány track látszik.
    """

    SEGMENTS = 4

    def __init__(self, parent: QWidget, scale: float = 1.0) -> None:
        super().__init__(parent)
        self._scale = scale
        self._zone: int | None = None
        self.setFixedHeight(max(6, int(8 * scale)))
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background-color: transparent;")

    def set_scale(self, s: float) -> None:
        self._scale = s
        self.setFixedHeight(max(6, int(8 * s)))
        self.update()

    def set_zone(self, zone: int | None) -> None:
        if zone != self._zone:
            self._zone = zone
            self.update()

    def paintEvent(self, event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        n = self.SEGMENTS
        gap = max(2, int(3 * self._scale))
        seg_w = (w - gap * (n - 1)) / n
        if seg_w < 2:
            p.end()
            return
        r = h / 2
        for i in range(n):
            x = i * (seg_w + gap)
            if self._zone is not None and i <= self._zone:
                color = HUDWindow.ZONE_COLORS.get(
                    self._zone, HUDWindow.LCARS_CYAN
                )
            else:
                color = HUDWindow._VAL_BG
            seg = QPainterPath()
            seg.addRoundedRect(QRectF(x, 0, seg_w, h), r, r)
            p.fillPath(seg, _qbrush(color))
        p.end()


class LCARSMeterWidget(QWidget):
    """Vékony, lekerekített kitöltés-sáv (power / pulzus vizualizáció)."""

    def __init__(self, parent: QWidget, scale: float = 1.0) -> None:
        super().__init__(parent)
        self._scale = scale
        self._fraction: float | None = None
        self._color: str = HUDWindow.TEXT_DIM
        self.setFixedHeight(max(4, int(5 * scale)))
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background-color: transparent;")

    def set_scale(self, s: float) -> None:
        self._scale = s
        self.setFixedHeight(max(4, int(5 * s)))
        self.update()

    def set_value(self, fraction: float | None, color: str) -> None:
        """Kitöltés frissítése – fraction: 0.0–1.0 vagy None (üres track)."""
        if fraction is not None:
            fraction = max(0.0, min(1.0, fraction))
        if fraction != self._fraction or color != self._color:
            self._fraction = fraction
            self._color = color
            self.update()

    def paintEvent(self, event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        r = h / 2
        track = QPainterPath()
        track.addRoundedRect(QRectF(0, 0, w, h), r, r)
        p.fillPath(track, _qbrush(HUDWindow._VAL_BG))
        if self._fraction is not None and self._fraction > 0:
            fill_w = max(float(h), w * self._fraction)
            fill = QPainterPath()
            fill.addRoundedRect(QRectF(0, 0, fill_w, h), r, r)
            p.fillPath(fill, _qbrush(self._color))
        p.end()


# ────────────────────────────────────────────────────────────────────────────
#  Star Trek LCARS hangeffektek – WAV generátor és lejátszó
# ────────────────────────────────────────────────────────────────────────────


class LCARSSoundManager:
    """Star Trek LCARS hangeffektek kezelője – QSoundEffect alapú lejátszás."""

    # Hang definíciók: (frekvencia_hz, időtartam_sec, amplitúdó)
    _SOUND_DEFS: dict[str, list[tuple[float, float, float]]] = {
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
        self._effects: dict[str, QSoundEffect] = {}
        self._enabled = True
        self._volume = 0.5
        self._cleaned_up = False
        self._generate_all()
        atexit.register(self.cleanup)

    def _generate_all(self) -> None:
        """Összes hangeffekt generálása és QSoundEffect létrehozása."""
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
                logger.warning("LCARS hang generálás sikertelen (%s): %s", name, exc)

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
            logger.debug("Temp dir törlési hiba: %s", exc)


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
    ZONE_NAMES = {0: "STANDBY", 1: "ZONE 1", 2: "ZONE 2", 3: "ZONE 3"}
    _VAL_BG = "#001828"

    UPDATE_INTERVAL_MS = 500

    # Abszolút minimum ablakméret (interaktív átméretezés alatt ez érvényes;
    # a tartalom-alapú minimum a húzás megállása után áll vissza)
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

        # Skálázható szöveges label-ek: (label, alap_pt_méret, fix_szélesség vagy None, bold)
        self._scalable_texts: list[tuple[QLabel, int, int | None, bool]] = []

        # Flash effekt: előző értékek és flash számlálók
        self._prev_power: float | None = None
        self._prev_hr: float | None = None
        self._flash_power: int = 0  # hátralévő flash ciklusok
        self._flash_hr: int = 0
        self._flash_ble_tick: int = 0  # folyamatos villogás számláló

        # ───────── LCARS HANGEFFEKTEK ─────────
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
        self._zwift_seen = False           # True ha egyszer már láttuk futni
        self._zwift_check_counter = 0
        self._ZWIFT_CHECK_INTERVAL = 20    # minden 20. _update hívás = ~10s
        self._zwift_check_running = False  # race condition védelem
        self._zwift_grace_start: float = time.time()
        self._ZWIFT_GRACE_PERIOD: float = 300.0  # 5 perc várakozás indulásra

        # BLE/ANT szenzor "élő" adat ablak. Szándékosan bőkezű: kerékpáros
        # mérők kifutáskor/0 W-nál elnémulhatnak, ne villantsanak folyton FAIL-t.
        self._SENSOR_STALE_S: float = 10.0

        # ───────── ABLAK BEÁLLÍTÁS ─────────
        self.setWindowTitle("LCARS Fan HUD")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        # Átlátszó ablakháttér: a lekerekített alap-panelt a paintEvent festi,
        # így a sarkok valóban átlátszóak (modern, lebegő kártya megjelenés)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # hud_cfg-t fentebb már lekértük (self._ctrl is controller)
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

        # ───────── ZÓNA KIJELZŐ ─────────
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
        # A szövegszín QPalette-ből jön (dinamikus), a stylesheet csak a statikus részt adja
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

        # Zóna szegmens-sáv a zóna kijelző alatt
        content_layout.addSpacing(3)
        self._zone_bar = LCARSZoneBarWidget(content, self._scale)
        content_layout.addWidget(self._zone_bar)
        content_layout.addSpacing(3)

        # ───────── ÁLLAPOT CSÍK (tiles) ─────────
        tile_frame = QWidget(content)
        tile_frame.setStyleSheet(f"background-color: {self.PANEL_BG};")
        tile_layout = QHBoxLayout(tile_frame)
        tile_layout.setContentsMargins(0, 0, 0, 4)
        tile_layout.setSpacing(2)

        self._tile_zero_imm = self._make_tile(tile_layout, "ZRO IMM", self.LCARS_CYAN)
        self._tile_zero_hr_imm = self._make_tile(tile_layout, "ZHR IMM", self.LCARS_CYAN)
        self._tile_higher_wins = self._make_tile(tile_layout, "HI WINS", self.LCARS_ORANGE)
        self._tile_ant = self._make_tile(tile_layout, "ANT+", self.LCARS_PURPLE)
        self._tile_ble = self._make_tile(tile_layout, "BLE", self.LCARS_BLUE)
        self._tile_cooldown = self._make_tile(tile_layout, "COOL", self.LCARS_GOLD)
        content_layout.addWidget(tile_frame)

        # ───────── TELEMETRIA SOROK ─────────
        self._lbl_power = self._make_row(content_layout, "POWER", "– – –",
                                          self.LCARS_GOLD, self.LCARS_TAN)
        self._power_meter = LCARSMeterWidget(content, self._scale)
        content_layout.addWidget(self._power_meter)
        self._lbl_hr = self._make_row(content_layout, "HEART RATE", "– – –",
                                       self.LCARS_RED, self.LCARS_ORANGE)
        self._hr_meter = LCARSMeterWidget(content, self._scale)
        content_layout.addWidget(self._hr_meter)

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

        # ───────── OPACITY VEZÉRLŐK (a footer felső, ívek közötti sávjába) ─────────
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

        # A csúszka eredeti (beállítás-panel) helyére a STARFLEET felirat kerül
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

        # Footer – az opacity csúszka ide, a felső ívek közötti sávba kerül
        self._footer = LCARSFooterWidget(self, self._font_family, self._scale)
        self._footer.set_opacity_controls(
            self._opacity_label, self._alpha_slider, self._alpha_value
        )
        main_layout.addWidget(self._footer)

        # A tartalom-alapú minimum méret újraszámítása debounce-olva történik:
        # húzás közben nem szabad emelni a minimumot, mert az megakasztja
        # a kicsinyítést és ugráló ablakot okoz
        self._min_size_timer = QTimer(self)
        self._min_size_timer.setSingleShot(True)
        self._min_size_timer.setInterval(200)
        self._min_size_timer.timeout.connect(self._update_min_size)

        # Debounce-olt automatikus geometria-mentés: mozgatás/átméretezés
        # után a pozíció akkor sem vész el, ha a program nem tisztán áll le
        self._geo_save_timer = QTimer(self)
        self._geo_save_timer.setSingleShot(True)
        self._geo_save_timer.setInterval(2500)
        self._geo_save_timer.timeout.connect(self._auto_save_geometry)

        # Az ablak minimális mérete a tartalom olvasható méretéhez igazodik
        self._update_min_size()

        # ───────── KONTEXTUS MENÜ ─────────
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

        # ───────── TIMER ─────────
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update)
        self._timer.start(self.UPDATE_INTERVAL_MS)

        # Innentől a resizeEvent már alkalmazhatja a skálát (a UI teljes)
        self._ui_ready = True

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
                    "LCARS fontok nem találhatók a %s mappában – "
                    "rendszer font használata. Lásd: fonts/README.txt", font_dir
                )
        except Exception as exc:
            logger.warning(
                "LCARS font betöltés sikertelen (rendszer font használata): %s", exc
            )

    def _detect_best_font(self) -> str:
        """Legjobb elérhető LCARS-stílusú font kiválasztása."""
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

    # ────────── UI SEGÉDFÜGGVÉNYEK ──────────

    def _make_row(self, layout: "QVBoxLayout", label: str, value: str,
                  color: str, label_bg: str) -> "QLabel":
        """Telemetria sor LCARS színes label háttérrel."""
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
        # A szövegszín QPalette-ből jön (dinamikus), a stylesheet csak a statikus részt adja
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
        """Állapot csík tile – a háttérszín állapotát a "hudState" dinamikus
        property vezérli ("off"/"on"/"flash"), a színek a stylesheet
        szelektorokban vannak egyszer definiálva."""
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setProperty("hudState", "off")
        # Kikapcsolva: halvány körvonalas pill; bekapcsolva: accent kitöltés
        lbl.setStyleSheet(
            f'QLabel {{ background-color: transparent; color: {self.TEXT_DIM}; '
            f'border: 1px solid {self.BORDER_GLOW}; '
            f'padding: 1px 4px; border-radius: 4px; }}'
            f'QLabel[hudState="on"] {{ background-color: {accent}; '
            f'color: #000a14; border-color: {accent}; }}'
            f'QLabel[hudState="flash"] {{ '
            f'background-color: {self._lighten(accent)}; '
            f'color: #000a14; border-color: {self._lighten(accent)}; }}'
        )
        # Ne vágódjon le a felirata (Minimum), és ne nyomódjon össze (Fixed)
        lbl.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self._register_scalable(lbl, 9)
        layout.addWidget(lbl, 1)
        return lbl

    def _make_status_row(self, layout: "QVBoxLayout", label: str, value: str,
                         label_bg: str) -> "QLabel":
        """Státusz sor LCARS színes label háttérrel."""
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
        # A szövegszín QPalette-ből jön (dinamikus), a stylesheet csak a statikus részt adja
        val_lbl.setStyleSheet(
            f"background-color: {self._VAL_BG}; "
            f"padding: 2px 6px; border-radius: 4px;"
        )
        self._set_label_color(val_lbl, self.TEXT_DIM)
        row_layout.addWidget(val_lbl, 1)
        self._register_scalable(val_lbl, 11, bold=False)

        layout.addWidget(row)
        return val_lbl

    # ────────── ALAP PANEL (lekerekített kártya) ──────────

    def paintEvent(self, event: Any) -> None:
        """A lekerekített alap-panel festése – a sugár a fejléc/lábléc
        corner_r értékével azonos, így az LCARS ívek pontosan a kártya
        kontúrját követik; a sarkokon kívüli terület átlátszó."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = max(12, int(18 * self._scale))
        card = QPainterPath()
        card.addRoundedRect(QRectF(self.rect()), radius, radius)
        p.fillPath(card, _qbrush(self.BG))
        p.end()

    # ────────── DRAG / RESIZE ──────────

    def mousePressEvent(self, event: "QMouseEvent") -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            wh = self.windowHandle()
            if (self.width() - pos.x() < 20) and (self.height() - pos.y() < 20):
                # Húzás idejére az abszolút minimumra engedjük az ablakot,
                # hogy egyetlen mozdulattal is le lehessen kicsinyíteni;
                # a tartalom-alapú minimum a debounce timerrel áll vissza
                self.setMinimumSize(self.MIN_W, self.MIN_H)
                self._min_size_timer.start()
                # Natív (rendszer szintű) átméretezés; kézi fallback, ha a
                # platform nem támogatja
                if wh is None or not wh.startSystemResize(
                    Qt.Edge.RightEdge | Qt.Edge.BottomEdge
                ):
                    self._resize_active = True
                    self._resize_start_pos = event.globalPosition().toPoint()
                    self._resize_start_size = self.size()
            else:
                # Natív ablakmozgatás (Windows snap, élsimítás); kézi fallback
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
        """Skála újraszámítása minden átméretezésnél – natív (startSystemResize)
        átméretezés alatt nem érkezik mouseMoveEvent, ezért itt a helye.

        A skála a szélesség ÉS magasság arányának minimuma, így a betűk
        akkor is elférnek, ha csak a magasságot csökkenti a felhasználó
        (nem csúsznak össze a sorok)."""
        super().resizeEvent(event)
        if not getattr(self, "_ui_ready", False):
            return
        new_scale = min(self.width() / self._base_width,
                        self.height() / self._base_height)
        if abs(new_scale - self._scale) >= 0.001:
            self._scale = new_scale
            self._apply_scale()
        # A tartalom-alapú minimumot csak a húzás megállása után frissítjük
        self._min_size_timer.start()
        self._geo_save_timer.start()

    def moveEvent(self, event: Any) -> None:
        """Mozgatás után debounce-olt geometria-mentést ütemez."""
        super().moveEvent(event)
        if getattr(self, "_ui_ready", False):
            self._geo_save_timer.start()

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

        # ─── LCARS HANG BEÁLLÍTÁSOK ───
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
            if save_hud_settings_only(self._ctrl.settings_file, hud_cfg):
                logger.info("HUD beállítás mentve: hud.%s = %s", key, value)
            elif hud_cfg.save_hud_settings:
                # save_hud_settings=True volt, de valamilyen hiba történt az íráskor
                logger.warning("HUD beállítás nem sikerült menteni: hud.%s = %s",
                               key, value)
            # Ha save_hud_settings=False, nincs log üzenet (szándékos)

    # ────────── LABEL FRISSÍTÉS SEGÉD ──────────

    @staticmethod
    def _set_label_color(lbl: "QLabel", color: str) -> None:
        """Label szövegszín beállítása QPalette-tel – a stylesheet nem tartalmaz
        color tulajdonságot, így a paletta érvényesül és nincs repolish."""
        pal = lbl.palette()
        pal.setColor(QPalette.ColorRole.WindowText, _qcolor(color))
        lbl.setPalette(pal)
        lbl._hud_color = color

    @staticmethod
    def _update_label(lbl: "QLabel", text: str, color: str) -> None:
        """Label szöveg és szín frissítése – csak tényleges változáskor."""
        if getattr(lbl, "_hud_color", None) != color:
            HUDWindow._set_label_color(lbl, color)
        if getattr(lbl, "_hud_text", None) != text:
            lbl.setText(text)
            lbl._hud_text = text

    @staticmethod
    def _set_tile_state(tile: "QLabel", state: str) -> None:
        """Tile állapot frissítése ("off"/"on"/"flash") – a háttérszínt a
        tile stylesheet-jének property-szelektorai adják; property-változáskor
        unpolish/polish érvényesíti az új szabályt."""
        if tile.property("hudState") == state:
            return
        tile.setProperty("hudState", state)
        style = tile.style()
        style.unpolish(tile)
        style.polish(tile)

    @staticmethod
    @lru_cache(maxsize=32)
    def _lighten(color_hex: str, factor: float = 0.35) -> str:
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

                # Power meter – kitöltés az FTP-hez képest, szín a power-zóna
                # küszöbök szerint (a kombinált zónától függetlenül)
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

                # HR meter – kitöltés a nyugalmi és max pulzus között,
                # szín a HR-zóna küszöbök (max_hr százalék) szerint
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

            # BLE fan – villogás OFFLINE/PIN FAIL állapotoknál
            # Monoton számláló: a villogó sorok eltolt FÁZISBAN (nem szinkronban)
            # villognak – period 4 tick (~2 s), 50% kitöltés.
            self._flash_ble_tick += 1
            _ft = self._flash_ble_tick
            flash_white = (_ft + 0) % 4 < 2      # BLE FAN fázis
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
                # Nem hibaállapot – nyugodt, statikus halvány kijelzés
                self._update_label(self._lbl_ble, "DISABLED", self.TEXT_DIM)

            # BLE fan hangeffekt
            if self._prev_ble_status is not None and ble_status != self._prev_ble_status:
                if ble_status == "ONLINE":
                    self._sound.play("sensor_reconnect")
                elif ble_status in ("OFFLINE", "PIN FAIL"):
                    self._sound.play("sensor_dropout")
            self._prev_ble_status = ble_status

            # BLE szenzorok
            ds: DatasourceConfig = settings["datasource"]
            power_ble = ds.power_source == DataSource.BLE
            hr_ble = ds.hr_source == DataSource.BLE
            flash_white = (_ft + 1) % 4 < 2      # BLE SENS fázis

            if not power_ble and not hr_ble:
                self._update_label(self._lbl_ble_sens, "– – –", self.TEXT_DIM)
            else:
                ble = getattr(self._ctrl, "_ble_sensor_handler", None)
                if ble is not None:
                    power_ok = (
                        power_ble
                        and (ble.power_lastdata > 0)
                        and (now - ble.power_lastdata < self._SENSOR_STALE_S)
                    )
                    hr_ok = (
                        hr_ble
                        and (ble.hr_lastdata > 0)
                        and (now - ble.hr_lastdata < self._SENSOR_STALE_S)
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
            flash_white = (_ft + 2) % 4 < 2      # ANT+ fázis

            if not power_ant and not hr_ant:
                self._update_label(self._lbl_ant, "– – –", self.TEXT_DIM)
            elif ant is not None:
                power_ok = (
                    power_ant
                    and (ant.power_lastdata > 0)
                    and (now - ant.power_lastdata < self._SENSOR_STALE_S)
                )
                hr_ok = (
                    hr_ant
                    and (ant.hr_lastdata > 0)
                    and (now - ant.hr_lastdata < self._SENSOR_STALE_S)
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
                self._update_label(self._lbl_ant, "– – –", self.TEXT_DIM)

            # Zwift
            zwift = getattr(self._ctrl, "_zwift_udp", None)
            power_zwift = ds.power_source == DataSource.ZWIFTUDP
            hr_zwift = ds.hr_source == DataSource.ZWIFTUDP
            flash_white = (_ft + 3) % 4 < 2      # ZWIFT fázis

            if zwift is not None and (power_zwift or hr_zwift):
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
                self._update_label(self._lbl_zwift_udp, "– – –", self.TEXT_DIM)

            # Last TX
            if ble_fan is not None and getattr(ble_fan, "last_sent_time", 0) > 0:
                cur_sent_time = ble_fan.last_sent_time
                ago = now - cur_sent_time
                self._update_label(self._lbl_last_sent, f"{ago:.0f}s AGO", self.LCARS_TAN)

                # Fan TX hangeffekt – csak ha új parancs ment ki
                if cur_sent_time != self._prev_last_sent_time and self._prev_last_sent_time > 0:
                    self._sound.play("fan_tx")
                self._prev_last_sent_time = cur_sent_time
            else:
                self._update_label(self._lbl_last_sent, "– – –", self.TEXT_DIM)

            # Cooldown – a snapshot-ot a tile frissítése is újrahasznosítja
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

            # ── Állapot csík frissítése (aktív = villogó háttér) ──
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

            # ── ZwiftApp.exe process figyelés (~10s-onként) ──
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
        self._zone_bar.set_scale(s)
        self._power_meter.set_scale(s)
        self._hr_meter.set_scale(s)
        # A szöveges label-ek betűmérete és fix szélessége is skálázódik
        for lbl, base_pt, base_fw, bold in self._scalable_texts:
            self._apply_label_scale(lbl, base_pt, base_fw, bold)
        # A minimum méret frissítését NEM itt végezzük: élő átméretezés alatt
        # a növekvő minimum visszalökné az ablakot (ugrálás); a debounce
        # timer hívja az _update_min_size-t, amikor a húzás megállt

    def _register_scalable(self, lbl: "QLabel", base_pt: int,
                           base_fw: int | None = None, bold: bool = True) -> None:
        """Label regisztrálása skálázáshoz (alap pt-méret + opcionális fix szélesség),
        és azonnali beállítás az aktuális skálára."""
        self._scalable_texts.append((lbl, base_pt, base_fw, bold))
        self._apply_label_scale(lbl, base_pt, base_fw, bold)

    def _apply_label_scale(self, lbl: "QLabel", base_pt: int,
                           base_fw: int | None, bold: bool) -> None:
        """A teljes fontot (család, méret, vastagság) setFont-tal állítjuk – a
        stíluslapban nincs font-* tulajdonság, így nem írja felül. A fix
        szélesség is skálázódik."""
        s = self._scale
        f = QFont(self._font_family, max(6, int(base_pt * s)))
        f.setBold(bold)
        lbl.setFont(f)
        if base_fw is not None:
            lbl.setFixedWidth(max(1, int(base_fw * s)))

    def _update_min_size(self) -> None:
        """Az ablak minimális méretét a tartalom természetes (olvasható) méretéből
        számolja, így egérrel átméretezve a sorok/tile-ok nem nyomhatók össze.
        Csak nyugalmi állapotban fut (debounce), élő húzás alatt soha."""
        lay = self.layout()
        if lay is not None:
            lay.activate()
        hint = self.minimumSizeHint()
        self.setMinimumSize(max(self.MIN_W, hint.width()),
                            max(self.MIN_H, hint.height()))

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
        # Az abszolút padlóhoz (MIN_W/MIN_H) igazítunk, NEM az aktuális
        # minimumhoz: induláskor a minimum még az 1.0-s skálájú tartalomhoz
        # van kiszámolva, ami a mentett kis méretet felfelé kerekítené
        w = max(self.MIN_W, min(rect["w"], sg.width()))
        h = max(self.MIN_H, min(rect["h"], sg.height()))
        # A setGeometry-t a Qt az érvényes minimumra vágná – előtte a padlóra
        # engedjük; a helyes (mentett skálájú) tartalom-minimumot a resizeEvent
        # által indított debounce timer számolja újra
        self.setMinimumSize(self.MIN_W, self.MIN_H)
        # A skálát a resizeEvent alkalmazza a setGeometry hatására
        self.setGeometry(x, y, w, h)

    def _store_geometry_in_cfg(self) -> "HudConfig | None":
        """Az aktuális geometria beírása a hud configba (fájlmentés nélkül).

        A kulcsot újra-beszúrás előtt eltávolítjuk, így a dict VÉGÉRE kerül:
        a visszaállítás az utolsó kulcsot tekinti az utoljára használt
        monitornak, de egy meglévő kulcs sima frissítése nem vinné hátra
        (Python dict a beszúrási sorrendet őrzi, nem a frissítésit)."""
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
        """Elmenti az ablak pozícióját/méretét az aktuális monitorhoz."""
        hud_cfg = self._store_geometry_in_cfg()
        if hud_cfg is not None:
            self._save_hud_setting("window_geometry", hud_cfg.window_geometry)

    def _auto_save_geometry(self) -> None:
        """Debounce-olt automatikus geometria-mentés mozgatás/átméretezés után.

        Csendes (nem ír log-sort minden mozgatásnál); a save_hud_settings
        flaget a save_hud_settings_only maga ellenőrzi. Így a pozíció
        váratlan leállás (crash/áramszünet) után sem vész el."""
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

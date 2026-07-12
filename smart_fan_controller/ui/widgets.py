"""Custom-painted LCARS widgets used by the HUD window.

  - LCARSHeaderWidget: top bar with title and version badge
  - LCARSFooterWidget: bottom bar hosting the opacity controls
  - LCARSSidebarWidget: colored segment strip on the left edge
  - LCARSZoneBarWidget: segmented zone indicator (STANDBY..ZONE 3)
  - LCARSMeterWidget: thin rounded fill bar (power / heart-rate)

All widgets paint on a transparent background: the rounded base card is
painted by :class:`~smart_fan_controller.ui.window.HUDWindow`, which keeps
the window corners genuinely transparent (floating-card look).
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import QHBoxLayout, QWidget

from smart_fan_controller import __version__
from smart_fan_controller.ui import theme
from smart_fan_controller.ui.theme import qbrush, qcolor


class LCARSHeaderWidget(QWidget):
    """Top LCARS bar – QPainter-drawn orange sweep with title and badge."""

    def __init__(self, parent: QWidget, font_family: str, scale: float = 1.0) -> None:
        super().__init__(parent)
        self._font_family = font_family
        self._scale = scale
        self._path_cache_key: tuple[int, float, int] | None = None
        self._path_cache: QPainterPath = QPainterPath()
        self.setFixedHeight(50)
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
            # Main orange sweep with rounded top-left AND top-right corners
            # (the top-right arc follows the window rounding so the bar
            # cannot poke into the transparent corner)
            rr = min(corner_r, bar_h)
            path = QPainterPath()
            path.moveTo(corner_r, 0)
            path.lineTo(w - 6 - rr, 0)
            path.arcTo(QRectF(w - 6 - 2 * rr, 0, 2 * rr, 2 * rr), 90, -90)
            path.lineTo(w - 6, bar_h)
            # Inner elbow arc – center (sw+R, bar_h+R), 90°→180°
            path.arcTo(QRectF(sw, bar_h, 2 * R, 2 * R), 90, 90)
            path.lineTo(sw, ch)
            path.lineTo(0, ch)
            path.lineTo(0, corner_r)
            path.arcTo(QRectF(0, 0, 2 * corner_r, 2 * corner_r), 180, -90)
            path.closeSubpath()

            self._path_cache = path
            self._path_cache_key = cache_key

        p.fillPath(self._path_cache, qbrush(theme.LCARS_ORANGE))

        # Title text
        title_size = max(8, int(12 * s))
        p.setFont(QFont(self._font_family, title_size, QFont.Weight.Bold))
        p.setPen(qcolor(theme.LCARS_CYAN))
        p.drawText(QRectF(sw + R, bar_h, w - 6 - sw - R, ch - bar_h),
                    Qt.AlignmentFlag.AlignCenter, "ZWIFT FAN CTRL")

        # Version badge (rounded magenta pill)
        badge_w = max(40, int(62 * s))
        badge_rect = QRectF(w - badge_w - 8, 1, badge_w, bar_h - 3)
        badge_path = QPainterPath()
        badge_path.addRoundedRect(badge_rect, (bar_h - 3) / 2, (bar_h - 3) / 2)
        p.fillPath(badge_path, qbrush(theme.LCARS_MAGENTA))
        ver_size = max(6, int(7 * s))
        p.setFont(QFont(self._font_family, ver_size))
        p.setPen(qcolor("#FFFFFF"))
        p.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, f"v{__version__}")

        p.end()


class LCARSFooterWidget(QWidget):
    """Bottom LCARS bar – QPainter-drawn sweep hosting the opacity slider."""

    def __init__(self, parent: QWidget, font_family: str, scale: float = 1.0) -> None:
        super().__init__(parent)
        self._font_family = font_family
        self._scale = scale
        self._path_cache_key: tuple[int, float, int] | None = None
        self._path_cache: tuple[Any, ...] = ()
        self._opacity_tw_cache: tuple[int, int] | None = None  # (font size, width)
        self.setFixedHeight(60)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background-color: transparent;")
        # The opacity controls live in the upper strip between the elbows
        self._overlay = QHBoxLayout(self)
        self._overlay.setSpacing(4)
        self._update_overlay_margins()

    def _bar_metrics(self) -> tuple[int, int, int]:
        # Same thickness/width/radius as the header (mirror image)
        s = self._scale
        sw = max(10, int(20 * s))
        R = max(14, int(26 * s))
        bar_h = max(18, int(25 * s))
        return sw, R, bar_h

    def _update_overlay_margins(self) -> None:
        # The slider starts after the painted OPACITY box, vertically aligned
        # with it (top/bottom); on the right it follows the elbow arc.
        sw, R, bar_h = self._bar_metrics()
        box_top, box_bottom, box_right = self._opacity_box()
        fh = self.height()
        gap_s = max(6, int(8 * self._scale))
        self._overlay.setContentsMargins(
            box_right + gap_s, box_top, sw + R, fh - box_bottom
        )

    def _opacity_box(self) -> tuple[int, int, int]:
        """The OPACITY box as (box_top, box_bottom, box_right).

        Its arc is CONCENTRIC with the blue elbow arc (same center, radius
        R-6), so the gap is a uniform 6px; the box top aligns with the top
        of the elbow arc and its left edge with the status rows (sw+6).
        """
        sw, R, bar_h = self._bar_metrics()
        fh = self.height()
        bar_top = fh - bar_h
        box_top = bar_top - R                    # aligned with the elbow arc top
        box_bottom = bar_top - 6                 # uniform 6px gap above the bar
        fs = max(7, int(9 * self._scale))
        if self._opacity_tw_cache is None or self._opacity_tw_cache[0] != fs:
            fm = QFontMetrics(QFont(self._font_family, fs, QFont.Weight.Bold))
            self._opacity_tw_cache = (fs, fm.horizontalAdvance("OPACITY"))
        text_w = self._opacity_tw_cache[1]
        pad = max(6, int(8 * self._scale))
        box_right = sw + R + text_w + 2 * pad    # text on the flat part past the arc
        return box_top, box_bottom, box_right

    def set_opacity_controls(self, label: QWidget, slider: QWidget,
                             value: QWidget) -> None:
        """Place the opacity controls into the footer's upper strip.

        The OPACITY caption is painted by paintEvent (with the curved box),
        so the label widget itself is hidden.
        """
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

        # Clip all footer painting to the window card's rounded BOTTOM
        # corners so the stacked bars can never poke square corners out
        # from behind the card's arc. The clip rect extends corner_r above
        # the top edge to keep the top corners unclipped (square).
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, -corner_r, w, fh + corner_r),
                            corner_r, corner_r)
        p.setClipPath(clip)

        cache_key = (w, s, fh)
        if self._path_cache_key != cache_key:
            # Segments – purple in the middle, blue base bar to its left,
            # tan to the right, starting right after the purple (no sliver).
            right_edge = w - 6                       # right end of the bar
            flat_left = sw + R                       # end of the left arc
            flat_right = right_edge - sw - R         # start of the right arc
            center = (flat_left + flat_right) / 2    # = right_edge / 2
            purple_w = max(1, int((flat_right - flat_left) / 3))
            purple_left = int(center - purple_w / 2)
            tan_left = purple_left + purple_w        # tan right after purple

            # Main blue bar with elbow arc + rounded bottom-left corner.
            # The bar only runs up to the start of the tan segment (purple
            # covers its end), so nothing blue can stick out from behind
            # the tan segment's rounded bottom-right corner.
            path = QPainterPath()
            path.moveTo(0, 0)
            path.lineTo(sw, 0)
            # Inner elbow arc – center (sw+R, bar_top-R), 180°→270°
            path.arcTo(QRectF(sw, bar_top - 2 * R, 2 * R, 2 * R), 180, 90)
            path.lineTo(tan_left, bar_top)
            path.lineTo(tan_left, fh)
            path.lineTo(corner_r, fh)
            path.arcTo(QRectF(0, fh - 2 * corner_r, 2 * corner_r, 2 * corner_r),
                       270, -90)
            path.lineTo(0, 0)
            path.closeSubpath()

            # Main tan bar with elbow arc + rounded bottom-right corner
            # – exact mirror of the blue corner (x' = right_edge - x)
            rpath = QPainterPath()
            rpath.moveTo(right_edge, 0)
            rpath.lineTo(right_edge - sw, 0)
            # Inner elbow arc – center (right_edge-sw-R, bar_top-R), 0°→-90°
            rpath.arcTo(QRectF(right_edge - sw - 2 * R, bar_top - 2 * R,
                               2 * R, 2 * R), 0, -90)
            rpath.lineTo(tan_left, bar_top)
            rpath.lineTo(tan_left, fh)
            rpath.lineTo(right_edge - corner_r, fh)
            rpath.arcTo(QRectF(right_edge - 2 * corner_r, fh - 2 * corner_r,
                                2 * corner_r, 2 * corner_r), 270, 90)
            rpath.closeSubpath()

            # Yellow OPACITY box – its left arc is CONCENTRIC with the blue
            # elbow arc (same center, radius R-6) for a uniform 6px gap; top
            # aligns with the elbow top, left edge with the status rows.
            box_top, box_bottom, box_right = self._opacity_box()
            gx = sw + 6                                 # left edge = status rows' left
            rr = R - 6                                  # concentric arc radius
            box_r = max(3, int(4 * s))                  # right corner rounding

            obox = QPainterPath()
            obox.moveTo(gx, box_top)                    # top-left (top of the arc)
            obox.lineTo(box_right - box_r, box_top)     # top edge
            obox.arcTo(QRectF(box_right - 2 * box_r, box_top,
                              2 * box_r, 2 * box_r), 90, -90)
            obox.lineTo(box_right, box_bottom - box_r)  # right edge
            obox.arcTo(QRectF(box_right - 2 * box_r, box_bottom - 2 * box_r,
                              2 * box_r, 2 * box_r), 0, -90)
            obox.lineTo(sw + R, box_bottom)             # bottom edge to the arc
            # Concentric arc (270°→180°) – center (sw+R, bar_top-R), radius rr
            obox.arcTo(QRectF(sw + R - rr, bar_top - R - rr, 2 * rr, 2 * rr),
                       270, -90)
            obox.lineTo(gx, box_top)                    # close
            obox.closeSubpath()

            self._path_cache = (
                path, rpath, obox,
                purple_left, purple_w, box_top, box_bottom, box_right,
            )
            self._path_cache_key = cache_key

        (path, rpath, obox,
         purple_left, purple_w, box_top, box_bottom, box_right) = self._path_cache

        p.fillPath(path, qbrush(theme.LCARS_BLUE))
        p.fillRect(purple_left, bar_top, purple_w, bar_h,
                    qcolor(theme.LCARS_PURPLE))
        p.fillPath(rpath, qbrush(theme.LCARS_TAN))
        p.fillPath(obox, qbrush(theme.LCARS_GOLD))

        # OPACITY caption on the flat part of the box, centered
        p.setFont(QFont(self._font_family, fs, QFont.Weight.Bold))
        p.setPen(qcolor("#000a14"))
        p.drawText(QRectF(sw + R, box_top, box_right - (sw + R), box_bottom - box_top),
                    Qt.AlignmentFlag.AlignCenter, "OPACITY")

        p.end()


class LCARSSidebarWidget(QWidget):
    """Left LCARS sidebar – strip of colored segments."""

    COLORS = theme.SIDEBAR_COLORS

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
            p.fillRect(0, y + gap, sw, bottom - gap - y - gap, qcolor(c))
        p.end()


class LCARSZoneBarWidget(QWidget):
    """Segmented zone bar – modern display of the 4 zones (STANDBY..ZONE 3).

    Segments light up in the current zone's color up to the active zone
    (signal-strength style); without a zone only the dim track is shown.
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
                color = theme.ZONE_COLORS.get(self._zone, theme.LCARS_CYAN)
            else:
                color = theme.VAL_BG
            seg = QPainterPath()
            seg.addRoundedRect(QRectF(x, 0, seg_w, h), r, r)
            p.fillPath(seg, qbrush(color))
        p.end()


class LCARSMeterWidget(QWidget):
    """Thin rounded fill bar (power / heart-rate visualization)."""

    def __init__(self, parent: QWidget, scale: float = 1.0) -> None:
        super().__init__(parent)
        self._scale = scale
        self._fraction: float | None = None
        self._color: str = theme.TEXT_DIM
        self.setFixedHeight(max(4, int(5 * scale)))
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background-color: transparent;")

    def set_scale(self, s: float) -> None:
        self._scale = s
        self.setFixedHeight(max(4, int(5 * s)))
        self.update()

    def set_value(self, fraction: float | None, color: str) -> None:
        """Update the fill – fraction: 0.0–1.0 or None (empty track)."""
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
        p.fillPath(track, qbrush(theme.VAL_BG))
        if self._fraction is not None and self._fraction > 0:
            fill_w = max(float(h), w * self._fraction)
            fill = QPainterPath()
            fill.addRoundedRect(QRectF(0, 0, fill_w, h), r, r)
            p.fillPath(fill, qbrush(self._color))
        p.end()

"""LCARS color palette and small paint helpers shared by the HUD widgets.

Pure constants plus a few cached QColor/QBrush factories – no widget code
here, so both :mod:`smart_fan_controller.ui.widgets` and
:mod:`smart_fan_controller.ui.window` can import it without cycles.
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtGui import QBrush, QColor

# ─── LCARS palette ───
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
VAL_BG = "#001828"

# Fan zone → accent color (0 = standby)
ZONE_COLORS: dict[int, str] = {
    0: "#556688",
    1: "#00CCFF",
    2: "#FF9900",
    3: "#FF3333",
}
ZONE_NAMES: dict[int, str] = {0: "STANDBY", 1: "ZONE 1", 2: "ZONE 2", 3: "ZONE 3"}

# Left sidebar segment colors, top to bottom
SIDEBAR_COLORS: list[str] = [
    "#FF9900", "#FFCC66", "#5599FF", "#CC6699", "#9977CC", "#FFAA66",
]


@lru_cache(maxsize=64)
def qcolor(spec: str) -> QColor:
    """Cached QColor – the returned instance must never be mutated."""
    return QColor(spec)


@lru_cache(maxsize=64)
def qbrush(spec: str) -> QBrush:
    """Cached QBrush – the returned instance must never be mutated."""
    return QBrush(QColor(spec))


@lru_cache(maxsize=32)
def lighten(color_hex: str, factor: float = 0.35) -> str:
    """Blend a ``#RRGGBB`` color towards white (0 = original, 1 = white)."""
    r = int(color_hex[1:3], 16)
    g = int(color_hex[3:5], 16)
    b = int(color_hex[5:7], 16)
    r = int(r + (255 - r) * factor)
    g = int(g + (255 - g) * factor)
    b = int(b + (255 - b) * factor)
    return f"#{r:02X}{g:02X}{b:02X}"

"""UI components for Smart Fan Controller – LCARS HUD interface.

This package contains the visual components for displaying telemetry
and status information in a Star Trek LCARS-style interface.
"""

from smart_fan_controller.ui.hud import (
    HUDWindow,
    LCARSHeaderWidget,
    LCARSFooterWidget,
    LCARSSidebarWidget,
    LCARSSoundManager,
)

__all__ = [
    "HUDWindow",
    "LCARSHeaderWidget",
    "LCARSFooterWidget",
    "LCARSSidebarWidget",
    "LCARSSoundManager",
]

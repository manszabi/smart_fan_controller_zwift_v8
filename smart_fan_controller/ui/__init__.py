"""UI components for Smart Fan Controller – LCARS HUD interface.

This package contains the visual components for displaying telemetry
and status information in a Star Trek LCARS-style interface:

  - ``theme``   – LCARS palette and cached paint helpers
  - ``widgets`` – custom-painted LCARS widgets
  - ``sound``   – file-based LCARS sound effects
  - ``window``  – the main floating HUDWindow
  - ``hud``     – backwards-compatible aggregator of the names above
"""

from smart_fan_controller.ui.sound import LCARSSoundManager
from smart_fan_controller.ui.widgets import (
    LCARSFooterWidget,
    LCARSHeaderWidget,
    LCARSMeterWidget,
    LCARSSidebarWidget,
    LCARSZoneBarWidget,
)
from smart_fan_controller.ui.window import HUDWindow

__all__ = [
    "HUDWindow",
    "LCARSFooterWidget",
    "LCARSHeaderWidget",
    "LCARSMeterWidget",
    "LCARSSidebarWidget",
    "LCARSSoundManager",
    "LCARSZoneBarWidget",
]

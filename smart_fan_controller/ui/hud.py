"""Backwards-compatible aggregator for the LCARS HUD components.

The former monolithic ``hud.py`` was split into focused modules:

  - :mod:`smart_fan_controller.ui.theme`   – LCARS palette and paint helpers
  - :mod:`smart_fan_controller.ui.widgets` – custom-painted LCARS widgets
  - :mod:`smart_fan_controller.ui.sound`   – file-based sound effects
  - :mod:`smart_fan_controller.ui.window`  – the HUDWindow itself

This module only re-exports the public names so existing imports
(``from smart_fan_controller.ui.hud import HUDWindow``) keep working.
Prefer importing from the specific modules in new code.
"""

from __future__ import annotations

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

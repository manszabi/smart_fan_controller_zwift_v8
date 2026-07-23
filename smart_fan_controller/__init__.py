"""Smart Fan Controller – modular package.

The full logic of the former monolithic entry point
(``zwift_fan_controller.py``) was organized into this package.
Sub-packages: ``config`` (settings models and loader), ``core`` (pure
domain logic), ``handlers`` (ANT+/BLE/Zwift UDP data handlers),
``processors`` (async processing tasks), ``ui`` (PySide6 HUD),
``zwift_api`` (Zwift HTTPS API polling), plus the ``controller``
(orchestrator) and ``app`` (entry point) modules.
``zwift_fan_controller.py`` is now only a thin entry calling
``app.main()``.
"""
from __future__ import annotations

__version__ = "8.1.1"

"""Smart Fan Controller – moduláris package.

A korábbi monolitikus belépő (``swift_fan_controller.py``) teljes logikája
ebbe a package-be szerveződött. Al-package-ek: ``config`` (beállítás-modellek
és -betöltő), ``core`` (tiszta domain-logika), ``handlers`` (ANT+/BLE/Zwift UDP
adatkezelők), ``processors`` (async feldolgozó task-ok), ``ui`` (PySide6 HUD),
``zwift_api`` (Zwift HTTPS API polling), valamint a ``controller`` (orchestrátor)
és az ``app`` (belépőpont) modulok. A ``swift_fan_controller.py`` már csak egy
vékony belépő, ami az ``app.main()``-t hívja.
"""
from __future__ import annotations

__version__ = "8.1.0"

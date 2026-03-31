"""Pytest conftest – PySide6 / bleak / openant stub-ok a headless teszteléshez."""
from __future__ import annotations

import sys
import types


def _ensure_fake_module(name: str, attrs: dict | None = None) -> types.ModuleType:
    """Csak akkor hoz létre fake modult, ha az eredeti nem elérhető."""
    if name in sys.modules and not isinstance(sys.modules[name], types.ModuleType):
        pass  # already there, fine
    if name not in sys.modules:
        mod = types.ModuleType(name)
        if attrs:
            for k, v in attrs.items():
                setattr(mod, k, v)
        sys.modules[name] = mod
    else:
        mod = sys.modules[name]
        if attrs:
            for k, v in attrs.items():
                if not hasattr(mod, k):
                    setattr(mod, k, v)
    return mod


# ---------------------------------------------------------------------------
# Minimal Qt stubs – elég hogy a class Foo(QWidget) ne dobjon TypeError
# ---------------------------------------------------------------------------

class _StubMeta(type):
    """Meta ami bármilyen __init_subclass__ hívást elnyel."""
    def __init_subclass__(cls, **kw):
        pass

class _Stub(metaclass=_StubMeta):
    def __init__(self, *a, **kw):
        pass
    def __init_subclass__(cls, **kw):
        pass

class _StubSignal:
    def __init__(self, *a, **kw):
        pass
    def __get__(self, obj, objtype=None):
        return self
    def connect(self, *a):
        pass
    def disconnect(self, *a):
        pass
    def emit(self, *a):
        pass

class _StubQt:
    """Fake Qt namespace – minden attribútum 0-t ad vissza."""
    def __getattr__(self, name):
        return 0

class _StubQUrl:
    @staticmethod
    def fromLocalFile(*a):
        return _StubQUrl()

# Build module hierarchy
_ensure_fake_module("PySide6")
_ensure_fake_module("PySide6.QtCore", {
    "Qt": _StubQt(),
    "QObject": _Stub,
    "Signal": _StubSignal,
    "QTimer": _Stub,
    "QThread": _Stub,
    "QUrl": _StubQUrl,
    "QPoint": _Stub,
    "QSize": _Stub,
    "QRectF": _Stub,
    "QEvent": _Stub,
    "QPropertyAnimation": _Stub,
})
_ensure_fake_module("PySide6.QtWidgets", {
    "QApplication": _Stub,
    "QWidget": _Stub,
    "QLabel": _Stub,
    "QHBoxLayout": _Stub,
    "QVBoxLayout": _Stub,
    "QSlider": _Stub,
    "QMenu": _Stub,
    "QFrame": _Stub,
    "QSizePolicy": _Stub,
    "QSystemTrayIcon": _Stub,
    "QAction": _Stub,
    "QGraphicsDropShadowEffect": _Stub,
    "QGraphicsOpacityEffect": _Stub,
    "QGridLayout": _Stub,
})
_ensure_fake_module("PySide6.QtGui", {
    "QColor": _Stub,
    "QPainter": _Stub,
    "QBrush": _Stub,
    "QFont": _Stub,
    "QFontDatabase": _Stub,
    "QPainterPath": _Stub,
    "QMouseEvent": _Stub,
    "QPixmap": _Stub,
    "QIcon": _Stub,
    "QPen": _Stub,
    "QLinearGradient": _Stub,
    "QRadialGradient": _Stub,
    "QFontMetrics": _Stub,
    "QCursor": _Stub,
})
_ensure_fake_module("PySide6.QtMultimedia", {
    "QSoundEffect": _Stub,
})

# bleak stub
_ensure_fake_module("bleak", {
    "BleakClient": _Stub,
    "BleakScanner": _Stub,
})

# openant stubs
_ensure_fake_module("openant")
_ensure_fake_module("openant.easy")
_ensure_fake_module("openant.easy.node", {"Node": _Stub})
_ensure_fake_module("openant.devices", {"ANTPLUS_NETWORK_KEY": b"\x00" * 8})
_ensure_fake_module("openant.devices.power_meter", {"PowerMeter": _Stub, "PowerData": _Stub})
_ensure_fake_module("openant.devices.heart_rate", {"HeartRate": _Stub, "HeartRateData": _Stub})

# pywinauto stub
_ensure_fake_module("pywinauto", {"Application": _Stub})

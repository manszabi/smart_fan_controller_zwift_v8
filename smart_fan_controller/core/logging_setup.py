"""Logging infrastruktúra: konzol, rotált fájl, korai pufferelés.

Biztosítja a két logger (user_logger, logger) konzisztens konfigurációját:
- Konzol: user_logger csak üzenetek, logger WARNING+ debug info
- Fájl: mindkettő DEBUG+
- Headless mód: logging:false → NullHandler (némaság)
- Korai logging: settings betöltés előtti logok pufferelése
"""

import logging
import os
import sys
from logging.handlers import MemoryHandler, RotatingFileHandler
from typing import Optional

from smart_fan_controller.core.helpers import resolve_log_dir

__all__ = [
    "logger",
    "user_logger",
    "setup_logging",
    "setup_early_logging",
    "flush_early_logging",
    "discard_early_logging",
    "is_logging_enabled",
]

logger = logging.getLogger("swift_fan_controller_new")
user_logger = logging.getLogger("user")

# Modul-szintű state
_log_dir: str = os.path.dirname(os.path.abspath(__file__))
_logging_enabled: bool = True
_early_mem_handlers: list = []


def setup_logging(log_directory: Optional[str] = None, logging_enabled: bool = True) -> None:
    """Logging konfiguráció: konzol + rotált fájl (500 KB max).

    Két logger:
      - ``user_logger``: Felhasználói üzenetek (konzolra + fájlba).
        Konzolra tiszta formátum (csak az üzenet), fájlba időbélyeggel.
      - ``logger``: Belső debug/info logok (fájlba mindig, konzolra WARNING+ felett).

    A log fájlok a ``log_directory``-ba kerülnek (ha érvényes), különben
    a program indítási könyvtárába.

    Ha ``logging_enabled`` False, mindkét logger NullHandler-t kap (teljes
    némaság – se fájl, se konzol). A program-indítási összefoglaló ettől
    függetlenül megjelenik (``print_startup_info`` print()-re vált).

    Többszöri hívás biztonságos: a korábbi handler-eket eltávolítja.

    Args:
        log_directory: Log fájlok könyvtára (None = alapértelmezett).
        logging_enabled: Ha False, minden loggolás kikapcsol.
    """
    global _log_dir, _logging_enabled
    _logging_enabled = logging_enabled

    # Loggolás kikapcsolva → mindkét logger elnémítása NullHandler-rel
    if not logging_enabled:
        for name in ("user", "swift_fan_controller_new"):
            lg = logging.getLogger(name)
            lg.handlers.clear()
            lg.addHandler(logging.NullHandler())
            lg.propagate = False
        logging.getLogger("bleak").setLevel(logging.CRITICAL)
        logging.getLogger("openant").setLevel(logging.CRITICAL)
        return

    _log_dir = resolve_log_dir(
        log_directory, default_dir=os.path.dirname(os.path.abspath(__file__))
    )
    log_file = os.path.join(_log_dir, "smart_fan_controller.log")

    file_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_file, maxBytes=500 * 1024, backupCount=2, encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_fmt)

    # ── user_logger: felhasználói üzenetek ──
    ul = logging.getLogger("user")
    ul.handlers.clear()  # Korábbi handler-ek törlése (újrahívás esetén)
    ul.setLevel(logging.DEBUG)
    ul.propagate = False

    console_user = logging.StreamHandler(sys.stdout)
    console_user.setLevel(logging.DEBUG)
    console_user.setFormatter(logging.Formatter("%(message)s"))
    ul.addHandler(console_user)
    ul.addHandler(file_handler)

    # ── logger: belső logok ──
    il = logging.getLogger("swift_fan_controller_new")
    il.handlers.clear()
    il.setLevel(logging.DEBUG)
    il.propagate = False

    console_internal = logging.StreamHandler(sys.stderr)
    console_internal.setLevel(logging.WARNING)
    console_internal.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S",
    ))
    il.addHandler(console_internal)
    il.addHandler(file_handler)

    # Zajos külső könyvtárak elnémítása
    logging.getLogger("bleak").setLevel(logging.CRITICAL)
    logging.getLogger("openant").setLevel(logging.CRITICAL)


def setup_early_logging() -> None:
    """Korai loggolás: a settings betöltése ELŐTTI logokat memóriába puffereli.

    Mivel a ``global_settings.logging`` flag csak a settings betöltése után
    ismert, a korai logokat (pl. config validációs warningok) memóriában
    tartjuk. A flag ismeretében később vagy visszajátsszuk a valódi
    handlerekre (``flush_early_logging``), vagy eldobjuk
    (``discard_early_logging``). Így ``logging: false`` esetén nem jön létre
    fölösleges log fájl, ``logging: true`` esetén pedig a korai warningok sem
    vesznek el.
    """
    global _early_mem_handlers
    _early_mem_handlers = []
    for name in ("user", "swift_fan_controller_new"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        # Nagy kapacitás + magas flushLevel → nem ürül ki automatikusan
        mh = MemoryHandler(capacity=100000, flushLevel=logging.CRITICAL + 10)
        lg.addHandler(mh)
        _early_mem_handlers.append((lg, mh))
    logging.getLogger("bleak").setLevel(logging.CRITICAL)
    logging.getLogger("openant").setLevel(logging.CRITICAL)


def flush_early_logging() -> None:
    """A pufferelt korai logokat visszajátssza a már beállított handlerekre."""
    global _early_mem_handlers
    for lg, mh in _early_mem_handlers:
        for record in mh.buffer:
            lg.handle(record)
        mh.close()
    _early_mem_handlers = []


def discard_early_logging() -> None:
    """A pufferelt korai logokat eldobja (logging: false eset)."""
    global _early_mem_handlers
    for _lg, mh in _early_mem_handlers:
        mh.buffer.clear()
        mh.close()
    _early_mem_handlers = []


def is_logging_enabled() -> bool:
    """Visszaadja, hogy a loggolás engedélyezve van-e (setup_logging állítja).

    A ``print_startup_info`` használja: ha a loggolás ki van kapcsolva,
    ``print()``-re vált, hogy a startup összefoglaló akkor is megjelenjen.
    """
    return _logging_enabled

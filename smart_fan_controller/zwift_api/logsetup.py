"""A zwift_api segédprocessz saját loggolása (külön zwift_api_polling.log fájl).

A subprocess külön processzben fut, ezért saját loggert és log fájlt használ,
hogy ne ütközzön a fő app fájlírásával. A loggolást a settings.json
``global_settings.logging`` / ``log_directory`` mezői vezérlik – a fő app
beállításaival egységesen.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

log = logging.getLogger("zwift_api_polling")

# A program indítási könyvtára (frozen exe vagy a settings.json mappája).
_base_dir: str = (
    os.path.dirname(os.path.abspath(sys.executable))
    if getattr(sys, "frozen", False)
    else os.getcwd()
)

# A feloldott log könyvtár (a setup_logging állítja be)
_log_dir: str = _base_dir
# A korai (settings betöltés előtti) logokat pufferelő handler
_early_mem_handler: Any = None


def set_base_dir(path: str) -> None:
    """Beállítja a log fájlok alapértelmezett könyvtárát (fallback)."""
    global _base_dir, _log_dir
    _base_dir = path
    _log_dir = path


def resolve_log_dir(log_directory: str | None) -> str:
    """Log könyvtár meghatározása és validálása (fallback: a base könyvtár)."""
    if not log_directory:
        return _base_dir
    resolved = os.path.abspath(os.path.expanduser(log_directory))
    try:
        os.makedirs(resolved, exist_ok=True)
        test_file = os.path.join(resolved, ".log_write_test")
        with open(test_file, "w", encoding="utf-8") as fh:
            fh.write("test")
        os.remove(test_file)
        return resolved
    except OSError:
        log.warning(
            f"⚠️  log_directory nem elérhető / not accessible: '{resolved}', "
            f"alapértelmezett / default: '{_base_dir}'"
        )
        return _base_dir


def setup_early_logging() -> None:
    """A settings betöltése ELŐTTI logokat memóriába puffereli.

    A 'logging' flag csak a settings betöltése után ismert, ezért a korai
    logokat (pl. validációs warningok) memóriában tartjuk, majd a flag
    ismeretében visszajátsszuk (flush_early_logging) vagy eldobjuk
    (discard_early_logging).
    """
    from logging.handlers import MemoryHandler

    global _early_mem_handler
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    log.propagate = False
    mh = MemoryHandler(capacity=100000, flushLevel=logging.CRITICAL + 10)
    log.addHandler(mh)
    _early_mem_handler = mh


def setup_logging(
    log_directory: str | None = None,
    enabled: bool = True,
    debug: bool = False,
) -> None:
    """Loggolás konfigurálása: konzol + rotált fájl (zwift_api_polling.log).

    Ha ``enabled`` False → NullHandler (teljes némaság, nincs fájl).
    """
    from logging.handlers import RotatingFileHandler

    global _log_dir
    log.handlers.clear()
    log.propagate = False

    if not enabled:
        log.addHandler(logging.NullHandler())
        return

    level = logging.DEBUG if debug else logging.INFO
    log.setLevel(level)

    # Konzol (tiszta formátum – csak az üzenet)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(console)

    # Rotált fájl (időbélyeggel)
    _log_dir = resolve_log_dir(log_directory)
    file_handler = RotatingFileHandler(
        os.path.join(_log_dir, "zwift_api_polling.log"),
        maxBytes=500 * 1024, backupCount=2, encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.addHandler(file_handler)


def flush_early_logging() -> None:
    """A pufferelt korai logokat visszajátssza a már beállított handlerekre."""
    global _early_mem_handler
    if _early_mem_handler is not None:
        for record in _early_mem_handler.buffer:
            log.handle(record)
        _early_mem_handler.close()
        _early_mem_handler = None


def discard_early_logging() -> None:
    """A pufferelt korai logokat eldobja (logging: false eset)."""
    global _early_mem_handler
    if _early_mem_handler is not None:
        _early_mem_handler.buffer.clear()
        _early_mem_handler.close()
        _early_mem_handler = None

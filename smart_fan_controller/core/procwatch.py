"""Windows process-existence check without spawning a helper process.

Both the HUD (``ZwiftApp.exe`` watch) and the ``zwift_api`` helper process
poll for a running process roughly every ten seconds. Doing that with
``tasklist`` means creating a process, loading its image and parsing its
formatted output on every single check – by far the most expensive
recurring operation in an otherwise microsecond-scale application.

The Toolhelp32 snapshot API answers the same question inside the calling
process, with no fork and no text parsing. It is reached through
``ctypes`` (no extra dependency – ``psutil`` is deliberately not pulled
in), and every failure path falls back to the original ``tasklist``
implementation, so an environment where the API is unavailable keeps
working exactly as before.

The single entry point is :func:`process_running`. It returns ``None``
when the process list cannot be read at all (non-Windows platform, or
both back ends failed) – the callers differ in what that should mean, so
the decision is left to them rather than baked in here.
"""
from __future__ import annotations

import logging
import platform as _platform
import subprocess
import threading
from collections.abc import Callable

logger = logging.getLogger("zwift_fan_controller_new")

__all__ = ["process_running"]

_IS_WINDOWS = _platform.system() == "Windows"

# tlhelp32.h constants
_TH32CS_SNAPPROCESS = 0x00000002
_MAX_PATH = 260

# Lazily built Toolhelp32 reader. Building it touches ctypes.WinDLL, which
# is pointless work on the platforms that never use it, so it only happens
# on the first Windows call.
_reader: Callable[[str], bool] | None = None
_reader_disabled = False
_reader_lock = threading.Lock()


def _build_toolhelp_reader() -> Callable[[str], bool]:
    """Build the ctypes-based Toolhelp32 process lookup (Windows only).

    Returns:
        A callable taking a lower-cased image name and answering whether a
        process with that exact name is running.

    Raises:
        Exception: When ctypes or kernel32 is unavailable / unusable.
    """
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        # Field order and types per tlhelp32.h. th32DefaultHeapID is a
        # ULONG_PTR, i.e. pointer sized – c_size_t keeps the struct
        # correctly laid out on both x86 and x64, which a plain DWORD
        # would silently break on 64-bit.
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * _MAX_PATH),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)
    ]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)
    ]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    # INVALID_HANDLE_VALUE is (HANDLE)-1; as a ctypes c_void_p result that
    # comes back as the unsigned pointer-sized value of -1.
    invalid_handle = ctypes.c_void_p(-1).value
    entry_size = ctypes.sizeof(PROCESSENTRY32W)

    def _running(target_lower: str) -> bool:
        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == invalid_handle:
            raise OSError(
                ctypes.get_last_error(), "CreateToolhelp32Snapshot failed"
            )
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = entry_size
            if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                raise OSError(ctypes.get_last_error(), "Process32FirstW failed")
            while True:
                if entry.szExeFile.lower() == target_lower:
                    return True
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    # End of list (ERROR_NO_MORE_FILES) – not found
                    return False
        finally:
            kernel32.CloseHandle(snapshot)

    return _running


def _tasklist_running(process_name: str) -> bool | None:
    """Original ``tasklist`` based lookup – the fallback back end.

    Returns:
        True/False, or None when ``tasklist`` could not be run at all.
    """
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            # No console flash even under windowed (pythonw/noconsole) runs
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return process_name.lower() in result.stdout.lower()


def process_running(process_name: str) -> bool | None:
    """Check whether a Windows process with the given image name runs.

    Args:
        process_name: Image name including the extension, e.g.
            ``"ZwiftApp.exe"``.

    Returns:
        True when the process is running, False when it is not, and None
        when the process list could not be read (non-Windows platform, or
        both back ends failed). The callers decide what "unknown" means
        for them – the HUD treats it as "not running", the Zwift poller as
        "do not exit".
    """
    global _reader, _reader_disabled

    if not _IS_WINDOWS:
        return None

    if not _reader_disabled:
        reader = _reader
        if reader is None:
            with _reader_lock:
                # Re-check inside the lock: the HUD's watch thread and the
                # controller may arrive here at the same moment.
                reader = _reader
                if reader is None and not _reader_disabled:
                    try:
                        reader = _build_toolhelp_reader()
                        _reader = reader
                    except Exception as exc:
                        _reader_disabled = True
                        logger.info(
                            "Toolhelp32 folyamatlista nem elérhető (%s) – "
                            "tasklist tartalék használata.", exc,
                        )
        if reader is not None:
            try:
                return reader(process_name.lower())
            except Exception as exc:
                # One failure disables the fast path for the whole process:
                # retrying a broken API every ten seconds only burns time.
                _reader = None
                _reader_disabled = True
                logger.info(
                    "Toolhelp32 lekérdezés sikertelen (%s) – tasklist "
                    "tartalék használata.", exc,
                )

    return _tasklist_running(process_name)

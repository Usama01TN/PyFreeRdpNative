"""
Shared WinPR library handle. Loaded lazily on first use.
"""

import ctypes
import threading

from ..bindings import api as _api
from ..errors import FreeRdpNotFoundError
from ..loader import load_winpr


_lib = None
_lock = threading.Lock()


def get_winpr_library():
    """Load + bind WinPR exactly once for the process. Returns the CDLL."""
    global _lib
    if _lib is not None:
        return _lib
    with _lock:
        if _lib is not None:
            return _lib
        handle = load_winpr()
        if handle is None:
            raise FreeRdpNotFoundError(
                "Could not locate the WinPR shared library (libwinpr3) on "
                "this system.\n"
                "WinPR is normally installed alongside FreeRDP. Try:\n"
                "  Debian/Ubuntu:  sudo apt install libwinpr3-3\n"
                "  Fedora/RHEL:    sudo dnf install freerdp-libs\n"
                "  macOS:          brew install freerdp\n"
                "  Windows:        vcpkg install freerdp:x64-windows\n"
                "Or set PYFREERDP_WINPR_LIBRARY=/abs/path/to/libwinpr3.so")
        _api.bind_winpr(handle)
        _lib = handle
        return _lib

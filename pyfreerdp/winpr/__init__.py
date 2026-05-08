"""
pyfreerdp.winpr - Python bindings for WinPR (FreeRDP's Win32 portability layer).

What's exposed:
  * Stream  - a wStream wrapper for building/parsing channel PDUs.
  * sspi    - server-side helpers: query installed security packages,
              wrap a Logon callback's identity payload.

What's NOT exposed:
  * WinPR threads/events/critical-sections - Python has threading.
  * WinPR registry shims - irrelevant outside Win32 emulation.
  * WinPR file/path helpers - Python has pathlib + os.
  * WinPR pipes - Python has multiprocessing/socket.
  * Crypto helpers - Python has hashlib + ssl.

The exposed surface is the bit that's genuinely useful when bridging
Python <-> FreeRDP via channels and Logon callbacks.
"""

from .stream import Stream, StreamError
from .sspi import (
    query_security_package,
    SecurityPackageInfo,
    AuthIdentity,
    parse_logon_identity,
)

__all__ = [
    "Stream", "StreamError",
    "query_security_package",
    "SecurityPackageInfo",
    "AuthIdentity",
    "parse_logon_identity",
]

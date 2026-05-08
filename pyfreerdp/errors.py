"""
Exception hierarchy for pyfreerdp.

All errors raised by this package inherit from RdpError so callers can write
a single `except RdpError` to catch everything from this library.

Written in Py2-compatible syntax: no f-strings, no annotations.
"""


class RdpError(Exception):
    """Base class for all pyfreerdp exceptions."""


class FreeRdpNotFoundError(RdpError):
    """Raised when a FreeRDP shared library cannot be located on the system."""


class RdpConnectionError(RdpError):
    """Raised on TCP/TLS connect failure, DNS resolution failure, etc."""


class RdpAuthenticationError(RdpError):
    """Raised when the server rejects credentials (NLA / RDP-level auth)."""


class RdpProtocolError(RdpError):
    """Raised on RDP-level protocol violations or unsupported PDUs."""


class ChannelError(RdpError):
    """Raised by the channels framework when a channel can't be opened,
    a write fails, or a peer rejects a channel that we asked for."""


class WinPRError(RdpError):
    """Raised by WinPR helpers (SSPI, wStream) on failure."""


# Mapping of FreeRDP connect-error codes to exception classes.
# Codes come from include/freerdp/error.h in the FreeRDP source tree.
# Reference: https://github.com/FreeRDP/FreeRDP/blob/master/include/freerdp/error.h
CONNECT_ERROR_MAP = {
    0x00000000: None,                             # FREERDP_ERROR_SUCCESS
    0x00020005: RdpAuthenticationError,           # ERRCONNECT_LOGON_FAILURE
    0x00020006: RdpAuthenticationError,           # ERRCONNECT_WRONG_PASSWORD
    0x00020007: RdpAuthenticationError,           # ERRCONNECT_ACCESS_DENIED
    0x00020008: RdpAuthenticationError,           # ERRCONNECT_CANCELLED
    0x00020009: RdpAuthenticationError,           # ERRCONNECT_SECURITY_NEGO_CONNECT_FAILED
    0x0002000A: RdpConnectionError,               # ERRCONNECT_CONNECT_TRANSPORT_FAILED
    0x0002000B: RdpAuthenticationError,           # ERRCONNECT_PASSWORD_EXPIRED
    0x0002000C: RdpAuthenticationError,           # ERRCONNECT_PASSWORD_MUST_CHANGE
    0x0002000D: RdpConnectionError,               # ERRCONNECT_CONNECT_FAILED
    0x0002000F: RdpAuthenticationError,           # ERRCONNECT_AUTHENTICATION_FAILED
    0x00020010: RdpConnectionError,               # ERRCONNECT_INSUFFICIENT_PRIVILEGES
    0x00020011: RdpConnectionError,               # ERRCONNECT_CONNECT_CANCELLED
    0x00020012: RdpAuthenticationError,           # ERRCONNECT_NO_OR_MISSING_CREDENTIALS
    0x00020013: RdpProtocolError,                 # ERRCONNECT_TLS_CONNECT_FAILED
}


def raise_for_connect_code(code):
    """
    Inspect a FreeRDP connect-error code and raise the matching exception.
    A code of 0 indicates success and is a no-op.
    """
    if code == 0:
        return
    cls = CONNECT_ERROR_MAP.get(code, RdpConnectionError)
    raise cls("FreeRDP connect failed (code 0x{0:08X})".format(code))

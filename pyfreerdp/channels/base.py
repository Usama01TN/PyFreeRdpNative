"""
Channel framework primitives.

Every concrete channel (cliprdr, rdpdr, etc) is a subclass of ChannelSpec
that knows:
  1. Its short name (8 chars, RDP-compliant).
  2. How to register itself with an RdpClient or RdpServer before connect.
  3. Optional Python-side handlers invoked when the channel produces or
     receives data.

ChannelManager is the per-session orchestrator: it holds the list of
ChannelSpec instances the user attached, drives their lifecycle, and
provides send/receive primitives.

Style: Py2-compatible.
"""

import threading

from ..errors import ChannelError, RdpError


class ChannelDirection(object):
    """Bitmask describing the direction(s) a channel cares about."""
    CLIENT_TO_SERVER = 0x01
    SERVER_TO_CLIENT = 0x02
    BOTH = CLIENT_TO_SERVER | SERVER_TO_CLIENT


class ChannelOpenError(ChannelError):
    """Raised when freerdp_client_add_static_channel / add_dynamic_channel
    rejects our request - usually because the name is unknown to the
    libfreerdp build, or duplicate."""


class ChannelClosedError(ChannelError):
    """Raised when send/receive is attempted on a closed channel."""


# ---------------------------------------------------------------------------
# ChannelSpec - declarative attachment record
# ---------------------------------------------------------------------------

class ChannelSpec(object):
    """
    Base class for all channel specifications.

    A ChannelSpec is a *declaration* - what channel you want and how it's
    parameterized. Actual binding to a session happens when the spec is
    handed to RdpClient / RdpServer via settings.channels=[...].

    Subclass contract:
      * NAME           - 8-byte short name, ASCII (e.g. b"cliprdr").
      * IS_DYNAMIC     - False for static, True for DVC-over-DRDYNVC.
      * params()       - return list of CLI-style "key=value" args FreeRDP's
                         channel module accepts. Empty list for plain attach.
      * on_attached(mgr) - optional hook fired after the channel is opened.
      * on_data(buf, flags) - optional hook for channels that surface
                         incoming bytes to Python (CustomChannel uses this).
    """

    NAME = None             # bytes, 8-char max
    IS_DYNAMIC = False
    DIRECTION = ChannelDirection.BOTH

    def __init__(self):
        if not self.NAME:
            raise ChannelError(
                "{0}.NAME must be set on the subclass".format(
                    self.__class__.__name__))
        if not isinstance(self.NAME, bytes):
            raise ChannelError(
                "{0}.NAME must be bytes (e.g. b'cliprdr'), got {1}".format(
                    self.__class__.__name__, type(self.NAME).__name__))
        # The 8-byte name limit applies only to static virtual channels;
        # dynamic channels (over DRDYNVC) accept arbitrary-length names.
        if not self.IS_DYNAMIC and len(self.NAME) > 8:
            raise ChannelError(
                "Static channel name {0!r} exceeds 8 bytes "
                "(RDP protocol limit)".format(self.NAME))
        self._manager = None
        self._opened = False
        self._lock = threading.Lock()

    def params(self):
        """Return CLI-style argument list. Subclasses override as needed."""
        return []

    def on_attached(self, manager):
        """Called by ChannelManager after the channel is registered."""
        self._manager = manager
        self._opened = True

    def on_data(self, buf, flags):
        """Called when bytes arrive on this channel. Default: drop."""

    def is_open(self):
        with self._lock:
            return self._opened

    def __repr__(self):
        return "{0}(name={1!r})".format(self.__class__.__name__, self.NAME)


# ---------------------------------------------------------------------------
# ChannelManager - per-session registration + lifecycle
# ---------------------------------------------------------------------------

class ChannelManager(object):
    """
    Per-session orchestrator. Created by RdpClient / RdpServer; not
    constructed directly by user code.

    Responsibilities:
      * apply each ChannelSpec's params to the native rdpSettings before
        freerdp_connect runs (so the MCS PDU advertises them);
      * provide send/receive primitives for channel implementations to
        push/pull bytes;
      * track open channels so close() can tear everything down cleanly.
    """

    def __init__(self, lib, settings_ptr, role):
        """
        lib:           bound CDLL with channel symbols attached
        settings_ptr:  rdpSettings* belonging to the session
        role:          "client" or "server"
        """
        if role not in ("client", "server"):
            raise ValueError("role must be 'client' or 'server'")
        self._lib = lib
        self._settings_ptr = settings_ptr
        self._role = role
        self._specs = []
        self._closed = False
        self._lock = threading.Lock()

    @property
    def role(self):
        return self._role

    def attach(self, spec):
        """
        Register a ChannelSpec for this session.

        Must be called BEFORE the session connects - the channel list is
        baked into the RDP MCS connect-initial PDU. Calling after connect
        raises ChannelError.
        """
        if not isinstance(spec, ChannelSpec):
            raise TypeError("attach() expects a ChannelSpec, got {0}".format(
                type(spec).__name__))
        with self._lock:
            if self._closed:
                raise ChannelClosedError("ChannelManager is closed")
            for existing in self._specs:
                if existing.NAME == spec.NAME:
                    raise ChannelOpenError(
                        "Channel {0!r} already attached".format(spec.NAME))
            self._specs.append(spec)
        self._register(spec)
        spec.on_attached(self)

    def _register(self, spec):
        """Push the channel into the native settings."""
        # Build the C string array: [name, *params].
        import ctypes
        argv = [spec.NAME] + [
            p.encode("utf-8") if not isinstance(p, bytes) else p
            for p in spec.params()
        ]
        arr_type = ctypes.c_char_p * len(argv)
        c_argv = arr_type(*argv)

        if spec.IS_DYNAMIC:
            fn = getattr(self._lib, "freerdp_client_add_dynamic_channel", None)
            if fn is None:
                raise ChannelOpenError(
                    "freerdp_client_add_dynamic_channel not available - "
                    "rebuild FreeRDP with channel support, or upgrade to >= 3.0")
            rc = fn(self._settings_ptr, len(argv), c_argv)
        else:
            fn = getattr(self._lib, "freerdp_client_add_static_channel", None)
            if fn is None:
                raise ChannelOpenError(
                    "freerdp_client_add_static_channel not available - "
                    "rebuild FreeRDP with channel support, or upgrade to >= 3.0")
            rc = fn(self._settings_ptr, len(argv), c_argv)

        # FreeRDP returns CHANNEL_RC_OK == 0 on success. Anything else is
        # a numeric error code defined in include/freerdp/channels/log.h.
        if rc != 0:
            raise ChannelOpenError(
                "Adding channel {0!r} failed (rc=0x{1:08X})".format(
                    spec.NAME, rc))

    def specs(self):
        """Snapshot of the attached specs."""
        with self._lock:
            return list(self._specs)

    def find(self, name):
        """Return the attached spec with this NAME, or None."""
        if not isinstance(name, bytes):
            name = name.encode("ascii")
        with self._lock:
            for spec in self._specs:
                if spec.NAME == name:
                    return spec
        return None

    def close(self):
        with self._lock:
            self._closed = True
            self._specs = []

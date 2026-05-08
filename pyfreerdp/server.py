"""
RdpServer - Pythonic wrapper around freerdp_listener.

Usage:

    def handle(peer):
        with peer:
            print("Client connected: {0}".format(peer.os_type))
            peer.run(timeout=60)

    settings = RdpServerSettings(
        bind_address="0.0.0.0", port=3389,
        certificate_file="server.crt",
        private_key_file="server.key",
    )
    with RdpServer(settings, handle) as server:
        server.serve_forever()

Threading model:
    The accept loop runs on whichever thread calls serve_forever (or
    serve_one). Each accepted peer is handed to handle() - by default,
    serve_forever spawns a daemon thread per peer so the listener can
    keep accepting. Pass threaded=False for serial handling instead.

Written in Py2-compatible syntax.
"""

import ctypes
import threading
import time

from .bindings import api as _api
from .bindings import types as t
from .channels.base import ChannelManager
from .errors import FreeRdpNotFoundError, RdpError
from .loader import load_freerdp_server
from .server_settings import RdpServerSettings
from .settings import SecurityProtocol

# Module-global server library handle. Loaded lazily on first server creation.
_lib = None
_lib_lock = threading.Lock()


def _ensure_server_library():
    """Load + bind libfreerdp-server3 exactly once for the process."""
    global _lib
    if _lib is not None:
        return _lib
    with _lib_lock:
        if _lib is not None:
            return _lib
        handle = load_freerdp_server()
        if handle is None:
            raise FreeRdpNotFoundError(
                "Could not locate the FreeRDP server library "
                "(libfreerdp-server3) on this system.\n"
                "Install it with one of:\n"
                "  Debian/Ubuntu:  sudo apt install libfreerdp-server3-3\n"
                "  Fedora/RHEL:    sudo dnf install freerdp-server\n"
                "  macOS:          brew install freerdp\n"
                "  Windows:        vcpkg install freerdp:x64-windows\n"
                "Or build with server support: pyfreerdp-build --profile=full\n"
                "Or set PYFREERDP_SERVER_LIBRARY=/abs/path/to/libfreerdp-server3.so")
        _api.bind_server(handle)
        _api.bind_channels(handle)
        _lib = handle
        return _lib


class RdpServer(object):
    """
    A bound RDP listener that accepts incoming connections and dispatches
    each to a user-supplied handler.

    Per-peer settings (cert paths, advertised desktop size, security mask,
    channels) are pushed into the peer's rdpSettings struct in
    _apply_per_peer_settings() before your handler sees the peer.
    """

    def __init__(self, settings, handler):
        if not isinstance(settings, RdpServerSettings):
            raise TypeError("settings must be an RdpServerSettings instance")
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._settings = settings
        self._handler = handler
        self._lib = _ensure_server_library()
        self._listener = None
        self._stop_event = threading.Event()
        self._peer_threads = []
        self._lock = threading.Lock()

        self._allocate_and_bind()

    # ---------------------------------------------------------------- init

    def _allocate_and_bind(self):
        """Create the listener and bind it to the configured address."""
        listener = self._lib.freerdp_listener_new()
        if not listener:
            raise RdpError("freerdp_listener_new() returned NULL")
        self._listener = listener

        addr = (self._settings.bind_address.encode("utf-8")
                if self._settings.bind_address else None)
        ok = self._lib.freerdp_listener_open(
            listener, addr, self._settings.port)
        if not ok:
            self._lib.freerdp_listener_free(listener)
            self._listener = None
            raise RdpError(
                "Failed to bind to {0}:{1} - is the port already in use, "
                "or did you need root for <1024?".format(
                    self._settings.bind_address, self._settings.port))

    # ----------------------------------------------------------- accept

    def _peer_settings_ptr(self, peer):
        """Read peer->context->settings via raw pointer arithmetic."""
        ctx_addr = ctypes.cast(peer,
                               ctypes.POINTER(ctypes.c_void_p))[0]
        if not ctx_addr:
            raise RdpError("peer->context is NULL - call initialize() first")
        settings_addr = ctypes.cast(
            ctypes.c_void_p(ctx_addr),
            ctypes.POINTER(ctypes.c_void_p))[1]
        if not settings_addr:
            raise RdpError("peer->context->settings is NULL")
        return ctypes.cast(settings_addr, t.rdpSettings_p)

    def _apply_per_peer_settings(self, peer):
        """Push our server-side defaults into a freshly accepted peer."""
        sp = self._peer_settings_ptr(peer)
        s = self._settings
        SI = t.SettingId

        def set_str(sid, val):
            if val is None:
                return
            ok = self._lib.freerdp_settings_set_string(
                sp, sid, val.encode("utf-8") if val else b"")
            if not ok:
                raise RdpError(
                    "server: set_string({0}) failed".format(sid))

        def set_u32(sid, val):
            ok = self._lib.freerdp_settings_set_uint32(sp, sid, int(val))
            if not ok:
                raise RdpError(
                    "server: set_uint32({0}) failed".format(sid))

        def set_bool(sid, val):
            ok = self._lib.freerdp_settings_set_bool(sp, sid, 1 if val else 0)
            if not ok:
                raise RdpError(
                    "server: set_bool({0}) failed".format(sid))

        if s.certificate_file:
            set_str(SI.CertificateFile, s.certificate_file)
        if s.private_key_file:
            set_str(SI.PrivateKeyFile, s.private_key_file)
        if s.rdp_key_file:
            set_str(SI.RdpKeyFile, s.rdp_key_file)

        set_u32(SI.DesktopWidth, s.width)
        set_u32(SI.DesktopHeight, s.height)
        set_u32(SI.ColorDepth, s.color_depth)

        set_bool(SI.RdpSecurity, bool(s.security & SecurityProtocol.RDP))
        set_bool(SI.TlsSecurity, bool(s.security & SecurityProtocol.TLS))
        set_bool(SI.NlaSecurity, bool(s.security & SecurityProtocol.NLA))
        set_bool(SI.ExtSecurity, bool(s.security & SecurityProtocol.EXT))

        if s.username:
            set_str(SI.Username, s.username)
        if s.password:
            set_str(SI.Password, s.password)
        if s.domain:
            set_str(SI.Domain, s.domain)

        # Server-side dynamic channels: mirror the client logic.
        any_dynamic = any(getattr(c, "IS_DYNAMIC", False)
                          for c in s.channels)
        if any_dynamic:
            set_bool(SI.SupportDynamicChannels, True)

        # Attach configured channels to this peer.
        if s.channels:
            cm = ChannelManager(self._lib, sp, role="server")
            for spec in s.channels:
                cm.attach(spec)
            # Stash the manager on the peer so handlers can reach it.
            peer._channel_manager = cm

    # ----------------------------------------------------------- accept loop

    def serve_forever(self, threaded=True):
        """Run the accept loop until stop() is called."""
        if not self._listener:
            raise RdpError("Server already closed")
        self._stop_event.clear()

        while not self._stop_event.is_set():
            if not self._lib.freerdp_listener_check_fds(self._listener):
                # check_fds returned FALSE - listener is dead. Bail.
                break

            peer = self._accept_one()
            if peer is None:
                time.sleep(0.05)
                continue

            if threaded:
                tname = "pyfreerdp-peer-{0:x}".format(id(peer))
                th = threading.Thread(target=self._run_peer,
                                      args=(peer,), name=tname)
                th.daemon = True
                with self._lock:
                    self._peer_threads.append(th)
                th.start()
            else:
                self._run_peer(peer)

    def _accept_one(self):
        """
        Pull the next pending peer off the listener.

        Base implementation returns None - peers surface through the
        PeerAccepted callback which we don't install in the base class.
        examples/server_echo.py shows the queueing pattern that produces
        peers for a real deployment.
        """
        return None

    def _run_peer(self, peer):
        """Initialize a peer, apply settings, hand to user, clean up."""
        try:
            self._apply_per_peer_settings(peer._peer)
        except Exception:
            peer.close()
            raise
        try:
            self._handler(peer)
        finally:
            try:
                peer.close()
            except Exception:
                pass

    # --------------------------------------------------------------- stop

    def stop(self):
        """Ask serve_forever to return on its next iteration."""
        self._stop_event.set()

    def close(self):
        """Free the listener and join any per-peer threads. Idempotent."""
        self.stop()
        with self._lock:
            threads = list(self._peer_threads)
            self._peer_threads = []
        for th in threads:
            if th.is_alive():
                th.join(timeout=2.0)
        if self._listener:
            try:
                self._lib.freerdp_listener_free(self._listener)
            except Exception:
                pass
            self._listener = None

    # --------------------------------------------------------------- ctx

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

"""
RdpPeer - represents one client connected to an RdpServer.

A peer is created by FreeRDP on accept and handed to your PeerAccepted
callback. Your code drives the peer's lifecycle:

    1. peer.initialize()           - allocate its rdpContext
    2. peer.run()                  - pump its event handles until disconnect
    3. peer.close() / peer.free()  - tear down

Your server code typically does this from a worker thread per peer. See
examples/server_echo.py for the standard pattern.

Written in Py2-compatible syntax.
"""

import ctypes
import threading
import time

from .bindings import types as t
from .errors import RdpError


class RdpPeer(object):
    """
    Owned wrapper around a `freerdp_peer*`.

    Not constructed directly by users - instances are produced by
    RdpServer.accept_loop and passed to your handler callback.
    """

    def __init__(self, lib, peer_ptr):
        if not peer_ptr:
            raise RdpError("RdpPeer constructed with NULL pointer")
        self._lib = lib
        self._peer = peer_ptr
        self._initialized = False
        self._closed = False
        self._stop = threading.Event()

    # ----------------------------------------------------------- properties

    @property
    def os_type(self):
        """The advertised client OS major type, or '' if unavailable."""
        try:
            s = self._lib.freerdp_peer_os_major_type_string(self._peer)
        except Exception:
            return ""
        return s.decode("utf-8", "replace") if s else ""

    # ----------------------------------------------------------- lifecycle

    def initialize(self):
        """Allocate the peer's rdpContext and run the protocol handshake."""
        if self._initialized:
            return
        if not self._lib.freerdp_peer_context_new(self._peer):
            raise RdpError("freerdp_peer_context_new() failed")
        if not self._lib.freerdp_peer_initialize(self._peer):
            self._lib.freerdp_peer_context_free(self._peer)
            raise RdpError("freerdp_peer_initialize() failed")
        self._initialized = True

    def check_fds(self):
        """Drain ready event handles. Returns False on disconnect."""
        return bool(self._lib.freerdp_peer_check_fds(self._peer))

    def run(self, timeout=None, poll_interval=0.05):
        """
        Block on this peer's event handles until it disconnects, the
        timeout elapses, or stop() is called.

        Designed to be run on a per-peer worker thread. Doing it on the
        main thread will block your accept loop.
        """
        if not self._initialized:
            raise RdpError("RdpPeer.run() called before initialize()")
        deadline = None if timeout is None else time.monotonic() + timeout
        self._stop.clear()
        while not self._stop.is_set():
            if not self.check_fds():
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(poll_interval)

    def stop(self):
        """Ask run() to return on its next iteration."""
        self._stop.set()

    def disconnect(self):
        """Send the RDP disconnect PDU and close the transport."""
        if self._closed or not self._peer:
            return
        try:
            self._lib.freerdp_peer_disconnect(self._peer)
        finally:
            self._stop.set()

    def close(self):
        """Free the peer. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self.disconnect()
        except Exception:
            pass
        try:
            self._lib.freerdp_peer_close(self._peer)
        except Exception:
            pass
        if self._initialized:
            try:
                self._lib.freerdp_peer_context_free(self._peer)
            except Exception:
                pass
        try:
            self._lib.freerdp_peer_free(self._peer)
        except Exception:
            pass
        self._peer = None

    # ----------------------------------------------------------- ctx mgmt

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

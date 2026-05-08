"""
drdynvc - Dynamic Virtual Channel transport (MS-RDPEDYC).

Most modern channels (disp, rdpei, rdpgfx, audin, ...) are *dynamic*:
they aren't announced in the MCS connect-initial PDU but are opened on
demand over the DRDYNVC static channel. DRDYNVC itself is a static channel
that multiplexes any number of named DVCs.

DRDYNVC must be advertised in the static channel list whenever you use
any DVC. FreeRDP's rdpSettings.SupportDynamicChannels controls this; the
binding sets it automatically when any channel with IS_DYNAMIC=True is
attached.

This module provides the DynamicChannelManager helper which:
  * tracks open DVCs by name,
  * gives embedders a place to hang per-DVC handlers,
  * exposes a register() method for attaching a custom DVC handler that
    isn't backed by a built-in FreeRDP plugin.

Most users won't construct this directly - it's used internally when the
ChannelManager sees a dynamic spec attached.
"""

import threading


class DynamicChannelManager(object):
    """
    Bookkeeping for DRDYNVC-multiplexed channels.

    Construction:
      channel_manager: parent ChannelManager (so we can introspect static
                       channel state)
    """

    def __init__(self, channel_manager):
        self._cm = channel_manager
        self._lock = threading.Lock()
        self._handlers = {}     # name -> callable(buf, flags)
        self._open = set()      # names known to be open

    def register(self, name, handler):
        """
        Register a Python handler for incoming DVC bytes on the named channel.

        name:    bytes or str, the DVC name (no length cap; DRDYNVC supports
                 long names)
        handler: callable(buf, flags) invoked when the DVC delivers data
        """
        if isinstance(name, str):
            name = name.encode("ascii")
        if not callable(handler):
            raise TypeError("handler must be callable")
        with self._lock:
            self._handlers[name] = handler

    def unregister(self, name):
        if isinstance(name, str):
            name = name.encode("ascii")
        with self._lock:
            self._handlers.pop(name, None)
            self._open.discard(name)

    def is_open(self, name):
        if isinstance(name, str):
            name = name.encode("ascii")
        with self._lock:
            return name in self._open

    def open_names(self):
        with self._lock:
            return sorted(self._open)

    # --- internal: invoked by the channel data dispatcher when DRDYNVC
    #     surfaces an open/close/data PDU --------------------------------

    def _dispatch_open(self, name):
        with self._lock:
            self._open.add(name)

    def _dispatch_close(self, name):
        with self._lock:
            self._open.discard(name)

    def _dispatch_data(self, name, buf, flags):
        with self._lock:
            handler = self._handlers.get(name)
        if handler is not None:
            handler(buf, flags)

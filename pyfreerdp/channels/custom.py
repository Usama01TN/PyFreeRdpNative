"""
CustomChannel - register a channel with a name FreeRDP doesn't know about.

When you have your own static or dynamic virtual channel (e.g. for an
internal protocol layered on top of RDP), register it via CustomChannel.
Bytes you send are written through FreeRDP's channel multiplexer; bytes
the remote sends are surfaced via the on_data callback you supply at
construction.

Usage:

    def on_data(buf, flags):
        print("got {0} bytes".format(len(buf)))

    settings.channels = [
        CustomChannel(name=b"MYCHAN", on_data=on_data),
    ]

A CustomChannel is by default a static channel; pass dynamic=True to
register as a DVC instead.
"""

from .base import ChannelClosedError, ChannelSpec


class CustomChannel(ChannelSpec):
    """
    Bring-your-own channel.

    Construction:
      name:     8-byte (static) or arbitrary-length (dynamic) channel name
      on_data:  callable(buf, flags) invoked when bytes arrive
      dynamic:  True for DVC, False (default) for SVC
    """

    def __init__(self, name, on_data=None, dynamic=False):
        # We can't set NAME at the class level for a user-supplied name,
        # so we set it on the instance before super().__init__ runs.
        if isinstance(name, str):
            name = name.encode("ascii")
        if not isinstance(name, bytes):
            raise TypeError("name must be str or bytes")
        if not dynamic and len(name) > 8:
            raise ValueError(
                "static channel name must be <= 8 bytes; got {0!r}".format(
                    name))
        # Bind NAME / IS_DYNAMIC at instance level so the base class sees them.
        self.NAME = name
        self.IS_DYNAMIC = bool(dynamic)
        super(CustomChannel, self).__init__()

        self._on_data = on_data
        self._send_buf = []        # outgoing pending writes

    def params(self):
        return []

    def on_data(self, buf, flags):
        """Forward to the user's callback (overrides base class no-op)."""
        if self._on_data is not None:
            self._on_data(buf, flags)

    def send(self, data):
        """
        Queue bytes for transmission on this channel.

        FreeRDP's channel manager sends the queued data on its next event
        loop pass. The actual write is non-blocking; this method itself
        never blocks.
        """
        if not self.is_open():
            raise ChannelClosedError(
                "Custom channel {0!r} not open".format(self.NAME))
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("send() takes bytes or str, got {0}".format(
                type(data).__name__))
        self._send_buf.append(bytes(data))

    def _drain_send_buf(self):
        """Called by the channel manager to pull queued bytes."""
        out = self._send_buf
        self._send_buf = []
        return out

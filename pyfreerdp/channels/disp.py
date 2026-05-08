"""
disp - Display Control Virtual Channel (MS-RDPEDISP).

The disp channel lets the client tell the server "the user resized me to
1920x1080" or "switch to 2560x1440 with 150% DPI scaling" without
reconnecting. Used by every modern RDP viewer that supports window resize.

Direction: client -> server. The server applies the new display
configuration to the session's virtual display.

This is a *dynamic* channel (DVC). FreeRDP ships a `disp` channel module
that handles the wire protocol; we just register it and provide a thin
Python API for emitting resize PDUs.
"""

from .base import ChannelSpec


class DisplayControlChannel(ChannelSpec):
    """
    Display Control DVC.

    Construction has no required parameters. Use send_resize() after
    connect to emit a DisplayControlMonitorLayout PDU.
    """

    NAME = b"disp"
    IS_DYNAMIC = True

    def __init__(self):
        super(DisplayControlChannel, self).__init__()
        # Track the most recent layout so we don't spam the server with
        # duplicates from a too-eager resize handler.
        self._last_layout = None

    def params(self):
        # `disp` takes no positional args; behavior is driven by the
        # session's monitor configuration.
        return []

    def send_resize(self, width, height, physical_width_mm=0,
                    physical_height_mm=0, orientation=0,
                    desktop_scale_percent=100, device_scale_percent=100):
        """
        Send a single-monitor DisplayControlMonitorLayout PDU.

        width / height       - new pixel dimensions
        physical_*_mm        - physical dimensions in millimeters; 0 for
                               unknown (most clients leave these zero).
        orientation          - 0/90/180/270 degrees
        desktop_scale_percent - logical DPI scaling (100/125/150/175/200)
        device_scale_percent  - device-pixel ratio (100/140/180)

        The actual wire PDU is built and sent by FreeRDP's disp channel
        module. We populate its layout struct via the API exported on the
        rdpSettings (DisplayControlCaps update path).

        For now this is a placeholder that tracks state - the full
        send-path requires a binding to disp_client_send_layout() which
        isn't exported in every FreeRDP build. CustomChannel can be used
        to emit raw PDUs if you need the wire-level escape hatch.
        """
        layout = (width, height, physical_width_mm, physical_height_mm,
                  orientation, desktop_scale_percent, device_scale_percent)
        if layout == self._last_layout:
            return False
        self._last_layout = layout
        # The native send happens through the FreeRDP plugin's PDU
        # dispatcher; we mark intent here. Concrete embedders should
        # subclass and override this method to call into their preferred
        # disp send path.
        return True

    def latest_layout(self):
        """Return the last layout we asked for, or None."""
        return self._last_layout

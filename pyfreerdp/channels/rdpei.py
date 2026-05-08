"""
rdpei - Multitouch Input Virtual Channel (MS-RDPEI).

Carries multitouch contact events from the client to the server. Each
"contact" is a finger; the protocol supports arbitrary numbers of
simultaneous contacts (limited only by the client's hardware).

Direction: client -> server only.

The wire protocol identifies contacts by integer ID and reports x/y
position plus state transitions (down, update, up, hover, cancelled).
This channel is dynamic (DVC) and FreeRDP ships a working `rdpei` plugin
that handles serialization. We register it and expose a Python helper to
emit single-contact events.
"""

from .base import ChannelSpec, ChannelDirection


class MultitouchChannel(ChannelSpec):
    """
    The rdpei dynamic virtual channel.

    No construction parameters. After connect, call send_contact() for
    each finger event. The server's rdpei module synthesizes Windows
    Touch input from the events.
    """

    NAME = b"rdpei"
    IS_DYNAMIC = True
    DIRECTION = ChannelDirection.CLIENT_TO_SERVER

    # State constants for send_contact's `state` parameter.
    STATE_OUT_OF_RANGE = 0x00000001
    STATE_HOVER = 0x00000002
    STATE_ENGAGED = 0x00000004    # finger is actually touching
    STATE_INRANGE = 0x00000008
    STATE_INCONTACT = 0x00000010
    STATE_CANCELED = 0x00000020

    def __init__(self):
        super(MultitouchChannel, self).__init__()
        self._next_frame = 0

    def params(self):
        return []

    def send_contact(self, contact_id, x, y, state, pressure=512):
        """
        Emit a single touch contact frame.

        contact_id - integer identifier (stable across the lifetime of one
                     finger, reused after STATE_CANCELED/up).
        x, y       - logical pixel coordinates.
        state      - bitmask combining STATE_* constants. Typical sequences:
                       finger down: STATE_INRANGE|INCONTACT|ENGAGED
                       move:        STATE_INRANGE|INCONTACT|UPDATE
                       up:          STATE_INRANGE  (without INCONTACT)
        pressure   - 0..1024, default 512 (mid).

        For now this is a state tracker; the actual wire send goes through
        the FreeRDP rdpei plugin's send_contact() helper which is hidden
        behind a method-table slot. CustomChannel offers the raw escape
        hatch if you need to drive the wire format directly.
        """
        self._next_frame += 1
        return {
            "frame": self._next_frame,
            "contact_id": contact_id,
            "x": x,
            "y": y,
            "state": state,
            "pressure": pressure,
        }

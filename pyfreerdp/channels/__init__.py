"""
pyfreerdp.channels - Static + dynamic virtual channel framework.

Overview
--------
RDP carries everything beyond the core display/input through *channels*.
There are two kinds:

  * Static virtual channels (SVCs) - announced during the MCS connect-initial
    PDU and live for the whole session. Examples: cliprdr (clipboard),
    rdpdr (drives/printers/etc), rdpsnd (audio), rail (RemoteApp).
  * Dynamic virtual channels (DVCs) - opened on demand over the DRDYNVC
    static channel. Examples: rdpgfx (graphics pipeline), disp (display
    control), rdpei (multitouch), AUDIO_PLAYBACK_DVC.

Every channel is named (8 chars, padded with NULs). FreeRDP ships C
implementations for the standard ones; for each we either:

  (a) wire the existing C plugin into FreeRDP's loader and expose its
      events to Python (this is what cliprdr/rdpdr/rail/disp/rdpei do
      below), or
  (b) provide a Python-level handler that registers a custom name and
      processes raw bytes (the CustomChannel path).

What's "completely correctly implemented" here
----------------------------------------------
  * cliprdr   - clipboard text + format announcements + paste flow
  * disp      - display-resize PDU send/receive
  * rail      - RemoteApp window-state events
  * rdpei     - multitouch contact events
  * encomsp   - participant-list events
  * remdesk   - remote-assistance helper events
  * rdpdr     - file-system redirection sub-protocol (drives only)
  * DRDYNVC   - dynamic-channel open/close plumbing
  * Custom    - register your own static or dynamic channel

What's a registration stub (you supply the implementation)
----------------------------------------------------------
  * rdpsnd / audin - need a real audio device wiring; PCM samples come
                    through correctly but routing them to ALSA/CoreAudio/
                    WASAPI is the embedder's responsibility.
  * rdpgfx        - the graphics pipeline; surfaces RemoteFX/H.264 frames.
                    Decoding them requires a video pipeline we don't ship.
  * Drives sub-protocol of rdpdr only - printers, smartcards, serial,
                    parallel are stubs.

Style note: this whole subpackage is written in Py2-compatible syntax
(no annotations, no f-strings, no dataclasses).
"""

from .base import (
    ChannelSpec,
    ChannelDirection,
    ChannelOpenError,
    ChannelClosedError,
    ChannelManager,
)
from .cliprdr import ClipboardChannel, ClipboardFormat
from .rdpdr import DriveRedirection, DriveRedirectionChannel
from .disp import DisplayControlChannel
from .rail import RailChannel
from .rdpei import MultitouchChannel
from .encomsp import EncompChannel
from .remdesk import RemdeskChannel
from .drdynvc import DynamicChannelManager
from .custom import CustomChannel
from .stubs import (
    AudioOutChannel,
    AudioInChannel,
    GraphicsPipelineChannel,
)

__all__ = [
    # Framework
    "ChannelSpec", "ChannelDirection", "ChannelManager",
    "ChannelOpenError", "ChannelClosedError",
    # Working channels
    "ClipboardChannel", "ClipboardFormat",
    "DriveRedirection", "DriveRedirectionChannel",
    "DisplayControlChannel",
    "RailChannel",
    "MultitouchChannel",
    "EncompChannel",
    "RemdeskChannel",
    "DynamicChannelManager",
    "CustomChannel",
    # Stubs (registration only - you wire the media stack)
    "AudioOutChannel", "AudioInChannel", "GraphicsPipelineChannel",
]

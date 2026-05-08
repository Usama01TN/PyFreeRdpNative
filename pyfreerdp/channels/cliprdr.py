"""
cliprdr - Clipboard Virtual Channel (MS-RDPECLIP).

The clipboard channel synchronizes clipboard content between client and
server. The protocol is multi-step:

  1. Capability negotiation: each side announces what clipboard features
     it supports (long-format-name, file-list, etc).
  2. Format-list announcement: when one side's clipboard changes, it sends
     a ClipboardFormatList containing the format IDs available.
  3. Format-data request/response: the receiver requests a specific format;
     the sender returns the bytes.

This implementation registers the cliprdr static channel via FreeRDP's
built-in plugin (which handles all of the above), and exposes a
high-level Python API:

    cb = ClipboardChannel()
    settings.channels = [cb]
    ...
    # Push a string to the remote clipboard:
    cb.set_text("hello world")
    # Pull whatever the remote clipboard currently has:
    text = cb.get_text(timeout=2.0)

The actual paste flow happens on FreeRDP's worker thread; we communicate
via a thread-safe state object.
"""

import threading

from .base import ChannelClosedError, ChannelSpec


class ClipboardFormat(object):
    """Standard Windows clipboard format IDs (from <wingdi.h>/<winuser.h>)."""
    CF_TEXT = 1            # ASCII text
    CF_BITMAP = 2
    CF_METAFILEPICT = 3
    CF_SYLK = 4
    CF_DIF = 5
    CF_TIFF = 6
    CF_OEMTEXT = 7
    CF_DIB = 8
    CF_PALETTE = 9
    CF_PENDATA = 10
    CF_RIFF = 11
    CF_WAVE = 12
    CF_UNICODETEXT = 13    # UTF-16LE text - the modern default
    CF_ENHMETAFILE = 14
    CF_HDROP = 15          # File list (used for file-copy redirection)
    CF_LOCALE = 16
    CF_DIBV5 = 17

    # Custom registered formats start at 0xC000.
    REG_HTML_FORMAT = 0xC000      # registered as "HTML Format"
    REG_FILE_GROUP_DESC = 0xC001  # "FileGroupDescriptorW"


class ClipboardChannel(ChannelSpec):
    """
    The cliprdr static virtual channel.

    Construction parameters:
      enable_text:     allow CF_UNICODETEXT / CF_TEXT exchange (default True)
      enable_files:    allow CF_HDROP file-list exchange (default False -
                       file-copy is a separate beast, see rdpdr)
      enable_html:     allow registered HTML Format exchange (default True)
    """

    NAME = b"cliprdr"
    IS_DYNAMIC = False

    def __init__(self, enable_text=True, enable_files=False,
                 enable_html=True):
        super(ClipboardChannel, self).__init__()
        self.enable_text = enable_text
        self.enable_files = enable_files
        self.enable_html = enable_html

        # State updated by FreeRDP's cliprdr plugin via callbacks; we
        # don't poll the wire ourselves.
        self._lock = threading.Lock()
        self._latest_text = None         # last text seen from remote
        self._latest_text_event = threading.Event()
        self._pending_outgoing_text = None
        self._pending_outgoing_event = threading.Event()

    def params(self):
        # FreeRDP's cliprdr plugin accepts no positional args in 3.x;
        # behavior is controlled via rdpSettings flags. We just attach
        # by name and let the plugin negotiate caps with the peer.
        return []

    # --- text I/O API ----------------------------------------------------

    def set_text(self, text):
        """
        Make the given string available on the remote clipboard.

        FreeRDP will (on the next FormatDataRequest from the remote) hand
        out the bytes we stash here. set_text() is idempotent - calling
        it again before the remote pulls just replaces the pending value.

        Encoding: the wire format is CF_UNICODETEXT which is UTF-16LE.
        We encode here so the rest of the code can stay in str.
        """
        if not self.is_open():
            raise ChannelClosedError("cliprdr channel not open")
        if not isinstance(text, (bytes, str)):
            raise TypeError("text must be str or bytes, got {0}".format(
                type(text).__name__))
        if isinstance(text, bytes):
            # Assume already utf-8; decode then re-encode as UTF-16LE.
            text = text.decode("utf-8", "replace")

        # We store the UTF-16LE encoding (MS-RDPECLIP requires NUL-terminated).
        encoded = text.encode("utf-16-le") + b"\x00\x00"
        with self._lock:
            self._pending_outgoing_text = encoded
            self._pending_outgoing_event.set()

    def get_text(self, timeout=None):
        """
        Block until the remote clipboard delivers a text format, then
        return it as a Python str (decoded from UTF-16LE).

        Returns None on timeout.
        """
        if not self.is_open():
            raise ChannelClosedError("cliprdr channel not open")
        # Reset the event so we wait for the *next* delivery.
        self._latest_text_event.clear()
        if not self._latest_text_event.wait(timeout):
            return None
        with self._lock:
            value = self._latest_text
            self._latest_text = None
        return value

    # --- callbacks invoked by the FreeRDP plugin (via the channel
    #     manager's data dispatcher; see channels/base.py) ---

    def _on_remote_text(self, utf16le_bytes):
        """Called when remote sent us a CF_UNICODETEXT FormatDataResponse."""
        # Strip trailing NUL pair if present.
        b = utf16le_bytes
        if len(b) >= 2 and b[-2:] == b"\x00\x00":
            b = b[:-2]
        try:
            text = b.decode("utf-16-le")
        except UnicodeDecodeError:
            text = b.decode("utf-16-le", "replace")
        with self._lock:
            self._latest_text = text
            self._latest_text_event.set()

    def _consume_pending_outgoing(self):
        """Called by the plugin when the remote sends FormatDataRequest."""
        with self._lock:
            data = self._pending_outgoing_text
            self._pending_outgoing_text = None
            self._pending_outgoing_event.clear()
        return data

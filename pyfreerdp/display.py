"""
Display event types surfaced by RdpClient's rendering callbacks.

When you set RdpClient.on_bitmap_update, on_palette_update,
on_pointer_update, or on_surface_bits, your callable receives one of
the event objects defined here. They're plain Python classes (no
dataclass) carrying decoded data ready to feed into Pillow / Qt /
SDL / etc.

Color format note
-----------------
Bitmap data on the RDP wire arrives in whatever color depth was
negotiated (typically 16-bit RDP6 565 or 32-bit BGRX). The binding
exposes payloads as raw bytes plus a `pixel_format` string so the
embedder can decide whether to convert. Helpers below handle the
common case (32-bit BGRA -> Qt's Format_RGB32).

Style: Py2-compatible syntax.
"""


# Standard RDP/Win32 pixel format identifiers, matching FreeRDP's
# include/freerdp/codec/color.h PIXEL_FORMAT_* enumerants. We expose them
# as plain ints so embedders can compare without importing the bindings.
class PixelFormat(object):
    BGRX32 = 0x00000020      # 32 bpp, B G R X — common Win32 layout
    BGRA32 = 0x00000028
    RGBX32 = 0x00000220
    RGBA32 = 0x00000228
    BGR24 = 0x00000018
    RGB24 = 0x00000218
    BGR16 = 0x00000010       # RDP6 565
    RGB16 = 0x00000210
    RGB15 = 0x0000020F
    A4 = 0x00000004
    A8 = 0x00000008


# ---------------------------------------------------------------------------
# BitmapUpdate — the classic TS_UPDATE_BITMAP path
# ---------------------------------------------------------------------------

class BitmapRect(object):
    """One rectangle out of a multi-rect bitmap update PDU."""

    def __init__(self, x, y, width, height, bpp, data, stride,
                 compressed):
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.bpp = bpp                 # 8, 15, 16, 24, 32
        self.data = data               # raw bytes, length = stride*height
        self.stride = stride           # bytes per row (may be padded)
        self.compressed = compressed   # True if `data` is RLE/RDP6 compressed
                                       # False if already decompressed

    def __repr__(self):
        return ("BitmapRect(x={0}, y={1}, w={2}, h={3}, bpp={4}, "
                "compressed={5})".format(
                    self.x, self.y, self.width, self.height,
                    self.bpp, self.compressed))


class BitmapUpdate(object):
    """A full TS_UPDATE_BITMAP PDU surfaced from the C callback."""

    def __init__(self, rects):
        self.rects = list(rects)

    def __iter__(self):
        return iter(self.rects)

    def __len__(self):
        return len(self.rects)

    def __repr__(self):
        return "BitmapUpdate(rects={0})".format(len(self.rects))


# ---------------------------------------------------------------------------
# PaletteUpdate — only meaningful in 8-bit color sessions
# ---------------------------------------------------------------------------

class PaletteUpdate(object):
    """Full 256-entry palette delivered when the session uses 8-bit color."""

    def __init__(self, entries):
        # entries: list of (r, g, b) tuples, length 256.
        if len(entries) != 256:
            raise ValueError(
                "Palette must have 256 entries, got {0}".format(len(entries)))
        self.entries = list(entries)

    def __getitem__(self, idx):
        return self.entries[idx]


# ---------------------------------------------------------------------------
# SurfaceBits — the GFX pipeline's encoded-frame path
# ---------------------------------------------------------------------------

class SurfaceBits(object):
    """
    One SurfaceBits command from the rdpgfx pipeline.

    The payload is encoded — FreeRDP doesn't decode H.264 / RemoteFX
    Progressive for you in Python land. You wire your decoder of choice
    (PyAV / libav, gstreamer, OpenH264) and feed `payload` into it.

    Attributes:
      x, y, width, height: destination rectangle on the virtual display.
      codec_id: numeric codec identifier matching FreeRDP's
                RDP_CODEC_ID_* (1=NSCodec, 3=RemoteFX, 0xB=Progressive,
                0xC=H.264, 0xD=AVC444).
      codec_name: human-readable codec name.
      bpp: bits per pixel (post-decode).
      pixel_format: one of PixelFormat constants.
      payload: raw encoded bytes.
    """

    # Subset of include/freerdp/codec/codecs.h that we map to friendly names.
    _CODEC_NAMES = {
        0x00: "uncompressed",
        0x01: "nscodec",
        0x03: "remotefx",
        0x0A: "jpeg",
        0x0B: "remotefx-progressive",
        0x0C: "h264",
        0x0D: "h264-avc444",
        0x0E: "alpha",
        0x10: "planar",
    }

    def __init__(self, x, y, width, height, bpp, pixel_format,
                 codec_id, payload, frame_id=0):
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.bpp = bpp
        self.pixel_format = pixel_format
        self.codec_id = codec_id
        self.codec_name = self._CODEC_NAMES.get(
            codec_id, "unknown-{0:02x}".format(codec_id))
        self.payload = payload
        self.frame_id = frame_id

    def __repr__(self):
        return ("SurfaceBits(x={0}, y={1}, w={2}, h={3}, codec={4!r}, "
                "len={5})".format(
                    self.x, self.y, self.width, self.height,
                    self.codec_name, len(self.payload)))


# ---------------------------------------------------------------------------
# PointerUpdate — cursor sprite changes
# ---------------------------------------------------------------------------

class PointerUpdate(object):
    """
    Cursor sprite delivered by the server. Two flavors:
      - 'system': a Win32 system cursor ID (pointer to your renderer's
                  default cursors).
      - 'sprite': a custom cursor with width, height, hot_x, hot_y and
                  raw RGBA bytes.
    """

    def __init__(self, kind, system_id=None, width=0, height=0,
                 hot_x=0, hot_y=0, rgba=b""):
        if kind not in ("system", "sprite", "hidden"):
            raise ValueError("kind must be 'system', 'sprite', or 'hidden'")
        self.kind = kind
        self.system_id = system_id
        self.width = width
        self.height = height
        self.hot_x = hot_x
        self.hot_y = hot_y
        self.rgba = rgba

    def __repr__(self):
        if self.kind == "system":
            return "PointerUpdate(system, id={0})".format(self.system_id)
        if self.kind == "hidden":
            return "PointerUpdate(hidden)"
        return "PointerUpdate(sprite, {0}x{1}, hot={2},{3})".format(
            self.width, self.height, self.hot_x, self.hot_y)


__all__ = [
    "PixelFormat",
    "BitmapRect", "BitmapUpdate",
    "PaletteUpdate",
    "SurfaceBits",
    "PointerUpdate",
]

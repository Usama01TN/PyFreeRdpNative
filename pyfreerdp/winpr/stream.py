"""
Stream - a Python wrapper around WinPR's wStream.

wStream is a growable byte buffer with a position cursor, used everywhere
in FreeRDP for serializing channel PDUs. The C API has dozens of
type-specific read/write helpers (Stream_Write_UINT16, Stream_Read_UINT32_BE,
etc.); we expose a small idiomatic Python wrapper that uses struct
internally for type-safe serialization.

Typical use - building a custom channel PDU:

    s = Stream(capacity=128)
    s.write_u8(0x01)              # PDU type
    s.write_u32_le(0xDEADBEEF)    # request id
    s.write_zero_terminated_utf16("hello")
    payload = s.bytes()
    custom_channel.send(payload)

And parsing one the other side:

    s = Stream.from_bytes(buf)
    pdu_type = s.read_u8()
    request_id = s.read_u32_le()
    text = s.read_zero_terminated_utf16()

We deliberately keep this implementation Python-side rather than calling
into WinPR's Stream_Read_* / Stream_Write_* helpers. Reasons:
  1. No FFI overhead for small reads.
  2. Avoids a wStream lifetime dance (alloc on the C side, free on Python
     GC) for what's essentially a glorified bytes buffer.
  3. Works in environments where libwinpr isn't loaded (testing).

The Stream class is byte-compatible with what WinPR's wStream produces,
so payloads built here can be passed straight to the channel send path.
"""

import struct

from ..errors import WinPRError


class StreamError(WinPRError):
    """Raised on out-of-bounds reads, writes past capacity etc."""


class Stream(object):
    """Growable byte buffer with a position cursor."""

    def __init__(self, capacity=64, data=None):
        if data is not None:
            if isinstance(data, str):
                data = data.encode("utf-8")
            self._buf = bytearray(data)
            self._pos = len(self._buf)
        else:
            self._buf = bytearray()
            self._pos = 0
        # `capacity` is just a hint for pre-allocation; bytearray grows
        # automatically. Reserve up front to avoid early reallocs.
        if capacity > len(self._buf):
            # bytearray doesn't have reserve(); growing via slice extension
            # then truncating is the trick.
            need = capacity - len(self._buf)
            self._buf.extend(b"\x00" * need)
            del self._buf[capacity - need:]   # truncate back

    # --- factories -------------------------------------------------------

    @classmethod
    def from_bytes(cls, data):
        s = cls(data=data)
        s._pos = 0
        return s

    # --- introspection ---------------------------------------------------

    def __len__(self):
        return len(self._buf)

    def position(self):
        return self._pos

    def set_position(self, pos):
        if pos < 0 or pos > len(self._buf):
            raise StreamError(
                "set_position({0}) out of range [0, {1}]".format(
                    pos, len(self._buf)))
        self._pos = pos

    def remaining(self):
        return len(self._buf) - self._pos

    def bytes(self):
        """Return a bytes copy of the entire buffer (positions ignored)."""
        return bytes(self._buf)

    # --- write helpers ---------------------------------------------------

    def _ensure(self, n):
        need = self._pos + n - len(self._buf)
        if need > 0:
            self._buf.extend(b"\x00" * need)

    def write_bytes(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._ensure(len(data))
        self._buf[self._pos:self._pos + len(data)] = data
        self._pos += len(data)

    def write_u8(self, value):
        self._ensure(1)
        struct.pack_into("<B", self._buf, self._pos, value & 0xFF)
        self._pos += 1

    def write_u16_le(self, value):
        self._ensure(2)
        struct.pack_into("<H", self._buf, self._pos, value & 0xFFFF)
        self._pos += 2

    def write_u16_be(self, value):
        self._ensure(2)
        struct.pack_into(">H", self._buf, self._pos, value & 0xFFFF)
        self._pos += 2

    def write_u32_le(self, value):
        self._ensure(4)
        struct.pack_into("<I", self._buf, self._pos, value & 0xFFFFFFFF)
        self._pos += 4

    def write_u32_be(self, value):
        self._ensure(4)
        struct.pack_into(">I", self._buf, self._pos, value & 0xFFFFFFFF)
        self._pos += 4

    def write_u64_le(self, value):
        self._ensure(8)
        struct.pack_into("<Q", self._buf, self._pos,
                         value & 0xFFFFFFFFFFFFFFFF)
        self._pos += 8

    def write_zero_terminated_utf16(self, text):
        """Write text as UTF-16LE followed by a UTF-16 NUL (two zero bytes)."""
        if not isinstance(text, str):
            text = text.decode("utf-8") if isinstance(text, bytes) else str(text)
        encoded = text.encode("utf-16-le")
        self.write_bytes(encoded)
        self.write_u16_le(0)

    # --- read helpers ----------------------------------------------------

    def _need(self, n):
        if self.remaining() < n:
            raise StreamError(
                "read past end: need {0} bytes, have {1}".format(
                    n, self.remaining()))

    def read_bytes(self, n):
        self._need(n)
        result = bytes(self._buf[self._pos:self._pos + n])
        self._pos += n
        return result

    def read_u8(self):
        self._need(1)
        v = self._buf[self._pos]
        self._pos += 1
        return v

    def read_u16_le(self):
        self._need(2)
        (v,) = struct.unpack_from("<H", self._buf, self._pos)
        self._pos += 2
        return v

    def read_u16_be(self):
        self._need(2)
        (v,) = struct.unpack_from(">H", self._buf, self._pos)
        self._pos += 2
        return v

    def read_u32_le(self):
        self._need(4)
        (v,) = struct.unpack_from("<I", self._buf, self._pos)
        self._pos += 4
        return v

    def read_u32_be(self):
        self._need(4)
        (v,) = struct.unpack_from(">I", self._buf, self._pos)
        self._pos += 4
        return v

    def read_u64_le(self):
        self._need(8)
        (v,) = struct.unpack_from("<Q", self._buf, self._pos)
        self._pos += 8
        return v

    def read_zero_terminated_utf16(self):
        """Read up to a UTF-16 NUL (two zero bytes), returning the text."""
        start = self._pos
        while True:
            self._need(2)
            if (self._buf[self._pos] == 0 and
                    self._buf[self._pos + 1] == 0):
                end = self._pos
                self._pos += 2
                break
            self._pos += 2
        chunk = bytes(self._buf[start:end])
        return chunk.decode("utf-16-le")

    # --- repr ------------------------------------------------------------

    def __repr__(self):
        return "Stream(len={0}, pos={1})".format(
            len(self._buf), self._pos)

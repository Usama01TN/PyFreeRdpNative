"""
Connect, let the desktop paint, save it as screenshot.bmp.

    python examples/screenshot.py <host> <user> <password> [port]

Uses FreeRDP's software GDI: gdi_init() registers the update callbacks and
composes into a BGRX32 framebuffer that rdpGdi (freerdp/gdi/gdi.h) exposes.
"""
import ctypes
import struct
import sys
import time

from _common import close, connect

from pyfreerdpnative.freerdp.codec import color as COLOR

api, ctx = connect(sys.argv, software_gdi=True)
api.gdi_init(ctx.contents.instance, COLOR.PIXEL_FORMAT_BGRX32)

deadline = time.time() + 3.0                     # give the server time to paint
while time.time() < deadline and api.freerdp_check_event_handles(ctx):
    time.sleep(0.02)

g = ctx.contents.gdi.contents                    # rdpGdi, typed
w, h, stride = g.width, g.height, g.stride
raw = ctypes.string_at(g.primary_buffer, stride * h)

# 24-bit bottom-up BMP from the BGRX framebuffer
pad = (-w * 3) % 4
rows = []
for y in range(h - 1, -1, -1):
    row = raw[y * stride:y * stride + w * 4]
    rows.append(b"".join(row[x * 4:x * 4 + 3] for x in range(w)) + b"\0" * pad)
pixels = b"".join(rows)
with open("screenshot.bmp", "wb") as fh:
    fh.write(struct.pack("<2sIHHI", b"BM", 54 + len(pixels), 0, 0, 54))
    fh.write(struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(pixels), 2835, 2835, 0, 0))
    fh.write(pixels)
print("screenshot.bmp: {0}x{1}".format(w, h))
close(api, ctx)

"""
Connect, press Enter, move the mouse and click.

    python -m pyfreerdpnative.examples.send_input <host> <user> <password> [port]

Scancodes come from freerdp/scancode.h, flags from freerdp/input.h.
"""
import sys
import time

from pyfreerdpnative.freerdp import input as INPUT
from pyfreerdpnative.freerdp import scancode as SC
from pyfreerdpnative.freerdp.codec import color as COLOR

from ._common import close, connect


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)

    api, ctx = connect(argv, software_gdi=True)
    api.gdi_init(ctx.contents.instance, COLOR.PIXEL_FORMAT_BGRX32)   # consume server updates
    inp = ctx.contents.input                                          # rdpInput* from the layout

    down = api.freerdp_input_send_keyboard_event_ex(inp, True, False, SC.RDP_SCANCODE_RETURN)
    api.freerdp_check_event_handles(ctx)
    up = api.freerdp_input_send_keyboard_event_ex(inp, False, False, SC.RDP_SCANCODE_RETURN)
    print("Enter: press {0}, release {1}".format("ok" if down else "failed", "ok" if up else "failed"))

    api.freerdp_input_send_mouse_event(inp, INPUT.PTR_FLAGS_MOVE, 200, 150)
    api.freerdp_check_event_handles(ctx)
    api.freerdp_input_send_mouse_event(inp, INPUT.PTR_FLAGS_DOWN | INPUT.PTR_FLAGS_BUTTON1, 200, 150)
    api.freerdp_input_send_mouse_event(inp, INPUT.PTR_FLAGS_BUTTON1, 200, 150)
    print("left click at (200,150): sent")

    for _ in range(10):                                               # let the PDUs go out
        if not api.freerdp_check_event_handles(ctx):
            break
        time.sleep(0.05)
    close(api, ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())

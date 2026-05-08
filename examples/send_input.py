"""
send_input.py - connect, then drive the remote session with mouse + keyboard.

Demonstrates:
  - scancode-based key injection
  - unicode key injection
  - mouse move + click

Run:
    python examples/send_input.py rdp.example.com alice hunter2

Style: Py2-compatible syntax.
"""

import sys
import threading
import time

from pyfreerdp import RdpClient, RdpSettings


# A handful of common Microsoft RDP scancodes. Full table is in
# https://learn.microsoft.com/en-us/windows/win32/inputdev/about-keyboard-input
SCANCODE_LWIN = 0x5B
SCANCODE_R = 0x13
SCANCODE_ENTER = 0x1C


def hold_and_release(client, scancode, hold_seconds=0.05):
    """Press a key, sleep, release. The standard down/up pair."""
    client.send_key(scancode, pressed=True)
    time.sleep(hold_seconds)
    client.send_key(scancode, pressed=False)


def type_string(client, text):
    """Type a Python str via Unicode events. No layout translation needed."""
    for ch in text:
        client.send_unicode(ord(ch), pressed=True)
        client.send_unicode(ord(ch), pressed=False)


def main(argv):
    if len(argv) < 4:
        print("Usage: {0} <host> <username> <password>".format(argv[0]))
        return 1

    settings = RdpSettings(
        host=argv[1], username=argv[2], password=argv[3],
        ignore_certificate=True,
    )

    with RdpClient(settings) as client:
        # Start the event loop on a thread so input + pump run in parallel.
        loop_thread = threading.Thread(
            target=client.run_event_loop,
            kwargs={"timeout": 30.0},
            name="pyfreerdp-loop",
        )
        loop_thread.daemon = True
        loop_thread.start()

        # Give the connection a moment to settle.
        time.sleep(2.0)

        # Win+R -> Run dialog
        print("Pressing Win+R")
        client.send_key(SCANCODE_LWIN, pressed=True, extended=True)
        hold_and_release(client, SCANCODE_R)
        client.send_key(SCANCODE_LWIN, pressed=False, extended=True)
        time.sleep(0.5)

        # Type "notepad"
        print("Typing 'notepad'")
        type_string(client, "notepad")
        time.sleep(0.2)

        # Enter
        hold_and_release(client, SCANCODE_ENTER)
        time.sleep(2.0)

        # Move + click somewhere benign.
        print("Mouse move and left-click")
        client.send_mouse_move(400, 300)
        time.sleep(0.1)
        client.send_mouse_button("left", 400, 300, pressed=True)
        time.sleep(0.05)
        client.send_mouse_button("left", 400, 300, pressed=False)

        client.stop()
        loop_thread.join(timeout=5.0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""
clipboard_sync.py - bidirectional clipboard sync via the cliprdr channel.

Demonstrates:
  * Attaching ClipboardChannel to an RdpClient
  * Pushing a Python str to the remote clipboard with set_text()
  * Reading the remote clipboard with get_text()

Run:
    python examples/clipboard_sync.py rdp.example.com alice hunter2

Style: Py2-compatible syntax.
"""

import sys
import threading
import time

from pyfreerdp import RdpClient, RdpSettings, ClipboardChannel


def main(argv):
    if len(argv) < 4:
        print("Usage: {0} <host> <username> <password>".format(argv[0]))
        return 1

    clipboard = ClipboardChannel(enable_text=True, enable_html=False)

    settings = RdpSettings(
        host=argv[1], username=argv[2], password=argv[3],
        ignore_certificate=True,
        channels=[clipboard],
    )

    with RdpClient(settings) as client:
        # Run the event loop on a worker thread - clipboard exchange
        # happens on whatever thread is pumping events.
        loop = threading.Thread(
            target=client.run_event_loop,
            kwargs={"timeout": 30.0},
            name="pyfreerdp-loop",
        )
        loop.daemon = True
        loop.start()

        time.sleep(2.0)

        # Push a string to the remote clipboard.
        clipboard.set_text(
            "Hello from Python at {0}".format(time.ctime()))
        print("[cliprdr] Pushed text to remote.")

        # Wait up to 10s for the remote to send us back something.
        text = clipboard.get_text(timeout=10.0)
        if text is None:
            print("[cliprdr] No clipboard text from remote within 10s.")
        else:
            print("[cliprdr] Remote clipboard says: {0!r}".format(text))

        client.stop()
        loop.join(timeout=5.0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

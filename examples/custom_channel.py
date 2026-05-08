"""
custom_channel.py - bring-your-own virtual channel.

Demonstrates:
  * Registering a custom channel name (here: "PYECHO")
  * Sending bytes via .send()
  * Receiving bytes via the on_data callback

The remote side needs to register a matching channel handler for this to
do anything useful. This example just shows the client-side plumbing.

Run:
    python examples/custom_channel.py rdp.example.com alice hunter2

Style: Py2-compatible syntax.
"""

import sys
import threading
import time

from pyfreerdp import RdpClient, RdpSettings, CustomChannel


def main(argv):
    if len(argv) < 4:
        print("Usage: {0} <host> <username> <password>".format(argv[0]))
        return 1

    received = []

    def on_data(buf, flags):
        received.append((buf, flags))
        print("[PYECHO] received {0} bytes (flags=0x{1:08X})".format(
            len(buf), flags))

    pyecho = CustomChannel(name=b"PYECHO", on_data=on_data, dynamic=False)

    settings = RdpSettings(
        host=argv[1], username=argv[2], password=argv[3],
        ignore_certificate=True,
        channels=[pyecho],
    )

    with RdpClient(settings) as client:
        loop = threading.Thread(
            target=client.run_event_loop,
            kwargs={"timeout": 30.0},
            name="pyfreerdp-loop",
        )
        loop.daemon = True
        loop.start()

        time.sleep(2.0)

        for i in range(3):
            payload = "ping #{0} at t={1:.3f}".format(i, time.time())
            print("[PYECHO] sending: {0!r}".format(payload))
            pyecho.send(payload.encode("utf-8"))
            time.sleep(0.5)

        client.stop()
        loop.join(timeout=5.0)

    print("Total received: {0} messages".format(len(received)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""
basic_connect.py - minimal example: connect, idle, disconnect.

Usage:
    python examples/basic_connect.py rdp.example.com alice hunter2
    python examples/basic_connect.py rdp.example.com alice hunter2 CORP

Style: Py2-compatible syntax; runs only on Python 3 in practice.
"""

import sys

from pyfreerdp import RdpClient, RdpSettings


def main(argv):
    if len(argv) < 4:
        print("Usage: {0} <host> <username> <password> [domain]".format(argv[0]))
        return 1

    host = argv[1]
    username = argv[2]
    password = argv[3]
    domain = argv[4] if len(argv) > 4 else ""

    settings = RdpSettings(
        host=host,
        username=username,
        password=password,
        domain=domain,
        # Local development: skip cert verification. Don't do this in
        # production - configure a proper CA chain instead.
        ignore_certificate=True,
    )

    print("Connecting to {0} as {1}...".format(host, username))
    with RdpClient(settings) as client:
        print("Connected. Pumping events for 10s...")
        client.run_event_loop(timeout=10.0)
        print("Disconnecting.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

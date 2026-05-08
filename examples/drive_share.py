"""
drive_share.py - share a local directory with the remote session over rdpdr.

Demonstrates:
  * Constructing a DriveRedirection record
  * Wrapping it in a DriveRedirectionChannel
  * Attaching the channel to RdpSettings.channels

The remote will see "\\\\TSCLIENT\\share" backed by the local --share-path
directory. Reads/writes go through FreeRDP's `drive` plugin to the local
filesystem. Pass --read-only to make the redirection one-way.

Run:
    python examples/drive_share.py rdp.example.com alice hunter2 \\
        --share-path /home/me/shared

Style: Py2-compatible syntax.
"""

import sys
import argparse

from pyfreerdp import (
    RdpClient,
    RdpSettings,
    DriveRedirection,
    DriveRedirectionChannel,
)


def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument("host")
    p.add_argument("username")
    p.add_argument("password")
    p.add_argument("--share-path", required=True,
                   help="absolute local path to expose")
    p.add_argument("--share-name", default="share",
                   help="name shown on the remote (<= 8 chars)")
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--timeout", type=float, default=30.0)
    args = p.parse_args(argv[1:])

    drive = DriveRedirection(
        name=args.share_name,
        local_path=args.share_path,
        read_only=args.read_only,
    )

    settings = RdpSettings(
        host=args.host,
        username=args.username,
        password=args.password,
        ignore_certificate=True,
        channels=[DriveRedirectionChannel(drives=[drive])],
    )

    print("Sharing {0!r} as drive {1!r} (mode={2})".format(
        args.share_path, args.share_name,
        "ro" if args.read_only else "rw"))
    with RdpClient(settings) as client:
        client.run_event_loop(timeout=args.timeout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

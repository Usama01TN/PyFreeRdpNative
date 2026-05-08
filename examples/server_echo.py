"""
server_echo.py - minimal RDP server that accepts connections and idles.

Production-quality RDP server is large; this example shows how the
binding stitches together to:
  * load libfreerdp-server3
  * bind a listener
  * accept peers
  * apply per-peer settings (cert, security mask, desktop size)

For real graphics output you need a screen-capture source; that's out
of scope. This server accepts the handshake, idles for 60 seconds, and
disconnects cleanly.

Run:
    # Generate a self-signed cert for local testing:
    openssl req -newkey rsa:2048 -nodes -keyout server.key \\
        -x509 -days 365 -out server.crt -subj "/CN=localhost"
    python examples/server_echo.py 0.0.0.0 3389 server.crt server.key

Style: Py2-compatible syntax.
"""

import sys
import time

from pyfreerdp import RdpServer, RdpServerSettings, RdpError


def handle_peer(peer):
    """Per-peer worker. Runs on a daemon thread spawned by the server."""
    try:
        with peer:
            print("[server] Peer connected: os={0}".format(peer.os_type))
            peer.run(timeout=60.0)
            print("[server] Peer session ended cleanly")
    except RdpError as e:
        print("[server] Peer error: {0}".format(e))


def main(argv):
    if len(argv) < 5:
        print("Usage: {0} <bind_addr> <port> <cert.pem> <key.pem>".format(argv[0]))
        return 1

    settings = RdpServerSettings(
        bind_address=argv[1],
        port=int(argv[2]),
        certificate_file=argv[3],
        private_key_file=argv[4],
        width=1280,
        height=720,
        color_depth=32,
    )

    print("[server] Listening on {0}:{1}".format(
        settings.bind_address, settings.port))
    with RdpServer(settings, handle_peer) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("[server] Ctrl-C - shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""
Connect to an RDP server and disconnect.

    python -m pyfreerdpnative.examples.basic_connect <host> <user> <password> [port]
"""
import sys

from ._common import close, connect


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)

    api, ctx = connect(argv)
    print("session with FreeRDP", api.version())
    close(api, ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())

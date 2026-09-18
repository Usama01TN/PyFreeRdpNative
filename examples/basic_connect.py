"""
Connect to an RDP server and disconnect.

    python examples/basic_connect.py <host> <user> <password> [port]
"""
import sys

from _common import close, connect

api, ctx = connect(sys.argv)
print("session with FreeRDP", api.version())
close(api, ctx)

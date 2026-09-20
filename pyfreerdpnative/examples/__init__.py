"""
Runnable examples, installed with the package.

    python -m pyfreerdpnative.examples                      # list them
    python -m pyfreerdpnative.examples.basic_connect HOST USER PASS [PORT]
    python -m pyfreerdpnative.examples.list_api [search]

Or from Python:

    from pyfreerdpnative.examples import basic_connect
    basic_connect.main(["basic_connect", "10.0.0.5", "alice", "secret"])

Each example is a plain module with a main(argv) function; the source is the
point, so read it with `python -m pyfreerdpnative.examples --source NAME`.
"""

EXAMPLES = ("basic_connect", "send_input", "screenshot", "list_api", "diagnose")

__all__ = ["EXAMPLES"]

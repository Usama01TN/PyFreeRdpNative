"""List the examples, run one, or print its source."""
import importlib
import os
import sys

from . import EXAMPLES

SUMMARY = {
    "basic_connect": "connect to a host and disconnect",
    "send_input": "keyboard and mouse events",
    "screenshot": "save the remote desktop as screenshot.bmp",
    "list_api": "search prototypes and constants, see which library exports what",
}


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    args = argv[1:]

    if args and args[0] in ("--source", "-s"):
        if len(args) < 2 or args[1] not in EXAMPLES:
            sys.exit("usage: python -m pyfreerdpnative.examples --source <name>")
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args[1] + ".py")
        with open(path) as fh:
            sys.stdout.write(fh.read())
        return 0

    if args and args[0] in EXAMPLES:
        module = importlib.import_module("pyfreerdpnative.examples." + args[0])
        # argv[0] names the example so its usage message is accurate whichever
        # way it was started
        return module.main(["python -m pyfreerdpnative.examples " + args[0]] + args[1:])

    print("pyfreerdpnative examples:\n")
    for name in EXAMPLES:
        print("  {0:<16} {1}".format(name, SUMMARY.get(name, "")))
    print("\nrun:    python -m pyfreerdpnative.examples <name> [arguments]")
    print("        python -m pyfreerdpnative.examples.<name> [arguments]")
    print("source: python -m pyfreerdpnative.examples --source <name>")
    return 0


if __name__ == "__main__":
    sys.exit(main())

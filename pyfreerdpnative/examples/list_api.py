"""
Inspect the generated bindings and the libraries they load.

    python -m pyfreerdpnative.examples.list_api [search]

Shows which library exports each function, and where in the header tree a
name is declared - useful when translating a C snippet to Python.
"""
import sys

import pyfreerdpnative as P
from pyfreerdpnative import load


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)

    api = load()
    print(api)
    print("libraries:", ", ".join("{0} ({1} symbols)".format(k, len(v)) for k, v in api.bound.items()))

    needle = argv[1] if len(argv) > 1 else "freerdp_connect"
    print("\nprototypes matching {0!r}:".format(needle))
    for name in sorted(n for n in P.PROTOTYPES if needle.lower() in n.lower())[:40]:
        res, args, variadic = P.PROTOTYPES[name]
        where = api.library_of(name) or "(not exported by the loaded build)"
        print("  {0}({1}{2}) -> {3}   [{4}]".format(
            name, ", ".join(getattr(a, "__name__", str(a)) for a in args),
            ", ..." if variadic else "", getattr(res, "__name__", res), where))

    print("\nconstants matching {0!r}:".format(needle))
    for name in sorted(n for n in dir(P.constants) if needle.lower() in n.lower())[:20]:
        v = getattr(P.constants, name)
        print("  {0} = {1}".format(name, hex(v) if isinstance(v, int) else repr(v)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Version metadata for pyfreerdp.

Style note: this entire package is written in Python 2.7-compatible syntax
(no f-strings, no type annotations, no dataclasses, no walrus, no PEP-604
unions, no `match`, no positional-only parameters). It runs only on
Python 3 — Python 2 itself is end-of-life — but the syntax is restricted
to the subset that would also parse on 2.7. This is a project style choice;
see README for rationale.
"""

# Pyfreerdp's own version. Independent of the FreeRDP version it wraps.
__version__ = "0.2.0"

# The FreeRDP release this version of pyfreerdp was tested against. Other
# versions in the same major series are likely to work but only this one is
# guaranteed by CI.
FREERDP_VERIFIED_VERSION = "3.16.0"

# Anything older than this is rejected at load time. Keep aligned with the
# accessor functions / setting IDs we depend on.
FREERDP_MIN_VERSION = (3, 0, 0)

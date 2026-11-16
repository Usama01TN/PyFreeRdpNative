"""
setup.py - compatibility shim; all metadata lives in pyproject.toml.
pyfreerdpnative is a pure-Python package (ctypes bindings generated from the
FreeRDP headers) plus the prebuilt FreeRDP libraries in pyfreerdpnative/_libs.
It never links against libpython, so there is no C extension to build and no
per-interpreter wheel: `python -m build` produces a single py3-none-any wheel,
and scripts/package_wheels.py turns that into one py3-none-<platform> wheel
per platform by adding the matching _libs and re-tagging.

    python -m build                      # sdist + py3-none-any wheel (no libs)
    python scripts/package_wheels.py artifacts/ -o dist/ --all-variants

Style: Py2-compatible syntax; runs on Python 3.
"""
from setuptools import setup

setup()

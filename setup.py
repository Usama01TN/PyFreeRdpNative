"""
Editable setup.py kept alongside pyproject.toml so users can opt into building
FreeRDP at install time:

    PYFREERDP_BUILD_FREERDP=1 pip install .

Without that flag, this is a pure metadata install - pyproject.toml does the
real work via the setuptools backend. We only step in to run the C build when
explicitly requested. This keeps `pip install pyfreerdp` fast in CI and on
machines that already have FreeRDP from their package manager.

Style: Py2-compatible syntax; runs only on Python 3 in practice.
"""
import os
import subprocess
import sys
from os import path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithFreeRDP(build_py):
    """build_py subclass that optionally compiles FreeRDP first."""

    def run(self):
        if os.environ.get("PYFREERDP_BUILD_FREERDP") == "1":
            print("[pyfreerdp] PYFREERDP_BUILD_FREERDP=1 - "
                  "building FreeRDP from source")
            script = path.join(
                path.dirname(path.abspath(__file__)),
                "scripts", "build_freerdp.py")
            if not path.isfile(script):
                raise SystemExit(
                    "Build script missing at {0}".format(script))
            ref = os.environ.get("PYFREERDP_FREERDP_REF", "3.16.0")
            cmd = [sys.executable, script,
                   "--ref", ref, "--target", "host"]
            subprocess.check_call(cmd)
        build_py.run(self)


setup(cmdclass={"build_py": BuildWithFreeRDP})

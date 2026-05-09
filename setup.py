"""
setup.py - alongside pyproject.toml.

Two opt-in modes via environment variables:

  PYFREERDP_BUILD_FREERDP=1
      Triggers scripts/build_freerdp.py at build time. Compiles FreeRDP
      from source and stages the artifacts into pyfreerdp/_libs/. Used
      by power-users who want a from-source build without going through
      the cibuildwheel pipeline.

  PYFREERDP_BUILD_BUNDLED=1
      Compiles a small C extension (pyfreerdp/_native.c) and includes it
      in the wheel. The presence of this extension forces setuptools to
      emit a platform-tagged wheel (e.g. manylinux_2_28_x86_64 instead
      of py3-none-any), which is what we want for the BUNDLED wheel
      distribution that ships libfreerdp inside the wheel.

      cibuildwheel sets this in CIBW_ENVIRONMENT before each platform's
      wheel build.

Without either flag this is a pure metadata install, identical to what
pyproject.toml's setuptools backend produces on its own. That preserves
the universal `py3-none-any` wheel for users on apt/brew/vcpkg.

Style: Py2-compatible syntax; runs on Python 3.
"""
import os
import subprocess
import sys
from os import path

from setuptools import Extension, setup
from setuptools.command.build_py import build_py


def _bundled_mode():
    """True if we should build the wheel as a bundled (platform-tagged) wheel."""
    return os.environ.get("PYFREERDP_BUILD_BUNDLED") == "1"


def _native_extension():
    """Construct the dummy Extension if bundled mode is on, else None."""
    if not _bundled_mode():
        return None
    # Compile-time defines are populated by the cibuildwheel scripts so
    # the resulting _native module reports which FreeRDP version + which
    # platform tag this wheel was built for.
    define_macros = []
    fv = os.environ.get("PYFREERDP_BUILT_FREERDP_VERSION", "")
    if fv:
        define_macros.append(
            ("PYFREERDP_BUILT_FREERDP_VERSION", '"{0}"'.format(fv)))
    pt = os.environ.get("PYFREERDP_BUILT_PLATFORM_TAG", "")
    if pt:
        define_macros.append(
            ("PYFREERDP_BUILT_PLATFORM_TAG", '"{0}"'.format(pt)))
    return Extension(
        "pyfreerdp._native",
        sources=["pyfreerdp/_native.c"],
        define_macros=define_macros,
    )


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


# Pull together kwargs for setup(). pyproject.toml supplies most metadata
# in PEP 621 [project] form; we only override what's dynamic.
setup_kwargs = {
    "cmdclass": {"build_py": BuildWithFreeRDP},
}

ext = _native_extension()
if ext is not None:
    setup_kwargs["ext_modules"] = [ext]
    # Including a C extension means we also include compiled native
    # libraries (libfreerdp-*.so etc) as package data. Those live under
    # pyfreerdp/_libs/ and are staged by the cibuildwheel CIBW_BEFORE_ALL
    # script.
    setup_kwargs["package_data"] = {
        "pyfreerdp": [
            "_libs/*.so", "_libs/*.so.*",
            "_libs/*.dylib", "_libs/*.dylib.*",
            "_libs/*.dll",
        ],
    }
    setup_kwargs["include_package_data"] = True


setup(**setup_kwargs)

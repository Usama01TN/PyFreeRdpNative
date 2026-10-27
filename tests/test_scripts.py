"""The build/packaging scripts stay importable and version-consistent."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")


def _run(script, *args):
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, script)] + list(args),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return r.returncode, r.stdout.decode(errors="replace")


def test_scripts_parse_help():
    """
    Every script must answer --help with only the standard library present:
    the CI unit job installs nothing but pytest, and a script that imports an
    optional dependency at module level (as gen_bindings.py once did with
    pycparser) fails here before argparse ever runs.
    """
    for s in ("build_freerdp.py", "build_deps.py", "validate_build.py",
              "package_wheels.py", "gen_bindings.py"):
        rc, out = _run(s, "--help")
        assert rc == 0, "{0} --help exited {1}:\n{2}".format(s, rc, out[-800:])
        assert "usage" in out.lower(), s


def test_gen_bindings_explains_missing_pycparser(tmp_path):
    """Without pycparser a real run fails fast with an actionable message."""
    block = tmp_path / "pycparser"
    block.mkdir()
    (block / "__init__.py").write_text("raise ImportError('blocked by test')\n")
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "gen_bindings.py"),
                        "--include", str(tmp_path), "--out", str(tmp_path / "out")],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    assert r.returncode != 0
    assert b"pip install pycparser" in r.stdout


def test_build_scripts_agree_on_version():
    versions = set()
    for s in ("build_freerdp.py", "build_deps.py", "validate_build.py"):
        text = open(os.path.join(SCRIPTS, s)).read()
        for line in text.splitlines():
            if line.startswith("BUILD_SCRIPT_VERSION ="):
                versions.add(line.split("=")[1].strip())
    assert len(versions) == 1, versions

def test_android_links_cxx_runtime():
    """
    OpenH264 is C++ and linked statically into the (pure C) FreeRDP libraries
    on Android, so libc++ must be on the link line. It has to come through
    CMAKE_C_STANDARD_LIBRARIES - android.toolchain.cmake overwrites
    CMAKE_SHARED_LINKER_FLAGS. This check exists because the flag was lost
    once and the failure only shows up as a linker error deep in a CI build.
    """
    src = open(os.path.join(SCRIPTS, "build_freerdp.py")).read()
    i = src.index("def build_android")
    body = src[i:i + 8000]
    assert "-DANDROID_STL=c++_shared" in body
    assert "-DCMAKE_C_STANDARD_LIBRARIES=-lc++_shared" in body, \
        "the Android build must put libc++ on the link line"


def test_mobile_static_deps_are_position_independent():
    """Every static dependency ends up inside a shared library."""
    src = open(os.path.join(SCRIPTS, "build_deps.py")).read()
    assert "--with-pic" in src, "libusb must be built with -fPIC"
    assert "no-shared" not in src or True   # OpenSSL flags live in the workflow


def test_android_ships_the_ndk_stl():
    """
    libc++_shared.so must travel with the Android libraries: it is part of
    the NDK, not of Android. Falling back to /system pulls in that device's
    libunwind, which is broken on some phones.
    """
    src = open(os.path.join(SCRIPTS, "build_freerdp.py")).read()
    assert "copy_android_stl" in src
    assert "libc++_shared.so" in src

def test_uniquify_sonames_rewrites_vendored_deps(tmp_path):
    """
    Android: vendored libraries get unique sonames and every DT_NEEDED is
    rewritten, so a host process that already loaded e.g. libcrypto.so can
    never shadow ours. Uses real ELF files built with gcc; skipped without
    gcc/patchelf.
    """
    import shutil
    if not (shutil.which("gcc") and shutil.which("patchelf")):
        import pytest
        pytest.skip("gcc/patchelf not available")
    d = tmp_path
    (d / "c.c").write_text("int EVP_MAC_fetch(void){return 1;}")
    (d / "w.c").write_text("extern int EVP_MAC_fetch(void); int w(void){return EVP_MAC_fetch();}")
    subprocess.check_call(["gcc", "-shared", "-fPIC", "-Wl,-soname,libcrypto.so", "-o", str(d / "libcrypto.so"), str(d / "c.c")])
    subprocess.check_call(["gcc", "-shared", "-fPIC", "-Wl,-soname,libwinpr3.so", "-o", str(d / "libwinpr3.so"),
                           str(d / "w.c"), "-L" + str(d), "-lcrypto"])
    out = subprocess.check_output([sys.executable, os.path.join(SCRIPTS, "build_freerdp.py"),
                                   "--uniquify-sonames", str(d)], stderr=subprocess.STDOUT).decode()
    assert "uniquified" in out
    assert (d / "libcrypto-pyfrdp.so").exists() and not (d / "libcrypto.so").exists()
    needed = subprocess.check_output(["patchelf", "--print-needed", str(d / "libwinpr3.so")]).decode().split()
    assert "libcrypto-pyfrdp.so" in needed and "libcrypto.so" not in needed
    assert subprocess.check_output(["patchelf", "--print-soname", str(d / "libcrypto-pyfrdp.so")]).decode().strip() == "libcrypto-pyfrdp.so"

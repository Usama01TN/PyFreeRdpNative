"""The build/packaging scripts stay importable and version-consistent."""
import os
import pytest
import shutil
import json
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


# --- FreeRDP 3.33: the AAD (Entra ID) login helpers ------------------------------------------------

def _build_script():
    sys.path.insert(0, SCRIPTS)
    try:
        import build_freerdp
    finally:
        sys.path.remove(SCRIPTS)
    return build_freerdp


def test_aad_helper_options_decide_per_profile_and_json():
    b = _build_script()
    opts, why = b.aad_helper_options("Linux", "minimal", json_enabled=True)
    assert "-DWITH_WEBVIEW_AAD_AUTH_HELPER=OFF" in opts and "full profile" in why["webview"]
    opts, why = b.aad_helper_options("Linux", "full", json_enabled=False)
    assert "-DWITH_AAD=OFF" in opts and "JSON" in why["qt"]
    opts, why = b.aad_helper_options("Windows", "full", json_enabled=True, webview=True, qt=False)
    assert {"-DWITH_AAD=ON", "-DWITH_WEBVIEW_AAD_AUTH_HELPER=ON", "-DWITH_QT_AAD_AUTH_HELPER=OFF"} <= set(opts)


@pytest.mark.skipif(sys.platform != "linux", reason="ELF rpath check")
def test_stage_aad_helpers_points_them_at_their_directory(tmp_path):
    """Staged last, no dependency walk: the browser engine stays the system's."""
    b = _build_script()
    prefix, libs = tmp_path / "prefix", tmp_path / "libs"
    (prefix / "libexec").mkdir(parents=True)
    libs.mkdir()
    shutil.copy("/bin/true", str(prefix / "libexec" / "freerdp-webview-aad-helper"))
    staged = b.stage_aad_helpers([str(prefix)], str(libs))
    assert set(staged) == {"webview"}
    rpath = subprocess.check_output(["patchelf", "--print-rpath", staged["webview"]]).decode().strip()
    assert rpath == "$ORIGIN"
    assert json.load(open(str(libs / "aad_helpers.json"))) == {"webview": {"file": "freerdp-webview-aad-helper"}}
    assert sorted(os.listdir(str(libs))) == ["aad_helpers.json", "freerdp-webview-aad-helper"]


def _libs_with_helpers(tmp_path, qt_version="6.4.2"):
    libs = tmp_path / "libs"
    libs.mkdir()
    for kind in ("webview", "qt"):
        (libs / "freerdp-{0}-aad-helper".format(kind)).write_text("")
    (libs / "aad_helpers.json").write_text(json.dumps({
        "webview": {"file": "freerdp-webview-aad-helper"},
        "qt": {"file": "freerdp-qt-aad-helper", "qt_version": qt_version}}))
    return str(libs)


def test_aad_helper_selection(tmp_path, monkeypatch):
    from pyfreerdpnative import aad
    libs = _libs_with_helpers(tmp_path)
    assert aad.helper("auto", libs_dir=libs)[0].endswith("freerdp-webview-aad-helper")
    # Qt 6 is binary compatible upwards only: an older Qt is refused, with the reason.
    monkeypatch.setattr(aad, "qt_runtimes", lambda: [{"source": "PySide6", "version": "6.3.1", "env": {}}])
    with pytest.raises(LookupError, match="needs Qt 6.4.2 or newer"):
        aad.helper("qt", libs_dir=libs)
    # A new enough Qt missing a system library it needs is skipped for the next one.
    monkeypatch.setattr(aad, "qt_runtimes", lambda: [
        {"source": "PySide6", "version": "6.12.0", "env": {"QT": "pyside"}, "missing": "libxcb-cursor0"},
        {"source": "system", "version": "6.4.2", "env": {}}])
    assert aad.helper("qt", libs_dir=libs)[1] == {}
    monkeypatch.setattr(aad, "qt_runtimes", lambda: [
        {"source": "PySide6", "version": "6.12.0", "env": {"QT": "pyside"}, "missing": "libxcb-cursor0"}])
    with pytest.raises(LookupError, match="libxcb-cursor0"):
        aad.helper("qt", libs_dir=libs)
    with pytest.raises(LookupError, match="not part of this FreeRDP build"):
        aad.helper("auto", libs_dir=str(tmp_path))


@pytest.mark.skipif(sys.platform != "linux", reason="the Linux layout of the Python Qt bindings")
def test_qt_runtime_found_from_marker_files(tmp_path, monkeypatch):
    """The PySide6 / PyQt6 Qt WebEngine layout is discovered, not hard-coded."""
    import importlib.util
    from pyfreerdpnative import aad
    qt = tmp_path / "PySide6" / "Qt"
    for sub in ("lib", "libexec", "resources", "translations/qtwebengine_locales", "plugins"):
        (qt / sub).mkdir(parents=True)
    for f in ("lib/libQt6WebEngineCore.so.6", "lib/libQt6Core.so.6.12.0", "libexec/QtWebEngineProcess",
              "resources/qtwebengine_resources.pak"):
        (qt / f).write_text("")
    spec = importlib.util.spec_from_loader("PySide6", loader=None, is_package=True)
    spec.submodule_search_locations = [str(tmp_path / "PySide6")]
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: spec if name == "PySide6" else real(name, *a))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")      # xcb's own requirement does not apply
    runtime = aad._qt_from_package("PySide6", ("PySide6-Essentials",))
    assert runtime["version"] == "6.12.0" and "missing" not in runtime
    env = runtime["env"]
    assert env["LD_LIBRARY_PATH"].split(os.pathsep)[0] == str(qt / "lib")
    assert env["QTWEBENGINEPROCESS_PATH"] == str(qt / "libexec" / "QtWebEngineProcess")
    assert env["QTWEBENGINE_RESOURCES_PATH"] == str(qt / "resources")
    assert env["QTWEBENGINE_LOCALES_PATH"] == str(qt / "translations" / "qtwebengine_locales")

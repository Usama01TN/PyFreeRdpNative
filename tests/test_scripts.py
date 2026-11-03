"""The build/packaging scripts stay importable and version-consistent."""
import json
import os
import shutil
import subprocess
import sys

import pytest

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


# --- FreeRDP 3.33 CI failures: each test pins the cause of one ----------------------------------

def test_qt_prefix_reaches_only_the_qt_helper(tmp_path):
    """Globally, Qt's prefix leaked its ICU into winpr (u_errorName_74); as Qt6_DIR,
    Qt could not find its Tools packages. It goes to the Qt helper's directory only."""
    b = _build_script()
    qt = tmp_path / "Qt" / "6.5.3" / "gcc_64"
    for d in ("lib/cmake/Qt6", "lib/cmake/Qt6WebEngineWidgets"):
        (qt / d).mkdir(parents=True)
    # webview=False: this test is about Qt; do not probe the host for WebKitGTK
    opts, why = b.aad_helper_options("Linux", "full", json_enabled=True, webview=False, qt=True,
                                     qt_prefix=str(qt))
    assert "-DPYFREERDPNATIVE_QT_PREFIX={0}".format(str(qt).replace("\\", "/")) in opts
    assert not [o for o in opts if o.startswith(("-DCMAKE_PREFIX_PATH=", "-DQt6_DIR="))]


def test_qt_helper_patch_scopes_qt_and_makes_it_optional(tmp_path):
    b = _build_script()
    cmake = tmp_path / "client" / "common" / "qt-aad-helper" / "CMakeLists.txt"
    cmake.parent.mkdir(parents=True)
    cmake.write_text("find_package(Qt6 QUIET COMPONENTS Widgets WebEngineWidgets)\nif(Qt6_FOUND)\n"
                     "  set(L Qt6::Widgets)\nelse()\n"
                     "  find_package(Qt5 5.12 REQUIRED COMPONENTS Widgets WebEngineWidgets)\n"
                     "  set(L Qt5::Widgets)\nendif()\n")
    b._patch_qt_helper_scope(str(tmp_path))
    b._patch_qt_helper_scope(str(tmp_path))
    text = cmake.read_text()
    assert text.count('list(PREPEND CMAKE_PREFIX_PATH "${PYFREERDPNATIVE_QT_PREFIX}")') == 1
    assert "REQUIRED" not in text and "return()" in text


def test_ninja_older_than_1_10_is_not_used(tmp_path, monkeypatch):
    """Ninja 1.8.2 (AlmaLinux 8) rejects Qt AUTOMOC's multi-output rules."""
    b = _build_script()
    # A fake that this OS can run: Windows only runs extension-less scripts via PATHEXT types.
    fake, script = ((tmp_path / "ninja.cmd", "@echo {0}\r\n") if os.name == "nt"
                    else (tmp_path / "ninja", "#!/bin/sh\necho {0}\n"))
    for version, usable in (("1.8.2", False), ("1.11.1", True)):
        fake.write_text(script.format(version))
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", "{0}{1}{2}".format(tmp_path, os.pathsep, os.environ["PATH"]))
        assert b.usable_ninja() is usable, version


def test_winpr_objc_bool_patch(tmp_path):
    """objc.h first, then winpr: BOOL must follow OBJC_BOOL_IS_BOOL (macOS arm64 and x86-64)."""
    b = _build_script()
    header = tmp_path / "winpr" / "include" / "winpr" / "wtypes.h"
    header.parent.mkdir(parents=True)
    header.write_text("#if defined(OBJC_BOOL_IS_BOOL) && (OBJC_BOOL_IS_CHAR != 0)\ntypedef bool BOOL;\n#endif\n")
    b._patch_winpr_objc_bool(str(tmp_path))
    b._patch_winpr_objc_bool(str(tmp_path))
    text = header.read_text()
    assert text.count("defined(OBJC_BOOL_IS_BOOL) && OBJC_BOOL_IS_BOOL") == 1
    assert "OBJC_BOOL_IS_CHAR != 0" not in text


def test_webview_helper_links_threads(tmp_path):
    """glibc < 2.34 keeps pthread_create in libpthread: the helper must link it."""
    b = _build_script()
    cmake = tmp_path / "client" / "common" / "webview-aad-helper" / "CMakeLists.txt"
    cmake.parent.mkdir(parents=True)
    cmake.write_text("target_link_libraries(${PROJECT_NAME} PRIVATE winpr webview::core)\n")
    b._patch_webview_helper_threads(str(tmp_path))
    b._patch_webview_helper_threads(str(tmp_path))
    text = cmake.read_text()
    assert text.count("Threads::Threads") == 1 and "find_package(Threads REQUIRED)" in text


def test_h264_encoder_expectation_per_platform():
    """FreeRDP 3.33's VideoToolbox backend encodes H.264 on macOS in every edition."""
    sys.path.insert(0, SCRIPTS)
    try:
        import validate_build as v
    finally:
        sys.path.remove(SCRIPTS)
    vt = ["/System/Library/Frameworks/VideoToolbox.framework/Versions/A/VideoToolbox"]
    assert v.expected_h264_encoder("Darwin", True, False, vt) is True        # macOS + VideoToolbox
    assert v.expected_h264_encoder("Darwin", True, False, []) is False       # FreeRDP < 3.33 on macOS
    assert v.expected_h264_encoder("Windows", True, False, []) is True       # Media Foundation
    assert v.expected_h264_encoder("Linux", True, False, []) is False        # LGPL FFmpeg: no encoder
    assert v.expected_h264_encoder("Linux", False, True, []) is True         # OpenH264



def test_tools_run_by_their_resolved_path(monkeypatch):
    """have() may find a tool that cannot run as a bare name (Windows .bat/.cmd): the
    resolved path is what runs, and an unrunnable tool counts as absent, not a crash."""
    b = _build_script()
    monkeypatch.setattr(b, "tool_path", lambda tool: os.path.join(os.sep, "no", "such", tool))
    assert b._pkg_config_has("webkit2gtk-4.1") is False
    assert b.usable_ninja() is False



@pytest.mark.skipif(sys.platform != "linux", reason="the Linux layout of a Qt installation")
def test_qt_runtime_from_an_explicit_qt_directory(tmp_path, monkeypatch):
    """PYFREERDP_QT_DIR: a Qt installation (Qt installer / aqtinstall layout) is found first."""
    from pyfreerdpnative import aad
    qt = tmp_path / "Qt" / "6.5.3" / "gcc_64"
    for sub in ("lib/cmake/Qt6", "libexec", "resources", "translations/qtwebengine_locales", "plugins"):
        (qt / sub).mkdir(parents=True)
    for f in ("lib/libQt6WebEngineCore.so.6", "lib/libQt6Core.so.6.5.3", "libexec/QtWebEngineProcess",
              "resources/qtwebengine_resources.pak"):
        (qt / f).write_text("")
    monkeypatch.setenv("PYFREERDP_QT_DIR", str(qt))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    first = aad.qt_runtimes()[0]
    assert first["source"] == "PYFREERDP_QT_DIR" and first["version"] == "6.5.3"
    assert first["libdir"] == str(qt / "lib")
    assert first["env"]["QTWEBENGINEPROCESS_PATH"] == str(qt / "libexec" / "QtWebEngineProcess")


def test_validation_rejects_a_named_qt_that_is_not_usable(tmp_path, monkeypatch):
    """A Qt named for the check must be used: falling back would hide what is checked."""
    sys.path.insert(0, SCRIPTS)
    try:
        import validate_build as v
    finally:
        sys.path.remove(SCRIPTS)
    not_qt = tmp_path / "not-a-qt"
    not_qt.mkdir()
    monkeypatch.delenv("PYFREERDP_QT_DIR", raising=False)
    monkeypatch.setenv("QT_ROOT_DIR", str(not_qt))
    runtime, note, error = v.qt_runtime_for_helper(str(tmp_path))
    assert runtime is None and "not a Qt WebEngine runtime" in error


@pytest.mark.parametrize("system, files, libdir", [
    ("Darwin", ["lib/QtWebEngineCore.framework/Versions/A/Helpers/QtWebEngineProcess.app/Contents/MacOS/"
                "QtWebEngineProcess", "lib/QtWebEngineCore.framework/Versions/A/QtWebEngineCore"], "lib"),
    ("Windows", ["bin/QtWebEngineProcess.exe", "bin/Qt6WebEngineCore.dll", "bin/Qt6Core.dll"], "bin"),
])
def test_qt_installation_layouts_per_os(tmp_path, monkeypatch, system, files, libdir):
    """The Qt installer / aqtinstall layouts of macOS (framework, process 8 levels deep) and
    Windows (DLLs in bin): found from marker files, with the version from Qt's CMake files."""
    from pyfreerdpnative import aad
    monkeypatch.setattr(aad.platform, "system", lambda: system)
    monkeypatch.setattr(aad, "_EXE", ".exe" if system == "Windows" else "")
    qt = tmp_path / "Qt" / "6.5.3" / system
    (qt / "lib" / "cmake" / "Qt6").mkdir(parents=True)
    (qt / "lib" / "cmake" / "Qt6" / "Qt6ConfigVersionImpl.cmake").write_text('set(PACKAGE_VERSION "6.5.3")\n')
    for f in files:
        (qt / f).parent.mkdir(parents=True, exist_ok=True)
        (qt / f).write_text("")
    runtime = aad._qt_from_directory(str(qt))
    assert runtime is not None, system
    assert runtime["version"] == "6.5.3" and runtime["libdir"] == str(qt / libdir)
    assert runtime["env"]["QTWEBENGINEPROCESS_PATH"].endswith("QtWebEngineProcess" + aad._EXE)

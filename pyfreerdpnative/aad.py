"""
pyfreerdpnative.aad - the AAD (Entra ID) login helpers shipped with the FreeRDP libraries.

FreeRDP 3.33 signs in to Azure AD through an out-of-process helper that shows the Microsoft login
page and reports the final redirect back over a JSON-RPC pipe pair (FreeRDP's
client/common/aad-helper-protocol.md). FreeRDP looks for helpers next to the running executable,
which for a Python program is python.exe - so the helper's path is given explicitly, through the
FreeRDP_AadAuthHelper setting.

Two helpers can be shipped (see aad_helpers.json next to the libraries):

* "webview" - the operating system's browser engine: WebKitGTK on Linux (the libwebkit2gtk-4.1
  package), WKWebView on macOS, Edge WebView2 on Windows (part of Windows 10/11).
* "qt" - Qt WebEngine. Only the helper is shipped, not Qt: it runs with the system's Qt 6 or with
  the Qt of an installed PySide6-WebEngine or PyQt6-WebEngine (Qt WebEngine is a separate package
  of both since Qt 6.12/6.140; ``pip install pyfreerdpnative[qt-aad]`` installs it), or a Qt
  installation named by PYFREERDP_QT_DIR (Qt installer, aqtinstall), as long as that Qt is not older than the one
  it was built against (Qt 6 is binary compatible upwards only). helper("qt") returns the
  environment that makes the helper find that Qt.

    from pyfreerdpnative import aad
    path, env = aad.helper("auto")   # raises LookupError with the reason when there is none
"""
import ctypes.util
import importlib.util
import json
import os
import platform

try:
    from ._loader import default_libs_dir
except ImportError:  # pragma: no cover - used as a plain module
    from _loader import default_libs_dir

MANIFEST = "aad_helpers.json"
KINDS = ("webview", "qt")
_EXE = ".exe" if platform.system() == "Windows" else ""


def helpers(libs_dir=None):
    """
    The AAD helpers available next to the FreeRDP libraries.
    :param libs_dir: (str | None) The library directory; default: the one load() uses.
    :return: (dict) kind -> {"path": str, "qt_version": str (qt only)}
    """
    libs_dir = libs_dir or default_libs_dir()
    found = {}
    try:
        with open(os.path.join(libs_dir, MANIFEST)) as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        manifest = {kind: {"file": "freerdp-{0}-aad-helper{1}".format(kind, _EXE)} for kind in KINDS}
    for kind, entry in manifest.items():
        path = os.path.join(libs_dir, entry.get("file", ""))
        if kind in KINDS and os.path.isfile(path):
            item = {"path": path}
            if entry.get("qt_version"):
                item["qt_version"] = entry["qt_version"]
            found[kind] = item
    return found


def _version_tuple(text):
    parts = []
    for p in (text or "").split("."):
        if not p.isdigit():
            break
        parts.append(int(p))
    return tuple(parts)


def _find(top, names, want_dir=False, depth=6):
    """The first file (or directory) under top whose name is in names, breadth-first."""
    level = [top]
    for _ in range(depth):
        nxt = []
        for d in level:
            try:
                entries = sorted(os.listdir(d))
            except OSError:
                continue
            for e in entries:
                p = os.path.join(d, e)
                if e in names and (os.path.isdir(p) if want_dir else os.path.isfile(p)):
                    return p
                if os.path.isdir(p) and not os.path.islink(p):
                    nxt.append(p)
        level = nxt
    return None


def _qt_from_package(package, distributions):
    """
    The Qt WebEngine runtime inside an installed Python Qt binding.
    """
    spec = importlib.util.find_spec(package)
    if spec is None or not spec.submodule_search_locations:
        return None
    return _qt_runtime_in(list(spec.submodule_search_locations)[0], package, distributions)


def _qt_from_directory(root):
    """
    A Qt installation named by PYFREERDP_QT_DIR (the Qt online installer, aqtinstall, a
    distribution's prefix): e.g. ~/Qt/6.5.3/gcc_64, C:\\Qt\\6.5.3\\msvc2019_64.
    """
    return _qt_runtime_in(root, "PYFREERDP_QT_DIR", ()) if root and os.path.isdir(root) else None


def _qt_cmake_version(root):
    """The Qt version from Qt6ConfigVersionImpl.cmake, for layouts whose file names carry none."""
    import re
    for impl in (os.path.join(root, "lib", "cmake", "Qt6", "Qt6ConfigVersionImpl.cmake"),
                 os.path.join(root, "lib", "cmake", "Qt6", "Qt6ConfigVersion.cmake")):
        try:
            with open(impl) as fh:
                m = re.search(r'set\(PACKAGE_VERSION "([0-9.]+)"\)', fh.read())
            if m:
                return m.group(1)
        except OSError:
            continue
    return None


def _qt_runtime_in(root, source, distributions):
    """
    The Qt WebEngine runtime under root: its layout differs per OS and per distribution (PySide6,
    PyQt6, the Qt installer), so it is found from marker files rather than hard-coded paths.
    """
    # depth 9: on macOS it is lib/QtWebEngineCore.framework/Versions/A/Helpers/
    # QtWebEngineProcess.app/Contents/MacOS/QtWebEngineProcess
    process = _find(root, ("QtWebEngineProcess" + _EXE, "QtWebEngineProcess"), depth=9)
    if not process:
        return None  # The binding is there, its WebEngine add-on is not.
    system = platform.system()
    if system == "Darwin":
        core = _find(root, ("QtWebEngineCore.framework",), want_dir=True)
        libdir = os.path.dirname(core) if core else None
    else:
        core = _find(root, ("Qt6WebEngineCore.dll",) if system == "Windows" else ("libQt6WebEngineCore.so.6",))
        libdir = os.path.dirname(core) if core else None
    if not libdir:
        return None
    resources = _find(root, ("qtwebengine_resources.pak",))
    locales = _find(root, ("qtwebengine_locales",), want_dir=True)
    plugins = _find(root, ("plugins",), want_dir=True)
    # The Qt *core* version decides compatibility (Qt WebEngine has its own numbering, e.g.
    # 6.140): the libQt6Core file name where it carries it, else the core package's version.
    version = None
    for f in (os.listdir(libdir) if system == "Linux" else ()):
        if f.startswith("libQt6Core.so.6.") and _version_tuple(f[len("libQt6Core.so."):]):
            version = f[len("libQt6Core.so."):]
    for dist in (() if version else distributions):
        try:
            from importlib.metadata import version as dist_version
            version = dist_version(dist)
            break
        except Exception:
            continue
    version = version or _qt_cmake_version(root)
    env = {"QTWEBENGINEPROCESS_PATH": process}
    if resources:
        env["QTWEBENGINE_RESOURCES_PATH"] = os.path.dirname(resources)
    if locales:
        env["QTWEBENGINE_LOCALES_PATH"] = locales
    if plugins:
        env["QT_PLUGIN_PATH"] = plugins
    search = {"Windows": "PATH", "Darwin": "DYLD_FRAMEWORK_PATH"}.get(system, "LD_LIBRARY_PATH")
    env[search] = libdir + os.pathsep + os.environ.get(search, "") if os.environ.get(search) else libdir
    runtime = {"source": source, "version": version, "env": env, "libdir": libdir}
    missing = _missing_system_libraries(version)
    if missing:
        runtime["missing"] = missing
    return runtime


def _missing_system_libraries(version):
    """
    System libraries a Qt runtime needs and does not bring: on Linux under X11, Qt 6.5+ loads its
    xcb platform plugin only with libxcb-cursor (Qt's own requirement) - without it the helper
    cannot open its window. Wayland sessions do not use xcb.
    """
    if platform.system() != "Linux" or os.environ.get("WAYLAND_DISPLAY") \
            or os.environ.get("QT_QPA_PLATFORM", "").startswith(("wayland", "offscreen")):
        return None
    if _version_tuple(version) >= (6, 5) and not ctypes.util.find_library("xcb-cursor"):
        return "libxcb-cursor0 (e.g. sudo apt install libxcb-cursor0 / dnf install xcb-util-cursor)"
    return None


def _qt_from_system():
    """A system Qt 6 WebEngine (Linux distributions, Homebrew): found by the dynamic loader as is."""
    if platform.system() == "Windows":
        return None
    name = ctypes.util.find_library("Qt6WebEngineCore")
    if not name:
        return None
    version = None
    core = ctypes.util.find_library("Qt6Core")
    for libdir in ("/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu", "/usr/lib64", "/usr/lib",
                   "/opt/homebrew/lib", "/usr/local/lib"):
        if core and os.path.isdir(libdir):
            for f in os.listdir(libdir):
                if f.startswith("libQt6Core.so.6.") and _version_tuple(f[len("libQt6Core.so."):]):
                    version = f[len("libQt6Core.so."):]
                    break
        if version:
            break
    return {"source": "system", "version": version, "env": {}, "libdir": None}


def qt_runtimes():
    """
    Every Qt 6 WebEngine the Qt helper could use, best first: the Qt named by PYFREERDP_QT_DIR,
    an installed PySide6-WebEngine, PyQt6-WebEngine, then the system's.
    :return: (list[dict]) {"source", "version", "env", "libdir"; "missing": what it lacks}
    """
    found = []
    explicit = _qt_from_directory(os.environ.get("PYFREERDP_QT_DIR"))
    if explicit:
        found.append(explicit)
    for package, dists in (("PySide6", ("PySide6-Essentials", "PySide6")),  # Qt core, not WebEngine
                           ("PyQt6", ("PyQt6-Qt6",))):
        runtime = _qt_from_package(package, dists)
        if runtime:
            found.append(runtime)
    system = _qt_from_system()
    if system:
        found.append(system)
    return found


def helper(kind="auto", libs_dir=None):
    """
    The helper to give FreeRDP (FreeRDP_AadAuthHelper) and the environment it needs.
    :param kind: (str) "auto" (webview, else qt), "webview", "qt", or a path to any helper speaking
        FreeRDP's AAD helper protocol.
    :param libs_dir: (str | None) The library directory; default: the one load() uses.
    :return: (tuple) (path, env) - env: variables to set for the helper (empty for the webview one).
    :raises LookupError: no usable helper, with the reason.
    """
    if kind not in ("auto",) + KINDS:
        if os.path.isfile(kind):
            return kind, {}
        raise LookupError("no AAD helper at {0!r}".format(kind))
    available = helpers(libs_dir)
    reasons = []
    for candidate in (KINDS if kind == "auto" else (kind,)):
        entry = available.get(candidate)
        if entry is None:
            reasons.append("the {0} helper is not part of this FreeRDP build".format(candidate))
            continue
        if candidate == "webview":
            return entry["path"], {}
        needed = _version_tuple(entry.get("qt_version"))
        runtimes = qt_runtimes()
        # An explicitly named Qt (PYFREERDP_QT_DIR) whose version cannot be read is the user's call.
        recent = [r for r in runtimes if not needed or (r["version"] and _version_tuple(r["version"]) >= needed)
                  or (r["source"] == "PYFREERDP_QT_DIR" and not r["version"])]
        usable = [r for r in recent if not r.get("missing")]
        if usable:
            return entry["path"], dict(usable[0]["env"])
        if recent:
            reasons.append("the qt helper's Qt ({0}) needs {1}".format(
                ", ".join("{0} {1}".format(r["source"], r["version"]) for r in recent), recent[0]["missing"]))
        elif runtimes:
            reasons.append("the qt helper needs Qt {0} or newer; found {1}".format(
                entry.get("qt_version"), ", ".join("{0} {1}".format(r["source"], r["version"]) for r in runtimes)))
        else:
            reasons.append("the qt helper needs Qt 6 WebEngine: pip install \"pyfreerdpnative[qt-aad]\" "
                           "(PySide6-WebEngine), the system's Qt 6 WebEngine package, or "
                           "PYFREERDP_QT_DIR=<a Qt installation>")
    raise LookupError("; ".join(reasons) or "no AAD helper")

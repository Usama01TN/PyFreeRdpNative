"""
Locate and load the FreeRDP shared libraries across platforms.

We need up to four distinct libraries:

  * libfreerdp-client3 - client-side protocol implementation.
  * libfreerdp-server3 - server-side implementation.
  * libwinpr3          - Win32 portability layer; transitively pulled in by
                         the two above, but we open it explicitly when the
                         user wants WinPR APIs (SSPI, wStream).
  * libfreerdp-channels - older FreeRDP installs split out a channels
                         library; in 3.x the channel registration symbols
                         live inside libfreerdp-client3 / libfreerdp-server3
                         themselves, but we keep a finder for older builds.

Loading is lazy and per-role: a missing library only blocks the half that
needs it.

Search order (first hit wins) for each role:
  1. PYFREERDP_<ROLE>_LIBRARY env var (CLIENT, SERVER, WINPR)
  2. PYFREERDP_LIBRARY env var (legacy; client only)
  3. Bundled library inside the wheel (pyfreerdp/_libs/)
  4. Standard system paths via ctypes.util.find_library
  5. Platform-specific fallback dirs (Homebrew, vcpkg, etc.)
"""

import ctypes
import ctypes.util
import os
import sys

# Module-level state set by the loaders so users can introspect what got
# loaded. Three roles, three slots.
_LOADED_CLIENT_PATH = None
_LOADED_SERVER_PATH = None
_LOADED_WINPR_PATH = None


# ---------------------------------------------------------------------------
# Per-role candidate filenames
# ---------------------------------------------------------------------------

def _client_candidates():
    if sys.platform.startswith("linux") or "bsd" in sys.platform:
        return [
            "libfreerdp-client3.so.3", "libfreerdp-client3.so",
            "libfreerdp3.so.3", "libfreerdp3.so",
            "libfreerdp-client.so", "libfreerdp.so",
        ]
    if sys.platform == "darwin":
        return [
            "libfreerdp-client3.3.dylib", "libfreerdp-client3.dylib",
            "libfreerdp3.3.dylib", "libfreerdp3.dylib",
            "libfreerdp-client.dylib", "libfreerdp.dylib",
        ]
    if sys.platform == "win32":
        return [
            "freerdp-client3.dll", "freerdp3.dll",
            "libfreerdp-client3.dll", "libfreerdp3.dll",
        ]
    return ["libfreerdp-client3.so", "libfreerdp3.so"]


def _server_candidates():
    """
    Server-side library names. In FreeRDP 3.x, libfreerdp-server3 is the
    server protocol library. It's distinct from libfreerdp-shadow3 (the
    turnkey screen-share daemon) - we wrap the former.
    """
    if sys.platform.startswith("linux") or "bsd" in sys.platform:
        return [
            "libfreerdp-server3.so.3", "libfreerdp-server3.so",
            "libfreerdp-server.so",
        ]
    if sys.platform == "darwin":
        return [
            "libfreerdp-server3.3.dylib", "libfreerdp-server3.dylib",
            "libfreerdp-server.dylib",
        ]
    if sys.platform == "win32":
        return ["freerdp-server3.dll", "libfreerdp-server3.dll"]
    return ["libfreerdp-server3.so"]


def _winpr_candidates():
    """WinPR is FreeRDP's Win32 portability layer."""
    if sys.platform.startswith("linux") or "bsd" in sys.platform:
        return ["libwinpr3.so.3", "libwinpr3.so", "libwinpr.so"]
    if sys.platform == "darwin":
        return ["libwinpr3.3.dylib", "libwinpr3.dylib", "libwinpr.dylib"]
    if sys.platform == "win32":
        return ["winpr3.dll", "libwinpr3.dll"]
    return ["libwinpr3.so"]


def _bundled_lib_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "_libs")


def _extra_search_dirs():
    dirs = []
    if sys.platform == "darwin":
        dirs += ["/opt/homebrew/lib", "/usr/local/lib"]
    elif sys.platform == "win32":
        for root in (os.environ.get("VCPKG_ROOT"), r"C:\vcpkg"):
            if root:
                dirs.append(os.path.join(
                    root, "installed", "x64-windows", "bin"))
    elif sys.platform.startswith("linux"):
        dirs += [
            "/usr/local/lib", "/usr/local/lib64",
            "/usr/lib/x86_64-linux-gnu", "/usr/lib64",
        ]
    return [d for d in dirs if os.path.isdir(d)]


# ---------------------------------------------------------------------------
# Generic finder driving the per-role logic
# ---------------------------------------------------------------------------

def _is_client_role(candidates):
    """True if these candidates are client-side library filenames."""
    return not any(("server" in c) or ("winpr" in c) for c in candidates)


def _find(candidates, role_env):
    """Run the standard 5-step search for one role's candidate list."""
    # 1. Role-specific env var (always wins; may be invalid path on purpose).
    role_override = os.environ.get(role_env)
    if role_override:
        return role_override

    # 2. Legacy combined env var - only honour for the client role.
    if _is_client_role(candidates):
        legacy = os.environ.get("PYFREERDP_LIBRARY")
        if legacy:
            return legacy

    # 3. Bundled inside the wheel.
    lib_dir = _bundled_lib_dir()
    if os.path.isdir(lib_dir):
        for name in candidates:
            p = os.path.join(lib_dir, name)
            if os.path.isfile(p):
                return p

    # 4. ctypes.util.find_library.
    for name in candidates:
        stripped = name
        if stripped.startswith("lib"):
            stripped = stripped[3:]
        stripped = stripped.split(".")[0]
        found = ctypes.util.find_library(stripped)
        if found:
            return found

    # 5. Manual sweep of platform-conventional dirs.
    for d in _extra_search_dirs():
        for name in candidates:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p

    return None


# ---------------------------------------------------------------------------
# Public finders / loaders
# ---------------------------------------------------------------------------

def find_freerdp_library():
    """Path to the client-side library, or None. Backward-compatible name."""
    return _find(_client_candidates(), "PYFREERDP_CLIENT_LIBRARY")


def find_freerdp_server_library():
    """Path to the server-side library, or None."""
    return _find(_server_candidates(), "PYFREERDP_SERVER_LIBRARY")


def find_winpr_library():
    """Path to libwinpr3, or None."""
    return _find(_winpr_candidates(), "PYFREERDP_WINPR_LIBRARY")


def _load(path):
    if not path:
        return None
    try:
        if sys.platform == "win32":
            return ctypes.CDLL(path)
        # RTLD_GLOBAL so dependent symbols (libfreerdp3, libwinpr3) resolve
        # for plugins or the other half (client/server) loaded later.
        return ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
    except OSError:
        return None


def load_freerdp():
    """Load the client-side library."""
    global _LOADED_CLIENT_PATH
    path = find_freerdp_library()
    handle = _load(path)
    if handle is not None:
        _LOADED_CLIENT_PATH = path
    return handle


def load_freerdp_server():
    """Load the server-side library."""
    global _LOADED_SERVER_PATH
    path = find_freerdp_server_library()
    handle = _load(path)
    if handle is not None:
        _LOADED_SERVER_PATH = path
    return handle


def load_winpr():
    """Load libwinpr3 explicitly. Most users don't need this directly -
    it's pulled in transitively by libfreerdp-* - but the WinPR Python
    bindings (SSPI, wStream) need a handle to bind their signatures onto."""
    global _LOADED_WINPR_PATH
    path = find_winpr_library()
    handle = _load(path)
    if handle is not None:
        _LOADED_WINPR_PATH = path
    return handle


def get_loaded_library_path():
    """Path of the client library that was loaded, or None."""
    return _LOADED_CLIENT_PATH


def get_loaded_server_library_path():
    """Path of the server library that was loaded, or None."""
    return _LOADED_SERVER_PATH


def get_loaded_winpr_library_path():
    """Path of WinPR that was loaded, or None."""
    return _LOADED_WINPR_PATH


# Internal alias kept for backward compat with v2 tests.
def _candidate_names():
    """Returns client candidates (legacy alias)."""
    return _client_candidates()

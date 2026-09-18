"""
Load the FreeRDP libraries from pyfreerdpnative/_libs and attach every prototype
declared in the headers (restype + argtypes), so calls are type-checked by
ctypes and the struct/constant definitions from the header mirror apply.

    from pyfreerdpnative import load
    api = load()                      # finds pyfreerdpnative/_libs automatically
    api.freerdp_get_version_string()  # -> b'3.31.1'
    ctx = api.freerdp_client_context_new(ctypes.byref(entry))

`api.<name>` resolves the function in whichever library exports it (winpr,
freerdp, freerdp-client, freerdp-server). `api.libs` holds the raw CDLL
handles, `api.types` / `api.constants` the header definitions.
"""
import ctypes
import glob
import os
import platform

from ._core import constants, types
from ._core.functions import bind

# Load order matters: each depends on the ones before it.
LIBRARIES = ("winpr3", "freerdp3", "freerdp-client3", "freerdp-server3")

# Optional extras that some editions ship (media backends etc.). Loaded if
# present so that the core libraries can resolve them; never required.
OPTIONAL = ("winpr-tools3", "rdtk0", "uwac0")


def _ext():
    return {"Windows": ".dll", "Darwin": ".dylib"}.get(platform.system(), ".so")


def default_libs_dir():
    """
    Where the FreeRDP libraries live. Precedence:
      1. $PYFREERDP_LIBS - an explicit override always wins
      2. pyfreerdpnative/_libs next to this file (the bundled libraries)
      3. a _libs directory in a parent (source checkouts with a different layout)
    A directory only counts if it actually contains a FreeRDP library, so an
    empty placeholder cannot shadow a real location further down the list.
    """

    def has_libs(d):
        try:
            return any(f.startswith(("libfreerdp3", "freerdp3", "libwinpr3", "winpr3")) for f in os.listdir(d))
        except OSError:
            return False

    env = os.environ.get("PYFREERDP_LIBS")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, '_libs'), '_lib']
    for up in (1, 2, 3):
        candidates.append(os.path.normpath(os.path.join(here, *([".."] * up + ['_libs']))))
    for cand in candidates:
        if has_libs(cand):
            return cand
    return candidates[0]  # bundled location, even if empty (clear error later)


def find_library(libs_dir, stem):
    """First file in libs_dir for a library stem, any platform naming."""
    ext = _ext()
    patterns = [
        os.path.join(libs_dir, "lib" + stem + ext + "*"),  # libfreerdp3.so, .so.3
        os.path.join(libs_dir, "lib" + stem + "*" + ext),  # libfreerdp3.3.dylib
        os.path.join(libs_dir, stem + ext),  # freerdp3.dll
    ]
    for pat in patterns:
        hits = sorted(p for p in glob.glob(pat) if os.path.isfile(p))
        if hits:
            # prefer the plain / shortest name (libfreerdp3.so over .so.3.31.0)
            hits.sort(key=lambda p: (len(os.path.basename(p)), p))
            return hits[0]
    return None


class FreeRDP(object):
    """Bound libraries with header-derived prototypes attached."""

    def __init__(self, libs_dir=None, strict=False):
        self.libs_dir = os.path.abspath(libs_dir or default_libs_dir())
        if not os.path.isdir(self.libs_dir):
            raise OSError("FreeRDP libraries directory not found: {0}".format(
                self.libs_dir))
        self.types = types
        self.constants = constants
        self.libs = {}
        self.bound = {}
        self._lookup = {}

        if platform.system() == "Windows" and hasattr(os, "add_dll_directory"):
            self._dll_dir = os.add_dll_directory(self.libs_dir)
        mode = getattr(ctypes, "RTLD_GLOBAL", 0)

        # Core libraries first, in dependency order. dlsym() on a handle also
        # searches that library's dependencies, so whichever library is
        # bound FIRST claims a symbol - binding winpr before freerdp before
        # the client/server libs (and the optional extras last) attributes
        # every function to the library that really exports it.
        for stem in LIBRARIES + OPTIONAL:
            path = find_library(self.libs_dir, stem)
            if path is None:
                if stem in LIBRARIES:
                    raise OSError("{0} not found in {1} (files: {2})".format(
                        stem, self.libs_dir,
                        ", ".join(sorted(os.listdir(self.libs_dir))[:12])))
                continue
            try:
                lib = ctypes.CDLL(path, mode=mode)
            except OSError:
                if stem in LIBRARIES:
                    raise
                continue
            self.libs[stem] = lib
            names = bind(lib, strict=strict)
            self.bound[stem] = names
            for n in names:
                self._lookup.setdefault(n, stem)

    # --- attribute access -----------------------------------------------
    def __getattr__(self, name):
        stem = self._lookup.get(name)
        if stem is None:
            # not a header prototype: try each library raw (e.g. non-exported
            # symbols present on this platform) so callers still get something
            for lib in self.libs.values():
                try:
                    return getattr(lib, name)
                except AttributeError:
                    pass
            raise AttributeError("{0} is not exported by any loaded FreeRDP "
                                 "library".format(name))
        return getattr(self.libs[stem], name)

    def has(self, name):
        return name in self._lookup

    def library_of(self, name):
        """Which library exports a function ('freerdp3', 'winpr3', ...)."""
        return self._lookup.get(name)

    def version(self):
        return self.freerdp_get_version_string().decode("ascii", "replace")

    def __repr__(self):
        return "<FreeRDP {0} from {1}: {2} prototypes bound>".format(
            self.version(), self.libs_dir, len(self._lookup))


_default = None


def load(libs_dir=None, strict=False):
    """Load (once) and return the bound FreeRDP libraries."""
    global _default
    if libs_dir is None and _default is not None:
        return _default
    api = FreeRDP(libs_dir, strict=strict)
    if libs_dir is None:
        _default = api
    return api

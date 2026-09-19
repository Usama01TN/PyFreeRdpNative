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
            return any(f.startswith(("libfreerdp3", "freerdp3", "libwinpr3", "winpr3"))
                       for f in os.listdir(d))
        except OSError:
            return False

    env = os.environ.get("PYFREERDP_LIBS")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, '_libs'), '_lib']
    for up in (1, 2, 3):
        candidates.append(os.path.normpath(os.path.join(here, *(['..'] * up + ["_libs"]))))
    for cand in candidates:
        if has_libs(cand):
            return cand
    return candidates[0]        # bundled location, even if empty (clear error later)

def find_library(libs_dir, stem):
    """First file in libs_dir for a library stem, any platform naming."""
    ext = _ext()
    patterns = [
        os.path.join(libs_dir, "lib" + stem + ext + "*"),   # libfreerdp3.so, .so.3
        os.path.join(libs_dir, "lib" + stem + "*" + ext),   # libfreerdp3.3.dylib
        os.path.join(libs_dir, stem + ext),                 # freerdp3.dll
    ]
    for pat in patterns:
        hits = sorted(p for p in glob.glob(pat) if os.path.isfile(p))
        if hits:
            # prefer the plain / shortest name (libfreerdp3.so over .so.3.31.0)
            hits.sort(key=lambda p: (len(os.path.basename(p)), p))
            return hits[0]
    return None


# --- ELF inspection -------------------------------------------------------
# Android's linker resolves a library's DT_NEEDED entries only against
# already-loaded sonames and its own default search paths - never against the
# directory the library was loaded from. A bundled _libs therefore has to be
# loaded bottom-up: every dependency dlopen'd by absolute path BEFORE the
# library that needs it. Reading DT_SONAME / DT_NEEDED here keeps that order
# correct whatever the build shipped.

def _elf_dynamic(path):
    """(soname, [needed, ...]) from an ELF file; (None, []) if unreadable."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except (IOError, OSError):
        return None, []
    if len(data) < 64 or data[:4] != b"\x7fELF":
        return None, []                       # not ELF (Mach-O, PE, script)
    import struct
    is64 = data[4] == 2 if isinstance(data[4], int) else ord(data[4]) == 2
    little = (data[5] == 1 if isinstance(data[5], int) else ord(data[5]) == 1)
    end = "<" if little else ">"
    try:
        if is64:
            e_phoff, = struct.unpack_from(end + "Q", data, 0x20)
            e_phentsize, e_phnum = struct.unpack_from(end + "HH", data, 0x36)
        else:
            e_phoff, = struct.unpack_from(end + "I", data, 0x1C)
            e_phentsize, e_phnum = struct.unpack_from(end + "HH", data, 0x2A)
        dyn_off = dyn_size = 0
        for i in range(e_phnum):
            off = e_phoff + i * e_phentsize
            p_type, = struct.unpack_from(end + "I", data, off)
            if p_type == 2:                   # PT_DYNAMIC
                if is64:
                    dyn_off, = struct.unpack_from(end + "Q", data, off + 0x10)
                    dyn_size, = struct.unpack_from(end + "Q", data, off + 0x20)
                else:
                    dyn_off, = struct.unpack_from(end + "I", data, off + 0x08)
                    dyn_size, = struct.unpack_from(end + "I", data, off + 0x14)
                break
        if not dyn_size:
            return None, []
        entry = 16 if is64 else 8
        fmt = end + ("Q" if is64 else "I")
        strtab = strsz = 0
        raw = []
        for pos in range(dyn_off, dyn_off + dyn_size, entry):
            tag, = struct.unpack_from(fmt, data, pos)
            val, = struct.unpack_from(fmt, data, pos + entry // 2)
            if tag == 0:                      # DT_NULL
                break
            if tag == 5:                      # DT_STRTAB (virtual address)
                strtab = val
            elif tag == 10:                   # DT_STRSZ
                strsz = val
            elif tag in (1, 14):              # DT_NEEDED, DT_SONAME
                raw.append((tag, val))
        if not strtab:
            return None, []
        # translate the strtab virtual address to a file offset via PT_LOAD
        file_off = None
        for i in range(e_phnum):
            off = e_phoff + i * e_phentsize
            p_type, = struct.unpack_from(end + "I", data, off)
            if p_type != 1:                   # PT_LOAD
                continue
            if is64:
                p_offset, p_vaddr = struct.unpack_from(end + "QQ", data, off + 0x08)
                p_filesz, = struct.unpack_from(end + "Q", data, off + 0x20)
            else:
                p_offset, p_vaddr = struct.unpack_from(end + "II", data, off + 0x04)
                p_filesz, = struct.unpack_from(end + "I", data, off + 0x10)
            if p_vaddr <= strtab < p_vaddr + p_filesz:
                file_off = p_offset + (strtab - p_vaddr)
                break
        if file_off is None:
            return None, []
        blob = data[file_off:file_off + (strsz or 0x10000)]

        def name_at(idx):
            stop = blob.find(b"\x00", idx)
            return blob[idx:stop if stop >= 0 else None].decode("utf-8", "replace")

        soname, needed = None, []
        for tag, val in raw:
            if tag == 14:
                soname = name_at(val)
            else:
                needed.append(name_at(val))
        return soname, needed
    except (struct.error, IndexError, ValueError):
        return None, []


def _load_order(libs_dir, files):
    """
    Order `files` so that every dependency inside libs_dir is loaded before
    the library needing it. Returns (ordered_paths, info) where info maps a
    path to (soname, [needed, ...]).
    """
    info = {}
    by_soname = {}
    for path in files:
        soname, needed = _elf_dynamic(path)
        info[path] = (soname, needed)
        by_soname.setdefault(soname or os.path.basename(path), path)
        by_soname.setdefault(os.path.basename(path), path)

    ordered, seen, visiting = [], set(), set()

    def visit(path):
        if path in seen or path in visiting:
            return
        visiting.add(path)
        for dep in info.get(path, (None, []))[1]:
            target = by_soname.get(dep)
            if target and target != path:
                visit(target)
        visiting.discard(path)
        seen.add(path)
        ordered.append(path)

    for path in files:
        visit(path)
    return ordered, info


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
        wanted = {}
        for stem in LIBRARIES + OPTIONAL:
            path = find_library(self.libs_dir, stem)
            if path is None:
                if stem in LIBRARIES:
                    raise OSError("{0} not found in {1} (files: {2})".format(
                        stem, self.libs_dir,
                        ", ".join(sorted(os.listdir(self.libs_dir))[:12])))
                continue
            wanted[os.path.realpath(path)] = stem

        # Pre-load everything in _libs bottom-up, by absolute path. On Linux
        # and macOS the rpath ($ORIGIN / @loader_path) would find siblings on
        # its own, but Android's linker never searches the directory a
        # library came from: a dependency is only resolved if it is already
        # loaded under that soname. Loading dependencies first is what makes
        # the bundled _libs work there (Termux included).
        try:
            everything = [os.path.join(self.libs_dir, f)
                          for f in sorted(os.listdir(self.libs_dir))]
        except OSError:
            everything = []
        everything = [p for p in everything if os.path.isfile(p)]
        ordered, elf_info = _load_order(self.libs_dir, everything)
        self._elf = elf_info
        preloaded = {}
        for path in ordered:
            try:
                preloaded[os.path.realpath(path)] = ctypes.CDLL(path, mode=mode)
            except OSError as exc:
                if os.path.realpath(path) in wanted:
                    # Py2-compatible syntax: no `raise ... from`
                    raise OSError(self._explain(path, exc, elf_info, preloaded))  # noqa: B904

        # Bind in LIBRARIES + OPTIONAL order, NOT in preload order: dlsym on a
        # handle also searches that library's dependencies, so whichever
        # library is bound first claims a shared symbol. winpr before freerdp
        # before the client/server libs keeps `library_of()` honest.
        by_stem = dict((v, k) for k, v in wanted.items())
        for stem in LIBRARIES + OPTIONAL:
            path = by_stem.get(stem)
            if path is None:
                continue
            lib = preloaded.get(path)
            if lib is None:
                continue
            self.libs[stem] = lib
            names = bind(lib, strict=strict)
            self.bound[stem] = names
            for n in names:
                self._lookup.setdefault(n, stem)
        missing = [s for s in LIBRARIES if s not in self.libs]
        if missing:
            raise OSError("could not load {0} from {1}".format(
                ", ".join(missing), self.libs_dir))

    def _explain(self, path, exc, elf_info, loaded):
        """Turn a dlopen failure into something actionable."""
        soname, needed = elf_info.get(path, (None, []))
        present = set()
        for other, (oname, _n) in elf_info.items():
            present.add(oname or os.path.basename(other))
            present.add(os.path.basename(other))
        unmet = [d for d in needed if d not in present]
        lines = ["failed to load {0}: {1}".format(os.path.basename(path), exc),
                 "  directory: {0}".format(self.libs_dir),
                 "  soname:    {0}".format(soname or "(none)")]
        if needed:
            lines.append("  needs:     {0}".format(", ".join(needed)))
        if unmet:
            lines.append("  NOT in this directory: {0}".format(", ".join(unmet)))
            lines.append("  -> these must be provided by the system, or bundled "
                         "into _libs next to the others")
        else:
            lines.append("  every dependency is present in the directory; the "
                         "failure is the loader's, e.g. an ABI or page-size "
                         "mismatch (Android 15+ needs 16 KB-aligned libraries)")
        return "\n".join(lines)

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

# Architecture

`pyfreerdpnative` has three layers, and only the top one is written by hand.

```
 FreeRDP headers (include/freerdp, winpr/include/winpr, + CMake-generated)
        │  scripts/gen_bindings.py   (cpp → pycparser → Python)
        ▼
 pyfreerdpnative/_core/        constants.py  types.py  functions.py
 pyfreerdpnative/freerdp/**    one module per header, re-exporting from _core
 pyfreerdpnative/winpr/**
        │  pyfreerdpnative/_loader.py
        ▼
 load() → FreeRDP object: libraries from _libs with every prototype attached
```

## 1. Generation

`gen_bindings.py` builds one C translation unit that includes **every** public
header, preprocesses it with `cpp` against pycparser's stub libc (the real one
uses GCC extensions pycparser cannot parse), and walks the resulting AST.

| C construct | Python |
|---|---|
| `#define NAME <constant expr>` | `NAME = value` — the expression is expanded by cpp and evaluated with pycparser's AST, so `PIXEL_FORMAT_BGRX32` (built from `FREERDP_PIXEL_FORMAT(32, …)`) and `RDP_SCANCODE_RETURN` (a ternary inside a macro) resolve |
| `enum { A = 1, B }` | `A = 1`, `B = 2` |
| `typedef … Name` | `Name = <ctypes expr>` |
| `struct tag { … }` | `class struct_tag(ctypes.Structure)` with `_fields_`; the typedef alias (`rdpContext`) is the same object |
| `ALIGN64 T f;` | an anonymous 8-byte-aligned union — see below |
| `#pragma pack(push, 1)` | `_pack_ = 1` |
| function pointer typedef | `ctypes.CFUNCTYPE(res, args…)` |
| `FREERDP_API R f(A, B)` | `PROTOTYPES['f'] = (R, [A, B], variadic)` |

Headers that cannot coexist in one translation unit (`winpr/asn1.h` declares
an enum `ER_TAG_BOOLEAN`; `freerdp/crypto/er.h` `#define`s the same name) are
parsed in a separate unit and merged.

### Why the layouts are right

Three things silently corrupt a naive header translation, and each shifts
every later field of large structs like `rdpContext` and `rdpSettings`:

- **`ALIGN64`.** FreeRDP defines it as `DECLSPEC_ALIGN(8)` → `__attribute__((aligned(8)))`,
  so a 4-byte `BOOL` occupies an 8-byte slot. Stripping attributes (which
  pycparser needs) loses that. The generator maps the macro to the `restrict`
  qualifier — a token pycparser preserves and FreeRDP never uses bare — and
  emits marked fields as `_align64(name, type)`, an anonymous union of the
  field and a `c_uint64`.
- **Array-type caching.** ctypes caches `(T * n)`. A typedef like
  `SID_AND_ATTRIBUTES_ARRAY = (SID_AND_ATTRIBUTES * 1)` emitted before `T` has its
  layout bakes in size 0 for every later use. Array-of-struct typedefs are
  emitted after all layouts.
- **`sizeof()` in array bounds** (`WCHAR applicationID[520 / sizeof(WCHAR)]`) is
  evaluated at generation time.

The check: a C program compiled against the same headers prints `sizeof`
and `offsetof` for every nameable struct, and the generated `ctypes.sizeof`
must equal it. For FreeRDP 3.31.1 on x86-64: **767 / 767**. Types are emitted
symbolically (`ctypes.c_long`, not a fixed width), so Windows LLP64 resolves
correctly at import time.

### The header mirror

Every declaration records the header it came from (cpp's `# line "file"`
markers). `freerdp/codec/color.h` becomes `freerdp/codec/color.py` with that
header's constants inline (hex for flags/ids) and its types and functions
re-exported from `_core`, which holds the single correctly ordered definition
of everything. Two layout rules:

- A header named like a directory (`freerdp/client.h` beside `freerdp/client/`)
  becomes that package's `__init__.py`, so `from pyfreerdpnative.freerdp import
  client` yields both.
- `-` in a header name becomes `_` (`rdpecam-enumerator.h` → `rdpecam_enumerator.py`).

Headers that declare nothing bindable (pure include aggregators, build-config
headers) get no module.

## 2. Loading

`_loader.py` is a fixed template emitted alongside the generated code.
`load()` resolves the libraries directory (`$PYFREERDP_LIBS`, else the bundled
`_libs`, else a parent `_libs`), opens `winpr3 → freerdp3 → freerdp-client3 →
freerdp-server3` in that order (`RTLD_GLOBAL` on POSIX, `add_dll_directory` on
Windows), then any optional extras, and attaches every `PROTOTYPES` entry to
the library that exports it.

Attribution matters: `dlsym` on a handle also searches that library's
dependencies, so binding order decides which library "owns" a symbol. Core
libraries are bound before extras so `freerdp_connect` belongs to `freerdp3`,
not to `uwac0` which merely imports it.

## 3. The libraries

`_libs/` holds the FreeRDP build for the wheel's platform — libraries and
executables in one directory, rpath `$ORIGIN` / `@loader_path`, so the package
is relocatable. On Linux every non-baseline system library (per auditwheel's
manylinux whitelist) is vendored at build time: cJSON, ICU, OpenSSL, krb5,
ALSA, udev, xkbcommon, the X11 extension libraries. See
[../scripts/BUILD.md](../scripts/BUILD.md).

## Version coupling

Struct layouts, settings key ids and the prototype set are those of one
FreeRDP release. The `build-freerdp` workflow generates the bindings from the
**same checkout** it builds the libraries from, so a wheel is always
internally consistent, and `ci.yml` regenerates from the pinned tag and fails
if `pyfreerdpnative/` differs. Never combine the package with libraries from a
different FreeRDP version.

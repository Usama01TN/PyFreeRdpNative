# pyfreerdpnative

**FreeRDP for Python — the headers, translated 1:1 into ctypes, plus the prebuilt libraries.**

`pyfreerdpnative` is not a hand-written wrapper. Every module in it is generated
from FreeRDP's own C headers: each `.h` becomes a `.py` with the same name in the
same folder, carrying that header's `#define`s, enums, struct layouts and function
prototypes. The wheels bundle the matching FreeRDP libraries, so `pip install`
gives you a working RDP client *and* server stack with no system dependencies.

```python
import ctypes
from pyfreerdpnative import load
from pyfreerdpnative.freerdp import freerdp as F, client as CLIENT, settings_keys as KEY

api = load()                                          # opens the bundled libraries,
                                                      # binds every prototype
entry = CLIENT.RDP_CLIENT_ENTRY_POINTS_V1()
entry.Size = ctypes.sizeof(entry)
entry.Version = CLIENT.RDP_CLIENT_INTERFACE_VERSION
entry.ContextSize = ctypes.sizeof(F.rdpContext)
ctx = api.freerdp_client_context_new(ctypes.byref(entry))

s = ctx.contents.settings                             # typed struct access, no offsets
api.freerdp_settings_set_string(s, KEY.FreeRDP_ServerHostname, b"10.0.0.5")
api.freerdp_settings_set_string(s, KEY.FreeRDP_Username, b"alice")
api.freerdp_settings_set_string(s, KEY.FreeRDP_Password, b"secret")
api.freerdp_settings_set_bool(s, KEY.FreeRDP_IgnoreCertificate, True)

if api.freerdp_connect(ctx.contents.instance):
    print("connected")
    api.freerdp_disconnect(ctx.contents.instance)
api.freerdp_client_context_free(ctx)
```

If you know FreeRDP's C API, you already know this one. The names, argument
orders and constants are FreeRDP's; only the syntax is Python.

## Installation

Wheels are attached to the `freerdp-libs-<version>` GitHub release. One wheel
per platform, valid for **every Python 3.8+** (ctypes never links `libpython`,
so there is no per-interpreter build):

```bash
pip install pyfreerdpnative \
  --find-links https://github.com/Usama01TN/PyFreeRdpNative/releases/expanded_assets/freerdp-libs-3.31.1
```

pip picks the wheel for your OS and architecture. Or install a file directly:

```bash
pip install https://github.com/Usama01TN/PyFreeRdpNative/releases/download/freerdp-libs-3.31.1/pyfreerdpnative-0.2.0-py3-none-win_amd64.whl
```

### Variants

Every build variant is its own pip package. They all provide the same
`pyfreerdpnative` module, so **install exactly one**:

| Package | Profile | Media | Contents |
|---|---|---|---|
| `pyfreerdpnative` | full | FFmpeg + OpenH264 | everything: libraries, all channels, executables (`xfreerdp`, `sfreerdp-server`, `freerdp-proxy`, `ffmpeg` …) |
| `pyfreerdpnative-minimal` | minimal | none | libraries only, size-optimised (~4 MB) |
| `pyfreerdpnative-ffmpeg` / `-openh264` / `-standard` | full | as named | |
| `pyfreerdpnative-minimal-ffmpeg` / `-minimal-openh264` / `-minimal-media` | minimal | as named | |

### Platforms

| | Tags | Notes |
|---|---|---|
| Windows | `win_amd64`, `win32`, `win_arm64` | Windows 10+; 32-bit Python needs `win32` |
| Linux | `manylinux_2_39_x86_64`, `manylinux_2_39_aarch64` | glibc 2.39+ (Ubuntu 24.04, Fedora 40, Debian 13); system libraries such as cJSON, ICU, OpenSSL, krb5 are vendored |
| macOS | `macosx_11_0_arm64`, `macosx_11_0_x86_64` | |
| Android | `android_24_arm64_v8a`, `_armeabi_v7a`, `_x86_64`, `_x86` | PEP 738 tags; installed by cross-install into an app, or retagged for Termux/Pydroid — see [docs/MOBILE.md](docs/MOBILE.md) |
| iOS | `ios_13_0_arm64_iphoneos`, `_iphonesimulator` | PEP 730 tags; `.dylib`s to embed in a signed app bundle — see [docs/MOBILE.md](docs/MOBILE.md) |

Windows 7 / 8.1 are not supported by the published wheels (the VS 2022 runtime
dropped them); an experimental static-CRT build exists, see
[scripts/BUILD.md](scripts/BUILD.md#targeting-older-windows-experimental).

## What is in the package

```
pyfreerdpnative/
├── __init__.py          load(), FreeRDP, constants, types, functions, PROTOTYPES
├── _loader.py           finds _libs, opens the libraries, attaches every prototype
├── _libs/               the FreeRDP libraries (+ executables) for this platform
├── _core/
│   ├── constants.py     every #define and enum member, flat
│   ├── types.py         every typedef, struct and union as ctypes classes
│   └── functions.py     every FREERDP_API / WINPR_API prototype
├── freerdp/             mirror of include/freerdp/  (freerdp.h -> freerdp.py, codec/color.h -> codec/color.py …)
└── winpr/               mirror of winpr/include/winpr/
```

Use the mirror modules the way you would include headers:

```python
from pyfreerdpnative.freerdp import scancode, input as rdp_input
from pyfreerdpnative.freerdp.codec import color
from pyfreerdpnative.freerdp.gdi import gdi

scancode.RDP_SCANCODE_RETURN     # 0x1c   from freerdp/scancode.h
rdp_input.KBD_FLAGS_RELEASE      # 0x8000 from freerdp/input.h
color.PIXEL_FORMAT_BGRX32        # 0x20040888
gdi.rdpGdi.primary_buffer.offset # 64 — layouts match the C compiler byte for byte
```

Each mirror module also lists the functions its header declares
(`freerdp.FUNCTIONS`) and can bind just those (`freerdp.bind(lib)`).

### The `api` object

`load()` returns a `FreeRDP` object. `api.<name>` resolves the function in
whichever library exports it, with `restype`/`argtypes` from the header:

```python
api.library_of("freerdp_connect")          # 'freerdp3'
api.library_of("Stream_New")               # 'winpr3'
api.libs["freerdp-client3"]                # the raw ctypes.CDLL, if you need it
api.version()                              # '3.31.1'
```

Because argument types are enforced, passing a plain `int` where a
`POINTER(rdpSettings)` is expected raises `ctypes.ArgumentError` — the C
compiler's type checking, at call time.

## Examples

| | |
|---|---|
| [`examples/basic_connect.py`](examples/basic_connect.py) | connect and disconnect |
| [`examples/send_input.py`](examples/send_input.py) | keyboard and mouse events |
| [`examples/screenshot.py`](examples/screenshot.py) | software GDI framebuffer → BMP |
| [`examples/list_api.py`](examples/list_api.py) | search prototypes and constants, see which library exports what |

```bash
python examples/basic_connect.py 10.0.0.5 alice secret
```

## What you don't get

- **A high-level Python API.** This package deliberately stops at the C API.
  Event loops, callbacks (`PostConnect`, update handlers, channel callbacks)
  and rendering are yours to write, exactly as in C. `ctypes.CFUNCTYPE` types
  for every callback are in `types.py`.
- **A GUI client.** The `full` wheels ship FreeRDP's own clients (`xfreerdp`,
  `wfreerdp`, `sdl-freerdp`) as executables in `_libs/`; on Linux the SDL
  client needs the desktop's GTK/WebKit stack.
- **Loading on a store-installed iOS Python.** iOS only loads dylibs from a
  signed app bundle; see [docs/MOBILE.md](docs/MOBILE.md).

## How the bindings are made

`scripts/gen_bindings.py` preprocesses the headers with `cpp`, parses them with
pycparser, and emits Python. It handles the things a naive translation gets
wrong — `ALIGN64` fields, `#pragma pack`, `sizeof()` in array bounds, ctypes'
array-type caching — and the output is checked against gcc: **767 of 767 struct
sizes match** for FreeRDP 3.31.1. CI regenerates the bindings from the pinned
FreeRDP tag on every push and fails if the committed package differs, so
layouts can never drift from the libraries.

```bash
pip install pycparser
python scripts/gen_bindings.py \
    --include <freerdp>/include --include <freerdp>/winpr/include \
    --include <build>/include   --include <build>/winpr/include \
    --out pyfreerdpnative --version 3.31.1
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Building the libraries

`scripts/build_freerdp.py` builds FreeRDP and its dependencies for every
platform; `.github/workflows/build-freerdp.yml` runs it across the whole matrix,
generates the bindings from the same checkout, packages the wheels and attaches
everything to a release. [scripts/BUILD.md](scripts/BUILD.md) has the details,
including profiles, editions, channel selection and mobile builds.

## Development

```bash
pip install -e .[dev,generate]
pytest                        # bindings tests; library tests skip without _libs
PYFREERDP_LIBS=/path/to/_libs pytest   # …or point them at a build
ruff check scripts tests examples
```

## Versioning

The package version tracks the Python layer. The FreeRDP version is the
release tag (`freerdp-libs-3.31.1`) and `api.version()` at runtime. Struct
layouts are specific to a FreeRDP release: never mix a wheel's `pyfreerdpnative`
with libraries from a different FreeRDP build.

## License

Apache-2.0 for this project. FreeRDP is Apache-2.0; bundled FFmpeg is LGPL,
OpenH264 is BSD (Cisco's binary licence does not apply — it is built from source).

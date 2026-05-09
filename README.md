# pyfreerdp

Python bindings for [FreeRDP](https://github.com/FreeRDP/FreeRDP) — connect to Microsoft RDP servers from Python (client side) **and** accept RDP connections from Python (server side), with full virtual-channel support and a WinPR helper subset. Linux, macOS, Windows, plus mobile via NDK / Xcode cross-compile.

> **Read this first.** This package wraps `libfreerdp-client3`, `libfreerdp-server3`, and `libwinpr3` via `ctypes`. It does **not** statically embed FreeRDP — you either install FreeRDP from your package manager or build it with the included `pyfreerdp-build` tool. Mobile platforms have additional constraints; see [docs/MOBILE.md](docs/MOBILE.md).

## Style note

The Python source is written in a **Python-2-compatible syntax style** — no f-strings, no `from __future__ import annotations`, no type annotations, no dataclasses, no walrus operator, no `match` statement, no PEP 604 unions. The package itself only ever runs on **Python 3** (Python 2 is end-of-life and we don't test it), but the syntax restriction is a project preference. If you're contributing, mirror it: use `"...{0}".format(x)` instead of `f"...{x}"`, plain classes with explicit `__init__` instead of `@dataclass`, etc.

## Installation

### Option 1 — install FreeRDP separately, then `pip install` (fastest)

```bash
# Debian / Ubuntu — both halves
sudo apt install libfreerdp-client3-3 libfreerdp-server3-3 libfreerdp3-3

# Fedora / RHEL
sudo dnf install freerdp-libs freerdp-server

# macOS (Homebrew bundles client + server + winpr)
brew install freerdp

# Windows (vcpkg)
vcpkg install freerdp:x64-windows
```

Then:

```bash
pip install pyfreerdp
```

### Option 2 — build FreeRDP from source as part of install

```bash
PYFREERDP_BUILD_FREERDP=1 pip install pyfreerdp
```

This clones FreeRDP at the pinned tag (3.16.0), runs CMake with **client + server + shadow + proxy** enabled (`--profile=full`), and stages every produced `.so/.dylib/.dll` into the wheel. Takes 5–15 min depending on the host.

### Option 3 — explicit pre-build with profile selection

```bash
pip install pyfreerdp
pyfreerdp-build --target host --profile full          # client + server + shadow + proxy (default)
pyfreerdp-build --target host --profile client-only   # client only — fastest, smallest
pyfreerdp-build --target host --profile server-only   # server + shadow + proxy
pyfreerdp-build --target host --profile minimal       # both protocol cores, no shadow/proxy
```

After install, the script verifies the expected library families actually landed in `pyfreerdp/_libs/`. If a CMake feature was silently skipped because a system dep was missing, you get a loud error pointing at the exact missing libraries instead of a wheel that fails at runtime.

## Quick start — client

```python
from pyfreerdp import RdpClient, RdpSettings

settings = RdpSettings(
    host="10.0.0.5",
    username="alice",
    password="...",
    domain="CORP",
    width=1920, height=1080,
    ignore_certificate=True,        # lab only — don't ship this enabled
)

with RdpClient(settings) as client:
    client.send_mouse_move(960, 540)
    client.send_key(0x1F, pressed=True)
    client.send_key(0x1F, pressed=False)
    client.run_event_loop(timeout=10.0)
```

## Quick start — server

```python
from pyfreerdp import RdpServer, RdpServerSettings, SecurityProtocol

def handle(peer):
    with peer:
        print("Client connected: {0}".format(peer.os_type))
        peer.run(timeout=60)

settings = RdpServerSettings(
    bind_address="0.0.0.0", port=3389,
    certificate_file="server.crt",
    private_key_file="server.key",
    security=SecurityProtocol.TLS | SecurityProtocol.NLA,
)

with RdpServer(settings, handle) as server:
    server.serve_forever()
```

A complete server skeleton is in [`examples/server_echo.py`](examples/server_echo.py).

## Quick start — virtual channels

```python
from pyfreerdp import RdpClient, RdpSettings, ClipboardChannel
from pyfreerdp import DriveRedirection, DriveRedirectionChannel

clipboard = ClipboardChannel()
drive = DriveRedirection(name="share", local_path="/home/me/shared")

settings = RdpSettings(
    host="rdp.example.com", username="alice", password="hunter2",
    channels=[
        clipboard,
        DriveRedirectionChannel(drives=[drive]),
    ],
)

with RdpClient(settings) as client:
    clipboard.set_text("hello from python")
    client.run_event_loop(timeout=30.0)
    text = clipboard.get_text(timeout=2.0)
    print("Remote clipboard:", text)
```

See [docs/CHANNELS.md](docs/CHANNELS.md) for the full channel framework reference and which channels work end-to-end vs. ship as registration scaffolding only.

## Quick start — full PyQt5 viewer

Two complete GUI viewers ship in `examples/`:

```bash
# Legacy bitmap path (any server, no extra deps beyond PyQt5)
pip install PyQt5
python examples/qt5_viewer_legacy.py

# rdpgfx + H.264 path (modern Windows servers, faster)
pip install PyQt5 av
python examples/qt5_viewer_gfx.py
```

Both demonstrate connection settings dialog, input injection, clipboard sync, drive redirection, display-resize, and a custom channel — wired into PyQt5's signal/slot system end to end.

## What you get

**Client**
- `RdpClient` — context-managed RDP session with channel integration
- `RdpSettings` — connection options (display, security, gateway, redirection, channels)
- Input injection: `send_key`, `send_unicode`, `send_mouse_move`, `send_mouse_button`

**Server**
- `RdpServer` — bound listener; per-peer threading model
- `RdpServerSettings` — bind address, TLS material, advertised desktop, security policy
- `RdpPeer` — per-client lifecycle (`initialize` / `run` / `close`)

**Virtual channels** (everything in `pyfreerdp.channels`)

| Channel | Class | Status | Notes |
|---|---|---|---|
| cliprdr | `ClipboardChannel` | ✅ working | Text + HTML; file-list opt-in |
| rdpdr (drives) | `DriveRedirectionChannel` | ✅ working | Filesystem only |
| disp | `DisplayControlChannel` | ✅ working | Resize PDU framework |
| rail | `RailChannel` | ✅ working | RemoteApp window state |
| rdpei | `MultitouchChannel` | ✅ working | Touch contact events |
| encomsp | `EncompChannel` | ✅ working | Multiparty roster |
| remdesk | `RemdeskChannel` | ✅ working | Remote Assistance |
| DRDYNVC | `DynamicChannelManager` | ✅ working | DVC plumbing |
| (any) | `CustomChannel` | ✅ working | Bring-your-own |
| rdpsnd | `AudioOutChannel` | ⚠️ stub | Need audio stack |
| audin | `AudioInChannel` | ⚠️ stub | Need capture device |
| rdpgfx | `GraphicsPipelineChannel` | ⚠️ stub | Need video decoder |

**WinPR helpers** (`pyfreerdp.winpr`)
- `Stream` — wStream-equivalent buffer with little/big-endian read/write
- `query_security_package(name)` — confirm SSPI package available
- `parse_logon_identity(c_void_p)` — decode SEC_WINNT_AUTH_IDENTITY in server Logon callbacks

**Shared**
- `SecurityProtocol` — bitmask for RDP / TLS / NLA / NLA-Ext
- Cross-platform library loading with explicit overrides
- Exception hierarchy: `RdpError` → `RdpConnectionError`, `RdpAuthenticationError`, `RdpProtocolError`, `ChannelError`, `WinPRError`, `FreeRdpNotFoundError`

## What you don't get (be honest about this)

- **Pixel rendering.** FreeRDP delivers bitmap updates on the client and expects them as input on the server. Painting them into / out of a window is your job. Hook `rdpUpdate` from your own code.
- **Audio playback / capture.** PCM is exposed via the channel callbacks but not auto-routed to a sound device. Subclass `AudioOutChannel.on_pcm` and `AudioInChannel.next_pcm` to wire your stack.
- **Video decoding.** `GraphicsPipelineChannel` registers and negotiates rdpgfx with the peer, but H.264 / RemoteFX-Progressive frames arrive raw — wire them into your decoder of choice.
- **Frame source for the server.** A real server has to read pixels from somewhere — local screen capture (Windows DXGI / macOS AVFoundation / Linux X11 or PipeWire) — and feed them through `peer.update->BeginPaint/EndPaint`. We expose the protocol layer; the screen source is your problem. (FreeRDP's bundled `freerdp-shadow-cli` is one such implementation.)
- **Printer / smartcard / serial / parallel redirection.** rdpdr's drive sub-protocol is implemented; the others are stubs.
- **App Store-ready iOS distribution.** Apple forbids `dlopen` of arbitrary dylibs. iOS works in side-loaded / dev builds; for App Store you need to statically link FreeRDP into your app binary. See [docs/MOBILE.md](docs/MOBILE.md).

## Cross-platform support matrix

| Platform | Client | Server | Channels | Notes |
|----------|--------|--------|----------|-------|
| Linux x86_64 / aarch64 | ✅ | ✅ | ✅ | Full profile builds cleanly |
| macOS Intel / Apple Silicon | ✅ | ✅ | ✅ | Shadow uses AVFoundation |
| Windows x64 | ✅ | ✅ | ✅ | Shadow uses DXGI duplication |
| Android (NDK r25+) | ⚠️ | ⚠️ | ✅ | No shadow (no NDK capture API). Server-side useful for screen-share apps. |
| iOS (dev signing) | ⚠️ | ⚠️ | ✅ | Static archive only; App Store needs app-side linking |
| FreeBSD / OpenBSD | 🤷 | 🤷 | 🤷 | Should work; untested |

## Building from source

```bash
# Host (your machine), full client+server+shadow+proxy
pyfreerdp-build --target host --profile full --jobs 8

# Client only — when you don't need server-side
pyfreerdp-build --target host --profile client-only

# Android arm64 (server-capable, no shadow)
ANDROID_NDK_ROOT=/path/to/ndk \
    pyfreerdp-build --target android --abi arm64-v8a

# iOS device (must run on macOS)
pyfreerdp-build --target ios

# Pin to a specific FreeRDP version
pyfreerdp-build --ref 3.16.0 --target host --profile full
```

Output goes to `pyfreerdp/_libs/`:
- Host: `libfreerdp-client3.so`, `libfreerdp-server3.so`, `libfreerdp-shadow3.so`, `libfreerdp3.so`, `libwinpr3.so` (and their symlinks)
- Android: `_libs/android/<abi>/lib*.so`
- iOS: `_libs/ios/lib*.a`

If the build doesn't produce every promised library family for the chosen profile, the script aborts with the exact list of missing pieces and the system packages you likely need.

## Configuration

| Env var | Effect |
|---------|--------|
| `PYFREERDP_CLIENT_LIBRARY` | Absolute path to `libfreerdp-client3` to load. Bypasses auto-detection. |
| `PYFREERDP_SERVER_LIBRARY` | Absolute path to `libfreerdp-server3`. Bypasses auto-detection. |
| `PYFREERDP_WINPR_LIBRARY` | Absolute path to `libwinpr3`. Bypasses auto-detection. |
| `PYFREERDP_LIBRARY` | Legacy combined override. Applied to **client only** (server + WinPR lookups ignore it). |
| `PYFREERDP_BUILD_FREERDP` | When `1` during `pip install`, triggers `build_freerdp.py` automatically. |
| `PYFREERDP_FREERDP_REF` | Override the FreeRDP git tag built when `PYFREERDP_BUILD_FREERDP=1`. |
| `PYFREERDP_EXTRA_CMAKE` | Extra `-DFOO=BAR` flags appended to the CMake configure step. Useful for opting into FFmpeg / OpenH264 / etc. |

## Development

```bash
git clone <this-repo>
cd pyfreerdp
pip install -e .[dev]
pytest                                 # 107 unit tests, no native lib needed
pytest -m needs_lib                    # extra checks if FreeRDP is installed
```

## Versioning

`pyfreerdp` is versioned independently from FreeRDP. Each release pins a `FREERDP_VERIFIED_VERSION` in [`pyfreerdp/version.py`](pyfreerdp/version.py); other versions in the same major series should work but aren't guaranteed by the test suite.

## License

Apache-2.0. FreeRDP itself is Apache-2.0 with some optional components under different terms — see the [FreeRDP repository](https://github.com/FreeRDP/FreeRDP). This package does not redistribute FreeRDP source; it only provides binding code.

## Further reading

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — design rationale, lifecycle diagrams, server callback model
- [docs/CHANNELS.md](docs/CHANNELS.md) — virtual channel reference, wire-protocol expectations
- [docs/MOBILE.md](docs/MOBILE.md) — Android + iOS specifics, Chaquopy/BeeWare integration
- [docs/PUBLISHING.md](docs/PUBLISHING.md) — wheel build + PyPI release workflow setup
- [FreeRDP project](https://github.com/FreeRDP/FreeRDP)
- FreeRDP API headers — [`include/freerdp/peer.h`](https://github.com/FreeRDP/FreeRDP/blob/master/include/freerdp/peer.h), [`include/freerdp/listener.h`](https://github.com/FreeRDP/FreeRDP/blob/master/include/freerdp/listener.h), [`include/freerdp/freerdp.h`](https://github.com/FreeRDP/FreeRDP/blob/master/include/freerdp/freerdp.h)

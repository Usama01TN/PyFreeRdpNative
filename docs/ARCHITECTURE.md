# Architecture

## High-level structure

```
┌──────────────────────────────────────────────────────────────┐
│  Your Python code                                            │
│      from pyfreerdp import RdpClient, RdpServer              │
└────────────────┬─────────────────────────┬───────────────────┘
                 │                         │
       ┌─────────▼──────────┐    ┌─────────▼──────────┐
       │  RdpClient         │    │  RdpServer         │  Pythonic facades
       │  RdpSettings       │    │  RdpServerSettings │
       │                    │    │  RdpPeer           │
       └─────────┬──────────┘    └─────────┬──────────┘
                 │                         │
       ┌─────────▼─────────────────────────▼──────────┐
       │  pyfreerdp.bindings.api / .types             │  ctypes signatures
       │   bind_client(lib)   bind_server(lib)        │
       └─────────┬─────────────────────────┬──────────┘
                 │                         │
       ┌─────────▼──────────┐    ┌─────────▼──────────┐
       │  loader            │    │  loader            │
       │  load_freerdp()    │    │  load_freerdp_     │
       │                    │    │      server()      │
       └─────────┬──────────┘    └─────────┬──────────┘
                 │                         │
       ┌─────────▼──────────┐    ┌─────────▼──────────┐
       │ libfreerdp-client3 │    │ libfreerdp-server3 │  Compiled C
       │      .so/.dylib    │    │      .so/.dylib    │  (not us)
       │      /.dll         │    │      /.dll         │
       └────────────────────┘    └────────────────────┘
                  │                         │
                  └────┬────────────────────┘
                       │  shared transitive deps
                ┌──────▼───────┐
                │ libfreerdp3  │
                │ libwinpr3    │
                └──────────────┘
```

## Why two libraries

FreeRDP ships its client and server protocol implementations as separate
shared libraries. They share a common transitive base (`libfreerdp3` for
the protocol primitives and `libwinpr3` for the Win32 portability layer),
but each top-level library has its own entry-point set:

- `libfreerdp-client3` exports `freerdp_new`, `freerdp_connect`,
  `freerdp_input_send_*`, etc.
- `libfreerdp-server3` exports `freerdp_listener_new`, `freerdp_peer_*`.

The Python binding loads each independently. A user who wants to write
just a client doesn't pay the server-library cost (it stays unloaded).
A `--profile=server-only` install genuinely doesn't need the client lib.

When **both** are loaded, they share the underlying `libfreerdp3` /
`libwinpr3` symbols thanks to `RTLD_GLOBAL`. There's no double-loading
of the protocol core.

## Why ctypes and not cffi / pybind11 / Cython?

| Tool | Pro | Con |
|------|-----|-----|
| **ctypes** ✅ | Stdlib, no build step, works in any CPython | Manual signatures |
| cffi | Cleaner API; ABI mode also build-step-free | Extra dep; doesn't help much for opaque-pointer style |
| pybind11 / Cython | Best perf | Requires C++ toolchain at install for sdists; complicates mobile |
| SWIG | Generates from headers | FreeRDP's headers are too tangled to feed cleanly |

For a binding that wraps a small, stable subset of an opaque C API, ctypes is the right tool.

## Why opaque pointers everywhere?

FreeRDP's `rdpSettings` struct is **massive** (hundreds of fields) and was
deliberately moved behind accessor functions in 3.x specifically because
direct field access kept breaking ABI between minor releases. We mirror
only the things that are stable:

- The first few pointer slots in `struct freerdp` (`context`, `settings`, `input`, `update`).
- The first slot in `struct freerdp_peer` (`context`).
- The accessor function signatures (`freerdp_settings_*`, `freerdp_peer_*`).
- The `SettingId` enum values for the fields we expose.

If FreeRDP reshuffles its internal structs, our binding still works.

## Client lifecycle

```
RdpSettings  ──► RdpClient.__init__()
                     │
                     ├─ load_freerdp()           (once per process)
                     ├─ bind_client(handle)      (attach argtypes/restype)
                     ├─ freerdp_new()            ──► instance
                     ├─ freerdp_context_new()    ──► context
                     └─ _apply_settings()        (push every field native)

                 RdpClient.connect()
                     │
                     └─ freerdp_connect()
                         ├─ TCP connect
                         ├─ X.224 / MCS / TLS / NLA negotiation
                         └─ RDP handshake → BOOL

                 RdpClient.run_event_loop()
                     │
                     └─ loop:
                         ├─ freerdp_check_event_handles()
                         ├─ freerdp_shall_disconnect()? → break
                         └─ sleep(50ms)

                 RdpClient.disconnect() / close()
                     │
                     ├─ freerdp_disconnect()
                     ├─ freerdp_context_free()
                     └─ freerdp_free()
```

## Server lifecycle

```
RdpServerSettings  ──► RdpServer.__init__()
                          │
                          ├─ load_freerdp_server()       (once per process)
                          ├─ bind_server(handle)
                          ├─ freerdp_listener_new()      ──► listener
                          └─ freerdp_listener_open(addr, port)

                      RdpServer.serve_forever()
                          │
                          └─ loop:
                              ├─ freerdp_listener_check_fds()
                              │   (drives internal accept;
                              │    fires PeerAccepted callback)
                              ├─ _accept_one() → RdpPeer or None
                              └─ on peer:
                                  ├─ apply per-peer settings (cert, size...)
                                  └─ spawn handler thread per peer
                                      │
                                      └─ RdpPeer.__enter__()
                                          ├─ freerdp_peer_context_new()
                                          ├─ freerdp_peer_initialize()
                                          │   (TLS handshake, capability nego)
                                          │
                                          ├─ user code: paint frames,
                                          │             read input
                                          │
                                          └─ RdpPeer.close()
                                              ├─ freerdp_peer_disconnect()
                                              ├─ freerdp_peer_close()
                                              ├─ freerdp_peer_context_free()
                                              └─ freerdp_peer_free()
```

## Server callback bridge

This is the trickiest piece of the binding. FreeRDP's listener doesn't
expose a synchronous accept(). New peers come through the listener's
`PeerAccepted` method-table slot — a function pointer the user is
expected to set. The C function runs on the listener's internal thread
when a TCP connection arrives.

From Python, we need to:

1. Install a C-callable thunk into that slot (`@CFUNCTYPE`-decorated function).
2. Have that thunk push the incoming `freerdp_peer*` onto a thread-safe queue.
3. Drain the queue from the Python accept loop.

The thunk is the only Python code that runs on the C-managed listener
thread. It must be small and never raise. It just enqueues and returns
TRUE.

This pattern is implemented in [`examples/server_echo.py`](../examples/server_echo.py)
as `QueuedRdpServer`. The base `RdpServer` class deliberately doesn't
include it — the offset of `PeerAccepted` in `struct rdp_freerdp_listener`
is stable within FreeRDP 3.x but isn't part of the documented ABI, so we
keep that detail in the example where it can be patched without modifying
the package.

## Threading model

FreeRDP itself is **not thread-safe** at the instance level. We follow:

- One `RdpClient` per thread, *or* serialise with your own lock.
- One `RdpPeer` per thread on the server side. `RdpServer.serve_forever`
  with `threaded=True` (default) gives you exactly that: one daemon thread
  per peer.
- The `RdpServer` accept loop runs on whichever thread calls
  `serve_forever`; it's safe to call `stop()` from another thread.
- The `PeerAccepted` C callback runs on the listener's internal thread
  in some FreeRDP builds and on the main thread in others. The bridge
  must use a thread-safe queue regardless.

## What's not covered (future milestones)

- **Display rendering.** FreeRDP gives us pixel updates (client) or expects
  them (server) via `rdpUpdate`. Wiring into Pillow/Qt/Pygame/AppKit/etc.
  depends entirely on the embedder's stack.
- **Audio routing.** PCM samples surface through the `rdpsnd` / `audin`
  channel callbacks but are silently dropped by default — wire them into
  PortAudio / SoundIO / WASAPI in your subclass. See
  [docs/CHANNELS.md](CHANNELS.md) for the override pattern.
- **Video decoding.** `rdpgfx` negotiates and registers, but H.264 /
  RemoteFX-Progressive frames arrive raw — feed them into your decoder.
- **NLA on the server side without WinPR auth integration.** Skeleton
  works but real auth needs you to provide a `Logon` callback that
  validates against your user store (LDAP/PAM/etc.). The
  `pyfreerdp.winpr.parse_logon_identity()` helper decodes the
  SEC_WINNT_AUTH_IDENTITY for you; matching against a store is your code.

## Channels framework

The `pyfreerdp.channels` subpackage implements the static + dynamic virtual
channel layer on top of FreeRDP's plugin system. Architecturally:

```
+-----------------------------+
|  RdpSettings.channels=[...] |  user-facing list of ChannelSpec instances
+-------------+---------------+
              |
              v
+-----------------------------+
|        ChannelManager       |  per-session orchestrator (one per RdpClient
|                             |  or per peer on the server side)
+-------------+---------------+
              |  attach() each spec
              v
+-----------------------------+
|  freerdp_client_add_static_ |  bound from libfreerdp-client3 /
|  channel  /  _add_dynamic_  |  libfreerdp-server3
+-----------------------------+
              |
              v   (channel name + key=value params land in MCS PDU)
        FreeRDP plugin loader
              |
              v
+-----------------------------+
|  FreeRDP's C-side plugin    |  cliprdr.so, drive.so, disp.so, ...
|  for each channel name      |  bundled with libfreerdp
+-----------------------------+
```

Static channels are advertised in the MCS connect-initial PDU at handshake
time, so all `attach()` calls must happen *before* `freerdp_connect()`.
Dynamic channels (DVC) are multiplexed over the static `drdynvc` channel
and can be opened later — but the binding sets `SupportDynamicChannels=1`
automatically as soon as any `IS_DYNAMIC=True` spec is attached, so
DRDYNVC is advertised in the static list when you need it.

### Why some channels are stubs

`rdpsnd`, `audin`, and `rdpgfx` ship as `ChannelSpec` subclasses that
*register* the channel and let FreeRDP's plugin handle wire decoding,
but the high-level Python hook (`on_pcm`, `next_pcm`, `on_frame`) is a
no-op by default. The reason is that bridging decoded media to a real
output stack — PortAudio, CoreAudio, WASAPI, V4L2, FFmpeg — pulls in
platform-specific dependencies we don't want to force on every user.
The override path is documented in [docs/CHANNELS.md](CHANNELS.md).

## WinPR layer

`pyfreerdp.winpr` wraps a deliberately small subset of WinPR:

- `Stream` — pure-Python wStream-equivalent. We implement reads/writes
  in Python rather than calling into `Stream_New / Stream_Read_*` for
  three reasons: avoiding FFI per-byte overhead; sidestepping a wStream
  lifetime dance (alloc on the C side, free on Python GC); making the
  class testable without `libwinpr3` loaded. Buffers produced by Stream
  are byte-compatible with what WinPR's wStream produces, so payloads
  can be passed straight to channel send paths.
- `query_security_package(name)` — confirm SSPI package availability.
  Useful as a server-startup health check ("can we do NLA?") before
  binding the listener.
- `parse_logon_identity(c_void_p)` — decode the
  SEC_WINNT_AUTH_IDENTITY_W struct passed to peer Logon callbacks.

Threading, synchronization, registry, file-path, and pipe shims from
WinPR are not exposed: Python's stdlib does all of those better.

# Virtual channels

FreeRDP 3 links its channels into the client and server libraries; there are
no plugin files to load. Everything you need to use them is in the header
mirror.

## Which channels are compiled in

`scripts/build_freerdp.py` enables a default set, listed in
[../scripts/BUILD.md](../scripts/BUILD.md#channels). At runtime, ask the
client library's addin resolver:

```python
from pyfreerdpnative import load
from pyfreerdpnative.freerdp import addin

api = load()
STATIC, DYNAMIC, DEVICE, ENTRYEX = (addin.FREERDP_ADDIN_CHANNEL_STATIC,
                                    addin.FREERDP_ADDIN_CHANNEL_DYNAMIC,
                                    addin.FREERDP_ADDIN_CHANNEL_DEVICE,
                                    addin.FREERDP_ADDIN_CHANNEL_ENTRYEX)
for name, flags in (("cliprdr", STATIC | ENTRYEX), ("rdpgfx", DYNAMIC), ("drive", DEVICE)):
    entry = api.freerdp_channels_load_static_addin_entry(name.encode(), None, None, flags)
    print(name, "yes" if entry else "no")
```

Server-side channels are plain functions: `api.has("cliprdr_server_context_new")`.

## Client-side channel contexts

Each client channel has a header under `freerdp/client/` and a context struct
whose callbacks you fill in from `PostConnect` / the channel-connected event.
The types are in the mirror module of that header:

```python
from pyfreerdpnative.freerdp.client import cliprdr as C

ctx = ctypes.cast(ptr, ctypes.POINTER(C.CliprdrClientContext)).contents
# callbacks are CFUNCTYPE fields; keep a reference to every callback object
# you assign, or ctypes garbage-collects it while C still holds the pointer
on_formats = C.pcCliprdrServerFormatList(my_python_handler)
ctx.ServerFormatList = on_formats
```

Constants for a channel (`CB_FORMAT_LIST`, `CF_UNICODETEXT`, …) live in
`freerdp/channels/<name>.py`, mirroring `include/freerdp/channels/<name>.h`.

## Server-side channel contexts

Headers under `freerdp/server/` define `*_server_context_new(HANDLE vcm)` and
a context struct with `Start`/`Stop` and per-message callbacks:

```python
from pyfreerdpnative.freerdp.server import cliprdr as S

srv = api.cliprdr_server_context_new(vcm)          # POINTER(CliprdrServerContext)
srv.contents.custom = my_state_pointer
srv.contents.ClientFormatList = S.psCliprdrClientFormatList(handler)
srv.contents.Start(srv)
```

## Dynamic channels from Python

`freerdp/utils/drdynvc.py` and `freerdp/dvc.py` expose the DVC plugin
interfaces (`IWTSPlugin`, `IWTSListenerCallback`, `IWTSVirtualChannelCallback`)
as ctypes structures of function pointers. Implementing a custom dynamic
channel means filling those with `CFUNCTYPE` callbacks — the same shape as the
C plugins in `channels/*/client/`.

## The one rule that bites

A `CFUNCTYPE` object assigned into a struct is **not** kept alive by the
struct. Store every callback object on a Python object that outlives the
session, or the callback pointer dangles and the process crashes on the next
call from C.

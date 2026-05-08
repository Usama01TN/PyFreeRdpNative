# Virtual Channels in pyfreerdp

This document describes every virtual channel exposed by `pyfreerdp.channels`,
what FreeRDP's underlying C plugin does, and what you need to do at the
Python layer to make each one useful.

For the architectural overview of how channels integrate with `RdpClient` /
`RdpServer`, see [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Framework

Every channel is a subclass of `ChannelSpec`:

```python
from pyfreerdp.channels import ChannelSpec, ChannelDirection

class MySpec(ChannelSpec):
    NAME = b"MYCHAN"           # bytes; <= 8 chars for static channels
    IS_DYNAMIC = False         # True for DRDYNVC-multiplexed channels
    DIRECTION = ChannelDirection.BOTH

    def params(self):
        return ["my=arg"]      # CLI-style args FreeRDP's loader accepts

    def on_data(self, buf, flags):
        # called when bytes arrive on this channel
        ...
```

You attach specs to a session by populating `RdpSettings.channels` (client)
or `RdpServerSettings.channels` (server). The session's `ChannelManager`
calls `freerdp_client_add_static_channel` /
`freerdp_client_add_dynamic_channel` against the native settings before
the connect handshake runs.

### Static vs dynamic

| | Static | Dynamic |
|---|---|---|
| Advertised in | MCS connect-initial PDU | DRDYNVC PDU after handshake |
| Name length | ≤ 8 chars | unlimited |
| Open timing | All-or-nothing at connect | On-demand any time |
| Examples | cliprdr, rdpdr, rail | disp, rdpei, rdpgfx, audin |

The binding sets `SupportDynamicChannels=1` on `rdpSettings` automatically
when any `IS_DYNAMIC=True` spec is attached, so you don't have to enable
DRDYNVC manually.

## Working channels

These have full FreeRDP plugin backing and a usable Python API.

### `cliprdr` — Clipboard Virtual Channel (MS-RDPECLIP)

```python
from pyfreerdp import ClipboardChannel

cb = ClipboardChannel(enable_text=True, enable_html=True, enable_files=False)
settings.channels = [cb]
# After connect:
cb.set_text("hello world")            # push to remote
text = cb.get_text(timeout=2.0)       # pull from remote (blocks)
```

Format negotiation, capability exchange, and the FormatList /
FormatDataRequest / FormatDataResponse round-trip all happen inside
FreeRDP's `cliprdr.so` plugin. Text is encoded/decoded as `CF_UNICODETEXT`
(UTF-16LE with NUL terminator) on the wire.

`enable_files=True` is required to receive file-list paste from the
remote, but actually receiving the file *contents* requires `rdpdr`
drive redirection too (the file-copy flow uses both channels).

### `rdpdr` (drives) — Device Redirection (MS-RDPEFS)

```python
from pyfreerdp import DriveRedirection, DriveRedirectionChannel

drive = DriveRedirection(name="share", local_path="/home/me/shared",
                         read_only=False)
settings.channels = [DriveRedirectionChannel(drives=[drive])]
```

The remote sees `\\TSCLIENT\share` backed by your local `/home/me/shared`.
FreeRDP's `drive` channel module handles every NTFS-style operation
(create, read, write, lock, query). Read-only mode is enforced server-side
by FreeRDP rejecting write IRPs.

**Limitations:**
- `name` ≤ 8 chars (FreeRDP truncates silently otherwise; we reject early).
- Printer / smartcard / serial / parallel sub-protocols are not exposed
  through this binding. They have working FreeRDP plugins but require
  platform-specific glue we don't pull in. Use `CustomChannel` if you
  need them.

### `disp` — Display Control (MS-RDPEDISP)

```python
from pyfreerdp import DisplayControlChannel

disp = DisplayControlChannel()
settings.channels = [disp]
# After connect:
disp.send_resize(2560, 1440, desktop_scale_percent=125)
```

Lets the client tell the server "the user resized me; rerender at this
size" without reconnecting. Dynamic channel; one-way (client → server).

`send_resize()` deduplicates identical layouts so a too-eager resize
handler doesn't spam the wire.

### `rail` — Remote Application Integrated Locally (MS-RDPERP)

```python
from pyfreerdp import RailChannel

rail = RailChannel(exec_app="notepad.exe", working_dir="C:\\Users\\Public")
rail.on_window_created = lambda wid, title: print("opened", title)
rail.on_window_destroyed = lambda wid: print("closed", wid)
settings.channels = [rail]
```

Used when the server runs in RemoteApp mode and emits individual window
state to the client instead of a full desktop. Bidirectional. We expose
the most common window-state callbacks; the full RAIL surface (~30 PDU
types) is large and beyond the binding's scope.

### `rdpei` — Multitouch Input (MS-RDPEI)

```python
from pyfreerdp import MultitouchChannel as MT

ei = MT()
settings.channels = [ei]
# After connect, on a touch event:
ei.send_contact(contact_id=0, x=100, y=200,
                state=MT.STATE_INRANGE | MT.STATE_INCONTACT
                      | MT.STATE_ENGAGED)
```

Dynamic, client → server only. Each "contact" is a finger; the protocol
allows arbitrary numbers of simultaneous contacts.

State sequences:
- finger down: `STATE_INRANGE | STATE_INCONTACT | STATE_ENGAGED`
- move: `STATE_INRANGE | STATE_INCONTACT`
- up: `STATE_INRANGE` (without `INCONTACT`)
- cancel: `STATE_CANCELED`

### `encomsp` — Encompassing Multiparty (MS-RDPEMC)

```python
from pyfreerdp import EncompChannel
e = EncompChannel()
e.on_participant_changed = lambda parts: print(parts)
e.on_control_changed = lambda pid: print("control to", pid)
```

Used in remote-assistance scenarios with multiple participants. Bidirectional.
Most embedders don't need this, but it ships in any FreeRDP build with
`-DWITH_SERVER=ON`.

### `remdesk` — Remote Desktop Channel (MS-RA)

```python
from pyfreerdp import RemdeskChannel
rd = RemdeskChannel()
rd.on_ticket_exchanged = lambda expert, novice: ...
```

Supplemental channel for Windows Remote Assistance ticket exchange.
Skip it unless you're building an RA-compatible server.

### DRDYNVC — Dynamic Virtual Channel transport

The static channel that multiplexes all DVCs. You don't construct this
directly; it's added implicitly when any `IS_DYNAMIC=True` spec is
attached. `DynamicChannelManager` provides bookkeeping:

```python
from pyfreerdp import DynamicChannelManager

dvc_mgr = DynamicChannelManager(client.channels)

def on_my_dvc_data(buf, flags):
    print("got", len(buf), "bytes")

dvc_mgr.register("MYDVC", on_my_dvc_data)
```

### `CustomChannel` — bring-your-own

```python
from pyfreerdp import CustomChannel

def on_data(buf, flags):
    print("received", buf)

mychan = CustomChannel(name=b"PYECHO", on_data=on_data, dynamic=False)
settings.channels = [mychan]
# After connect:
mychan.send(b"hello")
```

Use this when you have your own protocol layered on top of RDP. Static
channels are limited to 8-char names; pass `dynamic=True` for
arbitrary-length DVC names.

## Stub channels (registration only)

These have working FreeRDP plugins but require platform-specific media
glue we don't ship by default. The `ChannelSpec` subclass registers the
channel and negotiates with the peer; the plugin processes the wire
protocol; the *Python* hook (`on_pcm`, `next_pcm`, `on_frame`) is a
no-op by default. Subclass and override to wire your stack.

### `rdpsnd` — Audio Output (MS-RDPEA)

Server → client PCM. FreeRDP's `rdpsnd` plugin handles format negotiation
(AAC / GSM / PCM), codec decompression, and surfaces decoded PCM through
its wave-data callback.

```python
from pyfreerdp import AudioOutChannel
import sounddevice            # your PortAudio binding of choice

class MyAudioOut(AudioOutChannel):
    def __init__(self):
        super(MyAudioOut, self).__init__()
        self.stream = sounddevice.OutputStream(
            samplerate=44100, channels=2, dtype="int16")
        self.stream.start()

    def on_pcm(self, samples, sample_rate, channels, format_name):
        self.stream.write(samples)

settings.channels = [MyAudioOut()]
```

### `audin` — Audio Input (MS-RDPEAI)

Client → server PCM (microphone). Dynamic channel. Override `next_pcm`
to feed real samples:

```python
from pyfreerdp import AudioInChannel

class MyAudioIn(AudioInChannel):
    def next_pcm(self, num_samples):
        # Pull `num_samples` interleaved s16le samples from your capture
        # device and return as bytes. Return None for silence.
        return capture_device.read(num_samples * 2 * self.channels)
```

### `rdpgfx` — Graphics Pipeline (MS-RDPEGFX)

Server → client encoded video frames. Dynamic. Override `on_frame`:

```python
from pyfreerdp import GraphicsPipelineChannel

class MyGfx(GraphicsPipelineChannel):
    def __init__(self):
        super(MyGfx, self).__init__(prefer_h264=True)
        self.decoder = MyH264Decoder()      # your stack

    def on_frame(self, codec, payload, surface_id, x, y, w, h):
        if codec == "h264":
            frame = self.decoder.decode(payload)
            self.renderer.blit(frame, x, y, w, h)
        # other codecs: 'rfx', 'avc444', 'progressive', 'planar'
```

## Channel direction reference

| Channel | Direction | Type |
|---|---|---|
| cliprdr | bidirectional | static |
| rdpdr | bidirectional | static |
| rail | bidirectional | static |
| encomsp | bidirectional | static |
| remdesk | bidirectional | static |
| disp | client → server | dynamic |
| rdpei | client → server | dynamic |
| audin | client → server | dynamic |
| rdpsnd | server → client | static |
| rdpgfx | server → client | dynamic |
| DRDYNVC | bidirectional | static (transport) |

## Error handling

All channel-related errors derive from `ChannelError`:

- `ChannelOpenError` — `freerdp_client_add_static_channel` /
  `_add_dynamic_channel` rejected the channel. Usually means the name
  is unknown to your FreeRDP build (build it with channel support) or
  the same name is already attached.
- `ChannelClosedError` — `send()` / `get_*()` called on a channel
  whose session has been disconnected.

Wrap channel operations in `try / except ChannelError` to handle both
cases at once.

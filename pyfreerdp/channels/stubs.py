"""
Channel stubs - registration scaffolding for channels whose implementation
requires platform-specific media stack wiring that this binding doesn't
ship.

These channels are *registered* by attaching one of the spec classes
below; the channel itself negotiates with the peer and FreeRDP's plugin
processes the wire protocol. What's missing is the routing of decoded
samples / frames to a real audio device or video pipeline.

If you attach one of these without overriding the relevant hook method,
the data is silently dropped. That's deliberate: a server that *advertises*
audio support but doesn't deliver a real audio stream is preferable to
one that hard-fails the connection.

To make these "really work" you subclass and override the hooks to
forward data into your stack:
  * AudioOutChannel.on_pcm    -> push samples to PortAudio / SoundIO / etc.
  * AudioInChannel.next_pcm   -> pull samples from your capture device.
  * GraphicsPipelineChannel.on_frame -> push frames into your renderer.
"""

from .base import ChannelSpec, ChannelDirection


class AudioOutChannel(ChannelSpec):
    """
    rdpsnd - Audio Output Virtual Channel (MS-RDPEA).

    Server -> client. The remote sends PCM audio that the client should
    play. FreeRDP's rdpsnd plugin handles format negotiation, codec
    decompression (AAC / GSM / etc.), and surfaces decoded PCM through
    the plugin's wave-data callback.

    Default behavior: drop PCM samples. Subclass and override on_pcm()
    to route them to a real device.

    Construction:
      formats: list of preferred PCM formats. Default ['s16le-44100-2']
               (16-bit signed LE stereo at 44.1 kHz). FreeRDP will
               negotiate down if the server can't produce this format.
    """

    NAME = b"rdpsnd"
    IS_DYNAMIC = False
    DIRECTION = ChannelDirection.SERVER_TO_CLIENT

    def __init__(self, formats=None):
        super(AudioOutChannel, self).__init__()
        self.formats = list(formats) if formats else ["s16le-44100-2"]

    def params(self):
        # FreeRDP's rdpsnd CLI args: format=<list>, sys=<backend>.
        return ["format:{0}".format(",".join(self.formats))]

    def on_pcm(self, samples, sample_rate, channels, format_name):
        """Override to consume decoded PCM. Default: drop silently."""


class AudioInChannel(ChannelSpec):
    """
    audin - Audio Input Virtual Channel (MS-RDPEAI).

    Client -> server. Client microphone audio gets piped to the remote.
    Dynamic channel.

    Default behavior: emit silence. Subclass and override next_pcm() to
    feed real samples from your capture device.
    """

    NAME = b"audin"
    IS_DYNAMIC = True
    DIRECTION = ChannelDirection.CLIENT_TO_SERVER

    def __init__(self, sample_rate=44100, channels=2):
        super(AudioInChannel, self).__init__()
        self.sample_rate = sample_rate
        self.channels = channels

    def params(self):
        return [
            "rate:{0}".format(self.sample_rate),
            "channel:{0}".format(self.channels),
        ]

    def next_pcm(self, num_samples):
        """Return `num_samples` worth of interleaved PCM bytes, or None
        for silence. Override in subclasses to read from a capture device."""
        return None


class GraphicsPipelineChannel(ChannelSpec):
    """
    rdpgfx - Graphics Pipeline Virtual Channel (MS-RDPEGFX).

    Server -> client. Replaces classic bitmap updates with H.264 or
    RemoteFX-Progressive frames. Dynamic channel.

    Default behavior: drop frames. Subclass and override on_frame() to
    route encoded frames into your video decoder + renderer.
    """

    NAME = b"rdpgfx"
    IS_DYNAMIC = True
    DIRECTION = ChannelDirection.SERVER_TO_CLIENT

    def __init__(self, prefer_h264=True, prefer_avc444=False):
        super(GraphicsPipelineChannel, self).__init__()
        self.prefer_h264 = prefer_h264
        self.prefer_avc444 = prefer_avc444

    def params(self):
        out = []
        if not self.prefer_h264:
            out.append("h264:0")
        if self.prefer_avc444:
            out.append("avc444:1")
        return out

    def on_frame(self, codec, payload, surface_id, x, y, w, h):
        """Override to consume an encoded frame. Default: drop silently."""

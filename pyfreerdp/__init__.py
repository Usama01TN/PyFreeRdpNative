"""
pyfreerdp - Python ctypes bindings for FreeRDP (client + server).

Quick start (client):

    from pyfreerdp import RdpClient, RdpSettings, ClipboardChannel

    settings = RdpSettings(
        host="rdp.example.com", username="alice", password="hunter2",
        channels=[ClipboardChannel()],
    )
    with RdpClient(settings) as client:
        client.send_unicode(ord("h"), pressed=True)
        client.run_event_loop(timeout=5.0)

Quick start (server):

    from pyfreerdp import RdpServer, RdpServerSettings

    def handle(peer):
        with peer:
            peer.run(timeout=60)

    settings = RdpServerSettings(
        bind_address="0.0.0.0", port=3389,
        certificate_file="server.crt", private_key_file="server.key",
    )
    with RdpServer(settings, handle) as server:
        server.serve_forever()

Style note
----------
The whole package is written in Python-2-compatible syntax (no f-strings,
no type annotations, no dataclasses) but only ever runs on Python 3.
This is a project style preference; see README for rationale.
"""

from .version import __version__, FREERDP_VERIFIED_VERSION, FREERDP_MIN_VERSION

# --- Errors ---
from .errors import (
    RdpError,
    FreeRdpNotFoundError,
    RdpConnectionError,
    RdpAuthenticationError,
    RdpProtocolError,
    ChannelError,
    WinPRError,
)

# --- Client side ---
from .settings import RdpSettings, SecurityProtocol, GfxCodec
from .client import RdpClient

# --- Server side ---
from .server_settings import RdpServerSettings
from .server import RdpServer
from .peer import RdpPeer

# --- Display events (rendering callbacks on RdpClient) ---
from .display import (
    PixelFormat,
    BitmapRect,
    BitmapUpdate,
    PaletteUpdate,
    SurfaceBits,
    PointerUpdate,
)

# --- Channels (re-exported for flat namespace convenience) ---
from .channels import (
    ChannelSpec,
    ChannelDirection,
    ChannelManager,
    ChannelOpenError,
    ChannelClosedError,
    ClipboardChannel,
    ClipboardFormat,
    DriveRedirection,
    DriveRedirectionChannel,
    DisplayControlChannel,
    RailChannel,
    MultitouchChannel,
    EncompChannel,
    RemdeskChannel,
    DynamicChannelManager,
    CustomChannel,
    AudioOutChannel,
    AudioInChannel,
    GraphicsPipelineChannel,
)

# --- WinPR helpers ---
from .winpr import (
    Stream,
    StreamError,
    SecurityPackageInfo,
    AuthIdentity,
    query_security_package,
    parse_logon_identity,
)

# --- Loader introspection ---
from .loader import (
    find_freerdp_library,
    find_freerdp_server_library,
    find_winpr_library,
    get_loaded_library_path,
    get_loaded_server_library_path,
    get_loaded_winpr_library_path,
)


__all__ = [
    # Version
    "__version__", "FREERDP_VERIFIED_VERSION", "FREERDP_MIN_VERSION",

    # Errors
    "RdpError", "FreeRdpNotFoundError", "RdpConnectionError",
    "RdpAuthenticationError", "RdpProtocolError",
    "ChannelError", "WinPRError",

    # Client
    "RdpClient", "RdpSettings", "SecurityProtocol", "GfxCodec",

    # Server
    "RdpServer", "RdpServerSettings", "RdpPeer",

    # Display events
    "PixelFormat", "BitmapRect", "BitmapUpdate",
    "PaletteUpdate", "SurfaceBits", "PointerUpdate",

    # Channels
    "ChannelSpec", "ChannelDirection", "ChannelManager",
    "ChannelOpenError", "ChannelClosedError",
    "ClipboardChannel", "ClipboardFormat",
    "DriveRedirection", "DriveRedirectionChannel",
    "DisplayControlChannel",
    "RailChannel",
    "MultitouchChannel",
    "EncompChannel",
    "RemdeskChannel",
    "DynamicChannelManager",
    "CustomChannel",
    "AudioOutChannel", "AudioInChannel", "GraphicsPipelineChannel",

    # WinPR
    "Stream", "StreamError",
    "SecurityPackageInfo", "AuthIdentity",
    "query_security_package", "parse_logon_identity",

    # Loader
    "find_freerdp_library", "find_freerdp_server_library",
    "find_winpr_library",
    "get_loaded_library_path", "get_loaded_server_library_path",
    "get_loaded_winpr_library_path",
]

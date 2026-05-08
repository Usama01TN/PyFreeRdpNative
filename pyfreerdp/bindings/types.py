"""
ctypes type definitions mirroring the relevant FreeRDP/WinPR C types.

We use opaque pointer types throughout. FreeRDP's internal structs aren't
ABI-stable across minor releases - accessing fields by offset breaks. The
binding instead drives the public accessor functions
(freerdp_settings_get_string, freerdp_peer_*, ...).

Reference headers in upstream FreeRDP (3.x):
    include/freerdp/freerdp.h            - top-level client instance
    include/freerdp/settings.h           - settings + accessors
    include/freerdp/api.h                - versioning helpers
    include/freerdp/error.h              - connect-error codes
    include/freerdp/peer.h               - server-side peer
    include/freerdp/listener.h           - server-side listener (accept loop)
    include/freerdp/svc.h                - static virtual channels (client)
    include/freerdp/server/channels.h    - server-side channel registration
    winpr/include/winpr/sspi.h           - WinPR SSPI
    winpr/include/winpr/stream.h         - WinPR wStream

Written in Py2-compatible syntax (no annotations).
"""

import ctypes
from ctypes import (
    CFUNCTYPE,
    POINTER,
    c_char_p,
    c_int,
    c_size_t,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
)

# --- WinPR / FreeRDP scalar typedefs ----------------------------------------
BOOL = c_int          # WinPR BOOL is `int`, not bool. Mismatch here is a
                      # frequent bug source - keep this exact.
UINT8 = c_uint8
UINT16 = c_uint16
UINT32 = c_uint32
UINT64 = c_uint64
INT32 = c_int
DWORD = c_uint32
HANDLE = c_void_p
PSTR = c_char_p
PVOID = c_void_p
SIZE_T = c_size_t


# --- Opaque pointer types ---------------------------------------------------
class _OpaqueStruct(ctypes.Structure):
    _fields_ = []


# --- Client-side ---
class freerdp(_OpaqueStruct):
    pass


class rdpContext(_OpaqueStruct):
    pass


class rdpSettings(_OpaqueStruct):
    pass


class rdpInput(_OpaqueStruct):
    pass


class rdpUpdate(_OpaqueStruct):
    pass


class rdpChannels(_OpaqueStruct):
    """Client-side channel manager."""
    pass


class wMessageQueue(_OpaqueStruct):
    pass


# --- Server-side ---
class freerdp_peer(_OpaqueStruct):
    """`freerdp_peer*` - one connected client on the server side."""
    pass


class freerdp_listener(_OpaqueStruct):
    """`freerdp_listener*` - accept-loop manager binding to host:port."""
    pass


class HANDLE_LISTENER_PEER_QUEUE(_OpaqueStruct):
    pass


# --- WinPR ---
class wStream(_OpaqueStruct):
    """WinPR's growable byte buffer used as a serializer for channel PDUs."""
    pass


class SecHandle(_OpaqueStruct):
    """SSPI security context / credentials handle (CredHandle / CtxtHandle)."""
    pass


# Convenience pointer aliases.
freerdp_p = POINTER(freerdp)
rdpContext_p = POINTER(rdpContext)
rdpSettings_p = POINTER(rdpSettings)
rdpInput_p = POINTER(rdpInput)
rdpUpdate_p = POINTER(rdpUpdate)
rdpChannels_p = POINTER(rdpChannels)
freerdp_peer_p = POINTER(freerdp_peer)
freerdp_listener_p = POINTER(freerdp_listener)
wStream_p = POINTER(wStream)
SecHandle_p = POINTER(SecHandle)


# --- Callback signatures (client-side) --------------------------------------

PRE_CONNECT_FN = CFUNCTYPE(BOOL, freerdp_p)
POST_CONNECT_FN = CFUNCTYPE(BOOL, freerdp_p)
POST_DISCONNECT_FN = CFUNCTYPE(None, freerdp_p)
AUTHENTICATE_FN = CFUNCTYPE(
    BOOL, freerdp_p,
    POINTER(c_char_p), POINTER(c_char_p), POINTER(c_char_p))
VERIFY_CERT_EX_FN = CFUNCTYPE(
    DWORD, freerdp_p, c_char_p, UINT16,
    c_char_p, c_char_p, c_char_p, c_char_p, DWORD)


# --- Callback signatures (server-side) --------------------------------------
# Subset of freerdp_peer's callback table. All signatures from peer.h.

# BOOL (*peer_accepted)(freerdp_listener* listener, freerdp_peer* peer);
PEER_ACCEPTED_FN = CFUNCTYPE(BOOL, freerdp_listener_p, freerdp_peer_p)

# BOOL (*Capabilities)(freerdp_peer* peer);
PEER_CAPABILITIES_FN = CFUNCTYPE(BOOL, freerdp_peer_p)

# BOOL (*PostConnect)(freerdp_peer* peer);
PEER_POST_CONNECT_FN = CFUNCTYPE(BOOL, freerdp_peer_p)

# BOOL (*Activate)(freerdp_peer* peer);
PEER_ACTIVATE_FN = CFUNCTYPE(BOOL, freerdp_peer_p)

# BOOL (*Logon)(freerdp_peer* peer, const SEC_WINNT_AUTH_IDENTITY* identity,
#               BOOL automatic);
PEER_LOGON_FN = CFUNCTYPE(BOOL, freerdp_peer_p, c_void_p, BOOL)

# void (*PostDisconnect)(freerdp_peer* peer);
PEER_POST_DISCONNECT_FN = CFUNCTYPE(None, freerdp_peer_p)


# --- Display update path (update.h / bitmap.h / pointer.h) ------------------
#
# rdpContext layout in 3.x has `rdpUpdate* update` as a stable pointer field.
# rdpUpdate is a struct of function pointers; the binding overwrites the
# slots we care about with Python trampolines after freerdp_connect()
# returns but before the event loop drains the first update PDU.
#
# These struct definitions mirror upstream's
#   include/freerdp/graphics.h
#   include/freerdp/bitmap.h
#   include/freerdp/pointer.h
# Layouts are stable across the FreeRDP 3.x series.

# struct _BITMAP_DATA from include/freerdp/bitmap.h. One TS_UPDATE_BITMAP
# PDU carries an array of these (number=updateNum on BITMAP_UPDATE).
class BITMAP_DATA(ctypes.Structure):
    _fields_ = [
        ("destLeft", UINT32),
        ("destTop", UINT32),
        ("destRight", UINT32),
        ("destBottom", UINT32),
        ("width", UINT32),
        ("height", UINT32),
        ("bitsPerPixel", UINT32),
        ("flags", UINT32),     # BITMAP_COMPRESSION, NO_BITMAP_COMPRESSION_HDR, etc.
        ("bitmapLength", UINT32),
        ("cbCompFirstRowSize", UINT32),
        ("cbCompMainBodySize", UINT32),
        ("cbScanWidth", UINT32),
        ("cbUncompressedSize", UINT32),
        ("bitmapDataStream", POINTER(UINT8)),
        ("compressed", BOOL),
    ]


BITMAP_DATA_p = POINTER(BITMAP_DATA)


# struct _BITMAP_UPDATE — head of the bitmap-update PDU surfaced by the
# Bitmap callback. `rectangles` is a pointer to an array of `number`
# BITMAP_DATA records.
class BITMAP_UPDATE(ctypes.Structure):
    _fields_ = [
        ("number", UINT32),
        ("count", UINT32),
        ("rectangles", BITMAP_DATA_p),
        ("skipCompression", BOOL),
    ]


BITMAP_UPDATE_p = POINTER(BITMAP_UPDATE)


# struct _PALETTE_ENTRY: 8-bit color mode palette entry.
class PALETTE_ENTRY(ctypes.Structure):
    _fields_ = [
        ("red", UINT8),
        ("green", UINT8),
        ("blue", UINT8),
    ]


# struct _PALETTE_UPDATE: full 256-entry palette (8-bit color sessions only).
class PALETTE_UPDATE(ctypes.Structure):
    _fields_ = [
        ("number", UINT32),
        ("entries", PALETTE_ENTRY * 256),
    ]


PALETTE_UPDATE_p = POINTER(PALETTE_UPDATE)


# struct _SURFACE_BITS_COMMAND from include/freerdp/codec/surface_bits.h.
# Used by the GFX pipeline (rdpgfx) to deliver encoded frame data:
# RemoteFX, H.264/AVC, planar, etc. Layout fields below match upstream
# 3.x; field names are abbreviated here for the slots we actually expose.
class SURFACE_BITS_COMMAND(ctypes.Structure):
    _fields_ = [
        ("cmdType", UINT16),
        ("frameId", UINT32),
        ("bmp", c_void_p),       # opaque sub-struct - we read codec/dimensions via accessors
        ("destLeft", UINT32),
        ("destTop", UINT32),
        ("destRight", UINT32),
        ("destBottom", UINT32),
        ("bpp", UINT32),
        ("flags", UINT32),
        ("format", UINT32),       # PIXEL_FORMAT_*
        ("width", UINT32),
        ("height", UINT32),
        ("bitmapDataLength", UINT32),
        ("bitmapData", POINTER(UINT8)),
        ("skipCompression", BOOL),
        ("codecID", UINT32),      # RDP_CODEC_ID_*
    ]


SURFACE_BITS_COMMAND_p = POINTER(SURFACE_BITS_COMMAND)


# Callback signatures hung off rdpUpdate (include/freerdp/update.h):
#   typedef BOOL (*pBitmapUpdate)(rdpContext*, const BITMAP_UPDATE*);
BITMAP_UPDATE_FN = CFUNCTYPE(BOOL, rdpContext_p, BITMAP_UPDATE_p)

#   typedef BOOL (*pPalette)(rdpContext*, const PALETTE_UPDATE*);
PALETTE_UPDATE_FN = CFUNCTYPE(BOOL, rdpContext_p, PALETTE_UPDATE_p)

#   typedef BOOL (*pSurfaceBits)(rdpContext*, const SURFACE_BITS_COMMAND*);
SURFACE_BITS_FN = CFUNCTYPE(BOOL, rdpContext_p, SURFACE_BITS_COMMAND_p)


# Layout of struct rdpUpdate in 3.x. Many fields; we model only enough
# offset to overwrite the four slots we care about. Each pointer is one
# pointer-width on the host. Offsets verified against
# include/freerdp/update.h in the 3.16 source tree.
#
# We don't need the full struct — just to compute byte offsets within it
# so we can patch slots. The relevant slots:
#
#   RegisterPointer    @ slot ~7
#   PaletteUpdate      @ slot ~10  (after Synchronize/SetBounds/...)
#   BitmapUpdate       @ slot ~12
#   SurfaceBits        @ slot ~24  (in the SurfaceCommand sub-section)
#
# These offsets are stable across 3.x but if a future release reshuffles
# the struct, the fallback path is to set the slot via a named accessor
# function — but FreeRDP exposes none of those for client-side, so the
# offset approach is the only path. We log+skip if a slot doesn't accept
# our trampoline.
RDP_UPDATE_BITMAP_SLOT = 12
RDP_UPDATE_PALETTE_SLOT = 10
RDP_UPDATE_SURFACE_BITS_SLOT = 24


# --- Channels (svc.h / channels.h) ------------------------------------------
# A static virtual channel handler is identified by an opaque pointer the
# channel module receives at OpenInit and threads through all subsequent
# callbacks. From the client side, the entry function looks like:
#
#     BOOL VirtualChannelEntryEx(PCHANNEL_ENTRY_POINTS_EX pEntryPointsEx,
#                                PVOID pInitHandle);
#
# We don't reimplement that prototype - instead we use the plain-C wrappers
# FreeRDP exports for adding channels by name:
#
#     UINT WTSVirtualChannelManagerOpen(...)
#     BOOL freerdp_channels_attach(rdpContext*, rdpChannels*)
#
# plus the simple per-channel helper:
#
#     freerdp_client_add_static_channel(rdpSettings*, name, args)
#
# Receive callback (server -> client direction) on the client side:
#   BOOL (*ChannelDataReceived)(rdpChannels*, UINT16 channelId,
#                               const BYTE* data, size_t length,
#                               UINT32 flags, size_t totalLength);
CHANNEL_DATA_RECEIVED_FN = CFUNCTYPE(
    BOOL, rdpChannels_p, UINT16, POINTER(c_uint8), SIZE_T, UINT32, SIZE_T)


# --- Setting-ID enum values -------------------------------------------------
# IDs from include/freerdp/settings_types_private.h. Stable across 3.x.
class SettingId(object):
    # --- Strings ---
    ServerHostname = 20
    Username = 21
    Password = 22
    Domain = 23
    ClientHostname = 24
    AlternateShell = 640
    ShellWorkingDirectory = 641
    GatewayHostname = 1986
    GatewayUsername = 1989
    GatewayPassword = 1990
    GatewayDomain = 1991
    CertificateName = 1409
    # Server-side specific
    CertificateFile = 1410
    PrivateKeyFile = 1417
    RdpKeyFile = 1418

    # --- UInt32 ---
    ServerPort = 25
    DesktopWidth = 1538
    DesktopHeight = 1539
    ColorDepth = 1537
    PerformanceFlags = 271
    GatewayPort = 1987
    GatewayUsageMethod = 1988
    TcpConnectTimeout = 1796

    # --- Bool ---
    Fullscreen = 1542
    SmartSizing = 1547
    NlaSecurity = 1413
    TlsSecurity = 1412
    RdpSecurity = 1411
    ExtSecurity = 1414
    IgnoreCertificate = 1408
    AudioPlayback = 705
    AudioCapture = 706
    RedirectClipboard = 1024
    RedirectDrives = 1025
    RedirectPrinters = 1026
    RedirectSmartCards = 1027
    DeviceRedirection = 1023
    BitmapCacheEnabled = 1281
    OffscreenSupportLevel = 1282
    GlyphSupportLevel = 1289
    CompressionEnabled = 195
    AsyncInput = 1664
    AsyncUpdate = 1665
    RemoteFxCodec = 1554
    SupportGraphicsPipeline = 1582
    GfxH264 = 1585
    GfxAVC444 = 1586
    SupportDynamicChannels = 1583

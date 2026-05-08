"""
Bind ctypes function signatures (argtypes/restype) onto loaded FreeRDP/WinPR
CDLL handles.

Bind functions:
  * bind_client(lib)   - attaches client-side symbols on libfreerdp-client3.
  * bind_server(lib)   - attaches server-side symbols on libfreerdp-server3.
  * bind_channels(lib) - attaches channel registration helpers; lives in
                         libfreerdp-client3 and libfreerdp-server3 in 3.x,
                         so callers pass whichever they're using.
  * bind_winpr(lib)    - attaches WinPR SSPI + wStream symbols on libwinpr3.

We bind in one pass after dlopen() rather than at import time so that:
  * we produce a clear error if a symbol is missing (older FreeRDP), and
  * we don't pay the cost of attribute lookup if the user only uses one half.

Every imported symbol is documented with the matching C signature copied
from the FreeRDP/WinPR header it lives in.

Written in Py2-compatible syntax (no annotations, no f-strings).
"""

import ctypes
from ctypes import POINTER, c_char_p, c_int, c_uint8

from . import types as t


# ---------------------------------------------------------------------------
# Shared bind helper
# ---------------------------------------------------------------------------

def _bind_one(lib, name, argtypes, restype, missing_list):
    try:
        fn = getattr(lib, name)
    except AttributeError:
        missing_list.append(name)
        return None
    fn.argtypes = argtypes
    fn.restype = restype
    return fn


def _format_missing(libname, missing):
    return (
        "FreeRDP {0} library at {1} is missing required symbols: {2}. "
        "You likely have FreeRDP < 3.0 or a build without the necessary "
        "components. Install FreeRDP >= 3.0 or rebuild with the relevant "
        "WITH_* flag.".format(libname, getattr(missing[0], "_name", "?")
                              if False else "<lib>", missing))


# ---------------------------------------------------------------------------
# Client side
# ---------------------------------------------------------------------------

def bind_client(lib):
    """Attach signatures for client-side symbols. Returns the same handle."""
    missing = []

    def b(name, argtypes, restype):
        return _bind_one(lib, name, argtypes, restype, missing)

    # --- core lifecycle ---------------------------------------------------
    b("freerdp_new", [], t.freerdp_p)
    b("freerdp_free", [t.freerdp_p], None)
    b("freerdp_context_new", [t.freerdp_p], t.BOOL)
    b("freerdp_context_free", [t.freerdp_p], None)
    b("freerdp_connect", [t.freerdp_p], t.BOOL)
    b("freerdp_disconnect", [t.freerdp_p], t.BOOL)
    b("freerdp_shall_disconnect", [t.freerdp_p], t.BOOL)
    b("freerdp_get_last_error", [t.rdpContext_p], t.UINT32)
    b("freerdp_get_last_error_name", [t.UINT32], c_char_p)
    b("freerdp_get_last_error_string", [t.UINT32], c_char_p)

    # --- main loop helpers ------------------------------------------------
    b("freerdp_check_event_handles", [t.rdpContext_p], t.BOOL)
    b("freerdp_get_event_handles",
      [t.rdpContext_p, POINTER(t.HANDLE), t.DWORD], t.DWORD)

    # --- settings accessors ----------------------------------------------
    b("freerdp_settings_get_string", [t.rdpSettings_p, c_int], c_char_p)
    b("freerdp_settings_set_string", [t.rdpSettings_p, c_int, c_char_p], t.BOOL)
    b("freerdp_settings_get_uint32", [t.rdpSettings_p, c_int], t.UINT32)
    b("freerdp_settings_set_uint32", [t.rdpSettings_p, c_int, t.UINT32], t.BOOL)
    b("freerdp_settings_get_bool", [t.rdpSettings_p, c_int], t.BOOL)
    b("freerdp_settings_set_bool", [t.rdpSettings_p, c_int, t.BOOL], t.BOOL)

    # --- input injection -------------------------------------------------
    b("freerdp_input_send_keyboard_event",
      [t.rdpInput_p, t.UINT16, t.UINT16], t.BOOL)
    b("freerdp_input_send_unicode_keyboard_event",
      [t.rdpInput_p, t.UINT16, t.UINT16], t.BOOL)
    b("freerdp_input_send_mouse_event",
      [t.rdpInput_p, t.UINT16, t.UINT16, t.UINT16], t.BOOL)
    b("freerdp_input_send_extended_mouse_event",
      [t.rdpInput_p, t.UINT16, t.UINT16, t.UINT16], t.BOOL)

    # --- version (api.h) -------------------------------------------------
    b("freerdp_get_version_string", [], c_char_p)
    b("freerdp_get_version",
      [POINTER(c_int), POINTER(c_int), POINTER(c_int)], None)

    truly_required = set([
        "freerdp_new", "freerdp_free", "freerdp_context_new",
        "freerdp_context_free", "freerdp_connect", "freerdp_disconnect",
        "freerdp_settings_set_string", "freerdp_settings_set_uint32",
        "freerdp_settings_set_bool", "freerdp_get_last_error",
    ])
    really_missing = [n for n in missing if n in truly_required]
    if really_missing:
        raise AttributeError(
            "FreeRDP client library at {0} is missing required symbols: "
            "{1}. You likely have FreeRDP < 3.0 or a build without client "
            "support. Install FreeRDP >= 3.0 or rebuild with "
            "-DWITH_CLIENT=ON.".format(
                getattr(lib, "_name", "<unknown>"), really_missing))
    return lib


# ---------------------------------------------------------------------------
# Server side
# ---------------------------------------------------------------------------

def bind_server(lib):
    """Attach signatures for server-side symbols."""
    missing = []

    def b(name, argtypes, restype):
        return _bind_one(lib, name, argtypes, restype, missing)

    # --- listener (listener.h) -------------------------------------------
    b("freerdp_listener_new", [], t.freerdp_listener_p)
    b("freerdp_listener_free", [t.freerdp_listener_p], None)
    b("freerdp_listener_open",
      [t.freerdp_listener_p, c_char_p, t.UINT16], t.BOOL)
    b("freerdp_listener_open_local",
      [t.freerdp_listener_p, c_char_p], t.BOOL)
    b("freerdp_listener_check_fds", [t.freerdp_listener_p], t.BOOL)
    b("freerdp_listener_get_event_handles",
      [t.freerdp_listener_p, POINTER(t.HANDLE), t.DWORD], t.DWORD)

    # --- peer (peer.h) ---------------------------------------------------
    b("freerdp_peer_new", [c_int], t.freerdp_peer_p)
    b("freerdp_peer_free", [t.freerdp_peer_p], None)
    b("freerdp_peer_initialize", [t.freerdp_peer_p], t.BOOL)
    b("freerdp_peer_close", [t.freerdp_peer_p], t.BOOL)
    b("freerdp_peer_disconnect", [t.freerdp_peer_p], t.BOOL)
    b("freerdp_peer_check_fds", [t.freerdp_peer_p], t.BOOL)
    b("freerdp_peer_get_event_handles",
      [t.freerdp_peer_p, POINTER(t.HANDLE), t.DWORD], t.DWORD)
    b("freerdp_peer_context_new", [t.freerdp_peer_p], t.BOOL)
    b("freerdp_peer_context_free", [t.freerdp_peer_p], None)
    b("freerdp_peer_os_major_type_string", [t.freerdp_peer_p], c_char_p)

    truly_required = set([
        "freerdp_listener_new", "freerdp_listener_free",
        "freerdp_listener_open", "freerdp_listener_check_fds",
        "freerdp_peer_initialize", "freerdp_peer_close",
        "freerdp_peer_disconnect", "freerdp_peer_check_fds",
    ])
    really_missing = [n for n in missing if n in truly_required]
    if really_missing:
        raise AttributeError(
            "FreeRDP server library at {0} is missing required symbols: "
            "{1}. You likely have FreeRDP < 3.0 or a build without server "
            "support. Install FreeRDP >= 3.0 or rebuild with "
            "-DWITH_SERVER=ON.".format(
                getattr(lib, "_name", "<unknown>"), really_missing))
    return lib


# ---------------------------------------------------------------------------
# Channels - lives in libfreerdp-client3 / libfreerdp-server3 in 3.x
# ---------------------------------------------------------------------------

def bind_channels(lib):
    """
    Attach signatures for channel-management symbols. Pass in the same
    library handle you'll use to open channels - the client-side binding
    pulls these from libfreerdp-client3, the server-side from
    libfreerdp-server3.

    Most of these may be missing when libfreerdp was built without client/
    server support; the channels framework will degrade gracefully and
    only complain when the user tries to actually open a channel.
    """
    missing = []

    def b(name, argtypes, restype):
        return _bind_one(lib, name, argtypes, restype, missing)

    # client/channels.h:
    #   UINT freerdp_client_add_static_channel(rdpSettings* settings,
    #                                          size_t count, const char** params);
    # The "params" array is name + key=value pairs, e.g. ["cliprdr"] or
    # ["rdpdr", "drives", "/tmp/share"]; see FreeRDP source for examples.
    b("freerdp_client_add_static_channel",
      [t.rdpSettings_p, t.SIZE_T, POINTER(c_char_p)], t.UINT32)

    # client/channels.h:
    #   UINT freerdp_client_add_dynamic_channel(rdpSettings* settings,
    #                                           size_t count,
    #                                           const char** params);
    b("freerdp_client_add_dynamic_channel",
      [t.rdpSettings_p, t.SIZE_T, POINTER(c_char_p)], t.UINT32)

    # channels/channels.h:
    #   rdpChannels* freerdp_channels_new(freerdp* instance);
    #   void freerdp_channels_free(rdpChannels* channels);
    b("freerdp_channels_new", [t.freerdp_p], t.rdpChannels_p)
    b("freerdp_channels_free", [t.rdpChannels_p], None)

    #   int freerdp_channels_attach(freerdp* instance);
    #   int freerdp_channels_detach(freerdp* instance);
    b("freerdp_channels_attach", [t.freerdp_p], t.INT32)
    b("freerdp_channels_detach", [t.freerdp_p], t.INT32)

    #   int freerdp_channels_pre_connect(rdpChannels*, freerdp*);
    #   int freerdp_channels_post_connect(rdpChannels*, freerdp*);
    #   int freerdp_channels_disconnect(rdpChannels*, freerdp*);
    b("freerdp_channels_pre_connect",
      [t.rdpChannels_p, t.freerdp_p], t.INT32)
    b("freerdp_channels_post_connect",
      [t.rdpChannels_p, t.freerdp_p], t.INT32)
    b("freerdp_channels_disconnect",
      [t.rdpChannels_p, t.freerdp_p], t.INT32)

    #   BOOL freerdp_channels_check_fds(rdpChannels*, freerdp*);
    b("freerdp_channels_check_fds",
      [t.rdpChannels_p, t.freerdp_p], t.BOOL)

    # Server side has its own helpers in server/channels.h:
    #   HANDLE WTSVirtualChannelManagerGetEventHandle(HANDLE manager);
    #   BOOL WTSVirtualChannelManagerCheckFileDescriptor(HANDLE manager);
    # WTS APIs come from WinPR; binding here for convenience.
    b("WTSVirtualChannelManagerCheckFileDescriptor", [t.HANDLE], t.BOOL)
    b("WTSVirtualChannelManagerGetEventHandle", [t.HANDLE], t.HANDLE)

    # No symbol is "truly required" - opening a channel will surface a
    # clear error if any of these are missing at use time. This is by
    # design: a server-only build of libfreerdp legitimately won't have
    # freerdp_client_add_static_channel.
    return lib


# ---------------------------------------------------------------------------
# WinPR
# ---------------------------------------------------------------------------

def bind_winpr(lib):
    """
    Attach signatures for WinPR symbols.

    We bind only what we expose in the high-level Python API:
      * SSPI primitives needed for server-side NLA Logon callbacks.
      * wStream allocation/inspection so users can construct/parse channel
        PDUs from Python.

    Threading / synchronization / registry shims from WinPR are NOT bound -
    Python's stdlib does all of those better.
    """
    missing = []

    def b(name, argtypes, restype):
        return _bind_one(lib, name, argtypes, restype, missing)

    # --- wStream (winpr/stream.h) ----------------------------------------
    # wStream* Stream_New(BYTE* buffer, size_t size);
    # void Stream_Free(wStream* s, BOOL bFreeBuffer);
    b("Stream_New", [POINTER(c_uint8), t.SIZE_T], t.wStream_p)
    b("Stream_Free", [t.wStream_p, t.BOOL], None)

    # size_t Stream_Length(wStream* s);
    # size_t Stream_GetPosition(wStream* s);
    # void Stream_SetPosition(wStream* s, size_t position);
    # BYTE* Stream_Buffer(wStream* s);
    # size_t Stream_Capacity(wStream* s);
    b("Stream_Length", [t.wStream_p], t.SIZE_T)
    b("Stream_GetPosition", [t.wStream_p], t.SIZE_T)
    b("Stream_SetPosition", [t.wStream_p, t.SIZE_T], None)
    b("Stream_Buffer", [t.wStream_p], POINTER(c_uint8))
    b("Stream_Capacity", [t.wStream_p], t.SIZE_T)

    # BOOL Stream_EnsureCapacity(wStream* s, size_t size);
    # BOOL Stream_EnsureRemainingCapacity(wStream* s, size_t size);
    b("Stream_EnsureCapacity", [t.wStream_p, t.SIZE_T], t.BOOL)
    b("Stream_EnsureRemainingCapacity", [t.wStream_p, t.SIZE_T], t.BOOL)

    # --- SSPI (winpr/sspi.h) ---------------------------------------------
    # SECURITY_STATUS QuerySecurityPackageInfoA(SEC_CHAR* pszPackageName,
    #                                           PSecPkgInfoA* ppPackageInfo);
    # The full SSPI surface is large; we expose just the call needed to
    # confirm a security package (e.g. "NTLM", "Negotiate") is available.
    b("QuerySecurityPackageInfoA",
      [c_char_p, POINTER(t.PVOID)], t.UINT32)

    # void FreeContextBuffer(PVOID pvContextBuffer);
    b("FreeContextBuffer", [t.PVOID], t.UINT32)

    # SEC_CHAR* GetSecurityPackageName(void); -- not standard; FreeRDP
    # doesn't expose it. We use QuerySecurityPackageInfoA above instead.

    # --- WinPR error -----------------------------------------------------
    # const char* GetErrorString(DWORD code);
    b("GetErrorString", [t.DWORD], c_char_p)

    # winpr/include/winpr/error.h GetLastError, SetLastError already match
    # the Win32 API names; we don't bind them since users would just call
    # the OS version on Windows anyway.

    # No "truly required" symbol set - WinPR is so feature-rich that any
    # subset may legitimately be missing. We surface errors at use-site.
    return lib


# ---------------------------------------------------------------------------
# Backward-compat alias
# ---------------------------------------------------------------------------

def bind(lib):
    """Alias for bind_client() preserved for backward compatibility."""
    return bind_client(lib)

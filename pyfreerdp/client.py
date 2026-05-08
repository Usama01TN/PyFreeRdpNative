"""
High-level RdpClient: a Pythonic wrapper around a freerdp* instance + rdpContext.

Lifecycle:
    client = RdpClient(settings)         # allocates instance + context
    client.connect()                     # blocks until handshake completes
    client.run_event_loop(timeout=...)   # pump events; can run on a thread
    client.send_key(...) / send_mouse(...)
    client.disconnect()
    client.close()                       # frees native resources
    # - or use as a context manager -
    with RdpClient(settings) as client:
        ...

If settings.channels is non-empty, a ChannelManager is created during
__init__ and each spec is registered against the native rdpSettings
before the connect handshake.

Written in Py2-compatible syntax: no f-strings, no annotations, no
keyword-only `*` separators.
"""

import ctypes
import threading
import time

from .bindings import api as _api
from .bindings import types as t
from .errors import (
    FreeRdpNotFoundError, RdpError, RdpConnectionError, raise_for_connect_code,
)
from .loader import load_freerdp
from .settings import RdpSettings, SecurityProtocol
from .version import FREERDP_MIN_VERSION
from .channels.base import ChannelManager


# Module-global library handle. Loaded lazily on first client construction.
_lib = None
_lib_lock = threading.Lock()


def _ensure_library():
    """Load + bind the FreeRDP library exactly once for the process."""
    global _lib
    if _lib is not None:
        return _lib
    with _lib_lock:
        if _lib is not None:
            return _lib
        handle = load_freerdp()
        if handle is None:
            raise FreeRdpNotFoundError(
                "Could not locate the FreeRDP shared library on this system.\n"
                "Install it with one of:\n"
                "  Debian/Ubuntu:  sudo apt install libfreerdp-client3-3 libfreerdp3-3\n"
                "  Fedora/RHEL:    sudo dnf install freerdp-libs\n"
                "  macOS:          brew install freerdp\n"
                "  Windows:        vcpkg install freerdp:x64-windows\n"
                "Or build from source: python -m pyfreerdp.scripts.build_freerdp\n"
                "Or set PYFREERDP_LIBRARY=/abs/path/to/libfreerdp-client3.so")
        _api.bind_client(handle)
        _api.bind_channels(handle)
        _check_version(handle)
        _lib = handle
        return _lib


def _check_version(lib):
    """Refuse to run against FreeRDP older than FREERDP_MIN_VERSION."""
    try:
        major = ctypes.c_int(0)
        minor = ctypes.c_int(0)
        rev = ctypes.c_int(0)
        lib.freerdp_get_version(ctypes.byref(major),
                                ctypes.byref(minor),
                                ctypes.byref(rev))
        actual = (major.value, minor.value, rev.value)
    except Exception:
        # Older libs may not expose freerdp_get_version - accept and hope.
        return
    if actual < FREERDP_MIN_VERSION:
        raise RdpError(
            "FreeRDP {0}.{1}.{2} is too old; this binding requires >= "
            "{3}".format(actual[0], actual[1], actual[2],
                         ".".join(str(n) for n in FREERDP_MIN_VERSION)))


class RdpClient(object):
    """
    A single RDP session. Not thread-safe - one client per thread, or guard
    with your own lock. The event loop (run_event_loop) is the only method
    safe to call from a worker thread while another thread holds the client.
    """

    # Keyboard flag constants (from input.h).
    KBD_FLAGS_DOWN = 0x4000
    KBD_FLAGS_RELEASE = 0x8000
    KBD_FLAGS_EXTENDED = 0x0100

    # Mouse flag constants (from input.h).
    PTR_FLAGS_HWHEEL = 0x0400
    PTR_FLAGS_WHEEL = 0x0200
    PTR_FLAGS_WHEEL_NEGATIVE = 0x0100
    PTR_FLAGS_MOVE = 0x0800
    PTR_FLAGS_DOWN = 0x8000
    PTR_FLAGS_BUTTON1 = 0x1000  # left
    PTR_FLAGS_BUTTON2 = 0x2000  # right
    PTR_FLAGS_BUTTON3 = 0x4000  # middle

    def __init__(self, settings):
        if not isinstance(settings, RdpSettings):
            raise TypeError("settings must be an RdpSettings instance")
        self._settings_obj = settings
        self._lib = _ensure_library()
        self._instance = None
        self._context = None
        self._connected = False
        self._stop_event = threading.Event()
        self._channel_manager = None

        self._allocate()
        self._apply_settings()
        self._attach_channels()

    # ---------------------------------------------------------------- alloc

    def _allocate(self):
        """Allocate the freerdp instance and its rdpContext."""
        inst = self._lib.freerdp_new()
        if not inst:
            raise RdpError("freerdp_new() returned NULL - out of memory?")
        self._instance = inst

        if not self._lib.freerdp_context_new(inst):
            self._lib.freerdp_free(inst)
            self._instance = None
            raise RdpError("freerdp_context_new() failed")

    def _last_error(self):
        """Return FreeRDP's last connect-error code, or 0 if unavailable."""
        if not self._instance:
            return 0
        try:
            ctx_pp = ctypes.cast(self._instance, ctypes.POINTER(ctypes.c_void_p))
            ctx_addr = ctx_pp[0]
            if not ctx_addr:
                return 0
            ctx = ctypes.cast(ctx_addr, t.rdpContext_p)
            return int(self._lib.freerdp_get_last_error(ctx))
        except Exception:
            return 0

    # ---------------------------------------------------------- settings push

    def _settings_ptr(self):
        """Pull instance->settings out as a typed pointer."""
        addr_table = ctypes.cast(self._instance,
                                 ctypes.POINTER(ctypes.c_void_p))
        settings_addr = addr_table[1]
        if not settings_addr:
            raise RdpError("instance->settings is NULL")
        return ctypes.cast(settings_addr, t.rdpSettings_p)

    def _apply_settings(self):
        """Push every field from the user's RdpSettings into native settings."""
        s = self._settings_obj
        sp = self._settings_ptr()
        SI = t.SettingId

        def set_str(sid, val):
            if val is None:
                return
            ok = self._lib.freerdp_settings_set_string(
                sp, sid, val.encode("utf-8") if val else b"")
            if not ok:
                raise RdpError(
                    "freerdp_settings_set_string({0}) failed".format(sid))

        def set_u32(sid, val):
            ok = self._lib.freerdp_settings_set_uint32(sp, sid, int(val))
            if not ok:
                raise RdpError(
                    "freerdp_settings_set_uint32({0}) failed".format(sid))

        def set_bool(sid, val):
            ok = self._lib.freerdp_settings_set_bool(sp, sid, 1 if val else 0)
            if not ok:
                raise RdpError(
                    "freerdp_settings_set_bool({0}) failed".format(sid))

        # Required identity / target.
        set_str(SI.ServerHostname, s.host)
        set_u32(SI.ServerPort, s.port)
        if s.username:
            set_str(SI.Username, s.username)
        if s.password:
            set_str(SI.Password, s.password)
        if s.domain:
            set_str(SI.Domain, s.domain)

        # Display.
        set_u32(SI.DesktopWidth, s.width)
        set_u32(SI.DesktopHeight, s.height)
        set_u32(SI.ColorDepth, s.color_depth)
        set_bool(SI.Fullscreen, s.fullscreen)
        set_bool(SI.SmartSizing, s.smart_sizing)

        # Security policy.
        set_bool(SI.RdpSecurity, bool(s.security & SecurityProtocol.RDP))
        set_bool(SI.TlsSecurity, bool(s.security & SecurityProtocol.TLS))
        set_bool(SI.NlaSecurity, bool(s.security & SecurityProtocol.NLA))
        set_bool(SI.ExtSecurity, bool(s.security & SecurityProtocol.EXT))
        set_bool(SI.IgnoreCertificate, s.ignore_certificate)
        if s.certificate_name:
            set_str(SI.CertificateName, s.certificate_name)

        # Caching / compression.
        set_bool(SI.BitmapCacheEnabled, s.bitmap_cache)
        set_bool(SI.OffscreenSupportLevel, s.offscreen_cache)
        set_bool(SI.GlyphSupportLevel, s.glyph_cache)
        set_bool(SI.CompressionEnabled, s.compression)
        set_bool(SI.AsyncInput, s.async_input)
        set_bool(SI.AsyncUpdate, s.async_update)
        set_bool(SI.RemoteFxCodec, s.enable_remotefx)
        set_bool(SI.SupportGraphicsPipeline,
                 bool(s.gfx_codec) or s.gfx_h264 or s.gfx_avc444)
        set_bool(SI.GfxH264, s.gfx_h264)
        set_bool(SI.GfxAVC444, s.gfx_avc444)

        # Redirection.
        set_bool(SI.AudioPlayback, s.audio_redirect)
        set_bool(SI.AudioCapture, s.audio_capture)
        set_bool(SI.RedirectClipboard, s.clipboard_redirect)
        set_bool(SI.RedirectPrinters, s.printers)
        set_bool(SI.RedirectSmartCards, s.smartcard)
        set_bool(SI.DeviceRedirection,
                 bool(s.drives or s.printers or s.smartcard))

        # Dynamic channels: set when any attached channel is dynamic. We
        # detect that here so the user doesn't have to flip the flag manually.
        any_dynamic = any(getattr(c, "IS_DYNAMIC", False)
                          for c in s.channels)
        if any_dynamic:
            set_bool(SI.SupportDynamicChannels, True)

        # Gateway.
        if s.gateway_host:
            set_str(SI.GatewayHostname, s.gateway_host)
            set_u32(SI.GatewayPort, s.gateway_port)
            set_u32(SI.GatewayUsageMethod, s.gateway_usage_method or 1)
            if s.gateway_username:
                set_str(SI.GatewayUsername, s.gateway_username)
            if s.gateway_password:
                set_str(SI.GatewayPassword, s.gateway_password)
            if s.gateway_domain:
                set_str(SI.GatewayDomain, s.gateway_domain)

        # Timeouts.
        set_u32(SI.TcpConnectTimeout, s.tcp_connect_timeout_ms)

        # Drives via the legacy boolean: drive redirection now goes through
        # DriveRedirectionChannel in settings.channels. Reject the old
        # `drives` list with a clear migration message.
        if s.drives:
            raise NotImplementedError(
                "settings.drives is deprecated. Use:\n"
                "  from pyfreerdp.channels import DriveRedirection, "
                "DriveRedirectionChannel\n"
                "  channels=[DriveRedirectionChannel(drives=["
                "DriveRedirection(name='share', local_path='/path')])]")

        # Escape hatch.
        for key, val in (s.extra or {}).items():
            sid = getattr(SI, key, None)
            if sid is None:
                raise ValueError("Unknown setting in extra: {0}".format(key))
            if isinstance(val, bool):
                set_bool(sid, val)
            elif isinstance(val, int):
                set_u32(sid, val)
            elif isinstance(val, str):
                set_str(sid, val)
            else:
                raise TypeError(
                    "extra[{0!r}]: unsupported type {1}".format(
                        key, type(val).__name__))

    def _attach_channels(self):
        """Register attached ChannelSpecs against the native settings."""
        if not self._settings_obj.channels:
            return
        cm = ChannelManager(self._lib, self._settings_ptr(), role="client")
        for spec in self._settings_obj.channels:
            cm.attach(spec)
        self._channel_manager = cm

    # --------------------------------------------------------------- connect

    @property
    def channels(self):
        """The ChannelManager for this session, or None."""
        return self._channel_manager

    def connect(self):
        """Perform the RDP handshake. Blocks; raises RdpError on failure."""
        if self._connected:
            return
        ok = self._lib.freerdp_connect(self._instance)
        if not ok:
            code = self._last_error()
            raise_for_connect_code(code)
            # Fall-through if code didn't map to anything: raise generic.
            raise RdpConnectionError("freerdp_connect() failed")
        self._connected = True

    def disconnect(self):
        """Close the RDP session cleanly."""
        if not self._connected or not self._instance:
            return
        try:
            self._lib.freerdp_disconnect(self._instance)
        finally:
            self._connected = False
            self._stop_event.set()

    def close(self):
        """Free all native resources. Idempotent."""
        if self._instance is None:
            return
        try:
            if self._channel_manager:
                self._channel_manager.close()
                self._channel_manager = None
            if self._connected:
                try:
                    self._lib.freerdp_disconnect(self._instance)
                except Exception:
                    pass
                self._connected = False
            self._lib.freerdp_context_free(self._instance)
            self._lib.freerdp_free(self._instance)
        finally:
            self._instance = None

    # --------------------------------------------------------- event pumping

    @property
    def is_connected(self):
        if not self._connected or not self._instance:
            return False
        return not bool(self._lib.freerdp_shall_disconnect(self._instance))

    def run_event_loop(self, timeout=None):
        """Pump the FreeRDP event loop until disconnect / timeout / stop()."""
        if not self.is_connected:
            raise RdpError("Not connected")
        deadline = None if timeout is None else time.monotonic() + timeout
        self._stop_event.clear()
        ctx = ctypes.cast(
            ctypes.cast(self._instance,
                        ctypes.POINTER(ctypes.c_void_p))[0],
            t.rdpContext_p)
        while not self._stop_event.is_set():
            if not self._lib.freerdp_check_event_handles(ctx):
                break
            if self._lib.freerdp_shall_disconnect(self._instance):
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.05)

    def stop(self):
        """Signal run_event_loop to exit on its next iteration."""
        self._stop_event.set()

    # ------------------------------------------------------------ input API

    def _input_ptr(self):
        """Read instance->input (third pointer field in struct freerdp)."""
        slot = ctypes.cast(self._instance,
                           ctypes.POINTER(ctypes.c_void_p))[2]
        if not slot:
            raise RdpError("instance->input is NULL - not connected yet?")
        return ctypes.cast(slot, t.rdpInput_p)

    def send_key(self, scancode, pressed, extended=False):
        """Inject a scancode-based keypress. Use Microsoft RDP scancodes."""
        flags = (self.KBD_FLAGS_DOWN if pressed else self.KBD_FLAGS_RELEASE)
        if extended:
            flags |= self.KBD_FLAGS_EXTENDED
        ok = self._lib.freerdp_input_send_keyboard_event(
            self._input_ptr(), flags, scancode)
        if not ok:
            raise RdpError("freerdp_input_send_keyboard_event failed")

    def send_unicode(self, codepoint, pressed):
        """Inject a Unicode keystroke (no scancode translation)."""
        flags = (self.KBD_FLAGS_DOWN if pressed else self.KBD_FLAGS_RELEASE)
        ok = self._lib.freerdp_input_send_unicode_keyboard_event(
            self._input_ptr(), flags, codepoint)
        if not ok:
            raise RdpError("freerdp_input_send_unicode_keyboard_event failed")

    def send_mouse_move(self, x, y):
        ok = self._lib.freerdp_input_send_mouse_event(
            self._input_ptr(), self.PTR_FLAGS_MOVE, x, y)
        if not ok:
            raise RdpError("freerdp_input_send_mouse_event failed")

    def send_mouse_button(self, button, x, y, pressed):
        """button: 'left' | 'right' | 'middle'."""
        button_map = {
            "left": self.PTR_FLAGS_BUTTON1,
            "right": self.PTR_FLAGS_BUTTON2,
            "middle": self.PTR_FLAGS_BUTTON3,
        }
        if button not in button_map:
            raise ValueError("button must be 'left', 'right', or 'middle'")
        flags = button_map[button] | (self.PTR_FLAGS_DOWN if pressed else 0)
        ok = self._lib.freerdp_input_send_mouse_event(
            self._input_ptr(), flags, x, y)
        if not ok:
            raise RdpError("freerdp_input_send_mouse_event failed")

    # ------------------------------------------------------------- context

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.disconnect()
        finally:
            self.close()

    def __del__(self):
        # Best-effort cleanup. Real code should use the context manager.
        try:
            self.close()
        except Exception:
            pass

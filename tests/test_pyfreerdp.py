"""
Unit tests covering the client + server + channels + WinPR halves of pyfreerdp.

These run without any FreeRDP installation. They cover:
  - settings validation (client + server)
  - error code mapping
  - loader: per-role candidate filenames + env-var precedence
  - public import surface
  - server requires cert when TLS/NLA is configured
  - channel framework: ChannelSpec validation, manager, registration
  - all concrete channel constructors
  - DriveRedirection validation
  - WinPR Stream read/write roundtrips
  - WinPR SSPI + AuthIdentity public surface

Tests requiring the actual library are gated behind @pytest.mark.needs_lib;
they're skipped on default CI runs.

Style: Py2-compatible (no f-strings, no annotations).
"""
import sys

import pytest

import pyfreerdp
from pyfreerdp import (
    FreeRdpNotFoundError,
    RdpError,
    RdpServerSettings,
    RdpSettings,
    SecurityProtocol,
    # Channels
    ChannelSpec,
    ChannelDirection,
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
    # WinPR
    Stream,
    StreamError,
    AuthIdentity,
)
from pyfreerdp.errors import (
    RdpAuthenticationError,
    RdpConnectionError,
    ChannelError,
    raise_for_connect_code,
)
from pyfreerdp.loader import (
    _candidate_names,
    _client_candidates,
    _server_candidates,
    _winpr_candidates,
    find_freerdp_library,
    find_freerdp_server_library,
    find_winpr_library,
)


# ============================================================================
# Public surface
# ============================================================================

def test_public_api_exports_expected_names():
    expected = set([
        # client
        "RdpClient", "RdpSettings", "SecurityProtocol", "GfxCodec",
        # server
        "RdpServer", "RdpServerSettings", "RdpPeer",
        # errors
        "RdpError", "RdpConnectionError", "RdpAuthenticationError",
        "RdpProtocolError", "FreeRdpNotFoundError",
        "ChannelError", "WinPRError",
        # channels
        "ChannelSpec", "ChannelDirection", "ChannelManager",
        "ClipboardChannel", "DriveRedirection", "DriveRedirectionChannel",
        "DisplayControlChannel", "RailChannel", "MultitouchChannel",
        "EncompChannel", "RemdeskChannel", "DynamicChannelManager",
        "CustomChannel",
        "AudioOutChannel", "AudioInChannel", "GraphicsPipelineChannel",
        # winpr
        "Stream", "AuthIdentity",
        # diagnostics
        "find_freerdp_library", "find_freerdp_server_library",
        "find_winpr_library",
        "get_loaded_library_path", "get_loaded_server_library_path",
        "get_loaded_winpr_library_path",
        "__version__",
    ])
    missing = expected - set(pyfreerdp.__all__)
    assert not missing, "public API is missing: {0}".format(missing)


def test_version_is_pep440_ish():
    parts = pyfreerdp.__version__.split(".")
    assert len(parts) >= 2 and all(p[0].isdigit() for p in parts)


# ============================================================================
# Client-side RdpSettings
# ============================================================================

def test_settings_minimal_construction():
    s = RdpSettings(host="10.0.0.1", username="bob", password="pw")
    assert s.host == "10.0.0.1"
    assert s.port == 3389
    assert s.security & SecurityProtocol.NLA


def test_settings_requires_host():
    with pytest.raises(ValueError, match="host"):
        RdpSettings(host="")


@pytest.mark.parametrize("depth", [0, 1, 7, 12, 33, 64])
def test_settings_rejects_bad_color_depth(depth):
    with pytest.raises(ValueError, match="color_depth"):
        RdpSettings(host="x", color_depth=depth)


@pytest.mark.parametrize("port", [0, -1, 65536, 100000])
def test_settings_rejects_bad_port(port):
    with pytest.raises(ValueError, match="port"):
        RdpSettings(host="x", port=port)


@pytest.mark.parametrize("w,h", [(0, 0), (199, 768), (1024, 100), (-1, 768)])
def test_settings_rejects_tiny_dimensions(w, h):
    with pytest.raises(ValueError):
        RdpSettings(host="x", width=w, height=h)


def test_settings_merge_returns_modified_copy():
    base = RdpSettings(host="a", username="u")
    other = base.merge(host="b", port=3390)
    assert base.host == "a" and base.port == 3389
    assert other.host == "b" and other.port == 3390
    assert other.username == "u"


def test_settings_extra_accepts_known_field():
    s = RdpSettings(host="x", extra={"AsyncInput": True})
    assert s.extra == {"AsyncInput": True}


def test_settings_channels_default_empty():
    s = RdpSettings(host="x")
    assert s.channels == []


def test_settings_accepts_channel_list():
    cb = ClipboardChannel()
    s = RdpSettings(host="x", channels=[cb])
    assert s.channels == [cb]


def test_settings_repr_includes_host_and_port():
    s = RdpSettings(host="rdp.example.com", username="alice")
    r = repr(s)
    assert "rdp.example.com" in r and "3389" in r


# ============================================================================
# Server-side RdpServerSettings
# ============================================================================

def test_server_settings_minimal_with_tls(tmp_path):
    cert = tmp_path / "c.crt"
    key = tmp_path / "k.key"
    cert.write_text("fake cert")
    key.write_text("fake key")
    s = RdpServerSettings(
        certificate_file=str(cert),
        private_key_file=str(key),
    )
    assert s.bind_address == "0.0.0.0"
    assert s.port == 3389
    assert s.security & SecurityProtocol.TLS


def test_server_settings_rejects_tls_without_cert():
    """TLS or NLA without a certificate is the most common misconfig."""
    with pytest.raises(ValueError, match="certificate"):
        RdpServerSettings()   # default security = TLS|NLA


def test_server_settings_allows_rdp_only_security_without_cert():
    """Legacy RDP-security has no certificate requirement."""
    s = RdpServerSettings(security=SecurityProtocol.RDP)
    assert s.security == SecurityProtocol.RDP


def test_server_settings_rejects_tls_only_without_cert():
    with pytest.raises(ValueError, match="certificate"):
        RdpServerSettings(security=SecurityProtocol.TLS)


def test_server_settings_rejects_nla_only_without_cert():
    with pytest.raises(ValueError, match="certificate"):
        RdpServerSettings(security=SecurityProtocol.NLA)


@pytest.mark.parametrize("port", [0, -1, 65536, 100000])
def test_server_settings_rejects_bad_port(port):
    with pytest.raises(ValueError, match="port"):
        RdpServerSettings(security=SecurityProtocol.RDP, port=port)


def test_server_settings_rejects_bad_color_depth():
    with pytest.raises(ValueError, match="color_depth"):
        RdpServerSettings(security=SecurityProtocol.RDP, color_depth=7)


def test_server_settings_rejects_tiny_dimensions():
    with pytest.raises(ValueError):
        RdpServerSettings(
            security=SecurityProtocol.RDP, width=100, height=100
        )


def test_server_settings_accepts_channels():
    s = RdpServerSettings(security=SecurityProtocol.RDP,
                          channels=[ClipboardChannel()])
    assert len(s.channels) == 1


# ============================================================================
# Error mapping
# ============================================================================

def test_connect_code_zero_is_noop():
    raise_for_connect_code(0)


def test_connect_code_logon_failure_maps_to_auth():
    with pytest.raises(RdpAuthenticationError):
        raise_for_connect_code(0x00020005)


def test_connect_code_transport_failure_maps_to_connection():
    with pytest.raises(RdpConnectionError):
        raise_for_connect_code(0x0002000A)


def test_unknown_connect_code_falls_back_to_connection_error():
    with pytest.raises(RdpConnectionError):
        raise_for_connect_code(0xDEADBEEF)


def test_all_concrete_errors_subclass_rdperror():
    for cls in (RdpAuthenticationError, RdpConnectionError, ChannelError):
        assert issubclass(cls, RdpError)


def test_connect_code_message_includes_hex():
    try:
        raise_for_connect_code(0x12345678)
    except RdpConnectionError as exc:
        assert "0x12345678" in str(exc)
    else:
        pytest.fail("expected RdpConnectionError")


# ============================================================================
# Loader - client + server + winpr roles
# ============================================================================

def test_client_candidates_nonempty_for_current_platform():
    names = _client_candidates()
    assert names and all(isinstance(n, str) for n in names)


def test_server_candidates_nonempty_for_current_platform():
    names = _server_candidates()
    assert names and all(isinstance(n, str) for n in names)


def test_winpr_candidates_nonempty_for_current_platform():
    names = _winpr_candidates()
    assert names and all(isinstance(n, str) for n in names)


def test_client_candidates_match_platform_extension():
    names = _client_candidates()
    if sys.platform.startswith("linux"):
        assert any(n.endswith(".so") or ".so." in n for n in names)
    elif sys.platform == "darwin":
        assert any(n.endswith(".dylib") for n in names)
    elif sys.platform == "win32":
        assert any(n.endswith(".dll") for n in names)


def test_server_candidates_match_platform_extension():
    names = _server_candidates()
    if sys.platform.startswith("linux"):
        assert any(n.endswith(".so") or ".so." in n for n in names)
    elif sys.platform == "darwin":
        assert any(n.endswith(".dylib") for n in names)
    elif sys.platform == "win32":
        assert any(n.endswith(".dll") for n in names)


def test_winpr_candidates_match_platform_extension():
    names = _winpr_candidates()
    if sys.platform.startswith("linux"):
        assert any(n.endswith(".so") or ".so." in n for n in names)
    elif sys.platform == "darwin":
        assert any(n.endswith(".dylib") for n in names)
    elif sys.platform == "win32":
        assert any(n.endswith(".dll") for n in names)


def test_server_candidates_disjoint_from_client_candidates():
    server_names = _server_candidates()
    assert all("server" in n for n in server_names)


def test_winpr_candidates_disjoint_from_client_candidates():
    winpr_names = _winpr_candidates()
    assert all("winpr" in n for n in winpr_names)


def test_client_candidates_do_not_include_server_or_winpr_libs():
    client_names = _client_candidates()
    assert not any("server" in n for n in client_names)
    assert not any("winpr" in n for n in client_names)


def test_legacy_candidate_names_alias_to_client():
    assert _candidate_names() == _client_candidates()


# ----- env-var precedence --------------------------------------------------

def test_role_specific_env_overrides_client(tmp_path, monkeypatch):
    f = tmp_path / "libfreerdp-client3.so"
    f.write_bytes(b"\x7fELF")
    monkeypatch.setenv("PYFREERDP_CLIENT_LIBRARY", str(f))
    monkeypatch.delenv("PYFREERDP_LIBRARY", raising=False)
    assert find_freerdp_library() == str(f)


def test_role_specific_env_overrides_server(tmp_path, monkeypatch):
    f = tmp_path / "libfreerdp-server3.so"
    f.write_bytes(b"\x7fELF")
    monkeypatch.setenv("PYFREERDP_SERVER_LIBRARY", str(f))
    monkeypatch.delenv("PYFREERDP_LIBRARY", raising=False)
    assert find_freerdp_server_library() == str(f)


def test_role_specific_env_overrides_winpr(tmp_path, monkeypatch):
    f = tmp_path / "libwinpr3.so"
    f.write_bytes(b"\x7fELF")
    monkeypatch.setenv("PYFREERDP_WINPR_LIBRARY", str(f))
    monkeypatch.delenv("PYFREERDP_LIBRARY", raising=False)
    assert find_winpr_library() == str(f)


def test_legacy_env_only_applies_to_client(tmp_path, monkeypatch):
    """PYFREERDP_LIBRARY (legacy) must NOT redirect server or winpr lookups."""
    f = tmp_path / "libfreerdp-client3.so"
    f.write_bytes(b"\x7fELF")
    monkeypatch.setenv("PYFREERDP_LIBRARY", str(f))
    monkeypatch.delenv("PYFREERDP_CLIENT_LIBRARY", raising=False)
    monkeypatch.delenv("PYFREERDP_SERVER_LIBRARY", raising=False)
    monkeypatch.delenv("PYFREERDP_WINPR_LIBRARY", raising=False)
    # Client lookup honours the legacy var.
    assert find_freerdp_library() == str(f)
    # Server lookup must NOT - it should fall through to system search.
    assert find_freerdp_server_library() != str(f)
    # WinPR lookup must NOT either.
    assert find_winpr_library() != str(f)


def test_role_specific_env_returns_path_even_if_missing(tmp_path, monkeypatch):
    fake = tmp_path / "missing-server.so"
    monkeypatch.setenv("PYFREERDP_SERVER_LIBRARY", str(fake))
    # Even though the file doesn't exist, the override is returned so
    # downstream loading produces a clear error instead of silently falling
    # back to a different library.
    assert find_freerdp_server_library() == str(fake)


# ============================================================================
# Server import surface
# ============================================================================

def test_rdp_server_class_imports():
    from pyfreerdp import RdpServer
    assert callable(RdpServer)


def test_server_construction_without_lib_raises_friendly_error():
    """Should fail with FreeRdpNotFoundError, not a stray AttributeError."""
    from pyfreerdp import RdpServer
    cert_settings = RdpServerSettings(
        security=SecurityProtocol.RDP,   # no cert needed
    )
    if find_freerdp_server_library():
        pytest.skip(
            "FreeRDP server library is installed; can't test missing-lib path")

    def handler(peer):
        pass

    with pytest.raises(FreeRdpNotFoundError):
        RdpServer(cert_settings, handler)


# ============================================================================
# Channel framework
# ============================================================================

def test_channel_spec_requires_NAME_attribute():
    class BadSpec(ChannelSpec):
        NAME = None
    with pytest.raises(ChannelError):
        BadSpec()


def test_channel_spec_requires_bytes_NAME():
    class BadSpec(ChannelSpec):
        NAME = "cliprdr"   # wrong - must be bytes
    with pytest.raises(ChannelError):
        BadSpec()


def test_channel_spec_rejects_long_static_name():
    class BadSpec(ChannelSpec):
        NAME = b"this_is_too_long_for_static"
    with pytest.raises(ChannelError):
        BadSpec()


def test_channel_direction_constants():
    assert ChannelDirection.CLIENT_TO_SERVER == 0x01
    assert ChannelDirection.SERVER_TO_CLIENT == 0x02
    assert ChannelDirection.BOTH == 0x03


# ----- per-channel constructors --------------------------------------------

def test_clipboard_channel_default_construction():
    c = ClipboardChannel()
    assert c.NAME == b"cliprdr"
    assert c.IS_DYNAMIC is False
    assert c.enable_text is True
    assert c.params() == []


def test_clipboard_format_constants():
    assert ClipboardFormat.CF_TEXT == 1
    assert ClipboardFormat.CF_UNICODETEXT == 13
    assert ClipboardFormat.CF_HDROP == 15


def test_drive_redirection_validates_name_length():
    with pytest.raises(ValueError, match="8 chars"):
        DriveRedirection(name="reallylongname", local_path="/tmp")


def test_drive_redirection_requires_absolute_path():
    with pytest.raises(ValueError, match="absolute"):
        DriveRedirection(name="x", local_path="rel/path")


def test_drive_redirection_accepts_valid_input():
    d = DriveRedirection(name="share", local_path="/tmp/share", read_only=True)
    assert d.name == "share"
    assert d.read_only is True


def test_drive_redirection_channel_requires_drives():
    with pytest.raises(ValueError, match="DriveRedirection"):
        DriveRedirectionChannel()


def test_drive_redirection_channel_emits_correct_params():
    d = DriveRedirection(name="share", local_path="/tmp/share")
    chan = DriveRedirectionChannel(drives=[d])
    params = chan.params()
    assert any("drive,share,/tmp/share,rw" in p for p in params)


def test_drive_redirection_channel_emits_ro_when_read_only():
    d = DriveRedirection(name="share", local_path="/tmp/share", read_only=True)
    chan = DriveRedirectionChannel(drives=[d])
    assert any(p.endswith(",ro") for p in chan.params())


def test_disp_channel_construction():
    c = DisplayControlChannel()
    assert c.NAME == b"disp"
    assert c.IS_DYNAMIC is True


def test_disp_channel_send_resize_dedups_identical_layouts():
    c = DisplayControlChannel()
    c._opened = True   # bypass channel-not-open check; not exercising send wire
    assert c.send_resize(1920, 1080) is True
    assert c.send_resize(1920, 1080) is False  # dedup


def test_disp_channel_tracks_latest_layout():
    c = DisplayControlChannel()
    c._opened = True
    c.send_resize(1920, 1080)
    layout = c.latest_layout()
    assert layout[0] == 1920 and layout[1] == 1080


def test_rail_channel_construction():
    c = RailChannel(exec_app="notepad.exe")
    assert c.NAME == b"rail"
    assert c.IS_DYNAMIC is False
    assert any("exec:notepad.exe" in p for p in c.params())


def test_rail_channel_no_exec_means_no_exec_param():
    c = RailChannel()
    assert not any(p.startswith("exec:") for p in c.params())


def test_multitouch_channel_construction():
    c = MultitouchChannel()
    assert c.NAME == b"rdpei"
    assert c.IS_DYNAMIC is True
    assert c.DIRECTION == ChannelDirection.CLIENT_TO_SERVER


def test_multitouch_send_contact_returns_frame():
    c = MultitouchChannel()
    frame = c.send_contact(
        contact_id=0, x=100, y=200,
        state=MultitouchChannel.STATE_INRANGE | MultitouchChannel.STATE_INCONTACT)
    assert frame["x"] == 100 and frame["y"] == 200


def test_encomsp_channel_construction():
    c = EncompChannel()
    assert c.NAME == b"encomsp"


def test_remdesk_channel_construction():
    c = RemdeskChannel()
    assert c.NAME == b"remdesk"


def test_dynamic_channel_manager_register_unregister():
    parent = object()  # we don't need a real ChannelManager for this layer
    mgr = DynamicChannelManager(parent)

    seen = []
    def handler(buf, flags):
        seen.append((buf, flags))

    mgr.register("MYDVC", handler)
    mgr._dispatch_open(b"MYDVC")
    assert mgr.is_open("MYDVC") is True
    mgr._dispatch_data(b"MYDVC", b"hello", 0)
    assert seen == [(b"hello", 0)]
    mgr.unregister("MYDVC")
    assert mgr.is_open("MYDVC") is False


def test_custom_channel_static_name_too_long():
    with pytest.raises(ValueError, match="<= 8"):
        CustomChannel(name=b"too_long_name", dynamic=False)


def test_custom_channel_dynamic_allows_long_names():
    c = CustomChannel(name=b"VeryLongDynamicName", dynamic=True)
    assert c.NAME == b"VeryLongDynamicName"
    assert c.IS_DYNAMIC is True


def test_custom_channel_str_name_encoded():
    c = CustomChannel(name="MYCHAN")
    assert c.NAME == b"MYCHAN"


def test_custom_channel_send_queues():
    c = CustomChannel(name=b"MYCHAN")
    c._opened = True
    c.send(b"hello")
    c.send("world")
    drained = c._drain_send_buf()
    assert drained == [b"hello", b"world"]


def test_custom_channel_callback_invoked():
    received = []
    def cb(buf, flags):
        received.append((buf, flags))
    c = CustomChannel(name=b"MYCHAN", on_data=cb)
    c.on_data(b"data", 0x42)
    assert received == [(b"data", 0x42)]


# ----- stub channels -------------------------------------------------------

def test_audio_out_channel_construction():
    c = AudioOutChannel()
    assert c.NAME == b"rdpsnd"
    assert c.DIRECTION == ChannelDirection.SERVER_TO_CLIENT
    # Default formats
    assert any("s16le-44100-2" in p for p in c.params())


def test_audio_in_channel_construction():
    c = AudioInChannel(sample_rate=48000, channels=1)
    assert c.NAME == b"audin"
    assert c.IS_DYNAMIC is True
    params = c.params()
    assert any("rate:48000" in p for p in params)
    assert any("channel:1" in p for p in params)


def test_audio_in_channel_silent_default():
    c = AudioInChannel()
    assert c.next_pcm(1024) is None  # silence


def test_graphics_pipeline_channel_construction():
    c = GraphicsPipelineChannel()
    assert c.NAME == b"rdpgfx"
    assert c.IS_DYNAMIC is True


def test_graphics_pipeline_channel_avc444_param():
    c = GraphicsPipelineChannel(prefer_avc444=True)
    assert any("avc444:1" in p for p in c.params())


# ============================================================================
# WinPR Stream
# ============================================================================

def test_stream_empty_construction():
    s = Stream()
    assert s.position() == 0
    assert s.remaining() == 0
    assert len(s) == 0


def test_stream_from_bytes_starts_at_zero():
    s = Stream.from_bytes(b"\x01\x02\x03\x04")
    assert s.position() == 0
    assert len(s) == 4


def test_stream_write_read_u8():
    s = Stream()
    s.write_u8(0xAB)
    s.set_position(0)
    assert s.read_u8() == 0xAB


def test_stream_write_u16_le_byte_order():
    s = Stream()
    s.write_u16_le(0x1234)
    assert s.bytes() == b"\x34\x12"


def test_stream_write_u16_be_byte_order():
    s = Stream()
    s.write_u16_be(0x1234)
    assert s.bytes() == b"\x12\x34"


def test_stream_write_u32_le_byte_order():
    s = Stream()
    s.write_u32_le(0xDEADBEEF)
    assert s.bytes() == b"\xEF\xBE\xAD\xDE"


def test_stream_roundtrip_mixed_types():
    s = Stream()
    s.write_u8(1)
    s.write_u16_le(2)
    s.write_u32_le(3)
    s.write_u64_le(4)
    s.set_position(0)
    assert s.read_u8() == 1
    assert s.read_u16_le() == 2
    assert s.read_u32_le() == 3
    assert s.read_u64_le() == 4


def test_stream_zero_terminated_utf16_roundtrip():
    s = Stream()
    s.write_zero_terminated_utf16("hello")
    s.set_position(0)
    assert s.read_zero_terminated_utf16() == "hello"


def test_stream_zero_terminated_utf16_unicode_roundtrip():
    s = Stream()
    s.write_zero_terminated_utf16(u"caf\u00e9 \u3053\u3093")
    s.set_position(0)
    assert s.read_zero_terminated_utf16() == u"caf\u00e9 \u3053\u3093"


def test_stream_read_past_end_raises():
    s = Stream.from_bytes(b"\x01\x02")
    s.read_u8()
    s.read_u8()
    with pytest.raises(StreamError):
        s.read_u8()


def test_stream_set_position_out_of_range():
    s = Stream.from_bytes(b"\x01\x02")
    with pytest.raises(StreamError):
        s.set_position(99)


def test_stream_remaining_after_partial_read():
    s = Stream.from_bytes(b"\x01\x02\x03\x04")
    s.read_u8()
    assert s.remaining() == 3


def test_stream_write_bytes_accepts_str_and_bytes():
    s = Stream()
    s.write_bytes("hello")
    s.write_bytes(b"world")
    assert s.bytes() == b"helloworld"


def test_stream_repr():
    s = Stream(capacity=10)
    s.write_u8(1)
    r = repr(s)
    assert "Stream" in r and "len=" in r and "pos=" in r


# ============================================================================
# WinPR SSPI public surface
# ============================================================================

def test_auth_identity_repr_does_not_leak_password():
    a = AuthIdentity(username="alice", domain="CORP", password="secret123")
    r = repr(a)
    assert "alice" in r
    assert "secret123" not in r


def test_auth_identity_fields():
    a = AuthIdentity(username="bob", domain="", password="pw", flags=0x2)
    assert a.username == "bob"
    assert a.password == "pw"
    assert a.flags == 0x2


def test_parse_logon_identity_handles_null():
    from pyfreerdp.winpr.sspi import parse_logon_identity
    assert parse_logon_identity(None) is None


# ============================================================================
# Display events
# ============================================================================

def test_bitmap_rect_construction():
    from pyfreerdp import BitmapRect
    r = BitmapRect(x=10, y=20, width=100, height=50, bpp=32,
                   data=b"\x00" * (100 * 50 * 4),
                   stride=400, compressed=False)
    assert r.x == 10 and r.y == 20
    assert r.width == 100 and r.height == 50
    assert r.bpp == 32
    assert len(r.data) == 100 * 50 * 4
    assert r.compressed is False


def test_bitmap_update_iter_and_len():
    from pyfreerdp import BitmapRect, BitmapUpdate
    rects = [
        BitmapRect(x=0, y=0, width=10, height=10, bpp=32,
                   data=b"", stride=40, compressed=False),
        BitmapRect(x=10, y=10, width=20, height=20, bpp=32,
                   data=b"", stride=80, compressed=True),
    ]
    upd = BitmapUpdate(rects)
    assert len(upd) == 2
    assert list(upd) == rects


def test_palette_update_requires_256_entries():
    from pyfreerdp import PaletteUpdate
    with pytest.raises(ValueError, match="256"):
        PaletteUpdate([(0, 0, 0)] * 100)


def test_palette_update_indexable():
    from pyfreerdp import PaletteUpdate
    pal = PaletteUpdate([(i, i, i) for i in range(256)])
    assert pal[0] == (0, 0, 0)
    assert pal[255] == (255, 255, 255)


def test_surface_bits_codec_name_lookup():
    from pyfreerdp import SurfaceBits
    sb = SurfaceBits(x=0, y=0, width=1920, height=1080,
                     bpp=32, pixel_format=0x20, codec_id=0x0C,
                     payload=b"FAKE_H264_DATA")
    assert sb.codec_name == "h264"
    assert sb.codec_id == 0x0C
    assert sb.payload == b"FAKE_H264_DATA"


def test_surface_bits_unknown_codec_falls_back_gracefully():
    from pyfreerdp import SurfaceBits
    sb = SurfaceBits(x=0, y=0, width=1, height=1,
                     bpp=32, pixel_format=0, codec_id=0xFF,
                     payload=b"")
    assert "unknown" in sb.codec_name


def test_pointer_update_kinds():
    from pyfreerdp import PointerUpdate
    sys_p = PointerUpdate(kind="system", system_id=32649)
    assert sys_p.kind == "system" and sys_p.system_id == 32649
    hidden = PointerUpdate(kind="hidden")
    assert hidden.kind == "hidden"
    sprite = PointerUpdate(kind="sprite", width=32, height=32,
                           hot_x=16, hot_y=16, rgba=b"\x00" * (32 * 32 * 4))
    assert sprite.width == 32 and sprite.hot_x == 16


def test_pointer_update_rejects_bad_kind():
    from pyfreerdp import PointerUpdate
    with pytest.raises(ValueError, match="kind"):
        PointerUpdate(kind="invalid")


def test_pixel_format_constants():
    from pyfreerdp import PixelFormat
    assert PixelFormat.BGRX32 == 0x20
    assert PixelFormat.BGRA32 == 0x28


def test_rdp_client_exposes_display_callback_hooks():
    """The four on_* callback attributes must exist on RdpClient and
    default to None so users can introspect."""
    from pyfreerdp.client import RdpClient
    # Don't construct - constructing requires the library. Inspect the
    # __init__ source via a synthetic instance probe.
    # Easier: walk the class for the documented hook names.
    hook_names = {"on_bitmap_update", "on_palette_update",
                  "on_surface_bits", "on_pointer_update"}
    # We can't introspect __init__ without running it, so check via
    # the source file content - cheap and reliable.
    import inspect
    src = inspect.getsource(RdpClient.__init__)
    for name in hook_names:
        assert name in src, "RdpClient.__init__ should set {0}".format(name)


# ============================================================================
# Integration smoke
# ============================================================================

@pytest.mark.needs_lib
def test_can_locate_client_library_when_installed():
    path = find_freerdp_library()
    if path is None:
        pytest.skip("FreeRDP client not installed on this host")
    # On Linux, ctypes.util.find_library returns the SONAME (e.g.
    # 'libfreerdp-client3.so.3'), not an absolute path - the dynamic
    # linker resolves it via ld.so.cache. On macOS/Windows the result
    # is typically absolute. Either way is fine; the real check is that
    # the result is loadable.
    assert isinstance(path, str) and path
    assert "freerdp" in path.lower()
    # Confirm it actually loads.
    import ctypes
    ctypes.CDLL(path)


@pytest.mark.needs_lib
def test_can_locate_server_library_when_installed():
    path = find_freerdp_server_library()
    if path is None:
        pytest.skip("FreeRDP server not installed on this host")
    assert isinstance(path, str) and path
    assert "freerdp" in path.lower() and "server" in path.lower()
    import ctypes
    ctypes.CDLL(path)


@pytest.mark.needs_lib
def test_can_locate_winpr_library_when_installed():
    path = find_winpr_library()
    if path is None:
        pytest.skip("WinPR not installed on this host")
    assert isinstance(path, str) and path
    assert "winpr" in path.lower()
    import ctypes
    ctypes.CDLL(path)

"""
RdpSettings - Pythonic facade over FreeRDP's rdpSettings struct (client side).

FreeRDP's native settings struct has hundreds of fields. This wrapper exposes
the commonly-needed subset; the rest can be reached via .extra (forwarded as
freerdp_settings_set_string / _set_uint32 / _set_bool calls).

Written in Py2-compatible syntax: no dataclass, no annotations, no f-strings.
"""


class SecurityProtocol(object):
    """
    RDP security negotiation flags. Combine with bitwise OR.

    Not an IntEnum (IntEnum existed in py3.4+ but the bitwise-OR semantics
    we want fall back to plain ints anyway). Class-attribute constants give
    us `SecurityProtocol.NLA` access without paying for enum machinery.
    """
    RDP = 0x01    # Legacy "Standard RDP Security"
    TLS = 0x02    # TLS 1.x
    NLA = 0x04    # Network Level Authentication (CredSSP)
    EXT = 0x08    # NLA-Extended


class GfxCodec(object):
    """Graphics codec preference for the RemoteFX / GFX channel."""
    AUTO = 0
    RFX = 1            # RemoteFX
    NSC = 2            # NSCodec
    H264 = 3           # AVC/H.264
    PROGRESSIVE = 4
    H264_AVC444 = 5


_VALID_COLOR_DEPTHS = (8, 15, 16, 24, 32)


class RdpSettings(object):
    """
    User-facing client settings.

    Required:
        host: Hostname or IP of the RDP server.

    Optional (sane defaults provided):
        username, password, domain, port, width, height, color_depth,
        fullscreen, security, gfx_codec, audio_redirect, clipboard_redirect,
        drives, certificate_name, ignore_certificate, gateway_*, performance
        flags, etc.

    The escape hatch `extra` is a dict of FreeRDP setting names (without the
    FreeRDP_ prefix) -> value. It's applied after the named fields so it can
    override anything.
    """

    def __init__(self,
                 host,
                 username="", password="", domain="", port=3389,
                 width=1024, height=768, color_depth=32,
                 fullscreen=False, smart_sizing=False,
                 security=None, ignore_certificate=False, certificate_name="",
                 gfx_codec=GfxCodec.AUTO, gfx_h264=False, gfx_avc444=False,
                 enable_remotefx=True,
                 bitmap_cache=True, offscreen_cache=True, glyph_cache=True,
                 compression=True, async_input=True, async_update=True,
                 audio_redirect=False, audio_capture=False,
                 clipboard_redirect=True,
                 drives=None, printers=False, smartcard=False,
                 usb_redirect=False,
                 gateway_host="", gateway_port=443,
                 gateway_username="", gateway_password="", gateway_domain="",
                 gateway_usage_method=0,
                 tcp_connect_timeout_ms=15000, tcp_keepalive=True,
                 channels=None,
                 extra=None):
        # Default for `security` can't be a SecurityProtocol expression in
        # the signature (py2 evaluates default args eagerly, fine; but using
        # `|` between IntEnums on py2 returns an int — keeping behavior
        # identical we resolve here):
        if security is None:
            security = SecurityProtocol.TLS | SecurityProtocol.NLA

        self.host = host
        self.username = username
        self.password = password
        self.domain = domain
        self.port = port

        self.width = width
        self.height = height
        self.color_depth = color_depth
        self.fullscreen = fullscreen
        self.smart_sizing = smart_sizing

        self.security = security
        self.ignore_certificate = ignore_certificate
        self.certificate_name = certificate_name

        self.gfx_codec = gfx_codec
        self.gfx_h264 = gfx_h264
        self.gfx_avc444 = gfx_avc444
        self.enable_remotefx = enable_remotefx
        self.bitmap_cache = bitmap_cache
        self.offscreen_cache = offscreen_cache
        self.glyph_cache = glyph_cache
        self.compression = compression
        self.async_input = async_input
        self.async_update = async_update

        self.audio_redirect = audio_redirect
        self.audio_capture = audio_capture
        self.clipboard_redirect = clipboard_redirect
        self.drives = list(drives) if drives else []
        self.printers = printers
        self.smartcard = smartcard
        self.usb_redirect = usb_redirect

        self.gateway_host = gateway_host
        self.gateway_port = gateway_port
        self.gateway_username = gateway_username
        self.gateway_password = gateway_password
        self.gateway_domain = gateway_domain
        self.gateway_usage_method = gateway_usage_method

        self.tcp_connect_timeout_ms = tcp_connect_timeout_ms
        self.tcp_keepalive = tcp_keepalive

        # `channels`: list of channel-spec objects to attach during connect.
        # See pyfreerdp.channels for the spec types. None == no static channels
        # beyond what FreeRDP wires up by default.
        self.channels = list(channels) if channels else []

        self.extra = dict(extra) if extra else {}

        self._validate()

    def _validate(self):
        if not self.host:
            raise ValueError("RdpSettings.host is required")
        if self.color_depth not in _VALID_COLOR_DEPTHS:
            raise ValueError(
                "color_depth must be one of 8/15/16/24/32, got {0}".format(
                    self.color_depth))
        if not (1 <= self.port <= 65535):
            raise ValueError("port out of range: {0}".format(self.port))
        if self.width < 200 or self.height < 200:
            raise ValueError("width/height must each be >= 200")

    def merge(self, **overrides):
        """Return a copy with the given fields overridden. Handy for templating."""
        # Walk our own attributes so we don't have to re-list them.
        kwargs = {}
        for k in (
            "host", "username", "password", "domain", "port",
            "width", "height", "color_depth", "fullscreen", "smart_sizing",
            "security", "ignore_certificate", "certificate_name",
            "gfx_codec", "gfx_h264", "gfx_avc444", "enable_remotefx",
            "bitmap_cache", "offscreen_cache", "glyph_cache",
            "compression", "async_input", "async_update",
            "audio_redirect", "audio_capture", "clipboard_redirect",
            "drives", "printers", "smartcard", "usb_redirect",
            "gateway_host", "gateway_port",
            "gateway_username", "gateway_password", "gateway_domain",
            "gateway_usage_method",
            "tcp_connect_timeout_ms", "tcp_keepalive",
            "channels", "extra",
        ):
            kwargs[k] = getattr(self, k)
        kwargs.update(overrides)
        return RdpSettings(**kwargs)

    def __repr__(self):
        return "RdpSettings(host={0!r}, port={1}, user={2!r})".format(
            self.host, self.port, self.username)

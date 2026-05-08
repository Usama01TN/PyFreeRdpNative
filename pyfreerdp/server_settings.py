"""
RdpServerSettings - Pythonic facade over the server-side options.

The server's rdpSettings struct overlaps heavily with the client's, but
the meaningful subset is different: the server cares about its certificate,
its bind address, its supported security protocols, and the desktop size
it advertises.

Written in Py2-compatible syntax: no dataclass, no annotations, no f-strings.
"""

from .settings import SecurityProtocol


_VALID_COLOR_DEPTHS = (8, 15, 16, 24, 32)


class RdpServerSettings(object):
    """
    Server-side configuration.

    Required when running over TLS or NLA (which is everyone you actually
    want to talk to):
        certificate_file: PEM-formatted X.509 cert chain.
        private_key_file: matching PEM key.

    The `username` / `password` fields are advisory: they describe the
    credential the server expects. Authentication itself happens in your
    Logon callback, where you decide whether to accept a peer's claim.
    Set them only if you want libfreerdp's built-in Auto-Logon handling.
    """

    def __init__(self,
                 bind_address="0.0.0.0", port=3389,
                 certificate_file=None, private_key_file=None,
                 rdp_key_file=None,
                 width=1024, height=768, color_depth=32,
                 security=None,
                 username="", password="", domain="",
                 accept_timeout_ms=30000,
                 channels=None,
                 extra=None):
        if security is None:
            security = SecurityProtocol.TLS | SecurityProtocol.NLA

        self.bind_address = bind_address
        self.port = port

        self.certificate_file = certificate_file
        self.private_key_file = private_key_file
        self.rdp_key_file = rdp_key_file

        self.width = width
        self.height = height
        self.color_depth = color_depth

        self.security = security

        self.username = username
        self.password = password
        self.domain = domain

        self.accept_timeout_ms = accept_timeout_ms

        # Static channels to advertise in the server-side capability set.
        # Same shape as RdpSettings.channels.
        self.channels = list(channels) if channels else []

        self.extra = dict(extra) if extra else {}

        self._validate()

    def _validate(self):
        if not (1 <= self.port <= 65535):
            raise ValueError("port out of range: {0}".format(self.port))
        if self.color_depth not in _VALID_COLOR_DEPTHS:
            raise ValueError(
                "color_depth must be one of 8/15/16/24/32, got {0}".format(
                    self.color_depth))
        if self.width < 200 or self.height < 200:
            raise ValueError("width/height must each be >= 200")

        # TLS without a cert is the most common misconfiguration. We don't
        # treat it as fatal here (RDP-only is legal) - but if TLS or NLA is
        # set, we require a cert.
        if (self.security & (SecurityProtocol.TLS | SecurityProtocol.NLA)):
            if not self.certificate_file or not self.private_key_file:
                raise ValueError(
                    "TLS/NLA security requires certificate_file and "
                    "private_key_file. Either provide them, or restrict "
                    "security to SecurityProtocol.RDP (insecure; legacy).")

    def __repr__(self):
        return "RdpServerSettings(bind={0!r}, port={1})".format(
            self.bind_address, self.port)

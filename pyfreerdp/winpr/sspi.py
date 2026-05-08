"""
WinPR SSPI helpers - server-side NLA support.

Why this exists
---------------
A real RDP server using NLA (Network Level Authentication) needs a Logon
callback that decides whether to accept a peer's claimed credentials.
The credentials arrive as a SEC_WINNT_AUTH_IDENTITY (or one of its
extended variants) - a struct containing username, domain, and password.

The wire format went through CredSSP and is decrypted by FreeRDP before
your callback runs, so by the time you see the identity it's plaintext.
But it's still in WinPR's internal layout, which uses platform-dependent
character sizes (UTF-16LE on all platforms; the field types claim "WCHAR
or CHAR" depending on flags).

This module gives you AuthIdentity - a Pythonic dataclass-style wrapper -
and parse_logon_identity() which converts the raw c_void_p into an
AuthIdentity. Plug those into your Logon callback and you can write
authentication logic in pure Python.

What's NOT here
---------------
We don't expose AcquireCredentialsHandle / InitializeSecurityContext etc.
- the full SSPI surface for actually doing the CredSSP dance. FreeRDP
performs CredSSP itself; you just see the result. If you're trying to
rewrite CredSSP from Python, this isn't your library.
"""

import ctypes

from ..errors import WinPRError


class SecurityPackageInfo(object):
    """Lightweight record describing an installed security package."""

    def __init__(self, name, comment, capabilities, version, max_token):
        self.name = name
        self.comment = comment
        self.capabilities = capabilities
        self.version = version
        self.max_token = max_token

    def __repr__(self):
        return "SecurityPackageInfo(name={0!r}, max_token={1})".format(
            self.name, self.max_token)


def query_security_package(name):
    """
    Look up an installed SSPI package by name. Returns SecurityPackageInfo
    or raises WinPRError if the package isn't available.

    Common package names: 'NTLM', 'Negotiate', 'Kerberos'. On non-Windows
    platforms WinPR provides its own NTLM and Negotiate implementations
    so these are usually present.
    """
    from ._loader import get_winpr_library
    lib = get_winpr_library()

    if isinstance(name, str):
        name_b = name.encode("ascii")
    else:
        name_b = name

    info_ptr = ctypes.c_void_p(0)
    rc = lib.QuerySecurityPackageInfoA(name_b, ctypes.byref(info_ptr))
    if rc != 0:
        raise WinPRError(
            "QuerySecurityPackageInfoA({0!r}) failed with status 0x{1:08X}"
            .format(name, rc))
    if not info_ptr.value:
        raise WinPRError(
            "QuerySecurityPackageInfoA({0!r}) returned NULL".format(name))

    # Read the SecPkgInfoA layout. From winpr/sspi.h:
    #
    #   typedef struct _SecPkgInfoA {
    #       UINT32 fCapabilities;     // offset 0
    #       UINT16 wVersion;          // offset 4
    #       UINT16 wRPCID;            // offset 6
    #       UINT32 cbMaxToken;        // offset 8
    #       SEC_CHAR* Name;           // offset 12 (or 16 on 64-bit due to alignment)
    #       SEC_CHAR* Comment;        // offset 16+ptr
    #   } SecPkgInfoA;
    #
    # Pointer alignment varies; we use a Structure with explicit fields.

    class SecPkgInfoA(ctypes.Structure):
        _fields_ = [
            ("fCapabilities", ctypes.c_uint32),
            ("wVersion", ctypes.c_uint16),
            ("wRPCID", ctypes.c_uint16),
            ("cbMaxToken", ctypes.c_uint32),
            ("Name", ctypes.c_char_p),
            ("Comment", ctypes.c_char_p),
        ]

    spi = ctypes.cast(info_ptr, ctypes.POINTER(SecPkgInfoA))[0]
    result = SecurityPackageInfo(
        name=spi.Name.decode("utf-8") if spi.Name else "",
        comment=spi.Comment.decode("utf-8") if spi.Comment else "",
        capabilities=int(spi.fCapabilities),
        version=int(spi.wVersion),
        max_token=int(spi.cbMaxToken),
    )

    # Free the buffer SSPI allocated for us.
    try:
        lib.FreeContextBuffer(info_ptr)
    except Exception:
        # Non-fatal: leak is annoying but not catastrophic in practice.
        pass

    return result


# ---------------------------------------------------------------------------
# AuthIdentity
# ---------------------------------------------------------------------------

# Flags from winpr/sspi.h:
SEC_WINNT_AUTH_IDENTITY_ANSI = 0x1
SEC_WINNT_AUTH_IDENTITY_UNICODE = 0x2


class AuthIdentity(object):
    """
    Pythonic view of a SEC_WINNT_AUTH_IDENTITY.

    Fields:
      username:  str
      domain:    str
      password:  str (plaintext - treat with care)
      flags:     int (raw SEC_WINNT_AUTH_IDENTITY_* bitmask)
    """

    def __init__(self, username, domain, password, flags=0):
        self.username = username
        self.domain = domain
        self.password = password
        self.flags = flags

    def __repr__(self):
        # Don't dump the password.
        return "AuthIdentity(user={0!r}, domain={1!r})".format(
            self.username, self.domain)


class _SecWinNTAuthIdentity(ctypes.Structure):
    """
    Layout of the C struct SEC_WINNT_AUTH_IDENTITY_W (Unicode variant).

    From winpr/sspi.h:

        typedef struct _SEC_WINNT_AUTH_IDENTITY_W {
            UINT16* User;               // pointer to UTF-16LE chars
            UINT32 UserLength;          // length in characters (not bytes)
            UINT16* Domain;
            UINT32 DomainLength;
            UINT16* Password;
            UINT32 PasswordLength;
            UINT32 Flags;
        } SEC_WINNT_AUTH_IDENTITY_W;

    The ANSI variant differs only in pointer type. FreeRDP almost always
    delivers the W variant on the server-side Logon callback, so we
    decode UTF-16LE.
    """
    _fields_ = [
        ("User", ctypes.POINTER(ctypes.c_uint16)),
        ("UserLength", ctypes.c_uint32),
        ("Domain", ctypes.POINTER(ctypes.c_uint16)),
        ("DomainLength", ctypes.c_uint32),
        ("Password", ctypes.POINTER(ctypes.c_uint16)),
        ("PasswordLength", ctypes.c_uint32),
        ("Flags", ctypes.c_uint32),
    ]


def _read_utf16_chars(ptr, num_chars):
    """Read num_chars UTF-16LE chars from ptr into a Python str."""
    if not ptr or num_chars == 0:
        return ""
    # Cast the pointer to an array of UINT16 so ctypes will let us iterate.
    arr_type = ctypes.c_uint16 * num_chars
    arr = ctypes.cast(ptr, ctypes.POINTER(arr_type))[0]
    # Pack the codepoints back to bytes and decode.
    raw = bytes(bytearray(
        b for code in arr for b in (code & 0xFF, (code >> 8) & 0xFF)))
    return raw.decode("utf-16-le", "replace")


def parse_logon_identity(identity_void_p):
    """
    Convert the raw c_void_p delivered to a peer Logon callback into an
    AuthIdentity. Returns None if the pointer is NULL.

    Use this from inside your Logon callback:

        from pyfreerdp.bindings.types import PEER_LOGON_FN

        @PEER_LOGON_FN
        def on_logon(peer, identity_p, automatic):
            identity = parse_logon_identity(identity_p)
            if identity and check_password(identity.username,
                                           identity.password):
                return 1   # TRUE - accept
            return 0       # FALSE - reject
    """
    if not identity_void_p:
        return None
    ptr = ctypes.cast(identity_void_p,
                      ctypes.POINTER(_SecWinNTAuthIdentity))
    if not ptr:
        return None
    s = ptr[0]
    user = _read_utf16_chars(s.User, s.UserLength)
    domain = _read_utf16_chars(s.Domain, s.DomainLength)
    pwd = _read_utf16_chars(s.Password, s.PasswordLength)
    return AuthIdentity(username=user, domain=domain,
                        password=pwd, flags=int(s.Flags))

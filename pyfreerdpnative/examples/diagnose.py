"""
Work out WHY a connection fails, layer by layer.

    python -m pyfreerdpnative.examples.diagnose <host> [port] [user] [password]

Checks, in order:
  1. DNS resolution
  2. TCP connect (is the port reachable at all from this device?)
  3. RDP negotiation by hand, over a plain socket - proves the server speaks
     RDP and reports which security layer it demands (TLS / NLA / RDP)
  4. TLS handshake with the bundled OpenSSL
  5. freerdp_connect(), with FreeRDP's own error string

Each step prints OK or the reason it failed, so the first failure names the
layer to fix instead of the generic "transport layer failed".
"""
import socket
import struct
import sys
import time


def step(n, what):
    sys.stdout.write("{0}. {1} ... ".format(n, what))
    sys.stdout.flush()


def ok(msg=""):
    print("OK" + (" (" + msg + ")" if msg else ""))


def fail(msg):
    print("FAILED: {0}".format(msg))


def x224_negotiate(sock, user=""):
    """
    Send an RDP Negotiation Request (MS-RDPBCGR 2.2.1.1) and read the reply.
    Returns (selected_protocol, failure_code) - protocol 0=RDP, 1=TLS, 2=NLA.
    """
    cookie = ("Cookie: mstshash=" + (user or "probe") + "\r\n").encode()
    # RDP_NEG_REQ: type 1, flags 0, length 8, requested protocols TLS|NLA
    neg = struct.pack("<BBHI", 0x01, 0x00, 0x0008, 0x00000003)
    x224 = struct.pack("!BBHHB", 0, 0xE0, 0, 0, 0)          # CR TPDU
    body = x224[1:] + cookie + neg
    tpkt = struct.pack("!BBH", 3, 0, 4 + 1 + len(body)) + struct.pack("!B", len(body)) + body
    sock.sendall(tpkt)
    head = sock.recv(4)
    if len(head) < 4:
        raise IOError("server closed the connection without replying")
    length = struct.unpack("!H", head[2:4])[0]
    rest = b""
    while len(rest) < length - 4:
        chunk = sock.recv(length - 4 - len(rest))
        if not chunk:
            break
        rest += chunk
    for off in range(len(rest) - 7):
        t = rest[off] if isinstance(rest[off], int) else ord(rest[off])
        if t in (2, 3):
            proto = struct.unpack("<I", rest[off + 4:off + 8])[0]
            return (proto, None) if t == 2 else (None, proto)
    return None, None


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 2:
        sys.exit("usage: python -m pyfreerdpnative.examples.diagnose "
                 "<host> [port] [user] [password]")
    host = argv[1]
    port = int(argv[2]) if len(argv) > 2 else 3389
    user = argv[3] if len(argv) > 3 else ""
    password = argv[4] if len(argv) > 4 else ""

    print("diagnosing {0}:{1}\n".format(host, port))

    step(1, "resolve " + host)
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        addrs = sorted(set(i[4][0] for i in infos))
        ok(", ".join(addrs))
    except Exception as exc:
        fail(str(exc))
        print("\n-> DNS does not resolve. Check the name, or use the IP address.")
        return 1

    step(2, "TCP connect")
    sock = None
    t0 = time.time()
    for info in infos:
        try:
            s = socket.socket(info[0], socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect(info[4])
            sock = s
            break
        except Exception as exc:
            last = exc
    if sock is None:
        fail("{0} after {1:.1f}s".format(last, time.time() - t0))
        print("\n-> The port is not reachable from this device. Mobile networks")
        print("   commonly block outbound 3389; try WiFi, a VPN, or a different")
        print("   port. No RDP client can work until this succeeds.")
        return 1
    ok("{0:.0f} ms".format((time.time() - t0) * 1000))

    step(3, "RDP negotiation")
    try:
        proto, failure = x224_negotiate(sock, user)
        if failure is not None:
            names = {1: "SSL_REQUIRED_BY_SERVER", 2: "SSL_NOT_ALLOWED_BY_SERVER",
                     3: "SSL_CERT_NOT_ON_SERVER", 4: "INCONSISTENT_FLAGS",
                     5: "HYBRID_REQUIRED_BY_SERVER", 6: "SSL_WITH_USER_AUTH_REQUIRED"}
            fail("server refused: " + names.get(failure, str(failure)))
        elif proto is None:
            fail("no negotiation response (is this really an RDP server?)")
        else:
            names = {0: "RDP (legacy, no TLS)", 1: "TLS", 2: "NLA/CredSSP",
                     8: "RDSTLS", 16: "AAD"}
            ok("server selected " + names.get(proto, "protocol " + str(proto)))
    except Exception as exc:
        fail(str(exc))
    finally:
        sock.close()

    step(4, "TLS handshake")
    try:
        import ssl
        raw = socket.create_connection((host, port), timeout=15)
        x224_negotiate(raw, user)                  # server switches to TLS after this
        ctxt = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctxt.check_hostname = False
        ctxt.verify_mode = ssl.CERT_NONE
        tls = ctxt.wrap_socket(raw, server_hostname=host)
        ok("{0}, cipher {1}".format(tls.version(), tls.cipher()[0]))
        tls.close()
    except Exception as exc:
        fail(str(exc))
        print("\n-> TLS to this server fails from this device. If step 2 and 3")
        print("   passed, the link is dropping mid-handshake (common on mobile")
        print("   data) or a middlebox is interfering. Retry on WiFi.")

    step(5, "FreeRDP libraries")
    try:
        from pyfreerdpnative import load
        api = load()
        ok(api.version())
    except Exception as exc:
        fail(str(exc))
        return 1

    if not user:
        print("\n(no user given - stopping before the authenticated connect)")
        return 0

    step(6, "freerdp_connect")
    import ctypes

    from pyfreerdpnative.freerdp import client as CLIENT
    from pyfreerdpnative.freerdp import freerdp as F
    from pyfreerdpnative.freerdp import settings_keys as KEY
    entry = CLIENT.RDP_CLIENT_ENTRY_POINTS_V1()
    entry.Size = ctypes.sizeof(entry)
    entry.Version = CLIENT.RDP_CLIENT_INTERFACE_VERSION
    entry.ContextSize = ctypes.sizeof(F.rdpContext)
    ctx = api.freerdp_client_context_new(ctypes.byref(entry))
    st = ctx.contents.settings
    api.freerdp_settings_set_string(st, KEY.FreeRDP_ServerHostname, host.encode())
    api.freerdp_settings_set_uint32(st, KEY.FreeRDP_ServerPort, port)
    api.freerdp_settings_set_string(st, KEY.FreeRDP_Username, user.encode())
    api.freerdp_settings_set_string(st, KEY.FreeRDP_Password, password.encode())
    api.freerdp_settings_set_bool(st, KEY.FreeRDP_IgnoreCertificate, True)
    # This server asked for NLA (step 3). Enable the security layers
    # explicitly and give the handshake room on a slow mobile link.
    for key, val in ((KEY.FreeRDP_NlaSecurity, True),
                     (KEY.FreeRDP_TlsSecurity, True),
                     (KEY.FreeRDP_RdpSecurity, False)):
        api.freerdp_settings_set_bool(st, key, val)
    for key, ms in ((KEY.FreeRDP_TcpConnectTimeout, 30000),
                    (KEY.FreeRDP_TcpAckTimeout, 30000)):
        try:
            api.freerdp_settings_set_uint32(st, key, ms)
        except Exception:
            pass
    if api.freerdp_connect(ctx.contents.instance):
        ok("connected")
        api.freerdp_disconnect(ctx.contents.instance)
    else:
        code = api.freerdp_get_last_error(ctx)
        msg = api.freerdp_get_last_error_string(code)
        fail("{0} (0x{1:08X})".format(msg.decode() if msg else "?", code))
        print("\n-> Step 3 tells you which security layer the server wants.")
        print("   Re-run with WLOG_LEVEL=DEBUG for FreeRDP's own trace.")
    api.freerdp_client_context_free(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())

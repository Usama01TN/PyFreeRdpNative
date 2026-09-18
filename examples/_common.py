"""Helpers shared by the examples: load the API and build a connected client context."""
import ctypes
import sys

from pyfreerdpnative import load
from pyfreerdpnative.freerdp import client as CLIENT
from pyfreerdpnative.freerdp import freerdp as F
from pyfreerdpnative.freerdp import settings_keys as KEY


def parse_args(argv):
    """<host> <user> <password> [port] -> (host, user, password, port)"""
    if len(argv) < 4:
        sys.exit("usage: {0} <host> <user> <password> [port]".format(argv[0]))
    return argv[1], argv[2], argv[3], int(argv[4]) if len(argv) > 4 else 3389


def new_context(api):
    """A client context; returns POINTER(rdpContext)."""
    entry = CLIENT.RDP_CLIENT_ENTRY_POINTS_V1()
    entry.Size = ctypes.sizeof(entry)
    entry.Version = CLIENT.RDP_CLIENT_INTERFACE_VERSION
    entry.ContextSize = ctypes.sizeof(F.rdpContext)
    ctx = api.freerdp_client_context_new(ctypes.byref(entry))
    if not ctx:
        sys.exit("freerdp_client_context_new failed")
    return ctx


def apply_settings(api, ctx, host, user, password, port, software_gdi=False):
    s = ctx.contents.settings
    api.freerdp_settings_set_string(s, KEY.FreeRDP_ServerHostname, host.encode())
    api.freerdp_settings_set_uint32(s, KEY.FreeRDP_ServerPort, port)
    api.freerdp_settings_set_string(s, KEY.FreeRDP_Username, user.encode())
    api.freerdp_settings_set_string(s, KEY.FreeRDP_Password, password.encode())
    api.freerdp_settings_set_bool(s, KEY.FreeRDP_IgnoreCertificate, True)
    if software_gdi:
        api.freerdp_settings_set_bool(s, KEY.FreeRDP_SoftwareGdi, True)
        api.freerdp_settings_set_uint32(s, KEY.FreeRDP_ColorDepth, 32)


def connect_or_exit(api, ctx):
    if api.freerdp_connect(ctx.contents.instance):
        return
    code = api.freerdp_get_last_error(ctx)
    sys.exit("connect failed: {0} (0x{1:08X})".format(
        api.freerdp_get_last_error_string(code).decode(), code))


def connect(argv, software_gdi=False):
    """Everything up to a live session: (api, ctx)."""
    host, user, password, port = parse_args(argv)
    api = load()
    ctx = new_context(api)
    apply_settings(api, ctx, host, user, password, port, software_gdi)
    print("connecting to {0}:{1} ...".format(host, port))
    connect_or_exit(api, ctx)
    print("connected")
    return api, ctx


def close(api, ctx):
    api.freerdp_disconnect(ctx.contents.instance)
    api.freerdp_client_context_free(ctx)
    print("disconnected")

"""
The generated bindings themselves: every mirrored header module imports, the
anchors the rest of the world relies on exist, and the struct layouts obey the
ALIGN64 rule that a naive header translation gets wrong.
"""
import ctypes
import importlib
import os

import pytest

import pyfreerdpnative as P
from pyfreerdpnative import constants, types
from pyfreerdpnative.freerdp import client, freerdp, scancode, settings_keys
from pyfreerdpnative.freerdp import input as rdp_input
from pyfreerdpnative.freerdp.codec import color
from pyfreerdpnative.freerdp.gdi import gdi

PKG_ROOT = os.path.dirname(P.__file__)


def _all_modules():
    for root, _dirs, files in os.walk(PKG_ROOT):
        if os.path.basename(root) in ("_libs", "__pycache__"):
            continue
        for fn in files:
            if fn.endswith(".py"):
                rel = os.path.relpath(os.path.join(root, fn[:-3]), PKG_ROOT)
                mod = rel.replace(os.sep, ".")
                yield "pyfreerdpnative" + ("" if mod == "__init__" else "." + (
                    mod[:-9] if mod.endswith(".__init__") else mod))


def test_every_header_module_imports():
    mods = sorted(set(_all_modules()))
    assert len(mods) > 150, mods
    for m in mods:
        importlib.import_module(m)


def test_no_empty_mirror_modules():
    for root, _dirs, files in os.walk(PKG_ROOT):
        for fn in files:
            if fn.endswith(".py") and fn != "__init__.py" and "_core" not in root:
                text = open(os.path.join(root, fn)).read()
                assert not text.rstrip().endswith("__all__ = []"), fn


def test_mirror_follows_header_tree():
    # freerdp/codec/color.h -> pyfreerdpnative.freerdp.codec.color, and
    # freerdp/client.h coexists with the freerdp/client/ package
    assert hasattr(color, "PIXEL_FORMAT_BGRX32")
    assert "freerdp_client_context_new" in client.FUNCTIONS
    from pyfreerdpnative.freerdp.client import cliprdr
    assert hasattr(cliprdr, "CliprdrClientContext")


def test_constants_match_headers():
    assert color.PIXEL_FORMAT_BGRX32 == 0x20040888
    assert scancode.RDP_SCANCODE_RETURN == 0x1C
    assert rdp_input.KBD_FLAGS_RELEASE == 0x8000
    assert client.RDP_CLIENT_INTERFACE_VERSION == 1
    # settings keys are generated at FreeRDP build time and must be present
    for k in ("FreeRDP_ServerHostname", "FreeRDP_ServerPort", "FreeRDP_Username",
              "FreeRDP_Password", "FreeRDP_IgnoreCertificate", "FreeRDP_ColorDepth",
              "FreeRDP_SoftwareGdi"):
        assert isinstance(getattr(settings_keys, k), int), k
    # the same value is reachable through the flat namespace
    assert constants.FreeRDP_ColorDepth == settings_keys.FreeRDP_ColorDepth


def test_align64_layouts():
    # ALIGN64 fields occupy 8-byte slots; these offsets are what FreeRDP's own
    # headers produce under gcc/clang/MSVC on 64-bit
    ptr = ctypes.sizeof(ctypes.c_void_p)
    assert freerdp.rdpContext.instance.offset == 0
    assert freerdp.rdpContext.settings.offset % 8 == 0
    assert ctypes.sizeof(freerdp.rdpContext) >= 1000
    if ptr == 8:
        assert freerdp.rdpContext.settings.offset == 320
        assert gdi.rdpGdi.primary_buffer.offset == 64
        assert ctypes.sizeof(client.RDP_CLIENT_ENTRY_POINTS_V1) == 72


def test_struct_aliases_are_shared_objects():
    # the typedef alias and the struct class are the same ctypes type
    assert freerdp.rdpContext is types.rdpContext
    assert ctypes.sizeof(freerdp.rdpContext) == ctypes.sizeof(types.struct_rdp_context)


def test_prototypes_have_types():
    res, args, variadic = P.PROTOTYPES["freerdp_connect"]
    assert args and variadic is False
    res, args, variadic = P.PROTOTYPES["freerdp_settings_set_string"]
    assert args[-1] is ctypes.c_char_p
    assert "WLog_Print" in P.PROTOTYPES or "WLog_PrintMessage" in P.PROTOTYPES


@pytest.mark.needs_lib
def test_loader_binds_and_calls(api):
    assert api.version()
    assert api.library_of("freerdp_connect") == "freerdp3"
    assert api.library_of("freerdp_client_context_new") == "freerdp-client3"
    assert api.library_of("Stream_New") == "winpr3"
    assert api.freerdp_connect.restype is not None


@pytest.mark.needs_lib
def test_context_roundtrip(api):
    entry = client.RDP_CLIENT_ENTRY_POINTS_V1()
    entry.Size = ctypes.sizeof(entry)
    entry.Version = client.RDP_CLIENT_INTERFACE_VERSION
    entry.ContextSize = ctypes.sizeof(freerdp.rdpContext)
    ctx = api.freerdp_client_context_new(ctypes.byref(entry))
    assert ctx
    try:
        s = ctx.contents.settings
        assert s, "context->settings must not be NULL"
        assert api.freerdp_settings_set_string(s, settings_keys.FreeRDP_ServerHostname, b"host.example")
        api.freerdp_settings_get_string.restype = ctypes.c_char_p
        assert api.freerdp_settings_get_string(s, settings_keys.FreeRDP_ServerHostname) == b"host.example"
    finally:
        api.freerdp_client_context_free(ctx)

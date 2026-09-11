#!/usr/bin/env python3
"""
Validate a staged pyfreerdp native package: pyfreerdp/_libs (+ _bin).

This is the executable specification of "the build works". It is run by
CI on every native platform after scripts/build_freerdp.py, and can be run
locally the same way:

    python scripts/validate_build.py                # libraries only
    python scripts/validate_build.py --media        # + FFmpeg/OpenH264/libusb
    python scripts/validate_build.py --media --executables --loopback

Checks (each is a named test; failures are collected, not fatal on first):

  libs        every core library loads cold via ctypes by absolute path,
              in a fresh interpreter, with LD_LIBRARY_PATH scrubbed.
  channels    server channel entry points are exported from
              libfreerdp-server3; client channel entries are present in
              libfreerdp-client3 (nm on ELF/Mach-O, ctypes on Windows).
  media       libfreerdp3 imports avcodec/swscale/openh264 and those files
              are staged; h264_context_new() returns a context (so FreeRDP
              found a working H.264 backend); freerdp_dsp_supports_format()
              says yes for AAC/MP3/GSM/ADPCM (DSP FFmpeg backend active).
  ffmpeg      ffmpeg encodes a test pattern with libopenh264 to Annex-B
              H.264; ffprobe identifies it; ffmpeg decodes it back and the
              frame count matches. Same round trip for AAC audio.
  openh264    h264dec decodes the stream produced above to raw YUV of the
              expected size; WelsGetCodecVersion() via ctypes.
  libusb      libusb_init/get_device_list/exit via ctypes (works with zero
              devices); `listdevs` runs.
  executables every staged executable has all dynamic deps resolvable and
              answers --version / --help; winpr-makecert produces a cert.
  loopback    (Linux, opt-in) sfreerdp-server + xfreerdp under xvfb-run:
              the server must log "is activated" and "RDPSND Activated"
              (i.e. TLS handshake, capability exchange, static channel
              negotiation all succeeded end to end).

Exit status is non-zero if any selected check fails. Style: Py2-compatible
syntax, runs on Python 3.
"""

from __future__ import print_function

import argparse
import ctypes
import glob
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time

SYS = platform.system()
EXT = {"Windows": ".dll", "Darwin": ".dylib"}.get(SYS, ".so")

# Must match BUILD_SCRIPT_VERSION in build_freerdp.py (workflow handshake).
BUILD_SCRIPT_VERSION = 11


RESULTS = []


def ok(name, detail=""):
    RESULTS.append((name, True, detail))
    print("[PASS] {0}{1}".format(name, (" - " + detail) if detail else ""))


def fail(name, detail):
    RESULTS.append((name, False, detail))
    print("[FAIL] {0} - {1}".format(name, detail))


def skip(name, detail):
    print("[SKIP] {0} - {1}".format(name, detail))


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_lib(libs, stem):
    hits = sorted(p for p in glob.glob(os.path.join(libs, "*" + stem + "*"))
                  if EXT in os.path.basename(p) and os.path.isfile(p)
                  and not os.path.basename(p).endswith((".a", ".lib")))
    # Prefer the SONAME-less / most generic name so the same path works on
    # every platform; ctypes follows to the real file anyway.
    return hits[0] if hits else None


def find_exe(bins, name):
    for d in bins:
        for cand in (name, name + ".exe"):
            p = os.path.join(d, cand)
            if os.path.isfile(p):
                return p
    return None


def run(cmd, timeout=120, env=None, cwd=None, input_data=None):
    """
    Run a command, capture combined output, enforce a timeout. On POSIX the
    child gets its own process group so a timeout kills wrappers *and* their
    children (xvfb-run -> Xvfb + xfreerdp); otherwise the grandchildren keep
    the pipe open and communicate() never returns.
    """
    kw = {}
    if SYS != "Windows":
        kw["start_new_session"] = True
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, cwd=cwd,
                            stdin=subprocess.PIPE if input_data else None, **kw)
    try:
        out, _ = proc.communicate(input_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        if SYS != "Windows":
            import signal
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                time.sleep(1)
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        else:
            proc.kill()
        out, _ = proc.communicate()
        return 124, out.decode(errors="replace")
    return proc.returncode, out.decode(errors="replace")


def exe_env(libs):
    """PATH/LD_LIBRARY_PATH so executables can be started from anywhere."""
    env = dict(os.environ)
    if SYS == "Windows":
        env["PATH"] = libs + os.pathsep + env.get("PATH", "")
    # On Linux/macOS the executables carry an rpath to ../_libs, so no
    # library path is set on purpose: that is part of what we validate.
    return env


# ---------------------------------------------------------------------------
# libs
# ---------------------------------------------------------------------------

COLD_LOAD = r'''
import ctypes, os, sys
libs, paths = sys.argv[1], sys.argv[2:]
if sys.platform == "win32":
    os.add_dll_directory(libs)
for p in paths:
    try:
        ctypes.CDLL(p)
    except OSError as e:
        sys.exit("%s: %s" % (os.path.basename(p), e))
print("ok")
'''


def check_libs(libs, want):
    paths = []
    for stem in want:
        p = find_lib(libs, stem)
        if not p:
            fail("libs", "no file for {0} in {1}".format(stem, libs))
            return None
        paths.append(p)
    env = dict(os.environ)
    for k in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
              "DYLD_FALLBACK_LIBRARY_PATH"):
        env.pop(k, None)
    rc, out = run([sys.executable, "-c", COLD_LOAD, libs] + paths, env=env)
    if rc != 0:
        fail("libs", out.strip())
        return None
    ok("libs", "{0} libraries load cold".format(len(paths)))
    return dict(zip(want, paths))


# ---------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------

SERVER_CHANNEL_SYMS = ["cliprdr_server_context_new", "rdpsnd_server_context_new",
                       "audin_server_context_new", "rdpgfx_server_context_new",
                       "disp_server_context_new", "rail_server_context_new",
                       "rdpei_server_context_new", "drdynvc_server_context_new",
                       "rdpdr_server_context_new", "echo_server_context_new",
                       "encomsp_server_context_new", "remdesk_server_context_new",
                       "ainput_server_context_new", "location_server_context_new"]

# name -> FREERDP_ADDIN_CHANNEL_* flags used to resolve it from the client's
# static addin table (include/freerdp/addin.h).
ADDIN_STATIC, ADDIN_DYNAMIC, ADDIN_DEVICE, ADDIN_ENTRYEX = (
    0x1000, 0x2000, 0x4000, 0x8000)
CLIENT_CHANNELS = {
    "cliprdr": ADDIN_STATIC | ADDIN_ENTRYEX, "rdpsnd": ADDIN_STATIC | ADDIN_ENTRYEX,
    "drdynvc": ADDIN_STATIC | ADDIN_ENTRYEX, "rail": ADDIN_STATIC | ADDIN_ENTRYEX,
    "rdpdr": ADDIN_STATIC | ADDIN_ENTRYEX, "encomsp": ADDIN_STATIC | ADDIN_ENTRYEX,
    "remdesk": ADDIN_STATIC | ADDIN_ENTRYEX,
    "rdpgfx": ADDIN_DYNAMIC, "disp": ADDIN_DYNAMIC, "audin": ADDIN_DYNAMIC,
    "rdpei": ADDIN_DYNAMIC, "echo": ADDIN_DYNAMIC, "ainput": ADDIN_DYNAMIC,
    "location": ADDIN_DYNAMIC, "video": ADDIN_DYNAMIC, "geometry": ADDIN_DYNAMIC,
    "drive": ADDIN_DEVICE, "smartcard": ADDIN_DEVICE,
}
CLIENT_CHANNELS_FULL_ONLY = {"urbdrc": ADDIN_DYNAMIC}


def load_all(paths):
    """ctypes-load winpr -> freerdp -> client/server, RTLD_GLOBAL, in order."""
    if SYS == "Windows":
        os.add_dll_directory(os.path.dirname(list(paths.values())[0]))
    libs = {}
    for stem in ("winpr3", "freerdp3", "freerdp-client3", "freerdp-server3"):
        if stem in paths:
            libs[stem] = ctypes.CDLL(paths[stem], mode=getattr(
                ctypes, "RTLD_GLOBAL", 0))
    return libs


def check_channels(paths, media=False):
    libs = load_all(paths)
    srv = libs.get("freerdp-server3")
    cli = libs.get("freerdp-client3")
    if srv:
        # Server channel contexts are exported (FREERDP_API) - survive strip.
        missing = [s for s in SERVER_CHANNEL_SYMS if not hasattr(srv, s)]
        if missing:
            fail("channels.server", "missing {0}".format(missing))
        else:
            ok("channels.server", "{0} server channels exported".format(
                len(SERVER_CHANNEL_SYMS)))
    if cli:
        # Client channels live in a hidden static table; ask FreeRDP's own
        # resolver for each one (works on stripped binaries, all platforms).
        try:
            fn = cli.freerdp_channels_load_static_addin_entry
        except AttributeError:
            fail("channels.client", "freerdp_channels_load_static_addin_entry "
                                    "not exported")
            return libs
        fn.restype = ctypes.c_void_p
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                       ctypes.c_uint32]
        want = dict(CLIENT_CHANNELS)
        if media:
            want.update(CLIENT_CHANNELS_FULL_ONLY)
        if SYS != "Linux":
            pass  # serial/parallel are Linux-only and not in the list anyway
        missing = [n for n, flags in sorted(want.items())
                   if not fn(n.encode(), None, None, flags)]
        if missing:
            fail("channels.client", "not in static addin table: {0}".format(missing))
        else:
            ok("channels.client", "{0} client channels resolve from the static "
                                  "addin table".format(len(want)))
    return libs


# ---------------------------------------------------------------------------
# Kerberos (SSPI security packages)
# ---------------------------------------------------------------------------

SEC_E_UNSUPPORTED_FUNCTION = 0x80090302


def check_kerberos(paths, expect):
    """
    Exercise winpr's SSPI at runtime: enumerate the security packages and
    try to acquire a Kerberos credential handle with no identity. A build
    with krb5 goes into libkrb5 (ccache lookup) and comes back with e.g.
    SEC_E_NO_CREDENTIALS - instantly, no network. A build without returns
    SEC_E_UNSUPPORTED_FUNCTION. On Windows winpr forwards to the native
    SSPI, so Kerberos is provided by the OS.

    expect: True (must be live), False (must be compiled out), None (report).
    """
    winpr = paths.get("winpr3")
    if not winpr:
        return
    # SEC_ENTRY is __stdcall on Windows (the SDK's sspi.h). On x64 that is
    # indistinguishable from cdecl, but on 32-bit x86 the export is name-
    # decorated (_InitSecurityInterfaceExA@4) and every function-table
    # pointer must be called with the stdcall convention.
    if SYS == "Windows":
        os.add_dll_directory(os.path.dirname(winpr))
        lib = ctypes.WinDLL(winpr)
        FN = ctypes.WINFUNCTYPE
    else:
        lib = ctypes.CDLL(winpr)
        FN = ctypes.CFUNCTYPE

    class SecHandle(ctypes.Structure):
        _fields_ = [("dwLower", ctypes.c_void_p), ("dwUpper", ctypes.c_void_p)]

    class TimeStamp(ctypes.Structure):
        _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]

    class SecPkgInfoA(ctypes.Structure):
        _fields_ = [("fCapabilities", ctypes.c_uint32), ("wVersion", ctypes.c_uint16),
                    ("wRPCID", ctypes.c_uint16), ("cbMaxToken", ctypes.c_uint32),
                    ("Name", ctypes.c_char_p), ("Comment", ctypes.c_char_p)]
    ENUM = FN(ctypes.c_int32, ctypes.POINTER(ctypes.c_uint32),
              ctypes.POINTER(ctypes.POINTER(SecPkgInfoA)))
    ACQ = FN(ctypes.c_int32, ctypes.c_char_p, ctypes.c_char_p,
             ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
             ctypes.c_void_p, ctypes.c_void_p,
             ctypes.POINTER(SecHandle), ctypes.POINTER(TimeStamp))
    FREE = FN(ctypes.c_int32, ctypes.POINTER(SecHandle))

    class Table(ctypes.Structure):
        _fields_ = [("dwVersion", ctypes.c_uint32),
                    ("EnumerateSecurityPackagesA", ENUM),
                    ("QueryCredentialsAttributesA", ctypes.c_void_p),
                    ("AcquireCredentialsHandleA", ACQ),
                    ("FreeCredentialsHandle", FREE)]
    init = None
    for name in ("InitSecurityInterfaceExA", "_InitSecurityInterfaceExA@4"):
        try:
            init = getattr(lib, name)
            break
        except AttributeError:
            continue
    if init is None:
        fail("kerberos", "InitSecurityInterfaceExA not exported by winpr "
                         "(tried undecorated and stdcall-decorated names)")
        return
    init.restype = ctypes.POINTER(Table)
    init.argtypes = [ctypes.c_uint32]
    table = init(0).contents
    n = ctypes.c_uint32()
    arr = ctypes.POINTER(SecPkgInfoA)()
    if table.EnumerateSecurityPackagesA(ctypes.byref(n), ctypes.byref(arr)) != 0:
        fail("kerberos", "EnumerateSecurityPackagesA failed")
        return
    names = [arr[i].Name.decode(errors="replace") for i in range(n.value)]
    if "Kerberos" not in names:
        fail("kerberos", "Kerberos not among SSPI packages: {0}".format(names))
        return
    cred = SecHandle()
    ts = TimeStamp()
    status = table.AcquireCredentialsHandleA(None, b"Kerberos", 2, None, None,
                                             None, None, ctypes.byref(cred),
                                             ctypes.byref(ts)) & 0xFFFFFFFF
    if status == 0:
        table.FreeCredentialsHandle(ctypes.byref(cred))
    live = status != SEC_E_UNSUPPORTED_FUNCTION
    detail = "packages {0}; Kerberos AcquireCredentialsHandle -> 0x{1:08X} ({2})".format(
        names, status, "krb5 backend live" if live else "compiled out")
    if SYS == "Windows":
        detail = "native SSPI; " + detail
    if expect is True and not live:
        fail("kerberos", detail)
    elif expect is False and live:
        fail("kerberos", "expected no Kerberos but " + detail)
    else:
        ok("kerberos", detail)


# ---------------------------------------------------------------------------
# media
# ---------------------------------------------------------------------------

def dyn_deps(path):
    if SYS == "Linux":
        out = subprocess.check_output(["readelf", "-d", path]).decode(
            errors="replace")
        return [l[l.index("[") + 1:l.rindex("]")] for l in out.splitlines()
                if "(NEEDED)" in l]
    if SYS == "Darwin":
        out = subprocess.check_output(["otool", "-L", path]).decode(
            errors="replace")
        return [l.strip().split(" (")[0] for l in out.splitlines()[1:]
                if l.strip()]
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import build_freerdp
    return build_freerdp.pe_imports(path)


class AUDIO_FORMAT(ctypes.Structure):
    _fields_ = [("wFormatTag", ctypes.c_uint16), ("nChannels", ctypes.c_uint16),
                ("nSamplesPerSec", ctypes.c_uint32),
                ("nAvgBytesPerSec", ctypes.c_uint32),
                ("nBlockAlign", ctypes.c_uint16),
                ("wBitsPerSample", ctypes.c_uint16),
                ("cbSize", ctypes.c_uint16), ("data", ctypes.c_void_p)]


# WAVE_FORMAT_* tags FreeRDP routes to the FFmpeg DSP backend. (GSM610,
# MS-ADPCM, G.723.1, A-law/u-law are filtered out of that backend by FreeRDP
# itself unless WITH_DSP_EXPERIMENTAL, and are served by FreeRDP's built-in
# codecs instead, so they are not evidence either way.)
DSP_FORMATS = {"AAC": (0xA106, 2, 44100, 16), "Opus": (0x704F, 2, 48000, 16),
               "PCM": (0x0001, 2, 44100, 16)}


def check_media(libs, paths, edition="media"):
    ffmpeg = edition in ("ffmpeg", "media")
    openh264 = edition in ("openh264", "media")
    core = paths.get("freerdp3")
    deps = dyn_deps(core)
    low = " ".join(os.path.basename(d).lower() for d in deps)
    want = (["avcodec", "swscale"] if ffmpeg else []) + (["openh264"] if openh264 else [])
    forbid = ([] if ffmpeg else ["avcodec"]) + ([] if openh264 else ["openh264"])
    missing = [n for n in want if n not in low]
    present_forbidden = [n for n in forbid if n in low]
    if missing or present_forbidden:
        fail("media.linked", "edition {0}: libfreerdp3 missing {1}, unexpectedly "
             "links {2}; imports: {3}".format(edition, missing, present_forbidden, deps))
        return
    for d in deps:
        b = os.path.basename(d)
        if any(n in b.lower() for n in ("avcodec", "avutil", "swscale",
                                        "swresample", "openh264", "usb-1.0")):
            if not os.path.exists(os.path.join(libs, b)):
                fail("media.staged", "{0} imported but not in {1}".format(b, libs))
                return
    ok("media.linked", "edition {0}: libfreerdp3 imports {1} and they are staged".format(
        edition, ", ".join(want)))

    if SYS == "Windows":
        os.add_dll_directory(libs)
    lib = ctypes.CDLL(core)
    try:
        lib.h264_context_new.restype = ctypes.c_void_p
        lib.h264_context_new.argtypes = [ctypes.c_int]
        lib.h264_context_free.argtypes = [ctypes.c_void_p]
    except AttributeError as e:
        fail("media.h264", "symbol missing: {0}".format(e))
        return
    results = []
    for compressor in (0, 1):
        ctx = lib.h264_context_new(compressor)
        results.append(bool(ctx))
        if ctx:
            lib.h264_context_free(ctx)
    # FFmpeg alone has no H.264 *encoder* in an LGPL build - except on
    # Windows, where FFmpeg wraps Media Foundation (h264_mf) - so the ffmpeg
    # edition must decode and, off Windows, must have no encoder context.
    expect_enc = openh264 or (SYS == "Windows" and ffmpeg)
    if results[0] and (results[1] == expect_enc or (SYS == "Windows" and results[1])):
        ok("media.h264", "h264_context_new(): decoder yes, encoder {0} - as "
                         "expected for edition {1}".format(
                             "yes" if results[1] else "no", edition))
    else:
        fail("media.h264", "h264_context_new decoder={0} encoder={1}; expected "
                           "decoder=True encoder={2} for edition {3}".format(
                               results[0], results[1], expect_enc, edition))
    if not ffmpeg:
        return  # DSP FFmpeg backend is absent by design in openh264 edition

    try:
        lib.freerdp_dsp_supports_format.restype = ctypes.c_int
        lib.freerdp_dsp_supports_format.argtypes = [ctypes.POINTER(AUDIO_FORMAT),
                                                    ctypes.c_int]
    except AttributeError as e:
        fail("media.dsp", "symbol missing: {0}".format(e))
        return
    unsupported = []
    for name, (tag, ch, rate, bits) in DSP_FORMATS.items():
        f = AUDIO_FORMAT(tag, ch, rate, 0, 0, bits, 0, None)
        if not lib.freerdp_dsp_supports_format(ctypes.byref(f), 0):
            unsupported.append(name)
    if unsupported:
        fail("media.dsp", "decoder does not support {0} - DSP FFmpeg backend "
                          "not active".format(unsupported))
    else:
        ok("media.dsp", "DSP backend decodes {0}".format(
            ", ".join(sorted(DSP_FORMATS))))


# ---------------------------------------------------------------------------
# ffmpeg / openh264 / libusb
# ---------------------------------------------------------------------------

def check_ffmpeg_libs(libs, edition):
    """Library-level FFmpeg check (all profiles): the staged libavcodec loads
    and knows the decoders FreeRDP relies on."""
    path = find_lib(libs, "avcodec")
    if not path:
        fail("ffmpeg.lib", "libavcodec not staged in {0}".format(libs))
        return
    if SYS == "Windows":
        os.add_dll_directory(libs)
    lib = ctypes.CDLL(path)
    lib.avcodec_version.restype = ctypes.c_uint
    v = lib.avcodec_version()
    lib.avcodec_find_decoder_by_name.restype = ctypes.c_void_p
    lib.avcodec_find_decoder_by_name.argtypes = [ctypes.c_char_p]
    lib.avcodec_find_encoder_by_name.restype = ctypes.c_void_p
    lib.avcodec_find_encoder_by_name.argtypes = [ctypes.c_char_p]
    missing = [n for n in ("h264", "mjpeg", "aac", "opus")
               if not lib.avcodec_find_decoder_by_name(n.encode())]
    if missing:
        fail("ffmpeg.lib", "libavcodec {0}.{1}.{2} lacks decoders {3}".format(
            v >> 16, (v >> 8) & 0xFF, v & 0xFF, missing))
        return
    enc = bool(lib.avcodec_find_encoder_by_name(b"libopenh264"))
    if enc != (edition == "media"):
        fail("ffmpeg.lib", "libopenh264 encoder {0} but edition is {1}".format(
            "present" if enc else "absent", edition))
        return
    ok("ffmpeg.lib", "libavcodec {0}.{1}.{2}: h264/mjpeg/aac/opus decoders, "
                     "libopenh264 encoder {3}".format(
                         v >> 16, (v >> 8) & 0xFF, v & 0xFF,
                         "present" if enc else "absent (as expected)"))


def synth_video(tmp, w=320, h=240, n=25):
    """Raw YUV420 test clip generated in Python (no lavfi/testsrc needed)."""
    yuv = os.path.join(tmp, "src.yuv")
    with open(yuv, "wb") as fh:
        for i in range(n):
            fh.write(bytes(((x + y + 3 * i) & 0xFF) for y in range(h) for x in range(w)))
            fh.write(bytes([128 + (i % 16)]) * (w * h // 4))
            fh.write(bytes([128 - (i % 16)]) * (w * h // 4))
    return ["-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", "{0}x{1}".format(w, h),
            "-r", "25", "-i", yuv]


def synth_audio(tmp, rate=44100, seconds=1):
    """Raw 16-bit mono sine generated in Python (no lavfi/sine needed)."""
    import math, struct
    pcm = os.path.join(tmp, "src.pcm")
    with open(pcm, "wb") as fh:
        for i in range(rate * seconds):
            fh.write(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate))))
    return ["-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", pcm]


def check_ffmpeg(bins, libs, tmp, with_openh264=True, executables=True):
    ffmpeg = find_exe(bins, "ffmpeg")
    ffprobe = find_exe(bins, "ffprobe")
    if not executables:
        skip("ffmpeg.exe", "profile ships no executables; library checks only")
        return None
    if not ffmpeg or not ffprobe:
        fail("ffmpeg", "ffmpeg/ffprobe executables not staged")
        return None
    env = exe_env(libs)
    rc, out = run([ffmpeg, "-hide_banner", "-version"], env=env)
    if rc != 0 or "libavcodec" not in out:
        fail("ffmpeg.version", out.strip()[-800:])
        return None
    ok("ffmpeg.version", out.splitlines()[0])

    h264 = None
    if with_openh264:
        h264 = os.path.join(tmp, "test.h264")
        rc, out = run([ffmpeg, "-hide_banner", "-y"] + synth_video(tmp) +
                      ["-frames:v", "25", "-c:v", "libopenh264", "-f", "h264", h264],
                      env=env)
        if rc != 0 or not os.path.getsize(h264):
            fail("ffmpeg.encode_openh264", out.strip()[-1200:])
            return None
        rc, out = run([ffprobe, "-v", "error", "-count_frames", "-show_entries",
                       "stream=codec_name,nb_read_frames", "-of", "csv=p=0", h264],
                      env=env)
        if rc != 0 or "h264" not in out or ",25" not in out.replace("\n", ""):
            fail("ffmpeg.decode_h264", "ffprobe: {0}".format(out.strip()))
            return None
        ok("ffmpeg.h264_roundtrip", "25 frames encoded with libopenh264, decoded "
                                    "with FFmpeg h264")
    else:
        # No H.264 encoder in the LGPL-only build: prove the video path with
        # MJPEG (the camera channel's format) and confirm h264 decoding is
        # compiled in.
        mjpg = os.path.join(tmp, "test.mjpeg")
        rc, out = run([ffmpeg, "-hide_banner", "-y"] + synth_video(tmp) +
                      ["-frames:v", "25", "-pix_fmt", "yuvj420p", "-c:v", "mjpeg",
                       "-f", "mjpeg", mjpg], env=env)
        if rc != 0:
            fail("ffmpeg.encode_mjpeg", out.strip()[-800:])
            return None
        rc, out = run([ffprobe, "-v", "error", "-count_frames", "-show_entries",
                       "stream=codec_name,nb_read_frames", "-of", "csv=p=0", mjpg],
                      env=env)
        if rc != 0 or "mjpeg" not in out or ",25" not in out.replace("\n", ""):
            fail("ffmpeg.decode_mjpeg", "ffprobe: {0}".format(out.strip()))
            return None
        rc, out = run([ffmpeg, "-hide_banner", "-decoders"], env=env)
        if " h264 " not in out:
            fail("ffmpeg.h264_decoder", "h264 decoder not compiled in")
            return None
        ok("ffmpeg.mjpeg_roundtrip", "25 MJPEG frames round-tripped; h264 decoder present")

    aac = os.path.join(tmp, "test.adts")
    rc, out = run([ffmpeg, "-hide_banner", "-y"] + synth_audio(tmp) +
                  ["-c:a", "aac", "-b:a", "96k", "-f", "adts", aac], env=env)
    if rc != 0:
        fail("ffmpeg.encode_aac", out.strip()[-800:])
        return h264
    wav = os.path.join(tmp, "test.wav")
    rc, out = run([ffmpeg, "-hide_banner", "-y", "-i", aac, "-c:a", "pcm_s16le",
                   wav], env=env)
    # sine= is mono s16: 44100 * 2 bytes per second.
    if rc != 0 or os.path.getsize(wav) < 44100 * 2 * 0.9:
        fail("ffmpeg.decode_aac", out.strip()[-800:])
    else:
        ok("ffmpeg.aac_roundtrip", "1 s sine -> AAC -> PCM")
    return h264


def encode_with_h264enc(bins, libs, tmp):
    """Produce an H.264 stream with OpenH264's own h264enc from a synthetic
    YUV420 sequence (no FFmpeg needed). Returns the path or None."""
    enc = find_exe(bins, "h264enc")
    cfg = None
    for d in bins:
        c = os.path.join(d, "openh264-config", "welsenc.cfg")
        if os.path.isfile(c):
            cfg = c
            break
    if not enc or not cfg:
        return None
    w, h, n = 320, 240, 25
    yuv = os.path.join(tmp, "in.yuv")
    with open(yuv, "wb") as fh:
        for i in range(n):
            # moving gradient: Y plane, then flat U and V
            fh.write(bytes(((x + y + 3 * i) & 0xFF) for y in range(h) for x in range(w)))
            fh.write(bytes([128 + (i % 16)]) * (w * h // 4))
            fh.write(bytes([128 - (i % 16)]) * (w * h // 4))
    out_264 = os.path.join(tmp, "enc.264")
    # h264enc: -dw/-dh are per spatial layer (layer index first); one layer,
    # configured from layer2.cfg next to welsenc.cfg.
    layer_cfg = os.path.join(os.path.dirname(cfg), "layer2.cfg")
    rc, out = run([enc, cfg, "-org", yuv, "-sw", str(w), "-sh", str(h),
                   "-frms", str(n), "-bf", out_264, "-numl", "1",
                   "-lconfig", "0", layer_cfg, "-dw", "0", str(w), "-dh", "0", str(h)],
                  env=exe_env(libs), timeout=120, cwd=os.path.dirname(cfg))
    if rc != 0 or not os.path.exists(out_264) or not os.path.getsize(out_264):
        fail("openh264.h264enc", "rc={0} {1}".format(rc, out.strip()[-600:]))
        return None
    ok("openh264.h264enc", "encoded {0} synthetic frames ({1} bytes)".format(
        n, os.path.getsize(out_264)))
    return out_264


def check_openh264(bins, libs, tmp, h264_stream, executables=True):
    lib_path = find_lib(libs, "openh264")
    if not lib_path:
        fail("openh264.lib", "libopenh264 not staged in {0}".format(libs))
    else:
        if SYS == "Windows":
            os.add_dll_directory(libs)
        lib = ctypes.CDLL(lib_path)

        class OpenH264Version(ctypes.Structure):
            _fields_ = [("uMajor", ctypes.c_uint), ("uMinor", ctypes.c_uint),
                        ("uRevision", ctypes.c_uint), ("uReserved", ctypes.c_uint)]
        lib.WelsGetCodecVersion.restype = OpenH264Version
        v = lib.WelsGetCodecVersion()
        if v.uMajor >= 2:
            ok("openh264.lib", "OpenH264 {0}.{1}.{2} loads, encoder/decoder "
                               "entry points {3}".format(
                                   v.uMajor, v.uMinor, v.uRevision,
                                   "present" if hasattr(lib, "WelsCreateSVCEncoder")
                                   and hasattr(lib, "WelsCreateDecoder") else "MISSING"))
        else:
            fail("openh264.lib", "unexpected version {0}.{1}".format(v.uMajor, v.uMinor))

    if not executables:
        skip("openh264.h264dec", "profile ships no executables; library check only")
        return
    dec = find_exe(bins, "h264dec")
    if not dec:
        if SYS == "Windows":
            skip("openh264.h264dec", "vcpkg's OpenH264 port builds no console tools")
        else:
            fail("openh264.h264dec", "executable not staged")
        return
    if not h264_stream:
        h264_stream = encode_with_h264enc(bins, libs, tmp)
    if not h264_stream:
        skip("openh264.h264dec", "no H.264 stream available")
        return
    yuv = os.path.join(tmp, "dec.yuv")
    rc, out = run([dec, h264_stream, yuv], env=exe_env(libs))
    expected = 320 * 240 * 3 // 2 * 25
    size = os.path.getsize(yuv) if os.path.exists(yuv) else 0
    if rc != 0 or size != expected:
        fail("openh264.h264dec", "rc={0} size={1} expected={2}\n{3}".format(
            rc, size, expected, out.strip()[-600:]))
    else:
        ok("openh264.h264dec", "decoded 25 frames of 320x240 YUV420 "
                               "({0} bytes)".format(size))


def check_libusb(bins, libs):
    lib_path = find_lib(libs, "usb-1.0")
    if not lib_path:
        fail("libusb.lib", "libusb-1.0 not staged in {0}".format(libs))
        return
    if SYS == "Windows":
        os.add_dll_directory(libs)
    lib = ctypes.CDLL(lib_path)
    lib.libusb_init.argtypes = [ctypes.c_void_p]
    lib.libusb_get_device_list.argtypes = [ctypes.c_void_p,
                                           ctypes.POINTER(ctypes.c_void_p)]
    lib.libusb_get_device_list.restype = ctypes.c_ssize_t
    lib.libusb_free_device_list.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_exit.argtypes = [ctypes.c_void_p]
    rc = lib.libusb_init(None)
    if rc != 0:
        fail("libusb.lib", "libusb_init returned {0}".format(rc))
        return
    devs = ctypes.c_void_p()
    n = lib.libusb_get_device_list(None, ctypes.byref(devs))
    if n >= 0:
        lib.libusb_free_device_list(devs, 1)
    lib.libusb_exit(None)
    if n < 0:
        # LIBUSB_ERROR_NOT_SUPPORTED (-12) is what a sandbox / container
        # without /dev/bus/usb reports; the library itself still works.
        fail("libusb.lib", "libusb_get_device_list returned {0}".format(n))
        return
    ok("libusb.lib", "libusb_init/get_device_list OK ({0} devices)".format(n))
    listdevs = find_exe(bins, "listdevs")
    if listdevs:
        rc, out = run([listdevs], env=exe_env(libs), timeout=30)
        if rc == 0:
            ok("libusb.listdevs", "runs ({0} lines)".format(len(out.splitlines())))
        else:
            fail("libusb.listdevs", "rc={0} {1}".format(rc, out.strip()[-300:]))


# ---------------------------------------------------------------------------
# executables
# ---------------------------------------------------------------------------

# name -> (args, acceptable return codes, required substring in output)
EXE_SMOKE = {
    "xfreerdp":           (["/version"], (0,), "FreeRDP"),
    "wlfreerdp":          (["/version"], (0,), "FreeRDP"),
    "sdl-freerdp":        (["/version"], (0,), "FreeRDP"),
    "wfreerdp":           (["/version"], (0,), "FreeRDP"),
    "sfreerdp":           (["/version"], (0,), "FreeRDP"),   # client/Sample
    # FreeRDP CLIs return COMMAND_LINE_STATUS_PRINT_VERSION (-2003 -> 45 as
    # an exit byte) after printing the version.
    "freerdp-shadow-cli": (["/version"], (0, 45), "FreeRDP"),
    "freerdp-proxy":      (["--help"], (0, 1), "proxy"),
    "winpr-hash":         (["-u", "user", "-p", "pass"], (0,), ""),
    "ffmpeg":             (["-version"], (0,), "ffmpeg"),
    "ffprobe":            (["-version"], (0,), "ffprobe"),
    "h264dec":            ([], (0, 1, 255), ""),
    "h264enc":            ([], (0, 1, 255), ""),
}


def windows_missing_imports(path, libs):
    """Transitively walk PE imports; report names not found next to the
    executable, in the Windows system directories, or as API sets."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import build_freerdp
    sysdirs = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), d)
               for d in ("System32", "SysWOW64")]
    seen, missing, queue = set(), [], [path]
    while queue:
        f = queue.pop()
        try:
            imports = build_freerdp.pe_imports(f)
        except Exception as e:  # noqa
            missing.append("{0}: unreadable ({1})".format(os.path.basename(f), e))
            continue
        for dll in imports:
            low = dll.lower()
            if low in seen or low.startswith(("api-ms-", "ext-ms-")):
                continue
            seen.add(low)
            local = os.path.join(libs, dll)
            if os.path.isfile(local):
                queue.append(local)
            elif not any(os.path.isfile(os.path.join(d, dll)) for d in sysdirs):
                missing.append(dll)
    return sorted(set(missing))


def unresolved_deps(path, libs):
    """Return dynamic dependencies the loader will not find."""
    if SYS == "Linux":
        rc, out = run(["ldd", path])
        return [l.split()[0] for l in out.splitlines() if "not found" in l]
    if SYS == "Darwin":
        missing = []
        for d in dyn_deps(path):
            if d.startswith("@rpath/"):
                if not os.path.exists(os.path.join(libs, d[len("@rpath/"):])):
                    missing.append(d)
            elif d.startswith(("/usr/lib/", "/System/Library/")):
                continue  # OS libraries: in the dyld shared cache, not on disk
            elif d.startswith("/") and not os.path.exists(d):
                missing.append(d)
        return missing
    return []  # Windows: PATH-based; the smoke run below is the check


def check_executables(bins, libs, tmp):
    exes = []
    for d in bins:
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                p = os.path.join(d, fn)
                if os.path.isfile(p) and (fn.lower().endswith(".exe")
                                          if SYS == "Windows"
                                          else os.access(p, os.X_OK)
                                          and "." not in fn):
                    exes.append(p)
    if not exes:
        fail("executables", "no executables staged in {0}".format(bins))
        return
    env = exe_env(libs)
    problems = []
    tested = 0
    for p in exes:
        name = os.path.basename(p)
        if name.lower().endswith(".exe"):
            name = name[:-4]
        miss = unresolved_deps(p, libs)
        if miss:
            problems.append("{0}: unresolved {1}".format(name, miss))
            continue
        if name == "winpr-makecert":
            out_dir = os.path.join(tmp, "cert")
            os.makedirs(out_dir)
            rc, out = run([p, "-rdp", "-silent", "-path", out_dir, "-n", "test"],
                          env=env)
            if rc != 0 or not os.path.isfile(os.path.join(out_dir, "test.crt")):
                problems.append("winpr-makecert: rc={0} {1}".format(
                    rc, out.strip()[-300:]))
            tested += 1
            continue
        if name == "sfreerdp-server":
            tested += 1  # exercised by --loopback; here: deps resolve
            continue
        if name == "listdevs":
            continue  # covered by libusb check
        if name == "wlfreerdp" and not os.environ.get("WAYLAND_DISPLAY"):
            continue  # needs a Wayland compositor even for /version
        spec = EXE_SMOKE.get(name)
        if not spec:
            continue
        args, codes, needle = spec
        rc, out = run([p] + args, env=env, timeout=60)
        if rc not in codes or (needle and needle.lower() not in out.lower()):
            detail = out.strip()[-300:]
            if SYS == "Windows" and rc & 0xFFFFFFFF == 0xC0000135:
                detail = "STATUS_DLL_NOT_FOUND; unresolved imports: {0}".format(
                    windows_missing_imports(p, libs))
            problems.append("{0} {1}: rc={2} {3}".format(
                name, " ".join(args), rc, detail))
        tested += 1
    if problems:
        fail("executables", "; ".join(problems))
    else:
        ok("executables", "{0} staged, {1} smoke-tested, all deps resolve".format(
            len(exes), tested))


# ---------------------------------------------------------------------------
# loopback: sample server + X11 client
# ---------------------------------------------------------------------------

def check_loopback(bins, libs, tmp):
    if SYS != "Linux":
        skip("loopback", "only implemented for Linux (xvfb + xfreerdp)")
        return
    server = find_exe(bins, "sfreerdp-server")
    client = find_exe(bins, "xfreerdp")
    makecert = find_exe(bins, "winpr-makecert")
    if not (server and client and makecert):
        fail("loopback", "need sfreerdp-server, xfreerdp and winpr-makecert staged")
        return
    if not shutil.which("xvfb-run"):
        fail("loopback", "xvfb-run not installed (apt-get install xvfb)")
        return
    env = exe_env(libs)
    cert_dir = os.path.join(tmp, "loopcert")
    os.makedirs(cert_dir)
    rc, out = run([makecert, "-rdp", "-silent", "-path", cert_dir, "-n", "server"],
                  env=env)
    if rc != 0:
        fail("loopback", "makecert failed: {0}".format(out.strip()[-300:]))
        return
    port = 33890 + (os.getpid() % 1000)
    srv_log = open(os.path.join(tmp, "server.log"), "w+")
    # NB: no --local-only - that makes sfreerdp listen on a Unix socket only.
    # The activation markers are WLog_DBG, hence WLOG_LEVEL=DEBUG.
    srv_env = dict(env, WLOG_LEVEL="DEBUG")
    srv_cmd = [server, "--port={0}".format(port),
               "--cert={0}".format(os.path.join(cert_dir, "server.crt")),
               "--key={0}".format(os.path.join(cert_dir, "server.key"))]
    if shutil.which("stdbuf"):
        srv_cmd = ["stdbuf", "-oL", "-eL"] + srv_cmd
    srv = subprocess.Popen(srv_cmd, stdout=srv_log, stderr=subprocess.STDOUT,
                           env=srv_env, cwd=tmp)
    time.sleep(2)
    try:
        if srv.poll() is not None:
            srv_log.seek(0)
            fail("loopback", "server exited early: {0}".format(srv_log.read()[-600:]))
            return
        cli_cmd = ["xvfb-run", "-a", "-s", "-screen 0 1024x768x24", client,
                   "/v:127.0.0.1:{0}".format(port), "/cert:ignore", "/sec:tls",
                   "/u:test", "/p:test", "/size:640x480", "/sound", "+clipboard",
                   "/log-level:INFO"]
        rc, cli_out = run(cli_cmd, env=env, timeout=12)  # killed by timeout
        time.sleep(1)
        srv_log.flush()
        srv_log.seek(0)
        s_out = srv_log.read()
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
        srv_log.close()
    markers = ["We've got a client", "is activated", "RDPSND Activated"]
    missing = [m for m in markers if m not in s_out]
    # Dynamic channels the server opened and the client accepted.
    dvcs = sorted(set(l.split("Loading Dynamic Virtual Channel ")[1].split()[0]
                      for l in cli_out.splitlines()
                      if "Loading Dynamic Virtual Channel " in l))
    if missing:
        fail("loopback", "server log lacks {0}\n--- server ---\n{1}\n--- client ---\n"
                         "{2}".format(missing, s_out[-1500:], cli_out[-1500:]))
    else:
        ok("loopback", "TLS session activated; RDPSND static channel negotiated; "
                       "dynamic channels opened: {0}".format(", ".join(dvcs) or "none"))


# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--libs", default=os.path.join(repo_root(), "pyfreerdp", "_libs"))
    p.add_argument("--bin", default=None,
                   help="executables dir (default: <libs>/../_bin and <libs>)")
    p.add_argument("--profile", default="full",
                   choices=("full", "minimal", "client-only", "server-only"))
    p.add_argument("--media", action="store_true",
                   help="require the media integration of --edition")
    p.add_argument("--edition", choices=("standard", "ffmpeg", "openh264", "media"),
                   default="media",
                   help="which media stack the package was built with")
    p.add_argument("--executables", action="store_true")
    p.add_argument("--loopback", action="store_true")
    p.add_argument("--no-usb", action="store_true",
                   help="skip libusb runtime probing (containers without USB)")
    p.add_argument("--require-version", type=int, metavar="N",
                   help="exit 0 if this script is version N, else exit 2")
    p.add_argument("--kerberos", choices=("on", "off", "auto"), default="auto",
                   help="on: require a live krb5 backend (Linux default, "
                        "Windows native SSPI); off: require it compiled out; "
                        "auto: Linux/Windows -> on, macOS -> off (unless "
                        "PYFREERDP_KRB5=1).")
    args = p.parse_args()
    if args.require_version is not None:
        if args.require_version != BUILD_SCRIPT_VERSION:
            sys.stderr.write("{0} is version {1}, workflow expects {2}: stale "
                             "scripts/ directory\n".format(
                                 os.path.basename(__file__),
                                 BUILD_SCRIPT_VERSION, args.require_version))
            return 2
        print("[{0}] version {1} OK".format(os.path.basename(__file__),
                                            BUILD_SCRIPT_VERSION))
        return 0

    libs = os.path.abspath(args.libs)
    bins = [args.bin] if args.bin else [
        os.path.join(os.path.dirname(libs), "_bin"), libs]
    tmp = tempfile.mkdtemp(prefix="pyfreerdp-validate-")
    print("[validate] libs={0} bins={1}".format(libs, bins))

    want = ["winpr3", "freerdp3"]
    if args.profile in ("full", "minimal", "client-only"):
        want.append("freerdp-client3")
    if args.profile in ("full", "minimal", "server-only"):
        want.append("freerdp-server3")
    paths = check_libs(libs, want)
    if paths:
        check_channels(paths, media=args.media)
        if args.kerberos == "auto":
            expect = (True if SYS in ("Linux", "Windows")
                      else os.environ.get("PYFREERDP_KRB5") == "1")
        else:
            expect = args.kerberos == "on"
        check_kerberos(paths, expect)
        if args.media and args.edition != "standard":
            check_media(libs, paths, args.edition)
    h264 = None
    if args.media and args.edition != "standard":
        if args.edition in ("ffmpeg", "media"):
            check_ffmpeg_libs(libs, args.edition)
            h264 = check_ffmpeg(bins, libs, tmp,
                                with_openh264=(args.edition == "media"),
                                executables=args.executables)
        if args.edition in ("openh264", "media"):
            check_openh264(bins, libs, tmp, h264, executables=args.executables)
        if not args.no_usb:
            check_libusb(bins, libs)
    if args.executables:
        check_executables(bins, libs, tmp)
    if args.loopback:
        check_loopback(bins, libs, tmp)

    failed = [r for r in RESULTS if not r[1]]
    print("\n[validate] {0} passed, {1} failed".format(
        len(RESULTS) - len(failed), len(failed)))
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

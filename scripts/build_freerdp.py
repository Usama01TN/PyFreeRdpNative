#!/usr/bin/env python3
"""
build_freerdp.py - fetch + compile FreeRDP (client + server), copy artifacts
                   into pyfreerdp/_libs/.

Usage:
    python -m pyfreerdp.scripts.build_freerdp [--ref v3.16.0]
                                              [--prefix /opt/freerdp]
                                              [--target {host,android,ios}]
                                              [--abi arm64-v8a]
                                              [--jobs 8]
                                              [--profile {full,client-only,server-only,minimal}]
                                              [--enable-channel NAME ...]
                                              [--disable-channel NAME ...]
                                              [--no-channels] [--list-channels]

Profiles
--------
    full          (default) Build client + server + shadow server + proxy.
                  Produces libfreerdp-client3, libfreerdp-server3,
                  libfreerdp-shadow3, plus winpr3, freerdp3.
    client-only   Skip server-side libs. Smaller, faster build.
    server-only   Skip client-side display/input glue.
    minimal       Library cores only - no sample binaries, no manpages,
                  no proxy, no shadow. Useful for embedded mobile builds
                  where you only want libfreerdp + libfreerdp-server.

Why --profile=full is the default
---------------------------------
The previous version of this script built client-only. That kept the build
modest but meant `freerdp-server3.so` never landed in `_libs/`, so the
Python server bindings (server.py, peer.py) had nothing to link against at
runtime. Anyone using RdpServer would get FreeRdpNotFoundError. We now build
both halves by default so the Python package is functional out of the box.

Channels (virtual channels: clipboard, audio, drive redirection, gfx, ...)
--------------------------------------------------------------------------
FreeRDP ships ~30 channels under channels/. Each has a client half and/or a
server half, selected with three CMake switches per channel:

    CHANNEL_<NAME>          master switch
    CHANNEL_<NAME>_CLIENT   compile the client side into libfreerdp-client3
    CHANNEL_<NAME>_SERVER   compile the server side into libfreerdp-server3

In FreeRDP 3.x channels are NOT separate plugin .so files: they are OBJECT
libraries linked into the client/server library and registered through a
generated static entry table. So there is nothing extra to copy into
_libs/ and no plugin path to configure at runtime - but it also means the
decision of which channels exist is made at *configure* time, and a
silently-disabled channel simply isn't there. This script therefore:

    * passes every CHANNEL_* switch explicitly (see CHANNELS below) instead
      of trusting upstream defaults, which vary by platform;
    * enables the server side of every channel that has one whenever the
      profile builds the server, so RdpServer can negotiate
      clipboard/audio/gfx/rail/etc. with connecting clients;
    * enables the client side of every channel whose dependencies are met
      by the default toolchain, and leaves the ones that need extra system
      libraries (urbdrc/libusb, printer/cups, rdpecam/ffmpeg, tsmf) OFF
      until you ask for them;
    * re-reads CMakeCache.txt after configure and fails fast if CMake
      dropped a channel we asked for.

CLI:
    --enable-channel NAME     turn on a default-off channel (repeatable)
    --disable-channel NAME    turn off a default-on channel (repeatable)
    --no-channels             WITH_CHANNELS=OFF - bare protocol build
    --list-channels           print the table below and exit

Style: Py2-compatible syntax. Runs on Python 3.
"""
import argparse
import glob
import os
import platform
import shutil
import subprocess
import sys
import tempfile

REPO_URL = "https://github.com/FreeRDP/FreeRDP.git"
DEFAULT_REF = "3.16.0"


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def run(cmd, cwd=None, env=None, check=True):
    printable = " ".join(str(c) for c in cmd)
    print("\n$ {0}".format(printable))
    sys.stdout.flush()
    proc = subprocess.run(cmd, cwd=cwd, env=env)
    if check and proc.returncode != 0:
        save_diagnostics(cmd)
        raise SystemExit("Command failed (exit {0}): {1}".format(
            proc.returncode, printable))
    return proc.returncode


def save_diagnostics(cmd=None):
    """
    Copy CMake's configure/build logs into <repo>/build/diagnostics so CI can
    upload them when a step fails (the interesting error is often in
    CMakeConfigureLog.yaml, not in the console output).
    """
    build_dir = None
    if cmd:
        for i, c in enumerate(cmd):
            if c == "-B" and i + 1 < len(cmd):
                build_dir = cmd[i + 1]
            elif c == "--build" and i + 1 < len(cmd):
                build_dir = cmd[i + 1]
            elif c == "--install" and i + 1 < len(cmd):
                build_dir = cmd[i + 1]
    build_dir = build_dir or os.environ.get("PYFREERDP_BUILD_DIR")
    if not build_dir or not os.path.isdir(build_dir):
        return
    out = os.path.join(repo_root(), "build", "diagnostics")
    if not os.path.isdir(out):
        os.makedirs(out)
    for rel in ("CMakeCache.txt", os.path.join("CMakeFiles", "CMakeConfigureLog.yaml"),
                os.path.join("CMakeFiles", "CMakeOutput.log"),
                os.path.join("CMakeFiles", "CMakeError.log"), ".ninja_log"):
        p = os.path.join(build_dir, rel)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(out, os.path.basename(p)))
    print("[diagnostics] CMake logs copied to {0}".format(out))


def have(tool):
    return shutil.which(tool) is not None


def require_tools(tools):
    missing = [t for t in tools if not have(t)]
    if missing:
        raise SystemExit(
            "Missing required tools: {0}. Install them and retry.".format(
                missing))


def package_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def repo_root():
    # The script lives in <repo>/scripts/, so the repo is package_root().
    # (Used for build/<target> install dirs, cmake/toolchains/, build/deps/.)
    return package_root()


# ---------------------------------------------------------------------------
# Source acquisition
# ---------------------------------------------------------------------------

def fetch_source(ref, dest):
    src = os.path.join(dest, "FreeRDP")
    if os.path.isdir(os.path.join(src, ".git")):
        print("[fetch] Reusing existing checkout at {0}".format(src))
        run(["git", "fetch", "--tags", "--depth=1", "origin", ref], cwd=src)
        run(["git", "checkout", "--force", ref], cwd=src)
        return src
    if not os.path.isdir(dest):
        os.makedirs(dest)
    run(["git", "clone", "--depth=1", "--branch", ref, REPO_URL, src])
    return src


# ---------------------------------------------------------------------------
# CMake option assembly
# ---------------------------------------------------------------------------

# Channel table, taken from channels/*/ChannelOptions.cmake in FreeRDP
# 3.16.0. Keys are the CMake channel names (CHANNEL_<NAME> after upper()).
#
#   type    : "static" (goes over the MCS/static virtual channel layer),
#             "dynamic" (needs drdynvc), "device" (needs rdpdr).
#   client  : upstream ships a client implementation
#   server  : upstream ships a server implementation
#   default : should this script build it when nothing is said? Differs
#             from upstream where the channel needs extra -dev packages or
#             is deprecated/experimental (see 'deps' / 'note').
#   deps    : extra system libraries the *client* side pulls in
#   extra   : additional -D flags to pass when the channel is enabled
CHANNELS = {
    # -- transports (everything else rides on these) ----------------------
    "drdynvc":  dict(type="static",  client=True,  server=True,  default=True,
                     note="dynamic virtual channel transport (required by "
                          "every 'dynamic' channel)"),
    "rdpdr":    dict(type="static",  client=True,  server=True,  default=True,
                     note="device redirection transport (required by every "
                          "'device' channel)"),
    # -- core desktop features --------------------------------------------
    "cliprdr":  dict(type="static",  client=True,  server=True,  default=True,
                     note="clipboard"),
    "rdpsnd":   dict(type="static",  client=True,  server=True,  default=True,
                     note="audio output"),
    "audin":    dict(type="dynamic", client=True,  server=True,  default=True,
                     note="audio input / microphone"),
    "rdpgfx":   dict(type="dynamic", client=True,  server=True,  default=True,
                     note="graphics pipeline (RDPEGFX)"),
    "disp":     dict(type="dynamic", client=True,  server=True,  default=True,
                     note="display control: resize, multimon"),
    "rail":     dict(type="static",  client=True,  server=True,  default=True,
                     note="RemoteApp"),
    "rdpei":    dict(type="dynamic", client=True,  server=True,  default=True,
                     note="multitouch input"),
    "ainput":   dict(type="dynamic", client=True,  server=True,  default=True,
                     note="advanced (relative) mouse input"),
    "location": dict(type="dynamic", client=True,  server=True,  default=True,
                     note="geolocation redirection"),
    "echo":     dict(type="dynamic", client=True,  server=True,  default=True,
                     note="echo channel, latency probing"),
    "encomsp":  dict(type="static",  client=True,  server=True,  default=True,
                     note="multiparty / shadowing control"),
    "remdesk":  dict(type="static",  client=True,  server=True,  default=True,
                     note="remote assistance"),
    "drive":    dict(type="device",  client=True,  server=False, default=True,
                     note="drive redirection (client only)"),
    "smartcard": dict(type="device", client=True,  server=False, default=True,
                     note="smartcard redirection; pcsc-lite is dlopen'ed at "
                          "runtime, no build-time dep"),
    "video":    dict(type="dynamic", client=True,  server=False, default=True,
                     note="video optimized remoting (client only)"),
    "geometry": dict(type="dynamic", client=True,  server=False, default=True,
                     note="geometry tracking, used by video (client only)"),
    "serial":   dict(type="device",  client=True,  server=False, default=True,
                     note="serial port redirection; Linux only",
                     platforms=("Linux",)),
    "parallel": dict(type="device",  client=True,  server=False, default=True,
                     note="parallel port redirection; Linux only",
                     platforms=("Linux",)),
    # -- server-only channels (no client half exists upstream) ------------
    "telemetry": dict(type="dynamic", client=False, server=True, default=True,
                      note="telemetry (server only)"),
    "rdpemsc":  dict(type="dynamic", client=False, server=True,  default=True,
                     note="mouse cursor shape (server only)"),
    "gfxredir": dict(type="dynamic", client=False, server=True,  default=False,
                     note="graphics redirection, upstream default OFF "
                          "(server only)"),
    # -- channels that need extra system libraries: OFF by default --------
    "rdpecam":  dict(type="dynamic", client=True,  server=True,  default=True,
                     note="camera redirection; server side has no deps and "
                          "is ON, client needs FFmpeg swscale (+ v4l) so it "
                          "stays OFF until --enable-channel rdpecam",
                     deps="ffmpeg", client_default=False),
    "urbdrc":   dict(type="dynamic", client=True,  server=False, default=False,
                     note="USB redirection; needs libusb-1.0",
                     deps="libusb"),
    "printer":  dict(type="device",  client=True,  server=False, default=False,
                     note="printer redirection; needs CUPS",
                     deps="cups", extra=["-DWITH_CUPS=ON"]),
    "tsmf":     dict(type="dynamic", client=True,  server=False, default=False,
                     note="legacy multimedia redirection; deprecated, needs "
                          "GStreamer or FFmpeg", deps="gstreamer"),
    # -- upstream default OFF, niche -------------------------------------
    "rdpear":   dict(type="dynamic", client=True,  server=False, default=False,
                     note="Kerberos/NTLM remote credential guard; optional "
                          "krb5 dep, upstream default OFF"),
    "rdp2tcp":  dict(type="static",  client=True,  server=False, default=False,
                     note="TCP tunnelling over a static channel, upstream "
                          "default OFF"),
    "sshagent": dict(type="dynamic", client=True,  server=False, default=False,
                     note="ssh-agent forwarding, upstream default OFF"),
}

# Bumped whenever the CLI/behaviour changes in a way the workflows depend on.
# .github/workflows/*.yml run `--require-version N` first so a stale copy of
# this script fails in one second with a clear message instead of ten minutes
# into a CMake configure with baffling errors.
BUILD_SCRIPT_VERSION = 11

# ---------------------------------------------------------------------------
# Build profiles
# ---------------------------------------------------------------------------
#
#   minimal      size-optimised library package: libwinpr3, libfreerdp3,
#                libfreerdp-client3, libfreerdp-server3 with every channel
#                that needs no external library. MinSizeRel, stripped, no
#                executables, no FFmpeg/OpenH264/libusb, no shadow/proxy.
#                This is what the Python wheel ships.
#
#   full         feature-complete: everything minimal has, plus FFmpeg +
#                OpenH264 (H.264 encode/decode, AAC/Opus DSP, swscale),
#                libusb (urbdrc USB redirection; rdpecam camera on Linux),
#                the proxy and sample servers, winpr tools, the platform
#                client(s), and - where upstream supports it - the shadow
#                server. Release build. Executables are staged in _bin/.
#
#   client-only / server-only   library-only variants of minimal.
#
# What "full" can include differs per platform; the table below is the
# single source of truth (facts from FreeRDP's own CMake):
#
#   shadow     server/CMakeLists.txt: "Mac shadow server implementation no
#              longer compiles" -> never on macOS; upstream's Windows CI
#              builds with WITH_SHADOW=OFF -> off on Windows; Linux only.
#   proxy      portable (needs cJSON for config) -> desktop hosts.
#   sample     portable -> desktop hosts (it is the loopback test server).
#   client     xfreerdp/wlfreerdp (Linux, X11/Wayland), wfreerdp (Windows),
#              sdl-freerdp (any desktop with SDL3 + SDL3_ttf; probed).
#   mobile     Android/iOS get libraries only: no executables can run there,
#              proxy/shadow make no sense; media + channels are included.

FULL_PLATFORM = {
    #            shadow proxy  sample tools
    "Linux":    (True,  True,  True,  True),
    "Darwin":   (False, True,  True,  True),
    "Windows":  (False, True,  True,  True),
    "Android":  (False, False, False, False),
    "iOS":      (False, False, False, False),
}


def profile_options(profile, host_os, sdl=False):
    """-D switches that distinguish minimal from full (beyond channels)."""
    shadow, proxy, sample, tools = FULL_PLATFORM.get(
        host_os, (False, False, False, False))
    want_client, want_server = PROFILE_SIDES[profile]
    if profile == "full":
        opts = [
            "-DCMAKE_BUILD_TYPE=Release",
            "-DWITH_SHADOW={0}".format("ON" if shadow and want_server else "OFF"),
            "-DWITH_PROXY={0}".format("ON" if proxy and want_server else "OFF"),
            "-DWITH_PROXY_MODULES={0}".format(
                "ON" if proxy and want_server else "OFF"),
            "-DWITH_SAMPLE={0}".format("ON" if sample and want_server else "OFF"),
            "-DWITH_WINPR_TOOLS={0}".format("ON" if tools else "OFF"),
            "-DWITH_VERBOSE_WINPR_ASSERT=OFF",
        ]
        if want_client and host_os == "Windows":
            # wfreerdp.exe links wfreerdp-client3.dll (add_library follows
            # BUILD_SHARED_LIBS), but client/Windows/CMakeLists.txt only
            # installs that DLL when WITH_CLIENT_INTERFACE is ON. Without it
            # the executable ships without its own library
            # (STATUS_DLL_NOT_FOUND: wfreerdp-client3.dll).
            opts += ["-DWITH_CLIENT_WINDOWS=ON", "-DWITH_CLIENT_INTERFACE=ON"]
        if want_client and host_os in ("Linux", "Darwin", "Windows"):
            opts.append("-DWITH_CLIENT_SDL={0}".format("ON" if sdl else "OFF"))
        return opts
    # minimal / client-only / server-only: small.
    return [
        "-DCMAKE_BUILD_TYPE=MinSizeRel",
        "-DWITH_CLIENT_WINDOWS=OFF",
        "-DWITH_SHADOW=OFF", "-DWITH_PROXY=OFF", "-DWITH_PROXY_MODULES=OFF",
        "-DWITH_SAMPLE=OFF", "-DWITH_WINPR_TOOLS=OFF", "-DWITH_MANPAGES=OFF",
        "-DWITH_VERBOSE_WINPR_ASSERT=OFF", "-DWITH_CLIENT_SDL=OFF",
        "-DBUILD_TESTING=OFF",
    ]


def expected_libs(profile, host_os):
    """Library families that must exist after the build, per platform."""
    libs = list(EXPECTED_LIBS[profile])
    shadow = FULL_PLATFORM.get(host_os, (False,))[0]
    if profile == "full" and not shadow:
        libs = [l for l in libs if l != "freerdp-shadow"]
    return libs


def sdl_available(host_os, deps_prefix=None):
    """SDL3 + SDL3_ttf present? (pkg-config on Unix, vcpkg tree on Windows)"""
    if host_os == "Windows":
        if not deps_prefix:
            return False
        return bool(glob.glob(os.path.join(deps_prefix, "lib", "SDL3*.lib"))
                    and glob.glob(os.path.join(deps_prefix, "lib", "SDL3_ttf*.lib")))
    return _pkg_config_has("sdl3") and _pkg_config_has("sdl3-ttf")


# ---------------------------------------------------------------------------
# Kerberos policy
# ---------------------------------------------------------------------------
#
# FreeRDP implements Kerberos through winpr's SSPI. Where it comes from:
#
#   Linux     MIT krb5 (or Heimdal) via -DWITH_KRB5=ON. FreeRDP's own default
#             and what upstream CI builds. Enabled in BOTH profiles: the
#             library size cost is nil (winpr links the system libkrb5). The
#             full profile bundles libkrb5/libk5crypto/libcom_err/
#             libkrb5support into _libs; minimal relies on the distro's
#             krb5-libs (installed by default on all mainstream distros).
#   Windows   the OS: winpr uses native SSPI (WITH_NATIVE_SSPI is forced ON
#             for WIN32), so Kerberos/Negotiate come from secur32.dll.
#             Nothing to build or ship.
#   macOS     OFF by default. FreeRDP's FindKRB5 rejects the system Kerberos
#             ("Apple MITKerberosShim is deprecated and not supported") and
#             upstream's macOS CI builds with WITH_KRB5=OFF. Homebrew's MIT
#             krb5 is usable but not upstream-verified: opt in with
#             --with-krb5 (needs `brew install krb5`; the dylibs are bundled).
#   Android   OFF - upstream CI sets WITH_KRB5=OFF; no supported krb5 build.
#   iOS       OFF - same.

KRB5_RUNTIME_LIBS = ("libkrb5.", "libk5crypto.", "libcom_err.",
                     "libkrb5support.", "libgssapi_krb5.")


def krb5_options(profile, host_os, target="host", with_krb5=None):
    """
    Return (-D switches, list of dirs holding the krb5 runtime libs to bundle
    or None). with_krb5 True/False overrides the policy; None applies it.
    """
    if target in ("android", "ios") or host_os in ("Android", "iOS"):
        if with_krb5:
            raise SystemExit("Kerberos (WITH_KRB5) is not supported on "
                             "Android/iOS builds of FreeRDP")
        return ["-DWITH_KRB5=OFF"], None
    if host_os == "Windows":
        # Native SSPI: Kerberos is provided by Windows itself.
        return [], None
    if host_os == "Darwin":
        if not with_krb5:
            return ["-DWITH_KRB5=OFF"], None
        cfg = None
        if have("brew"):
            try:
                pfx = subprocess.check_output(["brew", "--prefix", "krb5"],
                                              stderr=subprocess.DEVNULL
                                              ).decode().strip()
                if os.path.isfile(os.path.join(pfx, "bin", "krb5-config")):
                    cfg = os.path.join(pfx, "bin", "krb5-config")
            except (OSError, subprocess.CalledProcessError):
                pass
        if not cfg:
            raise SystemExit("--with-krb5 on macOS needs Homebrew MIT "
                             "Kerberos: brew install krb5")
        libdir = os.path.join(os.path.dirname(os.path.dirname(cfg)), "lib")
        return ["-DWITH_KRB5=ON", "-DKRB5_ROOT_CONFIG={0}".format(cfg)], [libdir]
    # Linux
    if with_krb5 is False:
        return ["-DWITH_KRB5=OFF"], None
    if not have("krb5-config"):
        if profile == "full" or with_krb5:
            raise SystemExit(
                "Kerberos is part of the full profile on Linux but "
                "krb5-config was not found. Install libkrb5-dev / krb5-devel, "
                "or pass --without-krb5.")
        print("\n[warn] krb5-config not found - minimal build without "
              "Kerberos (WITH_KRB5=OFF). NLA still works via NTLM.")
        return ["-DWITH_KRB5=OFF"], None
    libdirs = None
    if profile == "full":
        # Where the distro keeps the krb5 runtime libs (for bundling into
        # _libs): krb5-config's -L dir, its parent (Debian puts dev links in
        # mit-krb5/ but the .so.N runtime files one level up), and the usual
        # multiarch/lib64 dirs.
        libdirs = []
        try:
            out = subprocess.check_output(["krb5-config", "--libs"]).decode()
            for t in out.split():
                if t.startswith("-L"):
                    libdirs += [t[2:], os.path.dirname(t[2:])]
        except (OSError, subprocess.CalledProcessError):
            pass
        libdirs += ["/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu",
                    "/usr/lib64", "/lib64", "/usr/lib"]
        libdirs = [d for i, d in enumerate(libdirs)
                   if os.path.isdir(d) and d not in libdirs[:i]]
    return ["-DWITH_KRB5=ON"], libdirs


# Which flags a profile wants at all.
PROFILE_SIDES = {
    "full":        (True, True),
    "client-only": (True, False),
    "server-only": (False, True),
    "minimal":     (True, True),
}


def _norm_set(names):
    return set(n.strip().lower() for n in (names or []) if n.strip())


def resolve_channels(profile, host_os, enable=None, disable=None):
    """
    Decide, for every channel, whether its client and server halves are
    built. Returns an ordered list of
        (name, channel_on, client_on, server_on)
    tuples. Unknown names given via enable/disable are appended with both
    halves set according to the profile so new upstream channels can be
    used without editing CHANNELS.
    """
    enable = _norm_set(enable)
    disable = _norm_set(disable)
    clash = enable & disable
    if clash:
        raise SystemExit(
            "Channels listed as both --enable-channel and "
            "--disable-channel: {0}".format(sorted(clash)))

    want_client, want_server = PROFILE_SIDES[profile]
    result = []
    for name, spec in CHANNELS.items():
        if name in disable:
            result.append((name, False, False, False))
            continue
        plats = spec.get("platforms")
        if plats and host_os not in plats and name not in enable:
            result.append((name, False, False, False))
            continue
        forced = name in enable
        on = spec["default"] or forced
        client = (want_client and spec["client"] and on
                  and (forced or spec.get("client_default", True)))
        server = (want_server and spec["server"] and on
                  and (forced or spec.get("server_default", True)))
        channel_on = client or server
        result.append((name, channel_on, client, server))

    known = set(CHANNELS)
    for name in sorted(enable - known):
        result.append((name, True, want_client, want_server))
    for name in sorted(disable - known):
        result.append((name, False, False, False))
    return result


def channel_options(profile, host_os, enable=None, disable=None,
                    channels_enabled=True):
    """
    Return the -D switches that select which FreeRDP channels get built.
    """
    want_client, want_server = PROFILE_SIDES[profile]
    if not channels_enabled:
        return ["-DWITH_CHANNELS=OFF",
                "-DWITH_CLIENT_CHANNELS=OFF",
                "-DWITH_SERVER_CHANNELS=OFF"]

    opts = [
        "-DWITH_CHANNELS=ON",
        "-DWITH_CLIENT_CHANNELS={0}".format("ON" if want_client else "OFF"),
        "-DWITH_SERVER_CHANNELS={0}".format("ON" if want_server else "OFF"),
    ]
    extra = []
    for name, on, client, server in resolve_channels(profile, host_os,
                                                     enable, disable):
        up = name.upper()
        opts.append("-DCHANNEL_{0}={1}".format(up, "ON" if on else "OFF"))
        if on:
            opts.append("-DCHANNEL_{0}_CLIENT={1}".format(
                up, "ON" if client else "OFF"))
            opts.append("-DCHANNEL_{0}_SERVER={1}".format(
                up, "ON" if server else "OFF"))
            if client:
                extra += CHANNELS.get(name, {}).get("extra", [])
    return opts + extra


def format_channel_table(profile, host_os, enable=None, disable=None):
    rows = resolve_channels(profile, host_os, enable, disable)
    lines = ["{0:<10} {1:<7} {2:<6} {3:<6}  {4}".format(
        "channel", "type", "client", "server", "notes")]
    for name, on, client, server in rows:
        spec = CHANNELS.get(name, {})
        lines.append("{0:<10} {1:<7} {2:<6} {3:<6}  {4}".format(
            name, spec.get("type", "?"),
            "ON" if client else ("-" if not spec.get("client", True) else "off"),
            "ON" if server else ("-" if not spec.get("server", True) else "off"),
            spec.get("note", "")))
    return "\n".join(lines)


def expected_channel_cache(profile, host_os, enable=None, disable=None):
    """CMakeCache keys -> expected 'ON'/'OFF', for post-configure checks."""
    expected = {}
    for name, on, client, server in resolve_channels(profile, host_os,
                                                     enable, disable):
        up = name.upper()
        expected["CHANNEL_{0}".format(up)] = "ON" if on else "OFF"
        if on:
            expected["CHANNEL_{0}_CLIENT".format(up)] = "ON" if client else "OFF"
            expected["CHANNEL_{0}_SERVER".format(up)] = "ON" if server else "OFF"
    return expected


def verify_channel_cache(build_dir, expected):
    """
    Read CMakeCache.txt after configure and make sure every CHANNEL_*
    switch we asked for actually stuck. cmake_dependent_option() will
    silently flip a channel OFF if its parent (drdynvc, rdpdr, WITH_SERVER
    ...) is off, and a missing dependency can do the same, so trusting the
    command line alone isn't enough.
    """
    cache = os.path.join(build_dir, "CMakeCache.txt")
    if not os.path.isfile(cache):
        print("[verify] no CMakeCache.txt at {0}; skipping channel "
              "check".format(cache))
        return
    actual = {}
    with open(cache) as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("CHANNEL_") or ":" not in line:
                continue
            key, _, rest = line.partition(":")
            _, _, val = rest.partition("=")
            actual[key] = val.strip().upper()

    problems = []
    for key, want in sorted(expected.items()):
        got = actual.get(key)
        if got is None:
            # Not in cache at all: unknown channel name or option not
            # declared in this FreeRDP version.
            if want == "ON":
                problems.append("{0}: not a known option in this FreeRDP "
                                "version".format(key))
            continue
        if (got in ("ON", "TRUE", "1")) != (want == "ON"):
            problems.append("{0}: wanted {1}, CMake resolved {2}".format(
                key, want, got))
    if problems:
        raise SystemExit(
            "\nChannel configuration mismatch after configure:\n  "
            + "\n  ".join(problems)
            + "\nCheck the configure output above for 'Could NOT find' "
              "lines, or drop the channel with --disable-channel.")
    on_client = sorted(k[len("CHANNEL_"):-len("_CLIENT")].lower()
                       for k, v in actual.items()
                       if k.endswith("_CLIENT") and v == "ON")
    on_server = sorted(k[len("CHANNEL_"):-len("_SERVER")].lower()
                       for k, v in actual.items()
                       if k.endswith("_SERVER") and v == "ON")
    print("[verify] client channels: {0}".format(
        ", ".join(on_client) or "none"))
    print("[verify] server channels: {0}".format(
        ", ".join(on_server) or "none"))


# CMake variables that are ;-separated lists: several sources (OpenSSL
# prefix, deps prefix) may each contribute, so merge rather than override.
_LIST_DEFINES = ("CMAKE_PREFIX_PATH", "CMAKE_FIND_ROOT_PATH",
                 "CMAKE_MODULE_PATH")


def dedupe_defines(opts):
    """
    For -DKEY=VALUE entries keep only the last occurrence of each KEY (a
    channel's 'extra' flags may override something in the common list,
    e.g. WITH_CUPS). List-type variables (_LIST_DEFINES) are merged in
    order with ';' instead. Non -D entries are kept as is, in order.
    """
    def key_of(o):
        k = o[2:].split("=", 1)[0]
        return k.split(":", 1)[0]  # strip ":TYPE"

    merged = {}
    last = {}
    for i, o in enumerate(opts):
        if o.startswith("-D") and "=" in o:
            k = key_of(o)
            if k in _LIST_DEFINES:
                vals = merged.setdefault(k, [])
                for v in o.split("=", 1)[1].split(";"):
                    if v and v not in vals:
                        vals.append(v)
            last[k] = i
    out = []
    emitted = set()
    for i, o in enumerate(opts):
        if o.startswith("-D") and "=" in o:
            k = key_of(o)
            if k in _LIST_DEFINES:
                if k in emitted:
                    continue
                emitted.add(k)
                out.append("-D{0}={1}".format(k, ";".join(merged[k])))
                continue
            if last[k] != i:
                continue
        out.append(o)
    return out


EDITIONS = ("standard", "ffmpeg", "openh264", "media")


def deps_has_ffmpeg(deps_prefix):
    return bool(deps_prefix) and deps_has(deps_prefix, "libavcodec")


def deps_has_openh264(deps_prefix):
    return bool(deps_prefix) and deps_has(deps_prefix, "openh264")


def deps_media_available(deps_prefix):
    """Any media component present (FFmpeg and/or OpenH264)?"""
    return deps_has_ffmpeg(deps_prefix) or deps_has_openh264(deps_prefix)


def media_options(deps_prefix, host_os):
    """
    -D switches for whatever the build/deps/<label>-<edition> prefix (from
    scripts/build_deps.py) provides. FFmpeg and OpenH264 are switched
    independently so the ffmpeg / openh264 / media editions all work from
    the same code path:

      FFmpeg present   -> WITH_FFMPEG, WITH_DSP_FFMPEG (AAC/Opus), WITH_SWSCALE
                          (scaler) instead of cairo
      OpenH264 present -> WITH_OPENH264 (H.264 encode + decode)
      neither          -> only CMAKE_PREFIX_PATH (Windows: OpenSSL/zlib/cJSON)
    """
    if not deps_prefix:
        return []
    opts = ["-DCMAKE_PREFIX_PATH={0}".format(deps_prefix)]
    if deps_has_ffmpeg(deps_prefix):
        opts += ["-DWITH_FFMPEG=ON", "-DWITH_DSP_FFMPEG=ON",
                 "-DWITH_SWSCALE=ON", "-DWITH_CAIRO=OFF"]
    else:
        opts += ["-DWITH_FFMPEG=OFF", "-DWITH_DSP_FFMPEG=OFF",
                 "-DWITH_SWSCALE=OFF"]
    if deps_has_openh264(deps_prefix):
        opts += ["-DWITH_OPENH264=ON", "-DWITH_OPENH264_LOADING=OFF"]
        if host_os == "Windows":
            # vcpkg's openh264 port installs openh264.lib (not openh264_dll).
            opts.append("-DOPENH264_ROOT={0}".format(deps_prefix))
    else:
        opts.append("-DWITH_OPENH264=OFF")
    return opts


def deps_has(deps_prefix, pc_name):
    """True if <deps_prefix> carries a pkg-config file for pc_name."""
    if not deps_prefix:
        return False
    for sub in ("lib", "lib64"):
        if os.path.isfile(os.path.join(deps_prefix, sub, "pkgconfig",
                                       pc_name + ".pc")):
            return True
    # vcpkg flattens to lib/pkgconfig too, but check the .lib as fallback.
    return bool(glob.glob(os.path.join(deps_prefix, "lib",
                                       "*" + pc_name.split("-")[0] + "*")))


def media_channels(deps_prefix, host_os, target="host"):
    """Extra channels to enable when the deps prefix provides their libs."""
    if not deps_prefix:
        return []
    chans = []
    if deps_has(deps_prefix, "libusb-1.0") and target != "ios":
        chans.append("urbdrc")
    # rdpecam client: needs swscale and a capture backend; only Linux has
    # one (V4L) in FreeRDP 3.16. Server side is always on.
    if host_os == "Linux" and target == "host" and deps_has_ffmpeg(deps_prefix):
        chans.append("rdpecam")
    return chans


def cmake_options_for(profile, host_os, enable_channels=None,
                      disable_channels=None, channels_enabled=True,
                      deps_prefix=None, executables=False, sdl=False):
    """
    Return the list of -D CMake options for the given build profile.

    Flags here mirror upstream FreeRDP's documented switches (top-level
    CMakeLists.txt + cmake/ConfigOptions.cmake). We force-disable the
    heavy media stack (FFmpeg, X264, OpenH264, GStreamer) by default to
    keep the build fast and reproducible; users who want H.264 server
    output can pass extra flags via PYFREERDP_EXTRA_CMAKE.
    """
    common = [
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_SHARED_LIBS=ON",
        "-DWITH_MANPAGES=OFF",
        "-DWITH_SAMPLE=OFF",
        # OpenSSL is required by both halves for TLS / NLA / CredSSP.
        "-DWITH_OPENSSL=ON",
        # Heavy optional codecs off by default - see docstring.
        "-DWITH_FFMPEG=OFF",
        "-DWITH_DSP_FFMPEG=OFF",
        # WITH_SWSCALE defaults ON and does find_package(FFmpeg REQUIRED)
        # regardless of WITH_FFMPEG, which would abort configure. Use Cairo
        # for image scaling instead (host_dependency_probe() drops it if
        # cairo isn't installed - then there's no scaling at all).
        "-DWITH_SWSCALE=OFF",
        "-DWITH_CAIRO=ON",
        "-DWITH_X264=OFF",
        "-DWITH_OPENH264=OFF",
        "-DWITH_GSTREAMER_1_0=OFF",
        "-DWITH_PULSE=OFF",
        # cliprdr's file-transfer path on Linux wants FUSE3. Keep it off so
        # the clipboard channel builds without libfuse3-dev; text/image
        # clipboard still works. Re-enable via PYFREERDP_EXTRA_CMAKE.
        "-DWITH_FUSE=OFF",
    ]

    want_client, want_server = PROFILE_SIDES[profile]
    opts = [
        "-DWITH_CLIENT={0}".format("ON" if want_client else "OFF"),
        "-DWITH_CLIENT_COMMON={0}".format("ON" if want_client else "OFF"),
        "-DWITH_SERVER={0}".format("ON" if want_server else "OFF"),
    ]
    opts += profile_options(profile, host_os, sdl=sdl)

    if host_os == "Linux":
        # X11/Wayland only matter for the xfreerdp/wlfreerdp executables;
        # the minimal library package skips them (and their -dev packages).
        gui = "ON" if profile == "full" else "OFF"
        opts += [
            "-DWITH_X11={0}".format(gui),
            "-DWITH_WAYLAND={0}".format(gui),
            "-DWITH_ALSA=ON",
            "-DWITH_CUPS=OFF",
            "-DWITH_PCSC=OFF",
        ]
    elif host_os == "Darwin":
        opts += [
            "-DWITH_X11=OFF", "-DWITH_WAYLAND=OFF", "-DWITH_ALSA=OFF",
            # NTLM needs MD4/RC4, which OpenSSL 3 only offers via its
            # "legacy" provider module - found through a path compiled into
            # the libcrypto we ship, i.e. not on the user's machine. winpr's
            # own implementations remove that runtime dependency.
            "-DWITH_INTERNAL_MD4=ON", "-DWITH_INTERNAL_MD5=ON",
            "-DWITH_INTERNAL_RC4=ON",
            # Upstream: "Mac platform server implementation no longer
            # compiles". The GUI clients aren't needed for a binding.
            "-DWITH_PLATFORM_SERVER=OFF",
            "-DWITH_CLIENT_MAC=OFF",   # Xcode/Cocoa app; sdl-freerdp instead
            # Relocatable dylibs: install names use @rpath so the files
            # can be moved into pyfreerdp/_libs and bundled by delocate.
            "-DCMAKE_INSTALL_NAME_DIR=@rpath",
            "-DCMAKE_OSX_DEPLOYMENT_TARGET=11.0",
        ]
    elif host_os == "Windows":
        opts += [
            "-DWITH_X11=OFF", "-DWITH_WAYLAND=OFF",
            "-DWITH_INTERNAL_MD4=ON", "-DWITH_INTERNAL_MD5=ON",   # see Darwin
            "-DWITH_INTERNAL_RC4=ON",
            # No cairo in the default vcpkg set (it is a very long build);
            # scaling is unavailable unless you add it via
            # PYFREERDP_EXTRA_CMAKE.
            "-DWITH_CAIRO=OFF",
            "-DWITH_PLATFORM_SERVER=OFF",
        ]
    # (WITH_CLIENT_WINDOWS / WITH_CLIENT_SDL are decided by profile_options.)

    if deps_prefix:
        enable_channels = list(enable_channels or []) + media_channels(
            deps_prefix, host_os)
        opts += media_options(deps_prefix, host_os)
    if executables:
        opts += executable_options(profile, host_os)

    opts += channel_options(profile, host_os, enable_channels,
                            disable_channels, channels_enabled)

    return dedupe_defines(common + opts)


def executable_options(profile, host_os):
    """
    Turn on the executables that build cleanly on each platform:
      * winpr-makecert / winpr-hash   (WITH_WINPR_TOOLS, all platforms)
      * sfreerdp-server               (WITH_SAMPLE, all platforms)
      * xfreerdp / wlfreerdp          (Linux, via WITH_X11 / WITH_WAYLAND
                                       in the full profile)
      * wfreerdp                      (Windows)
      * freerdp-shadow-cli, freerdp-proxy (full profile only)
    """
    want_client, want_server = PROFILE_SIDES[profile]
    opts = ["-DWITH_WINPR_TOOLS=ON"]
    if want_server and host_os in ("Linux", "Darwin", "Windows"):
        opts.append("-DWITH_SAMPLE=ON")
    if want_client and host_os == "Windows":
        opts.append("-DWITH_CLIENT_WINDOWS=ON")
    return opts


# ---------------------------------------------------------------------------
# Host build (Linux / macOS / Windows native)
# ---------------------------------------------------------------------------

# Windows target architectures: CLI name -> (Visual Studio -A value,
# vcpkg triplet, PE machine type as reported by dumpbin / pefile).
WINDOWS_ARCHS = {
    "x64":   ("x64",   "x64-windows",   "x64"),
    "x86":   ("Win32", "x86-windows",   "x86"),
    "arm64": ("ARM64", "arm64-windows", "arm64"),
}


def host_windows_arch(arch="host"):
    """Normalise --arch for Windows builds; 'host' picks the runner's CPU."""
    if arch in (None, "", "host"):
        m = platform.machine().upper()
        if m == "ARM64":
            return "arm64"
        if m in ("X86", "I386", "I686"):
            return "x86"
        return "x64"
    if arch not in WINDOWS_ARCHS:
        raise SystemExit("Unknown --arch {0}; pick one of {1}".format(
            arch, sorted(WINDOWS_ARCHS)))
    return arch


def host_dependency_probe(host_os, arch="host", deps_prefix=None):
    """
    Look for optional-but-default-ON FreeRDP dependencies on the host and
    turn the matching feature off with a loud warning when they're absent,
    instead of letting CMake abort halfway through configure.

    Two things are handled this way, both of which find_package(REQUIRED)
    in 3.x and abort configure when absent:

      * Kerberos  (WITH_KRB5, default ON)        -> WITH_KRB5=OFF
      * ICU       (winpr unicode layer on Linux) -> WITH_UNICODE_BUILTIN=ON
      * Cairo     (image scaler we pick instead
                   of FFmpeg swscale)             -> WITH_CAIRO=OFF

    Everything else FreeRDP wants by default either ships with the
    toolchain or degrades gracefully with a CMake warning.
    """
    opts = []
    # (Kerberos is decided by krb5_options(), not probed here.)
    if host_os == "Darwin":
        if not os.environ.get("OPENSSL_ROOT_DIR") and have("brew"):
            # Homebrew's openssl@3 is keg-only, so CMake won't find it
            # without a hint.
            try:
                pfx = subprocess.check_output(
                    ["brew", "--prefix", "openssl@3"],
                    stderr=subprocess.DEVNULL).decode().strip()
            except (OSError, subprocess.CalledProcessError):
                pfx = ""
            if pfx and os.path.isdir(pfx):
                opts.append("-DOPENSSL_ROOT_DIR={0}".format(pfx))
    if host_os == "Windows":
        extra = os.environ.get("PYFREERDP_EXTRA_CMAKE", "")
        vcpkg = (os.environ.get("VCPKG_ROOT")
                 or os.environ.get("VCPKG_INSTALLATION_ROOT"))
        if vcpkg and "CMAKE_TOOLCHAIN_FILE" not in extra:
            tc = os.path.join(vcpkg, "scripts", "buildsystems", "vcpkg.cmake")
            if os.path.isfile(tc):
                triplet = os.environ.get(
                    "VCPKG_DEFAULT_TRIPLET",
                    WINDOWS_ARCHS[host_windows_arch(arch)][1])
                opts += ["-DCMAKE_TOOLCHAIN_FILE={0}".format(tc),
                         "-DVCPKG_TARGET_TRIPLET={0}".format(triplet)]
                print("[probe] using vcpkg toolchain {0} ({1})".format(
                    tc, triplet))
                # build_deps.py installs the manifest into
                # <deps>/vcpkg_installed; point the toolchain at it instead
                # of $VCPKG_ROOT/installed and keep it from re-running the
                # manifest against FreeRDP's own source tree.
                inst = os.path.join(deps_prefix or "", "vcpkg_installed")
                if deps_prefix and os.path.isdir(inst):
                    opts += ["-DVCPKG_INSTALLED_DIR={0}".format(inst),
                             "-DVCPKG_MANIFEST_MODE=OFF"]
    if host_os in ("Linux", "Darwin") and not _pkg_config_has("cairo"):
        print("\n[warn] cairo.pc not found - building with WITH_CAIRO=OFF. "
              "Screen scaling (SmartSizing) will be unavailable. Install "
              "libcairo2-dev / cairo-devel to enable it.")
        opts.append("-DWITH_CAIRO=OFF")
    if host_os == "Linux" and not _pkg_config_has("icu-uc"):
        print("\n[warn] ICU (icu-uc.pc) not found - building with "
              "WITH_UNICODE_BUILTIN=ON (winpr's own UTF-16 conversion). "
              "Install libicu-dev / libicu-devel to use system ICU.")
        opts.append("-DWITH_UNICODE_BUILTIN=ON")
    return opts


def _pkg_config_has(module):
    if not have("pkg-config"):
        return False
    return subprocess.run(["pkg-config", "--exists", module],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def deps_env(deps_prefix, cross=False):
    """Environment additions so pkg-config finds the deps prefix."""
    env = dict(os.environ)
    if not deps_prefix:
        return env
    pcs = [os.path.join(deps_prefix, "lib", "pkgconfig"),
           os.path.join(deps_prefix, "lib64", "pkgconfig")]
    key = "PKG_CONFIG_LIBDIR" if cross else "PKG_CONFIG_PATH"
    old = env.get(key)
    env[key] = os.pathsep.join(pcs + ([old] if old and not cross else []))
    if cross:
        env["PKG_CONFIG_SYSROOT_DIR"] = ""
    return env


def build_host(src, prefix, jobs, profile, enable_channels=None,
               disable_channels=None, channels_enabled=True, arch="host",
               deps_prefix=None, executables=False, with_krb5=None):
    require_tools(["cmake", "git"])
    host_os = platform.system()
    if host_os != "Windows" and arch not in (None, "", "host"):
        raise SystemExit("--arch is only supported for Windows host builds "
                         "(use --target android/ios for mobile).")
    if deps_prefix and not os.path.isdir(deps_prefix):
        raise SystemExit("--deps-prefix {0} does not exist; run "
                         "scripts/build_deps.py first".format(deps_prefix))
    build_dir = os.path.join(src, "build")
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    os.makedirs(build_dir)

    sdl = profile == "full" and sdl_available(host_os, deps_prefix)
    if profile == "full":
        print("[build] SDL3 client: {0}".format(
            "yes" if sdl else "no (SDL3 + SDL3_ttf not found)"))
    opts = cmake_options_for(profile, host_os=host_os,
                             enable_channels=enable_channels,
                             disable_channels=disable_channels,
                             channels_enabled=channels_enabled,
                             deps_prefix=deps_prefix, executables=executables,
                             sdl=sdl)
    opts += host_dependency_probe(host_os, arch, deps_prefix)
    krb_opts, krb_libdir = krb5_options(profile, host_os, "host", with_krb5)
    opts += krb_opts
    print("[build] Kerberos: {0}".format(
        "native SSPI (Windows)" if host_os == "Windows" else
        ("ON" if "-DWITH_KRB5=ON" in krb_opts else "OFF")))
    build_host.krb5_libdir = krb_libdir
    if deps_has_ffmpeg(deps_prefix):
        # cairo probe is irrelevant when swscale is used
        opts = [o for o in opts if not o.startswith("-DWITH_CAIRO=")]
        opts.append("-DWITH_CAIRO=OFF")
    opts = dedupe_defines(opts)
    extra = os.environ.get("PYFREERDP_EXTRA_CMAKE", "").split()
    env = deps_env(deps_prefix)

    cfg = ["cmake", "-S", src, "-B", build_dir,
           "-DCMAKE_INSTALL_PREFIX={0}".format(prefix)] + opts + extra
    if any(o in ("-G", "-A") or o.startswith(("-G", "-A")) for o in extra):
        pass  # caller picked a generator via PYFREERDP_EXTRA_CMAKE
    elif host_os == "Windows":
        # Visual Studio generator: works without a vcvars shell, unlike
        # Ninja+cl, and cross-compiles x86 / ARM64 from an x64 host.
        vs_arch = WINDOWS_ARCHS[host_windows_arch(arch)][0]
        print("[build] Windows target: {0} (-A {1})".format(
            host_windows_arch(arch), vs_arch))
        cfg += ["-A", vs_arch]
    elif have("ninja"):
        cfg += ["-G", "Ninja"]

    run(cfg, env=env)
    if channels_enabled:
        verify_channel_cache(build_dir, expected_channel_cache(
            profile, host_os,
            list(enable_channels or []) + media_channels(deps_prefix, host_os),
            disable_channels))
    if deps_media_available(deps_prefix):
        verify_media_cache(build_dir, deps_prefix)
    if host_os != "Windows":
        verify_krb5_cache(build_dir, "-DWITH_KRB5=ON" in krb_opts)
    config = "Release" if profile == "full" else "MinSizeRel"
    run(["cmake", "--build", build_dir, "--config", config,
         "--parallel", str(jobs)], env=env)
    install = ["cmake", "--install", build_dir, "--config", config]
    if profile != "full" and host_os != "Windows":
        install.append("--strip")   # size: drop symbol tables
    run(install, env=env)
    if executables and host_os == "Windows" and PROFILE_SIDES[profile][0]:
        build_sample_client(src, prefix, host_os, arch, deps_prefix, jobs,
                            [o for o in opts if o.startswith(
                                ("-DCMAKE_TOOLCHAIN_FILE=", "-DVCPKG_",
                                 "-DCMAKE_PREFIX_PATH="))],
                            env)
    return prefix


def build_sample_client(src, prefix, host_os, arch, deps_prefix, jobs,
                        toolchain_opts, env):
    """
    Build client/Sample (sfreerdp, the minimal reference client) as a
    standalone CMake project against the just-installed prefix.

    On Linux/macOS it is built in-tree by WITH_SAMPLE. On Windows upstream's
    client/CMakeLists.txt only adds it in the non-WIN32 branch (wfreerdp
    takes that slot), so we build it separately. client/Sample supports
    standalone builds via find_package(WinPR/FreeRDP/FreeRDP-Client), but
    3.16.0 forgets to include InstallFreeRDPDesktop.cmake, which is why
    CMAKE_PROJECT_sfreerdp_INCLUDE points at it.
    """
    sample_src = os.path.join(src, "client", "Sample")
    if not os.path.isdir(sample_src):
        print("[sample] client/Sample not present in this FreeRDP ref; skipping")
        return
    build_dir = os.path.join(src, "build-sample-client")
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    prefixes = [prefix] + ([deps_prefix] if deps_prefix else [])
    cfg = ["cmake", "-S", sample_src, "-B", build_dir,
           "-DCMAKE_BUILD_TYPE=Release",
           "-DCMAKE_INSTALL_PREFIX={0}".format(prefix),
           "-DCMAKE_PREFIX_PATH={0}".format(";".join(prefixes)),
           "-DCMAKE_PROJECT_sfreerdp_INCLUDE={0}".format(
               os.path.join(src, "cmake", "InstallFreeRDPDesktop.cmake"))]
    cfg += [o for o in toolchain_opts if not o.startswith("-DCMAKE_PREFIX_PATH=")]
    cfg = dedupe_defines(cfg)
    if host_os == "Windows":
        cfg += ["-A", WINDOWS_ARCHS[host_windows_arch(arch)][0]]
    elif have("ninja"):
        cfg += ["-G", "Ninja"]
    if host_os == "Linux" and deps_prefix:
        cfg.append("-DCMAKE_EXE_LINKER_FLAGS=-Wl,-rpath-link,{0}".format(
            os.path.join(deps_prefix, "lib")))
    print("[sample] building the sample client (sfreerdp) against {0}".format(prefix))
    run(cfg, env=env)
    run(["cmake", "--build", build_dir, "--config", "Release",
         "--parallel", str(jobs)], env=env)
    run(["cmake", "--install", build_dir, "--config", "Release"], env=env)


def verify_krb5_cache(build_dir, expect_on):
    """After configure: WITH_KRB5 resolved as intended (and a flavour found)."""
    vals = {}
    with open(os.path.join(build_dir, "CMakeCache.txt")) as fh:
        for line in fh:
            if ":" in line and "=" in line and not line.startswith(("#", "//")):
                k, _, rest = line.strip().partition(":")
                vals[k] = rest.partition("=")[2]
    on = vals.get("WITH_KRB5", "").upper() in ("ON", "TRUE", "1")
    if on != expect_on:
        raise SystemExit("[verify] WITH_KRB5 resolved {0}, expected {1}".format(
            vals.get("WITH_KRB5"), "ON" if expect_on else "OFF"))
    if on:
        flavour = vals.get("KRB5_FLAVOUR") or (
            "MIT" if vals.get("KRB5_MIT_FOUND", "").upper() in ("1", "TRUE")
            else "?")
        print("[verify] Kerberos: WITH_KRB5=ON ({0} {1})".format(
            flavour, vals.get("KRB5_VERSION", "")))
    else:
        print("[verify] Kerberos: WITH_KRB5=OFF")


def verify_media_cache(build_dir, deps_prefix=None):
    """After configure: the media components the prefix provides really got
    detected and enabled (and nothing else was silently turned on)."""
    cache = os.path.join(build_dir, "CMakeCache.txt")
    vals = {}
    with open(cache) as fh:
        for line in fh:
            if ":" in line and "=" in line and not line.startswith(("#", "//")):
                k, _, rest = line.strip().partition(":")
                vals[k] = rest.partition("=")[2]

    def on(k):
        return vals.get(k, "").upper() in ("ON", "TRUE", "1")

    def found(k):
        v = vals.get(k, "")
        return bool(v) and not v.endswith("-NOTFOUND")

    problems = []
    want_ff = deps_has_ffmpeg(deps_prefix)
    want_oh = deps_has_openh264(deps_prefix)
    for k in ("WITH_FFMPEG", "WITH_DSP_FFMPEG", "WITH_SWSCALE"):
        if on(k) != want_ff:
            problems.append("{0}={1} (expected {2})".format(
                k, vals.get(k), "ON" if want_ff else "OFF"))
    if want_ff:
        for k in ("AVCODEC_LIBRARIES", "SWSCALE_LIBRARIES"):
            if not found(k):
                problems.append("{0}=<missing>".format(k))
    if on("WITH_OPENH264") != want_oh:
        problems.append("WITH_OPENH264={0} (expected {1})".format(
            vals.get("WITH_OPENH264"), "ON" if want_oh else "OFF"))
    if want_oh and not found("OPENH264_LIBRARY"):
        problems.append("OPENH264_LIBRARY=<missing>")
    if problems:
        raise SystemExit("\nMedia dependencies not picked up by CMake: {0}\n"
                         "Check that --deps-prefix contains lib/pkgconfig/"
                         "{{libavcodec,libswscale,openh264}}.pc".format(problems))
    print("[verify] media: FFmpeg {0}, OpenH264 {1}".format(
        vals.get("AVCODEC_VERSION", "?") if want_ff else "off",
        vals.get("OPENH264_LIBRARY", "?") if want_oh else "off"))


# Library families we expect after install for each profile, used to
# verify the build actually produced what the user asked for.
EXPECTED_LIBS = {
    "full": ["freerdp", "freerdp-client", "freerdp-server",
             "freerdp-shadow", "winpr"],
    "client-only": ["freerdp", "freerdp-client", "winpr"],
    "server-only": ["freerdp", "freerdp-server", "freerdp-shadow", "winpr"],
    "minimal": ["freerdp", "freerdp-client", "freerdp-server", "winpr"],
}


# FreeRDP installs loadable modules (proxy plugins, SDL client helpers,
# ...) into <libdir>/freerdp<major>/. Channels themselves are compiled into
# libfreerdp-client/-server in 3.x and never show up here, but keep the
# subdirectory so anything that does land there is shipped with the same
# layout.
PLUGIN_SUBDIR_GLOB = "freerdp[0-9]*"


def _shared_lib_ext(sysname):
    if sysname == "Windows":
        return ".dll"
    if sysname == "Darwin":
        return ".dylib"
    return ".so"


# MSVC runtime / UCRT DLLs. FreeRDP's CMakeCPack.cmake installs these into
# <prefix>/bin via InstallRequiredSystemLibraries, and on a cross build it
# picks the *host* toolchain's copies (x64 vcruntime140_1.dll in an arm64
# install). They belong to the VC++ redistributable, not to us.
_MSVC_CRT_PREFIXES = ("msvcp", "vcruntime", "concrt", "vcomp", "vccorlib",
                      "mfc", "ucrtbase", "api-ms-", "ext-ms-")


def _is_msvc_crt_dll(filename):
    return filename.lower().startswith(_MSVC_CRT_PREFIXES)


def collect_host_artifacts(prefix):
    """
    Return a list of (path, relative_subdir) tuples.

    relative_subdir is "" for core libraries and e.g. "freerdp3" for
    loadable modules, so install_into_package() keeps the layout FreeRDP
    expects when it resolves addins.
    """
    sysname = platform.system()
    ext = _shared_lib_ext(sysname)
    candidates = []

    if sysname == "Windows":
        for p in glob.glob(os.path.join(prefix, "bin", "*.dll")):
            if _is_msvc_crt_dll(os.path.basename(p)):
                print("[collect] skipping CRT runtime {0}".format(
                    os.path.basename(p)))
                continue
            candidates.append((p, ""))
        # Modules on Windows land next to the exe or in bin/freerdp3/.
        bindir = os.path.join(prefix, "bin")
        for d in glob.glob(os.path.join(bindir, PLUGIN_SUBDIR_GLOB)):
            if not os.path.isdir(d):
                continue
            for root, _dirs, files in os.walk(d):
                rel = os.path.relpath(root, bindir)
                for fn in files:
                    if fn.lower().endswith(".dll") and not _is_msvc_crt_dll(fn):
                        candidates.append((os.path.join(root, fn), rel))
        return candidates

    for sub in ("lib", "lib64"):
        d = os.path.join(prefix, sub)
        if not os.path.isdir(d):
            continue
        for p in glob.glob(os.path.join(d, "*{0}*".format(ext))):
            if os.path.isfile(p) or os.path.islink(p):
                candidates.append((p, ""))
        for pd in glob.glob(os.path.join(d, PLUGIN_SUBDIR_GLOB)):
            if not os.path.isdir(pd):
                continue
            # Walk recursively: proxy modules sit in freerdp3/proxy/.
            for root, _dirs, files in os.walk(pd):
                rel = os.path.relpath(root, d)
                for fn in files:
                    if ext in fn:
                        candidates.append((os.path.join(root, fn), rel))
    return candidates


def verify_artifacts(artifacts, profile, host_os=None):
    """
    Assert that every library family the profile promised actually exists.
    Prevents shipping a wheel where CMake silently dropped server support
    because a dep was missing. (Channel presence is checked separately,
    right after configure, by verify_channel_cache().)
    """
    core_names = [os.path.basename(p).lower()
                  for p, sub in artifacts if not sub]
    want = expected_libs(profile, host_os or platform.system())
    missing = []
    for stem in want:
        if not any(stem in n for n in core_names):
            missing.append(stem)
    if missing:
        raise SystemExit(
            "\nBuild profile '{0}' promised these library families "
            "but they're missing from the install: {1}\n"
            "Found: {2}\n"
            "This usually means a CMake feature was silently disabled "
            "because a dependency was missing. Check the configure output "
            "above for 'Could NOT find ...' messages and install the "
            "corresponding -dev packages.".format(
                profile, missing, sorted(set(core_names))))
    print("[verify] core libraries present: {0}".format(want))

    if platform.system() == "Windows":
        machines = {}
        for p, _sub in artifacts:
            if p.lower().endswith(".dll"):
                machines.setdefault(pe_machine(p), []).append(
                    os.path.basename(p))
        if len(machines) > 1:
            raise SystemExit(
                "\nStaged DLLs have mixed architectures: {0}".format(
                    dict((hex(k), v) for k, v in machines.items())))
        print("[verify] all DLLs share PE machine type {0}".format(
            ", ".join(hex(m) for m in machines)))


def _ensure_patchelf():
    """
    Return a path to a working `patchelf`. Try PATH first, then install the
    PyPI wheel (which ships a static binary) into the running interpreter.
    """
    if have("patchelf"):
        return "patchelf"
    print("[rpath] patchelf not on PATH - installing the PyPI package")
    for extra in ([], ["--break-system-packages"], ["--user"]):
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--quiet",
                 "patchelf"] + extra,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            break
        except subprocess.CalledProcessError:
            continue
    import sysconfig
    for d in (sysconfig.get_path("scripts"),
              os.path.join(os.path.expanduser("~"), ".local", "bin")):
        cand = os.path.join(d or "", "patchelf")
        if d and os.path.isfile(cand):
            return cand
    if have("patchelf"):
        return "patchelf"
    raise SystemExit(
        "patchelf is required to make the staged .so files relocatable "
        "(RUNPATH=$ORIGIN). Install it with `pip install patchelf` or "
        "`apt-get install patchelf` and re-run.")


def _macho_rpaths(path):
    """LC_RPATH entries of a Mach-O file, exactly (not a substring search)."""
    out = subprocess.check_output(["otool", "-l", path]).decode(errors="replace")
    rpaths, in_rpath = [], False
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("cmd "):
            in_rpath = s == "cmd LC_RPATH"
        elif in_rpath and s.startswith("path "):
            rpaths.append(s.split("path ", 1)[1].rsplit(" (offset", 1)[0])
    return rpaths


def _macho_add_rpath(path, want, replace_prefix=None):
    """
    Add an LC_RPATH. If the header has no spare room ("can't be redone"),
    fall back to rewriting an existing entry starting with replace_prefix
    (e.g. FreeRDP's own @loader_path/../lib) to the wanted value.
    """
    have = _macho_rpaths(path)
    if want in have:
        return True
    r = subprocess.run(["install_name_tool", "-add_rpath", want, path],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode == 0:
        return True
    for old in have:
        if replace_prefix and old.startswith(replace_prefix):
            r2 = subprocess.run(["install_name_tool", "-rpath", old, want, path],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if r2.returncode == 0:
                return True
    print("[rpath] warning: could not set rpath {0} on {1}: {2}".format(
        want, path, r.stderr.decode(errors="replace").strip()[-200:]))
    return False


def _fix_rpath(out_dir):
    """
    FreeRDP installs its libraries with RUNPATH '$ORIGIN/../lib:$ORIGIN/..'
    (cmake/ConfigureRPATH.cmake forces CMAKE_INSTALL_RPATH as an INTERNAL
    cache entry, and CMake's install step strips any -rpath we add through
    linker flags). That is right inside an install prefix but wrong once
    the files sit flat in pyfreerdp/_libs/: dlopen('.../_libs/
    libfreerdp-client3.so.3') can't find libfreerdp3.so.3 next to it.

    Rewrite every staged shared object so it looks in its own directory
    (and, for modules in subdirectories, back up to the _libs root).
    """
    sysname = platform.system()
    if sysname == "Windows":
        return  # DLLs resolve siblings from their own directory already
    fixed = 0
    for root, _dirs, files in os.walk(out_dir):
        rel = os.path.relpath(out_dir, root)          # "." or "../.."
        for fn in files:
            path = os.path.join(root, fn)
            if os.path.islink(path):
                continue
            if sysname == "Linux":
                if ".so" not in fn:
                    continue
                rpath = "$ORIGIN" if rel == "." else "$ORIGIN:$ORIGIN/" + rel
                subprocess.check_call([_ensure_patchelf(), "--set-rpath",
                                       rpath, path])
                fixed += 1
            elif sysname == "Darwin":
                if not fn.endswith(".dylib") and ".so" not in fn:
                    continue
                want = ("@loader_path" if rel == "."
                        else "@loader_path/" + rel)
                _macho_add_rpath(path, want, replace_prefix="@loader_path/")
                fixed += 1
    print("[rpath] made {0} shared objects relocatable ({1})".format(
        fixed, "$ORIGIN" if sysname == "Linux" else "@loader_path"))


def verify_loadable(out_dir, profile, arch="host"):
    """
    Do exactly what pyfreerdp's tests do: ctypes-load the staged client and
    server libraries by absolute path, cold, in a fresh interpreter, without
    loading their dependencies first and without LD_LIBRARY_PATH.
    """
    sysname = platform.system()
    if sysname == "Windows" and host_windows_arch(arch) != \
            host_windows_arch("host"):
        print("[verify] cross-compiled Windows target; skipping load test")
        return
    want_client, want_server = PROFILE_SIDES[profile]
    stems = ["winpr3", "freerdp3"]
    if want_client:
        stems.append("freerdp-client3")
    if want_server:
        stems.append("freerdp-server3")
    ext = _shared_lib_ext(sysname)
    code = r'''
import ctypes, glob, os, sys
out, ext, stems = sys.argv[1], sys.argv[2], sys.argv[3:]
if sys.platform == "win32":
    os.add_dll_directory(out)
for stem in stems:
    cands = sorted(p for p in glob.glob(os.path.join(out, "*" + stem + "*"))
                   if ext in os.path.basename(p) and os.path.isfile(p))
    if not cands:
        sys.exit("no file for %s in %s" % (stem, out))
    path = cands[0]
    try:
        ctypes.CDLL(path)
    except OSError as e:
        sys.exit("FAILED to load %s: %s" % (path, e))
    print("[verify] loads cold: %s" % os.path.basename(path))
'''
    env = dict(os.environ)
    for k in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
              "DYLD_FALLBACK_LIBRARY_PATH"):
        env.pop(k, None)
    subprocess.check_call([sys.executable, "-c", code, out_dir, ext]
                          + stems, env=env)


# ---------------------------------------------------------------------------
# Windows: bundle the third-party DLLs the FreeRDP DLLs import
# ---------------------------------------------------------------------------

# Never copy these: the Universal CRT / VC++ runtime ship with Windows or
# the VC++ redistributable (and with Python itself), and vcpkg's per-triplet
# bin/ can even contain a wrong-architecture copy (x64 vcruntime140_1.dll in
# arm64-windows).
_WIN_SYSTEM_DLL_PREFIXES = ("api-ms-", "ext-ms-", "ucrtbase", "vcruntime",
                            "msvcp", "msvcr", "concrt", "kernel32", "user32",
                            "advapi32", "ws2_32", "crypt32", "secur32",
                            "ntdll", "ole32", "oleaut32", "shell32", "gdi32",
                            "winmm", "iphlpapi", "bcrypt", "ncrypt", "rpcrt4",
                            "shlwapi", "setupapi", "cfgmgr32", "dbghelp",
                            "winspool", "wtsapi32", "userenv", "credui",
                            "netapi32", "mpr", "version", "comdlg32",
                            "d3d11", "dxgi", "mf", "mfplat", "mfreadwrite",
                            "mfuuid", "strmiids", "ksuser", "avrt", "wsock32",
                            "python")


def pe_imports(path):
    """Return the DLL names imported by a PE file (pure-Python parser)."""
    import struct
    with open(path, "rb") as fh:
        data = fh.read()
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_off:pe_off + 4] != b"PE\0\0":
        raise ValueError("not a PE file: {0}".format(path))
    coff = pe_off + 4
    nsections = struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    magic = struct.unpack_from("<H", data, opt)[0]
    dd_off = opt + (112 if magic == 0x20B else 96)   # PE32+ vs PE32
    imp_rva = struct.unpack_from("<I", data, dd_off + 8)[0]  # dir[1]
    if not imp_rva:
        return []
    sections = []
    sec = opt + opt_size
    for i in range(nsections):
        vsize, va, rawsize, raw = struct.unpack_from("<IIII", data,
                                                     sec + i * 40 + 8)
        sections.append((va, max(vsize, rawsize), raw))

    def rva2off(rva):
        for va, size, raw in sections:
            if va <= rva < va + size:
                return rva - va + raw
        raise ValueError("bad RVA {0:#x} in {1}".format(rva, path))

    names = []
    desc = rva2off(imp_rva)
    while True:
        name_rva = struct.unpack_from("<I", data, desc + 12)[0]
        if not name_rva:
            break
        off = rva2off(name_rva)
        end = data.index(b"\0", off)
        names.append(data[off:end].decode("ascii", "replace"))
        desc += 20
    return names


def pe_machine(path):
    import struct
    with open(path, "rb") as fh:
        data = fh.read(4096)
    off = struct.unpack_from("<I", data, 0x3C)[0]
    return struct.unpack_from("<H", data, off + 4)[0]


def _vcpkg_bin_dir(arch):
    vcpkg = (os.environ.get("VCPKG_ROOT")
             or os.environ.get("VCPKG_INSTALLATION_ROOT"))
    if not vcpkg:
        return None
    triplet = os.environ.get("VCPKG_DEFAULT_TRIPLET",
                             WINDOWS_ARCHS[host_windows_arch(arch)][1])
    d = os.path.join(vcpkg, "installed", triplet, "bin")
    return d if os.path.isdir(d) else None


def bundle_windows_runtime_deps(out_dir, arch="host", deps_prefix=None,
                                roots=None):
    """
    Walk the import tables of the staged FreeRDP DLLs and copy every
    third-party DLL they (transitively) need from vcpkg's bin/ into
    out_dir, so the directory is loadable as-is. System and CRT DLLs are
    skipped; anything copied must have the same PE machine type as the
    FreeRDP DLLs.
    """
    src_dirs = [d for d in (_vcpkg_bin_dir(arch),
                            os.path.join(deps_prefix, "bin") if deps_prefix
                            else None)
                if d and os.path.isdir(d)]
    if not src_dirs:
        print("[deps] no vcpkg/deps bin dir found; not bundling runtime DLLs")
        return
    src_dir = ", ".join(src_dirs)
    available = {}
    for d in src_dirs:
        for fn in os.listdir(d):
            if fn.lower().endswith(".dll"):
                available.setdefault(fn.lower(), os.path.join(d, fn))

    staged = [os.path.join(out_dir, f) for f in os.listdir(out_dir)
              if f.lower().endswith((".dll", ".exe"))]
    if not staged:
        return
    want_machine = pe_machine(staged[0])

    queue = list(roots) if roots else list(staged)
    seen = set(os.path.basename(p).lower() for p in staged)
    copied = []
    while queue:
        dll = queue.pop()
        for dep in pe_imports(dll):
            dep_l = dep.lower()
            if dep_l in seen or dep_l.startswith(_WIN_SYSTEM_DLL_PREFIXES):
                continue
            seen.add(dep_l)
            src_path = available.get(dep_l)
            if not src_path:
                # Not in vcpkg: assume it's a Windows system DLL.
                continue
            if pe_machine(src_path) != want_machine:
                raise SystemExit(
                    "[deps] {0} in {1} has the wrong architecture for this "
                    "build".format(dep, src_dir))
            dst = os.path.join(out_dir, os.path.basename(src_path))
            shutil.copy2(src_path, dst)
            copied.append(os.path.basename(src_path))
            queue.append(dst)
    print("[deps] bundled {0} runtime DLL(s) from {1}: {2}".format(
        len(copied), src_dir, ", ".join(sorted(copied)) or "none"))


def _elf_needed(path):
    out = subprocess.check_output(["readelf", "-d", path]).decode(
        errors="replace")
    needed = []
    for line in out.splitlines():
        if "(NEEDED)" in line and "[" in line:
            needed.append(line[line.index("[") + 1:line.rindex("]")])
    return needed


def _macho_deps(path):
    out = subprocess.check_output(["otool", "-L", path]).decode(
        errors="replace")
    deps = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if line and " (" in line:
            deps.append(line.split(" (")[0])
    return deps


def bundle_unix_runtime_deps(out_dir, deps_prefix, roots=None,
                             extra_libdirs=None, extra_allow=None):
    """
    Linux/macOS counterpart of bundle_windows_runtime_deps(): walk the
    dynamic dependencies of everything staged in out_dir and copy the ones
    that live in <deps_prefix>/lib (libavcodec, libopenh264, libusb-1.0,
    ...) next to them. System libraries are left alone. On macOS the
    install names of the copied dylibs, and every reference to them, are
    rewritten to @rpath/<name> so @loader_path resolution works.
    """
    if not deps_prefix and not extra_libdirs:
        return
    sysname = platform.system()
    libdirs = [d for d in ((os.path.join(deps_prefix, "lib"),
                            os.path.join(deps_prefix, "lib64"))
                           if deps_prefix else ())
               if os.path.isdir(d)]
    available = {}
    for d in libdirs:
        for fn in os.listdir(d):
            available[fn] = os.path.join(d, fn)
    # Extra directories (system krb5) are searched only for an allowlist so
    # we never accidentally vendor glibc or other base libraries.
    for d in (extra_libdirs or []):
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if any(fn.startswith(p) for p in (extra_allow or ())):
                available.setdefault(fn, os.path.join(d, fn))

    def staged_files():
        return [os.path.join(r, f) for r, _d, fs in os.walk(out_dir)
                for f in fs if not os.path.islink(os.path.join(r, f))]

    queue = list(roots) if roots else staged_files()
    seen = set(os.path.basename(p) for p in staged_files())
    copied = []
    while queue:
        f = queue.pop()
        if sysname == "Linux":
            if ".so" not in os.path.basename(f) and not os.access(f, os.X_OK):
                continue
            try:
                deps = _elf_needed(f)
            except subprocess.CalledProcessError:
                continue
        else:
            if not (f.endswith(".dylib") or ".so" in f or os.access(f, os.X_OK)):
                continue
            try:
                deps = [os.path.basename(d) for d in _macho_deps(f)]
            except subprocess.CalledProcessError:
                continue
        for dep in deps:
            if dep in seen or dep not in available:
                continue
            seen.add(dep)
            srcp = os.path.realpath(available[dep])
            dst = os.path.join(out_dir, dep)
            shutil.copy2(srcp, dst)
            copied.append(dep)
            queue.append(dst)
    print("[deps] bundled {0} runtime libraries from {1}: {2}".format(
        len(copied), deps_prefix, ", ".join(sorted(copied)) or "none"))

    if sysname == "Darwin":
        # Normalise install names: every reference into deps_prefix (or
        # to a bare @rpath name) must point at @rpath/<basename>.
        for f in staged_files():
            if not (f.endswith(".dylib") or os.access(f, os.X_OK)):
                continue
            try:
                refs = _macho_deps(f)
            except subprocess.CalledProcessError:
                continue
            for ref in refs:
                base = os.path.basename(ref)
                if base in seen and ref != "@rpath/" + base and (
                        (deps_prefix and ref.startswith(deps_prefix))
                        or any(ref.startswith(d) for d in (extra_libdirs or []))
                        or not ref.startswith("/")):
                    subprocess.check_call(["install_name_tool", "-change", ref,
                                           "@rpath/" + base, f])
            if f.endswith(".dylib") and os.path.basename(f) in copied:
                subprocess.check_call(["install_name_tool", "-id",
                                       "@rpath/" + os.path.basename(f), f])


# Executables we know how to smoke-test. Anything else in bin/ is shipped
# too, this list just drives validate_build.py.
KNOWN_EXECUTABLES = ("xfreerdp", "wlfreerdp", "sdl-freerdp", "wfreerdp",
                     "sfreerdp-server", "freerdp-shadow-cli", "freerdp-proxy",
                     "winpr-makecert", "winpr-hash",
                     "ffmpeg", "ffprobe", "h264enc", "h264dec", "listdevs")


# Executables we ship from the *dependency* prefix. vcpkg drops every port's
# tools into bin/ (brotli.exe, bzip2.exe, ...); only these belong in the
# package. FreeRDP's own prefix is shipped completely.
DEPS_EXECUTABLES = ("ffmpeg", "ffprobe", "h264enc", "h264dec", "listdevs")


def stage_executables(prefixes, libs_dir, deps_prefix=None, arch="host"):
    """
    Copy executables from the bin/ dirs of the given prefixes into
    pyfreerdp/_bin/ (Windows: into _libs/, where the DLLs are - Windows
    resolves imports from the executable's own directory) and point their
    rpath at ../_libs. Returns the list of staged paths.
    """
    sysname = platform.system()
    if sysname == "Windows":
        out = libs_dir
    else:
        out = os.path.join(os.path.dirname(libs_dir), "_bin")
    if not os.path.isdir(out):
        os.makedirs(out)
    staged = []
    for pfx in prefixes:
        bindir = os.path.join(pfx, "bin")
        if not os.path.isdir(bindir):
            continue
        is_deps = deps_prefix and os.path.abspath(pfx) == os.path.abspath(deps_prefix)
        for fn in sorted(os.listdir(bindir)):
            p = os.path.join(bindir, fn)
            if not os.path.isfile(p):
                continue
            if is_deps and fn.lower().split(".")[0] not in DEPS_EXECUTABLES:
                continue  # vcpkg tool noise (brotli.exe, bzip2.exe, ...)
            if sysname == "Windows":
                if not fn.lower().endswith(".exe"):
                    continue
            elif not os.access(p, os.X_OK) or fn.endswith(
                    (".so", ".dylib", ".a", ".la", ".pc")):
                continue
            dst = os.path.join(out, fn)
            shutil.copy2(p, dst)
            staged.append(dst)
            print("[bin] {0}".format(dst))
    if sysname == "Linux" and staged:
        pe = _ensure_patchelf()
        for p in staged:
            try:
                subprocess.check_call([pe, "--set-rpath", "$ORIGIN/../_libs", p])
            except subprocess.CalledProcessError:
                print("[bin] warning: could not set rpath on {0}".format(p))
    elif sysname == "Darwin" and staged:
        for p in staged:
            _macho_add_rpath(p, "@executable_path/../_libs",
                             replace_prefix="@executable_path/")
    # The executables pull in libraries the core libs don't (ffmpeg ->
    # libavformat/avfilter/avdevice, xfreerdp -> libfreerdp-client ...).
    # Walk their imports too, then make the newly copied libs relocatable.
    if staged:
        if sysname == "Windows":
            bundle_windows_runtime_deps(libs_dir, arch, deps_prefix,
                                        roots=staged)
        else:
            bundle_unix_runtime_deps(libs_dir, deps_prefix, roots=staged)
        _fix_rpath(libs_dir)
    # OpenH264's h264enc needs its config files.
    for pfx in prefixes:
        share = os.path.join(pfx, "share", "openh264")
        if os.path.isdir(share):
            dst = os.path.join(out, "openh264-config")
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            shutil.copytree(share, dst)
    print("[bin] {0} executables staged into {1}".format(len(staged), out))
    return staged


def binary_soname(path):
    """SONAME (ELF) or install-name basename (Mach-O) of a shared library."""
    try:
        if platform.system() == "Linux":
            out = subprocess.check_output(["readelf", "-d", path]).decode(
                errors="replace")
            for line in out.splitlines():
                if "(SONAME)" in line and "[" in line:
                    return line[line.index("[") + 1:line.rindex("]")]
        elif platform.system() == "Darwin":
            out = subprocess.check_output(["otool", "-D", path]).decode(
                errors="replace").splitlines()
            if len(out) >= 2 and out[1].strip():
                return os.path.basename(out[1].strip())
    except subprocess.CalledProcessError:
        pass
    return None


def soname_only(artifacts):
    """
    Keep one file per library: the SONAME (libX.so.3 / libX.3.dylib). The
    unversioned dev link and the fully versioned file are the same bytes;
    the dynamic loader resolves dependencies by SONAME and pyfreerdp's
    loader probes the SONAME first, so nothing else is needed. Cuts the
    minimal package to a third of its size.
    """
    sysname = platform.system()
    if sysname == "Windows":
        return artifacts
    real = {}
    for p, sub in artifacts:
        rp = os.path.realpath(p)
        real.setdefault((rp, sub), []).append(os.path.basename(p))
    keep = []
    for (rp, sub), names in real.items():
        soname = binary_soname(rp)
        if soname not in names:        # modules without version: keep as is
            soname = sorted(names, key=len)[0]
        keep.append((os.path.join(os.path.dirname(rp), soname)
                     if os.path.exists(os.path.join(os.path.dirname(rp), soname))
                     else rp, sub))
    return keep


def install_into_package(artifacts, arch="host", deps_prefix=None,
                         compact=False, krb5_libdir=None):
    if compact:
        artifacts = soname_only(artifacts)
    out = os.path.join(package_root(), "pyfreerdp", "_libs")
    if not os.path.isdir(out):
        os.makedirs(out)
    count = 0
    module_dirs = set()
    for src_path, sub in artifacts:
        dst_dir = os.path.join(out, sub) if sub else out
        if not os.path.isdir(dst_dir):
            os.makedirs(dst_dir)
        dst = os.path.join(dst_dir, os.path.basename(src_path))
        if os.path.islink(src_path):
            target = os.path.realpath(src_path)
            shutil.copy2(target, dst)
        else:
            shutil.copy2(src_path, dst)
        if sub:
            module_dirs.add(dst_dir)
        count += 1
        print("[install] {0}".format(dst))
    print("[install] {0} libraries staged into {1}".format(count, out))
    for d in sorted(module_dirs):
        print("[install] loadable modules staged into {0}".format(d))
    if platform.system() == "Windows":
        bundle_windows_runtime_deps(out, arch, deps_prefix)
    else:
        extra_dirs = list(krb5_libdir or [])
        extra_allow = list(KRB5_RUNTIME_LIBS) if krb5_libdir else []
        if platform.system() == "Darwin":
            # libfreerdp3/libwinpr3 link Homebrew's OpenSSL by absolute path;
            # ship libssl/libcrypto (install names rewritten to @rpath) so
            # the package does not depend on Homebrew being installed.
            ssl_dir = os.environ.get("OPENSSL_ROOT_DIR")
            if not ssl_dir and have("brew"):
                try:
                    ssl_dir = subprocess.check_output(
                        ["brew", "--prefix", "openssl@3"],
                        stderr=subprocess.DEVNULL).decode().strip()
                except (OSError, subprocess.CalledProcessError):
                    ssl_dir = None
            if ssl_dir and os.path.isdir(os.path.join(ssl_dir, "lib")):
                extra_dirs.append(os.path.join(ssl_dir, "lib"))
                extra_allow += ["libssl.", "libcrypto."]
        bundle_unix_runtime_deps(out, deps_prefix,
                                 extra_libdirs=extra_dirs or None,
                                 extra_allow=tuple(extra_allow) or None)
    _fix_rpath(out)
    return out


# ---------------------------------------------------------------------------
# Linux dependency hint
# ---------------------------------------------------------------------------

LINUX_APT_DEPS = [
    "build-essential", "cmake", "ninja-build", "git", "pkg-config",
    "libssl-dev", "zlib1g-dev",
    # 3.x: WITH_KRB5 defaults ON and hard-fails without it; JSON is needed
    # for Azure AD / RDS AAD authentication (soft-disabled if missing).
    "libkrb5-dev", "libcjson-dev",
    # winpr's unicode layer needs ICU on Linux (or WITH_UNICODE_BUILTIN=ON).
    "libicu-dev",
    "libx11-dev", "libxext-dev", "libxrandr-dev", "libxinerama-dev",
    "libxfixes-dev", "libxcursor-dev", "libxi-dev", "libxv-dev",
    "libxkbfile-dev", "libxkbcommon-dev",
    "libwayland-dev", "wayland-protocols",
    "libasound2-dev",
    "libpng-dev", "libjpeg-dev",
    "libcairo2-dev",
]

LINUX_DNF_DEPS = [
    "gcc-c++", "cmake", "ninja-build", "git", "pkgconfig",
    "openssl-devel", "zlib-devel",
    "krb5-devel", "cjson-devel",
    "libicu-devel",
    "libX11-devel", "libXext-devel", "libXrandr-devel", "libXinerama-devel",
    "libXfixes-devel", "libXcursor-devel", "libXi-devel", "libXv-devel",
    "libxkbfile-devel", "libxkbcommon-devel",
    "wayland-devel", "wayland-protocols-devel",
    "alsa-lib-devel",
    "libpng-devel", "libjpeg-turbo-devel",
    "cairo-devel",
]

# Extra packages needed only when a channel with a 'deps' tag in CHANNELS
# is enabled via --enable-channel. Keyed by that tag.
DEPS_APT = {
    "libusb": ["libusb-1.0-0-dev"],
    "cups": ["libcups2-dev"],
    "gstreamer": ["libgstreamer1.0-dev", "libgstreamer-plugins-base1.0-dev"],
    "ffmpeg": ["libavcodec-dev", "libavutil-dev", "libswscale-dev",
               "libv4l-dev"],
}

DEPS_DNF = {
    "libusb": ["libusb1-devel"],
    "cups": ["cups-devel"],
    "gstreamer": ["gstreamer1-devel", "gstreamer1-plugins-base-devel"],
    "ffmpeg": ["ffmpeg-free-devel", "libv4l-devel"],
}


def print_linux_dep_hint(profile, enable_channels=None):
    if platform.system() != "Linux":
        return
    tags = sorted(set(CHANNELS[c]["deps"] for c in _norm_set(enable_channels)
                      if c in CHANNELS and CHANNELS[c].get("deps")))
    if have("apt-get"):
        pkgs = list(LINUX_APT_DEPS)
        for t in tags:
            pkgs += DEPS_APT.get(t, [])
        print("\n[hint] On Debian/Ubuntu, the build needs roughly these packages:")
        print("    sudo apt-get install -y " + " ".join(pkgs))
    elif have("dnf"):
        pkgs = list(LINUX_DNF_DEPS)
        for t in tags:
            pkgs += DEPS_DNF.get(t, [])
        print("\n[hint] On Fedora/RHEL, the build needs roughly these packages:")
        print("    sudo dnf install -y " + " ".join(pkgs))


# ---------------------------------------------------------------------------
# Android cross-build
# ---------------------------------------------------------------------------

ANDROID_ABIS = ("arm64-v8a", "armeabi-v7a", "x86_64", "x86")


def build_android(src, abi, api_level, jobs, profile, enable_channels=None,
                  disable_channels=None, channels_enabled=True,
                  deps_prefix=None):
    if abi not in ANDROID_ABIS:
        raise SystemExit("Unknown ABI: {0}. Pick one of {1}".format(
            abi, ANDROID_ABIS))
    ndk = (os.environ.get("ANDROID_NDK_ROOT")
           or os.environ.get("ANDROID_NDK_HOME"))
    if not ndk or not os.path.isdir(ndk):
        raise SystemExit(
            "ANDROID_NDK_ROOT (or ANDROID_NDK_HOME) must point to an NDK "
            "install.")
    toolchain = os.path.join(ndk, "build", "cmake", "android.toolchain.cmake")
    if not os.path.isfile(toolchain):
        raise SystemExit(
            "NDK missing toolchain at {0}".format(toolchain))

    build_dir = os.path.join(src, "build-android-{0}".format(abi))
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    os.makedirs(build_dir)

    install_dir = os.path.join(repo_root(), "build", "android", abi)
    if not os.path.isdir(install_dir):
        os.makedirs(install_dir)

    if profile == "full":
        print("[android] Down-shifting profile 'full' -> 'minimal' (shadow "
              "server requires platform capture APIs not available via NDK). "
              "WITH_SERVER=ON is preserved.")
        profile = "minimal"

    # host_os="Android" makes resolve_channels() drop the Linux-only
    # device channels (serial, parallel) - upstream does the same.
    opts = cmake_options_for(profile, host_os="Android",
                             enable_channels=enable_channels,
                             disable_channels=disable_channels,
                             channels_enabled=channels_enabled,
                             deps_prefix=deps_prefix)
    if deps_prefix:
        # The NDK toolchain restricts find_* to the sysroot.
        opts.append("-DCMAKE_FIND_ROOT_PATH={0}".format(deps_prefix))
    opts += [
        "-DWITH_X11=OFF", "-DWITH_WAYLAND=OFF",
        "-DWITH_PULSE=OFF", "-DWITH_ALSA=OFF",
        "-DWITH_CUPS=OFF", "-DWITH_PCSC=OFF",
        # No cairo in the NDK; and winpr's Android unicode backend goes
        # through JNI, which a non-Java host (Python) can't provide.
        "-DWITH_CAIRO=OFF",
        "-DWITH_UNICODE_BUILTIN=ON",
        "-DWITH_CLIENT_SDL=OFF",
        # FreeRDP turns ThinLTO on by default; with the NDK's
        # -Wl,--fatal-warnings a "loop not vectorized" remark from
        # prim_copy.c becomes a link error on i686. Not worth it.
        "-DCMAKE_INTERPROCEDURAL_OPTIMIZATION=OFF",
    ]
    # OpenSSL for Android is not in the NDK. Callers point us at a
    # cross-built copy with PYFREERDP_EXTRA_CMAKE, e.g.
    #   -DOPENSSL_ROOT_DIR=/path -DCMAKE_FIND_ROOT_PATH=/path
    # dedupe AFTER appending it so CMAKE_FIND_ROOT_PATH/CMAKE_PREFIX_PATH
    # from the deps prefix and from OpenSSL are merged, not last-wins.
    extra = os.environ.get("PYFREERDP_EXTRA_CMAKE", "").split()
    opts = dedupe_defines(opts + extra)
    extra = []

    cfg = [
        "cmake", "-S", src, "-B", build_dir,
        "-DCMAKE_TOOLCHAIN_FILE={0}".format(toolchain),
        "-DANDROID_ABI={0}".format(abi),
        "-DANDROID_PLATFORM=android-{0}".format(api_level),
        "-DANDROID_STL=c++_shared",
        "-DCMAKE_INSTALL_PREFIX={0}".format(install_dir),
    ] + opts + extra + ["-G", "Ninja"]
    require_tools(["cmake", "ninja"])
    run(cfg, env=deps_env(deps_prefix, cross=True))
    if channels_enabled:
        verify_channel_cache(build_dir, expected_channel_cache(
            profile, "Android", enable_channels, disable_channels))
    if deps_media_available(deps_prefix):
        verify_media_cache(build_dir, deps_prefix)
    env = deps_env(deps_prefix, cross=True)
    run(["cmake", "--build", build_dir, "--parallel", str(jobs)], env=env)
    run(["cmake", "--install", build_dir], env=env)

    target = os.path.join(package_root(), "pyfreerdp", "_libs", "android", abi)
    if not os.path.isdir(target):
        os.makedirs(target)
    for so in glob.glob(os.path.join(install_dir, "lib", "*.so")):
        dst = os.path.join(target, os.path.basename(so))
        shutil.copy2(so, dst)
        print("[android:{0}] {1}".format(abi, dst))
    if deps_prefix:
        # Ship the media libs the FreeRDP .so files import.
        bundle_unix_runtime_deps(target, deps_prefix)
        for so in glob.glob(os.path.join(deps_prefix, "lib", "*.so")):
            dst = os.path.join(target, os.path.basename(so))
            if not os.path.exists(dst):
                shutil.copy2(so, dst)
                print("[android] {0}".format(dst))
    return target


# ---------------------------------------------------------------------------
# iOS cross-build (host must be macOS)
# ---------------------------------------------------------------------------

IOS_PLATFORMS = ("OS64", "SIMULATOR64", "SIMULATORARM64")


def build_ios(src, jobs, profile, enable_channels=None,
              disable_channels=None, channels_enabled=True,
              ios_platform="OS64", deps_prefix=None):
    if platform.system() != "Darwin":
        raise SystemExit("iOS builds require macOS + Xcode.")
    if ios_platform not in IOS_PLATFORMS:
        raise SystemExit("Unknown --ios-platform {0}; pick one of {1}".format(
            ios_platform, IOS_PLATFORMS))
    require_tools(["cmake", "xcodebuild"])
    # Prefer the toolchain FreeRDP ships (a maintained copy of
    # leetal/ios-cmake): it disables code signing for the tool binaries,
    # honours CMAKE_FIND_ROOT_PATH for cross-built deps such as OpenSSL,
    # and understands the same PLATFORM values as our thin fallback.
    toolchain = os.path.join(src, "cmake", "ios.toolchain.cmake")
    if not os.path.isfile(toolchain):
        toolchain = os.path.join(repo_root(), "cmake", "toolchains",
                                 "ios.cmake")
    if not os.path.isfile(toolchain):
        raise SystemExit("Missing iOS toolchain at {0}".format(toolchain))
    print("[ios] toolchain: {0}  platform: {1}".format(toolchain,
                                                        ios_platform))

    build_dir = os.path.join(src, "build-ios-{0}".format(ios_platform))
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    os.makedirs(build_dir)
    install_dir = os.path.join(repo_root(), "build", "ios", ios_platform)
    if not os.path.isdir(install_dir):
        os.makedirs(install_dir)

    if profile in ("full", "minimal"):
        profile = "minimal"
    opts = cmake_options_for(profile, host_os="iOS",
                             enable_channels=enable_channels,
                             disable_channels=disable_channels,
                             channels_enabled=channels_enabled,
                             deps_prefix=deps_prefix)
    if deps_prefix:
        opts += ["-DCMAKE_FIND_ROOT_PATH={0}".format(deps_prefix),
                 "-DWITH_CAIRO=OFF"]
    opts += [
        "-DBUILD_SHARED_LIBS=OFF",          # static archives for iOS
        "-DWITH_CAIRO=OFF",                 # no cairo on iOS
        "-DWITH_CLIENT_IOS=OFF",            # native iOS client is an Xcode app
        "-DWITH_CLIENT_SDL=OFF",
        "-DWITH_PLATFORM_SERVER=OFF",
        "-DWITH_SAMPLE=OFF",
        "-DWITH_KRB5=OFF",
        "-DENABLE_BITCODE=OFF",
        "-DDEPLOYMENT_TARGET=13.0",
    ]
    # Generator. Static archives don't need Xcode, and the Xcode generator
    # is the fragile part of iOS builds: multi-config output dirs
    # (Release-iphoneos/) that `cmake --install` has to resolve, code-signing
    # phases for the tool executables, and much slower builds. Prefer Ninja
    # (single-config, plain install); fall back to Xcode only if ninja is
    # missing, in which case disable code signing.
    if have("ninja"):
        generator = ["-G", "Ninja"]
    else:
        print("[ios] ninja not found; falling back to the Xcode generator")
        generator = ["-G", "Xcode"]
        opts += [
            "-DCMAKE_XCODE_ATTRIBUTE_CODE_SIGNING_ALLOWED=NO",
            "-DCMAKE_XCODE_ATTRIBUTE_CODE_SIGNING_REQUIRED=NO",
            "-DCMAKE_XCODE_ATTRIBUTE_CODE_SIGN_IDENTITY=",
        ]
    # OpenSSL for iOS is not on the runner; callers hand us a cross-built
    # prefix via PYFREERDP_EXTRA_CMAKE, e.g.
    #   -DOPENSSL_ROOT_DIR=/p -DCMAKE_FIND_ROOT_PATH=/p -DCMAKE_PREFIX_PATH=/p
    # (merged with the deps prefix by dedupe_defines, see _LIST_DEFINES).
    extra = os.environ.get("PYFREERDP_EXTRA_CMAKE", "").split()
    opts = dedupe_defines(opts + extra)
    extra = []

    cfg = [
        "cmake", "-S", src, "-B", build_dir,
        "-DCMAKE_TOOLCHAIN_FILE={0}".format(toolchain),
        "-DPLATFORM={0}".format(ios_platform),
        "-DCMAKE_INSTALL_PREFIX={0}".format(install_dir),
    ] + opts + extra + generator
    env = deps_env(deps_prefix, cross=True)
    run(cfg, env=env)
    if deps_media_available(deps_prefix):
        verify_media_cache(build_dir, deps_prefix)
    if channels_enabled:
        verify_channel_cache(build_dir, expected_channel_cache(
            profile, "iOS", enable_channels, disable_channels))
    run(["cmake", "--build", build_dir, "--config", "Release",
         "--parallel", str(jobs)], env=env)

    # Install is only used to get the archives into one directory. If it
    # fails (historically: Xcode generator path resolution), fall back to
    # harvesting the .a files straight from the build tree - the libraries
    # themselves were already built successfully by the step above.
    rc = run(["cmake", "--install", build_dir, "--config", "Release"],
             check=False, env=env)
    if rc == 0:
        archives = glob.glob(os.path.join(install_dir, "lib", "*.a"))
    else:
        print("\n[ios] WARNING: cmake --install exited {0}; look for a "
              "'CMake Error' line above (it is printed before the "
              "surrounding 'Installing:' lines because cmake buffers "
              "stdout). Collecting archives from the build tree "
              "instead.".format(rc))
        archives = []
        for root, _dirs, files in os.walk(build_dir):
            for fn in files:
                if fn.endswith(".a") and not fn.startswith("libfreerdp-test"):
                    archives.append(os.path.join(root, fn))
        # Only ship the public libraries (the tree also holds object
        # library archives on some generators).
        keep = ("libwinpr", "libfreerdp", "librdtk", "libfreerdp-client",
                "libfreerdp-server")
        archives = [a for a in archives
                    if os.path.basename(a).startswith(keep)]

    target = os.path.join(package_root(), "pyfreerdp", "_libs", "ios",
                          ios_platform)
    if not os.path.isdir(target):
        os.makedirs(target)
    staged = []
    for a in sorted(archives):
        dst = os.path.join(target, os.path.basename(a))
        shutil.copy2(a, dst)
        staged.append(os.path.basename(a).lower())
        print("[ios] {0}".format(dst))
    if deps_prefix:
        for a in glob.glob(os.path.join(deps_prefix, "lib", "*.a")):
            dst = os.path.join(target, os.path.basename(a))
            shutil.copy2(a, dst)
            print("[ios] {0} (dependency)".format(dst))
    missing = [s for s in EXPECTED_LIBS[profile]
               if not any(s in n for n in staged)]
    if missing:
        raise SystemExit("[ios] expected static libraries missing: {0}\n"
                         "staged: {1}".format(missing, staged))
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def resolve_deps_prefix(args):
    if not args.deps_prefix:
        return None
    if args.deps_prefix != "auto":
        return os.path.abspath(args.deps_prefix)
    if args.target == "android":
        label = "android-{0}".format(args.abi)
    elif args.target == "ios":
        label = "ios-{0}".format(args.ios_platform)
    elif platform.system() == "Windows":
        label = "windows-{0}".format(host_windows_arch(args.arch))
    else:
        m = platform.machine().lower()
        m = {"amd64": "x86_64"}.get(m, m)
        label = "{0}-{1}".format(platform.system().lower(), m)
    cand = os.path.join(repo_root(), "build", "deps",
                        "{0}-{1}".format(label, args.edition))
    if os.path.isdir(cand):
        return cand
    print("[pyfreerdp-build] --deps-prefix auto: {0} not found".format(cand))
    return None


def verify_media_linked(out_dir, deps_prefix=None):
    """The staged libfreerdp3 must import exactly the media libs the edition
    provides, and those must be staged next to it."""
    sysname = platform.system()
    core = None
    for fn in os.listdir(out_dir):
        low = fn.lower()
        if low.startswith(("libfreerdp3", "freerdp3")) and (
                ".so" in low or low.endswith((".dylib", ".dll"))) and \
                not os.path.islink(os.path.join(out_dir, fn)):
            core = os.path.join(out_dir, fn)
            break
    if not core:
        raise SystemExit("[verify] libfreerdp3 not found in {0}".format(out_dir))
    if sysname == "Linux":
        deps = _elf_needed(core)
    elif sysname == "Darwin":
        deps = [os.path.basename(d) for d in _macho_deps(core)]
    else:
        deps = pe_imports(core)
    low = " ".join(d.lower() for d in deps)
    want = []
    if deps_has_ffmpeg(deps_prefix):
        want += ["avcodec", "swscale"]
    if deps_has_openh264(deps_prefix):
        want.append("openh264")
    missing = [n for n in want if n not in low]
    if missing:
        raise SystemExit("[verify] libfreerdp3 does not link {0}; imports: "
                         "{1}".format(missing, deps))
    present = [d for d in deps if any(n in d.lower() for n in
                                       ("av", "sw", "openh264", "usb"))]
    for d in present:
        if not os.path.exists(os.path.join(out_dir, d)):
            raise SystemExit("[verify] {0} imports {1} but it is not staged "
                             "in {2}".format(os.path.basename(core), d, out_dir))
    print("[verify] libfreerdp3 links and ships: {0}".format(
        ", ".join(sorted(present))))


def main():
    p = argparse.ArgumentParser(description="Build FreeRDP for pyfreerdp")
    p.add_argument("--ref", default=DEFAULT_REF, help="Git ref to build")
    p.add_argument("--prefix", default=None,
                   help="Install prefix for host builds (default: temp dir)")
    p.add_argument("--target", choices=("host", "android", "ios"),
                   default="host")
    p.add_argument("--profile",
                   choices=("full", "client-only", "server-only", "minimal"),
                   default="full",
                   help="minimal: size-optimised libraries only (MinSizeRel, "
                        "stripped, no media deps, no executables). full: "
                        "feature-complete (Release, FFmpeg/OpenH264/libusb, "
                        "all channels, executables, proxy/sample; shadow "
                        "on Linux). client-only/server-only: library-only "
                        "variants of minimal. Default: full.")
    p.add_argument("--enable-channel", action="append", default=[],
                   metavar="NAME",
                   help="Turn on a default-off channel (both client and "
                        "server halves where they exist): {0}, or any "
                        "upstream CHANNEL_<NAME>. Repeatable.".format(
                            ", ".join(sorted(
                                n for n, s in CHANNELS.items()
                                if not s["default"]))))
    p.add_argument("--disable-channel", action="append", default=[],
                   metavar="NAME",
                   help="Turn off a default-on channel. Repeatable.")
    p.add_argument("--no-channels", action="store_true",
                   help="Build without any virtual channels "
                        "(WITH_CHANNELS=OFF).")
    p.add_argument("--print-version", action="store_true",
                   help="Print BUILD_SCRIPT_VERSION and exit.")
    p.add_argument("--require-version", type=int, metavar="N",
                   help="Exit 0 if this script is BUILD_SCRIPT_VERSION N, "
                        "otherwise exit 2 with an explanation (used by the "
                        "workflows to detect a stale scripts/ directory).")
    p.add_argument("--list-channels", action="store_true",
                   help="Print the channel table for the chosen profile/"
                        "target and exit without building.")
    p.add_argument("--arch", default="host",
                   choices=("host", "x64", "x86", "arm64"),
                   help="Windows only: target architecture (default: the "
                        "runner's own). x86/arm64 cross-compile with the "
                        "Visual Studio generator and the matching vcpkg "
                        "triplet.")
    p.add_argument("--ios-platform", default="OS64", choices=IOS_PLATFORMS,
                   help="iOS only: OS64 = arm64 device (default), "
                        "SIMULATORARM64 = arm64 simulator, "
                        "SIMULATOR64 = x86_64 simulator.")
    p.add_argument("--deps-prefix", metavar="DIR",
                   help="Prefix produced by scripts/build_deps.py (FFmpeg, "
                        "OpenH264, libusb). Enables WITH_FFMPEG/DSP_FFMPEG/"
                        "SWSCALE/OPENH264 and the urbdrc (+ rdpecam on "
                        "Linux) channels, and bundles those libraries into "
                        "pyfreerdp/_libs. 'auto' = build/deps/<label> if "
                        "it exists.")
    p.add_argument("--edition", choices=EDITIONS, default=None,
                   help="Media stack: standard (built-in codecs only), ffmpeg "
                        "(FFmpeg: H.264/MJPEG decode, AAC/Opus, swscale; no "
                        "H.264 encoder), openh264 (OpenH264 encode+decode), "
                        "media (both). Selects build/deps/<label>-<edition> "
                        "for --deps-prefix auto. Default: media for the full "
                        "profile, standard for the others.")
    p.add_argument("--with-krb5", dest="with_krb5", action="store_true",
                   default=None,
                   help="Force Kerberos on (Linux: default anyway; macOS: uses "
                        "Homebrew MIT krb5, not upstream-verified).")
    p.add_argument("--without-krb5", dest="with_krb5", action="store_false",
                   help="Build without Kerberos even where it is supported.")
    p.add_argument("--no-deps", action="store_true",
                   help="full profile only: build without FFmpeg/OpenH264/"
                        "libusb even if build/deps/<label> exists.")
    p.add_argument("--no-executables", action="store_true",
                   help="full profile only: do not build/stage executables.")
    p.add_argument("--with-executables", action="store_true",
                   help="Also build and stage the executables (xfreerdp/"
                        "wfreerdp, sfreerdp-server, freerdp-shadow-cli, "
                        "freerdp-proxy, winpr-makecert, winpr-hash, plus "
                        "ffmpeg/ffprobe/h264enc/h264dec/listdevs from the "
                        "deps prefix) into pyfreerdp/_bin (Windows: _libs).")
    p.add_argument("--abi", default="arm64-v8a")
    p.add_argument("--api-level", type=int, default=24)
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    p.add_argument("--source-dir", default=None,
                   help="Use an existing FreeRDP checkout instead of cloning")
    p.add_argument("--skip-verify", action="store_true",
                   help="Skip post-install library verification")
    args = p.parse_args()

    if args.print_version:
        print(BUILD_SCRIPT_VERSION)
        return 0
    if args.require_version is not None:
        if args.require_version != BUILD_SCRIPT_VERSION:
            sys.stderr.write(
                "scripts/build_freerdp.py is version {0} but the workflow "
                "expects version {1}.\nThe repository has a stale copy of the "
                "build scripts: update scripts/build_freerdp.py, "
                "scripts/build_deps.py, scripts/validate_build.py and "
                "scripts/vcpkg.json together with the workflow files.\n".format(
                    BUILD_SCRIPT_VERSION, args.require_version))
            return 2
        print("[pyfreerdp-build] build script version {0} OK".format(
            BUILD_SCRIPT_VERSION))
        return 0

    print("[pyfreerdp-build] target={0} profile={1} ref={2} jobs={3}".format(
        args.target, args.profile, args.ref, args.jobs))
    host_os = {"host": platform.system(), "android": "Android",
               "ios": "iOS"}[args.target]
    channels_enabled = not args.no_channels
    # Profile defaults: full pulls in the media deps and the executables
    # unless told otherwise; minimal never does.
    if args.edition is None:
        args.edition = "media" if args.profile == "full" else "standard"
    if args.no_deps:
        args.edition = "standard"
    needs_deps = args.edition != "standard" or (
        platform.system() == "Windows" and args.target == "host")
    if args.deps_prefix is None and needs_deps:
        args.deps_prefix = "auto"
    if args.no_deps:
        args.deps_prefix = None
    print("[pyfreerdp-build] edition: {0}".format(args.edition))
    if args.profile == "full" and args.target == "host" and not args.no_executables:
        args.with_executables = True
    deps_prefix = resolve_deps_prefix(args)
    if needs_deps and args.deps_prefix and not deps_prefix \
            and not args.list_channels:
        raise SystemExit(
            "edition '{0}' needs a dependency prefix. Run "
            "scripts/build_deps.py --edition {0} first (or use "
            "--edition standard).".format(args.edition))
    if deps_prefix and args.edition != "standard":
        have_ff, have_oh = deps_has_ffmpeg(deps_prefix), deps_has_openh264(deps_prefix)
        want_ff = args.edition in ("ffmpeg", "media")
        want_oh = args.edition in ("openh264", "media")
        if have_ff != want_ff or have_oh != want_oh:
            raise SystemExit(
                "deps prefix {0} does not match edition {1} (has ffmpeg={2}, "
                "openh264={3})".format(deps_prefix, args.edition, have_ff, have_oh))
    if deps_prefix:
        print("[pyfreerdp-build] deps prefix: {0}".format(deps_prefix))
        args.enable_channel = list(args.enable_channel) + media_channels(
            deps_prefix, host_os, args.target)
    if args.list_channels:
        print(format_channel_table(args.profile, host_os,
                                   args.enable_channel, args.disable_channel))
        return 0
    if channels_enabled:
        rows = resolve_channels(args.profile, host_os,
                                args.enable_channel, args.disable_channel)
        print("[pyfreerdp-build] client channels: {0}".format(
            ", ".join(n for n, on, c, s in rows if c) or "none"))
        print("[pyfreerdp-build] server channels: {0}".format(
            ", ".join(n for n, on, c, s in rows if s) or "none"))
    else:
        print("[pyfreerdp-build] channels: disabled (--no-channels)")

    if args.source_dir:
        src = os.path.abspath(args.source_dir)
        if not os.path.isfile(os.path.join(src, "CMakeLists.txt")):
            sys.stderr.write(
                "Not a FreeRDP source tree: {0}\n".format(src))
            return 2
    else:
        work = os.path.join(tempfile.gettempdir(), "pyfreerdp-build")
        src = fetch_source(args.ref, work)

    if args.target == "host":
        print_linux_dep_hint(args.profile, args.enable_channel)
        prefix = os.path.abspath(
            args.prefix or os.path.join(tempfile.gettempdir(),
                                        "freerdp-prefix"))
        if not os.path.isdir(prefix):
            os.makedirs(prefix)
        build_host(src, prefix, args.jobs, args.profile,
                   enable_channels=args.enable_channel,
                   disable_channels=args.disable_channel,
                   channels_enabled=channels_enabled, arch=args.arch,
                   deps_prefix=deps_prefix, executables=args.with_executables,
                   with_krb5=args.with_krb5)
        artifacts = collect_host_artifacts(prefix)
        if not artifacts:
            sys.stderr.write(
                "No artifacts found after build - something went wrong.\n")
            return 3
        if not args.skip_verify:
            verify_artifacts(artifacts, args.profile, host_os)
        out = install_into_package(
            artifacts, arch=args.arch, deps_prefix=deps_prefix,
            compact=(args.profile != "full"),
            krb5_libdir=getattr(build_host, "krb5_libdir", None))
        if args.with_executables:
            stage_executables([prefix] + ([deps_prefix] if deps_prefix else []),
                              out, deps_prefix=deps_prefix, arch=args.arch)
        if not args.skip_verify:
            verify_loadable(out, args.profile, arch=args.arch)
            if deps_media_available(deps_prefix):
                verify_media_linked(out, deps_prefix)
        print("\nDone. Library installed under {0}.".format(out))
        return 0

    if args.target == "android":
        build_android(src, args.abi, args.api_level, args.jobs, args.profile,
                      enable_channels=args.enable_channel,
                      disable_channels=args.disable_channel,
                      channels_enabled=channels_enabled,
                      deps_prefix=deps_prefix)
        return 0

    if args.target == "ios":
        build_ios(src, args.jobs, args.profile,
                  enable_channels=args.enable_channel,
                  disable_channels=args.disable_channel,
                  channels_enabled=channels_enabled,
                  ios_platform=args.ios_platform, deps_prefix=deps_prefix)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())

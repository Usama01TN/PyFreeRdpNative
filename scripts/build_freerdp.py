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
        raise SystemExit("Command failed (exit {0}): {1}".format(
            proc.returncode, printable))
    return proc.returncode


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
    return os.path.dirname(package_root())


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


def dedupe_defines(opts):
    """
    For -DKEY=VALUE entries keep only the last occurrence of each KEY (a
    channel's 'extra' flags may override something in the common list,
    e.g. WITH_CUPS). Non -D entries are kept as is, in order.
    """
    last = {}
    for i, o in enumerate(opts):
        if o.startswith("-D") and "=" in o:
            last[o[2:].split("=", 1)[0]] = i
    out = []
    for i, o in enumerate(opts):
        if o.startswith("-D") and "=" in o:
            if last[o[2:].split("=", 1)[0]] != i:
                continue
        out.append(o)
    return out


def cmake_options_for(profile, host_os, enable_channels=None,
                      disable_channels=None, channels_enabled=True):
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

    if profile == "full":
        opts = [
            "-DWITH_CLIENT=ON", "-DWITH_CLIENT_COMMON=ON",
            "-DWITH_SERVER=ON", "-DWITH_SHADOW=ON", "-DWITH_PROXY=ON",
        ]
    elif profile == "client-only":
        opts = [
            "-DWITH_CLIENT=ON", "-DWITH_CLIENT_COMMON=ON",
            "-DWITH_SERVER=OFF", "-DWITH_SHADOW=OFF", "-DWITH_PROXY=OFF",
        ]
    elif profile == "server-only":
        opts = [
            "-DWITH_CLIENT=OFF", "-DWITH_CLIENT_COMMON=OFF",
            "-DWITH_SERVER=ON", "-DWITH_SHADOW=ON", "-DWITH_PROXY=ON",
        ]
    elif profile == "minimal":
        opts = [
            "-DWITH_CLIENT=ON", "-DWITH_CLIENT_COMMON=ON",
            "-DWITH_SERVER=ON", "-DWITH_SHADOW=OFF", "-DWITH_PROXY=OFF",
        ]
    else:
        raise SystemExit("Unknown profile: {0}".format(profile))

    if host_os == "Linux":
        opts += [
            "-DWITH_X11=ON",
            "-DWITH_WAYLAND=ON",
            "-DWITH_ALSA=ON",
            "-DWITH_CUPS=OFF",
            "-DWITH_PCSC=OFF",
        ]
    elif host_os == "Darwin":
        opts += [
            "-DWITH_X11=OFF", "-DWITH_WAYLAND=OFF", "-DWITH_ALSA=OFF",
        ]
    elif host_os == "Windows":
        opts += [
            "-DWITH_X11=OFF", "-DWITH_WAYLAND=OFF",
        ]

    opts += channel_options(profile, host_os, enable_channels,
                            disable_channels, channels_enabled)

    return dedupe_defines(common + opts)


# ---------------------------------------------------------------------------
# Host build (Linux / macOS / Windows native)
# ---------------------------------------------------------------------------

def host_dependency_probe(host_os):
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
    if host_os in ("Linux", "Darwin") and not have("krb5-config"):
        print("\n[warn] krb5-config not found - building with WITH_KRB5=OFF. "
              "NLA still works (NTLM), but Kerberos/SSO logins won't. "
              "Install libkrb5-dev / krb5-devel for full support.")
        opts.append("-DWITH_KRB5=OFF")
    if host_os == "Linux" and not _pkg_config_has("cairo"):
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


def build_host(src, prefix, jobs, profile, enable_channels=None,
               disable_channels=None, channels_enabled=True):
    require_tools(["cmake", "git"])
    host_os = platform.system()
    build_dir = os.path.join(src, "build")
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    os.makedirs(build_dir)

    opts = cmake_options_for(profile, host_os=host_os,
                             enable_channels=enable_channels,
                             disable_channels=disable_channels,
                             channels_enabled=channels_enabled)
    opts += host_dependency_probe(host_os)
    extra = os.environ.get("PYFREERDP_EXTRA_CMAKE", "").split()

    cfg = ["cmake", "-S", src, "-B", build_dir,
           "-DCMAKE_INSTALL_PREFIX={0}".format(prefix)] + opts + extra
    if have("ninja"):
        cfg += ["-G", "Ninja"]

    run(cfg)
    if channels_enabled:
        verify_channel_cache(build_dir, expected_channel_cache(
            profile, host_os, enable_channels, disable_channels))
    run(["cmake", "--build", build_dir, "--config", "Release",
         "--parallel", str(jobs)])
    run(["cmake", "--install", build_dir, "--config", "Release"])
    return prefix


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
            candidates.append((p, ""))
        # Modules on Windows land next to the exe or in bin/freerdp3/.
        bindir = os.path.join(prefix, "bin")
        for d in glob.glob(os.path.join(bindir, PLUGIN_SUBDIR_GLOB)):
            if not os.path.isdir(d):
                continue
            for root, _dirs, files in os.walk(d):
                rel = os.path.relpath(root, bindir)
                for fn in files:
                    if fn.lower().endswith(".dll"):
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


def verify_artifacts(artifacts, profile):
    """
    Assert that every library family the profile promised actually exists.
    Prevents shipping a wheel where CMake silently dropped server support
    because a dep was missing. (Channel presence is checked separately,
    right after configure, by verify_channel_cache().)
    """
    core_names = [os.path.basename(p).lower()
                  for p, sub in artifacts if not sub]
    missing = []
    for stem in EXPECTED_LIBS[profile]:
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
    print("[verify] core libraries present: {0}".format(
        EXPECTED_LIBS[profile]))


def install_into_package(artifacts):
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
                  disable_channels=None, channels_enabled=True):
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
                             channels_enabled=channels_enabled)
    opts = [o for o in opts if not o.startswith(
        ("-DWITH_X11=", "-DWITH_WAYLAND=", "-DWITH_ALSA=", "-DWITH_PULSE="))]
    opts += [
        "-DWITH_X11=OFF", "-DWITH_WAYLAND=OFF",
        "-DWITH_PULSE=OFF", "-DWITH_ALSA=OFF",
        "-DWITH_CUPS=OFF", "-DWITH_PCSC=OFF",
    ]

    cfg = [
        "cmake", "-S", src, "-B", build_dir,
        "-DCMAKE_TOOLCHAIN_FILE={0}".format(toolchain),
        "-DANDROID_ABI={0}".format(abi),
        "-DANDROID_PLATFORM=android-{0}".format(api_level),
        "-DANDROID_STL=c++_shared",
        "-DCMAKE_INSTALL_PREFIX={0}".format(install_dir),
    ] + opts + ["-G", "Ninja"]
    require_tools(["cmake", "ninja"])
    run(cfg)
    if channels_enabled:
        verify_channel_cache(build_dir, expected_channel_cache(
            profile, "Android", enable_channels, disable_channels))
    run(["cmake", "--build", build_dir, "--parallel", str(jobs)])
    run(["cmake", "--install", build_dir])

    target = os.path.join(package_root(), "pyfreerdp", "_libs", "android", abi)
    if not os.path.isdir(target):
        os.makedirs(target)
    for so in glob.glob(os.path.join(install_dir, "lib", "*.so")):
        dst = os.path.join(target, os.path.basename(so))
        shutil.copy2(so, dst)
        print("[android:{0}] {1}".format(abi, dst))
    return target


# ---------------------------------------------------------------------------
# iOS cross-build (host must be macOS)
# ---------------------------------------------------------------------------

def build_ios(src, jobs, profile, enable_channels=None,
              disable_channels=None, channels_enabled=True):
    if platform.system() != "Darwin":
        raise SystemExit("iOS builds require macOS + Xcode.")
    require_tools(["cmake", "xcodebuild"])
    toolchain = os.path.join(repo_root(), "cmake", "toolchains", "ios.cmake")
    if not os.path.isfile(toolchain):
        raise SystemExit("Missing iOS toolchain at {0}".format(toolchain))

    build_dir = os.path.join(src, "build-ios")
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    os.makedirs(build_dir)
    install_dir = os.path.join(repo_root(), "build", "ios")
    if not os.path.isdir(install_dir):
        os.makedirs(install_dir)

    if profile in ("full", "minimal"):
        profile = "minimal"
    opts = cmake_options_for(profile, host_os="iOS",
                             enable_channels=enable_channels,
                             disable_channels=disable_channels,
                             channels_enabled=channels_enabled)
    opts = [o for o in opts if not o.startswith("-DBUILD_SHARED_LIBS=")]
    opts.append("-DBUILD_SHARED_LIBS=OFF")  # static archive

    cfg = [
        "cmake", "-S", src, "-B", build_dir,
        "-DCMAKE_TOOLCHAIN_FILE={0}".format(toolchain),
        "-DPLATFORM=OS64",
        "-DCMAKE_INSTALL_PREFIX={0}".format(install_dir),
    ] + opts + ["-G", "Xcode"]
    run(cfg)
    if channels_enabled:
        verify_channel_cache(build_dir, expected_channel_cache(
            profile, "iOS", enable_channels, disable_channels))
    run(["cmake", "--build", build_dir, "--config", "Release",
         "--parallel", str(jobs)])
    run(["cmake", "--install", build_dir, "--config", "Release"])
    target = os.path.join(package_root(), "pyfreerdp", "_libs", "ios")
    if not os.path.isdir(target):
        os.makedirs(target)
    for a in glob.glob(os.path.join(install_dir, "lib", "*.a")):
        dst = os.path.join(target, os.path.basename(a))
        shutil.copy2(a, dst)
        print("[ios] {0}".format(dst))
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

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
                   help="Which subset of FreeRDP to build (default: full)")
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
    p.add_argument("--list-channels", action="store_true",
                   help="Print the channel table for the chosen profile/"
                        "target and exit without building.")
    p.add_argument("--abi", default="arm64-v8a")
    p.add_argument("--api-level", type=int, default=24)
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    p.add_argument("--source-dir", default=None,
                   help="Use an existing FreeRDP checkout instead of cloning")
    p.add_argument("--skip-verify", action="store_true",
                   help="Skip post-install library verification")
    args = p.parse_args()

    print("[pyfreerdp-build] target={0} profile={1} ref={2} jobs={3}".format(
        args.target, args.profile, args.ref, args.jobs))
    host_os = {"host": platform.system(), "android": "Android",
               "ios": "iOS"}[args.target]
    channels_enabled = not args.no_channels
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
                   channels_enabled=channels_enabled)
        artifacts = collect_host_artifacts(prefix)
        if not artifacts:
            sys.stderr.write(
                "No artifacts found after build - something went wrong.\n")
            return 3
        if not args.skip_verify:
            verify_artifacts(artifacts, args.profile)
        install_into_package(artifacts)
        print("\nDone. Library installed under {0}.".format(
            os.path.join(package_root(), "pyfreerdp", "_libs")))
        return 0

    if args.target == "android":
        build_android(src, args.abi, args.api_level, args.jobs, args.profile,
                      enable_channels=args.enable_channel,
                      disable_channels=args.disable_channel,
                      channels_enabled=channels_enabled)
        return 0

    if args.target == "ios":
        build_ios(src, args.jobs, args.profile,
                  enable_channels=args.enable_channel,
                  disable_channels=args.disable_channel,
                  channels_enabled=channels_enabled)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())

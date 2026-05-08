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

def cmake_options_for(profile, host_os):
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
        "-DWITH_X264=OFF",
        "-DWITH_OPENH264=OFF",
        "-DWITH_GSTREAMER_1_0=OFF",
        "-DWITH_PULSE=OFF",
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

    return common + opts


# ---------------------------------------------------------------------------
# Host build (Linux / macOS / Windows native)
# ---------------------------------------------------------------------------

def build_host(src, prefix, jobs, profile):
    require_tools(["cmake", "git"])
    build_dir = os.path.join(src, "build")
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    os.makedirs(build_dir)

    opts = cmake_options_for(profile, host_os=platform.system())
    extra = os.environ.get("PYFREERDP_EXTRA_CMAKE", "").split()

    cfg = ["cmake", "-S", src, "-B", build_dir,
           "-DCMAKE_INSTALL_PREFIX={0}".format(prefix)] + opts + extra
    if have("ninja"):
        cfg += ["-G", "Ninja"]

    run(cfg)
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


def collect_host_artifacts(prefix):
    sysname = platform.system()
    candidates = []
    if sysname == "Windows":
        for p in glob.glob(os.path.join(prefix, "bin", "*.dll")):
            candidates.append(p)
    else:
        for sub in ("lib", "lib64"):
            d = os.path.join(prefix, sub)
            if not os.path.isdir(d):
                continue
            ext = ".dylib" if sysname == "Darwin" else ".so"
            for p in glob.glob(os.path.join(d, "*{0}*".format(ext))):
                if os.path.isfile(p) or os.path.islink(p):
                    candidates.append(p)
    return candidates


def verify_artifacts(artifacts, profile):
    """
    Assert that every library family the profile promised actually exists.
    Prevents shipping a wheel where CMake silently dropped server support
    because a dep was missing.
    """
    names_lower = [os.path.basename(a).lower() for a in artifacts]
    missing = []
    for stem in EXPECTED_LIBS[profile]:
        if not any(stem in n for n in names_lower):
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
                profile, missing, sorted(set(names_lower))))


def install_into_package(artifacts):
    out = os.path.join(package_root(), "pyfreerdp", "_libs")
    if not os.path.isdir(out):
        os.makedirs(out)
    count = 0
    for src_path in artifacts:
        dst = os.path.join(out, os.path.basename(src_path))
        if os.path.islink(src_path):
            target = os.path.realpath(src_path)
            shutil.copy2(target, dst)
        else:
            shutil.copy2(src_path, dst)
        count += 1
        print("[install] {0}".format(dst))
    print("[install] {0} libraries staged into {1}".format(count, out))


# ---------------------------------------------------------------------------
# Linux dependency hint
# ---------------------------------------------------------------------------

LINUX_APT_DEPS = [
    "build-essential", "cmake", "ninja-build", "git", "pkg-config",
    "libssl-dev", "zlib1g-dev",
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
    "libX11-devel", "libXext-devel", "libXrandr-devel", "libXinerama-devel",
    "libXfixes-devel", "libXcursor-devel", "libXi-devel", "libXv-devel",
    "libxkbfile-devel", "libxkbcommon-devel",
    "wayland-devel", "wayland-protocols-devel",
    "alsa-lib-devel",
    "libpng-devel", "libjpeg-turbo-devel",
    "cairo-devel",
]


def print_linux_dep_hint(profile):
    if platform.system() != "Linux":
        return
    if have("apt-get"):
        print("\n[hint] On Debian/Ubuntu, the build needs roughly these packages:")
        print("    sudo apt-get install -y " + " ".join(LINUX_APT_DEPS))
    elif have("dnf"):
        print("\n[hint] On Fedora/RHEL, the build needs roughly these packages:")
        print("    sudo dnf install -y " + " ".join(LINUX_DNF_DEPS))


# ---------------------------------------------------------------------------
# Android cross-build
# ---------------------------------------------------------------------------

ANDROID_ABIS = ("arm64-v8a", "armeabi-v7a", "x86_64", "x86")


def build_android(src, abi, api_level, jobs, profile):
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

    opts = cmake_options_for(profile, host_os="Linux")
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

def build_ios(src, jobs, profile):
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
    opts = cmake_options_for(profile, host_os="Darwin")
    opts = [o for o in opts if not o.startswith("-DBUILD_SHARED_LIBS=")]
    opts.append("-DBUILD_SHARED_LIBS=OFF")  # static archive

    cfg = [
        "cmake", "-S", src, "-B", build_dir,
        "-DCMAKE_TOOLCHAIN_FILE={0}".format(toolchain),
        "-DPLATFORM=OS64",
        "-DCMAKE_INSTALL_PREFIX={0}".format(install_dir),
    ] + opts + ["-G", "Xcode"]
    run(cfg)
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
        print_linux_dep_hint(args.profile)
        prefix = os.path.abspath(
            args.prefix or os.path.join(tempfile.gettempdir(),
                                        "freerdp-prefix"))
        if not os.path.isdir(prefix):
            os.makedirs(prefix)
        build_host(src, prefix, args.jobs, args.profile)
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
        build_android(src, args.abi, args.api_level, args.jobs, args.profile)
        return 0

    if args.target == "ios":
        build_ios(src, args.jobs, args.profile)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Build the media / USB dependencies FreeRDP needs, per platform, into one
prefix that scripts/build_freerdp.py then consumes with --deps-prefix.

Components (all pinned, all verified by SHA-256):

    libusb    1.0.x   USB redirection (channel urbdrc) and the V4L camera
                      backend (channel rdpecam).
    OpenH264  2.x     FreeRDP's H.264 encoder/decoder (WITH_OPENH264) and
                      FFmpeg's libopenh264 encoder.
    FFmpeg    7.1.x   H.264 / MJPEG decode, the RDP audio codec set
                      (WITH_DSP_FFMPEG), pixel conversion (WITH_SWSCALE),
                      plus the ffmpeg / ffprobe executables.

Targets

    --target host       Linux and macOS build from source with the native
                        toolchain. Windows uses vcpkg in manifest mode
                        (vcpkg.json next to this script) because that is
                        the only sane way to build FFmpeg with MSVC for
                        x64, x86 and arm64 alike.
    --target android    Cross-builds with the NDK (--abi arm64-v8a|x86_64).
    --target ios        Static archives for OS64 / SIMULATORARM64. libusb
                        is skipped (iOS has no USB host API).

Output layout: <prefix>/{bin,lib,include,lib/pkgconfig}. Default prefix is
<repo>/build/deps/<label>, e.g. build/deps/linux-x86_64.

The FFmpeg build is deliberately not "everything": it is LGPL-only (no
--enable-gpl, so it can ship next to BSD OpenH264 and Apache FreeRDP), and
the component set is what FreeRDP and the validation suite need. The list
lives in FFMPEG_COMPONENTS and is easy to extend.

Style: Py2-compatible syntax. Runs on Python 3.
"""

from __future__ import print_function

import argparse
import hashlib
import multiprocessing
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile

try:
    from urllib.request import urlopen
except ImportError:  # Py2
    from urllib2 import urlopen


# ---------------------------------------------------------------------------
# Pinned sources
# ---------------------------------------------------------------------------

SOURCES = {
    "libusb": {
        "version": "1.0.30",
        # Release tarball (has ./configure; the git tag needs autogen).
        "url": "https://github.com/libusb/libusb/releases/download/"
               "v{version}/libusb-{version}.tar.bz2",
        "dirname": "libusb-{version}",
    },
    "openh264": {
        "version": "2.6.0",
        "url": "https://codeload.github.com/cisco/openh264/tar.gz/"
               "refs/tags/v{version}",
        "dirname": "openh264-{version}",
    },
    "ffmpeg": {
        "version": "7.1.5",
        "url": "https://codeload.github.com/FFmpeg/FFmpeg/tar.gz/"
               "refs/tags/n{version}",
        "dirname": "FFmpeg-n{version}",
    },
}

# Known-good hashes for the pinned versions above. Verified downloads only;
# set PYFREERDP_DEPS_SKIP_HASH=1 to bypass when bumping versions, then run
# with --print-hashes and paste the new values here.
KNOWN_HASHES = {
    "libusb-1.0.30": "fea36f34f9156400209595e300840767ab1a385ede1dc7ee893015aea9c6dbaf",
    "openh264-2.6.0": "558544ad358283a7ab2930d69a9ceddf913f4a51ee9bf1bfb9e377322af81a69",
    "ffmpeg-7.1.5": "e3963a50831c985933e1a625ed566ec4c7adb5c012c34fa9f84438e1d61bdacc",
}

# FFmpeg component selection - exactly what FreeRDP (libfreerdp/codec/
# dsp_ffmpeg.c, h264_ffmpeg.c, channels/rdpecam) uses, plus the bits the
# validation suite and the ffmpeg CLI need to exercise them.
FFMPEG_COMPONENTS = {
    "decoder": [
        # video ("wrapped_avframe" carries lavfi filter-graph output, i.e.
        # the testsrc2/sine sources used by the validation round trips)
        "h264", "mjpeg", "rawvideo", "wrapped_avframe",
        # RDP audio formats (dsp_ffmpeg.c)
        "aac", "aac_latm", "mp3", "gsm_ms", "adpcm_ms", "adpcm_ima_oki",
        "g723_1", "opus", "pcm_alaw", "pcm_mulaw", "pcm_u8", "pcm_u16le",
        "pcm_s16le", "pcm_s16be", "pcm_f32le",
    ],
    "encoder": [
        "libopenh264", "rawvideo", "mjpeg", "wrapped_avframe",
        # (no native GSM-MS encoder exists in FFmpeg; decode only)
        "aac", "adpcm_ms", "adpcm_ima_wav", "g723_1", "opus",
        "pcm_alaw", "pcm_mulaw", "pcm_u8", "pcm_u16le", "pcm_s16le",
        "pcm_s16be", "pcm_f32le",
    ],
    "parser": ["h264", "mjpeg", "aac", "aac_latm", "mpegaudio", "opus",
               "gsm"],
    "bsf": ["h264_mp4toannexb", "extract_extradata", "aac_adtstoasc"],
    "demuxer": ["h264", "mjpeg", "rawvideo", "mov", "matroska", "wav",
                "aac", "mp3", "ogg", "image2", "pcm_s16le",
                "pcm_alaw", "pcm_mulaw", "yuv4mpegpipe"],
    # lavfi is an input *device* (libavdevice): lets ffmpeg use filter
    # graphs (testsrc2, sine) as sources for the validation round trips.
    "indev": ["lavfi"],
    "muxer": ["h264", "mjpeg", "rawvideo", "mp4", "matroska", "wav", "adts",
              "ogg", "null", "image2", "pcm_s16le", "yuv4mpegpipe", "mp3"],
    "protocol": ["file", "pipe", "data"],
    "filter": ["scale", "format", "fps", "null", "anull", "aresample",
               "aformat", "testsrc", "testsrc2", "color", "sine", "anullsrc",
               "volume", "trim", "atrim", "setpts", "asetpts", "aevalsrc",
               "settb", "asettb", "copy", "acopy", "showinfo", "ashowinfo",
               "abuffer", "buffer"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg):
    print("[deps] {0}".format(msg))
    sys.stdout.flush()


def run(cmd, cwd=None, env=None, check=True, shell=False):
    printable = cmd if shell else " ".join(str(c) for c in cmd)
    print("\n$ {0}".format(printable))
    sys.stdout.flush()
    proc = subprocess.run(cmd, cwd=cwd, env=env, shell=shell)
    if check and proc.returncode != 0:
        raise SystemExit("Command failed (exit {0}): {1}".format(
            proc.returncode, printable))
    return proc.returncode


def have(tool):
    return shutil.which(tool) is not None


def require_tools(tools):
    missing = [t for t in tools if not have(t)]
    if missing:
        raise SystemExit("Missing required tools: {0}".format(missing))


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, dest):
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return dest
    log("downloading {0}".format(url))
    tmp = dest + ".part"
    resp = urlopen(url)
    with open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    os.rename(tmp, dest)
    return dest


def fetch_source(name, work):
    """Download + verify + extract one component. Returns the source dir."""
    spec = SOURCES[name]
    version = spec["version"]
    url = spec["url"].format(version=version)
    fname = os.path.basename(url)
    if not fname.endswith((".gz", ".bz2", ".xz")):
        fname = "{0}-{1}.tar.gz".format(name, version)
    archive = download(url, os.path.join(work, fname))

    digest = sha256_file(archive)
    expected = KNOWN_HASHES.get("{0}-{1}".format(name, version))
    if os.environ.get("PYFREERDP_DEPS_SKIP_HASH") == "1":
        log("{0}: sha256 {1} (verification skipped)".format(name, digest))
    elif expected is None:
        log("{0}: sha256 {1} (no pinned hash for this version; add it to "
            "KNOWN_HASHES)".format(name, digest))
    elif digest != expected:
        raise SystemExit("{0}: SHA-256 mismatch\n  got      {1}\n  "
                         "expected {2}".format(name, digest, expected))
    else:
        log("{0}: sha256 OK".format(name))

    srcdir = os.path.join(work, spec["dirname"].format(version=version))
    if os.path.isdir(srcdir):
        shutil.rmtree(srcdir)
    with tarfile.open(archive) as tf:
        tf.extractall(work)
    if not os.path.isdir(srcdir):
        # codeload archives are named <repo>-<tag-without-v>; be lenient
        cands = [d for d in os.listdir(work)
                 if os.path.isdir(os.path.join(work, d))
                 and d.lower().startswith(name.replace("ffmpeg", "ffmpeg"))]
        if not cands:
            raise SystemExit("could not locate extracted {0}".format(name))
        srcdir = os.path.join(work, sorted(cands)[-1])
    return srcdir


def print_hashes(work):
    for name in SOURCES:
        spec = SOURCES[name]
        url = spec["url"].format(version=spec["version"])
        fname = os.path.basename(url)
        if not fname.endswith((".gz", ".bz2", ".xz")):
            fname = "{0}-{1}.tar.gz".format(name, spec["version"])
        archive = download(url, os.path.join(work, fname))
        print('    "{0}-{1}": "{2}",'.format(name, spec["version"],
                                              sha256_file(archive)))


def ncpu():
    try:
        return multiprocessing.cpu_count()
    except NotImplementedError:
        return 2


def label_for(target, arch=None, abi=None, ios_platform=None):
    if target == "android":
        return "android-{0}".format(abi)
    if target == "ios":
        return "ios-{0}".format(ios_platform)
    sysname = platform.system()
    if sysname == "Windows":
        return "windows-{0}".format(arch or "x64")
    m = platform.machine().lower()
    m = {"amd64": "x86_64", "arm64": "arm64", "aarch64": "aarch64"}.get(m, m)
    return "{0}-{1}".format(sysname.lower(), m)


# ---------------------------------------------------------------------------
# Toolchain descriptions
# ---------------------------------------------------------------------------

class Toolchain(object):
    """
    Everything the three build systems (autotools for libusb/FFmpeg,
    plain make for OpenH264) need to know about the target.
    """

    def __init__(self, target, prefix, jobs, abi=None, api_level=24,
                 ios_platform="OS64"):
        self.target = target
        self.prefix = prefix
        self.jobs = jobs
        self.abi = abi
        self.api_level = api_level
        self.ios_platform = ios_platform
        self.host_os = platform.system()
        self.env = dict(os.environ)
        self.env["PKG_CONFIG_PATH"] = os.pathsep.join(
            [os.path.join(prefix, "lib", "pkgconfig"),
             os.path.join(prefix, "lib64", "pkgconfig")]
            + ([self.env["PKG_CONFIG_PATH"]]
               if self.env.get("PKG_CONFIG_PATH") else []))
        # For cross builds pkg-config must not fall back to host .pc files.
        if target in ("android", "ios"):
            self.env["PKG_CONFIG_LIBDIR"] = self.env["PKG_CONFIG_PATH"]
            self.env["PKG_CONFIG_SYSROOT_DIR"] = ""
        self.shared = target != "ios"
        self._setup()

    # -- per-target ---------------------------------------------------------

    def _setup(self):
        if self.target == "android":
            ndk = (os.environ.get("ANDROID_NDK_ROOT")
                   or os.environ.get("ANDROID_NDK_HOME")
                   or os.environ.get("ANDROID_NDK_LATEST_HOME"))
            if not ndk or not os.path.isdir(ndk):
                raise SystemExit("Set ANDROID_NDK_ROOT to your NDK.")
            self.ndk = ndk
            host_tag = {"Linux": "linux-x86_64",
                        "Darwin": "darwin-x86_64"}[self.host_os]
            self.tc = os.path.join(ndk, "toolchains", "llvm", "prebuilt",
                                   host_tag)
            self.sysroot = os.path.join(self.tc, "sysroot")
            triple = {"arm64-v8a": "aarch64-linux-android",
                      "x86_64": "x86_64-linux-android"}[self.abi]
            self.triple = triple
            b = os.path.join(self.tc, "bin")
            self.cc = os.path.join(b, "{0}{1}-clang".format(
                triple, self.api_level))
            self.cxx = self.cc + "++"
            self.ar = os.path.join(b, "llvm-ar")
            self.ranlib = os.path.join(b, "llvm-ranlib")
            self.strip = os.path.join(b, "llvm-strip")
            self.nm = os.path.join(b, "llvm-nm")
            self.env.update({"CC": self.cc, "CXX": self.cxx, "AR": self.ar,
                             "RANLIB": self.ranlib, "STRIP": self.strip,
                             "PATH": b + os.pathsep + self.env["PATH"]})
            self.cflags = ["-fPIC", "-O2"]
        elif self.target == "ios":
            if self.host_os != "Darwin":
                raise SystemExit("iOS builds need macOS + Xcode")
            sim = self.ios_platform.startswith("SIMULATOR")
            self.sdk = "iphonesimulator" if sim else "iphoneos"
            self.sysroot = subprocess.check_output(
                ["xcrun", "--sdk", self.sdk, "--show-sdk-path"]).decode().strip()
            self.cc = subprocess.check_output(
                ["xcrun", "--sdk", self.sdk, "--find", "clang"]).decode().strip()
            self.cxx = self.cc + "++"
            self.arch = "x86_64" if self.ios_platform == "SIMULATOR64" else "arm64"
            minflag = ("-mios-simulator-version-min=13.0" if sim
                       else "-mios-version-min=13.0")
            self.cflags = ["-arch", self.arch, "-isysroot", self.sysroot,
                           minflag, "-fPIC", "-O2", "-fembed-bitcode=off"]
            self.env.update({
                "CC": self.cc, "CXX": self.cxx,
                "CFLAGS": " ".join(self.cflags),
                "CXXFLAGS": " ".join(self.cflags),
                "LDFLAGS": " ".join(["-arch", self.arch, "-isysroot",
                                     self.sysroot, minflag]),
            })
        else:  # host
            self.cflags = ["-fPIC", "-O2"]
            if self.host_os == "Darwin":
                self.cflags += ["-mmacosx-version-min=11.0"]
                self.env["MACOSX_DEPLOYMENT_TARGET"] = "11.0"

    # -- OpenH264 make variables --------------------------------------------

    def openh264_make_vars(self):
        v = ["PREFIX={0}".format(self.prefix)]
        if self.target == "android":
            arch = {"arm64-v8a": "arm64", "x86_64": "x86_64"}[self.abi]
            v += ["OS=android", "NDKROOT={0}".format(self.ndk),
                  "TARGET=android-{0}".format(self.api_level),
                  "ARCH={0}".format(arch),
                  "NDKLEVEL={0}".format(self.api_level)]
        elif self.target == "ios":
            v += ["OS=ios", "ARCH={0}".format(self.arch)]
            if self.ios_platform.startswith("SIMULATOR"):
                v += ["SDK_MIN=13.0", "SDKROOT={0}".format(self.sysroot)]
        else:
            m = platform.machine().lower()
            arch = {"x86_64": "x86_64", "amd64": "x86_64",
                    "aarch64": "arm64", "arm64": "arm64"}.get(m, m)
            v += ["ARCH={0}".format(arch)]
            if self.host_os == "Darwin":
                v += ["OS=darwin"]
        if not have("nasm") and self.target != "android":
            v += ["USE_ASM=No"]  # slower but correct
        return v

    # -- FFmpeg configure flags --------------------------------------------

    def ffmpeg_configure_flags(self):
        f = ["--prefix={0}".format(self.prefix),
             "--disable-doc", "--disable-debug", "--enable-pic",
             "--disable-ffplay", "--enable-libopenh264",
             # Deterministic, dependency-free build:
             "--disable-autodetect", "--enable-pthreads",
             "--pkg-config-flags=--static" if not self.shared else
             "--pkg-config=pkg-config",
             "--disable-everything"]
        for kind, items in FFMPEG_COMPONENTS.items():
            for it in items:
                f.append("--enable-{0}={1}".format(kind, it))
        if self.shared:
            f += ["--enable-shared", "--disable-static",
                  "--enable-ffmpeg", "--enable-ffprobe"]
        else:
            f += ["--enable-static", "--disable-shared", "--disable-programs"]
        if self.target == "android":
            arch = {"arm64-v8a": "aarch64", "x86_64": "x86_64"}[self.abi]
            f += ["--enable-cross-compile", "--target-os=android",
                  "--arch={0}".format(arch),
                  "--cc={0}".format(self.cc), "--cxx={0}".format(self.cxx),
                  "--ar={0}".format(self.ar), "--ranlib={0}".format(self.ranlib),
                  "--nm={0}".format(self.nm), "--strip={0}".format(self.strip),
                  "--sysroot={0}".format(self.sysroot),
                  "--disable-programs",     # no place to run them on-device
                  "--enable-jni", "--enable-mediacodec",
                  "--extra-cflags=-fPIC -O2",
                  "--extra-ldflags=-Wl,-z,max-page-size=16384"]
            if arch == "aarch64":
                f += ["--cpu=armv8-a"]
            else:
                f += ["--disable-asm"]  # x86_64 asm needs nasm cross setup
        elif self.target == "ios":
            f += ["--enable-cross-compile", "--target-os=darwin",
                  "--arch={0}".format(self.arch),
                  "--cc={0}".format(self.cc),
                  "--sysroot={0}".format(self.sysroot),
                  "--extra-cflags={0}".format(" ".join(self.cflags)),
                  "--extra-ldflags={0}".format(self.env["LDFLAGS"]),
                  "--disable-audiotoolbox", "--disable-videotoolbox"]
            if self.arch == "arm64":
                f += ["--disable-asm"]  # avoid needing gas-preprocessor.pl
        else:
            if self.host_os == "Darwin":
                f += ["--extra-cflags=-mmacosx-version-min=11.0",
                      "--extra-ldflags=-mmacosx-version-min=11.0",
                      "--install-name-dir=@rpath"]
            # (rpaths are set afterwards with patchelf / install_name_tool in
            # fix_prefix_rpaths(): "$ORIGIN" does not survive FFmpeg's
            # configure -> config.mak -> make expansion.)
            if not have("nasm") and not have("yasm"):
                f += ["--disable-x86asm"]
        return f

    # -- libusb configure flags --------------------------------------------

    def libusb_configure_flags(self):
        f = ["--prefix={0}".format(self.prefix), "--disable-examples-build",
             "--disable-tests-build"]
        if self.shared:
            f += ["--enable-shared", "--disable-static"]
        else:
            f += ["--enable-static", "--disable-shared"]
        if self.target == "android":
            f += ["--host={0}".format(self.triple), "--disable-udev"]
        elif self.host_os == "Linux":
            f += ["--enable-udev"] if os.path.isfile(
                "/usr/include/libudev.h") else ["--disable-udev"]
        return f


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def build_libusb(tc, work):
    if tc.target == "ios":
        log("libusb: skipped on iOS (no USB host API)")
        return
    src = fetch_source("libusb", work)
    env = dict(tc.env)
    if tc.target == "host":
        env["CFLAGS"] = " ".join(tc.cflags)
    run(["./configure"] + tc.libusb_configure_flags(), cwd=src, env=env)
    run(["make", "-j{0}".format(tc.jobs)], cwd=src, env=env)
    run(["make", "install"], cwd=src, env=env)
    # Build the listdevs example for validation (make install skips it).
    ex = os.path.join(src, "examples")
    if tc.target == "host" and os.path.isfile(os.path.join(ex, "listdevs.c")):
        bindir = os.path.join(tc.prefix, "bin")
        if not os.path.isdir(bindir):
            os.makedirs(bindir)
        cc = env.get("CC", "cc")
        cmd = [cc] + tc.cflags + ["-I" + os.path.join(tc.prefix, "include",
                                                      "libusb-1.0"),
                                  os.path.join(ex, "listdevs.c"), "-o",
                                  os.path.join(bindir, "listdevs"),
                                  "-L" + os.path.join(tc.prefix, "lib"),
                                  "-lusb-1.0"]
        run(cmd, env=env)


def build_openh264(tc, work):
    src = fetch_source("openh264", work)
    env = dict(tc.env)
    if tc.target == "android":
        # OpenH264's android platform makefile drives the NDK itself.
        for k in ("CC", "CXX", "AR", "RANLIB", "STRIP"):
            env.pop(k, None)
    make_vars = tc.openh264_make_vars()
    run(["make", "-j{0}".format(tc.jobs)] + make_vars, cwd=src, env=env)
    if tc.shared:
        run(["make", "install-shared"] + make_vars, cwd=src, env=env)
    else:
        run(["make", "install-static"] + make_vars, cwd=src, env=env)
    # Executables (h264enc / h264dec) are not part of `make install`.
    if tc.target == "host":
        bindir = os.path.join(tc.prefix, "bin")
        if not os.path.isdir(bindir):
            os.makedirs(bindir)
        for exe in ("h264enc", "h264dec"):
            p = os.path.join(src, exe)
            if os.path.isfile(p):
                shutil.copy2(p, os.path.join(bindir, exe))
        # Encoder config files the h264enc executable needs.
        share = os.path.join(tc.prefix, "share", "openh264")
        if not os.path.isdir(share):
            os.makedirs(share)
        for fn in os.listdir(os.path.join(src, "res")):
            if fn.endswith((".cfg", ".txt")):
                shutil.copy2(os.path.join(src, "res", fn),
                             os.path.join(share, fn))
    if tc.host_os == "Darwin" and tc.shared:
        # Give the dylib an @rpath install name like FFmpeg's.
        import glob as _glob
        for dylib in _glob.glob(os.path.join(tc.prefix, "lib",
                                             "libopenh264*.dylib")):
            if not os.path.islink(dylib):
                run(["install_name_tool", "-id",
                     "@rpath/" + os.path.basename(dylib), dylib])


def build_ffmpeg(tc, work):
    src = fetch_source("ffmpeg", work)
    env = dict(tc.env)
    flags = tc.ffmpeg_configure_flags()
    run(["./configure"] + flags, cwd=src, env=env)
    run(["make", "-j{0}".format(tc.jobs)], cwd=src, env=env)
    run(["make", "install"], cwd=src, env=env)


# ---------------------------------------------------------------------------
# Windows: vcpkg manifest
# ---------------------------------------------------------------------------

def build_windows_vcpkg(prefix, arch, jobs, profile="full"):
    vcpkg_root = (os.environ.get("VCPKG_ROOT")
                  or os.environ.get("VCPKG_INSTALLATION_ROOT"))
    if not vcpkg_root:
        raise SystemExit("Set VCPKG_ROOT (or run on a GitHub Windows runner).")
    exe = os.path.join(vcpkg_root, "vcpkg.exe")
    triplet = {"x64": "x64-windows", "x86": "x86-windows",
               "arm64": "arm64-windows"}[arch]
    manifest_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isfile(os.path.join(manifest_dir, "vcpkg.json")):
        raise SystemExit("vcpkg.json missing next to build_deps.py")
    installed = os.path.join(prefix, "vcpkg_installed")
    env = dict(os.environ)
    env.setdefault("VCPKG_MAX_CONCURRENCY", str(jobs))
    cmd = [exe, "install", "--triplet", triplet,
           "--x-manifest-root={0}".format(manifest_dir),
           "--x-install-root={0}".format(installed),
           "--clean-after-build"]
    if profile == "full":
        cmd += ["--x-feature=media", "--x-feature=sdl"]
    run(cmd, env=env)
    # Flatten to the same <prefix>/{bin,lib,include} shape as source builds
    # so build_freerdp.py can treat all targets alike.
    tri = os.path.join(installed, triplet)
    for sub in ("bin", "lib", "include", "share"):
        s = os.path.join(tri, sub)
        d = os.path.join(prefix, sub)
        if os.path.isdir(s):
            if os.path.isdir(d):
                shutil.rmtree(d)
            shutil.copytree(s, d)
    # vcpkg puts executables under tools/<port>/.
    bindir = os.path.join(prefix, "bin")
    if not os.path.isdir(bindir):
        os.makedirs(bindir)
    tools = os.path.join(tri, "tools")
    if os.path.isdir(tools):
        for root, _dirs, files in os.walk(tools):
            for fn in files:
                if fn.lower().endswith(".exe"):
                    shutil.copy2(os.path.join(root, fn),
                                 os.path.join(bindir, fn))
    log("vcpkg deps flattened into {0}".format(prefix))


# ---------------------------------------------------------------------------
# Manifest + summary
# ---------------------------------------------------------------------------

def _ensure_patchelf():
    if have("patchelf"):
        return "patchelf"
    log("patchelf not on PATH - installing the PyPI package")
    for extra in ([], ["--break-system-packages"], ["--user"]):
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                   "--quiet", "patchelf"] + extra,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
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
    raise SystemExit("patchelf is required (pip install patchelf).")


def fix_prefix_rpaths(prefix, target):
    """
    Make the prefix self-contained at runtime: every shared library looks
    for its siblings next to itself, every executable in bin/ looks in
    ../lib. Linux via patchelf ($ORIGIN), macOS via install_name_tool
    (@loader_path / @executable_path). Static (iOS) and Android (no
    executables, libraries loaded from one app dir) need nothing.
    """
    if target != "host":
        return
    sysname = platform.system()
    libdir = os.path.join(prefix, "lib")
    bindir = os.path.join(prefix, "bin")
    if sysname == "Linux":
        pe = _ensure_patchelf()
        n = 0
        for fn in os.listdir(libdir):
            p = os.path.join(libdir, fn)
            if ".so" in fn and os.path.isfile(p) and not os.path.islink(p):
                subprocess.check_call([pe, "--set-rpath", "$ORIGIN", p]); n += 1
        if os.path.isdir(bindir):
            for fn in os.listdir(bindir):
                p = os.path.join(bindir, fn)
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    subprocess.check_call([pe, "--set-rpath", "$ORIGIN/../lib", p])
                    n += 1
        log("rpaths fixed on {0} files ($ORIGIN)".format(n))
    elif sysname == "Darwin":
        n = 0
        for fn in os.listdir(libdir):
            p = os.path.join(libdir, fn)
            if fn.endswith(".dylib") and os.path.isfile(p) and not os.path.islink(p):
                subprocess.check_call(["install_name_tool", "-id",
                                       "@rpath/" + fn, p])
                out = subprocess.check_output(["otool", "-L", p]).decode()
                for line in out.splitlines()[1:]:
                    ref = line.strip().split(" (")[0]
                    if ref.startswith(prefix):
                        subprocess.check_call(["install_name_tool", "-change", ref,
                                               "@rpath/" + os.path.basename(ref), p])
                if "@loader_path" not in subprocess.check_output(
                        ["otool", "-l", p]).decode():
                    subprocess.check_call(["install_name_tool", "-add_rpath",
                                           "@loader_path", p])
                n += 1
        if os.path.isdir(bindir):
            for fn in os.listdir(bindir):
                p = os.path.join(bindir, fn)
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    out = subprocess.check_output(["otool", "-L", p]).decode()
                    for line in out.splitlines()[1:]:
                        ref = line.strip().split(" (")[0]
                        if ref.startswith(prefix):
                            subprocess.check_call(["install_name_tool", "-change", ref,
                                                   "@rpath/" + os.path.basename(ref), p])
                    if "@executable_path/../lib" not in subprocess.check_output(
                            ["otool", "-l", p]).decode():
                        subprocess.check_call(["install_name_tool", "-add_rpath",
                                               "@executable_path/../lib", p])
                    n += 1
        log("install names / rpaths fixed on {0} files".format(n))


def write_manifest(prefix, label, built):
    lines = ["# pyfreerdp dependency prefix", "label={0}".format(label)]
    for name in built:
        lines.append("{0}={1}".format(name, SOURCES[name]["version"]))
    with open(os.path.join(prefix, "DEPS-MANIFEST.txt"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=("host", "android", "ios"),
                   default="host")
    p.add_argument("--arch", default="x64", choices=("x64", "x86", "arm64"),
                   help="Windows only")
    p.add_argument("--abi", default="arm64-v8a", choices=("arm64-v8a", "x86_64"))
    p.add_argument("--api-level", type=int, default=24)
    p.add_argument("--ios-platform", default="OS64",
                   choices=("OS64", "SIMULATOR64", "SIMULATORARM64"))
    p.add_argument("--prefix", help="install prefix (default build/deps/<label>)")
    p.add_argument("--work", help="download/extract dir (default: temp)")
    p.add_argument("--jobs", type=int, default=ncpu())
    p.add_argument("--only", help="comma list of components: libusb,openh264,ffmpeg")
    p.add_argument("--profile", choices=("minimal", "full"), default="full",
                   help="full (default): everything. minimal: only what the "
                        "size-optimised library build needs - on Windows that "
                        "is OpenSSL/zlib/cJSON from vcpkg; on other platforms "
                        "nothing (FreeRDP's minimal build uses system OpenSSL).")
    p.add_argument("--print-hashes", action="store_true",
                   help="download the pinned tarballs and print their sha256")
    args = p.parse_args()

    label = label_for(args.target, args.arch, args.abi, args.ios_platform)
    prefix = os.path.abspath(args.prefix or os.path.join(
        repo_root(), "build", "deps", label))
    work = os.path.abspath(args.work or os.path.join(
        tempfile.gettempdir(), "pyfreerdp-deps-src"))
    for d in (prefix, work):
        if not os.path.isdir(d):
            os.makedirs(d)

    if args.print_hashes:
        print_hashes(work)
        return 0

    log("target={0} label={1} prefix={2} jobs={3}".format(
        args.target, label, prefix, args.jobs))

    if args.target == "host" and platform.system() == "Windows":
        build_windows_vcpkg(prefix, args.arch, args.jobs, args.profile)
        write_manifest(prefix, label, [])
        return 0
    if args.profile == "minimal":
        log("profile minimal: no source dependencies to build on this "
            "platform (system OpenSSL is used); nothing to do")
        write_manifest(prefix, label + "-minimal", [])
        return 0

    require_tools(["make", "pkg-config"])
    if args.target == "host" and platform.system() == "Linux":
        require_tools(["cc"])
    only = set(args.only.split(",")) if args.only else {"libusb", "openh264",
                                                         "ffmpeg"}
    tc = Toolchain(args.target, prefix, args.jobs, abi=args.abi,
                   api_level=args.api_level, ios_platform=args.ios_platform)
    built = []
    # Order matters: FFmpeg links libopenh264 via pkg-config.
    if "libusb" in only:
        build_libusb(tc, work)
        built.append("libusb")
    if "openh264" in only:
        build_openh264(tc, work)
        built.append("openh264")
    if "ffmpeg" in only:
        build_ffmpeg(tc, work)
        built.append("ffmpeg")
    fix_prefix_rpaths(prefix, args.target)
    write_manifest(prefix, label, built)
    log("done: {0}".format(", ".join(built)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

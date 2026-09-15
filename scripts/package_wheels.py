#!/usr/bin/env python3
"""
Package the build-freerdp artifacts into installable wheels.

    python scripts/package_wheels.py artifacts/ -o dist/

`artifacts/` is where you unpacked the build-freerdp run (one directory or
archive per platform, named like
freerdp-3.31.1-linux-x86_64-full-media). Each becomes ONE wheel that works
on every Python version, because pyfreerdp loads the libraries with ctypes
rather than linking against libpython - so the wheels are tagged
`py3-none-<platform>` instead of `cp312-cp312-<platform>`.

Android and iOS are deliberately not turned into wheels: those platforms
install code as part of an app bundle, not with pip. They are copied to the
output directory as archives instead.

Style: Py2-compatible syntax. Runs on Python 3.
"""

from __future__ import print_function

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile


# Artifact platform name -> wheel platform tag.
#
# manylinux_2_28 matches the Ubuntu 24.04 runners' glibc floor loosely; if you
# build on a different image, check with `auditwheel show` and adjust. macOS
# tags carry the deployment target the libraries were built with (11.0).
PLATFORM_TAGS = {
    "linux-x86_64": "manylinux_2_28_x86_64",
    "linux-aarch64": "manylinux_2_28_aarch64",
    "macos-arm64": "macosx_11_0_arm64",
    "macos-x86_64": "macosx_11_0_x86_64",
    "windows-x64": "win_amd64",
    "windows-x86": "win32",
    "windows-arm64": "win_arm64",
}

# Mobile tags are defined by PEP 738 (Android) and PEP 730 (iOS):
#   android_<api>_<abi>              e.g. android_24_arm64_v8a
#   ios_<major>_<minor>_<arch>_<sdk> e.g. ios_13_0_arm64_iphoneos
# Requires CPython 3.13+ and a recent pip, and you install them with a
# cross-install (see --mobile below), not onto a running device.
ANDROID_ABIS = {
    "android-arm64-v8a": "arm64_v8a",
    "android-armeabi-v7a": "armeabi_v7a",
    "android-x86_64": "x86_64",
    "android-x86": "x86",
}
IOS_SDKS = {
    "ios-arm64": ("arm64", "iphoneos"),
    "ios-simulator-arm64": ("arm64", "iphonesimulator"),
    "ios-simulator-x86_64": ("x86_64", "iphonesimulator"),
}


def log(msg):
    print("[package] {0}".format(msg))
    sys.stdout.flush()


def run(cmd, cwd=None):
    print("\n$ {0}".format(" ".join(cmd)))
    sys.stdout.flush()
    rc = subprocess.call(cmd, cwd=cwd)
    if rc != 0:
        raise SystemExit("command failed ({0})".format(rc))


def unpack(src, dest):
    """Return a directory holding _libs/ for this artifact."""
    if os.path.isdir(src):
        # Either <dir>/_libs or <dir>/<archive>
        if os.path.isdir(os.path.join(src, "_libs")):
            return src
        for pattern in ("*.tar.gz", "*.zip"):
            hits = glob.glob(os.path.join(src, pattern))
            if hits:
                return unpack(hits[0], dest)
        raise SystemExit("no _libs or archive in {0}".format(src))
    os.makedirs(dest)
    if src.endswith((".tar.gz", ".tgz")):
        with tarfile.open(src) as tf:
            tf.extractall(dest)
    elif src.endswith(".zip"):
        with zipfile.ZipFile(src) as zf:
            zf.extractall(dest)
    else:
        raise SystemExit("unknown archive type: {0}".format(src))
    if os.path.isdir(os.path.join(dest, "_libs")):
        return dest
    inner = [os.path.join(dest, d) for d in os.listdir(dest)
             if os.path.isdir(os.path.join(dest, d, "_libs"))]
    if inner:
        return inner[0]
    raise SystemExit("no _libs inside {0}".format(src))


def platform_of(name):
    """Longest matching platform key in an artifact name."""
    best = None
    for key in PLATFORM_TAGS:
        if key in name and (best is None or len(key) > len(best)):
            best = key
    return best


def mobile_tag(name, android_api, ios_version):
    """PEP 738 / PEP 730 platform tag for a mobile artifact, or None."""
    best = None
    for key in ANDROID_ABIS:
        if key in name and (best is None or len(key) > len(best)):
            best = key
    if best:
        return "android_{0}_{1}".format(android_api, ANDROID_ABIS[best])
    best = None
    for key in IOS_SDKS:
        if key in name and (best is None or len(key) > len(best)):
            best = key
    if best:
        arch, sdk = IOS_SDKS[best]
        return "ios_{0}_{1}_{2}".format(
            ios_version.replace(".", "_"), arch, sdk)
    return None


def build_base_wheel(repo, workdir):
    """Build the ordinary pure-Python wheel once; it is retagged per platform."""
    run([sys.executable, "-m", "build", "--wheel", "--outdir", workdir], cwd=repo)
    hits = glob.glob(os.path.join(workdir, "*-py3-none-any.whl"))
    if not hits:
        hits = glob.glob(os.path.join(workdir, "*.whl"))
    if not hits:
        raise SystemExit("no wheel produced")
    return hits[0]


def wheel_with_libs(base_wheel, libs_dir, bin_dir, tag, outdir):
    """
    Insert _libs (and optionally _bin) into a copy of the wheel and retag it.

    Uses `wheel unpack` / `wheel pack` rather than appending to the zip:
    pack regenerates RECORD with hashes for the new files, which `wheel
    tags` (and pip's install-time verification) require.
    """
    tmp = tempfile.mkdtemp(prefix="whl-")
    run([sys.executable, "-m", "wheel", "unpack", base_wheel, "-d", tmp])
    roots = [os.path.join(tmp, d) for d in os.listdir(tmp)
             if os.path.isdir(os.path.join(tmp, d))]
    if len(roots) != 1:
        raise SystemExit("unexpected unpack layout: {0}".format(roots))
    root = roots[0]

    for src_dir, arc_root in ((libs_dir, "_libs"), (bin_dir, "_bin")):
        if not src_dir or not os.path.isdir(src_dir):
            continue
        dest = os.path.join(root, "pyfreerdp", arc_root)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        # copy2 keeps the executable bit; symlinks are followed so the wheel
        # stays self-contained (zip has no portable symlink support).
        shutil.copytree(src_dir, dest, symlinks=False)

    packed = tempfile.mkdtemp(prefix="whl-out-")
    # Root-Is-Purelib must be false: the wheel carries platform binaries, so
    # its contents belong in platlib. `wheel pack` copies the flag from the
    # WHEEL file, so fix it there before packing.
    for dist_info in glob.glob(os.path.join(root, "*.dist-info")):
        wheel_meta = os.path.join(dist_info, "WHEEL")
        if os.path.isfile(wheel_meta):
            with open(wheel_meta) as fh:
                text = fh.read()
            text = text.replace("Root-Is-Purelib: true", "Root-Is-Purelib: false")
            with open(wheel_meta, "w") as fh:
                fh.write(text)
    run([sys.executable, "-m", "wheel", "pack", root, "-d", packed])
    built = glob.glob(os.path.join(packed, "*.whl"))
    if not built:
        raise SystemExit("wheel pack produced nothing")

    run([sys.executable, "-m", "wheel", "tags",
         "--platform-tag", tag, "--remove", built[0]])
    final = glob.glob(os.path.join(packed, "*.whl"))
    if not final:
        raise SystemExit("retagging produced no wheel")
    dest = os.path.join(outdir, os.path.basename(final[0]))
    shutil.move(final[0], dest)
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(packed, ignore_errors=True)
    return dest


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("artifacts", help="directory of unpacked/downloaded artifacts")
    p.add_argument("-o", "--outdir", default="dist")
    p.add_argument("--repo", default=os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    p.add_argument("--with-executables", action="store_true",
                   help="also ship _bin/ inside the wheels (much larger)")
    p.add_argument("--mobile", choices=("wheel", "archive", "both"),
                   default="both",
                   help="how to emit Android/iOS artifacts: PEP 738/730 "
                        "wheels, plain archives to bundle into an app, or "
                        "both (default).")
    p.add_argument("--android-api", default="24",
                   help="API level in the android_<api>_<abi> tag; must match "
                        "--api-level of the build (default 24).")
    p.add_argument("--ios-version", default="13.0",
                   help="minimum iOS version in the ios_<ver>_<arch>_<sdk> "
                        "tag (default 13.0).")
    args = p.parse_args()

    outdir = os.path.abspath(args.outdir)
    if not os.path.isdir(outdir):
        os.makedirs(outdir)

    entries = sorted(glob.glob(os.path.join(args.artifacts, "*")))
    if not entries:
        raise SystemExit("nothing in {0}".format(args.artifacts))

    work = tempfile.mkdtemp(prefix="pkg-")
    base = build_base_wheel(args.repo, work)
    log("base wheel: {0}".format(os.path.basename(base)))

    made, skipped = [], []
    for entry in entries:
        name = os.path.basename(entry)
        if name.endswith(".whl"):
            continue
        mtag = mobile_tag(name, args.android_api, args.ios_version)
        if mtag:
            if args.mobile in ("archive", "both"):
                dest = os.path.join(outdir, name)
                if os.path.isdir(entry):
                    shutil.make_archive(dest, "gztar", entry)
                else:
                    shutil.copy2(entry, dest)
                skipped.append(name)
            if args.mobile in ("wheel", "both"):
                root = unpack(entry, os.path.join(work, name + ".m"))
                whl = wheel_with_libs(base, os.path.join(root, "_libs"),
                                      None, mtag, outdir)
                log("{0} -> {1}".format(name, os.path.basename(whl)))
                made.append(whl)
            continue
        plat = platform_of(name)
        if not plat:
            log("skipping {0} (unknown platform)".format(name))
            continue
        root = unpack(entry, os.path.join(work, name + ".x"))
        libs = os.path.join(root, "_libs")
        bins = os.path.join(root, "_bin") if args.with_executables else None
        whl = wheel_with_libs(base, libs, bins, PLATFORM_TAGS[plat], outdir)
        log("{0} -> {1}".format(name, os.path.basename(whl)))
        made.append(whl)

    # The sdist lets people build from source on platforms with no wheel.
    run([sys.executable, "-m", "build", "--sdist", "--outdir", outdir],
        cwd=args.repo)

    print("\n=== wheels")
    for w in made:
        print("  {0}  ({1:.1f} MB)".format(os.path.basename(w),
                                           os.path.getsize(w) / 1048576.0))
    if skipped:
        print("=== mobile archives (unpack into jniLibs/ or the .app bundle)")
        for s in skipped:
            print("  {0}".format(s))
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()

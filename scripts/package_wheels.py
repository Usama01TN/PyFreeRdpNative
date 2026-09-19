#!/usr/bin/env python3
"""
Package the build-freerdp artifacts into installable wheels.

    python scripts/package_wheels.py artifacts/ -o dist/

`artifacts/` is where you unpacked the build-freerdp run (one directory or
archive per platform, named like
freerdp-3.31.1-linux-x86_64-full-media). Each becomes ONE wheel that works
on every Python version, because pyfreerdpnative loads the libraries with ctypes
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

# Bumped whenever the CLI changes; the workflow asks for the version it was
# written against so a stale copy fails with an explanation rather than
# "unrecognized arguments".
PACKAGE_SCRIPT_VERSION = 4


# Artifact platform name -> wheel platform tag.
#
# Linux is built twice:
#   * on Ubuntu 24.04 (glibc 2.39) -> manylinux_2_39, the newest toolchain;
#   * in the manylinux_2_34 container (AlmaLinux 9, glibc 2.34) -> also
#     installs on RHEL/Alma 9, Debian 12 (2.36), Ubuntu 22.04 (2.35);
#   * in the manylinux_2_28 container (AlmaLinux 8, glibc 2.28) -> the lowest
#     floor pypa maintains: RHEL/Alma 8+, Ubuntu 18.04+, Debian 10+, SLES 15.
#   * in the manylinux2014 container (CentOS 7, glibc 2.17) -> the widest
#     reach a glibc wheel can have, covering distributions back to 2014.
# pip prefers the highest tag a system satisfies, so a 2.39 machine still
# gets the 2.39 wheel. Alpine (musl) is NOT covered by any of these; that
# needs musllinux wheels built in an Alpine toolchain.
# `auditwheel show` confirms each floor. Non-baseline system libraries
# (cJSON, ICU, OpenSSL, krb5, ...) are vendored into _libs at build time, so
# the wheel depends on nothing but glibc and X11. macOS tags carry the
# deployment target the libraries were built with (11.0).
PLATFORM_TAGS = {
    "linux-x86_64": "manylinux_2_39_x86_64",
    "linux-aarch64": "manylinux_2_39_aarch64",
    "linux-x86_64-glibc234": "manylinux_2_34_x86_64",
    "linux-aarch64-glibc234": "manylinux_2_34_aarch64",
    "linux-x86_64-glibc228": "manylinux_2_28_x86_64",
    "linux-aarch64-glibc228": "manylinux_2_28_aarch64",
    "linux-x86_64-glibc217": "manylinux2014_x86_64",
    "linux-aarch64-glibc217": "manylinux2014_aarch64",
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


# A build run produces one artifact per (platform, profile, edition) - up to
# eight per platform - but a wheel filename only encodes the platform. Pick
# deliberately instead of letting the last one win.
PROFILE_RANK = {"full": 0, "minimal": 1}
EDITION_RANK = {"media": 0, "ffmpeg": 1, "openh264": 2, "standard": 3}


def variant_of(name):
    """(profile, edition) parsed out of an artifact directory name."""
    profile = next((p for p in PROFILE_RANK if "-" + p + "-" in name), None)
    edition = next((e for e in EDITION_RANK if name.endswith("-" + e)
                    or ("-" + e + "-") in name), None)
    return profile, edition


# Every (profile, edition) variant becomes its own pip package, because a
# wheel filename has no slot for the variant and pip must see exactly one
# candidate per name+version+platform. The bare name is each profile's
# default edition. All of them install the same `pyfreerdpnative` module, so only
# one may be installed at a time (like opencv-python vs opencv-python-headless).
BASE_NAME = "pyfreerdpnative"
DEFAULT_EDITION = {"full": "media", "minimal": "standard"}


def package_name(profile, edition):
    parts = [BASE_NAME]
    if profile == "minimal":
        parts.append("minimal")
    if edition and edition != DEFAULT_EDITION.get(profile or "full"):
        parts.append(edition)
    return "-".join(parts)


def rename_package(root, new_name):
    """
    Rename the distribution inside an unpacked wheel: the dist-info directory
    (wheel pack derives the filename from it) and the Name in METADATA.
    """
    import re
    dist_infos = glob.glob(os.path.join(root, "*.dist-info"))
    if len(dist_infos) != 1:
        raise SystemExit("expected one dist-info, found {0}".format(dist_infos))
    old = dist_infos[0]
    version = os.path.basename(old)[:-len(".dist-info")].split("-", 1)[1]
    normalized = re.sub(r"[-_.]+", "_", new_name).lower()
    new = os.path.join(root, "{0}-{1}.dist-info".format(normalized, version))
    if old != new:
        os.rename(old, new)
    meta = os.path.join(new, "METADATA")
    with open(meta) as fh:
        lines = fh.read().splitlines()
    lines = ["Name: {0}".format(new_name) if l.startswith("Name: ") else l
             for l in lines]
    with open(meta, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def choose_variants(entries, want_profile, want_edition, key_of):
    """
    Keep one artifact per wheel tag. An exact --profile/--edition match wins;
    otherwise the ranking above decides, so the result does not depend on
    directory order.
    """
    best = {}
    for entry, key in entries:
        profile, edition = variant_of(os.path.basename(entry))
        exact = ((want_profile is None or profile == want_profile)
                 and (want_edition is None or edition == want_edition))
        rank = (0 if exact else 1,
                PROFILE_RANK.get(profile, 9), EDITION_RANK.get(edition, 9))
        if key not in best or rank < best[key][0]:
            best[key] = (rank, entry, profile, edition)
    chosen = {}
    for key, (_rank, entry, profile, edition) in best.items():
        chosen[key] = entry
        log("{0}: using {1} ({2}/{3})".format(
            key, os.path.basename(entry), profile or "?", edition or "?"))
    return chosen


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
    """
    Return a directory holding _libs/ for this artifact, or None if there is
    none (a stray artifact, or a layout this version does not produce).
    Skipping is better than aborting: one odd directory should not stop the
    rest of the platforms from being packaged.
    """
    if os.path.isdir(src):
        if os.path.isdir(os.path.join(src, "_libs")):
            return src
        # Older layout: an archive inside the artifact directory.
        for pattern in ("*.tar.gz", "*.tgz", "*.zip"):
            hits = glob.glob(os.path.join(src, pattern))
            if hits:
                return unpack(hits[0], dest)
        # Or _libs one level deeper.
        for sub in sorted(os.listdir(src)):
            deeper = os.path.join(src, sub, "_libs")
            if os.path.isdir(deeper):
                return os.path.join(src, sub)
        log("skipping {0}: no _libs directory".format(os.path.basename(src)))
        return None
    os.makedirs(dest)
    if src.endswith((".tar.gz", ".tgz")):
        with tarfile.open(src) as tf:
            tf.extractall(dest)
    elif src.endswith(".zip"):
        with zipfile.ZipFile(src) as zf:
            zf.extractall(dest)
    else:
        log("skipping {0}: unknown archive type".format(os.path.basename(src)))
        return None
    if os.path.isdir(os.path.join(dest, "_libs")):
        return dest
    inner = [os.path.join(dest, d) for d in os.listdir(dest)
             if os.path.isdir(os.path.join(dest, d, "_libs"))]
    if inner:
        return inner[0]
    log("skipping {0}: no _libs inside".format(os.path.basename(src)))
    return None


def restore_exec_bits(directory):
    """
    GitHub's artifact zip does not carry unix permissions, so executables
    arrive as plain files. Put the bit back by looking at the magic number:
    ELF and Mach-O files that are not shared libraries are programs.
    """
    fixed = 0
    for root, _dirs, files in os.walk(directory):
        for fn in files:
            path = os.path.join(root, fn)
            if ".so" in fn or fn.endswith((".dylib", ".dll", ".lib", ".a",
                                           ".pc", ".txt", ".cfg", ".h")):
                continue
            try:
                with open(path, "rb") as fh:
                    magic = fh.read(4)
            except OSError:
                continue
            is_elf = magic == b"\x7fELF"
            is_macho = magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
                                 b"\xca\xfe\xba\xbe")
            if is_elf or is_macho:
                mode = os.stat(path).st_mode
                if not mode & 0o111:
                    os.chmod(path, mode | 0o755)
                    fixed += 1
    if fixed:
        log("restored the executable bit on {0} file(s)".format(fixed))


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


def wheel_with_libs(base_wheel, libs_dir, bin_dir, tag, outdir, name=None):
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

    if name and name != BASE_NAME:
        rename_package(root, name)

    for src_dir, arc_root in ((libs_dir, "_libs"), (bin_dir, "_bin")):
        if not src_dir or not os.path.isdir(src_dir):
            continue
        dest = os.path.join(root, "pyfreerdpnative", arc_root)
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
    p.add_argument("artifacts", nargs="?",
                   help="directory of unpacked/downloaded artifacts")
    p.add_argument("--require-version", type=int, metavar="N",
                   help="exit 0 if this script is version N, else explain "
                        "that scripts/package_wheels.py is stale")
    p.add_argument("-o", "--outdir", default="dist")
    p.add_argument("--repo", default=os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    p.add_argument("--with-executables", action="store_true",
                   help="also ship a legacy _bin/ directory if the artifact "
                        "has one. Current builds put the executables in "
                        "_libs alongside the libraries, so they are included "
                        "either way.")
    p.add_argument("--all-variants", action="store_true",
                   help="package every (profile, edition) artifact as its own "
                        "pip package: pyfreerdpnative (full/media), "
                        "pyfreerdpnative-minimal (minimal/standard), "
                        "pyfreerdpnative-ffmpeg, -openh264, -standard, "
                        "-minimal-ffmpeg, ... Without this, one variant is "
                        "chosen per platform with --profile/--edition.")
    p.add_argument("--profile", choices=("full", "minimal"), default="full",
                   help="which build profile to publish when a platform has "
                        "several artifacts (default: full).")
    p.add_argument("--edition", choices=("standard", "ffmpeg", "openh264",
                                         "media"), default="media",
                   help="which media edition to publish (default: media).")
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

    if args.require_version is not None:
        if args.require_version == PACKAGE_SCRIPT_VERSION:
            print("[package_wheels.py] version {0} OK".format(
                PACKAGE_SCRIPT_VERSION))
            return 0
        sys.stderr.write(
            "scripts/package_wheels.py is version {0} but the workflow expects "
            "version {1}.\nThe repository has a stale copy: commit the "
            "package_wheels.py that came with this workflow.\n".format(
                PACKAGE_SCRIPT_VERSION, args.require_version))
        return 1
    if not args.artifacts:
        p.error("the artifacts directory is required")

    outdir = os.path.abspath(args.outdir)
    if not os.path.isdir(outdir):
        os.makedirs(outdir)

    entries = sorted(glob.glob(os.path.join(args.artifacts, "*")))
    if not entries:
        raise SystemExit("nothing in {0}".format(args.artifacts))

    work = tempfile.mkdtemp(prefix="pkg-")
    base = build_base_wheel(args.repo, work)
    log("base wheel: {0}".format(os.path.basename(base)))

    # Decide which artifact represents each wheel tag before building any.
    candidates = []
    for entry in entries:
        name = os.path.basename(entry)
        if name.endswith(".whl"):
            continue
        tag = mobile_tag(name, args.android_api, args.ios_version)
        if not tag:
            plat = platform_of(name)
            tag = PLATFORM_TAGS[plat] if plat else None
        if tag:
            candidates.append((entry, tag))
    if args.all_variants:
        chosen = set(e for e, _t in candidates)
        log("packaging every variant as its own pip package")
    else:
        chosen = set(choose_variants(candidates, args.profile, args.edition,
                                     None).values())

    made, skipped = [], []
    for entry in entries:
        name = os.path.basename(entry)
        if name.endswith(".whl"):
            continue
        if candidates and entry not in chosen and any(
                e == entry for e, _t in candidates):
            continue                      # a different variant won this tag
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
                if root is None:
                    continue
                restore_exec_bits(os.path.join(root, "_libs"))
                prof, ed = variant_of(name)
                whl = wheel_with_libs(base, os.path.join(root, "_libs"),
                                      None, mtag, outdir,
                                      name=package_name(prof, ed)
                                      if args.all_variants else None)
                log("{0} -> {1}".format(name, os.path.basename(whl)))
                made.append(whl)
            continue
        plat = platform_of(name)
        if not plat:
            log("skipping {0} (unknown platform)".format(name))
            continue
        root = unpack(entry, os.path.join(work, name + ".x"))
        if root is None:
            continue
        libs = os.path.join(root, "_libs")
        restore_exec_bits(libs)
        bins = os.path.join(root, "_bin") if args.with_executables else None
        prof, ed = variant_of(name)
        whl = wheel_with_libs(base, libs, bins, PLATFORM_TAGS[plat], outdir,
                              name=package_name(prof, ed)
                              if args.all_variants else None)
        log("{0} -> {1}".format(name, os.path.basename(whl)))
        made.append(whl)

    # The sdist lets people build from source on platforms with no wheel.
    run([sys.executable, "-m", "build", "--sdist", "--outdir", outdir],
        cwd=args.repo)

    if not made:
        raise SystemExit(
            "no wheels were produced - check that the artifact directories "
            "are named like freerdp-<ref>-<platform>-<profile>-<edition> and "
            "contain a _libs directory")
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
    sys.exit(main())

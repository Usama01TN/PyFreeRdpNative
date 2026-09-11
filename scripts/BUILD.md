# Native build system

`pyfreerdp` ships FreeRDP 3 together with the media and USB stack it needs.
Three scripts under `scripts/` build, package and prove it, on every platform,
from pinned sources:

| Step | Script | Produces |
|---|---|---|
| 1 | `build_deps.py` | libusb 1.0.30, OpenH264 2.6.0, FFmpeg 7.1.5 → `build/deps/<label>/` |
| 2 | `build_freerdp.py` | libfreerdp3 / -client3 / -server3 / winpr3 (+ shadow, proxy) with all channels, H.264, DSP audio, USB + camera redirection, and the executables → `pyfreerdp/_libs/`, `pyfreerdp/_bin/` |
| 3 | `validate_build.py` | pass/fail report: cold loads, channel symbols, live codec backends, FFmpeg/OpenH264 round trips, libusb, every executable, and a real RDP loopback session |

Everything is driven by `.github/workflows/build-freerdp.yml` (all platforms,
artifacts + optional release) and `ci.yml` (Linux, every push/PR).

## Editions (media stack)

Every platform x architecture is built in four editions, selected with
`--edition` on both `build_deps.py` and `build_freerdp.py` (workflow input
`editions: all|standard|ffmpeg|openh264|media`):

| Edition | Third-party libs | H.264 | Audio DSP | Scaler | Camera (`rdpecam`) | USB (`urbdrc`) |
|---|---|---|---|---|---|---|
| `standard` | none | built-in only (no H.264) | built-in (PCM/ADPCM/G.711) | cairo | server side only | no |
| `ffmpeg` | FFmpeg (LGPL) + libusb | **decode** only (FFmpeg has no LGPL H.264 encoder) | + AAC, Opus | swscale | client (Linux/V4L) + server | yes |
| `openh264` | OpenH264 + libusb | encode + decode | built-in | cairo | server side only | yes |
| `media` | FFmpeg (with libopenh264) + OpenH264 + libusb | encode + decode | + AAC, Opus | swscale | client (Linux/V4L) + server | yes |

Dependency prefixes are per edition: `build/deps/<platform>-<edition>`. The
validator adapts to the edition: for `ffmpeg` it expects an H.264 *decoder*
context but no encoder and round-trips MJPEG + AAC; for `openh264` it encodes
with `h264enc` and decodes with `h264dec`; for `media` it does both. All four
were built and validated on Linux x86_64 (media 16/16, ffmpeg 13/13,
openh264 12/12, standard 4/4).

Artifacts are named `freerdp-<ref>-<platform>-<profile>-<edition>`.

## Two profiles, built independently

| | `minimal` | `full` |
|---|---|---|
| Purpose | what the Python wheel ships | everything FreeRDP can do on the platform |
| Libraries | libwinpr3, libfreerdp3, libfreerdp-client3, libfreerdp-server3 (+ librdtk0) | those plus FFmpeg, OpenH264, libusb, shadow (Linux), proxy |
| Channels | every channel with no external dependency (20 client / 17 server) | all of them, incl. `urbdrc` (USB) and `rdpecam` client (Linux camera) |
| Media | none | H.264 encode/decode (OpenH264 + FFmpeg), AAC/Opus DSP, swscale |
| Executables | none | platform client(s), `sfreerdp` (the sample client from `client/Sample`, on Windows built as a standalone project against the installed prefix), `sdl-freerdp` where SDL3 is available, `sfreerdp-server`, `freerdp-proxy`, `freerdp-shadow-cli` (Linux), `winpr-makecert`, `winpr-hash`, `ffmpeg`, `ffprobe`, `h264enc`, `h264dec` (not on Windows: vcpkg ships no tools), `listdevs` |
| Build type | `MinSizeRel`, stripped, one SONAME file per library | `Release`, dev links kept |
| Linux x86_64 size | **3.5 MB** | 28 MB `_libs` + 3.7 MB `_bin` |
| Command | `build_freerdp.py --profile minimal --edition <e>` | `build_deps.py --edition <e>` then `build_freerdp.py --profile full --edition <e>` |

The profiles share nothing at build time except the FreeRDP source checkout;
each one wipes and repopulates `pyfreerdp/_libs` (and `_bin` for full), so
build the one you want to ship last. In CI every platform produces both as
separate artifacts (`freerdp-<ref>-<platform>-minimal` / `-full`).

`--profile full` defaults to `--edition media` and implies `--with-executables`;
`--profile minimal` defaults to `--edition standard`. Any profile can be paired
with any edition. On Windows every edition consumes a vcpkg prefix (at least
OpenSSL/zlib/cJSON); media activation is driven by what the prefix actually
contains, and the script refuses a prefix that does not match the edition.

### What "full" means per platform

Facts from FreeRDP's own CMake (`server/CMakeLists.txt`, upstream CI presets),
encoded once in `FULL_PLATFORM` in `build_freerdp.py`:

| Platform | shadow server | proxy | sample server | winpr tools | client executables |
|---|---|---|---|---|---|
| Linux | yes (X11 subsystem) | yes | yes | yes | xfreerdp, wlfreerdp, sdl-freerdp (if SDL3) |
| macOS | **no** - upstream: "Mac shadow server implementation no longer compiles" | yes | yes | yes | sdl-freerdp (brew sdl3 + sdl3_ttf) |
| Windows | **no** - upstream builds Windows with `WITH_SHADOW=OFF` | yes | yes | yes | wfreerdp, sdl-freerdp (vcpkg sdl3 + sdl3-ttf) |
| Android | no | no | no | no | none (libraries only; media + channels included) |
| iOS | no | no | no | no | none (static libraries only; no libusb - no USB host API) |

## Quick start (Linux / macOS)

```bash
# 1. system toolchain (Debian/Ubuntu; the script prints the exact list)
sudo apt-get install -y build-essential cmake ninja-build git pkg-config nasm patchelf \
    libssl-dev zlib1g-dev libkrb5-dev libcjson-dev libicu-dev libudev-dev libv4l-dev \
    libx11-dev libxext-dev libxrandr-dev libxinerama-dev libxfixes-dev libxcursor-dev \
    libxi-dev libxv-dev libxkbfile-dev libxkbcommon-dev libwayland-dev wayland-protocols \
    libasound2-dev libpng-dev libjpeg-dev libcairo2-dev xvfb

# minimal: libraries only, ~10 min
python scripts/build_freerdp.py --target host --profile minimal
python scripts/validate_build.py --profile minimal          # expect 4 passed

# full: dependencies (~10 min, cached by CI) + FreeRDP with everything (~15 min)
python scripts/build_deps.py --target host
python scripts/build_freerdp.py --target host --profile full
python scripts/validate_build.py --media --executables --loopback   # expect 16 passed
```

## What ends up in the package

```
pyfreerdp/_libs/        libwinpr3, libfreerdp3, libfreerdp-client3, libfreerdp-server3,
                        libfreerdp-shadow3, libfreerdp-server-proxy3, librdtk0, libuwac0,
                        libavcodec, libavutil, libswscale, libswresample, libavformat,
                        libavfilter, libavdevice, libopenh264, libusb-1.0
                        (+ OpenSSL/zlib DLLs on Windows; freerdp3/proxy/ modules on Linux)
pyfreerdp/_bin/         xfreerdp, wlfreerdp (Linux) / wfreerdp (Windows), sfreerdp-server,
                        freerdp-shadow-cli, freerdp-proxy, winpr-makecert, winpr-hash,
                        ffmpeg, ffprobe, h264enc, h264dec (+ openh264-config/), listdevs
                        (Windows: executables live in _libs/ next to their DLLs)
```

Every shared object is relocatable (`$ORIGIN` / `@loader_path`), every
executable finds `../_libs`. Nothing needs `LD_LIBRARY_PATH`. The only
external requirements are the OS itself and, on Windows, the VC++ 2015-2022
redistributable that Python already installs.

### Channels

All FreeRDP channels are compiled into the libraries (FreeRDP 3 has no
plugin files). Client side: drdynvc, rdpdr, cliprdr, rdpsnd, audin, rdpgfx,
disp, rail, rdpei, ainput, location, echo, encomsp, remdesk, drive, smartcard,
video, geometry, serial/parallel (Linux), **urbdrc** (USB, via libusb) and
**rdpecam** (camera, Linux/V4L). Server side: everything with a server half,
including telemetry, rdpemsc and rdpecam. `--list-channels` prints the table;
`--enable-channel` / `--disable-channel` adjust it; the script re-reads
`CMakeCache.txt` after configure and fails if CMake dropped one.

### Media

* **OpenH264** is FreeRDP's H.264 encoder/decoder (`WITH_OPENH264`) and
  FFmpeg's `libopenh264` encoder.
* **FFmpeg** provides H.264/MJPEG decode, the AAC/Opus DSP path
  (`WITH_DSP_FFMPEG`), pixel conversion (`WITH_SWSCALE`) and the `ffmpeg` /
  `ffprobe` tools. The build is LGPL-only and scoped to exactly the components
  FreeRDP and the validation suite use (`FFMPEG_COMPONENTS` in
  `build_deps.py`); add names there to extend it.
* **libusb** backs the `urbdrc` channel and `rdpecam`'s V4L backend.

## Per-platform notes

| Target | How deps are built | Library type |
|---|---|---|
| Linux x86_64 / aarch64 | source (autotools/make) | shared |
| macOS arm64 / x86_64 | source; `@rpath` install names; SDL3 via Homebrew | shared |
| Windows x64 / x86 / arm64 | vcpkg manifest (`scripts/vcpkg.json`: core + `media` + `sdl` features, pinned baseline); x86 + arm64 cross-compiled with `-A Win32` / `-A ARM64` | shared |
| Android arm64-v8a / armeabi-v7a / x86_64 / x86 | NDK cross; OpenSSL cross-built in the workflow; LTO off (NDK `--fatal-warnings`); OpenH264 x86 built with `ENABLEPIC=Yes` | shared |
| iOS OS64 (device, both profiles) / SIMULATORARM64 (minimal only) | static; libusb skipped (no USB host API); OpenH264/FFmpeg build for the device SDK only | static |

`build_freerdp.py --arch {x64,x86,arm64}` selects the Windows target;
`--ios-platform` and `--abi` select mobile targets. `--deps-prefix auto`
resolves to `build/deps/<label>` for the current target.

## Kerberos

Kerberos authentication (NLA with a domain account, SSO) goes through winpr's
SSPI layer. Whether and how it is available is decided per platform, from
FreeRDP's own support matrix, in `krb5_options()` in `build_freerdp.py`:

| Platform | Kerberos | Source | Ships in package |
|---|---|---|---|
| Linux (both profiles) | **on** | MIT krb5 via `WITH_KRB5=ON` (FreeRDP's default; upstream CI builds it) | full: `libkrb5`, `libk5crypto`, `libcom_err`, `libkrb5support` bundled into `_libs`. minimal: uses the distro's krb5 runtime (part of every mainstream base install) — zero size cost |
| Windows (both) | **on** | the OS: winpr forwards to native SSPI (`WITH_NATIVE_SSPI` is forced on for WIN32), Kerberos/Negotiate come from `secur32.dll` | nothing needed |
| macOS | **off** by default | FreeRDP's `FindKRB5` rejects Apple's system Kerberos ("MITKerberosShim is deprecated and not supported") and upstream's macOS CI builds with `WITH_KRB5=OFF`. Homebrew MIT krb5 works but is not upstream-verified: `brew install krb5 && build_freerdp.py --with-krb5` (dylibs are then bundled) | — |
| Android, iOS | **off** | upstream CI sets `WITH_KRB5=OFF`; no supported krb5 build | — |

Full on Linux **requires** krb5 (`libkrb5-dev` / `krb5-devel`) and fails with a
clear message otherwise; minimal falls back to `WITH_KRB5=OFF` with a loud
warning. `--without-krb5` disables it anywhere. After configure the script
re-reads `CMakeCache.txt` and fails if `WITH_KRB5` didn't resolve as intended.

`validate_build.py --kerberos on|off|auto` proves it at runtime without a KDC:
it enumerates winpr's SSPI packages (must list `Kerberos`) and calls
`AcquireCredentialsHandle("Kerberos")` with no identity. A live krb5 backend
goes into libkrb5 and returns e.g. `SEC_E_NO_CREDENTIALS` instantly; a build
without krb5 returns `SEC_E_UNSUPPORTED_FUNCTION`. Configuration at runtime is
the standard `/etc/krb5.conf` / `KRB5_CONFIG`; a ticket cache from `kinit` (or
`/u:user@REALM /p:...`) is used by the client as usual.

## Validation

`validate_build.py` is the executable definition of "works":

* `libs` — each core library loads by absolute path in a fresh interpreter with
  library paths scrubbed.
* `channels.*` — server contexts exported; every client channel resolves through FreeRDP's own `freerdp_channels_load_static_addin_entry()` (strip-proof).
* `kerberos` — SSPI package list + live-backend probe (see the Kerberos section).
* `media.linked / media.h264 / media.dsp` — libfreerdp3 imports FFmpeg and
  OpenH264 and they are staged; `h264_context_new()` returns a context for both
  decoder and encoder; `freerdp_dsp_supports_format()` accepts AAC and Opus.
* `ffmpeg.*` — test pattern → `libopenh264` → Annex-B → ffprobe counts 25
  frames; sine → AAC → PCM.
* `openh264.*` — `WelsGetCodecVersion`; `h264dec` decodes to the exact byte count.
* `libusb.*` — `libusb_init` / `libusb_get_device_list` (0 devices is fine on CI).
* `executables` — all dynamic deps resolve; each answers `/version` etc.;
  `winpr-makecert` produces a certificate.
* `loopback` (Linux) — `sfreerdp-server` + `xfreerdp` under Xvfb: TLS session
  activates, RDPSND is negotiated, dynamic channels open.

## When a CI leg fails

Every build job uploads a `diagnostics-<platform>-<profile>` artifact on
failure containing `CMakeCache.txt`, `CMakeConfigureLog.yaml` /
`CMakeError.log` and the resolved `vcpkg.json` (Windows). The real cause of a
CMake configure failure is almost always in those files rather than in the
console log; attach them when reporting a problem.

Every job also starts with a version handshake (`--require-version N`) so a
stale `scripts/` directory fails immediately with an explanation.

## Releases

Artifacts (30-day retention) are listed with direct links in each run's
Summary. Additionally the `links` job attaches every artifact to the rolling
pre-release `freerdp-libs-<freerdp_ref>` on each push to `main`, on
`workflow_dispatch` with `publish_release`, and on `release: published`, so
there are permanent download URLs of the form
`https://github.com/<owner>/<repo>/releases/download/freerdp-libs-3.16.0/freerdp-3.16.0-<platform>-<profile>.tar.gz`
(`.zip` on Windows).

## Reproducibility

Sources are pinned by version and SHA-256 (`KNOWN_HASHES`); `vcpkg.json` pins
a registry baseline (resolved at build time against the runner's vcpkg clone,
falling back to its HEAD if the pinned commit is absent - the chosen commit is
printed and stored in the diagnostics); FreeRDP is pinned by tag (`--ref`, default 3.16.0). CI
caches `build/deps/<label>` keyed on the hash of `build_deps.py` +
`vcpkg.json`, so a dependency change rebuilds everything and nothing else does.
To bump a dependency: change the version in `SOURCES`, run
`build_deps.py --print-hashes`, paste the hashes, commit.

## Known limits

* Windows `arm64` and `x86` FreeRDP builds are cross-compiled; the workflow
  load-tests arm64 natively on `windows-11-arm` and x86 with a 32-bit Python.
* `rdpecam`'s client half has a capture backend only on Linux (V4L) in FreeRDP
  3.16; the server half is built everywhere.
* GSM 6.10, MS-ADPCM, G.723.1 and A-law/µ-law audio go through FreeRDP's
  built-in codecs, not FFmpeg (upstream filters them out of the FFmpeg backend
  unless `WITH_DSP_EXPERIMENTAL`).
* The Linux path of all three scripts has been exercised end to end; the
  macOS, Windows, Android and iOS paths are written against upstream build
  documentation and FreeRDP's CMake and are verified by the workflow's own
  per-platform validation steps.

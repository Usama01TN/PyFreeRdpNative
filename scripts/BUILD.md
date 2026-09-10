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

## Quick start (Linux / macOS)

```bash
# 1. system toolchain (Debian/Ubuntu; the script prints the exact list)
sudo apt-get install -y build-essential cmake ninja-build git pkg-config nasm patchelf \
    libssl-dev zlib1g-dev libkrb5-dev libcjson-dev libicu-dev libudev-dev libv4l-dev \
    libx11-dev libxext-dev libxrandr-dev libxinerama-dev libxfixes-dev libxcursor-dev \
    libxi-dev libxv-dev libxkbfile-dev libxkbcommon-dev libwayland-dev wayland-protocols \
    libasound2-dev libpng-dev libjpeg-dev libcairo2-dev xvfb

# 2. dependencies (~10 min on 4 cores; cached by CI)
python scripts/build_deps.py --target host

# 3. FreeRDP + executables (~15 min)
python scripts/build_freerdp.py --target host --profile full \
    --deps-prefix auto --with-executables

# 4. prove it
python scripts/validate_build.py --media --executables --loopback
```

Expected tail of step 4: `[validate] 15 passed, 0 failed`.

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

All 30 FreeRDP channels are compiled into the libraries (FreeRDP 3 has no
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

| Target | How deps are built | FreeRDP | Executables |
|---|---|---|---|
| Linux x86_64 / aarch64 | source (autotools/make) | shared, `full` | all |
| macOS arm64 / x86_64 | source; `@rpath` install names | shared, `minimal` | sfreerdp-server, winpr tools, ffmpeg, ffprobe, h264enc/dec, listdevs |
| Windows x64 / x86 / arm64 | vcpkg manifest (`scripts/vcpkg.json`, pinned baseline); x86 + arm64 cross-compiled with `-A Win32` / `-A ARM64` | shared, `minimal` | wfreerdp, sfreerdp-server, winpr tools, ffmpeg, ffprobe |
| Android arm64-v8a / x86_64 | NDK cross; OpenSSL cross-built in the workflow | shared, `minimal` | none (libraries only) |
| iOS OS64 / SIMULATORARM64 | static; libusb skipped (no USB host API) | static, `minimal` | none |

`build_freerdp.py --arch {x64,x86,arm64}` selects the Windows target;
`--ios-platform` and `--abi` select mobile targets. `--deps-prefix auto`
resolves to `build/deps/<label>` for the current target.

## Validation

`validate_build.py` is the executable definition of "works":

* `libs` — each core library loads by absolute path in a fresh interpreter with
  library paths scrubbed.
* `channels.*` — server contexts exported; client static entry table present.
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

## Reproducibility

Sources are pinned by version and SHA-256 (`KNOWN_HASHES`); `vcpkg.json` pins
a registry baseline; FreeRDP is pinned by tag (`--ref`, default 3.16.0). CI
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

#!/usr/bin/env bash
# CIBW_BEFORE_ALL hook for macOS wheels (x86_64 + arm64).
#
# Runs natively on the macos-13 (Intel) or macos-14 (Apple Silicon)
# runner. Builds FreeRDP from source linked against Homebrew's OpenSSL,
# then stages the resulting .dylib files for `delocate` to bundle.
set -euo pipefail

PROJECT_DIR="${1:-$(pwd)}"
LIBS_DIR="${PROJECT_DIR}/pyfreerdp/_libs"
echo "[before_all_macos] PROJECT_DIR=${PROJECT_DIR}"
echo "[before_all_macos] arch=$(uname -m)"

# --- 1. Install build deps via brew ----------------------------------------

# brew is preinstalled on GitHub macOS runners. We avoid `brew install
# freerdp` because (a) we want a controlled version and (b) we want to
# disable the optional codec deps, which the homebrew formula enables.
brew update >/dev/null
brew install cmake ninja openssl@3 libpng jpeg-turbo

# Locate openssl@3 so cmake can find it. Apple Silicon brew uses
# /opt/homebrew, Intel uses /usr/local.
OPENSSL_PREFIX="$(brew --prefix openssl@3)"
echo "[before_all_macos] OPENSSL_PREFIX=${OPENSSL_PREFIX}"

# --- 2. Resolve FreeRDP version --------------------------------------------

if [ -x "${PROJECT_DIR}/.github/cibw-scripts/get_freerdp_version.sh" ]; then
  FREERDP_VERSION=$("${PROJECT_DIR}/.github/cibw-scripts/get_freerdp_version.sh")
else
  FREERDP_VERSION="${PYFREERDP_FREERDP_REF:-3.16.0}"
fi
echo "[before_all_macos] FREERDP_VERSION=${FREERDP_VERSION}"

# --- 3. Clone + build FreeRDP ----------------------------------------------

WORK_DIR="$(mktemp -d /tmp/freerdp-build-XXXXXX)"
trap 'rm -rf "${WORK_DIR}"' EXIT

cd "${WORK_DIR}"
git clone --depth=1 --branch="${FREERDP_VERSION}" \
  https://github.com/FreeRDP/FreeRDP.git

cd FreeRDP
mkdir build && cd build

# CMAKE_OSX_DEPLOYMENT_TARGET=11.0 - matches what cibuildwheel sets for
# the wheel itself (MACOSX_DEPLOYMENT_TARGET in the env). Anything older
# won't have the BSDish APIs FreeRDP uses.
#
# CMAKE_INSTALL_NAME_DIR=@rpath - embeds @rpath into the dylib's install
# names. delocate rewrites these at wheel-bundling time. Without this
# flag, the dylib would have an absolute path baked in and delocate
# would have to do extra rewriting.
cmake .. \
  -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=/tmp/freerdp-prefix \
  -DCMAKE_OSX_DEPLOYMENT_TARGET=11.0 \
  -DCMAKE_INSTALL_NAME_DIR=@rpath \
  -DBUILD_SHARED_LIBS=ON \
  -DOPENSSL_ROOT_DIR="${OPENSSL_PREFIX}" \
  -DWITH_MANPAGES=OFF \
  -DWITH_SAMPLE=OFF \
  -DWITH_OPENSSL=ON \
  -DWITH_CLIENT=ON \
  -DWITH_CLIENT_COMMON=ON \
  -DWITH_SERVER=ON \
  -DWITH_SHADOW=OFF \
  -DWITH_PROXY=OFF \
  -DWITH_X11=OFF \
  -DWITH_WAYLAND=OFF \
  -DWITH_ALSA=OFF \
  -DWITH_FFMPEG=OFF \
  -DWITH_OPENH264=OFF \
  -DWITH_GSTREAMER_1_0=OFF \
  -DWITH_CUPS=OFF \
  -DWITH_PCSC=OFF

ninja -j"$(sysctl -n hw.ncpu)"
ninja install

# --- 4. Stage artifacts into pyfreerdp/_libs/ ------------------------------

mkdir -p "${LIBS_DIR}"

for pat in libfreerdp-client3 libfreerdp-server3 libfreerdp3 libwinpr3; do
  for f in /tmp/freerdp-prefix/lib/${pat}*.dylib; do
    if [ -e "${f}" ]; then
      cp -P "${f}" "${LIBS_DIR}/"
      echo "[before_all_macos] staged $(basename ${f})"
    fi
  done
done

if ! ls "${LIBS_DIR}"/libfreerdp-client3*.dylib >/dev/null 2>&1; then
  echo "::error::libfreerdp-client3 was not built/installed correctly"
  ls -la /tmp/freerdp-prefix/lib 2>/dev/null || true
  exit 1
fi

# delocate inspects DYLD_LIBRARY_PATH to find dependent dylibs at
# wheel-repair time. Append our prefix.
echo "DYLD_LIBRARY_PATH=/tmp/freerdp-prefix/lib:${DYLD_LIBRARY_PATH:-}" \
  >> "${GITHUB_ENV:-/dev/null}" || true

echo "[before_all_macos] complete"
ls -la "${LIBS_DIR}/"

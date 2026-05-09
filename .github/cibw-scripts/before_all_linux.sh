#!/usr/bin/env bash
# CIBW_BEFORE_ALL hook for Linux wheels (manylinux + musllinux).
#
# Runs ONCE per architecture before any wheel build, inside the
# manylinux_2_28_* or musllinux_1_2_* container. Responsibilities:
#
#   1. Install the build toolchain + FreeRDP dependencies.
#   2. Resolve the latest stable FreeRDP version.
#   3. Clone, configure, build, and install FreeRDP into a prefix.
#   4. Stage the built libraries into pyfreerdp/_libs/ where setup.py's
#      package_data globs will pick them up at wheel-build time.
#
# Note: each unique (Python version × architecture) combo gets a fresh
# container. CIBW_BEFORE_ALL runs once per arch, BEFORE the per-Python
# build loop. The libs we stage are reused across all Python versions
# of the same architecture.
set -euo pipefail

PROJECT_DIR="${1:-$(pwd)}"
LIBS_DIR="${PROJECT_DIR}/pyfreerdp/_libs"
echo "[before_all_linux] PROJECT_DIR=${PROJECT_DIR}"
echo "[before_all_linux] LIBS_DIR=${LIBS_DIR}"

# Detect distro inside the container.
if [ -f /etc/alpine-release ]; then
  DISTRO=alpine
elif [ -f /etc/redhat-release ]; then
  DISTRO=rhel
else
  echo "::warning::unrecognized base image; trying RHEL-style packages"
  DISTRO=rhel
fi
echo "[before_all_linux] DISTRO=${DISTRO}"

# --- 1. Install build deps --------------------------------------------------

if [ "${DISTRO}" = "alpine" ]; then
  apk add --no-cache \
    cmake ninja git pkgconfig \
    gcc g++ make musl-dev \
    openssl-dev openssl-libs-static \
    zlib-dev zlib-static \
    libpng-dev libjpeg-turbo-dev \
    cairo-dev \
    curl-dev \
    bash
else
  # manylinux_2_28_*. Uses dnf.
  dnf install -y --setopt=tsflags=nodocs \
    cmake ninja-build git pkgconfig \
    gcc gcc-c++ make \
    openssl-devel \
    zlib-devel \
    libpng-devel libjpeg-turbo-devel \
    cairo-devel \
    libcurl-devel
fi

# --- 2. Resolve FreeRDP version --------------------------------------------

# Use the helper script if available, else honor PYFREERDP_FREERDP_REF.
if [ -x "${PROJECT_DIR}/.github/cibw-scripts/get_freerdp_version.sh" ]; then
  FREERDP_VERSION=$("${PROJECT_DIR}/.github/cibw-scripts/get_freerdp_version.sh")
else
  FREERDP_VERSION="${PYFREERDP_FREERDP_REF:-3.16.0}"
fi
echo "[before_all_linux] FREERDP_VERSION=${FREERDP_VERSION}"

# Surface this to setup.py so it can bake into the _native module.
echo "PYFREERDP_BUILT_FREERDP_VERSION=${FREERDP_VERSION}" >> "${GITHUB_ENV:-/dev/null}" || true

# --- 3. Clone + build FreeRDP ----------------------------------------------

WORK_DIR="$(mktemp -d /tmp/freerdp-build-XXXXXX)"
trap 'rm -rf "${WORK_DIR}"' EXIT

cd "${WORK_DIR}"
git clone --depth=1 --branch="${FREERDP_VERSION}" \
  https://github.com/FreeRDP/FreeRDP.git

cd FreeRDP
mkdir build && cd build

# Configure flags chosen to:
#   * produce libfreerdp-client3, libfreerdp-server3, libwinpr3 (the
#     three our binding loads)
#   * skip GUI viewers, X11/Wayland, audio, sample apps
#   * skip heavy media codecs - rdpgfx surfaces encoded H.264 but
#     decoding is the embedder's responsibility
#   * skip CUPS / PCSC (printer / smartcard redirection) - these
#     pull in dev packages we don't have in the container
#
# CMAKE_INSTALL_PREFIX intentionally points inside the container's /tmp
# so the install isn't seen by the system; we copy artifacts manually
# to LIBS_DIR.
cmake .. \
  -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=/tmp/freerdp-prefix \
  -DBUILD_SHARED_LIBS=ON \
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
  -DWITH_PULSE=OFF \
  -DWITH_FFMPEG=OFF \
  -DWITH_DSP_FFMPEG=OFF \
  -DWITH_X264=OFF \
  -DWITH_OPENH264=OFF \
  -DWITH_GSTREAMER_1_0=OFF \
  -DWITH_CUPS=OFF \
  -DWITH_PCSC=OFF

ninja -j"$(nproc)"
ninja install

# --- 4. Stage artifacts into pyfreerdp/_libs/ ------------------------------

mkdir -p "${LIBS_DIR}"

# Copy the .so files plus their version-suffixed siblings. Using cp -P
# preserves symlinks; auditwheel needs the SONAME chain intact.
for pat in libfreerdp-client3 libfreerdp-server3 libfreerdp3 libwinpr3; do
  for f in /tmp/freerdp-prefix/lib/${pat}*.so* /tmp/freerdp-prefix/lib64/${pat}*.so*; do
    if [ -e "${f}" ]; then
      cp -P "${f}" "${LIBS_DIR}/"
      echo "[before_all_linux] staged $(basename ${f})"
    fi
  done
done

# Sanity check: at least the client lib must be present.
if ! ls "${LIBS_DIR}"/libfreerdp-client3.so* >/dev/null 2>&1; then
  echo "::error::libfreerdp-client3 was not built/installed correctly"
  ls -la /tmp/freerdp-prefix/lib* 2>/dev/null || true
  exit 1
fi

# Add the prefix to the linker search path so auditwheel can find the
# dependent libraries (libssl, libcrypto, libpng, libjpeg, libz...) and
# bundle them into the wheel during the repair step.
echo "/tmp/freerdp-prefix/lib" > /etc/ld.so.conf.d/freerdp-prefix.conf 2>/dev/null || true
echo "/tmp/freerdp-prefix/lib64" >> /etc/ld.so.conf.d/freerdp-prefix.conf 2>/dev/null || true
ldconfig 2>/dev/null || true

echo "[before_all_linux] complete"
ls -la "${LIBS_DIR}/"

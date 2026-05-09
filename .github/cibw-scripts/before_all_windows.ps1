# CIBW_BEFORE_ALL hook for Windows wheels (x64).
#
# Runs natively on the windows-latest runner. Uses vcpkg to obtain
# OpenSSL etc., then builds FreeRDP from source against the vcpkg
# triplet so the resulting DLLs have stable, well-known dependencies.
# `delvewheel` then bundles them into the wheel.

$ErrorActionPreference = "Stop"

$PROJECT_DIR = if ($args[0]) { $args[0] } else { (Get-Location).Path }
$LIBS_DIR = Join-Path $PROJECT_DIR "pyfreerdp/_libs"
Write-Host "[before_all_windows] PROJECT_DIR=$PROJECT_DIR"

# --- 1. vcpkg deps ---------------------------------------------------------

# vcpkg is preinstalled on GitHub windows-latest runners at
# C:\vcpkg. The VCPKG_INSTALLATION_ROOT env var points to it.
$vcpkg = Join-Path $env:VCPKG_INSTALLATION_ROOT "vcpkg.exe"
if (-not (Test-Path $vcpkg)) {
    Write-Error "vcpkg not found at $vcpkg"
    exit 1
}

# Use GitHub Actions binary cache to keep cold-build time tolerable
# (~40 min -> ~3 min on warm cache).
$env:VCPKG_BINARY_SOURCES = "clear;x-gha,readwrite"

# Install just the deps we need - openssl, zlib, libpng, libjpeg-turbo.
# We explicitly do NOT use vcpkg's freerdp port: we want a controlled
# version and our own build flags.
& $vcpkg install --triplet x64-windows openssl zlib libpng libjpeg-turbo
if ($LASTEXITCODE -ne 0) {
    Write-Error "vcpkg install failed"
    exit 1
}

$VCPKG_INSTALLED = Join-Path $env:VCPKG_INSTALLATION_ROOT "installed/x64-windows"
Write-Host "[before_all_windows] VCPKG_INSTALLED=$VCPKG_INSTALLED"

# --- 2. Resolve FreeRDP version --------------------------------------------

$ver_script = Join-Path $PROJECT_DIR ".github/cibw-scripts/get_freerdp_version.sh"
if (Test-Path $ver_script) {
    # bash is available on the windows-latest runner via Git for Windows.
    $FREERDP_VERSION = (& bash $ver_script).Trim()
} elseif ($env:PYFREERDP_FREERDP_REF) {
    $FREERDP_VERSION = $env:PYFREERDP_FREERDP_REF
} else {
    $FREERDP_VERSION = "3.16.0"
}
Write-Host "[before_all_windows] FREERDP_VERSION=$FREERDP_VERSION"

# Make the version available to setup.py.
if ($env:GITHUB_ENV) {
    Add-Content -Path $env:GITHUB_ENV -Value "PYFREERDP_BUILT_FREERDP_VERSION=$FREERDP_VERSION"
}

# --- 3. Clone + build FreeRDP ---------------------------------------------

$WORK_DIR = Join-Path $env:TEMP "freerdp-build"
if (Test-Path $WORK_DIR) { Remove-Item -Recurse -Force $WORK_DIR }
New-Item -ItemType Directory -Path $WORK_DIR | Out-Null

Push-Location $WORK_DIR
& git clone --depth=1 --branch=$FREERDP_VERSION `
    "https://github.com/FreeRDP/FreeRDP.git"
if ($LASTEXITCODE -ne 0) { Write-Error "git clone failed"; exit 1 }

Push-Location FreeRDP
New-Item -ItemType Directory -Path build | Out-Null
Push-Location build

$prefix = Join-Path $env:TEMP "freerdp-prefix"
$toolchain = Join-Path $env:VCPKG_INSTALLATION_ROOT "scripts/buildsystems/vcpkg.cmake"

& cmake .. `
    -A x64 `
    -DCMAKE_BUILD_TYPE=Release `
    -DCMAKE_INSTALL_PREFIX="$prefix" `
    -DCMAKE_TOOLCHAIN_FILE="$toolchain" `
    -DVCPKG_TARGET_TRIPLET=x64-windows `
    -DBUILD_SHARED_LIBS=ON `
    -DWITH_MANPAGES=OFF `
    -DWITH_SAMPLE=OFF `
    -DWITH_OPENSSL=ON `
    -DWITH_CLIENT=ON `
    -DWITH_CLIENT_COMMON=ON `
    -DWITH_SERVER=ON `
    -DWITH_SHADOW=OFF `
    -DWITH_PROXY=OFF `
    -DWITH_FFMPEG=OFF `
    -DWITH_OPENH264=OFF `
    -DWITH_GSTREAMER_1_0=OFF `
    -DWITH_CUPS=OFF `
    -DWITH_PCSC=OFF
if ($LASTEXITCODE -ne 0) { Write-Error "cmake configure failed"; exit 1 }

& cmake --build . --config Release --parallel
if ($LASTEXITCODE -ne 0) { Write-Error "cmake build failed"; exit 1 }

& cmake --install . --config Release
if ($LASTEXITCODE -ne 0) { Write-Error "cmake install failed"; exit 1 }

Pop-Location; Pop-Location; Pop-Location

# --- 4. Stage DLLs into pyfreerdp/_libs/ ----------------------------------

if (-not (Test-Path $LIBS_DIR)) {
    New-Item -ItemType Directory -Path $LIBS_DIR -Force | Out-Null
}

# FreeRDP installs DLLs into bin/ on Windows (not lib/).
$freerdp_bin = Join-Path $prefix "bin"
foreach ($pat in @("freerdp-client3", "freerdp-server3", "freerdp3", "winpr3")) {
    Get-ChildItem -Path $freerdp_bin -Filter "$pat.dll" -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item $_.FullName -Destination $LIBS_DIR -Force
        Write-Host "[before_all_windows] staged $($_.Name)"
    }
}

if (-not (Get-ChildItem -Path $LIBS_DIR -Filter "freerdp-client3.dll" -ErrorAction SilentlyContinue)) {
    Write-Error "freerdp-client3.dll was not built/installed correctly"
    Get-ChildItem -Path $freerdp_bin -ErrorAction SilentlyContinue
    exit 1
}

# Also stage vcpkg's runtime DLLs for OpenSSL/zlib/etc. delvewheel will
# pick these up when it scans the wheel for native deps.
$vcpkg_bin = Join-Path $VCPKG_INSTALLED "bin"
$env:PATH = "$freerdp_bin;$vcpkg_bin;$env:PATH"
if ($env:GITHUB_ENV) {
    # Persist the PATH addition so the per-Python build steps inherit it.
    Add-Content -Path $env:GITHUB_ENV -Value "FREERDP_BIN_DIR=$freerdp_bin"
    Add-Content -Path $env:GITHUB_ENV -Value "VCPKG_BIN_DIR=$vcpkg_bin"
}
if ($env:GITHUB_PATH) {
    Add-Content -Path $env:GITHUB_PATH -Value $freerdp_bin
    Add-Content -Path $env:GITHUB_PATH -Value $vcpkg_bin
}

Write-Host "[before_all_windows] complete"
Get-ChildItem -Path $LIBS_DIR | Format-Table Name, Length

# cmake/toolchains/android.cmake
#
# Thin wrapper that defers to the NDK's official CMake toolchain. We don't
# reinvent it — the NDK's own android.toolchain.cmake handles ABI/API/STL
# selection correctly and tracks new NDK releases. This file just enforces
# the FreeRDP-specific defaults.
#
# Usage:
#   cmake -S <freerdp-src> -B build-android \
#         -DCMAKE_TOOLCHAIN_FILE=<this file> \
#         -DANDROID_ABI=arm64-v8a \
#         -DANDROID_PLATFORM=android-24 \
#         -G Ninja

if(NOT DEFINED ENV{ANDROID_NDK_ROOT} AND NOT DEFINED ENV{ANDROID_NDK_HOME})
    message(FATAL_ERROR
        "Set ANDROID_NDK_ROOT (or ANDROID_NDK_HOME) to your NDK install path.")
endif()

if(DEFINED ENV{ANDROID_NDK_ROOT})
    set(_ndk "$ENV{ANDROID_NDK_ROOT}")
else()
    set(_ndk "$ENV{ANDROID_NDK_HOME}")
endif()

if(NOT EXISTS "${_ndk}/build/cmake/android.toolchain.cmake")
    message(FATAL_ERROR
        "NDK at ${_ndk} is missing build/cmake/android.toolchain.cmake")
endif()

# Defer to the NDK's toolchain. Anything set above this include() — like
# ANDROID_ABI — is picked up.
include("${_ndk}/build/cmake/android.toolchain.cmake")

# FreeRDP-specific overrides. Android has no X11/Wayland/PulseAudio, so kill
# all those probes early.
set(WITH_X11 OFF CACHE BOOL "" FORCE)
set(WITH_WAYLAND OFF CACHE BOOL "" FORCE)
set(WITH_PULSE OFF CACHE BOOL "" FORCE)
set(WITH_ALSA OFF CACHE BOOL "" FORCE)
set(WITH_CUPS OFF CACHE BOOL "" FORCE)
set(WITH_PCSC OFF CACHE BOOL "" FORCE)
set(WITH_GSTREAMER_1_0 OFF CACHE BOOL "" FORCE)
set(WITH_FFMPEG OFF CACHE BOOL "" FORCE)
set(WITH_SERVER OFF CACHE BOOL "" FORCE)
set(WITH_SHADOW OFF CACHE BOOL "" FORCE)
set(WITH_PROXY OFF CACHE BOOL "" FORCE)

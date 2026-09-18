# cmake/toolchains/ios.cmake
#
# Minimal iOS toolchain file for cross-compiling FreeRDP. Inspired by the
# widely-used ios-cmake toolchain (leetal/ios-cmake) but trimmed to what
# FreeRDP needs.
#
# Usage:
#   cmake -S <freerdp-src> -B build-ios \
#         -DCMAKE_TOOLCHAIN_FILE=<this file> \
#         -DPLATFORM=OS64 \
#         -G Xcode
#
# Supported PLATFORM values:
#   OS64        — iOS device, arm64
#   SIMULATOR64 — iOS simulator on Intel Mac, x86_64
#   SIMULATORARM64 — iOS simulator on Apple Silicon, arm64

if(NOT DEFINED PLATFORM)
    set(PLATFORM "OS64" CACHE STRING "iOS deployment target type")
endif()

set(CMAKE_SYSTEM_NAME iOS)
set(CMAKE_SYSTEM_VERSION 13.0 CACHE STRING "Minimum iOS deployment target")
set(CMAKE_OSX_DEPLOYMENT_TARGET "13.0" CACHE STRING "" FORCE)

if(PLATFORM STREQUAL "OS64")
    set(CMAKE_OSX_SYSROOT iphoneos)
    set(CMAKE_OSX_ARCHITECTURES "arm64" CACHE STRING "" FORCE)
elseif(PLATFORM STREQUAL "SIMULATOR64")
    set(CMAKE_OSX_SYSROOT iphonesimulator)
    set(CMAKE_OSX_ARCHITECTURES "x86_64" CACHE STRING "" FORCE)
elseif(PLATFORM STREQUAL "SIMULATORARM64")
    set(CMAKE_OSX_SYSROOT iphonesimulator)
    set(CMAKE_OSX_ARCHITECTURES "arm64" CACHE STRING "" FORCE)
else()
    message(FATAL_ERROR "Unsupported PLATFORM: ${PLATFORM}")
endif()

# Tell CMake we're cross-compiling so it doesn't try to run host binaries
# during configure tests.
set(CMAKE_CROSSCOMPILING TRUE)

# Bitcode is dead since Xcode 14, but keep this off to be safe with older toolchains.
set(CMAKE_XCODE_ATTRIBUTE_ENABLE_BITCODE NO)

# iOS apps may not load arbitrary dylibs from disk — build static.
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)

# Search for libraries inside the SDK only.
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

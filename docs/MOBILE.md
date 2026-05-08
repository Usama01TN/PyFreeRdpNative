# Mobile Builds — Android & iOS

This document describes how to build pyfreerdp for mobile platforms, **and where the limits are**. Read all of it before opening an issue.

## TL;DR

| Platform | Status | Approach |
|----------|--------|----------|
| Linux x86_64 / aarch64 | ✅ Works | System package or `pyfreerdp-build` |
| macOS (Intel + Apple Silicon) | ✅ Works | Homebrew or `pyfreerdp-build` |
| Windows x64 | ✅ Works | vcpkg or `pyfreerdp-build` |
| Android (arm64-v8a, armeabi-v7a, x86_64) | ⚠️ Builds; integration is your problem | NDK cross-compile + Chaquopy / BeeWare / Kivy |
| iOS (device + simulator) | ⚠️ Builds static; ctypes can't dlopen on iOS | Pre-link into your app binary |

## Why mobile is hard

Python on mobile isn't a normal Python install. You ship Python *into your app* via a framework like:

- **Chaquopy** — embeds CPython into an Android Gradle build
- **BeeWare / Briefcase** — produces native iOS + Android apps from Python
- **Kivy / python-for-android** — same idea, different toolchain
- **Pyto** (iOS, App Store)

Each one has its own rules for shipping native libraries alongside Python code. There is no universal `pip install pyfreerdp` story on mobile, and there can't be — Apple in particular forbids loading dynamic libraries from disk in App Store builds, which is exactly what `ctypes.CDLL` does.

## Android

### Build the native library

```bash
export ANDROID_NDK_ROOT=$HOME/Android/Sdk/ndk/26.1.10909125
python -m pyfreerdp.scripts.build_freerdp \
    --target android \
    --abi arm64-v8a \
    --api-level 24
```

Repeat for each ABI you want to ship (`armeabi-v7a`, `x86_64`, `x86`). Output lands in `pyfreerdp/_libs/android/<abi>/libfreerdp-client3.so` along with all dependent `.so` files (libfreerdp, winpr, freerdp-client, OpenSSL, zlib).

### Integrate with Chaquopy

In your app's `build.gradle`:

```gradle
android {
    defaultConfig {
        ndk { abiFilters 'arm64-v8a', 'x86_64' }
        python {
            pip {
                install "pyfreerdp"        // or install local source
            }
        }
    }
    sourceSets.main.jniLibs.srcDirs += ['src/main/jniLibs']
}
```

Copy the per-ABI `.so` files into `src/main/jniLibs/<abi>/`. Chaquopy's loader honours the standard Android `System.loadLibrary` paths, and pyfreerdp's loader (`PYFREERDP_LIBRARY` override) lets you point it explicitly:

```python
import os
from android.content import Context  # provided by Chaquopy
ctx = Context.getApplicationContext()
os.environ["PYFREERDP_LIBRARY"] = (
    ctx.getApplicationInfo().nativeLibraryDir + "/libfreerdp-client3.so"
)
import pyfreerdp
```

### Integrate with python-for-android (Kivy)

Add a recipe under `p4a/recipes/freerdp/__init__.py` based on `pyjnius`'s pattern. The cross-compile CMake invocation is the same as our `build_freerdp.py --target android` produces; just plumb it into p4a's `build_arch` step. Full recipe is out of scope here.

### Audio / display on Android

FreeRDP's Android client (`client/Android/`) implements the Java↔C bridge for surface rendering and audio routing. Our binding does **not** include that bridge — `pyfreerdp` gives you the protocol layer, not the UI. If you need a viewable desktop on Android, either:

- Use FreeRDP's prebuilt Android app (`aFreeRDP`) directly, or
- Port the JNI bridge from `client/Android/Studio/` and call into it from your app, with pyfreerdp handling the connection lifecycle.

## iOS

### Why ctypes is fragile here

- **App Store builds disallow `dlopen`.** A signed binary may only call into libraries that were linked at build time. `ctypes.CDLL("libfreerdp.dylib")` works in the simulator and in jailbroken / sideloaded builds, but it will get your app rejected from the Store.
- **No system Python.** You ship Python via BeeWare's [`Python-Apple-support`](https://github.com/beeware/Python-Apple-support) or similar.

The supported pattern is: link `libfreerdp` statically into your app's main binary, and access symbols via `ctypes.CDLL(None)` (the global symbol table) — `pyfreerdp` honours this if you set `PYFREERDP_LIBRARY=__SELF__`.

### Build static archives

```bash
python -m pyfreerdp.scripts.build_freerdp --target ios
```

This produces `pyfreerdp/_libs/ios/libfreerdp-client3.a` and friends, built against the iOS 13+ SDK for arm64 device. For simulator, edit the `PLATFORM` flag in `cmake/toolchains/ios.cmake` and rebuild.

### Integrate with BeeWare

In your `pyproject.toml`:

```toml
[tool.briefcase.app.myapp.iOS]
requires = [
    "pyfreerdp",
]
# Bundle the static archive into the app target so the linker picks it up.
extra_link_args = [
    "-L<path to pyfreerdp>/_libs/ios",
    "-lfreerdp-client3",
    "-lfreerdp3",
    "-lwinpr3",
    "-framework", "Security",
    "-framework", "CoreFoundation",
]
```

Then in your Python code:

```python
import os
os.environ["PYFREERDP_LIBRARY"] = "__SELF__"   # pull symbols from the host binary
import pyfreerdp
```

> **Note:** `__SELF__` mode is a planned enhancement in `loader.py`; currently the loader rejects non-existent paths. See `docs/ROADMAP.md`.

### App Store distribution

Anthropic's lawyers aren't writing Apple's. Confirm independently that your use of FreeRDP's Apache-2.0 / GPL-conditional code complies with Apple's licensing requirements before submission. FreeRDP's core is Apache-2.0; some optional plugins (e.g., the legacy MS-RDP licensing test code) are different.

## What "cross-platform" buys you and what it doesn't

This binding is genuinely cross-platform in that:

- The Python source has zero platform-specific imports beyond `sys.platform` checks in the loader.
- The settings, error mapping, input injection, and lifecycle code are identical on every OS.
- A test you write against the API runs the same everywhere.

It is **not** cross-platform in the sense that:

- A wheel built on Linux will not work on Windows (different `.so` vs `.dll`).
- Display + audio integration is unavoidably platform-specific. We don't render pixels — FreeRDP itself doesn't, in this configuration. Your app supplies the surface.
- Mobile distribution requires you to be the one who knows your mobile toolchain.

# Mobile — Android and iOS

Both platforms get libraries, bindings and wheels from `build-freerdp.yml`.
What differs is how you get them onto a device: neither runs `pip install`
the way a desktop does.

## Android

**Libraries:** `libfreerdp3.so` and friends for `arm64-v8a`, `armeabi-v7a`,
`x86_64`, `x86`, built against API 24 with `libc++_shared` bundled. Artifact
`freerdp-<ver>-android-<abi>-<profile>-<edition>.zip` unpacks to `_libs/`.

**Wheels:** `pyfreerdpnative-…-py3-none-android_24_<abi>.whl` (PEP 738).

### In your own app (Briefcase / Chaquopy / python-for-android)

Cross-install from a desktop into the app's Python site-packages:

```bash
pip install --platform android_24_arm64_v8a --only-binary :all: \
    --target app/src/main/python pyfreerdpnative \
    --find-links https://github.com/Usama01TN/PyFreeRdpNative/releases/expanded_assets/freerdp-libs-3.31.1
```

Then `load()` finds `pyfreerdpnative/_libs` inside the app and `dlopen`s from
there — Android permits loading native libraries from the app's private
storage.

### Termux / Pydroid 3

Their Pythons report `linux_aarch64`, not the Android tag, so pip refuses the
wheel by tag alone. The libraries themselves are the right ones (Bionic,
arm64). Retag and install:

```bash
pip install wheel
wget https://github.com/…/pyfreerdpnative-0.2.0-py3-none-android_24_arm64_v8a.whl
wheel tags --platform-tag any --remove pyfreerdpnative-0.2.0-py3-none-android_24_arm64_v8a.whl
pip install pyfreerdpnative-0.2.0-py3-none-any.whl
```

Check `uname -m` first (`aarch64` → `arm64_v8a`). Do **not** use the
`manylinux` wheel there: it needs glibc, Android has Bionic.

**Devices with 16 KB pages** (Android 15 on recent hardware) require
16 KB-aligned libraries. If `dlopen` fails with a page-size error, the build
needs `-Wl,-z,max-page-size=16384`; open an issue.

**The aFreeRDP APK** (`--target android-apk`) is FreeRDP's own Android
client, built for all ABIs plus a universal APK. It is unrelated to the Python
package; install `aFreeRDP-universal-release.apk` (Android 10+, uninstall any
previous aFreeRDP first — signing keys differ).

## iOS

**Libraries:** `.dylib` by default (`ios_linkage: dynamic`), for `OS64`
(device, all editions) and `SIMULATORARM64` (minimal/standard only — OpenH264's
iOS makefile targets the device SDK only). Media dependencies are linked *into*
the FreeRDP dylibs; `libc++` is linked explicitly because OpenH264 is C++ and
FreeRDP is a C project. `ios_linkage: static` gives `.a` archives instead.

**Wheels:** `pyfreerdpnative-…-py3-none-ios_13_0_arm64_iphoneos.whl` (PEP 730).

### What can load them

iOS enforces code signing on every executable page: a dylib loads **only from
inside a signed app bundle**, signed with the app's identity. Consequently:

| Python on the device | Can `ctypes` load FreeRDP? |
|---|---|
| a-Shell, Pyto, Pythonista (App Store) | **No** — they load only what their developer bundled |
| iSH | No — it emulates x86 Linux; wrong ABI and far too slow |
| Jailbroken device | Yes — signing is not enforced |
| **Your own app** (Briefcase / BeeWare, CPython 3.13+) | **Yes** — embed the dylibs as a framework, Xcode signs them, `load()` opens them from the bundle |

The cross-install command is the Android one with the iOS tag. Set
`PYFREERDP_LIBS` to the bundle's `Frameworks/` path if you place the dylibs
there rather than under `pyfreerdpnative/_libs`.

**The iFreeRDP app** (`--target ios-app`) is FreeRDP's own iOS client,
unsigned by default (`--sign-ios-app` to sign). It needs a provisioning
profile to run on a device.

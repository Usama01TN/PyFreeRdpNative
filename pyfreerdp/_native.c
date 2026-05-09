/*
 * pyfreerdp._native — a deliberately minimal C extension.
 *
 * Why this exists
 * ---------------
 * pyfreerdp itself is a pure-Python ctypes binding. Setuptools would
 * normally produce a `py3-none-any` (universal) wheel for it. That's
 * the right answer when users install FreeRDP separately via apt /
 * brew / vcpkg.
 *
 * For the BUNDLED wheel distribution — where we ship libfreerdp-client3
 * inside the wheel itself — we need a platform-tagged wheel
 * (`manylinux_2_28_x86_64` / `macosx_11_0_arm64` / `win_amd64` / etc.).
 * Without a platform tag, pip wouldn't know that this wheel only works
 * on the target architecture. The bundled `.so` / `.dylib` / `.dll` is
 * platform-specific even though the Python code isn't.
 *
 * Compiling this 30-line file forces setuptools to emit a platform tag.
 * The resulting `_native` module exposes one constant so importers can
 * verify "yes, this is a bundled wheel" at runtime — useful for
 * diagnostics ("am I using the apt-installed FreeRDP or the bundled
 * one?").
 *
 * This file is only compiled when PYFREERDP_BUILD_BUNDLED=1 is set in
 * the build environment. The default `python -m build` (no env var)
 * still produces a universal wheel, preserving the "system FreeRDP"
 * install path for users who want it.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>


static PyMethodDef _native_methods[] = {
    {NULL, NULL, 0, NULL}   /* sentinel */
};


static struct PyModuleDef _native_module = {
    PyModuleDef_HEAD_INIT,
    "_native",
    "Internal stub module that forces a platform-tagged wheel when "
    "pyfreerdp is built with bundled FreeRDP libraries.",
    -1,
    _native_methods,
    NULL, NULL, NULL, NULL
};


PyMODINIT_FUNC
PyInit__native(void)
{
    PyObject* m = PyModule_Create(&_native_module);
    if (m == NULL) {
        return NULL;
    }

    /* Marker so Python code can detect a bundled wheel:
     *
     *     try:
     *         from pyfreerdp import _native
     *         is_bundled = _native.IS_BUNDLED
     *     except ImportError:
     *         is_bundled = False   # universal/system wheel
     */
    if (PyModule_AddIntConstant(m, "IS_BUNDLED", 1) < 0) {
        Py_DECREF(m);
        return NULL;
    }

    /* Build identification — populated at compile time from env vars set
     * by the cibuildwheel BEFORE_BUILD scripts. Empty string if not set. */
#ifndef PYFREERDP_BUILT_FREERDP_VERSION
#define PYFREERDP_BUILT_FREERDP_VERSION ""
#endif
    if (PyModule_AddStringConstant(
            m, "FREERDP_VERSION", PYFREERDP_BUILT_FREERDP_VERSION) < 0) {
        Py_DECREF(m);
        return NULL;
    }

#ifndef PYFREERDP_BUILT_PLATFORM_TAG
#define PYFREERDP_BUILT_PLATFORM_TAG ""
#endif
    if (PyModule_AddStringConstant(
            m, "PLATFORM_TAG", PYFREERDP_BUILT_PLATFORM_TAG) < 0) {
        Py_DECREF(m);
        return NULL;
    }

    return m;
}

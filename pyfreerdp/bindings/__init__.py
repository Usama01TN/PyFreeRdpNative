"""
Low-level ctypes bindings for FreeRDP.

This subpackage is split from the high-level Pythonic facade so that:
  * the high-level API can be tested without loading any native code, and
  * downstream embedders that want raw access to FreeRDP function pointers
    can import from pyfreerdp.bindings directly.
"""

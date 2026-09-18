"""Shared fixtures: the generated package and, if present, the loaded libraries."""
import os

import pytest

import pyfreerdpnative


def _libs_present():
    d = pyfreerdpnative.default_libs_dir()
    return os.path.isdir(d) and any(
        f.startswith(("libfreerdp3", "freerdp3")) for f in os.listdir(d))


@pytest.fixture(scope="session")
def api():
    """Loaded, prototype-bound FreeRDP libraries; skips when _libs is empty."""
    if not _libs_present():
        pytest.skip("no FreeRDP libraries in pyfreerdpnative/_libs")
    return pyfreerdpnative.load()


def pytest_collection_modifyitems(config, items):
    if _libs_present():
        return
    skip = pytest.mark.skip(reason="needs the FreeRDP libraries in _libs")
    for item in items:
        if "needs_lib" in item.keywords:
            item.add_marker(skip)

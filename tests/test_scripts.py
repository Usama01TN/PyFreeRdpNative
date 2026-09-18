"""The build/packaging scripts stay importable and version-consistent."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")


def _version_of(script):
    out = subprocess.check_output([sys.executable, os.path.join(SCRIPTS, script), "--help"],
                                  stderr=subprocess.STDOUT).decode()
    assert "usage" in out.lower(), script


def test_scripts_parse_help():
    for s in ("build_freerdp.py", "build_deps.py", "validate_build.py",
              "package_wheels.py", "gen_bindings.py"):
        _version_of(s)


def test_build_scripts_agree_on_version():
    versions = set()
    for s in ("build_freerdp.py", "build_deps.py", "validate_build.py"):
        text = open(os.path.join(SCRIPTS, s)).read()
        for line in text.splitlines():
            if line.startswith("BUILD_SCRIPT_VERSION ="):
                versions.add(line.split("=")[1].strip())
    assert len(versions) == 1, versions

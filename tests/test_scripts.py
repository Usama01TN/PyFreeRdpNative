"""The build/packaging scripts stay importable and version-consistent."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")


def _run(script, *args):
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, script)] + list(args),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return r.returncode, r.stdout.decode(errors="replace")


def test_scripts_parse_help():
    """
    Every script must answer --help with only the standard library present:
    the CI unit job installs nothing but pytest, and a script that imports an
    optional dependency at module level (as gen_bindings.py once did with
    pycparser) fails here before argparse ever runs.
    """
    for s in ("build_freerdp.py", "build_deps.py", "validate_build.py",
              "package_wheels.py", "gen_bindings.py"):
        rc, out = _run(s, "--help")
        assert rc == 0, "{0} --help exited {1}:\n{2}".format(s, rc, out[-800:])
        assert "usage" in out.lower(), s


def test_gen_bindings_explains_missing_pycparser(tmp_path):
    """Without pycparser a real run fails fast with an actionable message."""
    block = tmp_path / "pycparser"
    block.mkdir()
    (block / "__init__.py").write_text("raise ImportError('blocked by test')\n")
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "gen_bindings.py"),
                        "--include", str(tmp_path), "--out", str(tmp_path / "out")],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    assert r.returncode != 0
    assert b"pip install pycparser" in r.stdout


def test_build_scripts_agree_on_version():
    versions = set()
    for s in ("build_freerdp.py", "build_deps.py", "validate_build.py"):
        text = open(os.path.join(SCRIPTS, s)).read()
        for line in text.splitlines():
            if line.startswith("BUILD_SCRIPT_VERSION ="):
                versions.add(line.split("=")[1].strip())
    assert len(versions) == 1, versions

# Publishing pyfreerdp

The `release.yml` workflow handles wheel building, multi-platform testing, and publishing to PyPI / TestPyPI. This document covers the one-time setup required before it works end-to-end, plus the day-to-day release process.

## What gets built

`pyfreerdp` produces a single universal wheel (`pyfreerdp-X.Y.Z-py3-none-any.whl`) plus a source distribution. Because this is a `ctypes` binding (not a C extension), one wheel is enough for every platform — the platform-specific behavior lives in `loader.py` at runtime, not in compiled code.

Users still install FreeRDP itself from their package manager:

```bash
sudo apt install libfreerdp-client3-3        # Debian/Ubuntu
sudo dnf install freerdp-libs                # Fedora/RHEL
brew install freerdp                         # macOS
vcpkg install freerdp:x64-windows            # Windows
```

The wheel doesn't bundle FreeRDP. See README's "What you don't get" section for the rationale.

## One-time setup

### 1. Configure trusted publishing on PyPI

Go to https://pypi.org/manage/account/publishing/ and add a "pending publisher" with these values:

| Field | Value |
|---|---|
| PyPI Project Name | `PyFreeRdpNative` |
| Owner | *your-github-username-or-org* |
| Repository name | `PyFreeRdpNative` (or whatever your repo is named) |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

Repeat at https://test.pypi.org/manage/account/publishing/ with environment name `testpypi`.

Trusted publishing means GitHub Actions authenticates to PyPI via OIDC tokens issued by GitHub at job time. You don't manage any API tokens — there's nothing to leak, rotate, or expire.

### 2. Configure GitHub environments

Repository settings → Environments → New environment, twice:

- **`testpypi`** — no protection rules needed. Used for every push to main.
- **`pypi`** — add the protection rule "Required reviewers" listing yourself, so a tag push doesn't accidentally publish a release without a human approval click.

### 3. Update repository URLs

Edit `pyproject.toml` and replace the placeholder `github.com/example/pyfreerdp` URLs in `[project.urls]` with your actual repository URL. PyPI displays these as clickable links on the project page.

## Releasing a new version

1. **Bump the version** in `pyproject.toml` (`[project].version`) and `pyfreerdp/version.py` (`__version__`). Both must match — the workflow's `verify-version` step blocks the release otherwise.

2. **Update changelog / docs** as needed.

3. **Commit and push to main.** The `release` workflow runs the multi-platform test matrix and publishes to TestPyPI. It also creates/overwrites a rolling pre-release at https://github.com/`<owner>`/`<repo>`/releases/tag/main-latest. Confirm `pip install -i https://test.pypi.org/simple/ pyfreerdp==X.Y.Z` works in a clean venv.

4. **Tag and push:**

   ```bash
   git tag v0.2.1
   git push origin v0.2.1
   ```

5. **Approve the deployment** when GitHub prompts (the `pypi` environment requires reviewer approval). The workflow then:
   - Re-runs the test matrix against the tagged commit
   - Publishes the wheel + sdist to PyPI
   - Generates SLSA build provenance attestations (visible on the PyPI project page)
   - Signs the wheel + sdist with Sigstore
   - Cuts a GitHub release with the artifacts attached and signatures

## Rolling pre-release for sharing dev builds

Every push to `main` creates or overwrites a `main-latest` GitHub Release with the freshly built wheel and sdist attached. This gives you a stable URL you can share with people who want to try the current development build without waiting for a tag:

```bash
pip install --force-reinstall \
    https://github.com/<owner>/<repo>/releases/download/main-latest/pyfreerdp-0.2.0-py3-none-any.whl
```

The release is marked as a prerelease so the repo homepage's "Latest release" badge keeps pointing at the most recent `v*` tag, not at this rolling build.

A few things to know about rolling builds:

- **The version number is the same as what's in `pyproject.toml`.** If you've bumped to `0.3.0` in main but haven't tagged yet, the rolling build will be `pyfreerdp-0.3.0-py3-none-any.whl`. That can collide with future installs — `--force-reinstall` is your friend.
- **No Sigstore signatures or SLSA attestations.** Those are reserved for tagged releases. Rolling builds are convenience-grade, not security-grade.
- **The previous `main-latest` is deleted on each new push.** If you need a permanent record of a specific build, grab it from the workflow run's artifacts before the next push, or just `git checkout` that commit and run the build locally.
- **`workflow_dispatch` runs use a stamped dev version** (e.g. `0.2.0.dev123+abc1234`) so multiple manual builds don't collide. Push-to-main builds use the bare `pyproject.toml` version because they only land on TestPyPI (which uses `skip-existing`) and the rolling release (which is overwritten).

## Troubleshooting

**`twine check` fails with "long_description has syntax errors"**
README.md uses a Markdown feature PyPI's renderer doesn't support. Run `twine check dist/*` locally to see the exact line. Common culprit: HTML inside Markdown that PyPI's bleach config strips.

**Wheel build produces a platform-tagged wheel instead of `py3-none-any`**
Somebody added a C extension or platform-conditional code in `setup.py`. The workflow's "Verify wheel is platform-universal" step catches this. Either remove the extension or update the workflow's check to allow the new platform tag.

**Test matrix Linux jobs can't apt-install FreeRDP**
The runner is on a distro that doesn't have FreeRDP packages (Ubuntu 20.04 for the older versions). The workflow has a fallback to FreeRDP 2.x — that's fine for the loader test (we just want to confirm find_library works) but the API-level tests will skip. If you need 3.x specifically, pin the runner to `ubuntu-22.04` or newer.

**macOS test job fails with "cannot find libfreerdp-client3"**
Homebrew installed FreeRDP to a non-standard prefix. Set `PYFREERDP_CLIENT_LIBRARY` to the absolute path, or update `loader.py`'s `_extra_search_dirs` to include the new prefix.

**Trusted publisher OIDC fails with "no pending publisher matches"**
The configuration on PyPI doesn't match the workflow exactly — workflow filename, environment name, and repository name all must match character-for-character. Check the values again at https://pypi.org/manage/account/publishing/.

## Bundled wheels

Alongside the universal `py3-none-any` wheel, PyPI also receives platform-tagged **bundled wheels** that ship `libfreerdp` and its transitive dependencies inside the wheel itself. Users on common platforms get a working install with no `apt install` step:

```bash
# On Linux x86_64, macOS arm64, Windows x64, etc:
pip install PyFreeRdpNative
# pip automatically picks the bundled platform-tagged wheel.

# On unusual platforms (FreeBSD, Linux ppc64le, ...):
pip install PyFreeRdpNative
# pip falls back to the universal wheel; install FreeRDP via your
# package manager.
```

The `.github/workflows/wheels.yml` workflow handles bundled wheels:

| Trigger | Action |
|---|---|
| Push of `v*` tag | Build all 6 platforms, publish to PyPI |
| Weekly cron (Monday 03:17 UTC) | Build all 6 platforms against latest stable FreeRDP, publish to TestPyPI as `0.2.0.dev<YYYYWW>` |
| `workflow_dispatch` | Manual run, choose where (if anywhere) to publish |

The cron rebuild keeps bundled wheels fresh against OpenSSL CVEs and other dep updates. If a cron run fails, the workflow opens a `cron-failure`-labeled issue automatically.

### Detecting which wheel is installed

```python
import pyfreerdp
try:
    from pyfreerdp import _native
    print("Bundled wheel, FreeRDP", _native.FREERDP_VERSION)
except ImportError:
    print("Universal wheel; using system-installed FreeRDP")
```

### Maintenance burden

Bundled wheels mean **you take on responsibility for FreeRDP's security updates**. When OpenSSL ships a CVE (every 2-3 months), every bundled wheel on PyPI contains the old, vulnerable libssl until you publish a new release. The weekly cron handles this *automatically* by rebuilding against the latest stable underlying images, but if the cron breaks and you don't notice, users can be running stale OpenSSL for weeks.

Mitigations already in place:
- Weekly cron auto-bumps the FreeRDP version and rebuilds against the latest manylinux/musllinux/macOS images
- Failed cron runs open a GitHub issue
- Bundled wheels publish to **TestPyPI** on cron, not real PyPI - users opt into the dev rebuilds explicitly
- Tagged releases publish to real PyPI; you have to actively cut a release to push a new bundled wheel to the masses

If you're not prepared to monitor the cron and cut frequent releases, drop the cron schedule from `wheels.yml` and only build bundled wheels on tag pushes. Users will be on stale OpenSSL between releases — make that a release-frequency commitment.

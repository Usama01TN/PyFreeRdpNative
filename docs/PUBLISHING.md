# Publishing

Two workflows; the second consumes the first.

## `build-freerdp.yml` — build, generate, package, release

Runs on push to `main`, on release, and manually. For the FreeRDP tag it is
given (default `3.31.1`) it:

1. builds the libraries for every desktop platform, Android ABI and iOS
   platform, in every `profile × edition` combination selected;
2. validates each build (loads cold, channels present, media backends work,
   executables run, Kerberos via SSPI);
3. **regenerates the bindings from the same FreeRDP checkout** and asserts the
   anchors (`rdpContext` layout, key ids, prototypes);
4. packages one wheel per platform **per variant** (`pyfreerdpnative`,
   `pyfreerdpnative-minimal`, `-ffmpeg`, …), audits the Linux ones with
   auditwheel, installs the x86-64 one in a clean venv and loads the library
   from it;
5. uploads every wheel and every library zip as its **own** artifact
   (named after the file), and — on `main`, on release, or with
   `publish_release` — attaches all of them to the rolling pre-release
   `freerdp-libs-<tag>` with permanent download URLs.

Useful dispatch inputs: `profiles`, `editions`, `mobile_apps`, `wheels`,
`ios_linkage`, `win_target`/`win_crt`/`win_toolset` (experimental legacy
Windows), `apk_abis`.

### Where things land

| Want | Where |
|---|---|
| a wheel for one platform | run → Artifacts → `pyfreerdpnative-0.2.0-py3-none-<tag>.whl` |
| raw libraries | run → Artifacts → `freerdp-<tag>-<platform>-<profile>-<edition>` (unzips to `_libs/`) |
| permanent links | Releases → `freerdp-libs-<tag>` |
| the summary table | run → Summary |

Artifacts expire after 30 days; release assets do not.

## `release.yml` — publish to (Test)PyPI

Manual. Give it the run id of a green `build-freerdp` run and a target index;
it downloads that run's `pyfreerdpnative*` artifacts, runs `twine check`, and
publishes with PyPI trusted publishing (configure the `testpypi` / `pypi`
environments in the repository settings; no API tokens are stored).

`packages` limits the upload to some variants, e.g. `pyfreerdpnative,pyfreerdpnative-minimal`.

Publish to TestPyPI first; `pip install --index-url https://test.pypi.org/simple/ pyfreerdpnative`
exercises the whole path.

## Version bumps

- **FreeRDP version:** change the `freerdp_ref` default in `build-freerdp.yml`
  and re-run `scripts/gen_bindings.py` (CI's `bindings-up-to-date` job fails
  until the committed package matches the new headers).
- **Package version:** `version` in `pyproject.toml`. Every variant shares it.
- **Build scripts:** `BUILD_SCRIPT_VERSION` in the three build scripts and
  `--require-version` in both workflows move together; the handshake fails
  fast when they disagree. `package_wheels.py` has its own
  `PACKAGE_SCRIPT_VERSION`.

## Checklist before a release

- `ci.yml` green (bindings match the generator, tests pass on 3.8–3.14).
- `build-freerdp.yml` green for `profiles: both`, `editions: all`.
- Wheel summary shows one `installed wheel loads FreeRDP <tag>` line.
- `release.yml` to TestPyPI, install from there on one machine per OS.

#!/usr/bin/env bash
# Resolve the most recent stable FreeRDP release tag from GitHub.
#
# Stable = matches /^[0-9]+\.[0-9]+\.[0-9]+$/. Excludes RCs, betas, and
# the rolling 'master' branch. Prints the tag (e.g. "3.16.0") to stdout.
#
# If the GitHub API is unreachable or rate-limited (commonly happens in
# CI when no GITHUB_TOKEN is set), falls back to PYFREERDP_FREERDP_REF
# env var if set, then to the hardcoded sentinel below.
#
# Usage:
#   FREERDP_VERSION=$(./.github/cibw-scripts/get_freerdp_version.sh)
set -euo pipefail

FALLBACK_VERSION="3.16.0"

# Prefer caller's explicit pin if they set one.
if [ -n "${PYFREERDP_FREERDP_REF:-}" ]; then
  echo "${PYFREERDP_FREERDP_REF}"
  exit 0
fi

# GitHub API call. Authenticated when GITHUB_TOKEN is available (60 vs
# 5000 req/hour rate limit). cibuildwheel exports CIBW_ENVIRONMENT_PASS_LINUX
# so GITHUB_TOKEN reaches the manylinux container.
auth_header=()
if [ -n "${GITHUB_TOKEN:-}" ]; then
  auth_header=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
fi

# /releases/latest only returns "latest non-prerelease non-draft" but
# requires the project to mark its releases as such on GitHub. FreeRDP
# does this consistently. Fall back to /tags scanning if /latest 404s.
latest_url="https://api.github.com/repos/FreeRDP/FreeRDP/releases/latest"

if response=$(curl -fsSL "${auth_header[@]}" "${latest_url}" 2>/dev/null); then
  # Extract tag_name with a small Python one-liner to avoid jq dep.
  tag=$(printf '%s' "${response}" | python3 -c \
    "import sys, json; print(json.load(sys.stdin).get('tag_name','').lstrip('v'))" \
    2>/dev/null || true)
  if printf '%s' "${tag}" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "${tag}"
    exit 0
  fi
fi

# Fallback: list tags and pick the highest stable one.
tags_url="https://api.github.com/repos/FreeRDP/FreeRDP/tags?per_page=100"
if response=$(curl -fsSL "${auth_header[@]}" "${tags_url}" 2>/dev/null); then
  tag=$(printf '%s' "${response}" | python3 <<'PY'
import json, re, sys
try:
    tags = json.load(sys.stdin)
    stable = [t["name"].lstrip("v") for t in tags
              if re.match(r"^v?[0-9]+\.[0-9]+\.[0-9]+$", t["name"])]
    if stable:
        # Sort by parsed integer tuple, take highest.
        stable.sort(key=lambda v: tuple(int(p) for p in v.split(".")))
        print(stable[-1])
except Exception:
    pass
PY
  )
  if printf '%s' "${tag}" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "${tag}"
    exit 0
  fi
fi

# Network unreachable or unexpected response shape. Use sentinel so the
# build continues; weekly cron will catch the upgrade later.
echo "${FALLBACK_VERSION}"

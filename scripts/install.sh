#!/usr/bin/env bash
# claude-rotate install script
#
# Installs claude-rotate from GitHub using whichever installer is available
# (uv → pipx → bootstrap pipx via pip). Safe to re-run for upgrades.
#
# claude-rotate is not on PyPI yet; until a tagged release lands, we install
# directly from the git source.

set -euo pipefail

REPO="${CLAUDE_ROTATE_REPO:-https://github.com/codename-cn/claude-rotate}"
SRC="git+${REPO}"

msg() { printf "==> %s\n" "$*" >&2; }
err() { printf "!! %s\n" "$*" >&2; exit 1; }

if command -v uv >/dev/null 2>&1; then
    msg "Installing claude-rotate via uv tool (source: ${REPO})"
    uv tool install --force "${SRC}"
    exit 0
fi

if command -v pipx >/dev/null 2>&1; then
    msg "Installing claude-rotate via pipx (source: ${REPO})"
    pipx install --force "${SRC}"
    exit 0
fi

if command -v python3 >/dev/null 2>&1; then
    PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info[0])')
    PY_MINOR=$(python3 -c 'import sys; print(sys.version_info[1])')
    if [[ "${PY_MAJOR}" -lt 3 ]] || [[ "${PY_MAJOR}" -eq 3 && "${PY_MINOR}" -lt 11 ]]; then
        err "Python 3.11+ required (found ${PY_MAJOR}.${PY_MINOR}). Install uv or a newer Python."
    fi
    msg "No uv/pipx found; bootstrapping pipx via pip"
    python3 -m pip install --user pipx
    python3 -m pipx ensurepath || true
    export PATH="${HOME}/.local/bin:${PATH}"
    command -v pipx >/dev/null 2>&1 || err "pipx bootstrap failed. Install manually: https://pipx.pypa.io"
    pipx install --force "${SRC}"
    exit 0
fi

err "No usable Python installer found. Install uv (https://docs.astral.sh/uv/) or Python 3.11+."

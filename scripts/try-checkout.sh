#!/usr/bin/env bash
# Launch this checkout's Desktop, TUI, or CLI in a private Hermes home.
# Nothing here writes the installed profile, tool store, Desktop app, or
# application menu. Trial state lives in <checkout>/.try-checkout/.
#
#   scripts/try-checkout.sh desktop
#   scripts/try-checkout.sh tui
#   scripts/try-checkout.sh cli
#
# Windows PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\try-checkout.ps1 desktop
#
# TRY_CHECKOUT_SKIP_SETUP and HERMES_TRY_LAUNCHER are for tests. Leave them unset.

if [ -z "${BASH_VERSION-}" ]; then
  echo "error: run this script with bash" >&2
  exit 1
fi
# Sourcing would export the trial home into the caller's shell. Their next
# `hermes` would then use the trial profile.
if [ "${BASH_SOURCE[0]}" != "$0" ]; then
  echo "error: run scripts/try-checkout.sh; do not source it" >&2
  return 1
fi

set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX="${HERMES_TRY_DIR:-$REPO/.try-checkout}"

usage() {
  cat <<EOF
Usage: scripts/try-checkout.sh <desktop|tui|cli|build> [options]

Run this checkout's Desktop, TUI, or CLI in a private Hermes home.
The installed Hermes profile, tool store, and Desktop app are left alone.

  desktop            Build if needed, then open Desktop
  desktop --rebuild  Rebuild Desktop, then open it
  tui                Open the terminal UI (later args are passed through)
  cli                Open the classic CLI (later args are passed through)
  build              Build Desktop without opening a window
  exec -- CMD...     Run CMD in the private home (after the trial Python exists)

Windows:
  powershell -ExecutionPolicy Bypass -File scripts\\try-checkout.ps1 desktop

The trial is <checkout>/.try-checkout/. Delete that folder to reset it.
Sign in again inside the trial. It keeps its own profile.
The first Desktop or TUI launch installs Python and builds the UI.
EOF
}

apply_isolation() {
  export HERMES_HOME="$SANDBOX/home"
  export HERMES_RUNTIME_DIR="$SANDBOX/tools"
  export HERMES_DESKTOP_USER_DATA_DIR="$SANDBOX/desktop"
  export HERMES_DESKTOP_APP_NAME="Hermes Try"
  export HERMES_DESKTOP_IGNORE_EXISTING=1
  export HERMES_DESKTOP_ISOLATED_BACKEND=1
  export HERMES_DESKTOP_HERMES_ROOT="$REPO"
  # `hermes desktop` writes the Linux menu entry under XDG_DATA_HOME.
  # Point it at the trial so the installed Hermes launcher is not replaced.
  export XDG_DATA_HOME="$SANDBOX/xdg"
  export UV_CACHE_DIR="$SANDBOX/uv-cache"
  export NPM_CONFIG_CACHE="$SANDBOX/npm-cache"
  export npm_config_cache="$SANDBOX/npm-cache"
  export NPM_CONFIG_USERCONFIG="$SANDBOX/npmrc"
  # These would send the trial at another install or a running dev server.
  # __HERMES_ACTIVATED would make setup skip and reuse the caller's real env.
  unset VIRTUAL_ENV PYTHONHOME HERMES_DESKTOP_HERMES HERMES_TUI_DIR \
    HERMES_DESKTOP_DEV_SERVER HERMES_DESKTOP_IS_PACKAGED __HERMES_ACTIVATED \
    __HERMES_TEST_PYTHON
  mkdir -p "$HERMES_HOME" "$HERMES_RUNTIME_DIR" "$HERMES_DESKTOP_USER_DATA_DIR" \
    "$XDG_DATA_HOME" "$UV_CACHE_DIR" "$NPM_CONFIG_CACHE"
  [ -f "$SANDBOX/npmrc" ] || : > "$SANDBOX/npmrc"
}

# Git Bash spawns Windows Python and Electron, which do not understand /c/Users paths.
to_native_paths() {
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) ;;
    *) return 0 ;;
  esac
  command -v cygpath >/dev/null 2>&1 || return 0
  local name value
  for name in HERMES_HOME HERMES_RUNTIME_DIR HERMES_DESKTOP_USER_DATA_DIR \
    HERMES_DESKTOP_HERMES_ROOT HERMES_DESKTOP_PYTHON XDG_DATA_HOME UV_CACHE_DIR \
    NPM_CONFIG_CACHE npm_config_cache NPM_CONFIG_USERCONFIG; do
    value="${!name:-}"
    if [ -n "$value" ]; then
      printf -v "$name" '%s' "$(cygpath -m "$value")"
      export "$name"
    fi
  done
}

banner() {
  cat >&2 <<EOF
Trial home: $HERMES_HOME
Desktop data: $HERMES_DESKTOP_USER_DATA_DIR
This trial keeps its own profile. Sign in again here.
Installed Hermes is left alone. Delete $SANDBOX to reset.
The first Desktop or TUI launch installs Python and builds the UI.
EOF
}

ensure_desktop_python() {
  # `python` on PATH is the tool-store binary. Its sys.executable is that same
  # binary, and Desktop launches it with PYTHONPATH set to the checkout only,
  # which drops the venv. Ask PM for the venv interpreter and do not resolve it:
  # resolving the symlink also drops the venv.
  if [ -n "${HERMES_DESKTOP_PYTHON:-}" ] && [ -f "$HERMES_DESKTOP_PYTHON" ]; then
    return 0
  fi
  if ! HERMES_DESKTOP_PYTHON="$(python -c 'import os; from pathlib import Path; from pm.environments import project_python; print(project_python(Path(os.environ["HERMES_DESKTOP_HERMES_ROOT"])))')"; then
    echo "error: the trial Python did not start" >&2
    exit 1
  fi
  HERMES_DESKTOP_PYTHON="${HERMES_DESKTOP_PYTHON//$'\r'/}"
  export HERMES_DESKTOP_PYTHON
  to_native_paths
  if [ ! -f "$HERMES_DESKTOP_PYTHON" ]; then
    echo "error: no Hermes interpreter at $HERMES_DESKTOP_PYTHON" >&2
    echo "The trial environment did not finish installing. Re-run this command." >&2
    exit 1
  fi
}

launch() {
  if [ -n "${HERMES_TRY_LAUNCHER:-}" ]; then
    exec "$HERMES_TRY_LAUNCHER" "$@"
  fi
  exec python -m hermes_cli.main "$@"
}

original=("$@")
surface="${1:-}"
if [ "$#" -gt 0 ]; then
  shift
fi

case "$surface" in
  -h|--help)
    usage
    exit 0
    ;;
  "")
    usage >&2
    exit 1
    ;;
  desktop|tui|cli|build|exec) ;;
  *)
    echo "error: unknown command '$surface'" >&2
    usage >&2
    exit 1
    ;;
esac

apply_isolation

if [ -z "${TRY_CHECKOUT_INNER:-}" ] && [ "${TRY_CHECKOUT_SKIP_SETUP:-}" != 1 ]; then
  if ! command -v curl >/dev/null 2>&1; then
    echo "error: curl is required to install the trial Python" >&2
    exit 1
  fi
  banner
  export TRY_CHECKOUT_INNER=1
  exec "$REPO/scripts/run-in-hermes-env" bash "$SCRIPT_PATH" "${original[@]}"
fi

to_native_paths

case "$surface" in
  exec)
    if [ "${1:-}" = "--" ]; then
      shift
    fi
    if [ "$#" -eq 0 ]; then
      echo "error: exec needs a command" >&2
      exit 1
    fi
    exec "$@"
    ;;
  desktop|build)
    args=(desktop --source --ignore-existing --hermes-root "$HERMES_DESKTOP_HERMES_ROOT")
    if [ "$surface" = "build" ]; then
      args+=(--build-only)
    fi
    if [ "${1:-}" = "--rebuild" ]; then
      args+=(--force-build)
      shift
    fi
    if [ "$#" -ne 0 ]; then
      echo "error: $surface only accepts --rebuild" >&2
      exit 1
    fi
    ensure_desktop_python
    if [ "$surface" = "build" ]; then
      echo "Building the trial Desktop." >&2
    else
      echo "Opening the trial Desktop." >&2
    fi
    launch "${args[@]}"
    ;;
  tui)
    echo "Opening the trial TUI." >&2
    launch --tui "$@"
    ;;
  cli)
    echo "Opening the trial CLI." >&2
    launch "$@"
    ;;
esac

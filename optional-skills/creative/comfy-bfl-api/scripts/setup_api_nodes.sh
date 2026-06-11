#!/usr/bin/env bash
# setup_api_nodes.sh — Stand up a CPU-only ComfyUI with the BFL API node pack
# for the comfy-bfl-api skill.
#
# Inference happens on BFL's hosted API (api.bfl.ai), so this install needs
# NO GPU and downloads NO model weights. ComfyUI is just the
# workflow/orchestration engine.
#
# Node pack installed:
#   * gelasdev/ComfyUI-FLUX-BFL-API — FLUX.2 Pro/Max/Flex/Klein, Kontext
#     editing, Fill, ControlNet, straight against api.bfl.ai with your own
#     BFL key
#
# Idempotent: safe to re-run. Steps already done are skipped.
#
# Usage:
#   bash setup_api_nodes.sh [--workspace=PATH] [--port=N] [--no-launch] [--smoke-test]
#
#   --workspace=PATH  ComfyUI workspace (default: ~/comfy/ComfyUI, or $COMFY_WORKSPACE)
#   --port=N          Server port (default: 8188, or $COMFY_PORT)
#   --no-launch       Install everything but don't start the server
#   --smoke-test      After launch, run a real FLUX.2 generation end-to-end
#                     (requires BFL_API_KEY; costs a few cents)

set -euo pipefail

WORKSPACE="${COMFY_WORKSPACE:-$HOME/comfy/ComfyUI}"
PORT="${COMFY_PORT:-8188}"
BFL_PACK_REPO="https://github.com/gelasdev/ComfyUI-FLUX-BFL-API"
BFL_PACK_DIRNAME="ComfyUI-FLUX-BFL-API"
# Pinned to the commit audited on 2026-06-11 (v1.2.0). Bump deliberately after
# re-reviewing the diff — do not float on master.
BFL_PACK_COMMIT="63da662ebc9dcda06d95558bb3be0890ed07dd2d"
DO_LAUNCH=1
DO_SMOKE=0

for arg in "$@"; do
  case "$arg" in
    --workspace=*) WORKSPACE="${arg#*=}" ;;
    --port=*)      PORT="${arg#*=}" ;;
    --no-launch)   DO_LAUNCH=0 ;;
    --smoke-test)  DO_SMOKE=1 ;;
    -h|--help)     grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown flag: $arg (see --help)" >&2; exit 2 ;;
  esac
done

log()  { printf '\n\033[1;36m[comfy-api-setup]\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m[comfy-api-setup]\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[1;31m[comfy-api-setup]\033[0m %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 is required"
command -v git     >/dev/null 2>&1 || die "git is required"

# ---------------------------------------------------------------------------
# 1. comfy-cli
# ---------------------------------------------------------------------------
if command -v comfy >/dev/null 2>&1; then
  log "comfy-cli already installed: $(comfy --version 2>/dev/null | head -1 || echo ok)"
else
  log "Installing comfy-cli..."
  if command -v pipx >/dev/null 2>&1; then
    pipx install comfy-cli
  elif command -v uv >/dev/null 2>&1; then
    uv tool install comfy-cli
  else
    python3 -m pip install --user comfy-cli
  fi
  command -v comfy >/dev/null 2>&1 || die "comfy-cli installed but 'comfy' is not on PATH — open a new shell or add the install dir to PATH, then re-run"
fi

comfy --skip-prompt tracking disable >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 2. ComfyUI itself (CPU — deliberate: BFL's API does the inference)
# ---------------------------------------------------------------------------
if [ -f "$WORKSPACE/main.py" ]; then
  log "ComfyUI already present at $WORKSPACE"
else
  log "Installing ComfyUI (CPU) into $WORKSPACE ... (a few minutes; downloads torch)"
  comfy --skip-prompt --workspace "$WORKSPACE" install --cpu
fi
comfy --skip-prompt set-default "$WORKSPACE" >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 3. BFL node pack
# ---------------------------------------------------------------------------
BFL_NODE_DIR="$WORKSPACE/custom_nodes/$BFL_PACK_DIRNAME"
if [ ! -d "$BFL_NODE_DIR/.git" ]; then
  log "Cloning BFL node pack into custom_nodes/ ..."
  git clone "$BFL_PACK_REPO" "$BFL_NODE_DIR"
fi
log "Pinning BFL node pack to audited commit ${BFL_PACK_COMMIT:0:12}..."
git -C "$BFL_NODE_DIR" fetch --quiet origin "$BFL_PACK_COMMIT" 2>/dev/null || git -C "$BFL_NODE_DIR" fetch --quiet origin
git -C "$BFL_NODE_DIR" checkout --quiet "$BFL_PACK_COMMIT" \
  || die "Could not check out pinned commit $BFL_PACK_COMMIT — inspect $BFL_NODE_DIR manually"

# Install the pack's python deps into the SAME environment that runs ComfyUI.
# comfy-cli installs ComfyUI's deps into the python env that `comfy` itself
# runs from, so we resolve that interpreter and pip-install there.
log "Installing BFL node pack dependencies..."
COMFY_BIN="$(command -v comfy)"
COMFY_PY="$(head -1 "$COMFY_BIN" | sed 's/^#!//')"
if ! { [ -x "$COMFY_PY" ] && "$COMFY_PY" -m pip install --quiet -r "$BFL_NODE_DIR/requirements.txt"; }; then
  python3 -m pip install --user --quiet -r "$BFL_NODE_DIR/requirements.txt" \
    || warn "Could not install deps automatically; run: pip install -r $BFL_NODE_DIR/requirements.txt"
fi

# ---------------------------------------------------------------------------
# 4. BFL_API_KEY
# ---------------------------------------------------------------------------
HERMES_ENV="${HERMES_HOME:-$HOME/.hermes}/.env"
if [ -z "${BFL_API_KEY:-}" ] && [ -f "$HERMES_ENV" ]; then
  BFL_API_KEY="$(grep -E '^BFL_API_KEY=' "$HERMES_ENV" | tail -1 | cut -d= -f2- | tr -d '"' || true)"
fi
if [ -z "${BFL_API_KEY:-}" ]; then
  warn "No BFL_API_KEY found in the environment or $HERMES_ENV"
  printf 'Enter your BFL API key (from https://api.bfl.ai), or press Enter to skip: '
  read -r -s key_input
  echo
  if [ -n "$key_input" ]; then
    BFL_API_KEY="$key_input"
    mkdir -p "$(dirname "$HERMES_ENV")"
    printf '\nBFL_API_KEY=%s\n' "$BFL_API_KEY" >> "$HERMES_ENV"
    log "Saved BFL_API_KEY to $HERMES_ENV"
  else
    warn "Skipping key setup — generation will fail until BFL_API_KEY is set"
  fi
fi
export BFL_API_KEY="${BFL_API_KEY:-}"

# The key is injected per run via --args (see workflows/*.schema.json); it is
# deliberately NOT written into the pack's config.ini.

# ---------------------------------------------------------------------------
# 5. Launch + verify
# ---------------------------------------------------------------------------
if [ "$DO_LAUNCH" -eq 1 ]; then
  if curl -s --max-time 3 "http://127.0.0.1:$PORT/system_stats" >/dev/null 2>&1; then
    log "ComfyUI already running on port $PORT"
    warn "If you just installed the node pack, restart the server so it loads: comfy stop && re-run this script"
  else
    log "Launching ComfyUI in the background on port $PORT ..."
    comfy launch --background -- --port "$PORT" --cpu
    for _ in $(seq 1 30); do
      curl -s --max-time 2 "http://127.0.0.1:$PORT/system_stats" >/dev/null 2>&1 && break
      sleep 2
    done
  fi

  curl -s --max-time 3 "http://127.0.0.1:$PORT/system_stats" >/dev/null 2>&1 \
    || die "Server did not come up on port $PORT — check: comfy launch (foreground) for errors"

  if curl -s --max-time 5 "http://127.0.0.1:$PORT/object_info/Flux2Pro_BFL" | grep -q Flux2Pro_BFL; then
    log "BFL nodes are loaded — server is ready"
  else
    die "Server is up but Flux2Pro_BFL is not registered. Check custom node import errors in the ComfyUI log (likely missing deps in the server's python env)."
  fi
fi

# ---------------------------------------------------------------------------
# 6. Optional smoke test (real generation, BFL direct)
# ---------------------------------------------------------------------------
if [ "$DO_SMOKE" -eq 1 ]; then
  [ -n "${BFL_API_KEY:-}" ] || die "--smoke-test requires BFL_API_KEY"
  SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

  # Locate the bundled comfyui skill's scripts (we reuse its runner).
  COMFYUI_SCRIPTS=""
  for candidate in \
    "${COMFYUI_SKILL_DIR:-}/scripts" \
    "$SKILL_DIR/../../../skills/creative/comfyui/scripts" \
    "$HOME/.hermes/hermes-agent/skills/creative/comfyui/scripts" \
    "$HOME/.hermes/skills/comfyui/scripts"; do
    [ -n "$candidate" ] && [ -f "$candidate/run_workflow.py" ] && COMFYUI_SCRIPTS="$candidate" && break
  done
  [ -n "$COMFYUI_SCRIPTS" ] || die "Could not find the bundled comfyui skill's scripts — set COMFYUI_SKILL_DIR to its directory"

  log "Smoke test: FLUX.2 [pro] via api.bfl.ai (uses your BFL credit; a few cents)..."
  python3 "$COMFYUI_SCRIPTS/run_workflow.py" \
    --workflow "$SKILL_DIR/workflows/bfl_flux2_t2i.json" \
    --schema   "$SKILL_DIR/workflows/bfl_flux2_t2i.schema.json" \
    --args     "{\"api_key\": \"$BFL_API_KEY\", \"prompt\": \"a tiny robot watering a plant, simple illustration\"}" \
    --host     "http://127.0.0.1:$PORT" \
    --timeout  600 \
    --output-dir "$SKILL_DIR/outputs/smoke-test"
  log "Smoke test passed — output in $SKILL_DIR/outputs/smoke-test"
fi

log "Done. ComfyUI web UI: http://127.0.0.1:$PORT  |  workflows: $(cd "$(dirname "$0")/.." && pwd)/workflows"

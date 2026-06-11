---
name: comfy-bfl-api
description: "Run BFL FLUX image models via ComfyUI API nodes."
version: 0.3.0
author: [Rob Maron]
license: MIT
platforms: [macos, linux, windows]
compatibility: "Requires a ComfyUI server with the ComfyUI-FLUX-BFL-API node pack (scripts/setup_api_nodes.sh installs it) and a BFL API key."
prerequisites:
  commands: ["python3", "git"]
setup:
  help: "Run scripts/setup_api_nodes.sh — installs a CPU-only ComfyUI + the BFL node pack and launches the server. No GPU or model weights needed."
metadata:
  hermes:
    tags:
      - comfyui
      - bfl
      - flux
      - image-generation
      - image-editing
      - creative
    related_skills: [comfyui, image_gen]
    category: creative
---

# Comfy BFL API Skill

Drive image generation and editing through ComfyUI workflow graphs whose
generation nodes call Black Forest Labs directly (`api.bfl.ai` — FLUX.2
Pro/Max/Flex/Klein, Kontext editing, Fill, ControlNet) with your own BFL
key, instead of loading model weights locally. ComfyUI is the orchestration
layer; BFL's hosted API is the inference layer — any machine can run these
workflows, GPU or not. BFL's API is image-only today (no video models), so
this skill covers image pipelines.

This skill does NOT cover local-checkpoint generation (that's the bundled
`comfyui` skill) or single-shot generation without a workflow graph (that's
the `image_gen` tool, which is cheaper and simpler when no chaining is
needed).

## When to Use

- Multi-step generative pipelines: text → image → edit → upscale chains
- FLUX/BFL model work where the machine has no GPU for local weights
- Kontext-style instruction editing as part of a larger graph
- Sketching workflows that will later run on a remotely hosted ComfyUI
- Iterating on workflow graphs as the unit of work (export, parameterize, run)

Do NOT use when a single `image_gen` tool call would do — the direct tool is
simpler.

## Prerequisites

- A running ComfyUI server with the
  [ComfyUI-FLUX-BFL-API](https://github.com/gelasdev/ComfyUI-FLUX-BFL-API)
  node pack loaded. `scripts/setup_api_nodes.sh` installs everything
  (CPU-only ComfyUI via comfy-cli + the pack) and launches it.
- `BFL_API_KEY` in the environment or `~/.hermes/.env` (from
  https://api.bfl.ai). Generation bills against this key.
- The bundled `comfyui` skill — this skill reuses its execution scripts
  (`run_workflow.py` etc.); nothing is duplicated here.

## How to Run

First-time setup (idempotent; use `terminal`):

```bash
bash scripts/setup_api_nodes.sh                # install + launch
bash scripts/setup_api_nodes.sh --smoke-test   # + verify with a real FLUX.2 generation
```

Run a workflow — always through the bundled `comfyui` skill's runner, always
with the `--schema` sidecar, with the key injected from the env:

```bash
python3 <comfyui-skill>/scripts/run_workflow.py \
  --workflow workflows/bfl_flux2_t2i.json \
  --schema   workflows/bfl_flux2_t2i.schema.json \
  --args     "{\"api_key\": \"$BFL_API_KEY\", \"prompt\": \"...\"}" \
  --timeout 600 \
  --output-dir ./outputs
```

`<comfyui-skill>` is `skills/creative/comfyui` in the hermes-agent checkout.
The server defaults to `http://127.0.0.1:8188`; pass `--host` to target a
remote ComfyUI instead. Every workflow also exposes a `base_url` parameter
to repoint API traffic (a regional endpoint or a proxy of your own) — keep
host, key, and base URL out of the workflow JSON so the same workflow runs
anywhere.

## Quick Reference

| Task | Workflow | Notes |
|------|----------|-------|
| Text → image (FLUX.2) | `workflows/bfl_flux2_t2i.json` | `--timeout 600` |
| Edit image (Kontext) | `workflows/bfl_kontext_edit.json` | add `--input-image image=./photo.png` |
| Server health | — | `curl http://127.0.0.1:8188/object_info/Flux2Pro_BFL` |
| BFL credit balance | — | `FluxCredits_BFL` node, or the BFL dashboard |
| New workflow | — | design in web UI → Export (API) → write sidecar schema |

See `workflows/README.md` for full invocations and the parameter surface of
each workflow.

## Procedure

1. Confirm the server is up and the BFL nodes are loaded (Quick Reference
   health check). If not, run `scripts/setup_api_nodes.sh`.
2. Pick the closest workflow from `workflows/`; `read_file` its
   `<name>.schema.json` to see what's controllable.
3. Run it via the bundled `comfyui` skill's `run_workflow.py`, passing
   `--schema` and injecting `api_key` from `$BFL_API_KEY` in `--args`.
4. Collect outputs from `--output-dir`.
5. For a new pipeline: build the graph in the ComfyUI web UI with the "BFL"
   nodes, export in API format, blank the `x_key` field, write a sidecar
   schema (copy an existing one), smoke-test with the cheapest model
   (`Flux2Klein4b_BFL`) before using Pro/Max.

## Pitfalls

1. **The sidecar schema is mandatory.** The bundled skill's auto-extraction
   (`extract_schema.py`) only knows standard/community node types — it finds
   zero parameters in BFL_* nodes. Without `--schema`, every `--args` entry
   is skipped with an "unknown parameter" warning and the workflow runs with
   its baked-in defaults.
2. **Never bake the key into a workflow JSON or the pack's `config.ini`.**
   Inject per run via `--args`. The key passes through the ComfyUI server
   (and its history) — only use servers you control.
3. **A solid-black 512x512 output means failure, not art.** The BFL nodes
   swallow errors (bad key, moderation, timeout) and return a blank black
   image instead of raising. Treat black output as "read the ComfyUI server
   log", which also prints the exact curl of each BFL request.
4. **Timeouts.** BFL polls up to ~200 s server-side and the BFL nodes are
   not in the bundled skill's slow-node heuristic, so the default 300 s
   timeout can fire mid-run on slow generations. Use `--timeout 600`.
5. **Every run costs money.** BFL bills per image. Iterate on
   `Flux2Klein4b_BFL`; switch to Pro/Max for finals. Check balance with the
   `FluxCredits_BFL` node. Seeds: `-1` randomizes.
6. **Kontext needs base64 input.** BFL edit nodes take `input_image` as a
   base64 STRING, not an IMAGE tensor — wire `LoadImage →
   ImageToBase64_BFL → input_image` (see `bfl_kontext_edit.json`).
7. **`width`/`height` must be multiples of 32** for BFL nodes (0 = model
   default); the node raises otherwise.
8. **Node pack changes need a server restart.** ComfyUI imports custom nodes
   at startup; after updating the pack, `comfy stop` then relaunch.
9. **No video through BFL.** BFL's API has no video models (their
   text-to-video is unreleased). If a task needs video, say so rather than
   improvising another provider — video support is a deliberate non-goal of
   this skill for now.

## Verification

- [ ] `curl http://127.0.0.1:8188/system_stats` returns JSON
- [ ] `curl http://127.0.0.1:8188/object_info/Flux2Pro_BFL` lists the node
- [ ] `BFL_API_KEY` is set in env or `~/.hermes/.env`
- [ ] `setup_api_nodes.sh --smoke-test` produces a non-black PNG in
      `outputs/smoke-test/`

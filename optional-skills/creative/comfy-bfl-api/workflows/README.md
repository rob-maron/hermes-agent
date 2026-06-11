# BFL API-Node Example Workflows

API-format ComfyUI workflows whose generation nodes call Black Forest Labs
directly — your own `BFL_API_KEY` against `api.bfl.ai` via
[gelasdev/ComfyUI-FLUX-BFL-API](https://github.com/gelasdev/ComfyUI-FLUX-BFL-API)
(FLUX.2 Pro/Max/Flex/Klein, Kontext Pro/Max, Fill, ControlNet). No local
model weights, no GPU. BFL's API is image-only — no video models exist there
yet.

Each workflow ships with a `<name>.schema.json` sidecar that MUST be passed
via `--schema` when running through the bundled `comfyui` skill's
`run_workflow.py` (the BFL node types are not in that skill's built-in
parameter catalog, so auto-extraction finds nothing).

| Workflow | Purpose | Key params |
|----------|---------|------------|
| `bfl_flux2_t2i.json` | FLUX.2 [pro] text-to-image | `prompt`, `width`/`height`, `seed` |
| `bfl_kontext_edit.json` | Kontext [pro] image editing | `prompt`, `aspect_ratio` + `--input-image image=...` |

## Running

```bash
SCRIPTS=<path to skills/creative/comfyui/scripts>   # bundled comfyui skill

# Text → image
python3 "$SCRIPTS/run_workflow.py" \
  --workflow bfl_flux2_t2i.json \
  --schema   bfl_flux2_t2i.schema.json \
  --args     "{\"api_key\": \"$BFL_API_KEY\", \"prompt\": \"a lighthouse in a storm\"}" \
  --timeout 600 \
  --output-dir ./outputs

# Edit an existing image (Kontext)
python3 "$SCRIPTS/run_workflow.py" \
  --workflow bfl_kontext_edit.json \
  --schema   bfl_kontext_edit.schema.json \
  --input-image image=./photo.png \
  --args     "{\"api_key\": \"$BFL_API_KEY\", \"prompt\": \"replace the sky with a thunderstorm\"}" \
  --timeout 600 \
  --output-dir ./outputs
```

Notes:

- `api_key` is injected at run time from `$BFL_API_KEY`. The checked-in
  workflows deliberately keep the `FluxConfig_BFL.x_key` field empty — never
  commit a workflow with a real key baked in, and don't write keys into the
  node pack's `config.ini`.
- `base_url` is injectable on every workflow — point it at a regional
  endpoint or a proxy of your own without touching the workflow.
- The BFL pack polls `get_result` every 5 s for up to 40 attempts; generous
  `--timeout` values (600+) keep the runner from giving up first.
- **Failure mode to know:** on API errors / moderation, the BFL nodes return
  a black 512x512 image instead of raising — a solid-black output means
  "check the ComfyUI server log", not "the model drew night".
- Image outputs flow through `SaveImage`, so `run_workflow.py` downloads them
  into `--output-dir` as usual.
- Kontext-style editing requires the input image as a base64 string — that's
  what the `ImageToBase64_BFL` node in `bfl_kontext_edit.json` does; keep it
  in any edit workflow you author.

## Adding a new workflow

1. Design the graph in the ComfyUI web UI (`http://127.0.0.1:8188`) using the
   "BFL" nodes.
2. Export with **Workflow → Export (API)** — editor-format JSON will be
   rejected by the scripts.
3. Blank out the `FluxConfig_BFL.x_key` value.
4. Write a `<name>.schema.json` sidecar mapping friendly parameter names to
   `{"node_id": ..., "field": ...}` (copy an existing sidecar as a template).
5. Smoke it with the cheapest model first (`Flux2Klein4b_BFL`), then switch
   to Pro/Max for finals.
